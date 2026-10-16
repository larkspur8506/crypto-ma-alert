"""下破开空行（③）第三重门槛：现价 < SMA200（fix/ma200-gate）。

背景：面板「三标的动态」的下破开空行琥珀原先只判「距线 <1% 且密集 ≥6」两重，
但开空实际要三重（收盘跌破下轨 + 密集满 6 根 + **收盘 < MA200**）。价格回升到
MA200 上方而距下轨仍 <1% 时，琥珀会误导「今晚可能开空」。

覆盖：
a. db 迁移：老库（symbol_state 无 sma200 列）打开后补列且旧数据保留；新库直建含列；
   迁移幂等（重开不重复加列）
b. set_symbol_state：sma200 往返（写入/覆盖/清空为 NULL）+ 新列不影响旧字段
c. engine.symbol_snapshot：sma200 取**最后一根已收盘 bar**（与 sma20 同源同索引）、
   历史不足 → NULL、持仓标的也照存
d. engine.tick：sma200 经 _record_symbol_state 透传落库（与 ind 同值）
e. views._symbol_state_rows / status_payload：payload 带 sma200 且 JSON 可序列化
f. 琥珀三态：③ 行 = dense≥6 且 sma200 非 None 且 price < sma200（严格小于，
   price == sma200 不亮）；sma200 缺失 → fail-closed 不亮
g. 做多行（②）回归：只看 dense≥6，不查 MA200；① 离场行不受影响

全部临时库、零网络；文件内不写任何本地绝对路径。
"""
from __future__ import annotations

import json
import os
import sqlite3
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
    from app.hl.views import (DashboardDeps, _symbol_state_rows,  # noqa: E402
                              render_dashboard, status_payload)

IV = market.INTERVAL_MS
T0 = 1_760_000_000_000

# 面板渲染断言用的固定串（与 views._NEAR_TITLE 同源；用原文而非正则，改文案即报错）
WARN = '<b class="warn" title="接近触发（按收盘判定）">'

OLD_SYMBOL_STATE_DDL = """
CREATE TABLE symbol_state (
    symbol TEXT PRIMARY KEY,
    price REAL,
    sma20 REAL,
    long_trigger REAL,
    short_trigger REAL,
    dense_cnt INTEGER,
    updated_at REAL
)
"""


def cols(db_path: str, table: str) -> list:
    c = sqlite3.connect(db_path)
    try:
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
    finally:
        c.close()


def flat_bars(n: int, close: float) -> list:
    return [strategy.Bar(open_ms=T0 + i * IV, open=close, high=close * 1.001,
                         low=close * 0.999, close=close, volume=1.0) for i in range(n)]


def wide_then_quiet_bars(wide: int = 408, quiet: int = 12) -> list:
    """宽幅震荡 → 极窄收敛（与 tests/test_symbol_state.py 同构、必出密集）。"""
    seq = [100.0 * (1 + (0.02 if i % 2 == 0 else -0.02)) for i in range(wide)]
    seq += [100.0 * (1 + 0.0002 * (j % 2)) for j in range(quiet)]
    seq += [100.0 * 1.002]
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


