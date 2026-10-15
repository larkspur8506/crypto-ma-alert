"""feat/registry-core 回归测试：单一标的注册表 + 信号链路优雅降级 + per-symbol 显性告警。

覆盖：
  a. registry：三标的登记齐、hl_coin 正确、未注册 get/hl_coin 抛 SymbolNotConfigured
  b. mexc_spec：三标的返回规格；「已注册但无 MEXC 段」→ None 不抛
  c. size_order：mexc_spec=None 的 symbol 返回 None 且不抛（不再 KeyError）
  d. format_entry / tick 降级：无 MEXC 段的标 → 信号照出、文案含「已跳过张数建议」
  e. tick per-symbol 异常 → 显性告警（带 symbol）+ 落 alert_logs + (symbol,类型) 窗口去重
  f. 现有 BTC/ETH/SOL 的 sizing / 文案断言不回归（SPECS shim 与原值一致）

约束：不放本地绝对路径（CI 容器没有 /opt/data）；不 import app.main。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market, strategy, symbol_registry as registry   # noqa: E402
from app.config import Config                                          # noqa: E402
from app.db import DB                                                  # noqa: E402
from app.sizing import SPECS, size_order                               # noqa: E402
from app.symbol_registry import (MexcSpec, SymbolEntry,                # noqa: E402
                                 SymbolNotConfigured)

THREE = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]


class FakeFeed:
    source = "test"

    def __init__(self, n=400):
        self.bars = [strategy.Bar(open_ms=1791900000000 + i * market.INTERVAL_MS,
                                  open=100.0 + i, high=101.0 + i, low=99.0 + i,
                                  close=100.5 + i, volume=1.0) for i in range(n)]


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


def _sig(symbol="BTC_USDT", side=1, entry=83000.0, stop=80600.0, bar_ms=1791900000000):
    return strategy.Signal(symbol=symbol, bar_index=1, bar_open_ms=bar_ms,
                           entry_price=entry, stop_price=stop,
                           stop_dist=abs(entry - stop) / entry,
                           ma_max=entry, ma_min=stop, band=entry - stop, side=side)


def _cfg(**kw):
    c = Config()
    c.equity, c.risk_pct, c.leverage = 1000.0, 0.02, 5.0
    c.stop_min, c.stop_max = 0.015, 0.10
    c.token_secret = "test-secret"
    c.base_url = "https://ma.example.com"
    c.alert_prefix = "【双均线】"
    c.dry_run = False
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ================================================================ a. registry
class TestRegistryBasics(unittest.TestCase):
    def test_known_symbols_has_three(self):
        self.assertEqual(registry.known_symbols(), THREE)

    def test_get_and_hl_coin_correct(self):
        for s, coin in [("BTC_USDT", "BTC"), ("ETH_USDT", "ETH"), ("SOL_USDT", "SOL")]:
            self.assertEqual(registry.get(s).symbol, s)
            self.assertEqual(registry.hl_coin(s), coin)

    def test_unregistered_get_raises(self):
        with self.assertRaises(SymbolNotConfigured):
            registry.get("DOGE_USDT")

    def test_unregistered_hl_coin_raises(self):
        with self.assertRaises(SymbolNotConfigured):
            registry.hl_coin("DOGE_USDT")

    def test_module_is_stdlib_only_no_side_effects(self):
        """registry 纯 stdlib：源码不得 import 网络/DB/行情类模块（import 无副作用）。"""
        import re
        src = Path(registry.__file__).read_text(encoding="utf-8")
        imports = re.findall(r"^\s*(?:from|import)\s+([\w\.]+)", src, re.M)
        forbidden = [m for m in imports
                     if m.split(".")[0] in {"app", "urllib", "socket", "sqlite3",
                                            "requests", "json", "os", "http"}]
        self.assertEqual(forbidden, [], f"registry 不该 import: {forbidden}")


# ================================================================ b. mexc_spec
class TestMexcSpec(unittest.TestCase):
    def test_three_symbols_return_specs(self):
        self.assertEqual(registry.mexc_spec("BTC_USDT"), MexcSpec(0.0001, 1, 1, 1))
        self.assertEqual(registry.mexc_spec("ETH_USDT"), MexcSpec(0.01, 1, 1, 2))
        self.assertEqual(registry.mexc_spec("SOL_USDT"), MexcSpec(0.1, 1, 1, 2))

    def test_registered_without_mexc_returns_none_not_raises(self):
        entry = SymbolEntry("XRP_USDT", "XRP", None)
        with mock.patch.dict(registry._REGISTRY, {"XRP_USDT": entry}):
            self.assertIsNone(registry.mexc_spec("XRP_USDT"))     # 不抛
            self.assertEqual(registry.hl_coin("XRP_USDT"), "XRP")  # 但元数据在

    def test_unregistered_returns_none(self):
        self.assertIsNone(registry.mexc_spec("DOGE_USDT"))         # 未注册也不抛


# ================================================================ c. size_order
class TestSizeOrderDegrade(unittest.TestCase):
    def test_unregistered_symbol_returns_none(self):
        self.assertIsNone(size_order("DOGE_USDT", 100.0, 95.0, 1000.0, 0.02))

    def test_registered_without_mexc_returns_none(self):
        entry = SymbolEntry("XRP_USDT", "XRP", None)
        with mock.patch.dict(registry._REGISTRY, {"XRP_USDT": entry}):
            self.assertIsNone(size_order("XRP_USDT", 1.0, 0.95, 1000.0, 0.02))

    def test_known_symbol_still_sizes(self):
        o = size_order("BTC_USDT", 83000.0, 80600.0, 1000.0, 0.02, 5.0)
        self.assertIsNotNone(o)
        self.assertEqual(o.symbol, "BTC_USDT")


# ================================================================ d. 降级文案
class TestFormatEntryDegrade(unittest.TestCase):
    def test_none_order_keeps_signal_and_notes_skipped(self):
        c = _cfg()
        text = engine.format_entry(c, _sig(), None, 7)
        self.assertIn("做多信号", text)                 # 信号照出
        self.assertIn("已跳过张数建议", text)           # 张数段降级
        self.assertIn("确认开仓", text)                 # 提醒不残缺
        self.assertNotIn("输入", text)                  # 不再给名义金额
        self.assertNotIn("风险：", text)                # 无法算风险/保证金

    def test_order_present_text_unchanged(self):
        c = _cfg()
        o = size_order("BTC_USDT", 83000.0, 80600.0, 1000.0, 0.02, 5.0)
        text = engine.format_entry(c, _sig(), o, 7)
        self.assertIn("逐仓 5x", text)
        self.assertIn("约", text)
        self.assertIn("风险：", text)
        self.assertNotIn("已跳过张数建议", text)


class TestTickDegrade(unittest.TestCase):
    """无 MEXC 段的标的：tick 全程不抛、信号照出、文案降级。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        engine._TICK_ERR_SENT_AT.clear()

    def tearDown(self):
        self.tmp.cleanup()
        engine._TICK_ERR_SENT_AT.clear()

    def test_tick_missing_spec_still_emits_signal(self):
        self.db.set_last_bar("BTC_USDT", 1791900000000)
        cfg = _cfg(symbols=["XRP_USDT"])
        sig = _sig(symbol="XRP_USDT")
        with mock.patch.object(market, "fetch", return_value=FakeFeed()), \
             mock.patch.object(strategy, "compute_indicators",
                               return_value={"sma200": [1.0] * 400, "band": [0.0] * 400}), \
             mock.patch.object(strategy, "squeeze_flags", return_value=[True] * 400), \
             mock.patch.object(strategy, "evaluate_entry", return_value=sig):
            sent = engine.tick(self.db, cfg, self.notifier, now_ms=1791900000000 + 400 * market.INTERVAL_MS)
        self.assertEqual(len(sent), 1, "信号提醒必须照发（不吞）")
        self.assertIn("已跳过张数建议", sent[0])
        self.assertIn("做多信号", sent[0])
        self.assertEqual(self.notifier.messages, sent)
        row = self.db.open_position("XRP_USDT")
        self.assertIsNotNone(row, "仓位行仍应建立")
        self.assertEqual(row["contracts"], 0)


