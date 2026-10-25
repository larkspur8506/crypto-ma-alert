"""面板 title 口径全面可见化（fix/title-visibility）。

背景：iOS Safari 无 title 悬停（长按只出复制菜单），靠 title 放的口径/内容手机上读不到。
上一轮（PR #23/#24）把「三标的动态」的口径做成可见图例；本轮收口其余各处：

A. 总览三条可见注脚（账户 / 系统 / 数据源健康）—— 原文渲染断言 + 位置断言。
   豁免：资金分布的 title（「…合约侧占用 = 已用保证金」）**不做**可见化，
   「已用保证金」这个词不上可见文本（title 保留）。
B. 记录 Tab：
   - 消息 >160 字符 → 原生 <details> 折叠（零 JS），<summary> 截断、.fullmsg 含全文；
   - ≤160 字符 → 保持原 <span title=…>（不新增可点噪音）；
   - 熔断 badge：触发且原因非空 → 值可见带「已触发 · 原因」；未触发不变、title 保留。
C. ui_theme CSS：.legend 与 .sym-legend 合组、.msgdetails summary / .fullmsg 规则。

全部临时库、零网络；文件内不写任何本地绝对路径。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:                                          # pragma: no cover - CI 里必然已装
    HAVE_FASTAPI = False

if HAVE_FASTAPI:
    from app.db import DB
    from app.admin_ui import AdminDeps, _msg_cell, _status_strip, render_records
    from app.hl.views import render_dashboard

NOW = 1_760_000_000.0

# 三条注脚的原文（与源码逐字一致；用常量便于「渲染原文」断言）
LEGEND_ACCOUNT = ("已实现为交易所口径，未扣手续费与资金费 · 可提 = 现货 − 仓位/挂单占用")
LEGEND_SYSTEM = ("卡单 = 持续 3 分钟未推进才计入 · "
                 "HL 延迟 = 容器请求 /info 耗时（含失败重试），与本机网速无关")
LEGEND_FEED = "信号源固定 hl-candle（fail-closed：源失败即跳过本轮，不换源）"

_TITLE_ATTR = re.compile(r"""\s+title\s*=\s*("[^"]*"|'[^']*')""")
_TAG = re.compile(r"<[^>]+>")


def visible_text(html: str) -> str:
    """剥掉所有 title 属性与标签后的可见文本（近似手机上真正能读到的内容）。"""
    return _TAG.sub(" ", _TITLE_ATTR.sub("", html))


def overview_payload(**over) -> dict:
    """总览页最小 payload：统一账户 + 一条数据源健康行（覆盖三条注脚所在区块）。"""
    p = {
        "mode": "live",
        "account": {
            "address": "0xABCDEF0123", "unified": True, "spot_ok": True,
            "equity": 100.0, "spot_usdc": 100.0, "spot_hold": 10.0,
            "withdrawable": 0.0, "margin_used": 14.0, "notional": 500.0,
            "unrealized_pnl": 1.0, "today_realized": 0.0, "total_realized": 0.0,
            "funding_total": 0.0, "fees_total": 0.0,
        },
        "system": {
            "mismatch": False,
            "feed": [{"symbol": "BTC_USDT", "coin": "BTC", "ok": True,
                      "last_ok_at": NOW, "last_error": None}],
            "last_bar": {},
        },
        "network": {"base_url": "https://api.hyperliquid.xyz", "testnet": False,
                    "latency_ms": 12.0, "reachable": True},
        "positions": [], "orders_open": [], "fills": [], "signals": [],
        # P2：三标的卡也承载「数据源健康/已处理到」内容（fail-closed 脚注在卡图例里），
        # 给一行 symbol_state 让该卡渲染出来
        "symbol_state": [{"symbol": "BTC_USDT", "coin": "BTC", "price": 100.0,
                          "updated_at": NOW}],
        "curve": {},
    }
    p.update(over)
    return p


