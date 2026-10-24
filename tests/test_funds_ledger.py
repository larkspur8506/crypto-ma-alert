"""feat/funds-ledger 全链路测试：hl_funding 表 + 同步/回填 + v_ledger 视图 +
记录页「资金流水」区块 + 单粒度资金费收口（P3）。

规格要点（/opt/data/cache/scratch/spec-funds-ledger.md）逐条对应：
1. 迁移（新库建表、老库补表不炸）
2. funding 同步：幂等 / 符号（正=收入不取反）/ 水位增量 / 脏值
3. position 归集：区间内 / 区间外 / 边界时刻
4. v_ledger union：手算 fixture 与视图逐行一致；fee_quote NULL 标注路径
5. running balance：全量正序算完再切片，跨页余额连续；rows 10/20/50 联动
6. 面板渲染：区块在记录页最前、对账行/「未归属」/「老数据缺手续费」可见、数字唯一
7. 确认页 P3 文案：净盈亏与总成本并列、net_pnl 原值未被改动

全部用临时 sqlite DB + 假 info，不触网、不碰生产库。
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB                                              # noqa: E402
from app.hl import funding as hl_funding                           # noqa: E402

NOW = 1_800_000_000.0
ME = "0xme0000000000000000000000000000000000000000"


def _ev(ms: int, coin: str = "BTC", usdc: str = "0.01", szi: str = "-0.001",
        hash_: str | None = None) -> dict:
    """一条实测形状的 userFunding 事件（hash 缺省 = 交易所实测的全零值）。"""
    return {"time": ms,
            "hash": hash_ if hash_ is not None else "0x" + "0" * 64,
            "delta": {"type": "funding", "coin": coin, "usdc": usdc,
                      "szi": szi, "fundingRate": "0.0000125", "nSamples": 7}}


def _pos(db: DB, symbol: str = "BTC_USDT", opened: float = 2000.0,
         closed: float | None = 5000.0, status: str = "closed") -> int:
    db.conn.execute(
        "INSERT INTO positions(symbol, signal_bar_ms, status, side, entry_price, stop_price,"
        " stop_dist, contracts, coin_qty, notional, risk_u, created_at, opened_at, closed_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, 1, status, 1, 100, 90, 10, 0, 0, 0, 0, opened - 100, opened, closed))
    db.conn.commit()
    return int(db.conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _fill(db: DB, tid: str, coin: str = "BTC", ts: float = 2100.0,
          closed_pnl: float = 0.0, fee_quote: float | None = 0.02,
          fee: float = 0.02, dir_: str = "Open Long") -> None:
    db.hl_upsert_fill(tid=tid, symbol=f"{coin}_USDT", coin=coin, side=1, sz=0.001,
                      px=100, fee=fee, fee_token="USDC", fee_quote=fee_quote,
                      dir=dir_, closed_pnl=closed_pnl, hash=f"h{tid}",
                      reduce_only=("Close" in dir_), oid=None, ts=ts, created_at=ts)


# ================================================================ 1. 迁移
class TestMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="funds_ledger_")
        self.path = os.path.join(self.tmp, "t.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_db_has_table_and_view(self):
        db = DB(self.path)
        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("hl_funding", tables)
        views = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'")}
        self.assertIn("v_ledger", views)

    def test_old_db_gets_table_back_without_alters(self):
        """老库升级：表被删（模拟升级前的老库没有它）→ 重开连接补建，其它表不动。"""
        db = DB(self.path)
        db.hl_add_flow({"ts": 1.0, "kind": "send_in", "amount": 1.0, "hash": "x"})
        db.conn.execute("DROP TABLE hl_funding")
        db.conn.commit()
        db2 = DB(self.path)                       # 老库路径重开 → executescript 补表
        tables = {r[0] for r in db2.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("hl_funding", tables)
        self.assertEqual(len(db2.hl_flows_since(0)), 1, "老数据必须原样保留")
        # 视图引用三源，任一表被补齐后必须可查询
        self.assertEqual(db2.hl_ledger_rows()[0]["amount_usd"], 1.0)

    def test_view_rebuilt_on_reopen(self):
        """视图定义升级：DROP+CREATE 保证重开连接后是最新口径（不是老定义残留）。"""
        db = DB(self.path)
        db.conn.execute("DROP VIEW v_ledger")
        db.conn.commit()
        db2 = DB(self.path)
        self.assertIn("v_ledger", {r[0] for r in db2.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'")})


# ================================================================ 2. 解析 / 符号 / ref
class TestParseEvent(unittest.TestCase):
    def test_symbol_positive_is_income_no_flip(self):
        ev = hl_funding.parse_event(_ev(1_700_000_000_000, usdc="0.006007"))
        self.assertAlmostEqual(ev["amount_usd"], 0.006007)
        self.assertAlmostEqual(ev["ts"], 1_700_000_000.0)
        self.assertEqual(ev["coin"], "BTC")

    def test_negative_stays_negative(self):
        ev = hl_funding.parse_event(_ev(1_700_000_000_000, usdc="-0.25"))
        self.assertAlmostEqual(ev["amount_usd"], -0.25, "付出为负，绝不取反")

    def test_dirty_rows_dropped(self):
        for bad in (None, "x", {}, {"time": 0, "delta": {"type": "funding"}},
                    {"time": 1_700_000_000_000, "delta": {"type": "other", "coin": "BTC", "usdc": "1"}},
                    {"time": 1_700_000_000_000, "delta": {"type": "funding", "usdc": "1"}},
                    {"time": 1_700_000_000_000, "delta": {"type": "funding", "coin": "", "usdc": "1"}},
                    {"time": 1_700_000_000_000, "delta": {"type": "funding", "coin": "BTC", "usdc": "abc"}}):
            self.assertIsNone(hl_funding.parse_event(bad), f"{bad!r} 应被丢弃")

    def test_ref_prefers_nonzero_hash(self):
        h = "0xabc123"
        ev = hl_funding.parse_event(_ev(1_700_000_000_000, hash_=h))
        self.assertEqual(hl_funding.ref_of(ev), f"hl:{h}")

    def test_ref_falls_back_to_composite_on_zero_hash(self):
        """实测 userFunding 的 hash 恒为全零 → 组合键 t(ms)+coin+amount。"""
        ev = hl_funding.parse_event(_ev(1_700_000_000_000, coin="BTC", usdc="0.006007"))
        self.assertEqual(hl_funding.ref_of(ev), "1700000000000|BTC|+0.006007")
        ev2 = hl_funding.parse_event(_ev(1_700_000_000_000, coin="ETH", usdc="-0.1"))
        self.assertNotEqual(hl_funding.ref_of(ev), hl_funding.ref_of(ev2))


# ================================================================ 2b. 同步：幂等 / 水位 / 脏值
class _FakeInfo:
    """带 user_funding_history 的假 info（记录每次 startTime，模拟分页/水位）。"""

    def __init__(self, events=None, raise_=False):
        self.events = events or []
        self.raise_ = raise_
        self.starts: list = []

    def user_funding_history(self, address, start_ms, end_ms=None):
        self.starts.append(start_ms)
        if self.raise_:
            raise RuntimeError("userFunding down")
        return [e for e in self.events if e["time"] >= start_ms]


class TestSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="funds_sync_")
        self.db = DB(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idempotent_same_batch_twice(self):
        events = [_ev(1_700_000_000_000 + i * 3_600_000, usdc=f"0.0{i}") for i in range(5)]
        r1 = hl_funding.record_funding(self.db, events)
        self.assertEqual(r1["added"], 5)
        r2 = hl_funding.record_funding(self.db, events)     # 同一批再拉一次
        self.assertEqual(r2["added"], 0, "ref UNIQUE 必须兜底幂等")
        self.assertEqual(self.db.conn.execute(
            "SELECT count(*) FROM hl_funding").fetchone()[0], 5)

    def test_watermark_incremental(self):
        info = _FakeInfo([_ev(1_700_000_000_000, usdc="0.01")])
        hl_funding.sync_funding(info, self.db, ME)
        self.assertEqual(info.starts[0], 0, "首次全量回填从回填起点开始")
        info2 = _FakeInfo([_ev(1_700_003_600_000, usdc="0.02")])
        hl_funding.sync_funding(info2, self.db, ME)
        # 水位 = max(ts) − 60s 重叠
        self.assertEqual(info2.starts[0], (1_700_000_000 - 60) * 1000)

    def test_backfill_origin_earlier_of_snapshot_and_flows(self):
        """回填起点 = 首张正权益快照 与 最早流水 取早者（spec 拍板）。"""
        self.db.hl_add_flow({"ts": 1_700_000_000.0, "kind": "send_in",
                             "amount": 100.0, "hash": "f1"})
        self.db.hl_add_snapshot({"ts": 1_699_900_000.0, "equity_total": 50.0})
        info = _FakeInfo([_ev(1_700_000_000_000, usdc="0.01")])
        hl_funding.sync_funding(info, self.db, ME)
        self.assertEqual(info.starts[0], 1_699_900_000 * 1000, "快照更早 → 取快照")

    def test_dirty_events_skipped_not_fatal(self):
        events = [_ev(1_700_000_000_000, usdc="0.01"), {"garbage": True},
                  _ev(1_700_000_000_001, usdc="oops"), None]
        r = hl_funding.record_funding(self.db, events)
        self.assertEqual(r["added"], 1)
        self.assertEqual(r["skipped"], 3)

    def test_raw_archived(self):
        hl_funding.record_funding(self.db, [_ev(1_700_000_000_000, usdc="0.01")])
        raw = json.loads(self.db.conn.execute(
            "SELECT raw FROM hl_funding").fetchone()[0])
        self.assertEqual(raw["delta"]["coin"], "BTC", "原始 JSON 必须存档")


# ================================================================ 3. position 归集
class TestAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="funds_attr_")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.pid = _pos(self.db, opened=2000.0, closed=5000.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _match(self, ts):
        positions = self.db.recent_positions(1000)
        return hl_funding.match_position(positions, "BTC", ts)

    def test_inside_window(self):
        self.assertEqual(self._match(3000.0), self.pid)

    def test_outside_window(self):
        self.assertIsNone(self._match(1000.0), "开仓前 → 未归属")
        self.assertIsNone(self._match(6000.0), "平仓后 → 未归属")

    def test_boundary_inclusive(self):
        """拍板：±300s 闭区间（与引擎 pnl.window_bounds 同口径）——边界时刻命中。"""
        self.assertEqual(self._match(2000.0 - 300), self.pid, "opened_at−300 边界命中")
        self.assertEqual(self._match(5000.0 + 300), self.pid, "closed_at+300 边界命中")
        self.assertIsNone(self._match(2000.0 - 301), "窗口外 1 秒 → 不归属")

    def test_open_position_no_upper_bound(self):
        pid = _pos(self.db, symbol="ETH_USDT", opened=1000.0, closed=None,
                   status="open")
        positions = self.db.recent_positions(1000)
        got = hl_funding.match_position(positions, "ETH", 999999.0)
        self.assertEqual(got, pid, "在持仓位只卡下界")

    def test_coin_mismatch_never_attributed(self):
        got = hl_funding.match_position(self.db.recent_positions(1000), "SOL", 3000.0)
        self.assertIsNone(got, "coin 对不上 → 绝不猜归属")

    def test_record_funding_writes_position_id(self):
        hl_funding.record_funding(self.db, [_ev(3000_000, usdc="0.05")])
        row = self.db.conn.execute("SELECT position_id FROM hl_funding").fetchone()
        self.assertEqual(row["position_id"], self.pid)


# ================================================================ 4. v_ledger union
class TestVLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="funds_view_")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        # 手算 fixture：+500 入金；开仓腿 −0.02（fee）；平仓腿 +0.98（closed_pnl 1 − fee 0.02）；
        # 资金费 +0.05（无持仓期）；老行缺 fee_quote → 金额按 0 手续费计并标注。
        self.db.hl_add_flow({"ts": 1000.0, "kind": "send_in", "amount": 500.0, "hash": "h1"})
        self.pid = _pos(self.db, opened=2000.0, closed=5000.0)
        _fill(self.db, "t1", ts=2100, closed_pnl=0.0, fee_quote=0.02, dir_="Open Long")
        _fill(self.db, "t2", ts=4900, closed_pnl=1.0, fee_quote=0.02, dir_="Close Long")
        _fill(self.db, "t3", ts=2200, closed_pnl=0.0, fee_quote=None, fee=0.01,
              dir_="Open Long")
        hl_funding.record_funding(self.db, [_ev(3000_000, usdc="0.05")])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rows_amounts_kinds_order(self):
        rows = self.db.hl_ledger_rows()
        got = [(r["kind"], round(r["amount_usd"], 6)) for r in rows]
        # 时间正序；老行（fee_quote NULL）金额 = closed_pnl − 0
        self.assertEqual(got, [
            ("send_in", 500.0),
            ("fill_open", -0.02),
            ("fill_open", 0.0),            # 老行：缺手续费，按 0 计入金额
            ("funding", 0.05),
            ("fill_close", 0.98),
        ])
        # Σ = 500 − 0.02 + 0 + 0.05 + 0.98
        self.assertAlmostEqual(self.db.hl_ledger_sum(), 501.01, places=6)

    def test_fill_attribution_in_view(self):
        rows = [r for r in self.db.hl_ledger_rows() if r["src"] == "fill"]
        self.assertTrue(rows and all(r["position_id"] == self.pid for r in rows),
                        "成交行应归集到窗口内的仓位")

    def test_fee_missing_flagged_not_silently_zero(self):
        old = [r for r in self.db.hl_ledger_rows() if r["ref"] == "fill:t3"]
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0]["fee_missing"], 1, "fee_quote NULL 必须带标注位")
        fresh = [r for r in self.db.hl_ledger_rows() if r["ref"] == "fill:t1"]
        self.assertEqual(fresh[0]["fee_missing"], 0)

    def test_flow_kinds_pass_through_signed(self):
        """send_in → +amount；send_out → −amount（落库时已带符号，视图原样透传）。"""
        self.db.hl_add_flow({"ts": 6000.0, "kind": "send_out", "amount": -50.0, "hash": "h2"})
        row = [r for r in self.db.hl_ledger_rows() if r["kind"] == "send_out"][0]
        self.assertAlmostEqual(row["amount_usd"], -50.0)


# ================================================================ 5. running balance
def _seed_25(db: DB) -> None:
    """25 行流水：金额 = +1（偶数行）/ −0.5（奇数行），时间每行隔 1 秒。"""
    for i in range(25):
        db.hl_add_flow({"ts": 1000.0 + i, "kind": "send_in" if i % 2 == 0 else "send_out",
                        "amount": 1.0 if i % 2 == 0 else -0.5, "hash": f"h{i}"})


class TestRunningBalance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="funds_bal_")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        _seed_25(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_history_then_slice_pages_10_20_50(self):
        rows = self.db.hl_ledger_rows()
        balances = hl_funding.running_balances(rows)
        self.assertAlmostEqual(sum(r["amount_usd"] for r in rows), balances[-1])
        # 13 行 +1、12 行 −0.5 → 期末 13 − 6 = 7
        self.assertAlmostEqual(balances[-1], 7.0, places=6)
        for n in (10, 20, 50):
            seen: list = []
            start = len(rows)
            while start > 0:                      # 模拟渲染层翻页（每页 n 行，最新在前）
                page = rows[max(0, start - n):start]
                if not page:
                    break
                seen.extend(page)
                start -= n
            self.assertEqual(len(seen), len(rows), f"n={n} 必须覆盖全量")
            # 每行带上的余额必须与全量正序算的一致（跨页连续，不能页内现算）
            for r in seen:
                idx = rows.index(r)
                self.assertAlmostEqual(balances[idx],
                                       sum(x["amount_usd"] for x in rows[:idx + 1]),
                                       places=6)

    def test_page1_latest_first_with_correct_balances(self):
        """首页（最新 10 条，倒序）的余额是全量算出来的，不是页内现算。"""
        rows = self.db.hl_ledger_rows()
        balances = hl_funding.running_balances(rows)
        page = list(zip(rows[-10:], balances[-10:]))
        page.reverse()
        # 全表第 25 行（最后一行）余额 = 7.0；它显示在首页第一行
        self.assertAlmostEqual(page[0][1], 7.0, places=6)
        self.assertAlmostEqual(page[-1][1], balances[len(rows) - 10], places=6,
                               msg="首页最旧一行的余额 = 全量第 16 行的累计值")

    def test_balance_continuous_across_three_pages_of_10(self):
        rows = self.db.hl_ledger_rows()
        balances = hl_funding.running_balances(rows)
        # 按「最新在前」切成每页 10 行的三页，逐页检查相邻行余额差 = 该行金额
        ordered = list(zip(rows, balances))[::-1]
        for page_start in range(0, len(ordered), 10):
            chunk = ordered[page_start:page_start + 10]
            for (r, b), (r_prev, b_prev) in zip(chunk, [chunk[0]] + chunk[:-1]):
                if r_prev is not r:
                    # 最新在前：上一行（更新）的余额 − 当前行（更旧）的余额 = 更新那行的金额
                    self.assertAlmostEqual(b_prev - b, r_prev["amount_usd"], places=6,
                                           msg="余额列必须跨页连续")


# ================================================================ 6. 面板渲染
try:
    from fastapi import FastAPI                                        # noqa: E402
    from fastapi.testclient import TestClient                          # noqa: E402
    from tests.test_admin_ui import BaseCase, TOKEN                    # noqa: E402
    _HAVE_FASTAPI = True
except ImportError:                                                    # pragma: no cover
    _HAVE_FASTAPI = False
    BaseCase = unittest.TestCase                                       # type: ignore


@unittest.skipUnless(_HAVE_FASTAPI, "fastapi 未安装")
class TestPanelRender(BaseCase):
    """记录页「资金流水」区块：位置 / 可见文本 / 数字唯一性 / 行数联动。"""

    def _seed(self):
        self.db.hl_add_flow({"ts": 1000.0, "kind": "send_in", "amount": 500.0, "hash": "h1"})
        self.db.hl_add_flow({"ts": 2000.0, "kind": "send_out", "amount": -100.0, "hash": "h2"})
        self.pid = _pos(self.db, opened=3000.0, closed=6000.0)
        _fill(self.db, "t1", ts=3100, closed_pnl=0.0, fee_quote=0.02, dir_="Open Long")
        _fill(self.db, "t9", ts=3200, closed_pnl=0.0, fee_quote=None, dir_="Open Long")
        _fill(self.db, "t2", ts=6000, closed_pnl=1.0, fee_quote=0.02, dir_="Close Long")
        # 两条资金费：4000s 在仓位窗内（归集 pid）；9000s 在所有窗之外 → 「无持仓期资金费」
        hl_funding.record_funding(self.db, [_ev(4000_000, usdc="0.05"),
                                            _ev(9000_000, usdc="0.02")])
        # Σ = 500 −100 −0.02 +0 +0.05 +0.98 +0.02 = 401.03；权益故意取不相干的 437.26
        # → 残差 36.23（避免与任何余额列数值撞车，数字唯一性断言才成立）
        self.db.hl_add_snapshot({"ts": NOW, "equity_total": 437.26})

    def _records_html(self, n: int = 20) -> str:
        from app.admin_ui import render_records
        return render_records(self.deps, n, [])

    def test_block_is_first_h2(self):
        self._seed()
        h = self._records_html()
        h2s = [seg.split("</h2>")[0] for seg in h.split("<h2>")[1:]]
        self.assertEqual(h2s[0], "资金流水", "资金流水必须是记录页第一个 <h2> 区块")
        self.assertIn("记录", h2s[1])

    def test_visible_texts(self):
        self._seed()
        h = self._records_html()
        for text in ("Σ全部流水", "当前账户权益（最新快照）", "残差",
                     "余额 = 自账户 0 起累计", "老数据缺手续费", "未归属", "入金", "出金",
                     "资金费", "平仓成交"):
            self.assertIn(text, h, f"可见文本缺：{text}")

    def test_unattributed_funding_wording(self):
        self._seed()
        h = self._records_html()
        self.assertIn("无持仓期资金费", h, "无在持仓位的资金费必须如实标注")

    def test_recon_numbers(self):
        self._seed()
        h = self._records_html()
        # Σ全部流水 = 401.03（手算：500−100−0.02+0+0.05+0.98+0.02）
        self.assertIn("401.03", h)
        self.assertIn("437.26", h)
        self.assertIn("36.23", h)

    def test_same_number_appears_once(self):
        self._seed()
        h = self._records_html()
        # 权益与残差是口径数字，全页只许出现一次（余额列数值故意错开不撞车）
        for num in ("437.26", "36.23"):
            self.assertEqual(h.count(num), 1, f"数字 {num} 全页只许出现一次")

    def test_rows_cookie_10_20_50(self):
        _seed_25(self.db)
        for n, expect in ((10, 10), (20, 20), (50, 25)):
            h = self._records_html(n)
            self.assertEqual(h.count("<tr><td class='num'>"), expect + 0,
                             f"n={n} 的资金流水行数错误")
        # 与底部选择器同一个 cookie：?n= 语义已由 normalize_rows 保证，这里验证
        # 资金流水区块吃的是同一个 n（不单设行数入口）。

    def test_empty_db_renders_gracefully(self):
        h = self._records_html()
        self.assertIn("资金流水", h)
        self.assertIn("暂无流水", h)

    def test_ledger_read_error_shown(self):
        """v_ledger 坏了必须显示错误（不许渲染成空表装没事）。"""
        from app.admin_ui import render_records
        class BadDB:
            def __getattr__(self, name):
                raise RuntimeError("boom")
        deps = self.deps
        old = deps.db
        deps.db = BadDB()
        try:
            h = render_records(deps, 20, [])
        finally:
            deps.db = old
        self.assertIn("读取失败", h)


# ================================================================ 7. P3 确认页/持仓卡
@unittest.skipUnless(_HAVE_FASTAPI, "fastapi 未安装")
class TestConfirmPageP3(unittest.TestCase):
    """/c 摘要页：HL 仓新增资金费两行；net_pnl 原口径原值不动。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="funds_p3_")
        os.environ["DB_PATH"] = os.path.join(self.tmp, "t.db")
        os.environ["ENABLE_SCHEDULER"] = "false"
        os.environ["ADMIN_TOKEN"] = "tok"
        os.environ["TOKEN_SECRET"] = "sec"
        os.environ["BASE_URL"] = "https://smoke.example"
        os.environ["DRY_RUN"] = "true"
        self.mod = importlib.import_module("app.main")
        importlib.reload(self.mod)
        self.db = self.mod.db
        self.pid = _pos(self.db, opened=2000.0, closed=5000.0)
        self.db.conn.execute(
            "UPDATE positions SET hl_status='closed', net_pnl=0.96, pnl_source='fills'"
            " WHERE id=?", (self.pid,))
        self.db.conn.commit()
        _fill(self.db, "t1", ts=2100, closed_pnl=0.0, fee_quote=0.02, dir_="Open Long")
        _fill(self.db, "t2", ts=4900, closed_pnl=1.0, fee_quote=0.02, dir_="Close Long")
        # 归集一条资金费给本仓（+0.05）+ 一条无持仓期的（不归集）
        hl_funding.record_funding(self.db, [_ev(3000_000, usdc="0.05"),
                                            _ev(9000_000, usdc="0.07")])
        self.client = TestClient(self.mod.app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self):
        t = self.mod.engine.sign_token(self.mod.config.token_secret, self.pid, "view")
        return self.client.get(f"/c/{self.pid}", params={"a": "view", "t": t})

    def test_hl_row_shows_funding_net_and_cost(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        h = r.text
        self.assertIn("本单资金费合计", h)
        self.assertIn("+0.05 U", h)
        self.assertIn("本单净盈亏（含手续费，不含资金费）", h)
        self.assertIn("+0.96 U", h, "净盈亏 = net_pnl 原值")
        self.assertIn("本单总成本（含资金费）", h)
        self.assertIn("0.04 U", h, "总成本 = 手续费 0.04 + 资金费支出 0")

    def test_net_pnl_in_db_untouched(self):
        self._get()
        row = self.db.position_by_id(self.pid)
        self.assertAlmostEqual(row["net_pnl"], 0.96, "net_pnl 原口径一律不动")
        self.assertEqual(row["pnl_source"], "fills")

    def test_non_hl_row_has_no_funding_lines(self):
        pid2 = _pos(self.db, symbol="ETH_USDT", opened=100.0, closed=200.0)
        self.db.conn.execute("UPDATE positions SET hl_status=NULL WHERE id=?", (pid2,))
        self.db.conn.commit()
        t = self.mod.engine.sign_token(self.mod.config.token_secret, pid2, "view")
        r = self.client.get(f"/c/{pid2}", params={"a": "view", "t": t})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("本单资金费合计", r.text, "非 HL 仓不加资金费行")

    def test_no_funding_record_shown_honestly(self):
        pid3 = _pos(self.db, symbol="SOL_USDT", opened=100.0, closed=200.0)
        self.db.conn.execute("UPDATE positions SET hl_status='closed' WHERE id=?", (pid3,))
        self.db.conn.commit()
        t = self.mod.engine.sign_token(self.mod.config.token_secret, pid3, "view")
        r = self.client.get(f"/c/{pid3}", params={"a": "view", "t": t})
        self.assertIn("暂无资金费记录", r.text, "没有资金费记录就明说，不猜 0")


# ================================================================ 8. 执行器挂点
class TestExecutorHook(unittest.TestCase):
    """_sync_flows 闸门内顺带拉资金费；失败不影响 _flows_fresh。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="funds_hook_")
        self.db = DB(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _exec(self, info):
        from app.hl import MODE_LIVE
        from app.hl.executor import Executor, ExecParams
        p = ExecParams(mode=MODE_LIVE, account_address=ME, private_key="0xkey",
                       equity=1000.0, code_rev="testrev")
        return Executor(self.db, p, info, None), p

    def test_flows_gate_also_pulls_funding(self):
        info = _HookInfo(flows=[], funding=[_ev(1_700_000_000_000, usdc="0.01")])
        ex, _ = self._exec(info)
        ex._sync_flows(now=NOW)
        self.assertEqual(self.db.conn.execute(
            "SELECT count(*) FROM hl_funding").fetchone()[0], 1,
            "同一轮闸门必须顺带落资金费")

    def test_funding_failure_does_not_break_flows_fresh(self):
        info = _HookInfo(flows=[], funding_raise=True)
        ex, _ = self._exec(info)
        ex._sync_flows(now=NOW)
        self.assertTrue(ex._flows_fresh, "资金费失败绝不能弄脏流水的可信标记")
        evs = self.db.hl_recent_events(20, kind="error")
        self.assertTrue(evs, "失败要留 error 事件")

    def test_gate_closed_skips_funding_too(self):
        info = _HookInfo(flows=[], funding=[_ev(1, usdc="0.01")])
        ex, p = self._exec(info)
        self.db.set_config("hl_flows_synced_at", str(int(NOW - 60)))   # 闸门内
        ex._sync_flows(now=NOW)
        self.assertEqual(info.funding_calls, 0, "闸门未开就不许多发请求")


class _HookInfo:
    """ledger_updates + user_funding_history 双能力替身（记录各自调用次数）。"""

    def __init__(self, flows=None, funding=None, funding_raise=False):
        self._flows = flows or []
        self._funding = funding or []
        self._funding_raise = funding_raise
        self.flows_calls = 0
        self.funding_calls = 0

    def ledger_updates(self, address, start_ms=0, deadline_ts=None, **kw):
        self.flows_calls += 1
        return list(self._flows)

    def user_funding_history(self, address, start_ms, end_ms=None):
        self.funding_calls += 1
        if self._funding_raise:
            raise RuntimeError("userFunding down")
        return [e for e in self._funding if e["time"] >= start_ms]


if __name__ == "__main__":
    unittest.main()
