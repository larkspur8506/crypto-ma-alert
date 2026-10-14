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
from app.hl.exchange import STATUS_DRY, STATUS_ERROR, STATUS_FILLED, STATUS_RESTING, make_cloid
from app.hl.info import HlInfoError

DAY_MS = 4 * 3600 * 1000


def _snap(equity=1000.0, positions=(), orders=(), notional=0.0, ts=None):
    # ts 默认取「现在」：当日亏损熔断按 UTC 当日第一张快照做基线，
    # 快照时间戳写死会让测试只在某一天有效（CI 上就栽过一次）。
    return {"ts": time.time() if ts is None else ts, "address": "0xabc", "equity": equity,
            "withdrawable": equity,
            "margin_used": 100.0, "notional": notional,
            "positions": list(positions), "open_orders": list(orders)}


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
        return {"agent_address": "0x3f7aaf0334c7ab4baaf36d6f5860389ce742d1f3",
                "agent_valid_until": 1793162156561}


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
        self.assertFalse(rec["ok"])
        self.assertIn("本地有仓", rec["problems"][0])
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
        alerts = self.executor().run(force_snapshot=True)
        self.assertTrue(self.db.hl_kill())
        self.assertTrue(any("对账异常" in a for a in alerts))
        self.assertEqual(len(self.events("kill")), 1)

    def test_run_unknown_state_does_not_kill(self):
        self.info = FakeInfo(raise_on_snapshot="network down")
        alerts = self.executor().run(force_snapshot=True)
        self.assertFalse(self.db.hl_kill())
        self.assertEqual(alerts, [])
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

    def test_run_schedules_deadman_switch_when_live(self):
        self.executor().run(force_snapshot=True)
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
        self.assertIn("BTC 已开仓", self.notifier.messages[0])
        self.assertNotIn("BTC_USDT", self.notifier.messages[0])
        self.ex.calls.clear()
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": 55, "avg_px": 84500.0,
                        "filled_sz": 0.00833, "error": None, "raw": {}}
        self.info = FakeInfo(snap=_snap(positions=[_pos("BTC")], orders=[_stop_order()]))
        self.executor().on_exit(pid, "ma20", 84400.0)
        self.assertIn("BTC 已平仓", self.notifier.messages[-1])
        self.assertNotIn("BTC_USDT", self.notifier.messages[-1])

    def test_stop_failure_alert_uses_coin(self):
        self.ex = FakeExchange(stop={"ok": False, "status": STATUS_ERROR, "error": "trigger rejected",
                                     "oid": None, "avg_px": None, "filled_sz": None, "raw": None})
        pid = self.add_signal()
        self.executor().on_entry(pid)
        self.assertIn("BTC 止损单挂失败", self.notifier.messages[-1])
