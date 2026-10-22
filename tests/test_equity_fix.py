"""`app/hl/equity_fix.py` 的单元测试：统一账户权益双算的一次性修正。"""
from __future__ import annotations

import sqlite3
import unittest

from app.hl.equity_fix import fix_double_count, pending_rows

DDL = """
CREATE TABLE hl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    equity REAL NOT NULL,
    unrealized_pnl REAL,
    equity_fixed INTEGER NOT NULL DEFAULT 0
)
"""


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(DDL)
    for ts, eq, upnl in rows:
        conn.execute("INSERT INTO hl_snapshots(ts, equity, unrealized_pnl) VALUES(?,?,?)",
                     (ts, eq, upnl))
    conn.commit()
    return conn


class TestEquityFix(unittest.TestCase):
    def test_dry_run_does_not_write(self):
        conn = _db([(1.0, 975.25, -12.86), (2.0, 987.31, 0.0)])
        self.assertEqual(pending_rows(conn), 1)
        st = fix_double_count(conn, apply=False)
        self.assertEqual(st["rows"], 1)
        self.assertFalse(st["applied"])
        eq = conn.execute("SELECT equity FROM hl_snapshots ORDER BY ts").fetchall()
        self.assertAlmostEqual(eq[0]["equity"], 975.25)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM hl_snapshots WHERE equity_fixed=0").fetchone()[0], 2)

    def test_apply_adds_back_the_double_counted_loss(self):
        """浮亏被算两次 → 修正后权益 = 现货口径（975.25 + 12.86 = 988.11）。"""
        conn = _db([(1.0, 975.25, -12.86), (2.0, 987.31, 0.0)])
        st = fix_double_count(conn, apply=True)
        self.assertTrue(st["applied"])
        rows = conn.execute("SELECT ts, equity, equity_fixed FROM hl_snapshots ORDER BY ts").fetchall()
        self.assertAlmostEqual(rows[0]["equity"], 988.11, places=6)
        self.assertEqual(rows[0]["equity_fixed"], 1)
        self.assertAlmostEqual(rows[1]["equity"], 987.31, places=6, msg="upnl=0 的行不动")
        self.assertEqual(rows[1]["equity_fixed"], 0)

    def test_second_run_is_noop(self):
        conn = _db([(1.0, 975.25, -12.86)])
        fix_double_count(conn, apply=True)
        self.assertEqual(pending_rows(conn), 0)
        st = fix_double_count(conn, apply=True)
        self.assertEqual(st["rows"], 0)
        self.assertAlmostEqual(
            conn.execute("SELECT equity FROM hl_snapshots").fetchone()[0], 988.11, places=6)

    def test_profit_rows_are_trimmed_down(self):
        """浮盈同理：不能让权益虚高。"""
        conn = _db([(1.0, 1010.0, 8.0)])
        fix_double_count(conn, apply=True)
        self.assertAlmostEqual(
            conn.execute("SELECT equity FROM hl_snapshots").fetchone()[0], 1002.0, places=6)

    def test_null_unrealized_pnl_is_ignored(self):
        conn = _db([(1.0, 100.0, None)])
        self.assertEqual(pending_rows(conn), 0)
        self.assertEqual(fix_double_count(conn)["rows"], 0)


if __name__ == "__main__":
    unittest.main()
