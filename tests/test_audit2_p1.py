"""第二轮审计 P1 回归测试。

覆盖：
1. H1/H2：平仓/重挂止损的 cloid 与开仓止损不同源；同一仓位两次 on_exit 的
   cloid 不同（重试不被拒）；重挂失败 → 兜底市价平仓（不留裸仓）。
2. H3：pending 未过期时占槽 —— 连续第二个突破 bar 不再建第二个 pending。
3. feed_health：成功路径写 last_ok_at；心跳判定「最近失败晚于最近成功」为失败。
4. data_error：NULL 幂等键带 2h 时间窗 —— 窗口外重复故障会再报。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB
from app.hl import (PURPOSE_ENTRY, PURPOSE_EXIT, PURPOSE_EXIT_REHANG, PURPOSE_STOP)
from app.hl.exchange import make_cloid


def _tmp_db():
    import tempfile
    return DB(os.path.join(tempfile.mkdtemp(), "t.db"))


class TestCloidSeparation(unittest.TestCase):
    """H1/H2：cloid 必须互不相同。"""

    def test_rehang_cloid_differs_from_entry_stop(self):
        bar = 1791966119102
        self.assertNotEqual(
            make_cloid("BTC_USDT", bar, PURPOSE_EXIT_REHANG),
            make_cloid("BTC_USDT", bar, PURPOSE_STOP),
            "重挂止损不得复用开仓止损的 cloid（HL 拒重复 cloid → 裸仓）")

    def test_exit_cloid_varies_by_bar(self):
        b1, b2 = 1791966119102, 1791966119102 + 4 * 3600 * 1000
        self.assertNotEqual(
            make_cloid("BTC_USDT", b1, PURPOSE_EXIT),
            make_cloid("BTC_USDT", b2, PURPOSE_EXIT),
            "不同离场 bar 的平仓 cloid 必须不同（首次失败后重试要能发出）")

    def test_exit_cloid_differs_from_all_others(self):
        bar = 1791966119102
        c = make_cloid("BTC_USDT", bar, PURPOSE_EXIT)
        for p in (PURPOSE_ENTRY, PURPOSE_STOP, PURPOSE_EXIT_REHANG, PURPOSE_STOP + ":fix"):
            self.assertNotEqual(c, make_cloid("BTC_USDT", bar, p))


class TestOnExitRehangPath(unittest.TestCase):
    """on_exit resting 分支：重挂成功 → 保护仓位；重挂失败 → 兜底平仓。"""

    def _mk_executor(self, stop_result, close_result):
        from app.hl.executor import Executor, ExecParams
        db = _tmp_db()
        pid = db.create_position(
            symbol="BTC_USDT", signal_bar_ms=1700000000000, side=1,
            entry_price=100.0, stop_price=97.0, stop_dist=0.03,
            contracts=10, coin_qty=0.01, notional=1000.0, risk_u=2.0,
            equity_at_entry=100.0)
        db.confirm_position(pid, 100.0, 0.01, 97.0, True)
        db.set_pos_hl(pid, hl_status="open", hl_sz=0.01)

        class FakeInfo:
            def specs(self, coins):
                return {c: {"sz_decimals": 5} for c in coins}

        class FakeEx:
            def __init__(self):
                self.orders = []
                self.cancels = []
                self.stop_result = None      # type: ignore[assignment]
                self.close_result = None     # type: ignore[assignment]

            def _cloid(self, symbol, bar_ms, purpose):
                from app.hl.exchange import _coerce_cloid
                return _coerce_cloid(make_cloid(symbol, bar_ms, purpose))

            def mid(self, coin):
                return 99.5

            def slippage_limit(self, coin, is_buy, mid=None):
                return (mid or 99.5) * (0.995 if is_buy else 1.005)

            def place_order(self, coin, is_buy, sz, px, reduce_only=False,
                            trigger_px=None, cloid=None):
                cloid_s = str(cloid) if cloid is not None else None
                if trigger_px is not None and self.stop_result is not None:
                    r = dict(self.stop_result)
                    r["cloid"] = cloid_s
                    self.orders.append(("stop-ish", cloid_s, trigger_px))
                    return r
                # 平仓 IOC
                r = dict(self.close_result)
                r["cloid"] = cloid_s
                self.orders.append(("close", cloid_s, px))
                return r

            def cancel(self, coin, oid):
                self.cancels.append(oid)

        fake_ex = FakeEx()
        fake_ex.stop_result = stop_result
        fake_ex.close_result = {"ok": True, "status": "resting", "oid": 555,
                                "filled_sz": 0.0, "avg_px": None, "error": None}

        # 预置交易所侧持仓（快照）
        snap = {"positions": [{"coin": "BTC", "size": 0.01, "entry_px": 100.0}],
                "orders_trusted": True, "open_orders": [], "equity_total": 100.0}

        ex = Executor(db=db,
                      params=ExecParams(mode="live", account_address="0xabc",
                                        private_key="0x" + "11" * 32),
                      exchange=fake_ex, info=FakeInfo(),
                      notifier=mock.Mock())
        # 打桩：mid/snapshot 固定
        ex._snapshot = lambda force=False: dict(snap)
        return db, pid, ex, fake_ex

    def test_rehang_uses_new_cloid_and_succeeds(self):
        db, pid, ex, fake_ex = self._mk_executor(
            stop_result={"ok": True, "status": "resting", "oid": 777,
                         "filled_sz": 0.0, "avg_px": None, "error": None},
            close_result=None)
        handled = ex.on_exit(pid, "ma20", 99.5)
        self.assertTrue(handled)
        # 重挂的止损单确实发出，且 cloid 带 rehang purpose
        rehang = [o for o in fake_ex.orders if o[0] == "stop-ish"]
        self.assertEqual(len(rehang), 1)
        # 平仓 resting 被撤
        self.assertIn(555, fake_ex.cancels)
        # 仓位行未被关账（保护仍在，等下一轮）
        row = db.position_by_id(pid)
        self.assertEqual(row["status"], "open")

    def test_rehang_fail_closes_position(self):
        db, pid, ex, fake_ex = self._mk_executor(
            stop_result={"ok": False, "status": "error", "oid": None,
                         "filled_sz": 0.0, "avg_px": None, "error": "duplicate cloid"},
            close_result=None)
        # 平仓 resting 后，兜底平仓（第二次 close 调用）返回 filled
        calls = {"n": 0}

        orig_place = fake_ex.place_order

        def place(coin, is_buy, sz, px, reduce_only=False, trigger_px=None, cloid=None):
            if trigger_px is None:
                calls["n"] += 1
                if calls["n"] == 1:      # 首次平仓 IOC → resting
                    return {"ok": True, "status": "resting", "oid": 555,
                            "filled_sz": 0.0, "avg_px": None, "error": None}
                return {"ok": True, "status": "filled", "oid": 556,
                        "filled_sz": sz, "avg_px": px, "error": None}   # 兜底平仓成交
            return orig_place(coin, is_buy, sz, px, reduce_only, trigger_px, cloid)

        fake_ex.place_order = place
        ex._sync_fills = lambda: None
        handled = ex.on_exit(pid, "ma20", 99.5)
        self.assertTrue(handled)
        # 兜底平仓确实发生
        self.assertGreaterEqual(calls["n"], 2)
        # 本地已关账
        row = db.position_by_id(pid)
        self.assertEqual(row["status"], "closed")
        self.assertIsNotNone(row["exit_price"])


class TestPendingSlot(unittest.TestCase):
    """H3：pending 占槽，第二个突破 bar 不再建新 pending。"""

    def test_second_breakout_no_new_pending(self):
        from app import engine, market, strategy
        from app.config import Config
        from tests.test_strategy import trend_then_flat, breakout_bar, flat_bar, STEP

        class FakeNotifier:
            def __init__(self): self.msgs = []
            def send_message(self, m): self.msgs.append(m); return True

        base = trend_then_flat()
        bo = breakout_bar(base, 0.04)
        f1 = flat_bar(bo, bo.close)
        bars1 = base + [bo, f1]
        f2 = flat_bar(f1, f1.close)
        bars2 = base + [bo, f1, f2]
        X = base[-1].open_ms + STEP
        X1 = X + STEP

        FEEDS = {"n": 0}

        def fake_fetch(symbol, now_ms=None, allow_fallback=False):
            bars = bars1 if FEEDS["n"] == 0 else bars2
            return market.Feed(symbol=symbol, source="hl-candle", bars=bars,
                               fetched_at_ms=now_ms or int(time.time() * 1000))

        db = _tmp_db()
        cfg = Config(); cfg.dry_run = False; cfg.alert_prefix = ""
        cfg.symbols = ["BTC_USDT"]
        orig = engine.market.fetch
        engine.market.fetch = fake_fetch
        try:
            engine.tick(db, cfg, FakeNotifier(), now_ms=X + 4 * 3600 * 1000 + 60_000)
            p1 = db.recent_positions(10)
            self.assertEqual(len([r for r in p1 if r["status"] == "pending"]), 1)

            FEEDS["n"] = 1
            engine.tick(db, cfg, FakeNotifier(), now_ms=X1 + 4 * 3600 * 1000 + 60_000)
            all_rows = db.recent_positions(10)
            pend = [r for r in all_rows if r["status"] == "pending"]
            self.assertEqual(len(pend), 1, "pending 期间不得再建第二个 pending（占槽）")
            # 审计三轮 M12：pending 期间**不推进** bar 进度 —— 以前会推进到 bars2
            # 的最新收盘根，晚确认的仓就跳过中间根的止损判定；现在停在第一次
            # tick 的进度（bars1 最新收盘根），确认后从入场根起重扫。
            self.assertEqual(db.get_last_bar("BTC_USDT"), bars1[len(bars1) - 2].open_ms)
        finally:
            engine.market.fetch = orig


class TestFeedHealth(unittest.TestCase):
    """P1 附带：成功路径写 feed_health + 心跳判定。"""

    def test_mark_feed_ok_then_error_heartbeat(self):
        from app import engine
        db = _tmp_db()
        db.mark_feed("BTC_USDT", "hl-candle", True)
        rows = db.feed_status()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "hl-candle")
        self.assertIsNotNone(rows[0]["last_ok_at"])

        # 出错后再看心跳：error_at 更新 → 判失败
        time.sleep(0.01)
        db.mark_feed("BTC_USDT", "", False, "boom")
        from app.config import Config
        cfg = Config(); cfg.alert_prefix = ""; cfg.symbols = ["BTC_USDT"]
        text = engine.heartbeat_text(db, cfg)
        self.assertIn("❌ 失败", text)
        self.assertIn("boom", text)

        # 恢复成功 → 又判 ok
        time.sleep(0.01)
        db.mark_feed("BTC_USDT", "hl-candle", True)
        text = engine.heartbeat_text(db, cfg)
        self.assertNotIn("❌", text)


class TestDataErrorDedupWindow(unittest.TestCase):
    """data_error 的 NULL 幂等键：2h 窗口外会再报。"""

    def test_was_alerted_null_window(self):
        db = _tmp_db()
        # 一条很久以前的成功 data_error
        db.conn.execute(
            "INSERT INTO alert_logs(symbol, bar_ms, kind, message, sent_successfully, created_at)"
            " VALUES('BTC_USDT', NULL, 'data_error', 'old', 1, ?)", (time.time() - 86400,))
        db.conn.commit()
        # 窗口内无记录 → 不算重复
        self.assertFalse(db.was_alerted("BTC_USDT", None, "data_error", within_s=7200.0))
        # 无窗口（旧行为）→ 任意历史都算重复
        self.assertTrue(db.was_alerted("BTC_USDT", None, "data_error"))

        # 刚发过一条 → 窗口内算重复
        db.log_alert("BTC_USDT", None, "data_error", "new", True)
        self.assertTrue(db.was_alerted("BTC_USDT", None, "data_error", within_s=7200.0))

    def test_emit_data_error_repeats_after_window(self):
        from app import engine, market
        from app.config import Config

        class FakeNotifier:
            def __init__(self): self.msgs = []
            def send_message(self, m): self.msgs.append(m); return True

        db = _tmp_db()
        cfg = Config(); cfg.dry_run = False; cfg.alert_prefix = ""
        cfg.symbols = ["BTC_USDT"]

        def boom(symbol, now_ms=None, allow_fallback=False):
            raise market.FeedError("模拟主源失败")

        orig = engine.market.fetch
        engine.market.fetch = boom
        try:
            n1 = FakeNotifier()
            engine.tick(db, cfg, n1, now_ms=int(time.time() * 1000))
            self.assertEqual(len(n1.msgs), 1)

            n2 = FakeNotifier()
            engine.tick(db, cfg, n2, now_ms=int(time.time() * 1000))
            self.assertEqual(len(n2.msgs), 0, "2h 窗口内去重")

            # 把成功记录改成 3 小时前 → 窗口外，应再报
            db.conn.execute(
                "UPDATE alert_logs SET created_at = ? WHERE kind='data_error'",
                (time.time() - 3 * 3600,))
            db.conn.commit()
            n3 = FakeNotifier()
            engine.tick(db, cfg, n3, now_ms=int(time.time() * 1000))
            self.assertEqual(len(n3.msgs), 1, "窗口过后故障仍在 → 再报")
        finally:
            engine.market.fetch = orig


if __name__ == "__main__":
    unittest.main()
