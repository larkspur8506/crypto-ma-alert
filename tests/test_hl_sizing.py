"""Hyperliquid 仓位换算与下单参数取整单测（stdlib unittest，CI 直接跑）。

覆盖点：floor 取整绝不越界、价格 5 位有效数字 + 小数位上限、>100k 取整、
整数价格恒合法、最小名义 10 USDC、强平价晚于止损价、方向非法 raise、warnings 文案。

夹具口径与 `app/strategy.py` / `app/sizing.py` 一致：
stop_dist 建议区间 [1.5%, 10%]，逐仓 5x 强平距离 19.5%。
"""
import os
import sys
import unittest
from dataclasses import FrozenInstanceError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.hl import sizing as S                                             # noqa: E402
from app.hl.sizing import (                                                # noqa: E402
    HlOrderPlan, DEFAULT_MIN_NOTIONAL, STOP_MIN, STOP_MAX,
    liq_price, meets_min_notional, max_safe_leverage,
    round_price, round_size, size_order_hl, validate_stop_side,
)

# BTC 永续常用参数（szDecimals=5）
ENTRY, STOP_LONG, STOP_SHORT = 84_000.0, 81_960.0, 86_140.0
EQUITY, RISK_PCT, LEV, SZ_DEC = 10_000.0, 0.01, 5.0, 5


# ================================================================ 数量取整
class TestRoundSize(unittest.TestCase):
    def test_floor_truncates_tail(self):
        # 规格里点名的用例：floor 到步长 10^-5
        self.assertEqual(S.round_size(0.000129, 5), 0.00012)

    def test_floor_is_not_round(self):
        # 同样的输入，round 会进位——floor 绝不允许
        self.assertEqual(round_size(0.000129, 5), 0.00012)
        self.assertEqual(round(0.000129, 5), 0.00013)          # 对照组：round 行为不同
        self.assertEqual(round_size(1.999, 2), 1.99)
        self.assertEqual(round(1.999, 2), 2.0)

    def test_exact_step_multiple_not_under_floored(self):
        # 浮点会把 0.00013/1e-5 算成 12.999999999999998；不能因此少下 1 个步长
        self.assertEqual(round_size(0.00013, 5), 0.00013)
        self.assertEqual(round_size(1.0, 0), 1.0)
        self.assertEqual(round_size(123.456, 3), 123.456)

    def test_result_is_exact_step_multiple(self):
        for sz, d in [(0.000129, 5), (0.04902, 5), (1.999, 2), (0.12345, 2),
                      (123.456789, 3), (99.9999, 1), (2.999, 0), (1e-8, 4)]:
            v = round_size(sz, d)
            self.assertAlmostEqual(v * 10 ** d, round(v * 10 ** d), places=9,
                                   msg=f"{sz}@{d} 不是步长整数倍")

    def test_never_exceeds_input(self):
        for sz, d in [(0.000129, 5), (0.04902, 5), (1.999, 2), (123.456789, 3),
                      (99.9999, 1), (0.5, 5), (84000.123, 4)]:
            v = round_size(sz, d)
            self.assertLessEqual(v, sz, msg=f"floor 越界: {sz}@{d} -> {v}")

    def test_zero_and_sub_step(self):
        self.assertEqual(round_size(0.0, 5), 0.0)
        self.assertEqual(round_size(1e-8, 4), 0.0)      # 小于 1 个步长 → 0
        self.assertEqual(round_size(1e-5, 3), 0.0)

    def test_zero_decimals_floors_to_int(self):
        self.assertEqual(round_size(2.999, 0), 2.0)
        self.assertEqual(round_size(10.0, 0), 10.0)

    def test_negative_size_raises(self):
        with self.assertRaises(ValueError):
            round_size(-0.0001, 5)

    def test_negative_decimals_raises(self):
        with self.assertRaises(ValueError):
            round_size(1.0, -1)


