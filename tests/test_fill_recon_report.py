"""scripts/fill_recon_report.py（PR-2 只读 fill 对账报告）的单元测试。

全部用**合成数据**：临时 sqlite 库 + 手写 positions/hl_fills 行，断言 VWAP、净额、
R、ΔR 的计算与 flag 标注。不联网、不碰任何真实库。

fixture 设计（数字都刻意取整数便于心算）：

- id=1 做多 ETH：入场 0.5@100 + 0.5@102（vwap 101），出场 1.0@110，fee 0.5 USDC，
  closed_pnl 9.0 → net_fill 8.5；risk = 1.0×|101-95| = 6 → R_fill 1.4167；
  本地 exit_price=110 → R_local 1.5，ΔR 0.0833（不超阈值）。
- id=2 做空 BTC：入场 0.2@200，出场 0.2@180，closed_pnl 4.0，fee 0.2 **HYPE 计价**
  → 该 fee 不计入 fee_total（UNRESOLVED_FEE），net_fill 4.0；risk 2.0 → R_fill 2.0，
  R_local 2.0，ΔR 0。
- id=3 无任何成交 → NO_FILLS，且缺 actual_entry → NO_ACTUAL_ENTRY。
- id=4 本地关账价记错（105 vs 实际 110）→ R_DIFF + NET_DIFF。
- id=5 窗口外的成交（同 coin 的另一回合）不得被算进来。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import fill_recon_report as R                                  # noqa: E402

T0 = 1_760_000_000.0          # 2025-10-09 15:33:20 UTC，仓 1 的 opened_at
DAY = 86400.0

# 老库最小 schema 的兜底（app.db 不可 import 时用）
MINIMAL_SCHEMA = """
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, signal_bar_ms INTEGER NOT NULL, status TEXT NOT NULL,
    side INTEGER NOT NULL DEFAULT 1, entry_price REAL NOT NULL, stop_price REAL NOT NULL,
    stop_dist REAL NOT NULL, contracts INTEGER NOT NULL, coin_qty REAL NOT NULL,
    notional REAL NOT NULL, risk_u REAL NOT NULL, actual_entry REAL, actual_contracts REAL,
    actual_stop REAL, stop_placed INTEGER DEFAULT 0, exit_price REAL, exit_reason TEXT,
    auto_closed INTEGER DEFAULT 0, r_multiple REAL, equity_at_entry REAL,
    created_at REAL NOT NULL, opened_at REAL, closed_at REAL,
    hl_status TEXT, hl_entry_oid INTEGER, hl_stop_oid INTEGER, hl_sz REAL,
    UNIQUE(symbol, signal_bar_ms)
);
CREATE TABLE hl_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tid TEXT UNIQUE, symbol TEXT, coin TEXT,
    side INTEGER, sz REAL, px REAL, fee REAL, fee_token TEXT, dir TEXT, closed_pnl REAL,
    hash TEXT, reduce_only INTEGER DEFAULT 0, ts INTEGER, created_at REAL NOT NULL
);
"""


def make_db(path: str) -> None:
    """建库：优先复用 app.db 的 DDL（保证列名与生产一致），失败则退回最小 schema。"""
    try:
        from app.db import DB
        DB(path).conn.close()
        return
    except Exception:                                          # noqa: BLE001
        pass
    conn = sqlite3.connect(path)
    conn.executescript(MINIMAL_SCHEMA)
    conn.commit()
    conn.close()


def add_position(conn, **kw) -> int:
    """插一行仓（缺的列给默认值），返回 id。"""
    row = {
        "symbol": "ETH_USDT", "signal_bar_ms": int(T0 * 1000), "status": "closed",
        "side": 1, "entry_price": 101.0, "stop_price": 95.0, "stop_dist": 0.06,
        "contracts": 10, "coin_qty": 1.0, "notional": 101.0, "risk_u": 6.0,
        "actual_entry": 101.0, "actual_contracts": 1.0, "actual_stop": 95.0,
        "exit_price": 110.0, "exit_reason": "ma20", "auto_closed": 0,
        "r_multiple": 1.5, "equity_at_entry": 1000.0,
        "created_at": T0 - 3600, "opened_at": T0, "closed_at": T0 + 7 * DAY,
        "hl_status": "closed", "hl_sz": 1.0,
    }
    row.update(kw)
    cols = ", ".join(row)
    qs = ", ".join("?" * len(row))
    cur = conn.execute(f"INSERT INTO positions({cols}) VALUES({qs})", list(row.values()))
    return int(cur.lastrowid)


def add_fill(conn, tid, coin, sz, px, fee, dir_, closed_pnl, ts, fee_token="USDC",
             symbol=None):
    conn.execute(
        "INSERT INTO hl_fills(tid, symbol, coin, side, sz, px, fee, fee_token, dir,"
        " closed_pnl, reduce_only, ts, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, symbol or f"{coin}_USDT", coin, 1 if "Long" in dir_ else -1, sz, px, fee,
         fee_token, dir_, closed_pnl, 1 if dir_.startswith("Close") else 0,
         int(ts), ts))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recon-")
        self.db_path = os.path.join(self.tmp, "t.db")
        make_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        # ---- id=1 做多 ETH：两笔入场 + 一笔出场
        self.p1 = add_position(
            conn, symbol="ETH_USDT", signal_bar_ms=1000, side=1,
            actual_entry=101.0, actual_contracts=1.0, actual_stop=95.0,
            exit_price=110.0, coin_qty=1.0, risk_u=6.0, hl_status="closed", hl_sz=1.0,
            opened_at=T0, closed_at=T0 + 20 * 3600)
        add_fill(conn, "e1", "ETH", 0.5, 100.0, 0.3, "Open Long", 0.0, T0 - 30)
        add_fill(conn, "e2", "ETH", 0.5, 102.0, 0.2, "Open Long", 0.0, T0 - 10)
        add_fill(conn, "x1", "ETH", 1.0, 110.0, 0.0, "Close Long", 9.0, T0 + 20 * 3600)
        # ---- id=2 做空 BTC：fee 用 HYPE 计价（不可换算）
        self.p2 = add_position(
            conn, symbol="BTC_USDT", signal_bar_ms=2000, side=-1,
            actual_entry=200.0, actual_contracts=0.2, actual_stop=210.0,
            exit_price=180.0, coin_qty=0.2, risk_u=2.0, hl_status="closed", hl_sz=0.2,
            opened_at=T0 + DAY, closed_at=T0 + DAY + 3600)
        add_fill(conn, "e3", "BTC", 0.2, 200.0, 0.1, "Open Short", 0.0, T0 + DAY)
        add_fill(conn, "x2", "BTC", 0.2, 180.0, 0.2, "Close Short", 4.0,
                 T0 + DAY + 3600, fee_token="HYPE")
        # ---- id=3 无成交 + 缺 actual_entry
        self.p3 = add_position(
            conn, symbol="SOL_USDT", signal_bar_ms=3000, side=1, actual_entry=None,
            actual_stop=None, exit_price=150.0, coin_qty=2.0, risk_u=5.0,
            hl_status="closed", hl_sz=None, opened_at=T0 + 2 * DAY,
            closed_at=T0 + 2 * DAY + 3600)
        # ---- id=4 本地关账价记错（105 vs 成交 110）
        self.p4 = add_position(
            conn, symbol="ETH_USDT", signal_bar_ms=4000, side=1,
            actual_entry=101.0, actual_contracts=1.0, actual_stop=95.0,
            exit_price=105.0, coin_qty=1.0, risk_u=6.0, hl_status="closed", hl_sz=1.0,
            opened_at=T0 + 3 * DAY, closed_at=T0 + 3 * DAY + 7200)
        add_fill(conn, "e5", "ETH", 1.0, 101.0, 0.1, "Open Long", 0.0, T0 + 3 * DAY)
        add_fill(conn, "x5", "ETH", 1.0, 110.0, 0.1, "Close Long", 9.0,
                 T0 + 3 * DAY + 7200)
        # ---- id=5 窗口外成交：属另一回合（opened_at 前 1 天），不得计入
        self.p5 = add_position(
            conn, symbol="ETH_USDT", signal_bar_ms=5000, side=1,
            actual_entry=101.0, actual_contracts=1.0, actual_stop=95.0,
            exit_price=110.0, coin_qty=1.0, risk_u=6.0, hl_status="closed", hl_sz=1.0,
            opened_at=T0 + 10 * DAY, closed_at=T0 + 10 * DAY + 3600)
        add_fill(conn, "old1", "ETH", 1.0, 90.0, 9.9, "Close Long", 99.0, T0 - 5 * DAY)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def report(self, **kw):
        conn = R.open_readonly(self.db_path)
        try:
            return R.build_report(conn, kw.pop("since", None), kw.pop("r_tol", 0.15),
                                  kw.pop("net_abs_tol", 1.0), kw.pop("net_rel_tol", 0.0005))
        finally:
            conn.close()

    @staticmethod
    def row(report, pid):
        return next(r for r in report["rows"] if r["id"] == pid)


class TestAggregation(Base):
    def test_entry_vwap_weighted_by_size(self):
        r = self.row(self.report(), self.p1)
        self.assertAlmostEqual(r["entry_sz"], 1.0, places=9)
        self.assertAlmostEqual(r["entry_vwap"], 101.0, places=9)   # (0.5*100+0.5*102)/1
        self.assertAlmostEqual(r["exit_vwap"], 110.0, places=9)
        self.assertEqual(r["n_fills"], 3)

    def test_fee_total_and_net_fill(self):
        r = self.row(self.report(), self.p1)
        self.assertAlmostEqual(r["fee_total"], 0.5, places=9)
        self.assertAlmostEqual(r["closed_pnl_sum"], 9.0, places=9)
        self.assertAlmostEqual(r["net_fill"], 8.5, places=9)
        self.assertNotIn(R.UNRESOLVED_FEE, r["flags"])

    def test_r_fill_uses_net_over_risk(self):
        r = self.row(self.report(), self.p1)
        self.assertAlmostEqual(r["risk_u"], 6.0, places=9)
        self.assertAlmostEqual(r["r_fill"], 8.5 / 6.0, places=9)
        self.assertAlmostEqual(r["r_local"], 1.5, places=9)        # (110-101)/6
        self.assertAlmostEqual(r["d_r"], abs(8.5 / 6.0 - 1.5), places=9)
        self.assertNotIn(R.R_DIFF, r["flags"])                     # 0.0833 < 0.15

    def test_local_net_matches_when_only_fee_differs(self):
        """两侧手续费同源 → Δnet 恒 0；这条是「Δnet 只反映价差/数量」的护栏。"""
        r = self.row(self.report(), self.p1)
        self.assertAlmostEqual(r["net_local"], 9.0 - 0.5, places=9)
        self.assertAlmostEqual(r["d_net"], 0.0, places=9)
        self.assertNotIn(R.NET_DIFF, r["flags"])

    def test_short_side_and_unresolved_fee(self):
        r = self.row(self.report(), self.p2)
        self.assertAlmostEqual(r["closed_pnl_sum"], 4.0, places=9)
        # 开仓腿 0.1 USDC 计入；平仓腿 0.2 HYPE 不计入（无法换算成 U）
        self.assertAlmostEqual(r["fee_total"], 0.1, places=9)
        self.assertEqual(r["unresolved_fee_rows"], 1)
        self.assertIn(R.UNRESOLVED_FEE, r["flags"])
        self.assertAlmostEqual(r["net_fill"], 3.9, places=9)
        self.assertAlmostEqual(r["r_fill"], 1.95, places=9)         # 3.9/2.0
        self.assertAlmostEqual(r["r_local"], 2.0, places=9)         # (200-180)/10

    def test_window_excludes_other_round_trip(self):
        r = self.row(self.report(), self.p5)
        self.assertIn(R.NO_FILLS, r["flags"])                      # 5 天前那笔不算
        self.assertIsNone(r["r_fill"])
        self.assertIsNone(r["net_fill"])

    def test_no_fills_flags(self):
        r = self.row(self.report(), self.p3)
        self.assertIn(R.NO_FILLS, r["flags"])
        self.assertIn(R.NO_ACTUAL_ENTRY, r["flags"])
        self.assertEqual(r["n_fills"], 0)
        self.assertIsNone(r["r_fill"])
        self.assertIsNone(r["r_local"])                            # 缺 actual_entry
        self.assertIsNone(r["d_r"])


class TestDiffFlags(Base):
    def test_wrong_local_exit_price_triggers_r_and_net_diff(self):
        r = self.row(self.report(), self.p4)
        self.assertAlmostEqual(r["r_fill"], (9.0 - 0.2) / 6.0, places=9)
        self.assertAlmostEqual(r["r_local"], (105.0 - 101.0) / 6.0, places=9)
        self.assertGreater(r["d_r"], 0.15)
        self.assertIn(R.R_DIFF, r["flags"])
        # net_local 用错的关账价：价差只有 4U 而成交侧是 9U → 差 5U，超 1U 绝对阈值
        self.assertAlmostEqual(r["d_net"], 5.0, places=6)
        self.assertIn(R.NET_DIFF, r["flags"])

    def test_thresholds_are_configurable(self):
        r = self.row(self.report(r_tol=0.001, net_abs_tol=0.01), self.p1)
        self.assertIn(R.R_DIFF, r["flags"])                        # 0.0833 > 0.001
        self.assertNotIn(R.NET_DIFF, r["flags"])                   # Δnet 仍是 0

    def test_relative_net_tol_requires_both(self):
        """绝对与相对阈值必须**同时**超过才算 NET_DIFF（小仓位不被绝对阈值淹没）。"""
        r = self.row(self.report(net_abs_tol=100.0, net_rel_tol=0.0001), self.p4)
        self.assertNotIn(R.NET_DIFF, r["flags"])


class TestSummary(Base):
    def test_summary_distribution(self):
        s = self.report()["summary"]
        self.assertEqual(s["positions"], 5)
        self.assertEqual(s["positions_with_fills"], 3)             # p1/p2/p4
        self.assertEqual(s["no_fills_count"], 2)                   # p3/p5
        self.assertEqual(s["r_diff_count"], 1)                     # p4
        self.assertEqual(s["net_diff_count"], 1)
        self.assertEqual(s["unresolved_fee_count"], 1)
        self.assertGreaterEqual(s["dr_max"], s["dr_p90"])
        self.assertGreaterEqual(s["dr_p90"] >= 0, True)

    def test_percentile_helper(self):
        self.assertEqual(R._pct([], 0.5), None)
        self.assertAlmostEqual(R._pct([0.0, 1.0], 0.5), 0.5)
        self.assertAlmostEqual(R._pct([0.0, 1.0], 0.9), 0.9)
        self.assertAlmostEqual(R._pct([5.0], 0.99), 5.0)


class TestRobustness(Base):
    def test_old_db_without_hl_columns(self):
        """老库：positions 没有 hl_status/opened_at，hl_fills 整表缺 → 不抛，标 NO_FILLS。"""
        path = os.path.join(self.tmp, "old.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                signal_bar_ms INTEGER NOT NULL, status TEXT NOT NULL, side INTEGER NOT NULL,
                entry_price REAL NOT NULL, stop_price REAL NOT NULL, stop_dist REAL NOT NULL,
                contracts INTEGER NOT NULL, coin_qty REAL NOT NULL, notional REAL NOT NULL,
                risk_u REAL NOT NULL, actual_entry REAL, actual_contracts REAL,
                actual_stop REAL, exit_price REAL, exit_reason TEXT, r_multiple REAL,
                equity_at_entry REAL, created_at REAL NOT NULL, opened_at REAL, closed_at REAL);
        """)
        conn.execute("INSERT INTO positions(symbol, signal_bar_ms, status, side, entry_price,"
                     " stop_price, stop_dist, contracts, coin_qty, notional, risk_u,"
                     " actual_entry, actual_contracts, actual_stop, exit_price, created_at,"
                     " opened_at, closed_at) VALUES('ETH_USDT',1,'closed',1,101,95,0.06,10,"
                     "1.0,101,6,101,1.0,95,110,?,?,?)", (T0 - 60, T0, T0 + 3600))
        conn.commit()
        conn.close()

        conn = R.open_readonly(path)
        try:
            report = R.build_report(conn, None, 0.15, 1.0, 0.0005)
        finally:
            conn.close()
        self.assertEqual(report["summary"]["positions"], 1)
        row = report["rows"][0]
        self.assertIn(R.NO_FILLS, row["flags"])
        self.assertIn(R.NO_HL, row["flags"])                       # 无 hl_status → 非 HL 行
        self.assertIsNone(row["r_fill"])
        self.assertAlmostEqual(row["r_local"], 1.5, places=9)       # 本地侧仍能算

    def test_readonly_connection_rejects_writes(self):
        conn = R.open_readonly(self.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM positions")
        finally:
            conn.close()

    def test_missing_file_is_reported_not_raised(self):
        with self.assertRaises(FileNotFoundError):
            R.open_readonly(os.path.join(self.tmp, "nope.db"))

    def test_bad_since_format(self):
        with self.assertRaises(SystemExit):
            R._parse_since("2026/10/01")


class TestCli(Base):
    def test_main_exit_zero_and_db_untouched(self):
        before = os.stat(self.db_path)
        rc = R.main(["--db", self.db_path])
        self.assertEqual(rc, 0)
        after = os.stat(self.db_path)
        self.assertEqual(before.st_mtime, after.st_mtime)          # 一个字节都没写
        self.assertEqual(before.st_size, after.st_size)

    def test_main_json_output(self):
        out = os.path.join(self.tmp, "out.json")
        rc = R.main(["--db", self.db_path, "--json", out, "--since", "2025-10-01"])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["summary"]["positions"], 5)
        self.assertEqual(len(payload["rows"]), 5)
        self.assertIn("flags", payload["rows"][0])

    def test_since_filters_by_closed_at(self):
        since = (T0 + 2 * DAY) / 1.0                              # 只留 closed_at >= 2 天后
        r = self.report(since=since)
        # p1/p2 在 2 天前关 → 被过滤；p3/p4/p5 留下
        ids = {row["id"] for row in r["rows"]}
        self.assertEqual(ids, {self.p3, self.p4, self.p5})

    def test_since_does_not_drop_entry_fills_of_long_held_position(self):
        """--since 是**关账日**过滤，但入场成交可能远早于它（跨月持仓）→ 不许误报 NO_FILLS。"""
        path = os.path.join(self.tmp, "long.db")
        make_db(path)
        conn = sqlite3.connect(path)
        opened = T0 - 120 * DAY                                    # 5 个月前开仓
        closed = T0 + 1 * DAY                                      # 昨天关
        pid = add_position(conn, symbol="ETH_USDT", signal_bar_ms=9000, side=1,
                           actual_entry=101.0, actual_contracts=1.0, actual_stop=95.0,
                           exit_price=110.0, coin_qty=1.0, risk_u=6.0,
                           hl_status="closed", hl_sz=1.0,
                           opened_at=opened, closed_at=closed)
        add_fill(conn, "L1", "ETH", 1.0, 101.0, 0.1, "Open Long", 0.0, opened - 10)
        add_fill(conn, "L2", "ETH", 1.0, 110.0, 0.1, "Close Long", 9.0, closed)
        conn.commit()
        conn.close()

        since = T0                                             # 只看到「昨天」为止的关账
        conn = R.open_readonly(path)
        try:
            report = R.build_report(conn, since, 0.15, 1.0, 0.0005)
        finally:
            conn.close()
        row = next(r for r in report["rows"] if r["id"] == pid)
        self.assertEqual(row["n_fills"], 2)
        self.assertNotIn(R.NO_FILLS, row["flags"])
        self.assertAlmostEqual(row["entry_vwap"], 101.0, places=9)
        self.assertAlmostEqual(row["net_fill"], 8.8, places=9)

    def test_main_missing_db_returns_zero(self):
        rc = R.main(["--db", os.path.join(self.tmp, "ghost.db")])
        self.assertEqual(rc, 0)                                    # 报告工具：恒 0

    def test_text_report_contains_flags_and_summary(self):
        text = R.format_text(self.report(), self.db_path, None)
        self.assertIn("ETH_USDT", text)
        self.assertIn("NET_DIFF", text)
        self.assertIn("ΔR 分布", text)
        self.assertIn("汇总", text)


if __name__ == "__main__":
    unittest.main()
