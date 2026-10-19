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
    """P2-5（已按周报 v2 口径改写）：不再做复利推演。

    旧版周报把「按每笔 X% 风险复利 ≈ NU」当卖点，实际是把配置本金乘出来的
    虚拟曲线（真实账户净值在 hl_snapshots 里），已在周报 v2 整段删除。
    这里锁死两件事：文案里不再出现复利/本金推演；上线以来的总 R 仍然正确。
    """

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

    def test_no_compounding_projection(self):
        text = self._weekly([3.0] * 50 + [-1.0] * 50)
        self.assertNotIn("复利", text)
        self.assertNotIn("本金", text)
        self.assertNotIn("18,420", text, "逐笔连乘的虚拟净值不该再出现在周报里")

    def test_lifetime_totals_still_correct(self):
        text = self._weekly([3.0] * 50 + [-1.0] * 50)
        self.assertIn("共 100 笔：胜 50 · 负 50，合计 +100.00R", text)

    def test_missing_snapshot_does_not_fake_equity(self):
        # FakeDB 没有 hl_snapshots/hl_latest_snapshot → 周报必须如实说取不到，
        # 不能拿 cfg.equity（1000U）冒充账户净值。
        text = self._weekly([1.0])
        self.assertIn("本轮取不到净值，下轮补", text)
        self.assertNotIn("1,000", text)


if __name__ == "__main__":
    unittest.main()
