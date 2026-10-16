"""面板「三标的动态」区块 + symbol_state 落库（feat/symbol-dynamics）。

覆盖：
a. 距离公式 views._need_pct：四方向（空离场/多离场/上破/下破）的符号与数值、
   缺值 → None（子行不渲染）
b. 状态词 views._sym_state_word：持仓中（多/空）、密集 0/3/6/9（展示封顶 6/6）
c. 渲染断言：**断言渲染出的 HTML 字符串**（区块头+时间、密集酝酿 n/6、① ② ③ title 原文、
   还差 +X.X%、入场行 <1% 且密集满 6 根才有的 class="warn"、底部可见图例行 sym-legend、
   区块位置在仓位表与交易所挂单之间、fail-soft 显示「—」）
d. DB：迁移建表、upsert 幂等、NULL 往返、缺行读取
e. 引擎计算：engine.symbol_snapshot（现价取未收盘 bar / 线取最后一根已收盘 bar / 持仓留 NULL /
   密集根数复用 strategy 的判定）
f. tick 写入路径：打桩 market.fetch + engine.walk（**按 symbol 分标的**）→ 断言落库值；
   「walk 抛异常 → 不写」「没有新 bar 的早退路径照写」「pending 挡下时照写且不推进 bar」
g. 面板读侧：status_payload 组装 + 缺表 fail-soft（db 没有 symbol_states 方法也不炸）

全部临时库、零网络；文件内不写任何本地绝对路径。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market, strategy                    # noqa: E402
from app.config import Config                               # noqa: E402
from app.db import DB                                       # noqa: E402

try:
    import fastapi                                           # noqa: F401
    HAVE_FASTAPI = True
except ImportError:                                          # pragma: no cover - CI 里必然已装
    HAVE_FASTAPI = False

if HAVE_FASTAPI:
    # 故意不写成「try/except ImportError → skip」：本文件的断言就是这个新功能的验收，
    # 在老代码（没有 _need_pct/_sym_state_word 的 main）上必须**报错**而不是静默跳过。
    from app.hl.views import (DashboardDeps, _fmt_dt, _need_pct, _sym_state_word,  # noqa: E402
                              render_dashboard, status_payload)

IV = market.INTERVAL_MS
T0 = 1_760_000_000_000


def flat_bars(n: int, close: float) -> list:
    return [strategy.Bar(open_ms=T0 + i * IV, open=close, high=close * 1.001,
                         low=close * 0.999, close=close, volume=1.0) for i in range(n)]


def wide_then_quiet_bars(wide: int = 408, quiet: int = 12) -> list:
    """宽幅震荡 → 极窄收敛（与 scripts/hl_rehearse.py 同构，必出密集）。

    最后一根是**进行中**的 bar（引擎按 len(bars)-2 认已收盘 bar），所以
    「最近连续密集根数」= quiet（12 根）。
    """
    seq = [100.0 * (1 + (0.02 if i % 2 == 0 else -0.02)) for i in range(wide)]
    seq += [100.0 * (1 + 0.0002 * (j % 2)) for j in range(quiet)]
    seq += [100.0 * 1.002]                                   # 进行中那根
    return [strategy.Bar(open_ms=T0 + i * IV, open=c, high=c * 1.001, low=c * 0.999,
                         close=c, volume=1.0) for i, c in enumerate(seq)]


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


class FakeFeed:
    def __init__(self, symbol, bars):
        self.symbol, self.source, self.bars = symbol, "test", bars
        self.fetched_at_ms = bars[-1].open_ms if bars else T0


# ================================================================ a. 距离公式
class TestNeedFormula(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")

    def test_four_directions_sign_and_value(self):
        # 持仓空：需要涨到 SMA20（收盘站上才自动平仓）
        self.assertAlmostEqual(_need_pct("exit_short", 100.0, 103.0), 3.0, places=6)
        self.assertAlmostEqual(_need_pct("exit_short", 100.0, 98.0), -2.0, places=6)
        # 持仓多：需要跌到 SMA20（收盘跌破）
        self.assertAlmostEqual(_need_pct("exit_long", 100.0, 97.0), 3.0, places=6)
        self.assertAlmostEqual(_need_pct("exit_long", 100.0, 102.0), -2.0, places=6)
        # 无仓上破开多：需要涨到 6均线上轨
        self.assertAlmostEqual(_need_pct("break_up", 100.0, 105.0), 5.0, places=6)
        self.assertAlmostEqual(_need_pct("break_up", 100.0, 99.0), -1.0, places=6)
        # 无仓下破开空：需要跌到 6均线下轨
        self.assertAlmostEqual(_need_pct("break_down", 100.0, 97.0), 3.0, places=6)
        self.assertAlmostEqual(_need_pct("break_down", 100.0, 101.0), -1.0, places=6)

    def test_missing_or_invalid_line_returns_none(self):
        for kind in ("exit_short", "exit_long", "break_up", "break_down"):
            self.assertIsNone(_need_pct(kind, None, 100.0), kind)
            self.assertIsNone(_need_pct(kind, 100.0, None), kind)
            self.assertIsNone(_need_pct(kind, 0, 100.0), kind)
            self.assertIsNone(_need_pct(kind, -5.0, 100.0), kind)
            self.assertIsNone(_need_pct(kind, "垃圾", 100.0), kind)
        self.assertIsNone(_need_pct("no_such_kind", 100.0, 100.0))

    def test_near_threshold_is_under_one_percent(self):
        # 0 < need < 1 → 渲染时要加 warn；边界 1.0 不加
        self.assertLess(_need_pct("break_up", 100.0, 100.5), 1.0)
        self.assertGreaterEqual(_need_pct("break_up", 100.0, 101.0), 1.0)


# ================================================================ b. 状态词
class TestStateWord(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")

    def test_open_position_wins_over_dense(self):
        self.assertEqual(_sym_state_word(1, 0)[0], "持仓中（多）")
        self.assertEqual(_sym_state_word(1, 99)[0], "持仓中（多）")
        self.assertEqual(_sym_state_word(-1, None)[0], "持仓中（空）")
        # 持仓是 muted，不挂 hot 的悬停
        txt, cls, title = _sym_state_word(-1, 6)
        self.assertEqual(cls, "muted")
        self.assertEqual(title, "")

    def test_dense_zero_or_null_is_not_squeezed(self):
        for v in (None, 0, "0", ""):
            txt, cls, title = _sym_state_word(None, v)
            self.assertEqual((txt, cls, title), ("均线未密集", "muted", ""), repr(v))

    def test_dense_counts_and_cap(self):
        txt, cls, title = _sym_state_word(None, 3)
        self.assertEqual(txt, "密集酝酿 3/6")
        self.assertEqual(cls, "hot")
        self.assertIn("连续 3 根密集", title)
        self.assertEqual(_sym_state_word(None, 6)[0], "密集酝酿 6/6")
        # ≥6 封顶显示 6/6，但悬停里的根数是真实值
        txt, cls, title = _sym_state_word(None, 9)
        self.assertEqual(txt, "密集酝酿 6/6")
        self.assertEqual(cls, "hot")
        self.assertIn("连续 9 根密集", title)
        self.assertEqual(title, "6 均线已连续 9 根密集，满 6 根后收盘突破即入场")


# ================================================================ c. 渲染
def sym_card(html: str, coin: str) -> str:
    """从渲染结果里切出一个标的的 .sym 卡片（按 coin 定位到下个 .sym 或块尾）。"""
    i = html.index(f'class="nm">{coin}</span>')
    j = html.find('class="sym"', i)
    return html[i:j if j != -1 else len(html)]


class TestRenderBlock(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")

    def render(self, rows, **extra):
        p = {"symbol_state": rows}
        p.update(extra)
        return render_dashboard(p)

    def row(self, **kw):
        base = {"symbol": "BTC_USDT", "coin": "BTC", "price": 100_000.0, "sma20": 96_000.0,
                "long_trigger": 105_000.0, "short_trigger": 94_000.0, "dense_cnt": 4,
                "updated_at": 1760000000.0, "position_side": None}
        base.update(kw)
        return base

    def test_block_title_time_and_position(self):
        html = self.render([self.row()])
        self.assertIn("三标的动态", html)
        # 区块头时间：最新的 updated_at，北京时间 HH:MM（与面板其它时间同口径 _fmt_dt）
        expect = _fmt_dt(1760000000.0, "%H:%M")
        self.assertIn(f'三标的动态<span class="muted">行情 {expect} · 每 30 分钟随信号 tick 更新</span>',
                      html)
        if os.environ.get("DISPLAY_TZ_OFFSET", "8").strip() in ("8", "8.0", ""):
            self.assertIn("行情 16:53 ·", html)   # 1760000000 = 2025-10-09 08:53:20 UTC = 北京 16:53
        # 位置：总览页最顶部 —— tabs 之后第一个区块，在「账户」卡片之前
        self.assertLess(html.index("三标的动态"), html.index("账户"))
        self.assertLess(html.index("<h1>Hyperliquid 执行器监控</h1>"), html.index("三标的动态"))
        self.assertLess(html.index("三标的动态"), html.index("<h2>仓位</h2>"))
        self.assertLess(html.index("三标的动态"), html.index("<h2>交易所挂单</h2>"))

    def test_head_time_takes_newest_of_three(self):
        html = self.render([self.row(symbol="A_USDT", coin="A", updated_at=1760000000.0),
                            self.row(symbol="B_USDT", coin="B", updated_at=1760003600.0),
                            self.row(symbol="C_USDT", coin="C", updated_at=None)])
        expect = _fmt_dt(1760003600.0, "%H:%M")
        self.assertIn(f"行情 {expect} ·", html)
        # 全部缺 updated_at → 时间显示「—」，区块照常渲染
        html2 = self.render([self.row(updated_at=None)])
        self.assertIn("行情 — ·", html2)

    def test_state_word_and_hot_title_rendered(self):
        html = self.render([self.row(dense_cnt=4)])
        card = sym_card(html, "BTC")
        self.assertIn('<span class="st hot" title="6 均线已连续 4 根密集，满 6 根后收盘突破即入场">'
                      "密集酝酿 4/6</span>", card)
        self.assertIn('class="nm">BTC</span>', card)
        self.assertIn('<span class="px">100,000.00</span>', card)

    def test_dense_capped_at_six_in_html(self):
        html = self.render([self.row(dense_cnt=9)])
        self.assertIn("密集酝酿 6/6", html)
        self.assertNotIn("密集酝酿 9/6", html)

    def test_flat_symbol_shows_not_squeezed_and_two_entry_rows(self):
        html = self.render([self.row(dense_cnt=0)])
        card = sym_card(html, "BTC")
        self.assertIn('<span class="st muted">均线未密集</span>', card)
        self.assertIn("上破开多", card)
        self.assertIn("下破开空", card)
        self.assertNotIn("距自动离场线", card)

    def test_entry_row_formula_titles_and_line_prices(self):
        html = self.render([self.row()])           # 现价 100000，上轨 105000，下轨 94000
        card = sym_card(html, "BTC")
        # ② 上破开多 = (105000-100000)/100000 = +5.0%
        self.assertIn('<span class="fn" title="4h 收盘突破该线即入场（需均线密集满 6 根）">②</span>',
                      card)
        self.assertIn("还差 <b>+5.0%</b> <span class=\"muted\">线 105,000.00</span>", card)
        # ③ 下破开空 = (100000-94000)/100000 = +6.0%
        self.assertIn('<span class="fn" title="收盘跌破该线且在 MA200 下方才开空">③</span>', card)
        self.assertIn("还差 <b>+6.0%</b> <span class=\"muted\">线 94,000.00</span>", card)

    def test_near_trigger_gets_warn_class_and_title(self):
        # 入场行：密集满 6 根（入场前提成立）→ 0.5% 才标 warn
        card = sym_card(self.render([self.row(dense_cnt=6, long_trigger=100_500.0)]), "BTC")
        self.assertIn('<b class="warn" title="接近触发（按收盘判定）">+0.5%</b>', card)
        # 远的那条不加 warn
        self.assertIn("还差 <b>+6.0%</b>", card)

    def test_entry_row_without_full_squeeze_never_warns(self):
        # 密集不满 6 根（4/0/NULL）时，即使距离 0.5% 也不加 warn：
        # 破线也不会开仓（还需密集/MA200 前置），琥珀会误导
        for dense in (4, 0, None):
            with self.subTest(dense=dense):
                card = sym_card(self.render([self.row(dense_cnt=dense,
                                                     long_trigger=100_500.0,
                                                     short_trigger=99_600.0)]), "BTC")
                self.assertIn("还差 <b>+0.5%</b>", card)          # 上破开多 +0.5%
                self.assertIn("还差 <b>+0.4%</b>", card)          # 下破开空 +0.4%
                self.assertNotIn('class="warn"', card)

    def test_position_exit_row_warns_without_dense(self):
        # ① 离场行不受 dense 影响：距自动离场线 0.5% → 仍标 warn（离场只看收盘破线）
        card = sym_card(self.render([self.row(position_side=1, sma20=99_500.0,
                                              dense_cnt=None)]), "BTC")
        self.assertIn('<b class="warn" title="接近触发（按收盘判定）">+0.5%</b>', card)
        # 同卡片里没有入场行（持仓标的只给离场线）
        self.assertNotIn("上破开多", card)

    def test_legend_row_rendered_in_block(self):
        # 手机（iOS Safari 无 title 悬停）唯一能看到 ①②③ 口径的地方 → 必须是可见文字
        # 注意：CSS 里也有 .sym-legend（在 <head>），所以用 div 标签定位，别用裸类名
        html = self.render([self.row()])
        legend = '<div class="sym-legend">① 离场按 4h 收盘判定 · ' \
                 '② 破线+密集满 6 根开多 · ③ 破线+MA200 下方才开空</div>'
        self.assertIn(legend, html)
        # 图例在 grid 之后、区块 card 收尾之前（即仍在「三标的动态」区块内）
        mark = '<div class="sym-legend">'
        self.assertLess(html.index("三标的动态"), html.index(mark))
        self.assertLess(html.index(mark), html.index("<h2>交易所挂单</h2>"))
        # 三段口径逐条可见
        for frag in ("① 离场按 4h 收盘判定", "② 破线+密集满 6 根开多",
                     "③ 破线+MA200 下方才开空"):
            self.assertIn(frag, html)
        # 样式类随页面下发（只用现有 CSS 变量，两主题自动适配）；
        # 本轮起 .legend 与 .sym-legend 合组同规则（账户/系统/数据源三条注脚共用）
        self.assertIn(".sym-legend,.legend{margin-top:8px;font-size:11px;"
                      "color:var(--muted);line-height:1.6}", html)
        # 桌面端悬停口径保留（title 原文不动）
        self.assertIn('title="4h 收盘突破该线即入场（需均线密集满 6 根）"', html)
        self.assertIn('title="收盘跌破该线且在 MA200 下方才开空"', html)

    def test_max_dense_plain_row_still_warns_at_nine(self):
        # dense_cnt 展示封顶 6/6，但判定用的是真实根数（9 ≥ 6 → 照常允许 warn）
        card = sym_card(self.render([self.row(dense_cnt=9, long_trigger=100_500.0)]), "BTC")
        self.assertIn('<b class="warn" title="接近触发（按收盘判定）">+0.5%</b>', card)

    def test_need_non_positive_shows_wait_for_close(self):
        # 现价已在触发线上方 → 不能报「还差」，改成待收盘判定（不加粗不琥珀）
        html = self.render([self.row(long_trigger=99_000.0)])
        card = sym_card(html, "BTC")
        self.assertIn('<span class="muted">待收盘判定</span> <span class="muted">线 99,000.00</span>',
                      card)
        up_row = card.split("下破开空")[0]                 # 只看「上破开多」那一行
        self.assertNotIn("还差", up_row)
        self.assertNotIn('class="warn"', card)

    def test_position_rows_exit_only_and_direction_titles(self):
        # 持仓多、离场线在下方：还需要跌 3% 才触发（need = (现价 − SMA20)/现价）
        long_card = sym_card(self.render([self.row(position_side=1, sma20=97_000.0)]), "BTC")
        self.assertIn("距自动离场线", long_card)
        self.assertIn("按 4h 收盘判定，非盘中触发：收盘跌破该线即自动平仓", long_card)
        self.assertIn("还差 <b>+3.0%</b> <span class=\"muted\">线 97,000.00</span>", long_card)
        self.assertNotIn("上破开多", long_card)
        self.assertNotIn("下破开空", long_card)

        # 持仓空、离场线在上方：还需要涨 3% 才触发（need = (SMA20 − 现价)/现价）
        short_card = sym_card(self.render([self.row(position_side=-1, sma20=103_000.0)]), "BTC")
        self.assertIn('<span class="st muted">持仓中（空）</span>', short_card)
        self.assertIn("按 4h 收盘判定，非盘中触发：收盘站上该线即自动平仓", short_card)
        self.assertIn("还差 <b>+3.0%</b> <span class=\"muted\">线 103,000.00</span>", short_card)

        # 持仓多、现价已跌破离场线 → 待收盘判定
        already = sym_card(self.render([self.row(position_side=1, sma20=103_000.0)]), "BTC")
        self.assertIn("待收盘判定", already)
        self.assertNotIn("还差", already)

    def test_missing_row_fails_soft_with_dash_and_no_subrows(self):
        rows = [self.row(symbol="BTC_USDT", coin="BTC"),
                {"symbol": "ETH_USDT", "coin": "ETH", "price": None, "sma20": None,
                 "long_trigger": None, "short_trigger": None, "dense_cnt": None,
                 "updated_at": None, "position_side": None},
                self.row(symbol="SOL_USDT", coin="SOL", price=150.0, sma20=None,
                         long_trigger=None, short_trigger=None, dense_cnt=None)]
        html = self.render(rows)
        eth = sym_card(html, "ETH")
        self.assertIn('<span class="px">—</span>', eth)
        self.assertNotIn('class="kv"', eth, "缺字段的标的不能渲染距离子行")
        sol = sym_card(html, "SOL")
        self.assertIn('<span class="px">150.00</span>', sol)
        self.assertNotIn('class="kv"', sol, "触发线 NULL → 子行整体不渲染")
        # 其余标的不受影响
        btc = sym_card(html, "BTC")
        self.assertEqual(btc.count('class="kv"'), 2)

    def test_partial_null_only_renders_present_line(self):
        html = self.render([self.row(long_trigger=None, short_trigger=94_000.0)])
        card = sym_card(html, "BTC")
        self.assertIn("下破开空", card)
        self.assertNotIn("上破开多", card)

    def test_no_rows_no_block(self):
        html = self.render([])
        self.assertNotIn("三标的动态", html)
        # 无数据时图例也不渲染（图例只在有三标的动态区块时出现；CSS 里的 .sym-legend 不算）
        self.assertNotIn('<div class="sym-legend">', html)
        self.assertIn("<h2>仓位</h2>", html)


# ================================================================ d. DB
class TestSymbolStateDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.db = DB(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_table_created_on_open_even_for_old_db(self):
        # 老库（建表前就存在）打开时也会补出 symbol_state（SCHEMA 里 CREATE TABLE IF NOT EXISTS）
        self.db.conn.execute("DROP TABLE symbol_state")
        self.db.conn.commit()
        db2 = DB(self.path)
        self.assertEqual(db2.symbol_states(), [])
        db2.set_symbol_state("BTC_USDT", price=1.0)
        self.assertEqual(len(db2.symbol_states()), 1)

    def test_upsert_is_idempotent_and_overwrites(self):
        self.db.set_symbol_state("BTC_USDT", price=100.0, sma20=99.0, long_trigger=101.0,
                                 short_trigger=95.0, dense_cnt=3)
        first = self.db.symbol_state("BTC_USDT")
        self.db.set_symbol_state("BTC_USDT", price=110.0, sma20=109.0, long_trigger=111.0,
                                 short_trigger=105.0, dense_cnt=9)
        rows = self.db.symbol_states()
        self.assertEqual(len(rows), 1, "同 symbol 二次写不能产生第二行")
        again = self.db.symbol_state("BTC_USDT")
        self.assertAlmostEqual(again["price"], 110.0)
        self.assertEqual(again["dense_cnt"], 9)
        self.assertGreaterEqual(again["updated_at"], first["updated_at"])

    def test_two_symbols_two_rows(self):
        self.db.set_symbol_state("BTC_USDT", price=1.0)
        self.db.set_symbol_state("ETH_USDT", price=2.0)
        self.assertEqual([r["symbol"] for r in self.db.symbol_states()],
                         ["BTC_USDT", "ETH_USDT"])

    def test_null_fields_roundtrip(self):
        self.db.set_symbol_state("BTC_USDT", price=100.0, sma20=None, long_trigger=None,
                                 short_trigger=None, dense_cnt=None)
        r = self.db.symbol_state("BTC_USDT")
        self.assertAlmostEqual(r["price"], 100.0)
        self.assertIsNone(r["sma20"])
        self.assertIsNone(r["long_trigger"])
        self.assertIsNone(r["short_trigger"])
        self.assertIsNone(r["dense_cnt"])

    def test_read_missing_symbol_returns_none(self):
        self.assertIsNone(self.db.symbol_state("SOL_USDT"))


# ================================================================ e. 引擎计算
class TestSymbolSnapshot(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.band_window = strategy.BAND_WINDOW
        self.cfg.band_quantile = strategy.BAND_QUANTILE
        self.cfg.min_squeeze_bars = strategy.MIN_SQUEEZE_BARS

    def test_price_is_last_unclosed_bar_line_is_last_closed_bar(self):
        bars = wide_then_quiet_bars()
        ind = strategy.compute_indicators(bars)
        li = len(bars) - 2
        snap = engine.symbol_snapshot(self.cfg, bars, ind, li)
        self.assertAlmostEqual(snap["price"], float(bars[-1].close), places=9)
        self.assertNotAlmostEqual(snap["price"], float(bars[li].close), places=9)
        self.assertAlmostEqual(snap["sma20"], float(ind["sma20"][li]), places=9)
        self.assertAlmostEqual(snap["long_trigger"], float(ind["ma_max"][li]), places=9)
        self.assertAlmostEqual(snap["short_trigger"], float(ind["ma_min"][li]), places=9)
        # 取的是「最后一根已收盘 bar」而不是进行中那根：两者确实不同，断言才有鉴别力
        self.assertNotAlmostEqual(snap["long_trigger"], float(ind["ma_max"][li + 1]), places=9)

    def test_dense_count_reuses_strategy_judgement(self):
        bars = wide_then_quiet_bars(wide=408, quiet=12)
        ind = strategy.compute_indicators(bars)
        li = len(bars) - 2
        snap = engine.symbol_snapshot(self.cfg, bars, ind, li)
        self.assertEqual(snap["dense_cnt"], 12, "12 根极窄收敛 → 连续密集 12 根")
        # 与信号侧的密集判定同源：密集根数 ≥ min_squeeze_bars ⟺ squeeze 标志为 True
        sq6 = strategy.squeeze_flags(ind["band"], self.cfg.band_window, self.cfg.band_quantile,
                                     self.cfg.min_squeeze_bars)
        self.assertTrue(sq6[li])
        self.assertGreaterEqual(snap["dense_cnt"], self.cfg.min_squeeze_bars)

    def test_dense_count_zero_without_enough_history(self):
        bars = flat_bars(130, 250.0)         # < 分位窗口(250) → 判定不成立
        ind = strategy.compute_indicators(bars)
        snap = engine.symbol_snapshot(self.cfg, bars, ind, len(bars) - 2)
        self.assertEqual(snap["dense_cnt"], 0)
        self.assertAlmostEqual(snap["long_trigger"], 250.0)

    def test_has_position_leaves_triggers_and_dense_null(self):
        bars = wide_then_quiet_bars()
        ind = strategy.compute_indicators(bars)
        snap = engine.symbol_snapshot(self.cfg, bars, ind, len(bars) - 2, has_position=True)
        self.assertAlmostEqual(snap["price"], float(bars[-1].close), places=9)
        self.assertIsNotNone(snap["sma20"])
        self.assertIsNone(snap["long_trigger"])
        self.assertIsNone(snap["short_trigger"])
        self.assertIsNone(snap["dense_cnt"])

    def test_dense_run_negative_index(self):
        self.assertEqual(engine._dense_run([0.0] * 300, self.cfg, -1), 0)
        self.assertEqual(engine._dense_run([], self.cfg, 250), 0)


# ================================================================ f. tick 写入路径
class BoomError(RuntimeError):
    """本文件专用异常类型：tick_error 告警按 (symbol, 异常类型) 去重，
    用专属类型避免与其它测试文件的去重窗口互相干扰。"""


class TestTickWritesSymbolState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "t.db"))
        self.cfg = Config()
        self.cfg.symbols = ["BTC_USDT", "ETH_USDT"]
        self.notifier = FakeNotifier()
        self.bars = {"BTC_USDT": wide_then_quiet_bars(408, 12),
                     "ETH_USDT": flat_bars(130, 250.0)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_tick(self, walk_stub, now_ms=None):
        feeds = {s: FakeFeed(s, b) for s, b in self.bars.items()}

        def fake_fetch(symbol, now=None, allow_fallback=False):
            return feeds[symbol]

        with mock.patch.object(market, "fetch", side_effect=fake_fetch), \
                mock.patch.object(engine, "walk", side_effect=walk_stub):
            return engine.tick(self.db, self.cfg, self.notifier, now_ms=now_ms)

    def test_writes_values_per_symbol(self):
        def walk_stub(cfg, symbol, bars, ind, sq, position, from_i, to_i, latest_closed_i):
            # 桩必须按 symbol 分标的：tick 会遍历多个标的，桩不区分会出现幽灵事件
            return {"BTC_USDT": ([], None), "ETH_USDT": ([], None)}[symbol]

        self.run_tick(walk_stub)
        btc = self.db.symbol_state("BTC_USDT")
        ind = strategy.compute_indicators(self.bars["BTC_USDT"])
        li = len(self.bars["BTC_USDT"]) - 2
        self.assertAlmostEqual(btc["price"], float(self.bars["BTC_USDT"][-1].close), places=9)
        self.assertAlmostEqual(btc["sma20"], float(ind["sma20"][li]), places=9)
        self.assertAlmostEqual(btc["long_trigger"], float(ind["ma_max"][li]), places=9)
        self.assertAlmostEqual(btc["short_trigger"], float(ind["ma_min"][li]), places=9)
        self.assertEqual(btc["dense_cnt"], 12)
        self.assertAlmostEqual(btc["updated_at"], time.time(), delta=60)

        eth = self.db.symbol_state("ETH_USDT")
        self.assertAlmostEqual(eth["price"], 250.0)
        self.assertAlmostEqual(eth["sma20"], 250.0)
        self.assertEqual(eth["dense_cnt"], 0)
        self.assertEqual(len(self.db.symbol_states()), 2)

    def test_position_symbol_stores_nulls_for_triggers(self):
        def walk_stub(cfg, symbol, bars, ind, sq, position, from_i, to_i, latest_closed_i):
            if symbol == "ETH_USDT":
                return [], engine.PositionState(symbol=symbol, entry_price=250.0, stop_price=240.0,
                                                contracts=1, signal_bar_ms=bars[-2].open_ms)
            return [], None

        self.run_tick(walk_stub)
        eth = self.db.symbol_state("ETH_USDT")
        self.assertAlmostEqual(eth["price"], 250.0)
        self.assertIsNotNone(eth["sma20"])
        self.assertIsNone(eth["long_trigger"])
        self.assertIsNone(eth["short_trigger"])
        self.assertIsNone(eth["dense_cnt"])
        # 无仓的 BTC 照常写三条线
        self.assertIsNotNone(self.db.symbol_state("BTC_USDT")["long_trigger"])

    def test_walk_failure_writes_nothing(self):
        def walk_stub(cfg, symbol, bars, ind, sq, position, from_i, to_i, latest_closed_i):
            raise BoomError("walk 炸了")

        self.run_tick(walk_stub)
        self.assertEqual(self.db.symbol_states(), [], "walk 失败不允许写面板状态")

    def test_early_return_without_walk_still_writes(self):
        # DB 已处理到「进行中」那根 → 本轮没有新的已收盘 bar，早退；面板状态仍要刷新
        for s, bars in self.bars.items():
            self.db.set_last_bar(s, bars[-1].open_ms)
        called = []

        def walk_stub(cfg, symbol, bars, ind, sq, position, from_i, to_i, latest_closed_i):
            called.append(symbol)
            return [], None

        self.run_tick(walk_stub)
        self.assertEqual(called, [], "这条路径不该调用 walk")
        self.assertEqual(len(self.db.symbol_states()), 2)

    def test_pending_position_path_writes_without_advancing_bar(self):
        self.cfg.symbols = ["BTC_USDT"]
        bars = self.bars["BTC_USDT"]
        self.db.create_position(symbol="BTC_USDT", signal_bar_ms=bars[-2].open_ms,
                                entry_price=100.0, stop_price=98.0, stop_dist=0.02,
                                contracts=1, coin_qty=0.01, notional=100.0, risk_u=2.0)
        self.db.set_last_bar("BTC_USDT", bars[-2].open_ms)
        called = []

        def walk_stub(cfg, symbol, bars_, ind, sq, position, from_i, to_i, latest_closed_i):
            called.append(symbol)
            return [], None

        self.run_tick(walk_stub, now_ms=bars[-1].open_ms)
        self.assertEqual(called, [], "pending 占槽时不该评估入场")
        self.assertEqual(self.db.get_last_bar("BTC_USDT"), bars[-2].open_ms,
                         "pending 期间不推进 bar 进度")
        row = self.db.symbol_state("BTC_USDT")
        self.assertAlmostEqual(row["price"], float(bars[-1].close), places=9)
        self.assertEqual(row["dense_cnt"], 12, "pending 不是仓位 → 触发线/密集照常算")


# ================================================================ g. 面板读侧
class BareDb:
    """只有 recent_positions 的极简 DB：模拟「库里没有 symbol_state 表 / 方法缺失」。"""

    def recent_positions(self, limit=50):
        return []


class TestPayloadSymbolState(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "t.db"))
        self.cfg = type("C", (), {"symbols": ["BTC_USDT", "ETH_USDT", "SOL_USDT"]})()

    def tearDown(self):
        self.tmp.cleanup()

    def deps(self, db=None):
        return DashboardDeps(db=db if db is not None else self.db, config_fn=lambda: self.cfg,
                             info=None, address_fn=lambda: "", mode_fn=lambda: "live")

    def test_missing_rows_are_none_and_render_dash(self):
        p = status_payload(self.deps())
        self.assertEqual([r["coin"] for r in p["symbol_state"]], ["BTC", "ETH", "SOL"])
        self.assertTrue(all(r["price"] is None for r in p["symbol_state"]))
        html = render_dashboard(p)
        self.assertIn("三标的动态", html)
        self.assertEqual(html.count('class="px">—</span>'), 3)

    def test_values_and_position_side(self):
        self.db.set_symbol_state("BTC_USDT", price=100_000.0, sma20=96_000.0,
                                 long_trigger=105_000.0, short_trigger=94_000.0, dense_cnt=4)
        pid = self.db.create_position(symbol="ETH_USDT", signal_bar_ms=1, entry_price=3_000.0,
                                      stop_price=3_200.0, stop_dist=0.06, contracts=0,
                                      coin_qty=1.0, notional=3000.0, risk_u=20.0)
        self.db.confirm_position(pid, 3_000.0, 1.0, 3_200.0, True)      # → open, side=1
        p = status_payload(self.deps())
        by_sym = {r["symbol"]: r for r in p["symbol_state"]}
        self.assertEqual(by_sym["BTC_USDT"]["dense_cnt"], 4)
        self.assertIsNone(by_sym["BTC_USDT"]["position_side"])
        self.assertEqual(by_sym["ETH_USDT"]["position_side"], 1)
        self.assertIsNone(by_sym["SOL_USDT"]["price"])
        html = render_dashboard(p)
        self.assertIn('<span class="st hot" title="6 均线已连续 4 根密集，满 6 根后收盘突破即入场">'
                      "密集酝酿 4/6</span>", html)
        self.assertIn("持仓中（多）", html)
        self.assertIn("均线未密集", html)

    def test_pending_position_is_not_position(self):
        self.db.create_position(symbol="ETH_USDT", signal_bar_ms=1, entry_price=3_000.0,
                                stop_price=3_200.0, stop_dist=0.06, contracts=0,
                                coin_qty=1.0, notional=3000.0, risk_u=20.0)
        p = status_payload(self.deps())
        by_sym = {r["symbol"]: r for r in p["symbol_state"]}
        self.assertIsNone(by_sym["ETH_USDT"]["position_side"], "pending 不算仓位")

    def test_db_without_symbol_state_table_still_renders(self):
        p = status_payload(self.deps(db=BareDb()))
        self.assertEqual(len(p["symbol_state"]), 3)
        self.assertTrue(all(r["price"] is None for r in p["symbol_state"]))
        self.assertIn("三标的动态", render_dashboard(p))

    def test_json_payload_is_serializable(self):
        import json
        p = status_payload(self.deps())
        json.dumps(p["symbol_state"])                 # 不能混进 sqlite Row / 自定义对象


if __name__ == "__main__":
    unittest.main(verbosity=2)