# ================================================================ 价格取整
class TestRoundPrice(unittest.TestCase):
    def test_five_significant_figures(self):
        self.assertEqual(S.round_price(1.2345678, 4), 1.23)   # max_dp = 6-4 = 2
        self.assertEqual(S.round_price(12345.6789, 0), 12346.0)

    def test_sz_decimals_caps_decimal_places(self):
        # sz_decimals=5 → 永续最多 1 位小数，5 位有效数字让位
        self.assertEqual(S.round_price(1234.56, 5), 1234.6)
        self.assertEqual(S.round_price(0.0012345, 0), 0.001234)
        self.assertEqual(S.round_price(0.0012345, 1), 0.00123)

    def test_price_over_100k_rounds_to_integer(self):
        self.assertEqual(S.round_price(100_001.7, 5), 100_002.0)
        self.assertEqual(S.round_price(1_000_000.0, 0), 1_000_000.0)
        self.assertEqual(S.round_price(12_345_678.9, 5), 12_345_679.0)

    def test_just_under_100k_still_uses_5_sig_figs(self):
        # 99999.9 走常规分支：5 位有效数字 → 1e5
        self.assertEqual(S.round_price(99_999.9, 5), 100_000.0)

    def test_integer_prices_always_allowed(self):
        # 官方规则：整数价格恒合法，不受 5 位有效数字约束
        self.assertEqual(S.round_price(84_000.0, 5), 84_000.0)
        self.assertEqual(S.round_price(123_456.0, 5), 123_456.0)   # >100k 且 6 位
        self.assertEqual(S.round_price(99_999.0, 0), 99_999.0)
        self.assertEqual(S.round_price(1.0, 5), 1.0)

    def test_decimal_places_respected(self):
        for px, d in [(0.0012345, 0), (0.0012345, 1), (1234.56, 5), (0.5, 5),
                      (1.2345678, 4), (12345.6789, 0), (100.5, 2)]:
            v = round_price(px, d)
            max_dp = max(0, 6 - d)
            self.assertEqual(round(v, max_dp), v,
                             msg=f"{px}@{d} -> {v} 小数位超过 {max_dp}")

    def test_non_positive_price_raises(self):
        with self.assertRaises(ValueError):
            S.round_price(0.0, 4)
        with self.assertRaises(ValueError):
            S.round_price(-5.0, 4)

    def test_spot_not_supported(self):
        with self.assertRaises(ValueError):
            S.round_price(100.0, 4, is_perp=False)

    def test_round_price_then_round_size_stable(self):
        # 执行器真实路径：先取整价格、再 floor 数量，两步都不能抛
        px = round_price(84_123.456, SZ_DEC)
        sz = round_size(0.047619047619047616, SZ_DEC)
        self.assertEqual(px, 84_123.0)
        self.assertEqual(sz, 0.04761)


# ================================================================ 最小名义
class TestMeetsMinNotional(unittest.TestCase):
    def test_at_minimum(self):
        self.assertTrue(meets_min_notional(0.0001, 100_000.0))       # = 10 USDC
        self.assertTrue(meets_min_notional(1.0, 10.0))
        self.assertTrue(meets_min_notional(2.0, 5.0))
        self.assertTrue(meets_min_notional(0.5, 20.0))

    def test_below_minimum(self):
        self.assertFalse(meets_min_notional(0.00009, 100_000.0))     # = 9 USDC
        self.assertFalse(meets_min_notional(0.0001, 99_999.0))       # = 9.9999 USDC
        self.assertFalse(meets_min_notional(1.0, 9.99))

    def test_zero_or_negative(self):
        self.assertFalse(meets_min_notional(0.0, 100_000.0))
        self.assertFalse(meets_min_notional(1.0, 0.0))
        self.assertFalse(meets_min_notional(-1.0, 100.0))

    def test_custom_minimum(self):
        self.assertEqual(DEFAULT_MIN_NOTIONAL, 10.0)
        self.assertTrue(meets_min_notional(1.0, 5.0, min_notional=5.0))
        self.assertFalse(meets_min_notional(1.0, 5.0, min_notional=6.0))

    def test_default_is_10_usdc(self):
        self.assertFalse(meets_min_notional(0.0001, 99_999.0))       # 默认 10 USDC 下限


# ================================================================ 强平价
class TestLiqPrice(unittest.TestCase):
    def test_long_formula(self):
        # entry×(1 - 1/lev + mmr)；5x → 强平在 -19.5%
        self.assertAlmostEqual(liq_price(100_000.0, 5.0, 1), 80_500.0)
        self.assertAlmostEqual(1 - liq_price(100_000.0, 5.0, 1) / 100_000.0, 0.195, places=3)

    def test_short_formula(self):
        self.assertAlmostEqual(liq_price(100_000.0, 5.0, -1), 119_500.0)
        self.assertAlmostEqual(liq_price(100_000.0, 5.0, -1) / 100_000.0 - 1, 0.195, places=3)

    def test_1x_long_is_at_mmr(self):
        self.assertAlmostEqual(liq_price(100.0, 1.0, 1), 100.0 * 0.005)

    def test_invalid_args_raise(self):
        with self.assertRaises(ValueError):
            liq_price(0.0, 5.0)
        with self.assertRaises(ValueError):
            liq_price(100.0, 0.0)
        with self.assertRaises(ValueError):
            liq_price(100.0, 5.0, side=2)


