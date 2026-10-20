"""PR-1 费用记账的单元测试：时间窗取数与费用币种归一。

覆盖两处会**静默算错净利**的地方：
1. ``_fees_since`` 以前用「最近 500 条」滚动窗口 —— 多标的并行 / 持仓跨日时，
   目标币的成交会被别的币挤出去，手续费少算 → 净利虚高。必须按时间窗取。
2. 交易所 fee 的币种不一定是计价币（USDC/USDT/USD）。把 0.2 ARB 当 0.2 U 加进
   净利是数量级错误，所以库里存折算结果 ``fee_quote``，折算不了就留 NULL 并记事件。

全部离线：临时 sqlite + 假 info/交易所（复用 tests/test_hl_executor 的假件）。
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.db import DB
from app.hl import EV_ORDER, MODE_LIVE
from app.hl.executor import Executor, ExecParams
from tests.test_hl_executor import FakeExchange, FakeInfo, FakeNotifier


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "t.db")
        self.db = DB(self.path)
        self.notifier = FakeNotifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="testrev")

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self) -> Executor:
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def add_fill(self, tid, coin="BTC", fee=0.01, fee_token="USDC", ts=None, **kw):
        """直接写一行成交（绕过交易所，用于构造历史/边界数据）。

        fee_quote 不显式给 → 落 NULL，正好模拟「PR-1 之前的历史行」。
        """
        self.db.hl_upsert_fill(
            tid=tid, symbol=f"{coin}_USDT", coin=coin, side=1, sz=0.001, px=100.0,
            fee=fee, fee_token=fee_token, dir="Open Long", closed_pnl=0.0,
            hash=tid, reduce_only=False,
            ts=int(time.time()) if ts is None else ts, **kw)

    def events(self, kind=EV_ORDER):
        return [dict(r) for r in self.db.hl_recent_events(200, kind=kind)]

    def unquoted_events(self):
        return [e for e in self.events() if "未能折算" in str(e["message"])]


class TestFeesSinceWindow(Base):
    """_fees_since 必须按时间窗取，不能被别的币的成交挤出窗口。"""

    def test_fees_counted_even_when_many_unrelated_fills_follow(self):
        ex = self.executor()
        now = time.time()
        # 目标币的两笔成交（入场 + 出场）先入库
        self.add_fill("btc-in", coin="BTC", fee=0.30, ts=int(now))
        self.add_fill("btc-out", coin="BTC", fee=0.20, ts=int(now))
        # 之后别的币刷了 600 笔 —— 旧实现取最近 500 条，BTC 会被完全挤出去
        # （批量插入：本用例只关心读侧窗口语义，不测写路径，逐条 commit 太慢）
        self.db.conn.executemany(
            "INSERT INTO hl_fills(tid, symbol, coin, side, sz, px, fee, fee_token, dir,"
            " closed_pnl, hash, reduce_only, ts, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"eth-{i}", "ETH_USDT", "ETH", 1, 0.001, 100.0, 0.01, "USDC", "Open Long",
              0.0, None, 0, int(now), now) for i in range(600)])
        self.db.conn.commit()

        fees = ex._fees_since("BTC", now)
        self.assertAlmostEqual(fees, 0.50)

    def test_since_lower_bound_is_opened_at_minus_120s(self):
        """入场成交发生在 opened_at 写库之前，−120s 的下界必须保留（否则入场费漏算）。"""
        ex = self.executor()
        now = int(time.time())
        self.add_fill("entry", coin="BTC", fee=0.40, ts=now - 100)    # opened_at 前 100s：要算
        self.add_fill("old", coin="BTC", fee=9.00, ts=now - 600)      # 上一轮持仓：不能算
        self.assertAlmostEqual(ex._fees_since("BTC", now), 0.40)

    def test_other_coins_never_counted(self):
        ex = self.executor()
        now = int(time.time())
        self.add_fill("btc", coin="BTC", fee=0.25, ts=now)
        self.add_fill("eth", coin="ETH", fee=7.00, ts=now)
        self.assertAlmostEqual(ex._fees_since("BTC", now), 0.25)

    def test_never_raises_on_garbage_since(self):
        ex = self.executor()
        self.add_fill("btc", coin="BTC", fee=0.25)
        # 非法时间戳：不抛异常，返回 0（不能凭猜的去累加）
        self.assertEqual(ex._fees_since("BTC", "not-a-timestamp"), 0.0)
        # since_ts=None 是「没给下界」，沿用老语义：since 归 0 → 全部计入
        self.assertAlmostEqual(ex._fees_since("BTC", None), 0.25)


class TestFeeQuoteOnSync(Base):
    """_sync_fills 写入时归一币种：计价币 → fee_quote=fee；其余 → NULL + 一条事件。"""

    def _fill(self, tid, fee="0.00526", fee_token="USDC", coin="BTC", time_ms=1791955000000):
        return {"tid": tid, "coin": coin, "dir": "Open Long", "sz": "0.00014",
                "px": "83500.0", "fee": fee, "feeToken": fee_token,
                "closedPnl": "0.0", "hash": f"0x{tid}", "time": time_ms}

    def test_quote_token_sets_fee_quote_equal_to_fee(self):
        self.info._fills = [self._fill(21)]
        self.executor()._sync_fills()
        row = self.db.hl_recent_fills(10)[0]
        self.assertEqual(row["fee_token"], "USDC")
        self.assertAlmostEqual(row["fee_quote"], 0.00526)
        self.assertEqual(self.unquoted_events(), [])

    def test_non_quote_token_leaves_fee_quote_null_and_logs_event(self):
        self.info._fills = [self._fill(22, fee="0.2", fee_token="ARB")]
        self.executor()._sync_fills()
        row = self.db.hl_recent_fills(10)[0]
        self.assertEqual(row["fee_token"], "ARB")
        self.assertAlmostEqual(row["fee"], 0.2)        # 原值保留，方便人工折算
        self.assertIsNone(row["fee_quote"])
        evs = self.unquoted_events()
        self.assertEqual(len(evs), 1)
        self.assertIn("ARB", evs[0]["message"])
        self.assertIn("22", evs[0]["message"])         # 带 tid，便于对账

    def test_unquoted_event_not_repeated_on_resync(self):
        """user_fills 每轮都会返回同一批成交：事件必须一仓一条，不能刷屏。"""
        self.info._fills = [self._fill(23, fee="0.2", fee_token="ARB")]
        self.executor()._sync_fills()
        self.executor()._sync_fills()
        self.executor()._sync_fills()
        self.assertEqual(len(self.unquoted_events()), 1)

    def test_unknown_token_also_treated_as_unquoted(self):
        self.info._fills = [self._fill(24, fee="0.1", fee_token=None)]
        self.executor()._sync_fills()
        self.assertIsNone(self.db.hl_recent_fills(10)[0]["fee_quote"])
        self.assertEqual(len(self.unquoted_events()), 1)

    def test_synced_fills_feed_net_fee(self):
        """端到端：同步进来的计价币成交要能被 _fees_since 全额计入。"""
        now = int(time.time())
        self.info._fills = [self._fill(25, fee="0.11", time_ms=(now - 30) * 1000),
                            self._fill(26, fee="0.09", time_ms=now * 1000)]
        ex = self.executor()
        ex._sync_fills()
        self.assertAlmostEqual(ex._fees_since("BTC", now), 0.20)


class TestFeesSinceQuoteFallback(Base):
    """历史行（fee_quote 为 NULL）回退用 fee；两列都缺的行跳过。"""

    def test_historical_row_without_fee_quote_falls_back_to_fee(self):
        ex = self.executor()
        now = int(time.time())
        self.add_fill("legacy", coin="BTC", fee=0.33, ts=now)      # fee_quote 不传 → NULL
        self.assertIsNone(self.db.hl_recent_fills(10)[0]["fee_quote"])
        self.assertAlmostEqual(ex._fees_since("BTC", now), 0.33)

    def test_quote_preferred_over_raw_fee(self):
        ex = self.executor()
        now = int(time.time())
        self.add_fill("mixed", coin="BTC", fee=5.0, fee_quote=0.42, ts=now)
        self.assertAlmostEqual(ex._fees_since("BTC", now), 0.42)

    def test_row_with_both_missing_is_skipped(self):
        ex = self.executor()
        now = int(time.time())
        self.add_fill("empty", coin="BTC", fee=None, fee_quote=None, ts=now)
        self.add_fill("ok", coin="BTC", fee=0.05, ts=now)
        self.assertAlmostEqual(ex._fees_since("BTC", now), 0.05)


class TestMigrateAddsFeeQuote(Base):
    """老库（hl_fills 没有 fee_quote 列）打开时要能自动补列，历史数据不丢。"""

    OLD_SCHEMA = """
    CREATE TABLE hl_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tid TEXT UNIQUE,
        symbol TEXT, coin TEXT,
        side INTEGER, sz REAL, px REAL,
        fee REAL, fee_token TEXT, dir TEXT, closed_pnl REAL, hash TEXT,
        reduce_only INTEGER DEFAULT 0,
        ts INTEGER, created_at REAL NOT NULL
    );
    """

    def test_migrate_adds_column_and_keeps_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "old.db")
            raw = sqlite3.connect(path)
            raw.executescript(self.OLD_SCHEMA)
            raw.execute("INSERT INTO hl_fills(tid, symbol, coin, side, sz, px, fee, fee_token,"
                        " dir, closed_pnl, hash, reduce_only, ts, created_at)"
                        " VALUES('old1','BTC_USDT','BTC',1,0.001,100.0,0.12,'USDC','Open Long',"
                        " 0,NULL,0,?,?)", (int(time.time()), time.time()))
            raw.commit()
            raw.close()

            db = DB(path)                                     # 打开即迁移
            cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(hl_fills)")}
            self.assertIn("fee_quote", cols)
            row = db.hl_recent_fills(10)[0]
            self.assertEqual(row["tid"], "old1")
            self.assertAlmostEqual(row["fee"], 0.12)
            self.assertIsNone(row["fee_quote"])

    def test_migrate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "new.db")
            DB(path).conn.close()
            DB(path).conn.close()                             # 再开两次不该报错（列已存在）
            cols = [r["name"] for r in DB(path).conn.execute("PRAGMA table_info(hl_fills)")]
            self.assertEqual(cols.count("fee_quote"), 1)


if __name__ == "__main__":
    unittest.main()