# ================================================================ a. db 迁移
class TestSma200Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_old_db(self) -> None:
        """老库：symbol_state 是加 sma200 之前的形状（payload 里的列一个不少、就是没 sma200）。"""
        c = sqlite3.connect(self.path)
        try:
            c.executescript(OLD_SYMBOL_STATE_DDL)
            c.execute("INSERT INTO symbol_state(symbol, price, sma20, long_trigger,"
                      " short_trigger, dense_cnt, updated_at) VALUES(?,?,?,?,?,?,?)",
                      ("BTC_USDT", 100_000.0, 96_000.0, 105_000.0, 94_000.0, 4, 1760000000.0))
            c.commit()
        finally:
            c.close()
        self.assertNotIn("sma200", cols(self.path, "symbol_state"), "前置：老库确实没有该列")

    def test_old_db_gains_column_and_keeps_old_data(self):
        self._make_old_db()
        db = DB(self.path)                                   # 打开即迁移
        self.assertIn("sma200", cols(self.path, "symbol_state"))
        row = db.symbol_state("BTC_USDT")
        self.assertIsNotNone(row, "迁移不能丢旧行")
        self.assertAlmostEqual(row["price"], 100_000.0, places=9)
        self.assertAlmostEqual(row["sma20"], 96_000.0, places=9)
        self.assertAlmostEqual(row["long_trigger"], 105_000.0, places=9)
        self.assertEqual(row["dense_cnt"], 4)
        self.assertAlmostEqual(row["updated_at"], 1760000000.0, places=9)
        self.assertIsNone(row["sma200"], "补列的历史行 sma200 为 NULL（不臆造值）")
        # 迁移后写入路径可用
        db.set_symbol_state("BTC_USDT", price=100_000.0, sma200=90_000.0)
        self.assertAlmostEqual(db.symbol_state("BTC_USDT")["sma200"], 90_000.0, places=9)

    def test_new_db_is_created_with_sma200(self):
        DB(self.path)
        self.assertIn("sma200", cols(self.path, "symbol_state"))

    def test_migration_is_idempotent(self):
        self._make_old_db()
        DB(self.path)
        DB(self.path)                                        # 再开一次
        self.assertEqual(cols(self.path, "symbol_state").count("sma200"), 1)

    def test_set_symbol_state_sma200_roundtrip_and_overwrite(self):
        db = DB(self.path)
        db.set_symbol_state("BTC_USDT", price=1.0, sma20=2.0, long_trigger=3.0,
                            short_trigger=4.0, dense_cnt=5, sma200=6.0)
        r = db.symbol_state("BTC_USDT")
        self.assertEqual((r["price"], r["sma20"], r["long_trigger"], r["short_trigger"],
                          r["dense_cnt"], r["sma200"]), (1.0, 2.0, 3.0, 4.0, 5, 6.0))
        # 覆盖：新值进、旧值不留
        db.set_symbol_state("BTC_USDT", price=10.0, sma20=20.0, sma200=60.0)
        r = db.symbol_state("BTC_USDT")
        self.assertAlmostEqual(r["sma200"], 60.0, places=9)
        self.assertEqual(len(db.symbol_states()), 1, "upsert 不许产生第二行")
        # 显式 NULL：允许清空（面板 fail-closed 由 views 负责）
        db.set_symbol_state("BTC_USDT", price=10.0, sma200=None)
        self.assertIsNone(db.symbol_state("BTC_USDT")["sma200"])


