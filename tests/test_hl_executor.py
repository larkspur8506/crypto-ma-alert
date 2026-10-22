"""app/hl/executor.py 的单元测试（全部离线，注入假 info / 假 exchange）。

覆盖的是「真实会赔钱」的那些路径：闸门、止损挂失败、IOC 未成交、幂等、
离场、对账不一致、熔断、死手开关。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.db import DB
from app.hl import MODE_DRY, MODE_LIVE, MODE_OFF
from app.hl.executor import Executor, ExecParams
from app.hl import sizing as hl_sizing
from app.hl.exchange import STATUS_DRY, STATUS_ERROR, STATUS_FILLED, STATUS_RESTING, make_cloid
from app.hl.info import HlInfoError

DAY_MS = 4 * 3600 * 1000


def _snap(equity=1000.0, positions=(), orders=(), notional=0.0, ts=None):
    # ts 默认取「现在」：当日亏损熔断按 UTC 当日第一张快照做基线，
    # 快照时间戳写死会让测试只在某一天有效（CI 上就栽过一次）。
    return {"ts": time.time() if ts is None else ts, "address": "0xabc", "equity": equity,
            "withdrawable": equity,
            "margin_used": 100.0, "notional": notional,
            "positions": list(positions), "open_orders": list(orders), "orders_trusted": True}


def _pos(coin="BTC", size=0.01, entry=83000.0, side=1, upnl=0.0):
    return {"coin": coin, "side": side, "size": size, "entry_px": entry, "mark_px": entry + 50,
            "position_value": size * entry, "liq_px": 0.0, "leverage": 5, "isolated": True,
            "margin_used": size * entry / 5, "unrealized_pnl": upnl, "roe": 0.0,
            "funding_since_open": 0.0}


def _stop_order(coin="BTC", trigger=80600.0):
    return {"coin": coin, "oid": 99, "is_buy": False, "sz": 0.01, "limit_px": trigger,
            "reduce_only": True, "trigger_px": trigger, "is_trigger": True, "timestamp": 1}


class FakeInfo:
    def __init__(self, snap=None, mids=None, fills=None, specs=None, raise_on_snapshot=None):
        self._snap = snap or _snap()
        self._mids = mids if mids is not None else {"BTC": 83000.0, "ETH": 2500.0}
        self._fills = fills or []
        self._specs = specs or {"BTC": {"sz_decimals": 5, "max_leverage": 40, "only_isolated": False},
                                "ETH": {"sz_decimals": 4, "max_leverage": 25, "only_isolated": False}}
        self._raise = raise_on_snapshot

    def all_mids(self):
        return dict(self._mids)

    def snapshot(self, address, mids=None):
        if self._raise:
            raise HlInfoError(self._raise)
        return dict(self._snap)

    def specs(self, coins=None):
        return {k: v for k, v in self._specs.items() if coins is None or k in coins}

    def open_orders(self, address):
        return list(self._snap.get("open_orders") or [])

    def user_fills(self, address):
        return list(self._fills)

    def spot_state(self, address):
        return {"balances": [{"coin": "USDC", "total": "100.79", "hold": "0.0"}]}

    def agent_status(self, address):
        return {"agents": [{"name": "Hermes", "address": "0xc1ee4519ce893070c83cf90b324c6c8e5484be22",
                            "valid_until": 1807505841495}],
                "max_valid_until": 1807505841495}


class FakeExchange:
    """按脚本返回结果的假写通道，并记录所有调用。"""

    def __init__(self, entry=None, stop=None, exit_=None, available=True, dry_run=False):
        self.calls = []
        self.entry, self.stop, self.exit = entry, stop, exit_
        self.dry_run = dry_run
        self._available = available
        self.fail_leverage = False

    @property
    def available(self):
        return self._available

    @property
    def init_error(self):
        return "" if self._available else "缺少 account_address / private_key"

    # --- 只读
    def mid(self, coin):
        return 83000.0 if coin == "BTC" else 2500.0

    def slippage_limit(self, coin, is_buy, mid=None):
        px = mid if mid else self.mid(coin)
        return px * (1 + 0.003) if is_buy else px * (1 - 0.003)

    # --- 写
    def _cloid(self, symbol, bar_ms, purpose):
        return make_cloid(symbol, bar_ms, purpose)

    def update_leverage(self, coin, leverage, cross=False):
        self.calls.append(("leverage", coin, leverage, cross))
        if self.fail_leverage:
            return {"ok": False, "status": STATUS_ERROR, "error": "leverage rejected", "oid": None,
                    "avg_px": None, "filled_sz": None, "raw": None}
        return {"ok": True, "status": STATUS_RESTING, "error": None, "oid": None,
                "avg_px": None, "filled_sz": None, "raw": None}

    def place_order(self, coin, is_buy, sz, limit_px, reduce_only=False, tif="Ioc",
                    trigger_px=None, cloid=None):
        self.calls.append(("order", coin, is_buy, sz, limit_px, reduce_only, trigger_px, str(cloid)))
        if trigger_px is not None:
            res = self.stop
        elif reduce_only:
            res = self.exit
        else:
            res = self.entry
        if res is None:
            return {"ok": True, "status": STATUS_FILLED, "oid": 1, "avg_px": limit_px,
                    "filled_sz": sz, "error": None, "raw": {}}
        if callable(res):
            return res(coin, is_buy, sz, limit_px)
        return res

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        return {"ok": True, "status": STATUS_RESTING, "oid": oid, "error": None,
                "avg_px": None, "filled_sz": None, "raw": None}

    def schedule_cancel(self, time_ms=None, minutes=30):
        self.calls.append(("schedule_cancel", minutes))
        return {"ok": True, "status": STATUS_DRY if self.dry_run else STATUS_RESTING, "error": None,
                "oid": None, "avg_px": None, "filled_sz": None, "raw": {}}


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


class Base(unittest.TestCase):
    mode = MODE_LIVE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=self.mode, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="testrev")

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self, **kw):
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def add_signal(self, symbol="BTC_USDT", side=1, entry=83000.0, stop=80600.0, bar_ms=1791912000000):
        return self.db.create_position(symbol=symbol, signal_bar_ms=bar_ms, side=side,
                                       entry_price=entry, stop_price=stop,
                                       stop_dist=abs(entry - stop) / entry, contracts=1,
                                       coin_qty=0.001, notional=entry * 0.001,
                                       risk_u=20.0, equity_at_entry=1000.0)

    def orders(self, purpose=None):
        return [r for r in self.db.hl_recent_orders(50) if purpose is None or r["purpose"] == purpose]

    def events(self, kind=None):
        """DB 语义：最新在前。"""
        return [dict(r) for r in self.db.hl_recent_events(50, kind=kind)]

    def newest_event(self, kind=None):
        evs = self.events(kind)
        self.assertTrue(evs, "没有任何事件")
        return evs[0]


class TestModeOff(Base):
    mode = MODE_OFF

    def test_off_never_touches_exchange(self):
        pid = self.add_signal()
        ex = self.executor()
        self.assertFalse(ex.on_entry(pid))
        self.assertFalse(ex.on_exit(pid, "ma20", 83000.0))
        self.assertEqual(ex.run(), [])
        self.assertEqual(self.ex.calls, [])
        self.assertEqual(self.orders(), [])
        self.assertEqual(self.db.position_by_id(pid)["status"], "pending")   # 仍等人工确认


class TestDryMode(Base):
    mode = MODE_DRY

    def test_dry_records_plan_only(self):
        pid = self.add_signal()
        ex = self.executor()
        # dry 必须返回 False：人工提醒要照常发出（否则信号被吞、槽位被 pending 占住）
        self.assertFalse(ex.on_entry(pid))
        rows = self.orders("entry")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "planned")
        self.assertGreater(rows[0]["sz"], 0)
        self.assertEqual([c for c in self.ex.calls if c[0] == "order"], [])   # 没发单
        self.assertEqual(self.db.position_by_id(pid)["status"], "pending")
        self.assertTrue(any("dry" in e["message"] for e in self.events()))
        # 影子提醒里要有可核对的数字（数量/止损/风险/名义）
        shadow = self.notifier.messages[-1]
        self.assertIn("HL影子", shadow)
        self.assertIn("止损", shadow)
        self.assertIn("风险", shadow)

    def test_dry_without_credentials_still_plans(self):
        """影子期不该要求私钥：写通道不可用只在 live 才拦。"""
        self.ex = FakeExchange(available=False)
        pid = self.add_signal()
        self.assertFalse(self.executor().on_entry(pid))
        self.assertEqual(self.orders("entry")[0]["status"], "planned")
        self.assertIn("HL影子", self.notifier.messages[-1])

    def test_dry_never_closes_a_real_position(self):
        """模式从 live 退回 dry 时，交易所还有真实仓位 → 不能替引擎关本地账。"""
        pid = self.add_signal()
        self.executor().on_entry(pid)          # dry 下建的是 pending 行
        with self.db.conn:                     # 手工造一个「执行器开过的仓」
            self.db.conn.execute("UPDATE positions SET hl_status='open', hl_sz=0.01,"
                                 " status='open' WHERE id=?", (pid,))
        self.ex.calls.clear()
        ex = self.executor()
        self.assertTrue(ex.on_exit(pid, "ma20", 84500.0))     # 接管，但不动本地状态
        self.assertEqual(self.ex.calls, [])                   # 没发任何单
        self.assertEqual(self.db.position_by_id(pid)["status"], "open")
        self.assertIn("不是 live 模式", self.notifier.messages[-1])
        self.assertIn("人工平仓", self.notifier.messages[-1])


class TestEntryLive(Base):
    def test_happy_path_opens_and_places_stop(self):
        pid = self.add_signal()
        ex = self.executor()
        self.assertTrue(ex.on_entry(pid))
        kinds = [c[0] for c in self.ex.calls]
        self.assertEqual(kinds, ["leverage", "order", "order"])          # 杠杆 → 入场 → 止损
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["hl_status"], "open")
        self.assertEqual(row["stop_placed"], 1)
        self.assertGreater(row["hl_sz"], 0)
        self.assertIsNotNone(row["hl_entry_oid"])
        self.assertIsNotNone(row["hl_stop_oid"])
        # 止损单必须是 reduce_only + trigger + 反向
        stop_call = self.ex.calls[2]
        self.assertTrue(stop_call[5])                 # reduce_only
        self.assertAlmostEqual(stop_call[6], 80600.0)  # trigger_px = 信号止损价
        self.assertFalse(stop_call[2])                 # is_buy=False（平多）
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("已开仓", self.notifier.messages[0])
        self.assertEqual(self.orders("entry")[0]["status"], "filled")
        self.assertEqual(self.orders("stop")[0]["status"], "resting")

    def test_size_uses_live_price_and_risk_normalized(self):
        pid = self.add_signal(entry=83000.0, stop=80600.0)
        ex = self.executor()
        ex.on_entry(pid)
        entry_call = [c for c in self.ex.calls if c[0] == "order" and not c[5]][0]
        sz = entry_call[3]
        # 风险 20U / 止损距离 2.89% → 名义 ≈ 692U → 数量 ≈ 692/83000
        self.assertAlmostEqual(sz, 0.00833, places=5)
        self.assertAlmostEqual(sz * 83000.0, 691.4, delta=3.0)

    def test_unknown_credentials_fail_closed(self):
        self.ex = FakeExchange(available=False)
        pid = self.add_signal()
        ex = self.executor()
        self.assertTrue(ex.on_entry(pid))
        self.assertEqual(self.ex.calls, [])
        self.assertEqual(self.orders("entry")[0]["status"], "skipped")
        self.assertIn("闸门", self.newest_event()["message"])

    def test_kill_switch_blocks_entry(self):
        self.db.hl_set_kill(True, "test")
        pid = self.add_signal()
        self.assertTrue(self.executor().on_entry(pid))
        self.assertEqual(self.orders("entry")[0]["status"], "skipped")
        self.assertIn("kill switch", self.newest_event()["message"])

    def test_below_min_notional_blocks(self):
        pid = self.add_signal(entry=83000.0, stop=60000.0)   # 距离大 → 名义小
        self.p.equity, self.p.risk_pct = 10.0, 0.005         # 风险 0.05U → 名义 ~0.1U
        self.p.equity_mode = "fixed"                         # 本用例测闸门，不走跟随
        self.assertTrue(self.executor().on_entry(pid))
        self.assertEqual(self.orders("entry")[0]["status"], "skipped")
        self.assertIn("下限", self.newest_event()["message"])

    def test_existing_exchange_position_blocks(self):
        self.info = FakeInfo(snap=_snap(positions=[_pos("BTC")]))
        pid = self.add_signal()
        self.assertTrue(self.executor().on_entry(pid))
        self.assertIn("已有", self.newest_event()["message"])
        self.assertEqual([c for c in self.ex.calls if c[0] == "order"], [])

    def test_wide_stop_blocks(self):
        pid = self.add_signal(entry=83000.0, stop=60000.0)   # 27% 止损距离
        self.assertTrue(self.executor().on_entry(pid))
        self.assertIn("硬上限", self.newest_event()["message"])

    def test_daily_order_limit(self):
        self.p.max_orders_per_day = 1
        pid1 = self.add_signal(bar_ms=1791900000000)
        self.executor().on_entry(pid1)
        pid2 = self.add_signal(bar_ms=1791912000000)
        self.assertTrue(self.executor().on_entry(pid2))
        skipped = [r for r in self.orders("entry") if r["status"] == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("当日下单", self.newest_event()["message"])

    def test_leverage_failure_aborts(self):
        self.ex.fail_leverage = True
        pid = self.add_signal()
        self.assertTrue(self.executor().on_entry(pid))
        self.assertEqual(self.orders("entry")[0]["status"], "failed")
        self.assertIn("杠杆", self.orders("entry")[0]["error"])
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], None)

    def test_entry_error_aborts_and_alerts(self):
        self.ex = FakeExchange(entry={"ok": False, "status": STATUS_ERROR, "error": "insufficient margin",
                                      "oid": None, "avg_px": None, "filled_sz": None, "raw": None})
        pid = self.add_signal()
        self.assertTrue(self.executor().on_entry(pid))
        self.assertEqual(self.orders("entry")[0]["status"], "failed")
        self.assertIn("insufficient margin", self.orders("entry")[0]["error"])
        self.assertIn("开仓失败", self.notifier.messages[-1])

    def test_unfilled_ioc_is_cancelled_and_abandoned(self):
        self.ex = FakeExchange(entry={"ok": True, "status": STATUS_RESTING, "oid": 77, "avg_px": None,
                                      "filled_sz": None, "error": None, "raw": {}})
        pid = self.add_signal()
        self.assertTrue(self.executor().on_entry(pid))
        self.assertIn(("cancel", "BTC", 77), self.ex.calls)
        self.assertEqual(self.orders("entry")[0]["status"], "failed")
        self.assertIn("未成交", self.orders("entry")[0]["error"])
        self.assertEqual(self.db.position_by_id(pid)["status"], "pending")

    def test_stop_failure_closes_position_immediately(self):
        self.ex = FakeExchange(stop={"ok": False, "status": STATUS_ERROR, "error": "trigger rejected",
                                     "oid": None, "avg_px": None, "filled_sz": None, "raw": None})
        pid = self.add_signal()
        self.assertTrue(self.executor().on_entry(pid))
        # 平仓单 = reduce_only 且 trigger_px 为 None（触发单是那个失败的止损）
        closes = [c for c in self.ex.calls if c[0] == "order" and c[5] and c[6] is None]
        self.assertEqual(len(closes), 1)
        self.assertAlmostEqual(closes[0][3], 0.00833, places=5)
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "aborted")
        self.assertEqual(self.db.position_by_id(pid)["status"], "pending")   # 本地未标记持仓
        self.assertEqual(self.orders("stop")[0]["status"], "failed")
        self.assertIn("止损单挂失败", self.notifier.messages[-1])

    def test_idempotent_second_call(self):
        pid = self.add_signal()
        ex1 = self.executor()
        ex1.on_entry(pid)
        n_before = len(self.ex.calls)
        self.assertTrue(ex1.on_entry(pid))            # 再调一次
        self.assertEqual(len(self.ex.calls), n_before)  # 没有再发单
        self.assertEqual(len(self.orders("entry")), 1)


class TestExit(Base):
    def _open(self):
        """开一个仓并把本地状态置为 open（走真实 on_entry）。"""
        pid = self.add_signal()
        self.executor().on_entry(pid)
        return pid

    def test_exit_ma20_cancels_stop_and_closes(self):
        pid = self._open()
        self.ex.calls.clear()
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": 55, "avg_px": 84500.0,
                        "filled_sz": 0.00833, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        ex = self.executor()
        self.assertTrue(ex.on_exit(pid, "ma20", 84400.0))
        self.assertEqual([c[0] for c in self.ex.calls][:2], ["cancel", "order"])
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["hl_status"], "closed")
        self.assertAlmostEqual(row["exit_price"], 84500.0)
        risk = abs(float(row["actual_entry"]) - float(row["actual_stop"]))
        self.assertAlmostEqual(row["r_multiple"], (84500.0 - float(row["actual_entry"])) / risk, places=4)
        self.assertGreater(row["actual_entry"], 83000.0)   # 假交易所按滑点限价成交
        self.assertEqual(self.orders("exit")[0]["status"], "filled")

    def test_exit_when_stop_already_triggered(self):
        pid = self._open()
        self.ex.calls.clear()
        self.info._snap = _snap(positions=[])                     # 交易所已无仓位
        self.info._fills = [{"coin": "BTC", "px": "80610.5", "dir": "Close Short"}]
        self.assertTrue(self.executor().on_exit(pid, "stop", 80600.0))
        self.assertEqual(self.ex.calls, [])                        # 不再发任何单
        row = self.db.position_by_id(pid)
        self.assertEqual(row["exit_reason"], "stop")
        self.assertAlmostEqual(row["exit_price"], 80610.5)
        risk = abs(float(row["actual_entry"]) - float(row["actual_stop"]))
        self.assertAlmostEqual(row["r_multiple"], (80610.5 - float(row["actual_entry"])) / risk, places=4)

    def test_exit_close_failure_alerts_loudly(self):
        pid = self._open()
        self.ex.calls.clear()
        self.ex.exit = {"ok": False, "status": STATUS_ERROR, "error": "market closed", "oid": None,
                        "avg_px": None, "filled_sz": None, "raw": None}
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        self.assertTrue(self.executor().on_exit(pid, "ma20", 84400.0))
        self.assertEqual(self.db.position_by_id(pid)["status"], "open")   # 本地仍持仓
        self.assertIn("平仓失败", self.notifier.messages[-1])
        self.assertIn("止损单已撤", self.notifier.messages[-1])
        self.assertEqual(self.orders("exit")[0]["status"], "failed")

    def test_exit_skipped_when_not_executor_managed(self):
        pid = self.add_signal()                                   # 没有走 on_entry
        self.assertFalse(self.executor().on_exit(pid, "ma20", 83000.0))


class TestReconcileAndRun(Base):
    def test_reconcile_detects_missing_exchange_position(self):
        pid = self.add_signal()
        self.executor().on_entry(pid)
        self.info._snap = _snap(positions=[])                     # 交易所没了
        rec = self.executor().reconcile()
        # 2026-10-22 起首轮先进宽限（closing）；超时才判 mismatch（下面把两段都钉住）
        self.assertTrue(rec["ok"], "首轮只应进宽限")
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "closing")
        self.db.set_config(f"hl_close_grace:{pid}", f"{time.time() - 1000:.3f}:3")
        rec2 = self.executor().reconcile()
        self.assertFalse(rec2["ok"])
        self.assertIn("未拿到成交证据", rec2["problems"][0])
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "mismatch")

    def test_reconcile_detects_missing_stop(self):
        pid = self.add_signal()
        self.executor().on_entry(pid)
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[])   # 有仓无止损
        rec = self.executor().reconcile()
        self.assertFalse(rec["ok"])
        self.assertIn("没有止损挂单", " ".join(rec["problems"]))

    def test_reconcile_detects_orphan_exchange_position(self):
        self.info._snap = _snap(positions=[_pos("ETH")], orders=[])
        rec = self.executor().reconcile()
        self.assertFalse(rec["ok"])
        self.assertIn("本地无记录", " ".join(rec["problems"]))

    def test_reconcile_ok(self):
        pid = self.add_signal()
        self.executor().on_entry(pid)
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        self.assertTrue(self.executor().reconcile()["ok"])

    def test_run_writes_snapshot_and_heartbeat(self):
        self.executor().run(force_snapshot=True)
        self.assertIsNotNone(self.db.hl_latest_snapshot())
        self.assertEqual(len(self.events("heartbeat")), 1)
        self.assertEqual(len(self.events("reconcile")), 1)

    def test_run_mismatch_triggers_kill(self):
        pid = self.add_signal()
        self.executor().on_entry(pid)
        self.info._snap = _snap(positions=[])
        # 首轮：进宽限，不 kill（先给「成交还在路上」一个重试窗口）
        alerts = self.executor().run(force_snapshot=True)
        self.assertFalse(self.db.hl_kill(), "宽限期内不应 kill")
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "closing")
        self.assertTrue(any("进入宽限" in m for m in self.notifier.messages))
        # 宽限超时：必须 kill 并推「对账异常」（原意保留）
        self.db.set_config(f"hl_close_grace:{pid}", f"{time.time() - 1000:.3f}:3")
        alerts2 = self.executor().run(force_snapshot=True)
        self.assertTrue(self.db.hl_kill())
        self.assertTrue(any("对账异常" in a for a in alerts2))
        self.assertEqual(len(self.events("kill")), 1)

    def test_run_unknown_state_does_not_kill(self):
        self.info = FakeInfo(raise_on_snapshot="network down")
        alerts = self.executor().run(force_snapshot=True)
        self.assertFalse(self.db.hl_kill())
        # 快照读不到：降级心跳写入 + 一条提示（不再整轮静默 return，
        # 看守据此能把「读不到账户」和「执行器死了」区分开）
        self.assertEqual(len(alerts), 1)
        self.assertIn("读不到 HL 账户快照", alerts[0])
        self.assertTrue(any("读取账户失败" in e["message"] for e in self.events()))

    def test_run_daily_loss_kills(self):
        # 基线快照必须落在「今日窗口内」且早于当前快照。两个 ts 都相对当日 00:00 UTC 固定，
        # 不依赖测试运行时刻（写死绝对时间戳会让它只在某一天通过）。
        from app.hl.executor import _today_start_ts
        day0 = _today_start_ts()
        self.db.hl_add_snapshot({"ts": day0 + 1, "equity": 1000.0, "positions": []})
        self.info = FakeInfo(snap=_snap(equity=930.0, ts=day0 + 2))   # -7% < -5%
        alerts = self.executor().run(force_snapshot=True)
        self.assertTrue(self.db.hl_kill())
        self.assertIn("当日亏损", alerts[-1])
        self.assertEqual(self.db.get_config_str("hl_exec_kill_reason"), "当日亏损熔断")

    def test_run_does_not_schedule_deadman_by_default(self):
        """死手开关默认关闭（见 docs/HL_RUNBOOK.md：它会连止损一起撤）。"""
        self.executor().run(force_snapshot=True)
        self.assertNotIn(("schedule_cancel", 30), self.ex.calls)

    def test_run_schedules_deadman_when_explicitly_enabled(self):
        self.executor(use_schedule_cancel=True).run(force_snapshot=True)
        self.assertIn(("schedule_cancel", 30), self.ex.calls)

    def test_run_skips_deadman_when_killed(self):
        self.db.hl_set_kill(True, "test")
        self.executor().run(force_snapshot=True)
        self.assertNotIn(("schedule_cancel", 30), self.ex.calls)


if __name__ == "__main__":
    unittest.main()


class TestAlertWording(Base):
    """推送文案里用 HL 合约名（BTC/ETH），别混着信号层的 MEXC 名。"""

    def test_entry_and_exit_alerts_use_coin_names(self):
        pid = self.add_signal()
        self.executor().on_entry(pid)
        self.assertIn("已开仓 · BTC 做多", self.notifier.messages[0])
        self.assertNotIn("BTC_USDT", self.notifier.messages[0])
        self.ex.calls.clear()
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": 55, "avg_px": 84500.0,
                        "filled_sz": 0.00833, "error": None, "raw": {}}
        self.info = FakeInfo(snap=_snap(positions=[_pos("BTC")], orders=[_stop_order()]))
        self.executor().on_exit(pid, "ma20", 84400.0)
        self.assertIn("BTC 做多", self.notifier.messages[-1])
        self.assertNotIn("BTC_USDT", self.notifier.messages[-1])

    def test_stop_failure_alert_uses_coin(self):
        self.ex = FakeExchange(stop={"ok": False, "status": STATUS_ERROR, "error": "trigger rejected",
                                     "oid": None, "avg_px": None, "filled_sz": None, "raw": None})
        pid = self.add_signal()
        self.executor().on_entry(pid)
        self.assertIn("BTC 止损单挂失败", self.notifier.messages[-1])


class TestFillSync(Base):
    """成交必须同步进 hl_fills —— 否则面板的「最近成交/手续费/今日与累计已实现」永远是 0。"""

    def test_open_fill_synced_and_deduped(self):
        self.info._fills = [{"tid": 11, "coin": "BTC", "dir": "Open Long", "sz": "0.00014",
                             "px": "83500.0", "fee": "0.00526", "feeToken": "USDC",
                             "closedPnl": "0.0", "hash": "0xh1", "time": 1791955000000}]
        self.executor().run(force_snapshot=True)
        rows = self.db.hl_recent_fills(10)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["coin"], "BTC")
        self.assertEqual(r["symbol"], "BTC_USDT")       # 反向映射回信号层符号
        self.assertEqual(r["side"], 1)
        self.assertAlmostEqual(r["fee"], 0.00526)
        self.assertAlmostEqual(r["ts"], 1791955000)     # 毫秒 → 秒
        self.executor().run(force_snapshot=True)         # 再跑一轮：不重复
        self.assertEqual(len(self.db.hl_recent_fills(10)), 1)

    def test_close_fill_marks_reduce_only_and_pnl(self):
        self.info._fills = [{"tid": 12, "coin": "BTC", "dir": "Close Long", "sz": "0.00014",
                             "px": "83542.0", "fee": "0.0052", "feeToken": "USDC",
                             "closedPnl": "0.0011", "hash": "0xh2", "time": 1791956000000}]
        self.executor().run(force_snapshot=True)
        r = self.db.hl_recent_fills(10)[0]
        self.assertEqual(r["reduce_only"], 1)
        self.assertAlmostEqual(r["closed_pnl"], 0.0011)
        self.assertEqual(r["side"], 1)

    def test_short_fill_side_is_minus_one(self):
        self.info._fills = [{"tid": 13, "coin": "ETH", "dir": "Open Short", "sz": "0.01",
                             "px": "2500.0", "fee": "0.001", "closedPnl": "0", "time": 1791957000000}]
        self.executor().run(force_snapshot=True)
        self.assertEqual(self.db.hl_recent_fills(10)[0]["side"], -1)

    def test_no_fills_is_noop(self):
        self.executor().run(force_snapshot=True)
        self.assertEqual(self.db.hl_recent_fills(10), [])


class TestAlertFormat(Base):
    """推送文案是给人看的：开仓要有价格/仓位/保证金/强平价，平仓要说清原因与盈亏。"""

    def test_entry_alert_layout(self):
        from app.hl.executor import fmt_entry_alert
        import datetime as dt
        txt = fmt_entry_alert(coin="BTC", side=1, sz=0.00095, entry_px=83545.0, stop=81456.0,
                              stop_dist=0.025, notional=79.4, margin=15.9, liq=67720.0,
                              risk_u=2.0, equity=100.0, signal_px=83544.5, oid=111, stop_oid=222,
                              when=dt.datetime(2026, 10, 14, 13, 35), tz_label="北京时间")
        for want in ("已开仓", "BTC 做多", "5x 逐仓", "成交价", "83,545.00", "数量", "0.00095 BTC",
                     "止损价", "81,456.00", "-2.50%", "保证金", "强平价", "67,720.00",
                     "风险", "2.00 U", "滑点", "时间", "单号"):
            self.assertIn(want, txt, f"开仓推送缺 {want}")
        self.assertLess(txt.index("成交价"), txt.index("止损价"))   # 顺序：价格在前

    def test_entry_alert_short_stop_is_positive(self):
        from app.hl.executor import fmt_entry_alert
        import datetime as dt
        txt = fmt_entry_alert(coin="ETH", side=-1, sz=0.01, entry_px=2500.0, stop=2600.0,
                              stop_dist=0.04, notional=25.0, margin=5.0, liq=2900.0,
                              risk_u=2.0, equity=100.0, signal_px=2500.0, oid=1, stop_oid=2,
                              when=dt.datetime(2026, 10, 14, 13, 35), tz_label="北京时间")
        self.assertIn("ETH 做空", txt)
        self.assertIn("+4.00%", txt)          # 空单止损在上方

    def test_exit_alert_says_why_and_pnl(self):
        from app.hl.executor import fmt_exit_alert
        import datetime as dt
        now = dt.datetime(2026, 10, 14, 13, 35)
        txt = fmt_exit_alert(coin="BTC", side=1, entry_px=83545.0, exit_px=81456.0, sz=0.00095,
                             reason="stop", r_multiple=-0.998, gross_pnl=-1.98, fees=0.07,
                             opened_at=now.timestamp() - 3600, when=now, tz_label="北京时间")
        for want in ("止损离场", "止损单被触发", "平仓价", "81,456.00", "开仓 83,545.00",
                     "本次盈亏", "-2.05 U", "-1.00R", "手续费", "持仓时长", "1 小时"):
            self.assertIn(want, txt, f"平仓推送缺 {want}")

    def test_exit_alert_profit_is_marked_as_tp(self):
        from app.hl.executor import fmt_exit_alert
        import datetime as dt
        now = dt.datetime(2026, 10, 14, 13, 35)
        txt = fmt_exit_alert(coin="BTC", side=1, entry_px=83545.0, exit_px=86000.0, sz=0.00095,
                             reason="ma20", r_multiple=1.0, gross_pnl=2.33, fees=0.07,
                             opened_at=now.timestamp() - 90, when=now, tz_label="北京时间")
        self.assertIn("策略止盈", txt)
        self.assertIn("收盘跌破 SMA20", txt)
        self.assertIn("+2.26 U", txt)                     # 净盈亏已扣手续费
        self.assertIn("1 分钟", txt)

    def test_exit_alert_short_reason_wording(self):
        from app.hl.executor import fmt_exit_alert
        import datetime as dt
        now = dt.datetime(2026, 10, 14, 13, 35)
        txt = fmt_exit_alert(coin="ETH", side=-1, entry_px=2500.0, exit_px=2450.0, sz=0.01,
                             reason="ma20", r_multiple=0.5, gross_pnl=0.5, fees=0.02,
                             opened_at=now.timestamp(), when=now, tz_label="北京时间")
        self.assertIn("收盘站上 SMA20", txt)              # 空单是站上


class TestEffectiveEquity(Base):
    """下单基数跟随实时权益（用户拍板：只在低位设下限，不设上限）。"""

    def _ex(self, **kw):
        return self.executor(**kw)

    def test_follow_uses_live_equity_rounded_to_10(self):
        ex = self._ex(equity_mode="follow", equity=100.0)
        self.assertEqual(ex.effective_equity({"equity_total": 100.68}), 100.0)
        self.assertEqual(ex.effective_equity({"equity_total": 108.9}), 100.0)
        self.assertEqual(ex.effective_equity({"equity_total": 111.0}), 110.0)
        # 入金到 1000U：无需改配置，直接按 1000 算（用户明确「上限没必要」）
        self.assertEqual(ex.effective_equity({"equity_total": 1000.0}), 1000.0)
        self.assertEqual(ex.effective_equity({"equity_total": 2500.0}), 2500.0)

    def test_follow_respects_lower_bound_only(self):
        ex = self._ex(equity_mode="follow", equity_min=20.0)
        self.assertEqual(ex.effective_equity({"equity_total": 5.0}), 20.0)     # 低于下限 → 抬到下限
        # 审计三轮 P1-H1：读到 0/不可信不再回落配置 equity（会把 100U 账户放大成
        # 1000U 定仓）—— 同样 fail-closed 回落 equity_min
        self.assertEqual(ex.effective_equity({"equity_total": 0.0}), 20.0)
        # 现货读取失败（统一账户 spot_ok=False）→ equity_total 不可信 → 回落 equity_min
        self.assertEqual(ex.effective_equity({"equity_total": 90.0, "spot_ok": False}), 20.0)

    def test_fixed_mode_ignores_live(self):
        ex = self._ex(equity_mode="fixed", equity=100.0)
        self.assertEqual(ex.effective_equity({"equity_total": 9999.0}), 100.0)

    def test_no_snapshot_falls_back_to_config(self):
        ex = self._ex(equity_mode="follow", equity=100.0)
        self.assertEqual(ex.effective_equity(None), 100.0)

    def test_sizing_follows_live_equity(self):
        """权益 1000 → 单笔风险 20U；止损距离 ~2.89% → 名义 ≈ 692U。"""
        self.info = FakeInfo(snap=_snap(equity=1000.0))
        pid = self.add_signal(entry=83000.0, stop=80600.0)
        ex = self.executor(equity=100.0, equity_mode="follow", equity_min=20.0)
        ex.on_entry(pid)                                       # 走真路径（假交易所）
        row = self.orders("entry")[0]
        self.assertGreater(row["sz"], 0.008)                   # 比按 100U 算(0.00083)大一个数量级
        self.assertAlmostEqual(row["sz"], 0.00833, places=5)   # 按 1000U 权益算出来的量

    def test_base_change_is_recorded(self):
        ex = self._ex(equity_mode="follow", equity=100.0)
        ex.effective_equity({"equity_total": 1000.0})          # 100 → 1000（+900）
        self.assertTrue(any("下单基数" in e["message"] for e in self.events()))
        self.assertAlmostEqual(float(self.db.get_config_str("hl_last_equity_base")), 1000.0)

    def test_small_change_not_recorded(self):
        ex = self._ex(equity_mode="follow", equity=100.0)
        ex.effective_equity({"equity_total": 1000.0})
        n = len(self.events())
        ex.effective_equity({"equity_total": 1010.0})          # +1% → 不记
        self.assertEqual(len(self.events()), n)

    def test_notional_caps_default_off(self):
        """名义上限默认 0 = 不设限（用户拍板）：1000U 账户紧止损 + 已有持仓，也不能被卡。"""
        self.info = FakeInfo(snap=_snap(equity=1000.0))
        pid = self.add_signal(entry=83000.0, stop=80600.0)
        ex = self.executor(equity=1000.0, max_notional_mult=0.0, max_total_notional_mult=0.0)
        plan = hl_sizing.size_order_hl("BTC", 5, 83000.0, 80600.0, 1000.0, 0.02, 5.0, side=1)
        snap = self.info.snapshot("0xabc")
        snap["notional"] = 5000.0                     # 已有 5 倍权益的持仓也不拦
        gates = ex._entry_gates("BTC_USDT", plan, snap, equity=1000.0)
        self.assertEqual([g for g in gates if "上限" in g], [])

    def test_notional_caps_still_work_when_explicitly_set(self):
        """留成可选（>0 生效）：将来要给暴露度加护栏，改配置即可。"""
        self.info = FakeInfo(snap=_snap(equity=1000.0))
        ex = self.executor(equity=1000.0, max_notional_mult=0.5, max_total_notional_mult=0.5)
        plan = hl_sizing.size_order_hl("BTC", 5, 83000.0, 80600.0, 1000.0, 0.02, 5.0, side=1)
        gates = ex._entry_gates("BTC_USDT", plan, self.info.snapshot("0xabc"), equity=1000.0)
        self.assertTrue([g for g in gates if "上限" in g])


class TestFillPushSwitch(Base):
    """面板「成交回报推送」开关（fill_push）必须真管用；异常提醒不受它影响。"""

    def test_off_suppresses_success_alert_but_still_records(self):
        pid = self.add_signal(entry=83000.0, stop=80600.0)
        self.executor(notify_fills=False).on_entry(pid)
        self.assertEqual(self.notifier.messages, [], "成交回报应被开关关掉")
        self.assertTrue(any("开仓完成" in e["message"] for e in self.events()),
                        "关掉推送不影响落库与面板")

    def test_on_still_alerts(self):
        pid = self.add_signal(entry=83000.0, stop=80600.0)
        self.executor(notify_fills=True).on_entry(pid)
        self.assertTrue(any("已开仓" in m for m in self.notifier.messages))

    def test_failure_alert_is_never_gated(self):
        """止损单挂失败属于异常提醒：开关关掉也必须喊（这是最危险的那条）。"""
        self.ex = FakeExchange(stop={"ok": False, "status": STATUS_ERROR, "error": "trigger rejected",
                                     "oid": None, "avg_px": None, "filled_sz": None, "raw": None})
        pid = self.add_signal()
        self.executor(notify_fills=False).on_entry(pid)
        self.assertTrue(any("止损单挂失败" in m for m in self.notifier.messages),
                        self.notifier.messages)

    def test_exit_alert_respects_switch(self):
        pid = self.add_signal(entry=83000.0, stop=80600.0)
        ex = self.executor(notify_fills=False)
        ex.on_entry(pid)
        self.notifier.messages.clear()
        ex.on_exit(pid, "ma20", 86000.0)
        self.assertEqual(self.notifier.messages, [])


class TestDeadmanLogDedupe(Base):
    """死手开关失败必须去重：低成交量账户每轮都失败，否则 live 下刷爆事件表。"""

    def _ex(self):
        self.ex.schedule_cancel = lambda minutes=60: {"ok": False, "error": "Cannot set scheduled cancel"}
        return self.executor(use_schedule_cancel=True)

    def test_failure_logged_once(self):
        ex = self._ex()
        for _ in range(4):
            ex.run()
        n = len([e for e in self.events() if "死手开关设置失败" in e["message"]])
        self.assertEqual(n, 1, f"应只记一条，实际 {n}")

    def test_recovery_is_reported(self):
        ex = self._ex()
        ex.run()
        self.ex.schedule_cancel = lambda minutes=60: {"ok": True, "error": ""}
        ex.run()
        self.assertTrue(any("死手开关恢复可用" in e["message"] for e in self.events()))
        # 恢复后再失败，应再记一条（因为状态变了）
        self.ex.schedule_cancel = lambda minutes=60: {"ok": False, "error": "又失败"}
        ex.run()
        n = len([e for e in self.events() if "死手开关设置失败" in e["message"]])
        self.assertEqual(n, 2)


class TestDeadmanOff(Base):
    """死手开关默认关闭：绝不能碰交易所，也绝不能拖累后面主流程（成交同步/心跳）。"""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.ex.schedule_cancel = lambda minutes=30: (self.calls.append(minutes) or
                                                      {"ok": False, "error": "should not be called"})

    def test_default_off_never_calls_exchange(self):
        self.executor().run()                     # 默认 use_schedule_cancel=False
        self.assertEqual(self.calls, [], "默认关闭时不该调用 scheduleCancel")
        self.assertEqual([e for e in self.events() if "死手开关" in e["message"]], [])

    def test_off_does_not_block_rest_of_cycle(self):
        ex = self.executor()
        ex.run()
        self.assertTrue(any(e["kind"] == "heartbeat" for e in self.events()),
                        "主流程（心跳/成交同步）必须照常走完")

    def test_exception_in_deadman_never_escapes(self):
        """即使显式开启且接口抛异常，也绝不能把 run() 打断。"""
        def boom(minutes=30):
            raise RuntimeError("网络炸了")
        self.ex.schedule_cancel = boom
        ex = self.executor(use_schedule_cancel=True)
        ex.run()                                   # 不抛异常 = 通过
        self.assertTrue(any("死手开关设置失败" in e["message"] for e in self.events()))
        self.assertTrue(any(e["kind"] == "heartbeat" for e in self.events()))

    def test_enabled_still_works(self):
        self.ex.schedule_cancel = lambda minutes=30: (self.calls.append(minutes) or
                                                      {"ok": True, "error": ""})
        self.executor(use_schedule_cancel=True).run()
        self.assertEqual(len(self.calls), 1)


class TestStopProtection(Base):
    """止损保护（用户拍板的验收点）：

    - 读不到触发单（挂单接口回落）→ **只提示、绝不动作**（否则会误平仓）
    - **确认**有仓没止损 → 先自动补挂原价；补不上就市价平掉（不允许无保护持仓）
    """

    def _open_position(self, clear_calls=True):
        pid = self.add_signal()
        self.executor().on_entry(pid)
        if clear_calls:
            self.ex.calls.clear()            # 只数「对账/修复」这一轮下的单
        return pid

    def test_untrusted_orders_never_acts(self):
        pid = self._open_position()
        snap = _snap(positions=[_pos("BTC")], orders=[])          # 读不到触发单
        snap["orders_trusted"] = False
        self.info._snap = snap
        before = len(self.ex.calls)
        alerts = self.executor().run(force_snapshot=True)
        self.assertFalse(self.db.hl_kill(), "不可信读数不得拉 kill")
        self.assertEqual(len(self.ex.calls), before, "不可信读数不得下任何单")
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "open")
        self.assertTrue(any("无法确认止损" in a for a in alerts))
        self.assertTrue(any("无法确认止损" in e["message"] for e in self.events()))

    def test_untrusted_notice_deduped_then_recovery_reported(self):
        self._open_position()
        snap = _snap(positions=[_pos("BTC")], orders=[])
        snap["orders_trusted"] = False
        self.info._snap = snap
        n1 = len([a for a in self.executor().run(force_snapshot=True) if "无法确认止损" in a])
        n2 = len([a for a in self.executor().run(force_snapshot=True) if "无法确认止损" in a])
        self.assertEqual((n1, n2), (1, 0), "同一状态只喊一次")
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        alerts = self.executor().run(force_snapshot=True)
        self.assertTrue(any("已恢复" in a for a in alerts), "恢复要报一次")

    def test_confirmed_missing_stop_is_auto_repaired(self):
        pid = self._open_position()
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[])   # 可信 + 确实没止损
        self.executor().run(force_snapshot=True)
        triggers = [c for c in self.ex.calls if c[0] == "order" and c[6] is not None]
        self.assertEqual(len(triggers), 1, "应当补挂一张触发单")
        self.assertTrue(triggers[0][5], "补挂必须是 reduce_only")
        self.assertAlmostEqual(triggers[0][6], 80600.0, places=2)      # 用原止损价
        self.assertIn("已自动补挂", " ".join(self.notifier.messages))
        self.assertIn("已自动补挂", " ".join(e["message"] for e in self.events()))
        self.assertTrue(self.db.hl_kill(), "异常已发生 → 暂停自动交易等人工核对")
        self.assertTrue(self.db.position_by_id(pid)["hl_stop_oid"], "新单号要落库")

    def test_repair_failure_closes_position(self):
        pid = self._open_position()
        self.ex.stop = {"ok": False, "status": STATUS_ERROR, "error": "trigger rejected",
                        "oid": None, "avg_px": None, "filled_sz": None, "raw": None}
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[])
        alerts = self.executor().run(force_snapshot=True)
        closes = [c for c in self.ex.calls if c[0] == "order" and c[5] and c[6] is None]
        self.assertGreaterEqual(len(closes), 1, "补挂失败 → 必须市价平掉，不留裸仓")
        self.assertIn("补挂失败", " ".join(self.notifier.messages))
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "closed")

    def test_repair_without_params_shouts_without_acting(self):
        pid = self._open_position()
        self.db.conn.execute("UPDATE positions SET stop_price=0 WHERE id=?", (pid,))
        self.db.conn.commit()
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[])
        before = len(self.ex.calls)
        alerts = self.executor().run(force_snapshot=True)
        self.assertEqual(len(self.ex.calls), before, "参数不全时不要瞎下单")
        self.assertTrue(any("未能自动处理" in msg for msg in self.notifier.messages))
