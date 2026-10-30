"""feat/p1-copy 回归：开仓/错过信号提醒文案按**执行模式**分流。

痛点：信号提醒文案（engine.format_entry）里一直带着 MEXC 人工下单指引
（「下单：逐仓 5x / 单位选 USDT / 输入 X USDT → 点【开多】」「核对持仓价值 ≈ XU，约 N 张」），
以及按配置 equity（1000U）算出来的资金数字；而线上执行器已接管（mode=live，账户实际 ~100U）——
照着这些字段手动操作是误导，且风险行的权益口径与 HL 实际生效基数（跟随实时权益）不是一回事。

口径（本文件钉死）：
- 托管模式 = 调用方（engine._tick_symbol）传 executor_managed=True，即 `executor is not None`
  （mode=live/dry；mode=off 时 main.build_executor() 返回 None ⇒ 纯提醒模式）。
  文案保留：标题 / 入场 / 止损触发价 / 止损单计划 / 止盈不设 / 确认链接；
  MEXC 下单段整段退役 → 一行「▶ 系统将自动在 Hyperliquid 执行，成交后推送回报」；
  风险行不写具体 U 数 →「风险：单笔 X% 权益（实际以 HL 开仓回报为准）」；
  人工模式的「4 小时内自己市价平仓」也不出现（离场由 executor.on_exit 接管）。
- 人工模式（executor_managed 默认 False）= 旧行为**逐字符不变**，本文件用整串 literal 钉住，
  防止有人顺手「统一文案」把人工流改坏。
- format_missed 本就不引用 order（无 MEXC 张数/名义段），只把「不建议追」这句人工处置建议
  在托管模式下据实改成「系统不会自动追补」。

约束（照仓库约定）：不放本地绝对路径（CI 容器没有 /opt/data）；不 import app.main。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market, strategy                              # noqa: E402
from app.config import Config                                         # noqa: E402
from app.db import DB                                                 # noqa: E402

BAR_MS = 1791900000000          # 10-13 18:00 UTC = 10-14 02:00 北京时间


def _cfg(**kw) -> Config:
    c = Config()
    c.equity, c.risk_pct, c.leverage = 1000.0, 0.02, 5.0
    c.stop_min, c.stop_max = 0.015, 0.10
    c.token_secret = "test-secret"
    c.base_url = "https://ma.example.com"
    c.alert_prefix = "【双均线】"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _sig(side: int = 1) -> strategy.Signal:
    return strategy.Signal(symbol="BTC_USDT", bar_index=1, bar_open_ms=BAR_MS,
                           entry_price=83000.0, stop_price=80600.0, stop_dist=0.0289,
                           ma_max=83000.0, ma_min=80600.0, band=2400.0, side=side)


class _Order:
    """与 sizing 返回的 OrderPlan 同形状（只用到这几个字段）。"""

    contracts, coin_qty, notional, risk_u, margin = 1, 0.001, 83.0, 20.0, 16.6
    below_minimum = False


def _lines(text: str):
    return text.split("\n")


# 人工模式全文（= 本次改动前的线上输出，逐字符钉住）
MANUAL_ENTRY = "\n".join([
    "【双均线】BTC_USDT 做多信号（4h 收盘 10-14 02:00 北京时间）",
    "入场：市价（当前 83,000）",
    "止损触发价：80,600（距离 2.89%）",
    "下单：逐仓 5x / 市价 / 单位选 USDT / 输入 83 USDT → 点【开多】",
    "      （核对持仓价值 ≈ 83U，约 1 张）",
    "止损单：计划委托，触发价 80,600，卖出平仓，触发参考=最新成交价",
    "止盈：不设",
    "风险：20.0U（权益 1,000U × 2%），保证金(5x) 17U",
    "离场：收到本系统提醒后 4 小时内市价平仓",
])   # 2026-10-30 PR B：末行「确认开仓：<签名链接>」随确认页退役一并删除

MISSED_TAIL_MANUAL = "该信号已超过一根 4h，入场价已变、风险几何失效，不建议追。仅作记录。"
MISSED_TAIL_MANAGED = "该信号已超过一根 4h，入场价已变、风险几何失效，系统不会自动追补。仅作记录。"


# ================================================================ ① 托管模式
class TestManagedEntryCopy(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()
        self.sig = _sig()

    def test_replaces_mexc_guide_with_auto_exec_line(self):
        text = engine.format_entry(self.c, self.sig, _Order(), 7, executor_managed=True)
        self.assertIn("▶ 系统将自动在 Hyperliquid 执行，成交后推送回报", text)

    def test_has_no_mexc_manual_guidance_at_all(self):
        text = engine.format_entry(self.c, self.sig, _Order(), 7, executor_managed=True)
        for bad in ("单位选 USDT", "逐仓 5x", "输入 ", "点【开多】", "持仓价值", "已跳过张数建议"):
            self.assertNotIn(bad, text)
        # 「张」的数量段必须整段消失（不是换个说法）
        self.assertNotIn("张", text)

    def test_risk_line_has_no_u_amount_or_config_equity(self):
        text = engine.format_entry(self.c, self.sig, _Order(), 7, executor_managed=True)
        self.assertIn("风险：单笔 2.00% 权益（实际以 HL 开仓回报为准）", text)
        # 配置 equity 口径（1000U）与实际 U 数都不许出现
        self.assertNotIn("1,000U", text)
        self.assertNotIn("20.0U", text)
        self.assertNotIn("保证金", text)

    def test_keeps_structural_lines_in_order(self):
        text = engine.format_entry(self.c, self.sig, _Order(), 7, executor_managed=True)
        self.assertEqual(_lines(text), [
            "【双均线】BTC_USDT 做多信号（4h 收盘 10-14 02:00 北京时间）",
            "入场：市价（当前 83,000）",
            "止损触发价：80,600（距离 2.89%）",
            "▶ 系统将自动在 Hyperliquid 执行，成交后推送回报",
            "止损单：计划委托，触发价 80,600，卖出平仓，触发参考=最新成交价",
            "止盈：不设",
            "风险：单笔 2.00% 权益（实际以 HL 开仓回报为准）",
        ])

    def test_manual_exit_advice_not_present(self):
        text = engine.format_entry(self.c, self.sig, _Order(), 7, executor_managed=True)
        self.assertNotIn("4 小时内市价平仓", text)

    def test_short_side_stop_note_kept(self):
        text = engine.format_entry(self.c, _sig(side=-1), _Order(), 7, executor_managed=True)
        self.assertIn("止损触发价：80,600（距离 2.89%，在价格上方）", text)
        self.assertNotIn("单位选 USDT", text)

    def test_order_none_same_copy_as_order_present(self):
        """无 MEXC 段的标的：托管模式下无需降级提示（本来就不给张数）。"""
        c = _cfg()
        with_order = engine.format_entry(c, self.sig, _Order(), 7, executor_managed=True)
        without = engine.format_entry(c, self.sig, None, 7, executor_managed=True)
        self.assertEqual(without, with_order)
        self.assertNotIn("已跳过张数建议", without)

    def test_stale_routes_to_missed_managed(self):
        text = engine.format_entry(self.c, self.sig, _Order(), 7, stale=True, executor_managed=True)
        self.assertIn("错过的信号（补报）", text)
        self.assertIn(MISSED_TAIL_MANAGED, text)
        self.assertNotIn("不建议追", text)


# ================================================================ ② 人工模式（钉住不变）
class TestManualEntryCopyUnchanged(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()
        self.sig = _sig()

    def test_full_text_pinned(self):
        self.assertEqual(engine.format_entry(self.c, self.sig, _Order(), 7), MANUAL_ENTRY)

    def test_default_is_manual(self):
        """新参数必须是向后兼容默认（旧调用点/旧测试不炸）。"""
        self.assertEqual(engine.format_entry(self.c, self.sig, _Order(), 7, executor_managed=False),
                         MANUAL_ENTRY)

    def test_keeps_mexc_guidance_and_exit_advice(self):
        text = engine.format_entry(self.c, self.sig, _Order(), 7)
        for want in ("逐仓 5x", "单位选 USDT", "输入 83 USDT", "约 1 张",
                     "风险：20.0U（权益 1,000U × 2%）", "4 小时内市价平仓"):
            self.assertIn(want, text)

    def test_none_order_degrade_unchanged(self):
        text = engine.format_entry(self.c, self.sig, None, 7)
        self.assertIn("已跳过张数建议", text)
        self.assertNotIn("输入", text)
        self.assertNotIn("风险：", text)

    def test_below_minimum_note_unchanged(self):
        o = _Order()
        o.below_minimum = True
        text = engine.format_entry(self.c, self.sig, o, 7)
        self.assertIn("已退化到最小张数", text)


# ================================================================ ③ format_missed 两模式
class TestMissedCopyModes(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()
        self.sig = _sig()

    def test_manual_text_unchanged(self):
        self.assertEqual(_lines(engine.format_missed(self.c, self.sig)), [
            "【双均线】⚠️ 错过的信号（补报）",
            "BTC_USDT 在 10-14 02:00 北京时间的 4h 收盘触发做多（当时入场 83,000，止损 80,600）",
            MISSED_TAIL_MANUAL,
        ])

    def test_managed_tail_is_not_manual_advice(self):
        text = engine.format_missed(self.c, self.sig, executor_managed=True)
        self.assertIn(MISSED_TAIL_MANAGED, text)
        self.assertNotIn("不建议追", text)
        # 头两行（结构）两模式一致
        manual = engine.format_missed(self.c, self.sig)
        self.assertEqual(_lines(text)[:2], _lines(manual)[:2])

    def test_neither_mode_leaks_mexc_guidance(self):
        for text in (engine.format_missed(self.c, self.sig),
                     engine.format_missed(self.c, self.sig, executor_managed=True)):
            for bad in ("单位选 USDT", "逐仓", "张", "输入"):
                self.assertNotIn(bad, text)


# ================================================================ ④ 接线（tick 传参）
class FakeFeed:
    source = "test"
    bars = [strategy.Bar(open_ms=BAR_MS + i * market.INTERVAL_MS, open=100.0 + i, high=101.0 + i,
                         low=99.0 + i, close=100.5 + i, volume=1.0) for i in range(400)]


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


class DecliningExecutor:
    """在场但拒绝接管（dry/闸门拦截语义）：on_entry 返回 False ⇒ 引擎照发提醒卡。"""

    def __init__(self):
        self.runs = 0
        self.entries = []

    def run(self, **kw):
        self.runs += 1
        return []

    def on_entry(self, pid):
        self.entries.append(pid)
        return False

    def on_exit(self, pid, reason, ref):
        return False


class TestTickWiring(unittest.TestCase):
    """executor 在场/不在场必须真的把 executor_managed 传下去（接线最容易漏）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.cfg = _cfg()
        self.cfg.dry_run = False
        self.patches = [
            mock.patch.object(market, "fetch", return_value=FakeFeed()),
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

    def _walk(self, kind):
        def _w(cfg, symbol, bars, ind, sq, position, from_i, latest_i, latest_closed_i):
            if symbol != "BTC_USDT":
                return [], None
            ev = engine.Event(kind=kind, symbol="BTC_USDT", bar_ms=BAR_MS, is_latest=(kind == "entry"),
                              sig=_sig(), order=_Order())
            return [ev], None
        return _w

    def test_managed_tick_emits_managed_copy(self):
        ex = DecliningExecutor()
        with mock.patch.object(engine, "walk", side_effect=self._walk("entry")):
            sent = engine.tick(self.db, self.cfg, self.notifier, executor=ex)
        self.assertEqual(len(sent), 1)
        self.assertIn("自动在 Hyperliquid 执行", sent[0])
        self.assertNotIn("单位选 USDT", sent[0])
        self.assertNotIn("张", sent[0])

    def test_no_executor_tick_emits_manual_copy(self):
        with mock.patch.object(engine, "walk", side_effect=self._walk("entry")):
            sent = engine.tick(self.db, self.cfg, self.notifier)
        self.assertEqual(len(sent), 1)
        self.assertIn("单位选 USDT", sent[0])
        self.assertIn("约 1 张", sent[0])
        self.assertNotIn("自动在 Hyperliquid 执行", sent[0])

    def test_managed_tick_emits_managed_missed_copy(self):
        with mock.patch.object(engine, "walk", side_effect=self._walk("entry_missed")):
            sent = engine.tick(self.db, self.cfg, self.notifier, executor=DecliningExecutor())
        self.assertEqual(len(sent), 1)
        self.assertIn(MISSED_TAIL_MANAGED, sent[0])

    def test_manual_tick_emits_manual_missed_copy(self):
        with mock.patch.object(engine, "walk", side_effect=self._walk("entry_missed")):
            sent = engine.tick(self.db, self.cfg, self.notifier)
        self.assertEqual(len(sent), 1)
        self.assertIn(MISSED_TAIL_MANUAL, sent[0])


if __name__ == "__main__":
    unittest.main()
