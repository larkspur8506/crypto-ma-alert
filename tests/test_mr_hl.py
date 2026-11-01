"""`app/hl/mr.py`（HL 侧 MR 腿）的契约测试 —— 全离线（假 HL + 临时 SQLite）。

钉住的东西（与 lighter-ma-bot 的 tests/test_mr.py 同一套场景）：
  * 默认停用（configuration `mr_enabled` 无值）→ 任何信号都不下单；
  * 入场：信号 + 窗口 + 权益 → 恰好一笔 IOC 限价单 + 台账 OPEN 行；
  * 幂等：同信号重复 tick 不重复下单；
  * 离场：收盘 > SMA5 → reduce_only 平仓 + 台账 CLOSED（funding/费用/净盈亏入账）；
  * 对账 fail-closed：交易所持仓 ↔ 本地台账任一方向不一致 → 不动作 + recon_err；
  * kill（全局）挡入场不挡离场；szDecimals 未知 fail-closed 拒单；
  * sizing：名义 = 权益×0.5、HL 价格/数量取整、权益不足 → SKIPPED 台账。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from app.db import DB
from app.hl.mr import HlMrConfig, HlMrLeg, _floor_sz, _round_px


def _hourly(date: str, close: float, *, hours=(14, 15, 16, 17, 18, 19)) -> list:
    base = int(datetime.strptime(date, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows = []
    for i, h in enumerate(hours):
        t = base + h * 3600_000
        o = close + 0.1 * (len(hours) - i)
        rows.append({"t": t, "o": o, "h": max(o, close), "l": min(o, close),
                     "c": close})
    return rows


CANDLES = []
for _date, _c in [("2026-04-06", 100.0), ("2026-04-07", 99.0), ("2026-04-08", 98.0),
                  ("2026-04-09", 97.0), ("2026-04-10", 96.0), ("2026-04-13", 95.0),
                  ("2026-04-14", 94.0), ("2026-04-15", 93.0), ("2026-04-16", 93.5),
                  ("2026-04-17", 95.5)]:
    CANDLES.extend(_hourly(_date, _c))

ENTRY_NOW_MS = int(datetime.fromisoformat("2026-04-16T13:40:00+00:00").timestamp() * 1000)
EXIT_NOW_MS = int(datetime.fromisoformat("2026-04-17T20:10:00+00:00").timestamp() * 1000)
ADDR = "0x" + "11" * 20


class FakeInfo:
    def __init__(self, *, equity=500.0, mr_szi="0", mid=93.0):
        self.equity = equity
        self.mr_szi = mr_szi
        self.mid = mid
        self.now_ms = EXIT_NOW_MS

    def spot_state(self, address: str) -> dict:
        return {"marginSummary": {"accountValue": str(self.equity)}}

    def user_state_dex(self, address: str, dex: str) -> dict:
        if self.mr_szi == "0":
            return {"marginSummary": {"marginUsed": "0"}, "assetPositions": []}
        return {"marginSummary": {"marginUsed": "250"},
                "assetPositions": [{"position": {
                    "coin": "xyz:XYZ100", "szi": self.mr_szi, "entryPx": "93.0",
                    "positionValue": str(abs(float(self.mr_szi)) * self.mid),
                    "unrealizedPnl": "3.0",
                    "leverage": {"marginUsed": "250"}}}]}

    def perp_meta_dex(self, dex: str) -> list:
        return [{"name": "xyz:XYZ100", "szDecimals": 2, "maxLeverage": "30"}]

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list:
        return [r for r in CANDLES if start_ms <= r["t"] <= end_ms]

    def all_mids(self) -> dict:
        return {"XYZ100": self.mid}

    def user_funding_history(self, address: str, start_ms: int, end_ms: int) -> list:
        return [{"delta": {"type": "funding", "coin": "xyz:XYZ100",
                           "usdc": str(getattr(self, "funding_usdc", "-0.03"))}}]


class FakeExchange:
    def __init__(self):
        self.orders = []

    def place_order(self, coin, is_buy, sz, limit_px, **kw):
        self.orders.append(dict(coin=coin, is_buy=is_buy, sz=sz, limit_px=limit_px, **kw))
        return {"ok": True, "status": "filled", "oid": 100 + len(self.orders),
                "avg_px": limit_px, "filled_sz": sz, "error": None, "raw": {}}


def _leg(tmpdir: str, **kw):
    db = DB(f"{tmpdir}/mr_{abs(hash(tmpdir)) % 99999}.db")
    info = FakeInfo(**kw)
    ex = FakeExchange()
    leg = HlMrLeg(db=db, info=info, exchange=ex, address_fn=lambda: ADDR,
                  clock=lambda: EXIT_NOW_MS / 1000.0)
    return leg, db, ex, info


def _enable(db: DB):
    db.set_config("mr_enabled", "1")


class TestRounding(unittest.TestCase):
    def test_round_px_five_sig_figs(self):
        self.assertEqual(_round_px(93.45678, 2), 93.457)
        self.assertEqual(_round_px(1234.5678, 2), 1234.6)   # ≤5 位有效数字

    def test_floor_sz(self):
        self.assertEqual(_floor_sz(2.68817, 2), 2.68)
        self.assertEqual(_floor_sz(2.6, 2), 2.6)


class TestMrDisabled(unittest.TestCase):
    def test_no_config_means_disabled_no_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, _ = _leg(tmp)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertFalse(status["enabled"])
            self.assertEqual(status["state"], "disabled")
            self.assertEqual(ex.orders, [])


class TestMrEntry(unittest.TestCase):
    def test_entry_places_single_ioc_order_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(status["state"], "entered")
            self.assertEqual(len(ex.orders), 1)
            order = ex.orders[0]
            self.assertTrue(order["is_buy"])
            self.assertFalse(order["reduce_only"])
            self.assertEqual(order["coin"], "xyz:XYZ100")
            # 名义 = 500×0.5 = 250；mark = 93.0；sz = floor(250/93, 2) = 2.68
            self.assertEqual(order["sz"], 2.68)
            self.assertTrue(str(order["cloid"]).startswith("0x"))
            row = db.mr_open_trade()
            self.assertIsNotNone(row)
            self.assertEqual(row["signal_date"], "2026-04-15")
            self.assertEqual(row["entry_session"], "2026-04-16")
            self.assertAlmostEqual(row["notional"], 2.68 * 93.0, places=6)

    def test_second_tick_same_signal_does_not_reorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            leg.tick(now_ms=ENTRY_NOW_MS)
            leg.tick(now_ms=ENTRY_NOW_MS + 60_000)
            self.assertEqual(len(ex.orders), 1)

    def test_insufficient_equity_records_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp, equity=16.0)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(ex.orders, [])
            rows = db.mr_recent_trades(5)
            self.assertEqual(rows[0]["state"], "SKIPPED")

    def test_global_kill_blocks_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            db.hl_set_kill(True, "测试")
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(ex.orders, [])
            self.assertTrue(status["kill"])

    def test_unknown_sz_decimals_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp)
            info.now_ms = ENTRY_NOW_MS
            info.perp_meta_dex = lambda dex: [{"name": "OTHER", "szDecimals": 2}]
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(ex.orders, [])
            self.assertEqual(status["state"], "error")


class TestMrExit(unittest.TestCase):
    def test_exit_closes_ledger_with_funding_fee_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp, mr_szi="2.68", mid=95.5)
            _enable(db)
            db.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
                qty_int=26800, entry_price=93.0, entry_order_id="100",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=2.68 * 93.0, margin=249.24,
                equity_at_entry=500.0)
            status = leg.tick(now_ms=EXIT_NOW_MS)
            self.assertEqual(len(ex.orders), 1)
            order = ex.orders[0]
            self.assertFalse(order["is_buy"])
            self.assertTrue(order["reduce_only"])
            self.assertEqual(order["sz"], 2.68)
            row = db.mr_recent_trades(1)[0]
            self.assertEqual(row["state"], "CLOSED")
            self.assertEqual(row["exit_signal_date"], "2026-04-17")
            # funding = 0.03/249.24×100 ≈ 0.0120（贴近 0.0004%/h × 30.5h 基线）
            self.assertAlmostEqual(row["funding_pct"], 0.012037, places=4)
            # net = (95.5 − 93)×2.68 − 0.03 − 0.0009×(249.24 + 2.68×95.5)
            fee = 0.0009 * (249.24 + 2.68 * 95.5)
            self.assertAlmostEqual(row["net_pnl"], 6.7 - 0.03 - fee, places=3)
            self.assertEqual(status["state"], "exited")


class TestMrTriggers(unittest.TestCase):
    """P3 对抗审核新增的即时止损触发器：滑点 >20bp、funding 超基线 2 倍 → 自动停用。"""

    def test_entry_slippage_over_threshold_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp)
            info.now_ms = ENTRY_NOW_MS
            real_place = ex.place_order

            def place(coin, is_buy, sz, limit_px, **kw):
                res = real_place(coin, is_buy, sz, limit_px, **kw)
                res["avg_px"] = limit_px * 1.005          # 50bp 滑点 > 20bp 阈值
                return res

            ex.place_order = place
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(status["state"], "entered")   # 单照下、台账照记
            self.assertEqual(db.get_config_str("mr_enabled", "1"), "0")

    def test_entry_slippage_within_threshold_keeps_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            leg.tick(now_ms=ENTRY_NOW_MS)                  # 默认 avg=limit → 15bp
            self.assertEqual(db.get_config_str("mr_enabled", "1"), "1")

    def test_funding_over_twice_baseline_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp, mr_szi="2.68", mid=95.5)
            info.funding_usdc = "-0.25"                    # 0.1003% >> 2×0.0122%
            _enable(db)
            db.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
                qty_int=26800, entry_price=93.0, entry_order_id="100",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=2.68 * 93.0, margin=249.24,
                equity_at_entry=500.0)
            status = leg.tick(now_ms=EXIT_NOW_MS)
            self.assertEqual(status["state"], "exited")    # 离场本身完成
            self.assertEqual(db.get_config_str("mr_enabled", "1"), "0")

    def test_funding_near_baseline_keeps_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp, mr_szi="2.68", mid=95.5)
            _enable(db)
            db.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
                qty_int=26800, entry_price=93.0, entry_order_id="100",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=2.68 * 93.0, margin=249.24,
                equity_at_entry=500.0)
            leg.tick(now_ms=EXIT_NOW_MS)
            self.assertEqual(db.get_config_str("mr_enabled", "1"), "1")


class TestMrRecon(unittest.TestCase):
    def test_exchange_position_without_ledger_is_recon_err(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp, mr_szi="1.0")
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(status["recon"], "err")
            self.assertEqual(status["state"], "recon_err")
            self.assertEqual(ex.orders, [])

    def test_ledger_open_without_exchange_position_recon_err(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            db.mr_record_entry(
                signal_date="2026-04-01", entry_session="2026-04-02", qty=1.0,
                qty_int=10000, entry_price=90.0, entry_order_id="x",
                entry_ts=0.0, notional=90.0, margin=90.0, equity_at_entry=500.0)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(status["recon"], "err")
            self.assertEqual(ex.orders, [])

    def test_fresh_entry_tolerated_before_position_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            db.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=1.0,
                qty_int=10000, entry_price=93.0, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0 - 60, notional=93.0,
                margin=93.0, equity_at_entry=500.0)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(status["recon"], "ok")


class TestMrKillButExitWorks(unittest.TestCase):
    def test_kill_blocks_entry_but_exit_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, db, ex, info = _leg(tmp, mr_szi="2.68", mid=95.5)
            _enable(db)
            db.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
                qty_int=26800, entry_price=93.0, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=249.24, margin=249.24,
                equity_at_entry=500.0)
            db.hl_set_kill(True, "测试")
            status = leg.tick(now_ms=EXIT_NOW_MS)
            self.assertEqual(len(ex.orders), 1)      # 离场不受 kill 影响
            self.assertEqual(status["state"], "exited")


if __name__ == "__main__":
    unittest.main()
