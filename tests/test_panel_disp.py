"""面板展示层收口回归（2026-11-07）。

覆盖六件展示层改动，全部按「真渲染 + 同屏断言」验收（单测钉结构不够，
词面不一致只在真渲染里显形 —— 见 review-and-deploy 的展示类改动验收链）：

1. 记录页与提醒/事件子页不得出现内部合约名（_USDT 次数为 0）：
   标的列走 _disp，消息正文走 _disp_text；DB 原值不动。
2. /admin/list/positions 筛选下拉：option 的 value 与文案都用 HL 显示名（ETH），
   取数侧映射回内部键（ETH_USDT）；旧内部键链接照认。
3. 资金流水小额（|x|<0.01 非 0）显示 4 位小数，不再是 ±0.00。
4. 盈亏曲线 SVG：preserveAspectRatio=none 铺满 + vector-effect 抗拉伸，
   SVG 内不再有 <text>（TWR 刻度移成 HTML .axlbl，避免横向拉变形）。
5. 曲线下方统计行负值带 neg 类。
6. QQQ(MR) 行的浮盈 % 标注「（价格）」口径。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_admin_ui import BaseCase, TOKEN, NOW     # noqa: E402

BAR = 1_700_000_000_000


def _seed(db) -> None:
    """造齐四类记录（都是 ETH_USDT 内部键，页面渲染后必须全变 ETH）。"""
    db.create_position(
        symbol="ETH_USDT", signal_bar_ms=BAR, status="open", side=1,
        entry_price=3_000.0, stop_price=2_910.0, stop_dist=0.03, contracts=6,
        coin_qty=0.03, notional=90.0, risk_u=1.2, equity_at_entry=100.0)
    db.log_alert("ETH_USDT", BAR, "exec_alert",
                 "【HL执行】✅ 已开仓 · ETH_USDT 做多 5x 逐仓", True)
    db.hl_log_event("order", "ETH_USDT 已开仓 0.03 ETH", symbol="ETH_USDT")
    # 小额资金费流水：2 位小数会显示成 +0.00，必须 4 位。
    db.hl_add_flow({"ts": 1_700_000_010.0, "kind": "funding",
                    "amount": 0.0008, "hash": f"small{NOW}"})


class TestRecordsPageDisp(BaseCase):
    """/admin?tab=records 与两个子页的真渲染同屏断言。"""

    def setUp(self):
        super().setUp()
        _seed(self.db)

    def test_records_page_no_internal_symbol(self):
        r = self.get("/admin", tab="records")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertNotIn("_USDT", html, "记录页不得出现内部合约名")
        self.assertIn(">ETH<", html)                      # 标的列显示名
        self.assertIn("已开仓 · ETH 做多", html)           # 消息正文走 _disp_text
        self.assertIn("+0.0008", html)                    # 小额流水 4 位小数

    def test_records_page_other_symbols_in_messages(self):
        """SOL_USDT 等其它合约的消息正文也要剥离（_disp_text 通用后缀口径）。"""
        self.db.log_alert("SOL_USDT", BAR + 1, "exec_alert",
                          "【HL执行】SOL_USDT 平仓完成 @ 93.41（ma20）", True)
        self.db.hl_log_event("order", "SOL_USDT 已平仓 1.0 SOL", symbol="SOL_USDT")
        html = self.get("/admin", tab="records").text
        self.assertNotIn("_USDT", html)
        self.assertIn("SOL 平仓完成 @ 93.41", html)

    def test_disp_text_generic_suffix(self):
        from app.hl.views import _disp_text
        self.assertEqual(_disp_text("SOL_USDT 已平仓"), "SOL 已平仓")
        self.assertEqual(_disp_text("BTC_USDT 已开仓"), "BTC 已开仓")
        self.assertEqual(_disp_text("XPL_USDT @ 1.23"), "XPL @ 1.23")
        self.assertEqual(_disp_text("普通文本不动"), "普通文本不动")

    def test_fills_tiny_closed_pnl_not_zero(self):
        """成交表 |cp|<0.01 非 0 → 4 位小数，不再显示 ±0.00。"""
        self.db.hl_upsert_fill(
            tid="tiny", symbol="ETH_USDT", coin="ETH", side=-1, sz=0.01, px=3_000.0,
            fee=0.0, fee_token="USDC", fee_quote=0.0, dir="Close Short",
            closed_pnl=-0.0004, ts=1_700_000_020_000.0)
        html = self.get("/admin", tab="records").text
        self.assertNotIn("_USDT", html)
        self.assertIn("-0.0004", html)
        self.assertNotIn(">-0.00<", html)

    def test_alerts_subpage_no_internal_symbol(self):
        r = self.get("/admin/list/alerts")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertNotIn("_USDT", html)
        self.assertIn(">ETH<", html)
        self.assertIn("已开仓 · ETH 做多", html)

    def test_events_subpage_no_internal_symbol(self):
        r = self.get("/admin/list/events")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertNotIn("_USDT", html)
        self.assertIn(">ETH<", html)
        self.assertIn("已开仓 0.03 ETH", html)

    def test_positions_subpage_dropdown_uses_display_value(self):
        html = self.get("/admin/list/positions").text
        self.assertNotIn("_USDT", html,
                         "option 的 value 也必须是显示名（ETH），不是内部键")
        self.assertIn("value='ETH'", html)
        self.assertIn(">ETH<", html)                      # 行内标的列（原有 _disp 路径）

    def test_positions_filter_by_display_name(self):
        r = self.get("/admin/list/positions", symbol="ETH")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn(">ETH<", html)                      # 筛中行还在（显示名映射回内部键）
        self.assertIn("selected", html)
        self.assertNotIn("_USDT", html,
                         "显示名筛选的回显/链接也不得带出内部键")

    def test_positions_filter_legacy_internal_key_still_works(self):
        """旧链接（内部键 value）不能 404 / 筛空：映射回内部键取数，回显改显示名。"""
        r = self.get("/admin/list/positions", symbol="ETH_USDT")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn(">ETH<", html)                      # 行还在
        self.assertNotIn("value='ETH_USDT'", html)        # 回显已改写成显示名
        self.assertIn("value='ETH'", html)

    def test_positions_filter_unknown_value_passthrough(self):
        """映射不到的值原样透传（筛空但不炸、不 500）。"""
        r = self.get("/admin/list/positions", symbol="SOL_USDT")
        self.assertEqual(r.status_code, 200)


class TestLedgerAmountPrecision(unittest.TestCase):
    """资金流水金额格式：|x|<0.01 的非零值 4 位小数，其余口径不变。"""

    def setUp(self):
        from app.admin_ui import _ledger_row_html
        self.fn = _ledger_row_html

    def _amt(self, amt):
        import re
        row = self.fn({"ts": 1_700_000_000_000, "kind": "funding",
                       "amount_usd": amt, "coin": "ETH"}, 100.0)
        cells = re.findall(r"<td class='num[^']*'>([^<]*)</td>", row)
        # num 列依次是：时间 / 金额 / 余额 / 关联单 —— 取第 2 个（金额）。
        return cells[1]

    def test_small_nonzero_gets_four_decimals(self):
        self.assertEqual(self._amt(0.0008), "+0.0008")
        self.assertEqual(self._amt(-0.005), "-0.0050")

    def test_num_small_for_fills(self):
        from app.admin_ui import _num_small
        self.assertEqual(_num_small(-0.0004), "-0.0004")
        self.assertEqual(_num_small(0.008), "0.0080")
        self.assertEqual(_num_small(0), "0.00")
        self.assertEqual(_num_small(1.5), "1.50")
        self.assertEqual(_num_small(None), "—")

    def test_zero_and_normal_keep_two_decimals(self):
        self.assertEqual(self._amt(0), "+0.00")
        self.assertEqual(self._amt(1.5), "+1.50")
        self.assertEqual(self._amt(-1234.5), "-1,234.50")

    def test_none_is_dash(self):
        self.assertEqual(self._amt(None), "—")


class TestPnlSvgGeometry(unittest.TestCase):
    """盈亏曲线：铺满 + 抗拉伸 + 文字出 SVG。"""

    def setUp(self):
        from app.hl.views import _pnl_svg
        self.fn = _pnl_svg
        # 3 个点、TWR 两个点：主曲线 + TWR 叠加都成立
        self.svg = self.fn([[1.0, 5.0], [2.0, -3.0], [3.0, 8.0]],
                           [[1.0, 100.0], [2.0, 98.0], [3.0, 103.0]])

    def test_stretches_to_fill(self):
        self.assertIn("preserveAspectRatio='none'", self.svg)

    def test_strokes_do_not_scale(self):
        self.assertGreaterEqual(self.svg.count("vector-effect='non-scaling-stroke'"), 3,
                                "两条主曲线 + TWR 折线都要 non-scaling-stroke")

    def test_no_text_inside_svg(self):
        self.assertNotIn("<text", self.svg.split("</svg>")[0],
                         "SVG 内 <text> 会被非等比拉伸变形，刻度必须移出")
        self.assertIn("class='axlbl'", self.svg)          # HTML 覆盖层刻度

    def test_dot_is_round_cap_line_not_circle(self):
        self.assertNotIn("<circle", self.svg)
        self.assertIn("class='gdot'", self.svg)

    def test_empty_state_keeps_centered_text(self):
        empty = self.fn([], [])
        self.assertIn("盈亏曲线", empty)
        self.assertNotIn("preserveAspectRatio='none'", empty)


class TestCurveStatsNegClass(unittest.TestCase):
    """曲线下方统计行：负值套现有 neg 类。"""

    def test_neg_txt(self):
        from app.hl.views import _neg_txt
        self.assertEqual(_neg_txt(-1.5, "-1.50U"),
                         "<span class='neg'>-1.50U</span>")
        self.assertEqual(_neg_txt(0, "0.00U"), "0.00U")
        self.assertEqual(_neg_txt(2.5, "+2.50%"), "+2.50%")
        self.assertEqual(_neg_txt(None, "—"), "—")


class TestMrPricePctNote(BaseCase):
    """QQQ(MR) 行：upnl_pct 是价格涨跌幅口径，必须标注「（价格）」。"""

    def test_holding_row_annotated(self):
        from app.hl.views import _render_body, status_payload
        p = status_payload(self.hl_deps)
        p["mr"] = {
            "enabled": True,
            "pos": {"qty": 2.0, "entry_price": 50.0, "mark": 51.0, "upnl": 2.0,
                    "upnl_pct": 2.0, "notional": 102.0, "margin": 102.0,
                    "leverage": 1, "exit_line": 49.0, "dist_to_exit_pct": 3.9},
            "signal": {"next_eval_et": "2026-11-09 09:30"},
        }
        html = _render_body(p)
        self.assertIn("%（价格）", html)
        self.assertIn("价格涨跌幅", html)                  # 图例里的口径说明
        self.assertIn("非 ROE", html)


if __name__ == "__main__":
    unittest.main()
