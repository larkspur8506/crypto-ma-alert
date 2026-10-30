"""feat/funds-ledger 全链路测试：hl_funding 表 + 同步/回填 + v_ledger 视图 +
记录页「资金流水」区块 + 单粒度资金费收口（P3）。

规格要点（/opt/data/cache/scratch/spec-funds-ledger.md）逐条对应：
1. 迁移（新库建表、老库补表不炸）
2. funding 同步：幂等 / 符号（正=收入不取反）/ 水位增量 / 脏值
3. position 归集：区间内 / 区间外 / 边界时刻
4. v_ledger union：手算 fixture 与视图逐行一致；fee_quote NULL 标注路径
5. running balance：全量正序算完再切片，跨页余额连续；rows 10/20/50 联动
6. 面板渲染：区块在记录页最前、对账行/「未归属」/「老数据缺手续费」可见、数字唯一
7. （已删除·PR B）确认页 P3 文案：随 /c/{pid} 确认页退役

全部用临时 sqlite DB + 假 info，不触网、不碰生产库。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
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
          fee: float = 0.02, dir_: str = "Open Long",
          fee_token: str = "USDC") -> None:
    db.hl_upsert_fill(tid=tid, symbol=f"{coin}_USDT", coin=coin, side=1, sz=0.001,
                      px=100, fee=fee, fee_token=fee_token, fee_quote=fee_quote,
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


# ================================================================ 2c. H1：分页游标含边界
class _PagedInfo:
    """模拟真实 userFunding 分页：startTime **含边界**、单页最多 FUNDING_PAGE 条。

    这是 H1 的关键替身形状：请求 start=T 时，time==T 的事件会**再次返回**
    （实测 startTime 为包含语义）。旧实现游标 last_t+1 会把同毫秒簇里排在
    页边界之后的成员永久跳过。
    """

    def __init__(self, events):
        self.events = sorted(events, key=lambda e: (e["time"], e["delta"]["coin"]))
        self.starts: list = []

    def user_funding_history(self, address, start_ms, end_ms=None):
        self.starts.append(start_ms)
        window = [e for e in self.events if e["time"] >= start_ms]
        return window[:hl_funding.FUNDING_PAGE]


class TestFetchPaginationH1(unittest.TestCase):
    """H1 回归：同毫秒跨币簇跨 500 行页边界，多轮拉取后行数必须完整。

    变异验证（必须做）：把 funding.py 游标改回 ``int(last_t) + 1`` 跑本类
    **必须 FAIL**（同毫秒簇尾部成员被永久跳过），改回后绿。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="funds_h1_")
        self.db = DB(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cluster_events(self):
        """502 条：498 条在 T0−1000ms，3 条同毫秒簇在 T0（跨页边界），1 条在 T1 抬水位。"""
        t0 = 1_700_000_000_000
        events = []
        for i in range(498):                       # 第一页装满 500 的主体
            events.append(_ev(t0 - 1000, coin="BTC", usdc=f"{i + 1}.0"))
        for j, coin in enumerate(("BTC", "ETH", "SOL")):   # 同毫秒簇：页边界落在簇内
            events.append(_ev(t0, coin=coin, usdc=f"900.{j + 1}"))
        events.append(_ev(t0 + 60_000, coin="BTC", usdc="950.0"))   # 更晚事件抬高水位
        return events

    def test_same_ms_cluster_across_page_boundary_complete(self):
        info = _PagedInfo(self._cluster_events())
        r1 = hl_funding.sync_funding(info, self.db, ME)
        total = self.db.conn.execute("SELECT count(*) FROM hl_funding").fetchone()[0]
        self.assertEqual(total, 502,
                         "同毫秒簇跨页边界的成员不能被 last_t+1 永久跳过")
        self.assertEqual(r1["added"], 502)
        # 多轮拉取：水位 = max(ts) − 60s 重叠 → 恰好回到 T0，簇整批重拉，ref 幂等吸收
        r2 = hl_funding.sync_funding(info, self.db, ME)
        self.assertEqual(r2["added"], 0, "含边界重拉必须被 ref UNIQUE 幂等吸收")
        total2 = self.db.conn.execute("SELECT count(*) FROM hl_funding").fetchone()[0]
        self.assertEqual(total2, 502, "第二轮拉取后行数不变（不重不漏）")
        # 簇里三条都必须在库（旧实现会永久丢掉排在页边界后的簇成员）
        coins = {r["coin"] for r in self.db.conn.execute(
            "SELECT coin FROM hl_funding WHERE ts = ?", (1_700_000_000.0,))}
        self.assertEqual(coins, {"BTC", "ETH", "SOL"}, "同毫秒簇三个币都必须落库")


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

    def test_fee_token_passthrough_two_causes(self):
        """M3：fee_missing 两种成因在视图层就能区分 —— fee_token 原样透传。

        渲染层按 fee_token 是否计价币分流文案：非计价币 → 「费用未折算」，
        计价币（含空）→ 「老数据缺手续费」。这里钉住视图透传，渲染分流在
        TestPanelRender.test_fee_missing_wording_two_causes。
        """
        # ARB 计价手续费（fee_quote 按设计为 None，不是「老数据」）
        _fill(self.db, "t7", ts=2300, closed_pnl=0.0, fee_quote=None, fee=1.5,
              fee_token="ARB", dir_="Open Long")
        rows = {r["ref"]: r for r in self.db.hl_ledger_rows()}
        self.assertEqual(rows["fill:t7"]["fee_missing"], 1)
        self.assertEqual(rows["fill:t7"]["fee_token"], "ARB",
                         "非计价币 fee_token 必须透传给渲染层分流")
        self.assertEqual(rows["fill:t3"]["fee_missing"], 1)
        self.assertEqual(rows["fill:t3"]["fee_token"], "USDC",
                         "计价币 + fee_quote NULL = 真老数据路径")
        # 非成交源（flow/funding）没有手续费概念 → fee_token 为空串而不是 NULL
        self.assertEqual(rows["flow:1"]["fee_token"], "")
        self.assertEqual(rows["fund:3000000|BTC|+0.050000"]["fee_token"], "")


# ================================================================ 4b. M2：毫秒 ts 归一
class TestVLedgerMsTimestamp(unittest.TestCase):
    """M2 回归：hl_fills 里 >1e12 的毫秒 ts 行必须被 v_ledger 归一成秒。

    变异验证：把 db.py 的 fill_ts CASE 归一改回裸 ``COALESCE(l.ts, l.created_at)``
    跑本类**必须 FAIL**（毫秒行排到表尾、永不归集、余额错位），改回后绿。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="funds_ms_")
        self.db = DB(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ms_row_normalized_order_attributed_balance(self):
        # 1 入金 + 毫秒开仓腿 + 资金费 + 秒级平仓腿；窗口用真实量级 epoch（毫秒才 >1e12）
        self.db.hl_add_flow({"ts": 1_699_990_000.0, "kind": "send_in",
                             "amount": 500.0, "hash": "h1"})
        pid = _pos(self.db, opened=1_699_999_000.0, closed=1_700_001_000.0)
        _fill(self.db, "m1", ts=1_700_000_000_000, closed_pnl=0.0, fee_quote=0.02,
              dir_="Open Long")                       # 毫秒行（历史导入形状）
        _fill(self.db, "m2", ts=1_700_000_500.0, closed_pnl=1.0, fee_quote=0.02,
              dir_="Close Long")
        hl_funding.record_funding(self.db, [_ev(1_700_000_250_000, usdc="0.05")])
        rows = self.db.hl_ledger_rows()
        # 排序：毫秒行按归一后的秒参与排序，不能被甩到表尾
        # （flow ref 是自增 id；funding ref 的金额走 ref_of 的 6 位小数格式）
        self.assertEqual([r["ref"] for r in rows],
                         ["flow:1", "fill:m1",
                          "fund:1700000250000|BTC|+0.050000", "fill:m2"],
                         "毫秒行必须归一成秒后参与排序")
        # 归集：毫秒行落在仓位窗口内 → position_id（不归一就永远窗口外）
        m1 = rows[1]
        self.assertEqual(m1["position_id"], pid, "毫秒行必须能归集到 position")
        self.assertAlmostEqual(m1["ts"], 1_700_000_000.0, places=6,
                               msg="视图吐出的 ts 必须是归一后的秒")
        # 后续余额不错位：500 − 0.02 → +0.05 → +0.98
        bals = hl_funding.running_balances(rows)
        self.assertAlmostEqual(bals[1], 499.98, places=6)
        self.assertAlmostEqual(bals[2], 500.03, places=6)
        self.assertAlmostEqual(bals[3], 501.01, places=6)


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

    # ---- M1：渲染层余额列数值断言（此前 8/8 用例对余额数值零断言）----

    def _ledger_balance_cells(self, h: str) -> list:
        """从「资金流水」区块的表格里抽出每行的余额列（第 4 个 <td>）文本。"""
        seg = h.split("<h2>资金流水</h2>", 1)[1].split("<h2>", 1)[0]
        out = []
        for row in re.findall(r"<tr><td class='num'>.*?</tr>", seg):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row)
            if len(cells) == 6:
                out.append(cells[3].strip())
        return out

    def test_balance_column_values_rendered(self):
        """M1 变异验证：running_balances 整列偏移 +1000 后本用例**必须 FAIL**。

        12 条 +100 流水，页只显示最新 10 条：余额列必须是 1200…300（全量累计），
        任何对余额数值的篡改（+1000 偏移、页内现算）都会让这列数值对不上。
        """
        for i in range(12):
            self.db.hl_add_flow({"ts": 1000.0 + i, "kind": "send_in",
                                 "amount": 100.0, "hash": f"bh{i}"})
        h = self._records_html(10)
        got = self._ledger_balance_cells(h)
        expect = [f"{v:,.2f}" for v in range(1200, 299, -100)]   # 最新在前
        self.assertEqual(len(got), 10, f"应渲染 10 行余额列，实际 {len(got)}")
        self.assertEqual(got, expect,
                         "余额列必须逐格等于全量正序累计值（最新在前）")

    def test_balance_not_page_local(self):
        """结构性用例：改成「页内现算余额」则 FAIL。

        页内第一行自己的金额是 +100，但它的余额是 1,200 —— 前面还有 11 条
        历史。页内现算会把首行余额画成 100.00，这里直接钉死 1,200.00。
        """
        for i in range(12):
            self.db.hl_add_flow({"ts": 1000.0 + i, "kind": "send_in",
                                 "amount": 100.0, "hash": "bh2"})
        got = self._ledger_balance_cells(self._records_html(10))
        self.assertEqual(got[0], "1,200.00",
                         "首页最新一行的余额 = 全账户累计（含页外 11 条历史），"
                         "页内现算会错成 100.00")
        self.assertEqual(got[-1], "300.00",
                         "首页最旧一行的余额含更早 2 条页外历史（300 而非 100）")

    def test_fee_missing_wording_two_causes(self):
        """M3：fee_missing 两种成因渲染文案分流 —— 非计价币 vs 真老数据。"""
        self._seed()                    # t9 = USDC 但 fee_quote 缺失（真老数据路径）
        _fill(self.db, "t8", ts=3300, closed_pnl=0.0, fee_quote=None, fee=1.5,
              fee_token="ARB", dir_="Open Long")      # 非计价币：fee_quote 按设计 None
        h = self._records_html()
        self.assertIn("费用未折算（非计价币 ARB）", h,
                      "非计价币手续费必须说「费用未折算」，不能冒充老数据")
        self.assertIn("老数据缺手续费", h,
                      "计价币但 fee_quote 缺失（t9）仍走「老数据缺手续费」路径")

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


# ================================================================ 7. P3 确认页（已删）
# 2026-10-30 PR B：/c/{pid} 确认页退役，main._hl_funding_lines（P3 的「本单资金费合计 /
# 本单总成本」两行）随页面一起删除，本节的四个渲染用例（HL 仓两行、net_pnl 原值不动、
# 非 HL 仓不加行、无记录如实说）随之删除。资金费归集与 net_pnl 口径本身仍由第 1~6 节
# 与执行器 §8 的用例锁住。


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

    # ---- L①/L②：失败事件 24h 去重 + 能力缺失只警告一次 ----

    def _funding_sync_errors(self) -> list:
        evs = self.db.hl_recent_events(50, kind="error") or []
        out = []
        for e in evs:
            try:
                d = json.loads(e["data"] or "{}")
            except (TypeError, ValueError):
                d = {}
            if (d or {}).get("src") == "funding_sync":
                out.append(e)
        return out

    def _capture_executor_warnings(self):
        """挂一个临时 handler 收 app.hl.executor 的 warning 文本（返回 (handler, 列表)）。"""
        logs: list = []

        class _Cap(logging.Handler):
            def emit(self, record):
                logs.append(record.getMessage())

        h = _Cap()
        logging.getLogger("app.hl.executor").addHandler(h)
        return h, logs

    def test_funding_fail_error_event_deduped_24h(self):
        """L①：连续两轮失败只记一条 error 事件（data.src=funding_sync 去重），
        但 warning 日志每轮照记 —— 事件是喊人的，复读是刷屏。"""
        info = _HookInfo(flows=[], funding_raise=True)
        ex, _ = self._exec(info)
        h, logs = self._capture_executor_warnings()
        try:
            ex._sync_flows(now=NOW)
            self.db.set_config("hl_flows_synced_at", str(int(NOW - 3600)))  # 重开闸门
            ex._sync_flows(now=NOW + 120)
        finally:
            logging.getLogger("app.hl.executor").removeHandler(h)
        self.assertEqual(len(self._funding_sync_errors()), 1,
                         "24h 内同一失败只许记一条 error 事件")
        warns = [m for m in logs if "拉取资金费失败" in m]
        self.assertEqual(len(warns), 2, "warning 每轮照记（去重只针对事件，不静音日志）")

    def test_info_without_funding_capability_warns_once(self):
        """L②：info 没有 user_funding_history → 首轮 warning 一声，之后保持安静。"""

        class NoFundInfo:
            def ledger_updates(self, address, start_ms=0, deadline_ts=None, **kw):
                return []

        info = NoFundInfo()
        ex, _ = self._exec(info)
        h, logs = self._capture_executor_warnings()
        try:
            ex._sync_flows(now=NOW)
            self.db.set_config("hl_flows_synced_at", str(int(NOW - 3600)))
            ex._sync_flows(now=NOW + 120)
        finally:
            logging.getLogger("app.hl.executor").removeHandler(h)
        warns = [m for m in logs if "userFunding" in m]
        self.assertEqual(len(warns), 1,
                         "能力缺失是配置事实：只警告一次，不每轮刷屏")


# ================================================================ 9. L③：常量同源
class TestConstantsSingleSource(unittest.TestCase):
    """L③：±300s 归集窗口与毫秒阈值只能有一份来源（app/hl/pnl.py）。"""

    def test_funding_constants_are_pnl_bindings(self):
        from app.hl import pnl as hl_pnl
        self.assertIs(hl_funding.ATTR_LOOKBACK_S, hl_pnl.ENTRY_LOOKBACK_S,
                      "funding 的窗口常量必须 import 自 pnl，不许另写一个数")
        self.assertIs(hl_funding.ATTR_LOOKAHEAD_S, hl_pnl.EXIT_LOOKAHEAD_S)

    def test_view_sql_uses_same_constants(self):
        """v_ledger 的毫秒归一阈值与 ±300s 窗口数值与 pnl 同源（f-string 注入）。"""
        from app.hl import pnl as hl_pnl
        tmp = tempfile.mkdtemp(prefix="funds_const_")
        try:
            db = DB(os.path.join(tmp, "t.db"))
            sql = db.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='v_ledger'").fetchone()[0]
            self.assertIn(str(int(hl_pnl.TS_MS_THRESHOLD)), sql,
                          "视图毫秒归一阈值必须来自 pnl.TS_MS_THRESHOLD")
            self.assertIn(str(int(hl_pnl.ENTRY_LOOKBACK_S)), sql)
            self.assertIn(str(int(hl_pnl.EXIT_LOOKAHEAD_S)), sql)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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
