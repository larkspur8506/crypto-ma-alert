"""P2 审核修复的回归测试（2026-10-14）。"""
import unittest

from app.hl import views as hl_views


class TestViewsHelper(unittest.TestCase):
    """P2-1/P2-2：面板口径。"""

    def test_reconcile_untrusted_skips_stop_rule(self):
        # 本地有仓有 stop_oid，交易所快照空挂单但不可信 → 不判 mismatch
        open_by_coin = {"BTC": {"hl_stop_oid": 123}}
        self.assertTrue(hl_views._reconcile(True, [], [], open_by_coin, [],
                                            orders_trusted=True))
        self.assertFalse(hl_views._reconcile(True, [{"coin": "BTC"}], [], open_by_coin, [],
                                             orders_trusted=False),
                         "不可信挂单列表不得触发止损判断")


class TestLiqPriceBothSides(unittest.TestCase):
    def test_liq_short_above_entry(self):
        from app.sizing import liq_price
        long_liq = liq_price(100000, 5, 1)
        short_liq = liq_price(100000, 5, -1)
        self.assertLess(long_liq, 100000)
        self.assertGreater(short_liq, 100000)


class TestCompoundedEquity(unittest.TestCase):
    """P2-5：周报复利逐笔连乘。"""

    def _weekly(self, rs, equity=1000.0, risk_pct=0.02):
        from types import SimpleNamespace
        from app import engine

        class FakeDB:
            def closed_positions(self, since):
                if since == 0.0:
                    return [{"r_multiple": r} for r in rs]
                return []

            def recent_positions(self, n):
                return []

        cfg = SimpleNamespace(alert_prefix="", equity=equity, risk_pct=risk_pct)
        return engine.weekly_text(FakeDB(), cfg)

    def test_compounding_matches_manual(self):
        # 50 笔 +3R + 50 笔 -1R → 连乘 = 1000 * 1.06^50 = 18,420U（旧公式会算 7,244U）
        text = self._weekly([3.0] * 50 + [-1.0] * 50)
        self.assertIn("复利", text)
        self.assertNotIn("(1+0.02R", text, "旧公式文案应已移除")
        # 手算逐笔连乘
        expect = 1000.0
        for r in [3.0] * 50 + [-1.0] * 50:
            expect *= 1 + 0.02 * r
        # 文本里的数 = fmt_money(expect)
        from app.engine import fmt_money
        self.assertIn(fmt_money(expect), text)


if __name__ == "__main__":
    unittest.main()