# ================================================================ 杠杆上限
class TestMaxSafeLeverage(unittest.TestCase):
    def test_formula(self):
        self.assertAlmostEqual(max_safe_leverage(0.025), 20.0)
        self.assertAlmostEqual(max_safe_leverage(0.10), 5.0)
        self.assertAlmostEqual(max_safe_leverage(0.05), 10.0)
        self.assertAlmostEqual(max_safe_leverage(0.015), 0.5 / 0.015, places=9)
        self.assertAlmostEqual(max_safe_leverage(0.20), 2.5)

    def test_narrower_stop_allows_higher_leverage(self):
        self.assertGreater(max_safe_leverage(0.02), max_safe_leverage(0.08))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            max_safe_leverage(0.0)
        with self.assertRaises(ValueError):
            max_safe_leverage(-0.01)


# ================================================================ 止损方向
class TestValidateStopSide(unittest.TestCase):
    def test_valid_returns_none(self):
        self.assertIsNone(validate_stop_side(1, 100.0, 97.0))
        self.assertIsNone(validate_stop_side(-1, 100.0, 103.0))

    def test_invalid_returns_chinese_message(self):
        self.assertIn("做多", validate_stop_side(1, 100.0, 103.0))
        self.assertIn("做空", validate_stop_side(-1, 100.0, 97.0))
        # 止损等于入场也算非法（无风险单位可算）
        self.assertIn("做多", validate_stop_side(1, 100.0, 100.0))
        self.assertIn("做空", validate_stop_side(-1, 100.0, 100.0))
        self.assertIn("side", validate_stop_side(0, 100.0, 97.0))
        self.assertIn("side", validate_stop_side(2, 100.0, 97.0))