# ================================================================ A. 总览三条注脚
class TestOverviewLegends(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")
        self.html = render_dashboard(overview_payload())

    def test_account_legend_visible(self):
        frag = f'<div class="legend">{LEGEND_ACCOUNT}</div>'
        self.assertIn(frag, self.html)
        # 在「账户」卡内（账户与盈亏曲线之间）
        self.assertLess(self.html.index("账户"), self.html.index(frag))
        self.assertLess(self.html.index(frag), self.html.index("<h2>盈亏曲线（已剔除出入金）</h2>"))

    def test_system_legend_visible(self):
        frag = f'<div class="legend">{LEGEND_SYSTEM}</div>'
        self.assertIn(frag, self.html)
        # 在系统卡内（P2：「数据源健康」表已合并进三标的卡，不存在于本页）
        self.assertLess(self.html.index("<h2>系统</h2>"), self.html.index(frag))
        self.assertNotIn("数据源健康", self.html)

    def test_feed_legend_visible(self):
        # P2 合并：fail-closed 口径脚注随数据源小字行搬进「三标的动态」卡图例
        self.assertIn(LEGEND_FEED, self.html)
        self.assertLess(self.html.index("三标的动态"), self.html.index(LEGEND_FEED))

    def test_margin_legacy_term_not_in_visible_text(self):
        """豁免：剥掉所有 title 后，「已用保证金」不得出现在可见文本里。"""
        self.assertNotIn("已用保证金", visible_text(self.html))
        # 但桌面端 title 保留（增强，不删）
        self.assertIn("合约侧占用 = 已用保证金", self.html)

    def test_legend_css_shipped(self):
        self.assertIn(".sym-legend,.legend{", self.html)
        self.assertIn(".msgdetails summary{cursor:pointer;color:var(--muted)}", self.html)
        self.assertIn(".msgdetails .fullmsg{white-space:pre-wrap;", self.html)


# ================================================================ B. 记录 Tab
class TestMsgCell(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")

    def test_short_keeps_span_title(self):
        cell = _msg_cell("短消息 ok")
        self.assertEqual(cell, "<span title='短消息 ok'>短消息 ok</span>")
        self.assertNotIn("details", cell)

    def test_boundary_160_keeps_span(self):
        msg = "A" * 160
        cell = _msg_cell(msg)
        self.assertNotIn("details", cell)
        self.assertIn(f"<span title='{msg}'>{msg}</span>", cell)

    def test_long_uses_details_with_full_text(self):
        msg = "长" + "A" * 200          # 201 字符 > 160
        cell = _msg_cell(msg)
        self.assertIn('<details class="msgdetails">', cell)
        self.assertIn(f'<div class="fullmsg">{msg}</div>', cell)
        # summary 是截断版（含省略号），不是全文
        self.assertIn("…</summary>", cell)
        self.assertNotIn(f"<summary>{msg}</summary>", cell)


class TestRecordsRender(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def _records(self):
        deps = AdminDeps(db=self.db, config_fn=lambda: None, hl_deps=None,
                         mode_fn=lambda: "live", token_fn=lambda: "tk")
        return render_records(deps)

    def test_long_alert_and_event_use_details(self):
        long_msg = "超长提醒" + "B" * 200
        self.db.log_alert("BTC_USDT", 1_700_000_000_000, "signal", long_msg, True)
        long_ev = "超长事件" + "C" * 200
        self.db.hl_log_event("decision", long_ev, "BTC_USDT")
        html = self._records()
        for msg in (long_msg, long_ev):
            self.assertIn('<details class="msgdetails">', html)
            self.assertIn(f'<div class="fullmsg">{msg}</div>', html)

    def test_short_alert_no_details(self):
        self.db.log_alert("BTC_USDT", 1_700_000_000_000, "signal", "开多 BTC", True)
        html = self._records()
        self.assertIn("<span title='开多 BTC'>开多 BTC</span>", html)
        self.assertNotIn("msgdetails", html)


class TestKillBadge(unittest.TestCase):
    def setUp(self):
        if not HAVE_FASTAPI:
            self.skipTest("fastapi 未安装")

    def _strip(self, kill, reason):
        return _status_strip({
            "mode": "live", "kill": kill, "kill_reason": reason,
            "system": {}, "network": {}, "ts": NOW,
        })

    def test_kill_triggered_reason_visible(self):
        html = self._strip(True, "当日亏损熔断")
        self.assertIn("熔断 <b>已触发 · 当日亏损熔断</b>", html)
        # title 保留（桌面增强）
        self.assertIn("title='当日亏损熔断'", html)

    def test_kill_triggered_without_reason(self):
        html = self._strip(True, "")
        self.assertIn("熔断 <b>已触发</b>", html)
        self.assertNotIn("已触发 ·", html)

    def test_kill_not_triggered_unchanged(self):
        html = self._strip(False, "旧原因")
        self.assertIn("熔断 <b>未触发</b>", html)
        self.assertNotIn("已触发", html)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
