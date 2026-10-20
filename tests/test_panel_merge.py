"""面板合并：把「三标的动态」与「为什么没开仓」合成一个板块（feat/panel-merge-dynamics）。

为什么合并：两块回答同一个问题（现在能不能动），分开渲染时同一屏出现两个现价（缓存 vs
现取，数字还不一样）、两套状态词（「均线未密集」/「均线不密集」）、两处密集根数，
而闸门正常时整列都是「放行」，纯噪音。

断言全部针对 ``render_dashboard`` / ``_symbol_dynamics`` 渲染出的 **HTML 字符串**
（不看源码文本），逐条对应验收清单：

1. 只渲染一个区块（`<h2>三标的动态` 在、`为什么没开仓` 不在）
2. 每标的现价在整页只出现 1 次（合并前是两个数字打架的核心症状）
3. 闸门全 ok → 连「闸门放行」字样都不渲染
4. 闸门 settle / churn → 对应卡片一行 `⚠️ 静置剩余` / `⚠️ churn n/m`
5. 持仓标的只给「距自动离场线」，不给 ②③
6. pending 标的出「待确认（占槽）」、不出距离行
7. why 行报错 → 退回 symbol_state 读数，并写明读数时间
8. dense_cnt ≥ 6 → 状态词「密集酝酿 6/6」，title 里是真实根数
9. why 缺某标的 → 该标的仍渲染（fail-soft），其他标的不受影响
10. `build_rows`（`_why_rows` 的返回）新增 `sma20` 字段 —— 面板 ① 行不再跨源取数

全部临时库 / 纯 dict，零网络。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import strategy                                        # noqa: E402
from app.db import DB                                           # noqa: E402

try:
    import fastapi                                              # noqa: F401
    HAVE_FASTAPI = True
except ImportError:                                             # pragma: no cover - CI 里必然已装
    HAVE_FASTAPI = False

if HAVE_FASTAPI:
    from app.hl.views import _fmt_dt, render_dashboard           # noqa: E402

T0_MS = 1_760_000_000_000
UPDATED_AT = 1_760_000_000.0


# ---------------------------------------------------------------- 夹具
def _st(symbol="BTC_USDT", coin="BTC", **kw):
    """symbol_state 行（引擎 tick 落的缓存，面板降级源）。"""
    base = {"symbol": symbol, "coin": coin, "price": 81_504.0, "sma20": 79_000.0,
            "sma200": 85_000.0, "long_trigger": 82_000.0, "short_trigger": 78_000.0,
            "dense_cnt": 2, "updated_at": UPDATED_AT, "position_side": None}
    base.update(kw)
    return base


def _why(symbol="BTC_USDT", coin="BTC", **kw):
    """why 行（app.why.build_rows 的形状，面板主源）。"""
    base = {"symbol": symbol, "coin": coin, "price": 81_516.0, "sma20": 79_500.0,
            "why": {"fired": False, "side": 0, "blocked_by": "squeeze", "drift": False,
                    "reason": "均线不密集：带宽 1.02% 未压到门槛 0.80% 以内，或连续不足 6 根（现 2 根）",
                    "values": {"ma_max_prev": 81_990.0, "ma_min_prev": 79_010.0,
                               "dense_cnt": 2, "regime_ma": 200, "regime_val": 85_000.0}},
            "position": None, "error": None,
            "gates": {}, "gate_side": 0, "gate": None}
    base.update(kw)
    return base


def _gate(verdict="ok", **kw):
    base = {"verdict": verdict, "settle_remaining_s": 0.0, "settle_window_s": 15.0,
            "churn_count": 0, "churn_limit": 5, "churn_tripped": False}
    base.update(kw)
    return base


def _card(html: str, coin: str) -> str:
    """切出某个标的的卡片（按 .nm 定位到下个 .sym 或块尾）。"""
    i = html.index(f'class="nm">{coin}</span>')
    j = html.find('<div class="sym">', i)
    return html[i:j if j != -1 else len(html)]


class PanelMergeBase(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")

    @staticmethod
    def render(payload):
        return render_dashboard(payload)


# ================================================================ 1. 单区块
class TestSingleBlock(PanelMergeBase):
    def test_only_one_block_and_no_old_title(self):
        html = self.render({"symbol_state": [_st()], "why": [_why()]})
        self.assertEqual(html.count("<h2>三标的动态"), 1)
        self.assertEqual(html.count("为什么没开仓"), 0,
                         "旧区块标题必须彻底消失（合并后只剩一个板块）")
        # 旧表格的表头（标的/现价/信号态/闸门）不该再出现
        self.assertNotIn("闸门（动作前置）", html)
        self.assertNotIn("<th>信号态 / 拦截原因</th>", html)

    def test_state_word_vocabulary_is_unified(self):
        # 旧代码两块各写一套词：A「均线未密集」/ B「均线不密集」。统一为前者。
        html = self.render({"symbol_state": [_st(dense_cnt=0)], "why": [_why()]})
        card = _card(html, "BTC")
        self.assertIn('<span class="st muted">均线未密集</span>', card)


# ================================================================ 2. 现价唯一
class TestSinglePrice(PanelMergeBase):
    def test_price_rendered_once_per_symbol(self):
        payload = {"symbol_state": [_st(price=81_504.0)],
                   "why": [_why(price=81_516.0)]}
        html = self.render(payload)
        self.assertEqual(html.count("81,516.00"), 1, "现价全页只能出现一次")
        self.assertNotIn("81,504", html, "缓存里的旧价不允许再出现在同一屏")
        self.assertIn('<span class="px">81,516.00</span>', _card(html, "BTC"))

    def test_reason_line_does_not_repeat_price(self):
        # 原因行是 strategy 给的人话（含带宽/门槛），它不该再抄一遍现价
        row = _why()
        row["why"]["reason"] = "均线不密集：口径统一后只讲原因"
        html = self.render({"symbol_state": [_st(price=81_504.0)], "why": [row]})
        self.assertEqual(html.count("81,516.00"), 1)


# ================================================================ 3. 闸门正常不渲染
class TestGateSilentWhenOk(PanelMergeBase):
    def test_ok_gate_produces_no_dom(self):
        ok = _gate("ok")
        payload = {"symbol_state": [_st()],
                   "why": [_why(gate_side=1, gate=ok,
                                gates={"long": ok, "short": ok})]}
        html = self.render(payload)
        self.assertEqual(html.count("闸门放行"), 0)
        self.assertEqual(html.count("静置剩余"), 0)
        self.assertNotIn("⚠️", _card(html, "BTC"))

    def test_missing_gate_data_produces_no_dom(self):
        html = self.render({"symbol_state": [_st()], "why": [_why()]})
        self.assertNotIn("⚠️", _card(html, "BTC"))


# ================================================================ 4. 闸门异常行
class TestGateAnomalyLines(PanelMergeBase):
    def test_settle_line(self):
        g = _gate("settle", settle_remaining_s=8.4)
        html = self.render({"symbol_state": [_st()],
                            "why": [_why(gate_side=1, gate=g, gates={"long": g})]})
        self.assertIn("⚠️ 静置剩余 8s", _card(html, "BTC"))

    def test_churn_line_explains_the_breaker(self):
        g = _gate("churn", churn_count=4, churn_limit=5, churn_tripped=False)
        html = self.render({"symbol_state": [_st()],
                            "why": [_why(gate_side=1, gate=g, gates={"long": g})]})
        card = _card(html, "BTC")
        self.assertIn("⚠️ churn 4/5", card)
        self.assertIn("10 分钟内 5 次撤挂即熔断", card)

    def test_settle_with_breaker_disabled_reports_only_settle(self):
        # churn_limit<=0 = 熔断被关闭：正在静置时只能报静置（报 churn 次数是假信息）
        g = _gate("settle", settle_remaining_s=3.0, churn_limit=0)
        html = self.render({"symbol_state": [_st()],
                            "why": [_why(gate_side=1, gate=g, gates={"long": g})]})
        card = _card(html, "BTC")
        self.assertIn("⚠️ 静置剩余 3s", card)
        self.assertNotIn("churn", card)

    def test_both_sides_checked_when_no_focus_side(self):
        # gate_side=0（无仓、无信号）→ 双侧都看，只有异常那侧出行
        long_ok, short_settle = _gate("ok"), _gate("settle", settle_remaining_s=5.0)
        html = self.render({"symbol_state": [_st()],
                            "why": [_why(gate_side=0, gate=None,
                                         gates={"long": long_ok, "short": short_settle})]})
        card = _card(html, "BTC")
        self.assertIn("⚠️ 静置剩余 5s", card)
        self.assertEqual(card.count("⚠️"), 1)


# ================================================================ 5. 持仓卡
class TestPositionCard(PanelMergeBase):
    def test_open_position_shows_exit_row_only(self):
        pos = {"status": "open", "side": 1, "qty": 0.5, "qty_unit": "BTC",
               "entry": 81_000.0, "stop": 79_000.0}
        html = self.render({"symbol_state": [_st(position_side=1)],
                            "why": [_why(price=81_516.0, sma20=79_500.0, position=pos,
                                         why={"fired": False, "reason": "无信号",
                                              "values": {}})]})
        card = _card(html, "BTC")
        self.assertIn('<span class="st muted">持仓中（多）</span>', card)
        self.assertIn("距自动离场线", card)
        self.assertIn("线 79,500.00", card)          # ① 用 why 行带来的 sma20
        self.assertNotIn("上破开多", card)
        self.assertNotIn("下破开空", card)
        self.assertIn("持仓中，本轮不评估新入场", card)

    def test_short_position_uses_upper_side_title(self):
        pos = {"status": "open", "side": -1, "qty": 1.0, "qty_unit": "ETH",
               "entry": 3_100.0, "stop": 3_200.0}
        html = self.render({"symbol_state": [_st()],
                            "why": [_why(position=pos, why={"fired": False, "values": {}})]})
        card = _card(html, "BTC")
        self.assertIn('<span class="st muted">持仓中（空）</span>', card)
        self.assertIn("收盘站上该线即自动平仓", card)


# ================================================================ 6. pending 卡
class TestPendingCard(PanelMergeBase):
    def test_pending_states_and_no_distance_row(self):
        pos = {"status": "pending", "side": 1, "qty": 0.5, "qty_unit": "BTC",
               "entry": 81_000.0, "stop": 79_000.0}
        html = self.render({"symbol_state": [_st()], "why": [_why(position=pos)]})
        card = _card(html, "BTC")
        self.assertIn('<span class="st warn"', card)
        self.assertIn("待确认（占槽）", card)
        self.assertIn("确认前不评估新入场", card)
        self.assertIn("待确认占用仓位槽，确认或忽略后才会评估新信号", card)
        self.assertNotIn('class="kv"', card, "pending 不出距离行")
        self.assertNotIn("距自动离场线", card)
        self.assertNotIn("上破开多", card)
        self.assertNotIn("下破开空", card)


# ================================================================ 7. why 报错降级
class TestWhyErrorDegrades(PanelMergeBase):
    def test_error_falls_back_to_cached_readings_with_timestamp(self):
        row = _why(price=None, why=None, error="FeedError: BTC_USDT 只有 1 根 K 线")
        html = self.render({"symbol_state": [_st(price=81_504.0, dense_cnt=2)], "why": [row]})
        card = _card(html, "BTC")
        self.assertIn('<span class="st neg">行情读取失败</span>', card)
        self.assertIn("行情读取失败：FeedError", card)
        self.assertIn("81,504.00", card, "退回 symbol_state 的现价，别显示 —")
        when = _fmt_dt(UPDATED_AT, "%H:%M")
        self.assertIn(f"显示 {when} 的读数", card, "降级读数必须标时间，否则会被当成现价")
        # 距离行照旧可用（缓存里的线价），页面不塌
        self.assertIn("上破开多", card)
        self.assertEqual(html.count("81,504.00"), 1)


# ================================================================ 8. 密集封顶
class TestDenseCap(PanelMergeBase):
    def test_state_word_capped_title_has_real_count(self):
        html = self.render({"symbol_state": [_st(dense_cnt=9)], "why": [_why()]})
        card = _card(html, "BTC")
        self.assertIn("密集酝酿 6/6</span>", card)
        self.assertNotIn("密集酝酿 9/6", card)
        self.assertIn('title="6 均线已连续 9 根密集，满 6 根后收盘突破即入场"', card)

    def test_why_only_payload_also_caps(self):
        # 没有 symbol_state 时用 why 的 values.dense_cnt 兜底，封顶规则不变
        row = _why()
        row["why"]["values"]["dense_cnt"] = 7
        html = self.render({"why": [row]})
        card = _card(html, "BTC")
        self.assertIn("密集酝酿 6/6</span>", card)
        self.assertIn("连续 7 根密集", card)


# ================================================================ 9. fail-soft
class TestMissingWhyRow(PanelMergeBase):
    def test_symbol_without_why_row_still_renders(self):
        html = self.render({
            "why": [_why()],
            "symbol_state": [_st(), _st(symbol="ETH_USDT", coin="ETH", price=3_001.0,
                                        dense_cnt=0, position_side=None),
                             _st(symbol="SOL_USDT", coin="SOL", price=None, dense_cnt=None)],
        })
        btc, eth, sol = _card(html, "BTC"), _card(html, "ETH"), _card(html, "SOL")
        self.assertIn("均线不密集：带宽", btc)         # 有 why 的原因行
        self.assertIn("诊断数据暂不可用", eth)          # 缺 why → 标注读数时间
        self.assertIn(f"显示 {_fmt_dt(UPDATED_AT, '%H:%M')} 的读数", eth)
        self.assertIn("3,001.00", eth)
        self.assertIn("上破开多", eth)                 # 距离行仍可用
        self.assertIn('<span class="st neg">行情读取失败</span>', sol)

    def test_why_order_wins_then_leftovers_appended(self):
        html = self.render({
            "why": [_why(symbol="ETH_USDT", coin="ETH", price=3_001.0)],
            "symbol_state": [_st(), _st(symbol="ETH_USDT", coin="ETH", price=3_000.0)],
        })
        self.assertLess(html.index('class="nm">ETH'), html.index('class="nm">BTC'),
                        "以 why 行为序，缺的标的按 symbol_state 顺序补在后面")


# ================================================================ 10. sma20 新字段
def _flat_bars(n: int, close: float = 100.0):
    return [strategy.Bar(open_ms=T0_MS + i * 14_400_000, open=close, high=close * 1.001,
                         low=close * 0.999, close=close, volume=1.0) for i in range(n)]


class TestWhyRowCarriesSma20(unittest.TestCase):
    def test_build_rows_adds_sma20_at_last_closed_bar(self):
        from app import why as why_mod
        from app.config import Config

        # 行情缓存按标的为键、跨测试共享 —— 不清掉会拿到别的用例喂进来的 K 线
        why_mod.clear_cache()
        self.addCleanup(why_mod.clear_cache)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = DB(os.path.join(tmp.name, "t.db"))
        cfg = Config()
        cfg.symbols = ["BTC_USDT"]
        bars = _flat_bars(60, 100.0)
        ind = strategy.compute_indicators(bars)

        class _Feed:
            source = "hl-candle"

            def __init__(self, b):
                self.bars = b

        rows = why_mod.build_rows(db, cfg, symbols=["BTC_USDT"],
                                  fetch=lambda sym, now=None: _Feed(bars))
        self.assertIn("sma20", rows[0], "why 行必须带 sma20（面板 ① 行不再跨源取数）")
        self.assertAlmostEqual(rows[0]["sma20"], ind["sma20"][len(bars) - 2], places=6)
        self.assertAlmostEqual(rows[0]["sma20"], 100.0, places=6)


if __name__ == "__main__":
    unittest.main()
