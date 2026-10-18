"""面板「每页行数」设置 + 提醒日志可读性的回归测试（2026-10-15 用户反馈）。

验收点：
- 默认每页 20 行；?n=10 / ?n=50 可切；非法值一律回落默认（展示层，不影响任何写路径）
- 页脚「每页行数」链接 → /admin/rows 设 cookie 后 302 回原 tab，cookie 记住选择
- 记录页「成交」跟随同一设置（总览的「下单记录 / 最近成交」2026-10-18 已迁到记录页）
- 总览不再有「执行器事件」（与记录页重复）；记录页仍有
- 提醒日志「类型」列显示中文（exec_alert_fills → 执行器成交回报）
- 回填历史（message 以「[回填] 」开头）出现时，区块上方给出口径说明

全部用临时 sqlite DB + 假 HlInfo，不触网。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_admin_ui import BaseCase, NOW, TOKEN          # noqa: E402
from app.hl.views import DEFAULT_ROWS, ROWS_COOKIE, normalize_rows  # noqa: E402

BAR = 1_700_000_000_000


def _fill(db, i: int) -> None:
    db.hl_upsert_fill(tid=f"f{i}", symbol="BTC_USDT", coin="BTC", side=1, sz=0.001,
                      px=100_000.0 + i, fee=0.01, fee_token="USDT", dir="a",
                      closed_pnl=1.0, hash=f"f{i}", reduce_only=True,
                      ts=int(NOW * 1000) + i, created_at=NOW + i)


def _alert(db, i: int, kind: str = "signal", msg: str | None = None) -> None:
    db.log_alert("BTC_USDT", BAR + i, kind, msg or f"REC-{i:03d}", True)


class TestNormalizeRows(unittest.TestCase):
    def test_choices_and_fallback(self):
        self.assertEqual(normalize_rows(None), DEFAULT_ROWS)
        self.assertEqual(normalize_rows(""), DEFAULT_ROWS)
        self.assertEqual(normalize_rows("10"), 10)
        self.assertEqual(normalize_rows("20"), 20)
        self.assertEqual(normalize_rows("50"), 50)
        for bad in ("0", "7", "999", "abc", "-1", "20.5", None):
            self.assertEqual(normalize_rows(bad), DEFAULT_ROWS, f"{bad!r} 应回落默认")


class TestRowsSwitch(BaseCase):
    """页脚切换链接 + cookie 记住选择。"""

    def test_switch_sets_cookie_and_redirects_back(self):
        r = self.get("/admin/rows", n=10, tab="records")
        self.assertEqual(r.status_code, 302)
        self.assertIn("tab=records", r.headers["location"])
        self.assertIn(TOKEN, r.headers["location"])
        self.assertEqual(r.cookies.get(ROWS_COOKIE), "10")

    def test_switch_clamps_illegal_value(self):
        r = self.get("/admin/rows", n=999, tab="records")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.cookies.get(ROWS_COOKIE), str(DEFAULT_ROWS))

    def test_cookie_is_remembered_on_next_render(self):
        for i in range(60):
            _alert(self.db, i)
        self.assertEqual(self.get("/admin/rows", n=50, tab="records").status_code, 302)
        html = self.body(tab="records")                     # 不带 ?n=，只靠 cookie
        self.assertIn("REC-010", html)
        self.assertNotIn("REC-009", html, "cookie 里的 50 行没生效")
        self.assertEqual(self.get("/admin/rows", n=10, tab="records").status_code, 302)
        html = self.body(tab="records")
        self.assertIn("REC-050", html)
        self.assertNotIn("REC-049", html, "切回 10 行的 cookie 没生效")

    def test_footer_has_three_presets(self):
        html = self.body(tab="records")
        for n in (10, 20, 50):
            self.assertIn(f"/admin/rows?n={n}&tab=records", html)
        self.assertIn("每页行数", html)


class TestRecordsRows(BaseCase):
    def test_default_20_and_param_overrides(self):
        for i in range(60):
            _alert(self.db, i)
        html = self.body(tab="records")
        self.assertIn("最近 20 条", html)
        self.assertIn("REC-059", html)
        self.assertNotIn("REC-039", html, "默认 20 行没生效")
        html = self.body(tab="records", n=10)
        self.assertNotIn("REC-049", html)
        self.assertIn("REC-050", html)
        html = self.body(tab="records", n=50)
        self.assertIn("REC-010", html)
        self.assertNotIn("REC-009", html)

    def test_invalid_param_falls_back(self):
        for i in range(60):
            _alert(self.db, i)
        html = self.body(tab="records", n="abc")
        self.assertIn("REC-040", html)
        self.assertNotIn("REC-039", html)


class TestRecordsTableRows(BaseCase):
    """成交表跟随「每页行数」，且标题写明「最近 N 条」。

    2026-10-18：这张表原来在总览（h2 标题），现在只在记录页（卡片 sec 标题）——
    断言位置跟着搬，行数口径不变。
    """

    @staticmethod
    def _count_rows(html: str, title: str) -> int:
        m = re.search(re.escape(title) + r".*?</thead><tbody>(.*?)</tbody>", html, re.S)
        assert m, f"找不到「{title}」表"
        return len(re.findall(r"<tr>", m.group(1)))

    def test_recent_fills_follows_setting(self):
        for i in range(30):
            _fill(self.db, i)
        html = self.body(tab="records")
        self.assertEqual(self._count_rows(html, "成交（hl_fills · 最近 20 条）"), 20)
        html = self.body(tab="records", n=50)
        self.assertEqual(self._count_rows(html, "成交（hl_fills · 最近 50 条）"), 30)
        html = self.body(tab="records", n=10)
        self.assertEqual(self._count_rows(html, "成交（hl_fills · 最近 10 条）"), 10)

    def test_orders_title_shows_count(self):
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, "entry", 1, 0.005, px=100_000.0)
        html = self.body(tab="records")
        self.assertIn("下单记录（hl_orders · 最近 20 条）", html)

    def test_signals_follow_setting(self):
        """信号块也用同一 n 切片（迁入记录页时就按这个口径做）。"""
        for i in range(30):
            self.db.create_position(
                symbol="BTC_USDT", signal_bar_ms=BAR + i, status="open", side=1,
                entry_price=100_000.0 + i, stop_price=97_000.0, stop_dist=0.03,
                contracts=6, coin_qty=0.0006, notional=60.0, risk_u=18.0,
                equity_at_entry=1000.0)
        for n, want in ((20, 20), (50, 30), (10, 10)):
            html = self.body(tab="records", n=n)
            seg = html.split("<h2>信号</h2>", 1)[1].split("</table>", 1)[0]
            got = len(re.findall(r"<tr>", seg.split("</thead><tbody>", 1)[1]))
            self.assertEqual(got, want, f"n={n} 时信号行数不对")

    def test_overview_has_no_long_tables(self):
        """总览不再有长表 → 「每页行数」在那里只是页脚设置，不影响任何表。"""
        for i in range(30):
            _fill(self.db, i)
        html = self.body(tab="overview")
        self.assertNotIn("最近成交（最近", html)
        self.assertNotIn("下单记录（最近", html)
        self.assertNotIn("<h2>信号</h2>", html)


class TestEventsNotOnOverview(BaseCase):
    def test_overview_has_no_events_table_but_records_does(self):
        self.db.hl_log_event("decision", "人工切换执行器模式 → dry", "BTC_USDT")
        ov = self.body(tab="overview")
        self.assertNotIn("执行器事件", ov, "总览不该再有执行器事件（与记录页重复）")
        self.assertNotIn("人工切换执行器模式", ov)
        rec = self.body(tab="records")
        self.assertIn("执行器事件（hl_events · 最近 20 条）", rec)
        self.assertIn("人工切换执行器模式", rec)


class TestAlertKindLabels(BaseCase):
    def test_kind_column_is_chinese(self):
        self.db.log_alert(None, None, "exec_alert_fills", "【HL执行】开仓完成", True)
        self.db.log_alert(None, None, "heartbeat_local", "心跳（静默）", True)
        self.db.log_alert("BTC_USDT", BAR, "exit", "BTC_USDT 离场成交", True)
        html = self.body(tab="records")
        self.assertIn("执行器成交回报", html)
        self.assertIn("心跳（静默）", html)
        self.assertIn("离场提醒", html)
        self.assertNotIn("exec_alert_fills", html)
        self.assertNotIn("heartbeat_local", html)

    def test_unknown_kind_falls_back_to_raw(self):
        self.db.log_alert(None, None, "brand_new_kind", "新类型", True)
        html = self.body(tab="records")
        self.assertIn("brand_new_kind", html)


class TestBackfillNote(BaseCase):
    def test_note_shown_with_count_when_backfilled_rows_exist(self):
        self.db.log_alert(None, None, "exec_alert_fills",
                          "[回填] 【HL执行】BTC_USDT 开仓完成", True)
        self.db.log_alert(None, None, "exec_alert_fills",
                          "[回填] 【HL执行】BTC_USDT 止损单已挂", True)
        html = self.body(tab="records")
        self.assertIn("2026-10-15 之前执行器推送不落库", html)
        self.assertIn("2 条", html)

    def test_no_note_without_backfilled_rows(self):
        self.db.log_alert(None, None, "exec_alert_fills", "【HL执行】正常推送", True)
        html = self.body(tab="records")
        self.assertNotIn("回填", html)


if __name__ == "__main__":
    unittest.main()
