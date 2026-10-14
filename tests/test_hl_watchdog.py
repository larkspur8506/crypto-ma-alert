"""app/hl/watchdog.py 的单元测试（离线）。

看守是「出事才发现」的最后一层，它自己必须可靠：
- 正常时**必须静默**（否则 cron 天天刷屏，人会开始忽略告警）
- 有问题时必须带 ⚠️ 前缀（Hermes cron 靠这个前缀决定推不推）
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.db import DB
from app.hl import MODE_DRY, MODE_LIVE, MODE_OFF
from app.hl import watchdog
from app.hl.info import HlInfoError

from tests.test_hl_executor import FakeInfo, _pos, _snap, _stop_order


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.info = FakeInfo(snap=_snap(positions=[], orders=[]))
        self.env = mock.patch.dict(os.environ, {"HL_ACCOUNT_ADDRESS": "0xabc"}, clear=False)
        self.env.start()
        self.skew = mock.patch.object(watchdog, "check_clock_skew", return_value=0.4)
        self.skew.start()

    def tearDown(self):
        self.skew.stop()
        self.env.stop()
        self.tmp.cleanup()

    def checks(self, mode=MODE_LIVE, force=False):
        return watchdog.run_checks(self.db, info=self.info, mode=mode, force=force)

    def _heartbeat(self, age_s=0.0):
        self.db.hl_log_event("heartbeat", "ok")
        if age_s:
            ev_id = self.db.hl_recent_events(1, kind="heartbeat")[0]["id"]
            with self.db.conn:
                self.db.conn.execute("UPDATE hl_events SET created_at=? WHERE id=?",
                                     (time.time() - age_s, ev_id))

    def _set_hl_status(self, pid, status):
        with self.db.conn:
            self.db.conn.execute("UPDATE positions SET hl_status=? WHERE id=?", (status, pid))

    @staticmethod
    def bad(notes):
        return [n for n in notes if n.startswith("⚠️")]


class TestBasic(Base):
    def test_off_mode_short_circuits(self):
        notes = self.checks(mode=MODE_OFF, force=True)
        self.assertEqual(len(notes), 1)
        self.assertIn("off 模式", notes[0])
        self.assertEqual(self.bad(notes), [])

    def test_healthy_state_has_no_warnings(self):
        self._heartbeat()
        self.assertEqual(self.bad(self.checks()), [])

    def test_force_shows_ok_items_too(self):
        self._heartbeat()
        notes = self.checks(force=True)
        self.assertTrue(any(n.startswith("· ") for n in notes))
        self.assertEqual(self.bad(notes), [])


class TestProblems(Base):
    def test_missing_heartbeat(self):
        self.assertIn("从未写过心跳", " ".join(self.bad(self.checks())))

    def test_stale_heartbeat(self):
        self._heartbeat()
        ev_id = self.db.hl_recent_events(1, kind="heartbeat")[0]["id"]
        with self.db.conn:
            self.db.conn.execute("UPDATE hl_events SET created_at=? WHERE id=?",
                                 (time.time() - 20 * 60, ev_id))
        self.assertIn("心跳超期", " ".join(self.bad(self.checks())))

    def test_kill_switch_reported_with_reason(self):
        self.db.hl_set_kill(True, "当日亏损熔断")
        joined = " ".join(self.bad(self.checks()))
        self.assertIn("kill switch 处于触发状态", joined)
        self.assertIn("当日亏损熔断", joined)            # 中文必须是可读的，不是 \uXXXX

    def test_stuck_intent_order(self):
        self.db.hl_record_intent("BTC_USDT", "BTC", 1791900000000, "entry", 1, sz=0.01)
        with self.db.conn:
            self.db.conn.execute("UPDATE hl_orders SET created_at=?", (time.time() - 600,))
        self.assertIn("卡住的意图单", " ".join(self.bad(self.checks())))

    def test_naked_position_without_stop(self):
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[])
        self.assertIn("没有止损挂单", " ".join(self.bad(self.checks())))

    def test_position_with_stop_is_fine(self):
        self._heartbeat()
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        self.assertEqual(self.bad(self.checks()), [])

    def test_local_open_missing_on_exchange(self):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1, side=1, entry_price=1.0,
                                      stop_price=0.9, stop_dist=0.1, contracts=1, coin_qty=1.0,
                                      notional=1.0, risk_u=1.0, equity_at_entry=100.0)
        self.db.confirm_position(pid, 1.0, 1, 0.9, True)
        self._set_hl_status(pid, "open")
        self.assertIn("交易所查不到该仓位", " ".join(self.bad(self.checks())))

    def test_mismatch_reported(self):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1, side=1, entry_price=1.0,
                                      stop_price=0.9, stop_dist=0.1, contracts=1, coin_qty=1.0,
                                      notional=1.0, risk_u=1.0, equity_at_entry=100.0)
        self._set_hl_status(pid, "mismatch")
        self.assertIn("mismatch", " ".join(self.bad(self.checks())))

    def test_recent_failures(self):
        self.db.hl_record_intent("BTC_USDT", "BTC", 1791900000000, "entry", 1, sz=0.01)
        self.db.hl_record_intent("ETH_USDT", "ETH", 1791900000000, "entry", 1, sz=0.1)
        rows = self.db.hl_recent_orders(10)
        self.assertTrue(rows)
        self.db.hl_update_order(rows[0]["symbol"], rows[0]["bar_ms"], rows[0]["purpose"],
                                status="failed", error="insufficient margin")
        self.assertIn("下单失败", " ".join(self.bad(self.checks())))

    def test_exchange_unreadable(self):
        self.info = FakeInfo(raise_on_snapshot="network down")
        self.assertIn("读不到 HL 账户", " ".join(self.bad(self.checks())))

    def test_missing_address_skips_exchange_checks(self):
        with mock.patch.dict(os.environ, {"HL_ACCOUNT_ADDRESS": ""}, clear=False):
            notes = self.checks(force=True)
        self.assertIn("未配置 HL_ACCOUNT_ADDRESS", " ".join(notes))

    def test_clock_skew_detected(self):
        self.skew.stop()
        self.skew = mock.patch.object(watchdog, "check_clock_skew", return_value=5.7)
        self.skew.start()
        self.assertIn("时间偏差", " ".join(self.bad(self.checks())))

    def test_clock_skew_unknown(self):
        self.skew.stop()
        self.skew = mock.patch.object(watchdog, "check_clock_skew", return_value=None)
        self.skew.start()
        self.assertIn("拿不到服务器时间", " ".join(self.bad(self.checks())))


class TestMain(Base):
    """main() 的输出契约：正常静默、异常带 ⚠️ 前缀。"""

    def _run(self, argv=None):
        buf = io.StringIO()
        with mock.patch("app.db.DB", return_value=self.db), \
             mock.patch("app.hl.info.HlInfo", return_value=self.info), \
             mock.patch.object(watchdog, "check_clock_skew", return_value=0.4), \
             contextlib.redirect_stdout(buf):
            rc = watchdog.main(argv or ["--mode", MODE_LIVE])
        return rc, buf.getvalue()

    def test_silent_when_healthy(self):
        self._heartbeat()
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_prints_when_problem(self):
        rc, out = self._run()                     # 没有心跳 → 有问题
        self.assertEqual(rc, 0)
        self.assertIn("HL 执行器看守告警", out)
        self.assertIn("从未写过心跳", out)

    def test_dry_run_lists_everything(self):
        rc, out = self._run(["--dry-run", "--mode", MODE_LIVE])
        self.assertEqual(rc, 0)
        self.assertIn("[dry-run]", out)
        self.assertIn("kill switch", out)


class TestAgentExpiry(Base):
    """agent（API 钱包）授权到期必须提前告警 —— 到期后所有下单会被拒。"""

    def test_warns_when_expiring_soon(self):
        self.db.hl_log_event("heartbeat", "ok")
        self.info.agent_status = lambda addr: {"agents": [{"name": "Hermes", "address": "0xc1ee", "valid_until": (time.time() + 3600) * 1000}], "max_valid_until": (time.time() + 3600) * 1000}
        joined = " ".join(self.bad(self.checks()))
        self.assertIn("agent 授权只剩", joined)

    def test_quiet_when_far_from_expiry(self):
        self._heartbeat()
        self.info.agent_status = lambda addr: {"agents": [{"name": "Hermes", "address": "0xc1ee", "valid_until": (time.time() + 30 * 86400) * 1000}], "max_valid_until": (time.time() + 30 * 86400) * 1000}
        self.assertEqual(self.bad(self.checks()), [])

    def test_warns_when_no_agent_at_all(self):
        self.db.hl_log_event("heartbeat", "ok")
        self.info.agent_status = lambda addr: {"agents": [], "max_valid_until": None}
        self.assertIn("没有任何 agent", " ".join(self.bad(self.checks())))

    def test_no_expiry_is_fine(self):
        self._heartbeat()
        self.info.agent_status = lambda addr: {"agents": [{"name": "Hermes", "address": "0xc1ee", "valid_until": None}], "max_valid_until": None}
        self.assertEqual(self.bad(self.checks()), [])


if __name__ == "__main__":
    unittest.main()
