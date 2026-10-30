"""引擎 ↔ 执行器的接线测试（feat/hl-executor）。

`engine.walk` 本身由 tests/test_engine.py 覆盖，这里**故意把它打桩**，只验证接线：
- 执行器接管后不再发「人工下单/平仓」提醒卡
- 执行器返回 False（未接管）时，原来的提醒路径必须完好
- 执行器抛异常不能让 tick 崩掉（否则提醒服务会被执行器的 bug 连坐）
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import engine, market, strategy
from app.config import Config
from app.db import DB


class FakeFeed:
    source = "test"

    def __init__(self, n=400):
        self.bars = [strategy.Bar(open_ms=1791900000000 + i * market.INTERVAL_MS, open=100.0 + i,
                                  high=101.0 + i, low=99.0 + i, close=100.5 + i, volume=1.0)
                     for i in range(n)]


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


class FakeExecutor:
    """记录接管调用；按脚本决定是否接管。"""

    def __init__(self, take_entry=True, take_exit=True, boom=False):
        self.take_entry, self.take_exit, self.boom = take_entry, take_exit, boom
        self.entries, self.exits, self.runs = [], [], 0

    def run(self, **kw):
        self.runs += 1
        if self.boom:
            raise RuntimeError("executor run boom")
        return []

    def on_entry(self, pid):
        self.entries.append(pid)
        if self.boom:
            raise RuntimeError("executor entry boom")
        return self.take_entry

    def on_exit(self, pid, reason, ref):
        self.exits.append((pid, reason, ref))
        if self.boom:
            raise RuntimeError("executor exit boom")
        return self.take_exit


def _sig(side=1):
    return strategy.Signal(symbol="BTC_USDT", bar_index=1, bar_open_ms=1791900000000,
                           entry_price=83000.0, stop_price=80600.0, stop_dist=0.0289,
                           ma_max=83000.0, ma_min=82000.0, band=1000.0, side=side)


def _order():
    class O:
        contracts, coin_qty, notional, risk_u, margin = 1, 0.001, 83.0, 20.0, 16.6
    return O()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.cfg = Config()
        self.cfg.dry_run = False
        self.notifier = FakeNotifier()
        self.feed = FakeFeed()
        self.patches = [
            mock.patch.object(market, "fetch", return_value=self.feed),
            mock.patch.object(strategy, "compute_indicators",
                              return_value={"sma200": [1.0] * 400, "band": [0.0] * 400}),
            mock.patch.object(strategy, "squeeze_flags", return_value=[True] * 400),
            mock.patch.object(strategy, "evaluate_entry", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def walk_stub(self, events_by_symbol):
        """按标的返回事件：tick 会遍历多个标的，桩必须区分，否则会出现假事件。"""
        def _walk(cfg, symbol, bars, ind, sq, position, from_i, latest_i, latest_closed_i):
            evs = events_by_symbol.get(symbol, [])
            rest = [e for e in evs if e.kind != "entry_exit_placeholder"]
            new_pos = None
            if any(e.kind == "entry" for e in rest) and position is None:
                new_pos = engine.PositionState(symbol=symbol, entry_price=83000.0, stop_price=80600.0,
                                               contracts=1, signal_bar_ms=1791900000000)
            return rest, new_pos
        return _walk

    def _entry_event(self, is_latest=True):
        ev = engine.Event(kind="entry" if is_latest else "entry_missed", symbol="BTC_USDT",
                          bar_ms=1791900000000, is_latest=is_latest, sig=_sig(), order=_order())
        return ([ev], engine.PositionState(symbol="BTC_USDT", entry_price=83000.0,
                                           stop_price=80600.0, contracts=1,
                                           signal_bar_ms=1791900000000))

    def _exit_event(self, reason="ma20"):
        ev = engine.Event(kind="exit", symbol="BTC_USDT", bar_ms=1791900000000, is_latest=True,
                          exit_price=84000.0, reason=reason, bars_held=3)
        return ([ev], None)


class TestEntryHook(Base):
    def test_executor_takes_entry_suppresses_manual_alert(self):
        ex = FakeExecutor()
        evs, _ = self._entry_event()
        stub = self.walk_stub({"BTC_USDT": evs})
        with mock.patch.object(engine, "walk", side_effect=stub):
            sent = engine.tick(self.db, self.cfg, self.notifier, executor=ex)
        self.assertEqual(len(ex.entries), 1)
        self.assertEqual(ex.runs, 1)
        self.assertEqual(sent, [])                       # 没发人工下单卡
        self.assertEqual(self.notifier.messages, [])
        row = self.db.position_by_id(ex.entries[0])
        self.assertEqual(row["status"], "pending")       # 执行器负责改状态

    def test_executor_declining_falls_back_to_manual_alert(self):
        ex = FakeExecutor(take_entry=False)
        evs, _ = self._entry_event()
        stub = self.walk_stub({"BTC_USDT": evs})
        with mock.patch.object(engine, "walk", side_effect=stub):
            sent = engine.tick(self.db, self.cfg, self.notifier, executor=ex)
        self.assertEqual(len(ex.entries), 1)
        self.assertEqual(len(sent), 1)                   # 原提醒路径完好
        # feat/p1-copy：执行器**在场**（live/dry）时提醒文案走托管口径 —— 不再给
        # MEXC 人工下单指引（逐仓/单位选 USDT/张数），因为执行器已接管、这些数字
        # 按配置 equity 算出来是误导。执行器拒绝接管只影响「谁来开仓」，不影响
        # 文案分流（文案分流只看 executor 是否为 None）。旧断言 assertIn("下单") 随之失效。
        self.assertIn("自动在 Hyperliquid 执行", sent[0])
        self.assertNotIn("单位选 USDT", sent[0])
        self.assertEqual(len(self.notifier.messages), 1)

    def test_no_executor_keeps_old_behaviour(self):
        evs, _ = self._entry_event()
        stub = self.walk_stub({"BTC_USDT": evs})
        with mock.patch.object(engine, "walk", side_effect=stub):
            sent = engine.tick(self.db, self.cfg, self.notifier)
        self.assertEqual(len(sent), 1)
        self.assertIn("确认开仓", sent[0])

    def test_exception_from_executor_does_not_break_tick(self):
        ex = FakeExecutor(boom=True)
        evs, _ = self._entry_event()
        stub = self.walk_stub({"BTC_USDT": evs})
        with mock.patch.object(engine, "walk", side_effect=stub):
            sent = engine.tick(self.db, self.cfg, self.notifier, executor=ex)
        self.assertEqual(len(sent), 1)                   # 异常被吞，提醒照发
        row = self.db.position_by_id(ex.entries[0])
        self.assertEqual(row["status"], "pending")

    def test_dry_mode_sends_shadow_alert_AND_manual_card(self):
        """影子期的核心契约：既要记录「若实盘会怎么下」，又必须保留人工操作卡。"""
        from app.hl import MODE_DRY
        from app.hl.executor import ExecParams, Executor
        from tests.test_hl_executor import FakeExchange, FakeInfo

        p = ExecParams(mode=MODE_DRY, account_address="0xabc", private_key="", equity=1000.0,
                       # 本文件测的是引擎钩子的布线（dry 不许吞信号），不建模信号时效；
                       # 时效闸由 tests/test_hl_signal_age.py 专测。
                       max_signal_age_min=0)
        ex = Executor(self.db, p, FakeInfo(), FakeExchange(dry_run=True), notifier=self.notifier)
        evs, _ = self._entry_event()
        stub = self.walk_stub({"BTC_USDT": evs})
        with mock.patch.object(engine, "walk", side_effect=stub):
            sent = engine.tick(self.db, self.cfg, self.notifier, executor=ex)

        self.assertEqual(len(sent), 1, "人工操作卡必须照发（dry 不许吞信号）")
        self.assertIn("确认开仓", sent[0])
        self.assertTrue(any("HL影子" in m for m in self.notifier.messages),
                        "应推送一条影子提醒，供逐条比对")
        # 影子期不发任何真实委托，仓位仍停在 pending 等人工确认
        self.assertEqual([c for c in ex.ex.calls if c[0] == "order"], [])
        pid = self.db.recent_positions(1)[0]["id"]
        self.assertEqual(self.db.position_by_id(pid)["status"], "pending")
        planned = [r for r in self.db.hl_recent_orders(10) if r["status"] == "planned"]
        self.assertEqual(len(planned), 1)


class TestExitHook(Base):
    def test_executor_takes_exit_suppresses_manual_alert(self):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1791900000000, side=1,
                                      entry_price=83000.0, stop_price=80600.0, stop_dist=0.0289,
                                      contracts=1, coin_qty=0.001, notional=83.0, risk_u=20.0,
                                      equity_at_entry=1000.0)
        self.db.confirm_position(pid, 83000.0, 1, 80600.0, True)
        ex = FakeExecutor()
        evs, _ = self._exit_event()
        stub = self.walk_stub({"BTC_USDT": evs})
        with mock.patch.object(engine, "walk", side_effect=stub):
            sent = engine.tick(self.db, self.cfg, self.notifier, executor=ex)
        self.assertEqual(len(ex.exits), 1)
        self.assertEqual(sent, [])
        self.assertEqual(self.db.position_by_id(pid)["status"], "open")   # 执行器负责关账

    def test_executor_declining_exit_keeps_manual_alert(self):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1791900000000, side=1,
                                      entry_price=83000.0, stop_price=80600.0, stop_dist=0.0289,
                                      contracts=1, coin_qty=0.001, notional=83.0, risk_u=20.0,
                                      equity_at_entry=1000.0)
        self.db.confirm_position(pid, 83000.0, 1, 80600.0, True)
        ex = FakeExecutor(take_exit=False)
        evs, _ = self._exit_event()
        stub = self.walk_stub({"BTC_USDT": evs})
        with mock.patch.object(engine, "walk", side_effect=stub):
            sent = engine.tick(self.db, self.cfg, self.notifier, executor=ex)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.db.position_by_id(pid)["status"], "closed")   # 走原路径
        self.assertEqual(self.db.position_by_id(pid)["auto_closed"], 1)


if __name__ == "__main__":
    unittest.main()
