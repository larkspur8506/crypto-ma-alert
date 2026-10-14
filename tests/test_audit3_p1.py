"""第三轮审计 P1 回归测试（高危 5 项）。

覆盖：
H1  effective_equity 接入 spot_ok / equity_total<=0 → 回落 equity_min（不再回落配置 equity）
H2  执行器告警落 alert_logs + 失败进补推（_retry_failed_alerts 放行 exec_alert）
H3  平仓 IOC 部分成交 → 剩余量重挂止损、本地保持 open
H4  run()/on_entry/on_exit 进程内互斥（_op_lock）
H5  live 下 on_entry 早期失败返回 True（不回落 MEXC 人工卡）；dry 仍返回 False

只依赖相对路径（CI 容器无 /opt/data）；不 import app.main。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB
from app.hl import MODE_DRY, MODE_LIVE, MODE_OFF
from app.hl.executor import Executor, ExecParams
from app.hl.exchange import STATUS_FILLED, STATUS_RESTING


def _snap(equity=1000.0, positions=(), orders=(), notional=0.0, ts=None, spot_ok=True):
    return {"ts": time.time() if ts is None else ts, "address": "0xabc", "equity": equity,
            "equity_total": equity, "withdrawable": equity, "spot_ok": spot_ok,
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
            from app.hl.info import HlInfoError
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
        return {"agents": [], "max_valid_until": 0}


class FakeExchange:
    def __init__(self, entry=None, stop=None, exit_=None):
        self.calls = []
        self.entry, self.stop, self.exit = entry, stop, exit_
        self._mid_gate = None          # None=正常；否则 mid() 抛它

    @property
    def available(self):
        return True

    @property
    def init_error(self):
        return ""

    def mid(self, coin):
        if self._mid_gate is not None:
            raise self._mid_gate
        return 83000.0 if coin == "BTC" else 2500.0

    def slippage_limit(self, coin, is_buy, mid=None):
        px = mid if mid else self.mid(coin)
        return px * (1 + 0.003) if is_buy else px * (1 - 0.003)

    def _cloid(self, symbol, bar_ms, purpose):
        return f"clid-{symbol}-{bar_ms}-{purpose}"

    def update_leverage(self, coin, leverage, cross=False):
        self.calls.append(("leverage", coin, leverage, cross))
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
        return {"ok": True, "status": STATUS_RESTING, "error": None, "oid": None,
                "avg_px": None, "filled_sz": None, "raw": None}


class FlakyNotifier:
    """send_message 第一次失败、之后成功 —— 用于验证 H2 补推。"""

    def __init__(self, fail_first=1):
        self.messages = []
        self.sent = 0
        self.fail_first = fail_first

    def send_message(self, text):
        self.sent += 1
        if self.sent <= self.fail_first:
            return False
        self.messages.append(text)
        return True


class SilentNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


class P3Base(unittest.TestCase):
    mode = MODE_LIVE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = SilentNotifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=self.mode, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, equity_min=20.0, risk_pct=0.02, leverage=5.0,
                            code_rev="audit3")

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self, **kw):
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def add_signal(self, symbol="BTC_USDT", side=1, entry=83000.0, stop=80600.0,
                   bar_ms=1791912000000):
        return self.db.create_position(symbol=symbol, signal_bar_ms=bar_ms, side=side,
                                       entry_price=entry, stop_price=stop,
                                       stop_dist=abs(entry - stop) / entry, contracts=1,
                                       coin_qty=0.001, notional=entry * 0.001,
                                       risk_u=20.0, equity_at_entry=1000.0)

    def open_pos(self, symbol="BTC_USDT", side=1, bar_ms=1791912000000):
        pid = self.add_signal(symbol=symbol, side=side, bar_ms=bar_ms)
        row = self.db.position_by_id(pid)
        self.db.set_pos_hl(pid, hl_status="open", hl_entry_oid=1, hl_stop_oid=99, hl_sz=0.005)
        self.db.confirm_position(pid, 83050.0, 0.005, 80600.0, True)
        self.db.hl_record_intent(symbol, "BTC", bar_ms, "stop", side, 0.005, px=80600.0,
                                 reduce_only=True, status="resting")
        return pid


class TestH1EffectiveEquitySpotOk(P3Base):
    """H1：spot_ok=False / equity_total<=0 → equity_min，绝不回落配置 1000U。"""

    def test_spot_not_ok_falls_to_equity_min(self):
        ex = self.executor()
        snap = _snap(equity=0.0, positions=(), spot_ok=False)
        snap["equity_total"] = 0.0
        self.assertEqual(ex.effective_equity(snap), 20.0)

    def test_zero_equity_total_falls_to_equity_min(self):
        ex = self.executor()
        self.assertEqual(ex.effective_equity({"equity_total": 0.0}), 20.0)

    def test_healthy_snapshot_still_follows(self):
        ex = self.executor()
        self.assertEqual(ex.effective_equity({"equity_total": 100.68, "spot_ok": True}), 100.0)


class TestH2AlertPersistence(P3Base):
    """H2：执行器告警落 alert_logs；推送失败可被 _retry_failed_alerts 语义补推。"""

    def test_alert_failure_logged_for_retry(self):
        self.notifier = FlakyNotifier(fail_first=1)
        ex = self.executor()
        ex._alert("【HL执行】测试告警：平仓失败")
        rows = [dict(r) for r in self.db.recent_alerts(10)
                if r["kind"] == "exec_alert"]
        self.assertTrue(rows, "告警应落 alert_logs")
        self.assertEqual(rows[0]["sent_successfully"], 0, "失败必须记 0 才能被补推")
        # 补推成功后标记（模拟 main._retry_failed_alerts 的行为）
        ok = self.notifier.send_message(rows[0]["message"])
        self.assertTrue(ok)
        self.db.mark_alert_sent(int(rows[0]["id"]))
        after = [dict(r) for r in self.db.recent_alerts(10) if r["kind"] == "exec_alert"][0]
        self.assertEqual(after["sent_successfully"], 1)

    def test_alert_success_logged(self):
        ex = self.executor()
        ex._alert("【HL执行】正常告警")
        rows = [dict(r) for r in self.db.recent_alerts(10) if r["kind"] == "exec_alert"]
        self.assertEqual(rows[0]["sent_successfully"], 1)

    def test_fill_alert_closed_switch_still_logs(self):
        self.p.notify_fills = False
        ex = self.executor()
        ex._alert_fill("【HL执行】✅ 已开仓（成交回报）")
        rows = [dict(r) for r in self.db.recent_alerts(10) if r["kind"] == "exec_alert_fills"]
        self.assertTrue(rows, "开关关闭也应落库（审计轨迹）")
        self.assertEqual(rows[0]["sent_successfully"], 1)

    def test_retry_whitelist_parsing(self):
        """main._retry_failed_alerts 的排除名单含 exec_alert_fills（读源码，不 import main）。"""
        main_py = Path(__file__).resolve().parent.parent / "app" / "main.py"
        src = main_py.read_text(encoding="utf-8")
        self.assertIn("exec_alert_fills", src)


class TestH3PartialFill(P3Base):
    """H3：平仓部分成交 → 剩余量重挂止损 + 本地保持 open。"""

    def test_partial_fill_rehangs_stop_and_stays_open(self):
        pid = self.open_pos()
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": 56, "avg_px": 84500.0,
                        "filled_sz": 0.004, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        ex = self.executor()
        self.assertTrue(ex.on_exit(pid, "ma20", 84400.0))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open", "部分成交不得整行关账")
        # 剩余量 0.001 重挂止损（trigger_px 非空）
        rehangs = [c for c in self.ex.calls if c[0] == "order" and c[6] is not None]
        self.assertTrue(rehangs, "应重挂止损")
        self.assertAlmostEqual(rehangs[-1][3], 0.001, places=6)
        self.assertTrue(any("部分成交" in m or "部分" in m for m in _notifier_msgs(self)))

    def test_partial_fill_rehang_fail_closes_remainder(self):
        pid = self.open_pos()
        # 第一发平仓部分成交；重挂止损失败；市价平剩余也失败 → 仓保持 open + 大声告警
        self.ex.stop = {"ok": False, "status": "error", "error": "trigger rejected",
                        "oid": None, "avg_px": None, "filled_sz": None, "raw": None}

        def _exit_seq(coin, is_buy, sz, limit_px):
            if not getattr(_exit_seq, "used", False):
                _exit_seq.used = True
                return {"ok": True, "status": STATUS_FILLED, "oid": 56, "avg_px": 84500.0,
                        "filled_sz": 0.004, "error": None, "raw": {}}
            return {"ok": False, "status": "error", "error": "reject",
                    "oid": None, "avg_px": None, "filled_sz": None, "raw": None}
        self.ex.exit = _exit_seq
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        ex = self.executor()
        self.assertTrue(ex.on_exit(pid, "ma20", 84400.0))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open", "平剩余失败时不得关账")


class TestH4OpLock(P3Base):
    """H4：Executor 有 _op_lock，run/on_entry/on_exit 串行化。"""

    def test_lock_exists_and_wraps(self):
        import inspect
        from app.hl import executor as E
        self.assertTrue(hasattr(Executor, "__init__"))
        src_init = inspect.getsource(E.Executor.__init__)
        self.assertIn("_op_lock", src_init)
        for m in ("run", "on_entry", "on_exit"):
            src = inspect.getsource(getattr(E.Executor, m))
            self.assertIn("self._op_lock", src, f"{m} 应包锁")

    def test_concurrent_run_is_serialized(self):
        """两个线程同时 run()，_snapshot 的读次数应串行不重叠（用事件序列验证）。"""
        order = []
        real_snapshot = Executor._snapshot

        def slow_snapshot(self, force=False):
            order.append("enter")
            time.sleep(0.15)
            order.append("leave")
            return real_snapshot(self, force)

        import app.hl.executor as E
        E.Executor._snapshot = slow_snapshot
        try:
            self.info._snap = _snap()
            ex = self.executor()
            t1 = threading.Thread(target=lambda: ex.run())
            t2 = threading.Thread(target=lambda: ex.run())
            t1.start(); t2.start()
            t1.join(); t2.join()
            # 串行 = 不出现 enter,enter,leave,leave
            bad = any(order[i] == "enter" and order[i + 1] == "enter"
                      for i in range(len(order) - 1))
            self.assertFalse(bad, f"run 未串行化：{order}")
        finally:
            E.Executor._snapshot = real_snapshot


class TestH5LiveNoFallback(P3Base):
    """H5：live 下 on_entry 早期失败返回 True（不回落人工卡）。"""

    def test_live_snapshot_failure_returns_true(self):
        pid = self.add_signal()
        self.info._raise = "info down"
        ex = self.executor()
        self.assertTrue(ex.on_entry(pid), "live 下已接管（发告警），不得回落人工卡")

    def test_live_mid_failure_returns_true(self):
        pid = self.add_signal()
        self.ex._mid_gate = RuntimeError("no mid")
        ex = self.executor()
        self.assertTrue(ex.on_entry(pid))

    def test_dry_snapshot_failure_still_returns_false(self):
        pid = self.add_signal()
        self.info._raise = "info down"
        self.p.mode = MODE_DRY
        ex = self.executor()
        self.assertFalse(ex.on_entry(pid), "dry 必须返回 False 让人工卡照发")


def _notifier_msgs(case):
    return getattr(case.notifier, "messages", []) or []


if __name__ == "__main__":
    unittest.main()