# ================================================================ e. 显性告警
class TestTickSymbolErrorAlert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.cfg = _cfg(symbols=["BTC_USDT"])
        engine._TICK_ERR_SENT_AT.clear()

    def tearDown(self):
        self.tmp.cleanup()
        engine._TICK_ERR_SENT_AT.clear()

    def _boom_tick(self):
        with mock.patch.object(market, "fetch", side_effect=KeyError("'SPECS'")):
            return engine.tick(self.db, self.cfg, self.notifier, now_ms=1791900000000)

    def test_error_pushed_with_symbol_and_logged(self):
        sent = self._boom_tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("BTC_USDT", sent[0])
        self.assertIn("处理失败", sent[0])
        self.assertIn("KeyError", sent[0])
        self.assertEqual(len(self.notifier.messages), 1)
        rows = [dict(r) for r in self.db.recent_alerts(10) if r["kind"] == "tick_error"]
        self.assertTrue(rows, "告警必须落 alert_logs")
        self.assertEqual(rows[0]["symbol"], "BTC_USDT")
        self.assertEqual(rows[0]["sent_successfully"], 1)

    def test_same_symbol_same_type_deduped_within_window(self):
        self._boom_tick()
        self._boom_tick()                                   # 窗口内第二次
        self.assertEqual(len(self.notifier.messages), 1, "同类异常 2h 内只报一次（防刷屏）")
        rows = [r for r in self.db.recent_alerts(10) if r["kind"] == "tick_error"]
        self.assertEqual(len(rows), 1)

    def test_different_type_still_alerts(self):
        self._boom_tick()                                   # KeyError
        with mock.patch.object(market, "fetch", side_effect=ValueError("其它故障")):
            engine.tick(self.db, self.cfg, self.notifier, now_ms=1791900000000)
        self.assertEqual(len(self.notifier.messages), 2, "换了异常类型应重新告警")
        self.assertIn("ValueError", self.notifier.messages[1])

    def test_dry_run_does_not_push_or_log(self):
        self.cfg.dry_run = True
        sent = self._boom_tick()
        self.assertEqual(self.notifier.messages, [], "dry_run 不推送")
        self.assertTrue(sent)                               # 但仍进入 sent（与 _emit 一致）
        rows = [r for r in self.db.recent_alerts(10) if r["kind"] == "tick_error"]
        self.assertEqual(rows, [], "dry_run 不落库")