# ================================================================ c. snapshot
class TestSnapshotSma200(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.band_window = strategy.BAND_WINDOW
        self.cfg.band_quantile = strategy.BAND_QUANTILE
        self.cfg.min_squeeze_bars = strategy.MIN_SQUEEZE_BARS

    def test_sma200_is_last_closed_bar_same_index_as_sma20(self):
        bars = wide_then_quiet_bars()
        ind = strategy.compute_indicators(bars)
        li = len(bars) - 2
        snap = engine.symbol_snapshot(self.cfg, bars, ind, li)
        self.assertIsNotNone(snap["sma200"], "400+ 根 bar → SMA200 必然有值")
        self.assertAlmostEqual(snap["sma200"], float(ind["sma200"][li]), places=9)
        self.assertAlmostEqual(snap["sma200"], float(ind["sma200"][li]), places=9)
        # 同一根已收盘 bar：与 sma20 同源同索引（不是进行中那根）
        self.assertAlmostEqual(snap["sma20"], float(ind["sma20"][li]), places=9)
        self.assertNotEqual(float(ind["sma200"][li]), float(ind["sma200"][li + 1]))

    def test_sma200_none_without_enough_history(self):
        bars = flat_bars(130, 250.0)                          # < 200 根 → SMA200 还出不来
        ind = strategy.compute_indicators(bars)
        snap = engine.symbol_snapshot(self.cfg, bars, ind, len(bars) - 2)
        self.assertIsNone(snap["sma200"])

    def test_sma200_stored_for_position_symbol_too(self):
        bars = wide_then_quiet_bars()
        ind = strategy.compute_indicators(bars)
        li = len(bars) - 2
        snap = engine.symbol_snapshot(self.cfg, bars, ind, li, has_position=True)
        self.assertAlmostEqual(snap["sma200"], float(ind["sma200"][li]), places=9,
                               msg="持仓标的也照存 sma200（无副作用）")
        # 其余持仓口径不变
        self.assertIsNone(snap["long_trigger"])
        self.assertIsNone(snap["short_trigger"])
        self.assertIsNone(snap["dense_cnt"])

    def test_missing_sma200_sequence_is_null_safe(self):
        bars = wide_then_quiet_bars()
        ind = strategy.compute_indicators(bars)
        ind.pop("sma200")                                     # 序列缺失（老桩）不许炸
        snap = engine.symbol_snapshot(self.cfg, bars, ind, len(bars) - 2)
        self.assertIsNone(snap["sma200"])


# ================================================================ d. tick 写入
class TestTickWritesSma200(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "t.db"))
        self.cfg = Config()
        self.cfg.symbols = ["BTC_USDT"]
        self.notifier = FakeNotifier()
        self.bars = {"BTC_USDT": wide_then_quiet_bars(408, 12)}

    def tearDown(self):
        self.tmp.cleanup()

    def _run_tick(self):
        feeds = {s: FakeFeed(s, b) for s, b in self.bars.items()}

        def fake_fetch(symbol, now=None, allow_fallback=False):
            return feeds[symbol]

        def walk_stub(cfg, symbol, bars, ind, sq, position, from_i, to_i, latest_closed_i):
            # 桩按 symbol 分标的（tick 遍历多标的，不区分会出现幽灵事件）
            return ([], None)

        with mock.patch.object(market, "fetch", side_effect=fake_fetch), \
                mock.patch.object(engine, "walk", side_effect=walk_stub):
            return engine.tick(self.db, self.cfg, self.notifier)

    def test_tick_persists_sma200(self):
        self._run_tick()
        bars = self.bars["BTC_USDT"]
        ind = strategy.compute_indicators(bars)
        li = len(bars) - 2
        row = self.db.symbol_state("BTC_USDT")
        self.assertAlmostEqual(row["sma200"], float(ind["sma200"][li]), places=9)
        self.assertAlmostEqual(row["sma20"], float(ind["sma20"][li]), places=9)
        self.assertAlmostEqual(row["updated_at"], time.time(), delta=60)


# ================================================================ e. payload
class BareDb:
    """只有 recent_positions 的极简 DB（模拟缺 symbol_state 方法的库）。"""

    def recent_positions(self, limit=50):
        return []


class TestPayloadSma200(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "t.db"))
        self.cfg = type("C", (), {"symbols": ["BTC_USDT", "ETH_USDT"]})()

    def tearDown(self):
        self.tmp.cleanup()

    def deps(self, db=None):
        return DashboardDeps(db=db if db is not None else self.db, config_fn=lambda: self.cfg,
                             info=None, address_fn=lambda: "", mode_fn=lambda: "live")

    def test_payload_carries_sma200_and_is_json_serializable(self):
        self.db.set_symbol_state("BTC_USDT", price=100_000.0, sma20=96_000.0,
                                 long_trigger=105_000.0, short_trigger=99_600.0,
                                 dense_cnt=9, sma200=101_000.0)
        p = status_payload(self.deps())
        by_sym = {r["symbol"]: r for r in p["symbol_state"]}
        self.assertAlmostEqual(by_sym["BTC_USDT"]["sma200"], 101_000.0, places=9)
        self.assertIsNone(by_sym["ETH_USDT"]["sma200"], "缺行 → None")
        json.dumps(p["symbol_state"])                         # 不能混进 sqlite Row

    def test_rows_helper_reads_sma200(self):
        self.db.set_symbol_state("BTC_USDT", price=100.0, sma200=90.0)
        rows = _symbol_state_rows(self.deps(), [], ["BTC_USDT", "ETH_USDT"])
        self.assertAlmostEqual(rows[0]["sma200"], 90.0, places=9)
        self.assertIsNone(rows[1]["sma200"])

    def test_bare_db_still_renders_with_null_sma200(self):
        p = status_payload(self.deps(db=BareDb()))
        self.assertTrue(all(r["sma200"] is None for r in p["symbol_state"]))
        self.assertIn("三标的动态", render_dashboard(p))