# ================================================================ 仓位换算
class TestSizeOrderHl(unittest.TestCase):
    def test_long_plan_full_fields(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, EQUITY, RISK_PCT, LEV, 1)
        # 目标名义 = 100U ÷ 2.4286% = 4117.65；sz = 0.049019607… floor 到 5 位 = 0.04901
        self.assertEqual(o.coin, "BTC")
        self.assertEqual(o.side, 1)
        self.assertEqual(o.entry_price, ENTRY)
        self.assertEqual(o.stop_price, STOP_LONG)
        self.assertAlmostEqual(o.stop_dist, 2040.0 / ENTRY, places=12)
        self.assertEqual(o.sz, 0.04901)
        self.assertAlmostEqual(o.notional, 0.04901 * ENTRY, places=6)
        self.assertAlmostEqual(o.risk_u, 0.04901 * 2040.0, places=6)
        self.assertAlmostEqual(o.margin, o.notional / LEV, places=6)
        self.assertEqual(o.leverage, LEV)
        self.assertAlmostEqual(o.max_safe_leverage, 0.5 / o.stop_dist, places=9)
        self.assertAlmostEqual(o.liq_price, ENTRY * (1 - 0.2 + 0.005), places=6)
        self.assertFalse(o.below_min_notional)
        self.assertEqual(o.warnings, [])

    def test_short_mirrors_long(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_SHORT, EQUITY, RISK_PCT, LEV, -1)
        self.assertEqual(o.side, -1)
        self.assertAlmostEqual(o.stop_dist, 2140.0 / ENTRY, places=12)
        self.assertEqual(o.sz, 0.04672)
        self.assertAlmostEqual(o.risk_u, 0.04672 * 2140.0, places=6)
        self.assertAlmostEqual(o.liq_price, ENTRY * (1 + 0.2 - 0.005), places=6)
        self.assertFalse(o.below_min_notional)

    def test_long_liq_price_below_stop(self):
        # 强平价必须**晚于**止损价：多单的清算价在止损价下方
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, EQUITY, RISK_PCT, LEV, 1)
        self.assertLess(o.liq_price, o.stop_price)

    def test_short_liq_price_above_stop(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_SHORT, EQUITY, RISK_PCT, LEV, -1)
        self.assertGreater(o.liq_price, o.stop_price)

    def test_sz_is_floored_not_rounded(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, EQUITY, RISK_PCT, LEV, 1)
        raw = (EQUITY * RISK_PCT) / o.stop_dist / ENTRY
        self.assertLess(o.sz, raw)                       # floor 严格小于目标（目标非整数倍）
        self.assertAlmostEqual(o.sz * 10 ** SZ_DEC, 4901.0, places=6)
        self.assertEqual(round(raw, SZ_DEC), 0.04902)    # round 会多给 1 个步长

    def test_risk_u_never_exceeds_budget(self):
        for entry, stop, eq, rp in [(ENTRY, STOP_LONG, EQUITY, RISK_PCT),
                                    (100.0, 98.0, 500.0, 0.02),
                                    (0.5, 0.49, 20.0, 0.05),
                                    (50.0, 49.0, 1000.0, 0.01)]:
            o = size_order_hl("X", SZ_DEC, entry, stop, eq, rp, LEV, 1)
            self.assertLessEqual(o.risk_u, eq * rp, msg=f"风险超预算: {entry}")

    def test_stop_too_wide_warns(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, ENTRY * 0.88, EQUITY, RISK_PCT, LEV, 1)
        self.assertGreater(o.stop_dist, STOP_MAX)
        self.assertTrue(any("止损距离" in w for w in o.warnings))
        self.assertEqual(o.warnings[0], f"止损距离 12.00% 超出建议区间 1.5%~10%")

    def test_stop_too_tight_warns(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, ENTRY * 0.995, EQUITY, RISK_PCT, LEV, 1)
        self.assertLess(o.stop_dist, STOP_MIN)
        self.assertEqual(len(o.warnings), 1)
        self.assertIn("止损距离", o.warnings[0])
        self.assertIn("1.5%", o.warnings[0])

    def test_stop_within_range_no_distance_warning(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, EQUITY, RISK_PCT, LEV, 1)
        self.assertTrue(STOP_MIN <= o.stop_dist <= STOP_MAX)
        self.assertFalse(any("止损距离" in w for w in o.warnings))

    def test_leverage_above_max_safe_warns(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, EQUITY, RISK_PCT, 50.0, 1)
        self.assertGreater(o.leverage, o.max_safe_leverage)
        self.assertTrue(any("杠杆" in w and "安全上限" in w for w in o.warnings))
        self.assertTrue(any("强平价会早于止损价" in w for w in o.warnings))

    def test_leverage_at_limit_does_not_warn(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, EQUITY, RISK_PCT, LEV, 1)
        self.assertLessEqual(o.leverage, o.max_safe_leverage)
        self.assertFalse(any("杠杆" in w for w in o.warnings))

    def test_below_min_notional_warns(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, 1.0, RISK_PCT, LEV, 1)
        self.assertTrue(o.below_min_notional)
        self.assertEqual(o.sz, 0.0)
        self.assertEqual(o.notional, 0.0)
        self.assertEqual(o.warnings, [f"名义 0.00 USDC 小于交易所 10 USDC 下限"])

    def test_custom_min_notional(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, EQUITY, RISK_PCT, LEV, 1,
                          min_notional=5000.0)
        self.assertLess(o.notional, 5000.0)
        self.assertTrue(o.below_min_notional)
        self.assertIn("5000 USDC 下限", o.warnings[0])

    def test_plan_is_frozen(self):
        o = size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, EQUITY, RISK_PCT, LEV, 1)
        self.assertIsInstance(o, HlOrderPlan)
        with self.assertRaises(FrozenInstanceError):
            o.sz = 999.0

    def test_invalid_inputs_raise(self):
        base = dict(coin="BTC", sz_decimals=SZ_DEC, entry_price=ENTRY,
                    stop_price=STOP_LONG, equity=EQUITY, risk_pct=RISK_PCT,
                    leverage=LEV, side=1)
        bad = [
            dict(coin=""), dict(coin=None),
            dict(entry_price=0.0), dict(entry_price=-1.0),
            dict(stop_price=0.0), dict(stop_price=-1.0),
            dict(equity=0.0), dict(equity=-1.0),
            dict(risk_pct=0.0), dict(risk_pct=-0.01),
            dict(leverage=0.0), dict(leverage=-5.0),
            dict(sz_decimals=-1),
        ]
        for kwargs in bad:
            args = {**base, **kwargs}
            with self.assertRaises(ValueError, msg=f"未拦下: {kwargs}"):
                size_order_hl(**args)

    def test_direction_rejected(self):
        # 做多止损不低于入场、做空止损不高于入场
        with self.assertRaises(ValueError):
            size_order_hl("BTC", SZ_DEC, ENTRY, ENTRY, EQUITY, RISK_PCT, LEV, 1)
        with self.assertRaises(ValueError):
            size_order_hl("BTC", SZ_DEC, ENTRY, ENTRY * 1.02, EQUITY, RISK_PCT, LEV, 1)
        with self.assertRaises(ValueError):
            size_order_hl("BTC", SZ_DEC, ENTRY, ENTRY, EQUITY, RISK_PCT, LEV, -1)
        with self.assertRaises(ValueError):
            size_order_hl("BTC", SZ_DEC, ENTRY, ENTRY * 0.98, EQUITY, RISK_PCT, LEV, -1)

    def test_bad_side_rejected(self):
        for side in (0, 2, -10):
            with self.assertRaises(ValueError):
                size_order_hl("BTC", SZ_DEC, ENTRY, STOP_LONG, EQUITY, RISK_PCT, LEV, side)

    def test_stop_distance_bounds_match_strategy(self):
        # 与 app/strategy.py 的过滤器保持一致，避免两处口径漂移
        self.assertEqual(STOP_MIN, 0.015)
        self.assertEqual(STOP_MAX, 0.10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
