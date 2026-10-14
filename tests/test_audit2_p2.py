"""第二轮审计 P2 回归测试。

覆盖：
1. watchdog 模式判定：DB 优先于 env（面板切 live 而 env=dry/off 时不再误判）。
2. watchdog agent 告警不再 ⚠️ 双写。
3. run() 读不到快照仍写心跳（看守不误报执行器死）。
4. 对账异常告警去重（6h 节流）+ 恢复时报告并清标记。
5. dry 模式 _repair_missing_stop 不动交易所。
6. reconcile 认 hl_status=NULL 的仓（开仓中途崩溃场景）。
7. 开仓推送：equity 用生效基数、leverage 不再硬编码 5x。
8. _prune_alert_logs / prune_hl_telemetry 走 @_locked 方法。
9. apply_db 范围校验：stop_min>=stop_max、leverage=0、出界值全部拒收。
10. pending TTL 文案：写「约 8 小时」而非「3 根 4h」。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB
from app.config import Config


def _tmp_db():
    import tempfile
    return DB(os.path.join(tempfile.mkdtemp(), "t.db"))


class TestWatchdogMode(unittest.TestCase):
    def test_mode_db_overrides_env(self):
        from app.hl import watchdog
        db = _tmp_db()
        db.set_config("hl_exec_mode", "live")
        with mock.patch.dict(os.environ, {"HL_EXEC_MODE": "dry"}):
            # 直接测 main 的模式解析段逻辑：复刻 main() 内的判定
            raw = str(db.get_config().get("hl_exec_mode", "") or watchdog._env("HL_EXEC_MODE", "off"))
            mode = raw.strip().strip('"')
            self.assertEqual(mode, "live")

    def test_mode_falls_back_to_env(self):
        from app.hl import watchdog
        db = _tmp_db()
        with mock.patch.dict(os.environ, {"HL_EXEC_MODE": "dry"}):
            raw = str(db.get_config().get("hl_exec_mode", "") or watchdog._env("HL_EXEC_MODE", "off"))
            self.assertEqual(raw.strip().strip('"'), "dry")

    def test_agent_alert_no_double_prefix(self):
        from app.hl import watchdog
        src = open(watchdog.__file__, encoding="utf-8").read()
        # add() 自身加 ⚠️；消息文本里不得再手写 ⚠️
        self.assertNotIn('add("⚠️', src.replace('add(msg', ''))
        self.assertNotIn('add(f"⚠️', src.replace('add(msg', ''))


class TestRunHeartbeatOnSnapshotFail(unittest.TestCase):
    def test_degraded_heartbeat_written(self):
        from app.hl.executor import Executor, ExecParams
        db = _tmp_db()
        ex = Executor(db=db,
                      params=ExecParams(mode="live", account_address="0xa",
                                       private_key="0x" + "22" * 32),
                      info=mock.Mock(), exchange=mock.Mock(), notifier=mock.Mock())
        ex._snapshot = lambda force=False: None
        alerts = ex.run()
        # 心跳事件确实落库（看守判活依据）
        last = db.hl_recent_events(1, kind="heartbeat")
        self.assertEqual(len(last), 1)
        self.assertIn("降级", last[0]["message"])
        self.assertTrue(any("读不到" in a for a in alerts))


class TestReconcileAlertDedup(unittest.TestCase):
    def _mk(self):
        from app.hl.executor import Executor, ExecParams
        db = _tmp_db()
        ex = Executor(db=db,
                      params=ExecParams(mode="live", account_address="0xa",
                                       private_key="0x" + "22" * 32),
                      info=mock.Mock(), exchange=mock.Mock(), notifier=mock.Mock())
        return db, ex

    def test_alert_throttled(self):
        db, ex = self._mk()
        # 交易所有仓、本地无记录 → 对账异常
        snap = {"positions": [{"coin": "BTC", "size": 0.01, "entry_px": 100.0}],
                "orders_trusted": True, "open_orders": [], "equity_total": 100.0}
        ex._snapshot = lambda force=False: dict(snap)
        a1 = ex.run()
        self.assertTrue(any("对账异常" in a for a in a1))
        # 第二轮：6h 内不再推
        a2 = ex.run()
        self.assertFalse(any("对账异常" in a for a in a2))
        # 事件仍然每轮记录（审计痕迹不受节流影响）
        evs = [e for e in db.hl_recent_events(10, kind="reconcile") if "对账异常" in (e["message"] or "")]
        self.assertGreaterEqual(len(evs), 2)

    def test_recovery_reported_once(self):
        db, ex = self._mk()
        snap_bad = {"positions": [{"coin": "BTC", "size": 0.01, "entry_px": 100.0}],
                    "orders_trusted": True, "open_orders": [], "equity_total": 100.0}
        snap_ok = {"positions": [], "orders_trusted": True, "open_orders": [], "equity_total": 100.0}
        ex._snapshot = lambda force=False: dict(snap_bad)
        ex.run()
        ex._snapshot = lambda force=False: dict(snap_ok)
        a = ex.run()
        self.assertTrue(any("对账已恢复正常" in x for x in a))
        # 标记已清：再坏会立即推
        ex._snapshot = lambda force=False: dict(snap_bad)
        a2 = ex.run()
        self.assertTrue(any("对账异常" in x for x in a2))


class TestDryRepairNoop(unittest.TestCase):
    def test_dry_does_not_touch_exchange(self):
        from app.hl.executor import Executor, ExecParams
        db = _tmp_db()
        ex = Executor(db=db,
                      params=ExecParams(mode="dry", account_address="0xa",
                                       private_key="0x" + "22" * 32),
                      info=mock.Mock(), exchange=mock.Mock(), notifier=mock.Mock())
        ex.ex.place_order = mock.Mock()
        ex._repair_missing_stop({"symbol": "BTC_USDT", "signal_bar_ms": 1, "side": 1,
                                 "stop_price": 97.0, "hl_sz": 0.01, "id": 1}, {})
        ex.ex.place_order.assert_not_called()
        evs = db.hl_recent_events(5)
        self.assertTrue(any("不动交易所" in (e["message"] or "") for e in evs))


class TestReconcileNullHlStatus(unittest.TestCase):
    def test_null_hl_status_counts_as_local_open(self):
        from app.hl.executor import Executor, ExecParams
        db = _tmp_db()
        pid = db.create_position(
            symbol="BTC_USDT", signal_bar_ms=1700000000000, side=1,
            entry_price=100.0, stop_price=97.0, stop_dist=0.03,
            contracts=10, coin_qty=0.01, notional=1000.0, risk_u=2.0,
            equity_at_entry=100.0)
        db.confirm_position(pid, 100.0, 0.01, 97.0, True)
        # 不写 hl_status（模拟 on_entry 中途崩溃）
        ex = Executor(db=db,
                      params=ExecParams(mode="live", account_address="0xa",
                                       private_key="0x" + "22" * 32),
                      info=mock.Mock(), exchange=mock.Mock(), notifier=mock.Mock())
        snap = {"positions": [{"coin": "BTC", "size": 0.01, "entry_px": 100.0}],
                "orders_trusted": True, "open_orders": [], "equity_total": 100.0}
        rec = ex.reconcile(snap)
        # 该仓进入 missing_stop（有仓无止损）而不是被当人工单
        self.assertEqual(len(rec["missing_stop"]), 1)


class TestEntryAlertEquity(unittest.TestCase):
    def test_equity_and_leverage_params(self):
        import inspect
        from app.hl import executor as E
        sig = inspect.signature(E.fmt_entry_alert)
        self.assertIn("leverage", sig.parameters)
        # equity 传的是生效基数（调用点核对）
        src = inspect.getsource(E.Executor.on_entry)
        self.assertIn("equity=eff_equity", src)
        self.assertNotIn("equity=self.p.equity", src)

    def test_pct_zero_equity_safe(self):
        from app.hl.executor import fmt_entry_alert
        import datetime as dt
        txt = fmt_entry_alert(coin="BTC", side=1, sz=0.001, entry_px=100.0, stop=97.0,
                              stop_dist=0.03, notional=100.0, margin=20.0, liq=80.0,
                              risk_u=2.0, equity=0.0, signal_px=100.0, oid=1, stop_oid=2,
                              when=dt.datetime.now(), tz_label="北京时间", leverage=5.0)
        self.assertIn("权益的 0.0%", txt)   # 不抛 ZeroDivisionError
        self.assertIn("5x", txt)


class TestPruneMethods(unittest.TestCase):
    def test_prune_alert_logs_locked(self):
        db = _tmp_db()
        db.log_alert(None, None, "heartbeat_local", "x", True)
        n = db.prune_alert_logs(keep_days=0.0, keep_rows=5000)
        self.assertIsNone(n)
        # bar_ms=NULL 的行按 keep_days=0 被清
        rows = db.conn.execute("SELECT count(*) FROM alert_logs WHERE bar_ms IS NULL").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_prune_hl_telemetry(self):
        db = _tmp_db()
        db.hl_log_event("heartbeat", "h1")
        db.hl_log_event("error", "e1")
        old = time.time() - 40 * 86400
        db.conn.execute("UPDATE hl_events SET created_at=?", (old,))
        db.conn.commit()
        db.prune_hl_telemetry(keep_days=30.0)
        kinds = [r["kind"] for r in db.hl_recent_events(50)]
        self.assertNotIn("heartbeat", kinds)   # 心跳超期被清
        self.assertIn("error", kinds)          # 告警类不按天清


class TestApplyDbValidation(unittest.TestCase):
    def test_stop_min_ge_max_rejected(self):
        c = Config()
        c.stop_min, c.stop_max = 0.015, 0.1
        c.apply_db({"stop_min": "0.1", "stop_max": "0.05"})
        self.assertEqual((c.stop_min, c.stop_max), (0.015, 0.1),
                         "stop_min>=stop_max 的组合必须整体拒收")

    def test_leverage_zero_rejected(self):
        c = Config()
        c.apply_db({"leverage": "0"})
        self.assertEqual(c.leverage, 5.0)

    def test_out_of_range_rejected(self):
        c = Config()
        c.apply_db({"risk_pct": "5"})       # 500%
        self.assertEqual(c.risk_pct, 0.02)
        c.apply_db({"equity": "-100"})
        self.assertEqual(c.equity, 1000.0)

    def test_valid_values_pass(self):
        c = Config()
        c.apply_db({"stop_min": "0.02", "stop_max": "0.08", "leverage": "10",
                    "risk_pct": "0.03", "equity": "100"})
        self.assertEqual((c.stop_min, c.stop_max, c.leverage, c.risk_pct, c.equity),
                         (0.02, 0.08, 10.0, 0.03, 100.0))

    def test_string_and_bool_unaffected(self):
        c = Config()
        c.apply_db({"equity_mode": "fixed", "enable_shorts": "0", "heartbeat_push": "1"})
        self.assertEqual(c.equity_mode, "fixed")
        self.assertFalse(c.enable_shorts)
        self.assertTrue(c.heartbeat_push)


class TestPendingTtlWording(unittest.TestCase):
    def test_ttl_text_hours(self):
        from app import engine
        src = open(engine.__file__, encoding="utf-8").read()
        self.assertIn("信号发出约", src)
        self.assertIn("小时未确认", src)
        self.assertNotIn("信号超过 {PENDING_BARS_MAX} 根 4h", src)


if __name__ == "__main__":
    unittest.main()