# ================================================================ f/g. 琥珀判定
class TestShortRowWarnGate(unittest.TestCase):
    """③ 下破开空行：dense≥6 且 sma200 非 None 且 price < sma200 才亮琥珀。"""

    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")

    def render(self, rows):
        return render_dashboard({"symbol_state": rows})

    def row(self, **kw):
        # need(下破开空) = (100000-99600)/100000 = +0.4% < 1% → 条件成立时必亮
        base = {"symbol": "BTC_USDT", "coin": "BTC", "price": 100_000.0, "sma20": 96_000.0,
                "sma200": 101_000.0, "long_trigger": 105_000.0, "short_trigger": 99_600.0,
                "dense_cnt": 9, "updated_at": 1760000000.0, "position_side": None}
        base.update(kw)
        return base

    @staticmethod
    def short_row(html: str) -> str:
        """切出「下破开空」行（到卡片结束为止）。"""
        i = html.index("下破开空")
        return html[i:]

    def test_dense_and_below_ma200_warns(self):
        card = self.short_row(self.render([self.row(dense_cnt=9, price=100_000.0,
                                                    sma200=101_000.0)]))
        self.assertIn(f"{WARN}+0.4%</b>", card)

    def test_dense_but_price_at_or_above_ma200_never_warns(self):
        # 价格已回到 MA200 上方（距下轨仍 0.4%）→ 不开空 → 不亮琥珀
        for ma200 in (99_000.0, 100_000.0, 99_999.0):
            with self.subTest(sma200=ma200):
                card = self.short_row(self.render([self.row(dense_cnt=9, price=100_000.0,
                                                            sma200=ma200)]))
                self.assertNotIn('class="warn"', card)
                self.assertIn("还差 <b>+0.4%</b>", card, "距离照样显示，只是不亮琥珀")

    def test_price_equal_ma200_boundary_is_not_short(self):
        # 严格小于：现价 == MA200 不算「在 MA200 下方」（与 strategy 的收盘 < SMA200 同向）
        card = self.short_row(self.render([self.row(dense_cnt=9, price=100_000.0,
                                                    sma200=100_000.0)]))
        self.assertNotIn('class="warn"', card)

    def test_missing_sma200_fails_closed(self):
        for val in (None, "", "垃圾"):
            with self.subTest(sma200=val):
                card = self.short_row(self.render([self.row(dense_cnt=9, sma200=val)]))
                self.assertNotIn('class="warn"', card)
        # 键整体缺失也算缺失（老 payload / 老库未迁移）
        r = self.row(dense_cnt=9)
        r.pop("sma200")
        self.assertNotIn('class="warn"', self.short_row(self.render([r])))

    def test_dense_gate_still_required(self):
        # 三重：密集不满 6 根 → 即使价在 MA200 下方也不亮
        for dense in (5, 4, 0, None):
            with self.subTest(dense=dense):
                card = self.short_row(self.render([self.row(dense_cnt=dense,
                                                            sma200=101_000.0)]))
                self.assertNotIn('class="warn"', card)

    def test_long_row_regression_ignores_ma200(self):
        # ② 上破开多（need = +0.5%）只看密集满 6 根：MA200 三态都不影响
        for ma200 in (90_000.0, None):
            with self.subTest(sma200=ma200):
                rows = [self.row(dense_cnt=6, sma200=ma200, long_trigger=100_500.0)]
                html = self.render(rows)
                up_row = html[html.index("上破开多"):html.index("下破开空")]
                self.assertIn(f"{WARN}+0.5%</b>", up_row)
        # 密集不满 6 根时做多行照旧不亮（原有门槛保留）
        rows = [self.row(dense_cnt=5, sma200=90_000.0, long_trigger=100_500.0)]
        self.assertNotIn('class="warn"', self.render(rows))

    def test_exit_row_unchanged(self):
        # ① 离场行不受 MA200 与 dense 影响（持仓只判 4h 收盘破线）
        rows = [self.row(position_side=1, sma20=99_500.0, dense_cnt=None, sma200=101_000.0)]
        html = self.render(rows)
        self.assertIn(f"{WARN}+0.5%</b>", html[html.index("距自动离场线"):])
        self.assertNotIn("下破开空", html, "持仓标的只给离场线")


if __name__ == "__main__":
    unittest.main(verbosity=2)