# ================================================================ f. 不回归
class TestNoRegression(unittest.TestCase):
    def test_specs_shim_matches_old_values(self):
        self.assertEqual(SPECS["BTC_USDT"], (0.0001, 1, 1, 1))
        self.assertEqual(SPECS["ETH_USDT"], (0.01, 1, 1, 2))
        self.assertEqual(SPECS["SOL_USDT"], (0.1, 1, 1, 2))

    def test_size_order_known_symbol_numbers_unchanged(self):
        # 与旧 SPECS 口径逐个对标：entry 83000 / stop 80600 / 1000U / 2% / 5x → 83 张
        o = size_order("BTC_USDT", 83000.0, 80600.0, 1000.0, 0.02, 5.0, side=1)
        self.assertEqual(o.contracts, 83)
        # 做空：止损在入场上方，距离同 2400 → 同样 83 张
        o2 = size_order("BTC_USDT", 83000.0, 85400.0, 1000.0, 0.02, 5.0, side=-1)
        self.assertEqual(o2.contracts, 83)

    def test_validations_still_raise(self):
        with self.assertRaises(ValueError):
            size_order("BTC_USDT", 100.0, 101.0, 1000.0, 0.02)      # 做多止损在上方
        with self.assertRaises(ValueError):
            size_order("BTC_USDT", 100.0, 99.0, 0.0, 0.02)          # equity<=0


if __name__ == "__main__":
    unittest.main()
