"""策略与仓位计算单测（stdlib unittest，CI 直接跑）。

夹具构造思路：band = (6均线极差)/收盘，只有**均线发散度变化**才会改变 band，
单纯大幅震荡（对称）会让均线都收敛到均值、band 反而很小。所以：
- 「先趋势后横盘」→ 末端均线收敛 → 构成密集
- 「先横盘后趋势」→ 末端均线发散 → 不构成密集
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import strategy as S                                     # noqa: E402
from app.sizing import size_order, liq_price, max_safe_leverage    # noqa: E402

STEP = 14_400_000
T0 = 1_700_000_000_000


def _bar(i, px, spread=0.001):
    return S.Bar(open_ms=T0 + i * STEP, open=px, high=px * (1 + spread),
                 low=px * (1 - spread), close=px, volume=1.0)


def trend_then_flat(n_trend=200, n_flat=200, p0=100.0, p1=130.0):
    """前段线性上涨（均线发散），后段横盘（均线收敛）→ 末端构成均线密集。

    横盘段要足够长（≥ SMA120 + EMA 收敛时间），否则 SMA120 仍在消化趋势段。
    """
    bars = []
    for i in range(n_trend):
        bars.append(_bar(i, p0 + (p1 - p0) * i / max(n_trend - 1, 1), spread=0.001))
    for j in range(n_flat):
        bars.append(_bar(n_trend + j, p1, spread=0.001))
    return bars


def flat_then_trend(n_flat=200, n_trend=200, p0=100.0, p1=130.0):
    """前段横盘、后段上涨 → 末端均线发散，不构成密集。"""
    bars = []
    for i in range(n_flat):
        bars.append(_bar(i, p0, spread=0.001))
    for j in range(n_trend):
        bars.append(_bar(n_flat + j, p0 + (p1 - p0) * j / max(n_trend - 1, 1), spread=0.001))
    return bars


def breakout_bar(bars, jump_pct):
    last = bars[-1]
    px = last.close * (1 + jump_pct)
    return S.Bar(open_ms=last.open_ms + STEP, open=last.close, high=px, low=last.close,
                 close=px, volume=1.0)


def flat_bar(prev, px):
    return S.Bar(open_ms=prev.open_ms + STEP, open=px, high=px, low=px, close=px, volume=1.0)


def prep(bars):
    """返回 (indicators, squeeze_flags)。"""
    ind = S.compute_indicators(bars)
    return ind, S.squeeze_flags(ind["band"])


class TestIndicators(unittest.TestCase):
    def test_sma(self):
        out = S.sma_series([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[4], 4.0)

    def test_ema_seeded_with_first_value(self):
        out = S.ema_series([10, 10, 10], 2)
        self.assertAlmostEqual(out[0], 10.0)
        self.assertAlmostEqual(out[2], 10.0)

    def test_ema_matches_recurrence(self):
        vals = [1.0, 2.0, 3.0, 4.0]
        k = 2 / 3
        expect = [1.0, 1.0 * (1 - k) + 2 * k]
        expect.append(expect[1] * (1 - k) + 3 * k)
        for a, b in zip(expect, S.ema_series(vals, 2)[:3]):
            self.assertAlmostEqual(a, b, places=12)

    def test_quantile_linear_interpolation(self):
        self.assertAlmostEqual(S.quantile([1, 2, 3, 4], 0.0), 1.0)
        self.assertAlmostEqual(S.quantile([1, 2, 3, 4], 1.0), 4.0)
        self.assertAlmostEqual(S.quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertAlmostEqual(S.quantile([1, 2, 3, 4], 0.2), 1.6)

    def test_band_uses_six_mas(self):
        bars = trend_then_flat()
        ind = S.compute_indicators(bars)
        i = len(bars) - 1
        vals = [ind[k][i] for k in S.MA_KEYS]
        self.assertAlmostEqual(ind["ma_max"][i], max(vals))
        self.assertAlmostEqual(ind["ma_min"][i], min(vals))
        self.assertAlmostEqual(ind["band"][i], (max(vals) - min(vals)) / bars[i].close)

    def test_band_shrinks_after_flat_period(self):
        bars = trend_then_flat(n_trend=200, n_flat=200)
        ind = S.compute_indicators(bars)
        self.assertGreater(ind["band"][199], 0.03, "趋势段末端 band 应很宽")
        self.assertLess(ind["band"][-1], 0.01, "长横盘后 band 应收窄到 1% 以内")

    def test_min_bars_required_covers_window_plus_warmup(self):
        self.assertGreaterEqual(S.MIN_BARS_REQUIRED, S.BAND_WINDOW + 120)


class TestSignals(unittest.TestCase):
    def test_squeeze_true_after_convergence(self):
        ind = S.compute_indicators(trend_then_flat())
        sq = S.squeeze_flags(ind["band"])
        self.assertTrue(all(sq[-10:]), "趋势后横盘，末端应判定为均线密集")

    def test_squeeze_false_when_mas_diverging(self):
        ind = S.compute_indicators(flat_then_trend())
        sq = S.squeeze_flags(ind["band"])
        self.assertFalse(any(sq[-10:]), "均线持续发散时不应判定为密集")

    def test_breakout_produces_signal(self):
        bars = trend_then_flat() + [breakout_bar(trend_then_flat(), 0.04)]
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        i = len(bars) - 1                                   # 突破 bar（最后一根）
        sig = S.evaluate_entry("BTC_USDT", bars, ind, sq, i, entry_price=bars[i].close * 1.01)
        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig.stop_price, ind["ma_min"][i])
        self.assertTrue(0.015 <= sig.stop_dist <= 0.10, f"止损距离 {sig.stop_dist:.3f} 应在区间内")

    def test_too_wide_stop_is_filtered(self):
        base = trend_then_flat()
        bars = base + [breakout_bar(base, 0.40)]
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        i = len(bars) - 1
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                           entry_price=bars[i].close * 1.01))

    def test_too_tight_stop_is_filtered(self):
        base = trend_then_flat()
        bars = base + [breakout_bar(base, 0.001)]           # 只跳 0.1% → 止损距离过窄
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        i = len(bars) - 1
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                           entry_price=bars[i].close * 1.001))

    def test_no_signal_without_squeeze(self):
        base = flat_then_trend()
        bars = base + [breakout_bar(base, 0.04)]
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        i = len(bars) - 1
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                           entry_price=bars[i].close * 1.01))

    def test_stop_above_entry_rejected(self):
        base = trend_then_flat()
        bars = base + [breakout_bar(base, 0.04)]
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        i = len(bars) - 1
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i, entry_price=1.0))

    def test_exit_on_close_below_sma20(self):
        bars = trend_then_flat()
        ind = S.compute_indicators(bars)
        self.assertFalse(S.exit_triggered(bars, ind, len(bars) - 1), "横盘时收盘等于 SMA20，不算离场")
        last = bars[-1]
        drop = S.Bar(open_ms=last.open_ms + STEP, open=130.0, high=130.0, low=120.0, close=120.0)
        bars2 = bars + [drop]
        ind2 = S.compute_indicators(bars2)
        self.assertTrue(S.exit_triggered(bars2, ind2, len(bars2) - 1))

def squeeze_then_breakdown(n_trend=200, n_flat=200, p1=130.0, drop_pct=0.045):
    """趋势后横盘（密集）→ 一根向下跌破 band 下沿的 bar → 次根（入场参考）。

    返回 (bars, 跌破 bar 下标)。末端收盘 124.15 同时低于 SMA200(≈130)，
    因此做空的趋势过滤会放行。
    """
    base = trend_then_flat(n_trend, n_flat, 100.0, p1)
    last = base[-1]
    px = p1 * (1 - drop_pct)
    brk = S.Bar(open_ms=last.open_ms + STEP, open=p1, high=p1, low=px, close=px)
    nxt = S.Bar(open_ms=brk.open_ms + STEP, open=px, high=px, low=px, close=px)
    return base + [brk, nxt], len(base)


class TestShorts(unittest.TestCase):
    def test_short_signal_fires_below_band(self):
        bars, i = squeeze_then_breakdown()
        ind, sq = prep(bars)
        sig = S.evaluate_entry("BTC_USDT", bars, ind, sq, i, entry_price=bars[i + 1].open,
                               allow_long=False, allow_short=True)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, -1)
        self.assertEqual(sig.action, "做空")
        self.assertEqual(sig.open_button, "开空")
        self.assertEqual(sig.close_side, "买入平仓")
        self.assertGreater(sig.stop_price, sig.entry_price, "做空止损必须在入场价上方")
        self.assertAlmostEqual(sig.stop_price, ind["ma_max"][i])
        self.assertTrue(0.015 <= sig.stop_dist <= 0.10)

    def test_long_not_triggered_on_breakdown(self):
        bars, i = squeeze_then_breakdown()
        ind, sq = prep(bars)
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                           entry_price=bars[i + 1].open, allow_short=False))

    def test_regime_filter_blocks_short_above_ma200(self):
        bars, i = squeeze_then_breakdown()
        ind, sq = prep(bars)
        ind["sma200"] = [0.0] * len(bars)          # 价格远高于 MA200 → 应拦下
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                           entry_price=bars[i + 1].open, allow_long=False))
        ind["sma200"] = [1e9] * len(bars)          # 价格远低于 MA200 → 放行
        self.assertIsNotNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                              entry_price=bars[i + 1].open, allow_long=False))

    def test_regime_filter_can_be_disabled(self):
        bars, i = squeeze_then_breakdown()
        ind, sq = prep(bars)
        ind["sma200"] = [0.0] * len(bars)
        self.assertIsNotNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                              entry_price=bars[i + 1].open,
                                              allow_long=False, short_regime_ma=0))

    def test_custom_regime_window_computed_on_the_fly(self):
        """回归：short_regime_ma 设成非 200 窗口时，过滤线必须现算而不是静默吞掉做空。

        旧实现 ind.get(f"sma{window}") 拿不到 → 全部做空信号无声消失。
        """
        bars, i = squeeze_then_breakdown()
        ind, sq = prep(bars)
        del ind["sma200"]                          # 模拟 compute_indicators 没算过该窗口
        # sma100 = 横盘段价格均值附近，明显低于跌破后的价格 → 过滤应放行
        sig = S.evaluate_entry("BTC_USDT", bars, ind, sq, i, entry_price=bars[i + 1].open,
                               allow_long=False, short_regime_ma=100)
        self.assertIsNotNone(sig, "自定义过滤窗口查不到时必须现算，不能吞掉做空信号")
        assert sig is not None
        self.assertEqual(sig.side, -1)
        self.assertIn("sma100", ind)               # 且缓存进 ind，同一 dict 内不重算
        # 同一窗口下，把过滤线设成 0（价格必在均线上方）→ 应拦下（证明 sma100 真的参与了判定）
        ind["sma100"] = [0.0] * len(bars)
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                           entry_price=bars[i + 1].open,
                                           allow_long=False, short_regime_ma=100))

    def test_short_stop_distance_filter(self):
        bars, i = squeeze_then_breakdown(drop_pct=0.40)     # 跌太狠 → 距离超 10%
        ind, sq = prep(bars)
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                           entry_price=bars[i + 1].open, allow_long=False))

    def test_exit_triggered_inverted_for_short(self):
        bars = trend_then_flat()
        ind = S.compute_indicators(bars)
        last = bars[-1]
        self.assertFalse(S.exit_triggered(bars, ind, len(bars) - 1, side=-1),
                         "横盘时收盘等于 SMA20，做空也不应离场")
        up = S.Bar(open_ms=last.open_ms + STEP, open=140.0, high=140.0, low=140.0, close=140.0)
        bars2 = bars + [up]
        ind2 = S.compute_indicators(bars2)
        self.assertTrue(S.exit_triggered(bars2, ind2, len(bars2) - 1, side=-1),
                        "做空：收盘站上 SMA20 应离场")
        self.assertFalse(S.exit_triggered(bars2, ind2, len(bars2) - 1, side=1))

    def test_scan_can_return_both_sides(self):
        bars, _ = squeeze_then_breakdown()
        shorts = S.scan(bars, "BTC_USDT", sides=(-1,))
        longs = S.scan(bars, "BTC_USDT", sides=(1,))
        self.assertTrue(all(s.side == -1 for s in shorts))
        self.assertTrue(all(s.side == 1 for s in longs))


class TestSizing(unittest.TestCase):
    def test_short_sizing_mirrors_long(self):
        entry, stop = 100_000.0, 103_000.0            # 做空：止损在上方 3%
        o = size_order("BTC_USDT", entry, stop, 1000.0, 0.02, 5.0, side=-1)
        self.assertAlmostEqual(o.notional, 20 / 0.03, delta=10.0)   # 1 张=10U，取整误差 ≤10U
        self.assertAlmostEqual(o.risk_u, 20.0, delta=0.7)
        self.assertGreater(o.max_safe_leverage, 5.0)

    def test_short_requires_stop_above(self):
        with self.assertRaises(ValueError):
            size_order("BTC_USDT", 100.0, 99.0, 1000.0, 0.02, 5.0, side=-1)


    def test_mexc_btc_contract_conversion(self):
        entry = 84_000.0
        stop = entry * 0.975                                 # 止损 2.5%
        o = size_order("BTC_USDT", entry, stop, 1000.0, 0.02, 5.0)
        self.assertAlmostEqual(o.notional, 800.0, delta=8.4)  # 20 / 0.025 = 800
        self.assertEqual(o.contracts, int(800 / entry / 0.0001))
        self.assertLess(abs(o.risk_u - 20.0), 0.6)
        self.assertAlmostEqual(o.margin, o.notional / 5.0)

    def test_mexc_eth_contract_conversion(self):
        entry = 2_500.0
        o = size_order("ETH_USDT", entry, entry * 0.95, 1000.0, 0.02, 5.0)
        self.assertAlmostEqual(o.notional, 400.0, delta=25.0)
        self.assertEqual(o.contracts, int(400 / entry / 0.01))
        self.assertLess(abs(o.risk_u - 20.0), 1.0)

    def test_below_minimum_falls_back_to_one_contract(self):
        entry = 84_000.0
        o = size_order("BTC_USDT", entry, entry * 0.95, 100.0, 0.0005, 5.0)   # 风险 0.05U
        self.assertEqual(o.contracts, 1)
        self.assertTrue(o.below_minimum)

    def test_liquidation_far_beyond_widest_stop_at_5x(self):
        liq_drop = 1 - liq_price(100_000.0, 5.0) / 100_000.0
        self.assertAlmostEqual(liq_drop, 0.195, places=3)
        self.assertGreater(liq_drop, 0.10, "5x 强平距离(19.5%)应大于最宽止损(10%)")
        self.assertGreaterEqual(max_safe_leverage(0.10), 5.0)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            size_order("BTC_USDT", 100.0, 101.0, 1000.0, 0.02)
        with self.assertRaises(ValueError):
            size_order("BTC_USDT", 100.0, 99.0, 0.0, 0.02)


if __name__ == "__main__":
    unittest.main(verbosity=2)
