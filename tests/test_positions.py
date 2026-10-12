"""做空执行链路回归测试。

这两个 bug 上线时漏掉了，都是做空专属：
  1. db.close_position 用了只做多的 R 公式 → 做空的 risk 为负 → r 恒为 0.0
     → 周报的均R/总R/胜率全部错（盈利的空单不算赢）
  2. 确认流不校验止损方向 → 做空可以填一个低于入场价的止损，静默污染统计
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine                                          # noqa: E402
from app.db import DB                                           # noqa: E402


def make_db() -> DB:
    tmp = tempfile.mkdtemp()
    return DB(os.path.join(tmp, "t.db"))


def new_position(db: DB, side: int, entry: float, stop: float) -> int:
    return db.create_position(symbol="BTC_USDT", signal_bar_ms=1_700_000_000_000,
                              side=side, entry_price=entry, stop_price=stop,
                              stop_dist=abs(entry - stop) / entry, contracts=10,
                              coin_qty=0.001, notional=entry * 0.001, risk_u=20.0,
                              equity_at_entry=1000.0)


class TestShortRMultiple(unittest.TestCase):
    """Bug 1 回归：做空的 R 必须按方向算。"""

    def test_short_win_is_positive_r(self):
        db = make_db()
        pid = new_position(db, side=-1, entry=100_000.0, stop=103_000.0)
        db.confirm_position(pid, 100_000.0, 10, 103_000.0, True)
        db.close_position(pid, 94_000.0, "ma20")            # 跌 6% = 2R 盈利
        row = db.position_by_id(pid)
        self.assertAlmostEqual(row["r_multiple"], 2.0, places=6,
                               msg="做空盈利必须记正 R（旧代码会记 0.0）")
        self.assertGreater(row["r_multiple"], 0)

    def test_short_loss_is_minus_one_r(self):
        db = make_db()
        pid = new_position(db, side=-1, entry=100_000.0, stop=103_000.0)
        db.confirm_position(pid, 100_000.0, 10, 103_000.0, True)
        db.close_position(pid, 103_000.0, "stop")
        self.assertAlmostEqual(db.position_by_id(pid)["r_multiple"], -1.0, places=6)

    def test_long_unchanged(self):
        db = make_db()
        pid = new_position(db, side=1, entry=100_000.0, stop=97_000.0)
        db.confirm_position(pid, 100_000.0, 10, 97_000.0, True)
        db.close_position(pid, 106_000.0, "ma20")
        self.assertAlmostEqual(db.position_by_id(pid)["r_multiple"], 2.0, places=6)

    def test_long_loss_is_minus_one_r(self):
        db = make_db()
        pid = new_position(db, side=1, entry=100_000.0, stop=97_000.0)
        db.confirm_position(pid, 100_000.0, 10, 97_000.0, True)
        db.close_position(pid, 97_000.0, "stop")
        self.assertAlmostEqual(db.position_by_id(pid)["r_multiple"], -1.0, places=6)

    def test_uses_actual_prices_when_confirmed(self):
        """用户填的实际成交价/实际止损价优先于建议值。"""
        db = make_db()
        pid = new_position(db, side=-1, entry=100_000.0, stop=103_000.0)
        db.confirm_position(pid, 99_000.0, 10, 101_000.0, True)   # 实际风险 = 2%
        db.close_position(pid, 95_000.0, "ma20")                  # 跌 4% = 2R
        self.assertAlmostEqual(db.position_by_id(pid)["r_multiple"], 2.0, places=6)

    def test_weekly_report_counts_short_wins(self):
        """周报的胜率必须把盈利的空单算进去。"""
        db = make_db()
        pid = new_position(db, side=-1, entry=100_000.0, stop=103_000.0)
        db.confirm_position(pid, 100_000.0, 10, 103_000.0, True)
        db.close_position(pid, 94_000.0, "ma20")
        rows = db.closed_positions(0.0)
        rs = [r["r_multiple"] or 0.0 for r in rows]
        self.assertEqual(len(rs), 1)
        self.assertEqual(sum(1 for x in rs if x > 0), 1, "盈利空单必须计入胜率")
        self.assertAlmostEqual(sum(rs), 2.0, places=6)


class TestStopSideValidation(unittest.TestCase):
    """Bug 2 回归：确认流必须挡住方向填反的止损。"""

    def test_long_stop_above_entry_rejected(self):
        self.assertIsNotNone(engine.validate_stop_side(1, 100.0, 101.0))
        self.assertIsNotNone(engine.validate_stop_side(1, 100.0, 100.0))

    def test_short_stop_below_entry_rejected(self):
        self.assertIsNotNone(engine.validate_stop_side(-1, 100.0, 99.0))
        self.assertIsNotNone(engine.validate_stop_side(-1, 100.0, 100.0))

    def test_valid_sides_pass(self):
        self.assertIsNone(engine.validate_stop_side(1, 100.0, 97.0))
        self.assertIsNone(engine.validate_stop_side(-1, 100.0, 103.0))

    def test_error_message_mentions_side(self):
        self.assertIn("做空", engine.validate_stop_side(-1, 100.0, 99.0))
        self.assertIn("做多", engine.validate_stop_side(1, 100.0, 101.0))


class TestShortStateMachine(unittest.TestCase):
    """做空从信号 → 落库 → 走 bar → 离场的完整链路。"""

    def _bars_and_signal(self):
        from tests.test_strategy import squeeze_then_breakdown, prep
        from app import strategy as S
        bars, i = squeeze_then_breakdown()
        ind, sq = prep(bars)
        return bars, ind, sq, i, S

    def test_short_row_roundtrip_keeps_side(self):
        db = make_db()
        pid = new_position(db, side=-1, entry=100_000.0, stop=103_000.0)
        self.assertEqual(db.position_by_id(pid)["side"], -1)
        st = engine._row_to_state(db.position_by_id(pid))
        self.assertEqual(st.side, -1)
        self.assertEqual(st.action_label, "空单")
        self.assertEqual(st.close_side, "买入平仓")

    def test_short_stop_hit_on_high(self):
        """做空的止损在价格**上方**，靠 bar.high 触发。"""
        from app.config import Config
        bars, ind, sq, i, S = self._bars_and_signal()
        cfg = Config()
        cfg.equity, cfg.risk_pct, cfg.leverage = 1000.0, 0.02, 5.0
        cfg.enable_long, cfg.enable_shorts, cfg.short_regime_ma = False, True, 200
        pos = None
        events, pos = engine.walk(cfg, "BTC_USDT", bars, ind, sq, pos, i, i, i)
        self.assertTrue(any(e.kind == "entry" for e in events), "做空信号应产生 entry 事件")
        entry_ev = [e for e in events if e.kind == "entry"][0]
        self.assertEqual(entry_ev.sig.side, -1)
        self.assertGreater(entry_ev.sig.stop_price, entry_ev.sig.entry_price)

        # 造一根冲上止损的 bar → 必须触发 stop 离场
        # 注意 walk 只处理「已收盘」的 bar（列表最后一根是进行中的，不处理），
        # 所以要在 spike 之后再补一根，让 spike 落在 len-2 的位置。
        last = bars[-1]
        spike = S.Bar(open_ms=last.open_ms + 14_400_000, open=last.close,
                      high=entry_ev.sig.stop_price * 1.02, low=last.close,
                      close=entry_ev.sig.stop_price * 1.01)
        tail = S.Bar(open_ms=spike.open_ms + 14_400_000, open=spike.close,
                     high=spike.close, low=spike.close, close=spike.close)
        bars2 = bars + [spike, tail]
        ind2 = S.compute_indicators(bars2)
        sq2 = S.squeeze_flags(ind2["band"])
        i2 = len(bars2) - 2
        ev2, _ = engine.walk(cfg, "BTC_USDT", bars2, ind2, sq2, pos, i2, i2, i2)
        ex = [e for e in ev2 if e.kind == "exit"]
        self.assertTrue(ex, "做空必须能被上方止损打掉")
        self.assertEqual(ex[0].reason, "stop")
        self.assertAlmostEqual(ex[0].exit_price, entry_ev.sig.stop_price)


class TestConfigOverrides(unittest.TestCase):
    """做空开关必须能从 /admin/rules 热改（DB 覆盖 env，不重启）。"""

    def setUp(self):
        from app.config import Config
        self.cfg = Config()

    def test_direction_flags_overridable(self):
        from app.config import Config
        for k in ("enable_long", "enable_shorts", "short_regime_ma"):
            self.assertIn(k, Config.OVERRIDABLE, f"{k} 不在 DB 可覆盖列表里")

    def test_disable_shorts_from_db(self):
        self.cfg.apply_db({"enable_shorts": 0})
        self.assertFalse(self.cfg.enable_shorts)
        self.cfg.apply_db({"enable_shorts": 1})
        self.assertTrue(self.cfg.enable_shorts)

    def test_disable_long_from_db(self):
        self.cfg.apply_db({"enable_long": 0})
        self.assertFalse(self.cfg.enable_long)
        self.cfg.apply_db({"enable_long": 1})
        self.assertTrue(self.cfg.enable_long)

    def test_short_regime_ma_zero_disables_filter(self):
        self.cfg.apply_db({"short_regime_ma": 0})
        self.assertEqual(self.cfg.short_regime_ma, 0)
        self.cfg.apply_db({"short_regime_ma": 100})
        self.assertEqual(self.cfg.short_regime_ma, 100)

    def test_garbage_ignored(self):
        before = self.cfg.enable_shorts
        self.cfg.apply_db({"enable_shorts": "abc"})
        self.assertEqual(self.cfg.enable_shorts, before)

    def test_disabled_short_side_suppresses_signal(self):
        """关掉做空后，跌破信号不应再产生做空票。"""
        from tests.test_strategy import squeeze_then_breakdown, prep
        from app import strategy as S
        bars, i = squeeze_then_breakdown()
        ind, sq = prep(bars)
        self.cfg.apply_db({"enable_shorts": 0})
        sig = S.evaluate_entry("BTC_USDT", bars, ind, sq, i, entry_price=bars[i + 1].open,
                               allow_long=self.cfg.enable_long,
                               allow_short=self.cfg.enable_shorts,
                               short_regime_ma=self.cfg.short_regime_ma)
        self.assertIsNone(sig, "关掉做空后不应再出做空信号")


if __name__ == "__main__":
    unittest.main(verbosity=2)
