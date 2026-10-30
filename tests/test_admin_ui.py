"""管理面板（app/admin_ui.py）的单元测试。

验收点逐条对应（2026-10-30 面板精简后：只剩 overview / records 两个页签）：
- 未授权 401
- 两个 Tab 各 200 且含关键区块串（账户总值 / 下单记录 …）
- 手机优先：无框架、无外部资源、零 JS、字号 ≥13px
- 参数页/开关页及其 POST 端点已删除（一律 404/405，不设兼容层）
- kill switch 往返（页面入口搬到总览页尾「管理」卡）
- 旧链接 /hl、/admin/rules → 总览；/admin/logs → 记录页
- /api/hl/status 仍 200（合并没弄坏 JSON API）
- info 只读接口全挂时两个页签仍 200

全部用临时 sqlite DB + 假 HlInfo，不触网。
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "panel-tk"

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.hl.views import DashboardDeps, build_router as build_hl_router
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = DashboardDeps = build_hl_router = None

NOW = 1_760_000_000.0
ADDR = "0xTESTADDRESS123456"

def fake_snapshot(equity=100.68, with_pos=True):
    """与 HlInfo.snapshot 口径一致的固定快照。equity_total=100.68 → 生效基数 100。"""
    snap = {"ts": NOW, "address": ADDR, "equity_total": equity, "equity": equity,
            "spot_usdc": equity, "unified": True, "withdrawable": 90.0,
            "margin_used": 10.0, "notional": 500.0, "positions": [], "open_orders": []}
    if with_pos:
        snap["positions"] = [{
            "coin": "BTC", "side": 1, "size": 0.005, "entry_px": 99_000.0,
            "mark_px": 100_000.0, "position_value": 500.0, "liq_px": 80_000.0,
            "leverage": 5, "isolated": True, "margin_used": 100.0,
            "unrealized_pnl": 5.0, "roe": 0.05, "funding_since_open": -0.5,
        }]
        snap["open_orders"] = [{
            "coin": "BTC", "oid": 7, "is_buy": False, "sz": 0.005, "limit_px": 0.0,
            "reduce_only": True, "trigger_px": 96_000.0, "is_trigger": True,
            "timestamp": int(NOW * 1000),
        }]
    return snap


class FakeInfo:
    """HlInfo 替身。raise_=True 模拟只读接口全挂（面板必须仍 200）。"""

    def __init__(self, snapshot=None, mids=None, raise_=False):
        self._snap = snapshot if snapshot is not None else fake_snapshot()
        self._mids = dict(mids) if mids is not None else {"BTC": 100_000.0, "ETH": 3_000.0}
        self.raise_ = raise_

    def snapshot(self, address, mids=None):
        if self.raise_:
            raise RuntimeError("boom: network down")
        return self._snap

    def all_mids(self):
        if self.raise_:
            raise RuntimeError("boom: allMids down")
        return dict(self._mids)


class _FakeHlInfo:
    """顶掉 app.hl.info.HlInfo：app.main 一 import 就会构造真实客户端并打网络。"""

    def __init__(self, *args, **kwargs):
        self._mids = {}

    def snapshot(self, address, mids=None):
        return {"ts": NOW, "equity_total": 100.0, "equity": 100.0, "spot_usdc": 100.0,
                "unified": True, "withdrawable": 90.0, "margin_used": 10.0,
                "notional": 0.0, "positions": [], "open_orders": []}

    def all_mids(self):
        return {}


class BaseCase(unittest.TestCase):
    """临时 DB + 假 info + 只挂合并面板路由的 app。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="admin_ui_")
        # 让 RISK_PCT 走 env（面板要显示「来源 env」）；值与代码默认一致，不影响断言
        self._env_backup = {k: os.environ.get(k) for k in ("RISK_PCT", "STOP_MAX")}
        os.environ["RISK_PCT"] = "0.02"
        os.environ["STOP_MAX"] = "0.1"
        from app.db import DB
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.info = FakeInfo()
        # 口径 DB > env > 代码默认：config_fn 直接复用生产同一条路径
        self.cfg_cache = {"cfg": Config()}
        self.hl_deps = DashboardDeps(
            db=self.db, config_fn=self._cfg, info=self.info,
            address_fn=lambda: ADDR, mode_fn=lambda: self._mode(),
            base_url_fn=lambda: "https://api.hyperliquid-testnet.xyz",
            now_fn=lambda: NOW)
        self.deps = AdminDeps(
            db=self.db, config_fn=self._cfg, hl_deps=self.hl_deps,
            mode_fn=self._mode, token_fn=lambda: TOKEN,
            address_fn=lambda: ADDR, private_key_fn=lambda: "",
            mids_fn=lambda: self.info.all_mids())
        app = FastAPI()
        app.include_router(build_admin_router(self.deps))
        app.include_router(build_hl_router(self.hl_deps))
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, val in self._env_backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    # ---- 依赖替身 ----
    def _cfg(self):
        return self.cfg_cache["cfg"].apply_db(self.db.get_config())

    def _mode(self):
        # set_config 走 json.dumps，所以必须用 get_config_str 解掉引号
        return self.db.get_config_str("hl_exec_mode", "off") or "off"

    def swap_info(self, info):
        """换掉只读客户端替身（deps 和 hl_deps 都要换，否则回落路径还会用旧的）。"""
        self.info = info
        self.deps.hl_deps.info = info
        self.deps.mids_fn = lambda: info.all_mids()

    def get(self, path, **params):
        params.setdefault("k", TOKEN)
        return self.client.get(path, params=params, follow_redirects=False)

    def post(self, path, **data):
        """不跟随 303：用例要检查重定向目标本身。"""
        data.setdefault("k", TOKEN)
        return self.client.post(path, data=data, follow_redirects=False)

    def body(self, path="/admin", **params):
        r = self.get(path, **params)
        self.assertEqual(r.status_code, 200, f"{path} 应 200，实际 {r.status_code}")
        return r.text


class TestAuth(BaseCase):
    def test_no_token_is_401(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 401)

    def test_wrong_token_is_401(self):
        r = self.client.get("/admin", params={"k": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_unauthorized_post_is_401(self):
        # 用保留下来的 kill 端点验鉴权（被删的 POST 端点连鉴权都到不了，直接 404）
        r = self.client.post("/admin/switch/kill", data={"on": "1"})
        self.assertEqual(r.status_code, 401)

    def test_cookie_auth_ok(self):
        self.get("/admin")                       # 带 k → 写 Cookie
        r = self.client.get("/admin")            # 之后只靠 Cookie
        self.assertEqual(r.status_code, 200)

    def test_unknown_tab_falls_back_to_overview(self):
        html = self.body("/admin", tab="javascript:void(0)")
        self.assertIn("账户总值", html)
        self.assertIn("tab=overview", html)

    def test_admin_token_missing_is_500(self):
        self.deps.token_fn = lambda: ""
        r = self.client.get("/admin", params={"k": TOKEN})
        self.assertEqual(r.status_code, 500)


class TestOverviewTab(BaseCase):
    def test_overview_200_with_key_blocks(self):
        html = self.body(tab="overview")
        for s in ("账户总值", "盈亏曲线（已剔除出入金）", "仓位"):
            self.assertIn(s, html)
        # 本 fixture 的快照没带 orders_trusted（挂单不可信）→ P3 R1 fail-open：
        # 判不清就照常渲染挂单表（正常态 0 处的两态断言在 tests/test_panel_p3.py）
        self.assertIn("交易所挂单", html)
        # 2026-10-19：状态卡在总览末尾；P3 R3 降级为 <details>（默认收起）
        self.assertIn("<summary>API 有效期</summary>", html)
        # 2026-10-15 用户反馈：总览与「记录」页的执行器事件表完全重复 → 总览移除
        self.assertNotIn("执行器事件", html)
        # 2026-10-18 同一类操作：下单记录 / 最近成交 / 信号 三张历史表迁到「记录」页
        # （前两张记录页本来就有；信号是本次新增）。总览只剩"当下"信息。
        for gone in ("<h2>下单记录", "<h2>最近成交", "<h2>信号</h2>", "最近成交（最近"):
            self.assertNotIn(gone, html, f"总览不该再有历史表：{gone}")

    def test_overview_keeps_current_state_blocks(self):
        """迁走三张表 + API 有效期卡降级后，总览的当下块必须还在（仓位/执行器）。"""
        html = self.body(tab="overview")
        for keep in ("<h2>仓位</h2>", "<h2>执行器</h2>",
                     "<h2>盈亏曲线（已剔除出入金）</h2>"):
            self.assertIn(keep, html, f"总览缺少当下块：{keep}")
        # P3 R2：对账/熔断两行从「系统」（现「执行器」）卡删除 —— 状态条徽章是唯一出处
        self.assertIn("<summary>API 有效期</summary>", html)
        # 三标的动态（用户钉死在 tabs 之后第一块）仍在
        self.assertIn("三标的动态", html)

    def test_overview_reuses_hl_body_without_duplicate_title(self):
        html = self.body(tab="overview")
        self.assertEqual(html.count("<h1>"), 1, "整页只能有一个 <h1>（hl 自带标题已剥掉）")
        # <h2> 区块是 hl/views._render_body 的产物，admin_ui 自己一个 <h2> 都不渲染
        # （记录页的五块用 <div class='sec'> 标题，不用 <h2>）
        for h2 in ("<h2>仓位</h2>", "<h2>盈亏曲线（已剔除出入金）</h2>", "<h2>执行器</h2>"):
            self.assertIn(h2, html, f"总览应内嵌 _render_body 的 {h2}")
        # 下单记录已迁走 → 总览的 <h2> 计数也随之减少（防"删了又长回来"）
        self.assertNotIn("<h2>下单记录", html)

    def test_status_strip_is_always_present(self):
        for tab in ("overview", "records"):
            html = self.body(tab=tab)
            for s in ("模式", "熔断", "对账", "HL 延迟", "更新"):
                self.assertIn(s, html, f"{tab} 缺状态条：{s}")

    def test_tabs_switch_via_querystring(self):
        html = self.body(tab="overview")
        for tab, label in (("overview", "总览"), ("records", "记录")):
            self.assertIn(f"/admin?tab={tab}&k={TOKEN}", html)
            self.assertIn(label, html)
        # 被删的两个页签不许再出现在导航里
        self.assertNotIn("tab=params", html)
        self.assertNotIn("tab=switch", html)
        self.assertIn("class='on'", html)


class TestAdminCard(BaseCase):
    """总览页尾「管理」卡：面板仅存的三个写动作（2026-10-30 参数/开关页删除后）。

    被删的是**可变性**（改参数/改开关/切模式/重置峰值），kill 这条应急通道必须还在，
    且交互与原来开关页「应急操作」卡一致（字段 on=1/0、按钮文案、配色）。
    """

    def test_kill_roundtrip(self):
        self.assertFalse(self.db.hl_kill())
        r1 = self.post("/admin/switch/kill", on="1")
        self.assertEqual(r1.status_code, 303)
        self.assertTrue(self.db.hl_kill())
        self.assertIn("tab=overview", r1.headers["location"], "拉闸后回总览（开关页已删）")
        r2 = self.post("/admin/switch/kill", on="0")
        self.assertEqual(r2.status_code, 303)
        self.assertFalse(self.db.hl_kill())
        html = self.body(tab="overview")
        self.assertIn("当前未触发", html)
        self.assertTrue(any(e["kind"] == "kill" for e in self.db.hl_recent_events(5)))

    def test_management_card_has_three_write_actions(self):
        html = self.body(tab="overview")
        self.assertIn("<div class='sec'>管理</div>", html)
        self.assertIn("action='/admin/switch/kill'", html)
        self.assertIn("action='/admin/hl/agent/renew'", html)
        # 执行模式只读徽章：有「实盘/影子/只通知」字样，但没有切换表单
        self.assertIn("模式", html)
        self.assertNotIn("name='mode'", html)
        self.assertNotIn("confirm_live", html)

    def test_fallback_clear_only_rendered_when_value_present(self):
        """没有兜底值时不渲染「清除兜底」（没值可清）；有值时出现且能清掉。"""
        html = self.body(tab="overview")
        self.assertNotIn("action='/admin/hl/agent/fallback/clear'", html)
        self.db.set_config("hl_agent_valid_until_ms", "1830000000000")
        html = self.body(tab="overview")
        self.assertIn("action='/admin/hl/agent/fallback/clear'", html)
        self.assertIn("1830000000000", html)
        r = self.post("/admin/hl/agent/fallback/clear")
        self.assertEqual(r.status_code, 303)
        self.assertIn("tab=overview", r.headers["location"])
        # set_config 走 json.dumps：空串在 DB 里是 '""'
        self.assertFalse(str(self.db.get_config_str("hl_agent_valid_until_ms") or "").strip())
        # 卡片按 cfg 读这个键；apply_db 对空串是「不改动」语义（Config._CLEARABLE_STR 为空），
        # 而本夹具复用了同一个 Config 实例 → 换一个干净实例再验渲染条件（等价于进程重启）。
        self.cfg_cache["cfg"] = Config()
        self.assertNotIn("action='/admin/hl/agent/fallback/clear'", self.body(tab="overview"))

    def test_management_card_shows_credentials_state(self):
        html = self.body(tab="overview")
        self.assertIn("私钥：未配置", html)
        self.assertIn("已配置", html)          # 地址


class TestRecordsTab(BaseCase):
    def setUp(self):
        super().setUp()
        BAR = 1_700_000_000_000
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, "entry", 1, 0.005, px=100_000.0)
        self.db.hl_update_order("BTC_USDT", BAR, "entry", status="filled", oid=1,
                                filled_sz=0.005, avg_px=100_000.0)
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR + 100_000, "stop", -1, 0.005,
                                 status="sent")
        self.db.hl_upsert_fill(tid="t1", symbol="BTC_USDT", coin="BTC", side=1, sz=0.005,
                               px=100_000.0, fee=0.05, fee_token="USDT", dir="a",
                               closed_pnl=5.0, hash="t1", reduce_only=True,
                               ts=int(NOW * 1000), created_at=NOW)
        self.db.log_alert("BTC_USDT", BAR, "signal", "开多 BTC", True)
        self.db.hl_log_event("decision", "人工切换执行器模式 → dry", "BTC_USDT")

    def test_records_200_with_key_blocks(self):
        html = self.body(tab="records")
        for s in ("下单记录", "成交", "信号", "提醒日志", "执行器事件",
                  "hl_orders", "hl_fills"):
            self.assertIn(s, html)

    def test_records_signal_block_after_fills(self):
        """信号块必须在「成交」之后、提醒日志之前（与总览原顺序一致）。"""
        html = self.body(tab="records")
        i_orders = html.index("下单记录（hl_orders")
        i_fills = html.index("成交（hl_fills")
        i_signals = html.index("<h2>信号</h2>")
        i_alerts = html.index("提醒日志（alert_logs")
        i_events = html.index("执行器事件（hl_events")
        self.assertLess(i_orders, i_fills)
        self.assertLess(i_fills, i_signals, "信号块应紧跟「成交」之后")
        self.assertLess(i_signals, i_alerts)
        self.assertLess(i_alerts, i_events)
        # 信号块用 _signals_html 渲染（与总览同一函数），标题只出现一次
        self.assertEqual(html.count("<h2>信号</h2>"), 1, "信号标题重复渲染了")

    def test_records_signal_block_renders_rows(self):
        # 信号数据源是 positions 表（payload["signals"]），不是 hl_orders
        self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1_700_000_000_000,
                                status="open", side=1, entry_price=100_000.0,
                                stop_price=97_000.0, stop_dist=0.03, contracts=6,
                                coin_qty=0.0006, notional=60.0, risk_u=18.0,
                                equity_at_entry=1000.0)
        html = self.body(tab="records")
        seg = html.split("<h2>信号</h2>", 1)[1].split("<h2>", 1)[0]
        self.assertIn("<th>信号 bar</th>", seg)
        self.assertIn("BTC", seg)
        self.assertIn("持仓中", seg)                    # status=open → _ST_TXT
        self.assertNotIn("暂无信号", seg)

    def test_records_shows_rows(self):
        html = self.body(tab="records")
        self.assertIn("开仓", html)
        self.assertIn("BTC", html)
        self.assertIn("100,000.00", html)
        self.assertIn("开多 BTC", html)
        self.assertIn("人工切换执行器模式", html)
        self.assertNotIn("暂无下单记录", html)

    def test_records_times_use_display_tz(self):
        html = self.body(tab="records")
        self.assertIn("北京时间", html)
        # NOW = 2025-10-09 08:53 UTC → 北京时间 10-09 16:53
        self.assertIn("10-09 16:53", html)
        self.assertNotIn("10-09 08:53", html)

    def test_records_default_20_rows_and_switches(self):
        """默认每页 20 行（手机优先）；?n=10 / ?n=50 可切；非法值回落默认。"""
        for i in range(60):
            self.db.log_alert("BTC_USDT", 1_700_000_000_000 + i, "signal", f"REC-{i:03d}", True)
        html = self.body(tab="records")
        self.assertIn("REC-059", html)
        self.assertIn("REC-040", html)
        self.assertNotIn("REC-039", html, "默认应只渲染 20 行")
        html10 = self.body(tab="records", n=10)
        self.assertIn("REC-050", html10)
        self.assertNotIn("REC-049", html10)
        html50 = self.body(tab="records", n=50)
        self.assertIn("REC-010", html50)
        self.assertNotIn("REC-009", html50, "?n=50 应渲染 50 行")
        html_bad = self.body(tab="records", n="999")
        self.assertIn("REC-040", html_bad)
        self.assertNotIn("REC-039", html_bad, "非法 n 应回落默认 20")

    def test_records_untouched_by_render_errors(self):
        class BrokenDB:
            """把 db 换成一个坏对象：面板必须降级成空表而不是 500。"""
        class FakeBroken:
            def __getattr__(self, name):
                raise RuntimeError("boom: db 挂了")
        self.deps.db = FakeBroken()
        html = self.body(tab="records")
        for s in ("暂无下单记录", "暂无成交记录", "暂无信号",
                  "暂无提醒日志", "暂无执行器事件"):
            self.assertIn(s, html)


class TestMobileNoJs(BaseCase):
    def test_all_tabs_are_js_free(self):
        """无框架、无外部资源、**零 JS**。

        唯一一段内联 JS（参数页资金口径显隐）随参数页删除；页面从此完全零 JS
        —— 弱网/老浏览器/禁用 JS 都能用，也不再有「脚本没跑起来字段被 disabled
        导致表单静默提交失败」那一类回退问题。
        """
        for tab in ("overview", "records"):
            html = self.body(tab=tab)
            self.assertNotIn("<script", html.lower(), f"{tab} 不该有 <script>")
            self.assertNotIn("javascript:", html.lower())
            self.assertNotIn("<script src", html.lower())
            self.assertNotIn("import ", html.lower())

    def test_no_external_resources(self):
        """页面必须零外部资源：只允许根相对的自家 <link>（图标/manifest 就是这套）。

        口径：<link> 不是禁用，**跨域**的 <link> 才是。所以先剔除 href 以 / 开头的
        同源链接，剩下的（http://、//cdn…、相对路径）任一条出现都算外部资源。
        """
        import re
        for tab in ("overview", "records"):
            html = self.body(tab=tab).lower()
            for bad in (" src=", "<img", "cdn.", "<iframe"):
                self.assertNotIn(bad, html, f"{tab} 含外部资源 {bad}")
            links = re.findall(r"<link [^>]*>", html)
            for tag in links:
                self.assertIn("href='/", tag, f"{tab} 的 <link> 必须根相对（同源）：{tag}")
                self.assertNotIn("http", tag, f"{tab} 的 <link> 不许带协议/域名：{tag}")

    def test_font_size_at_least_13px(self):
        html = self.body(tab="overview")
        self.assertIn("font-size:13px", html)
        self.assertIn("table{font-size:13px}", html)
        self.assertIn("font-size:13.5px", html)

    def test_mobile_viewport_and_horizontal_scroll(self):
        html = self.body(tab="records")
        self.assertIn("width=device-width,initial-scale=1", html)
        self.assertIn("overflow-x:auto", html)

    def test_head_has_home_screen_icon_links(self):
        """手机「添加到主屏」：面板 <head> 必须带 apple-touch-icon / icon / manifest / theme-color。"""
        html = self.body(tab="overview")
        self.assertIn("<link rel='apple-touch-icon' sizes='180x180' href='/apple-touch-icon.png'>", html)
        self.assertIn("<link rel='manifest' href='/site.webmanifest'>", html)
        self.assertIn("<meta name='theme-color' content='#0d1117'>", html)
        # 路径是根相对的（反代换域名后照样同源），不能是写死域名或 ../ 相对
        self.assertNotIn("apple-touch-icon.png' href='http", html)
        # 必须落在 <head> 里，不能跑到 body
        self.assertLess(html.index("apple-touch-icon"), html.index("</head>"))

    def test_page_renders_home_screen_icons(self):
        """_page() 本身（不经 HTTP）也要带 icon 链接 —— 确认页/HL 页共用同一份常量。"""
        import app.admin_ui as admin_mod
        html = bytes(admin_mod._page("tk", "overview", "", "<div>x</div>").body).decode()
        self.assertIn("rel='apple-touch-icon'", html)
        self.assertIn("href='/icon-192.png'", html)
        self.assertIn("href='/icon-512.png'", html)
        self.assertIn("href='/favicon.ico'", html)

    def test_dark_theme_matches_existing_panel(self):
        html = self.body(tab="overview")
        for c in ("#0f1115", "#171a21", "#262a33", "#e6e6e6"):
            self.assertIn(c, html)

    def test_no_emoji_spray_in_status_strip(self):
        html = self.body(tab="overview")
        strip = html.split("<div class='bar'>")[1].split("</div>")[0]
        # P3 R2/R3 后常驻徽标：模式/熔断/对账/HL 延迟/API 有效期/更新（卡单 >0 才加一个点）
        self.assertEqual(strip.count("&#9679;"), 6)


class TestLegacyRedirects(BaseCase):
    def test_hl_redirects_302_and_keeps_k(self):
        r = self.client.get("/hl", params={"k": TOKEN, "refresh": 30}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], f"/admin?tab=overview&k={TOKEN}")

    def test_admin_rules_redirects_3xx(self):
        """参数页已删（2026-10-30）：旧 GET 书签落到总览，不是 404。"""
        r = self.get("/admin/rules")
        self.assertIn(r.status_code, (301, 302, 303))
        self.assertEqual(r.headers["location"], f"/admin?tab=overview&k={TOKEN}")

    def test_admin_logs_redirects_3xx(self):
        r = self.get("/admin/logs")
        self.assertIn(r.status_code, (301, 302, 303))
        self.assertEqual(r.headers["location"], f"/admin?tab=records&k={TOKEN}")

    def test_redirect_without_token_passes_through(self):
        r = self.client.get("/hl", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin?tab=overview", r.headers["location"])

    def test_api_hl_status_still_200(self):
        r = self.client.get("/api/hl/status")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["schema_version"], 1)
        for k in ("ts", "mode", "kill", "network", "account", "positions",
                  "orders_open", "orders_recent", "fills", "signals", "system",
                  "curve", "events"):
            self.assertIn(k, j, f"JSON schema 少了 {k}")
        self.assertIn("equity", j["account"])

    def test_api_hl_equity_and_snapshot_still_200(self):
        for path in ("/api/hl/equity", "/api/hl/snapshot"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("ts", r.json())

    def test_legacy_rules_post_is_gone(self):
        """POST /admin/rules 已随参数表单删除：本 app 只挂了个 GET 同路径 → 405。

        （完整 app（含 main.py）里连 GET 之外的方法都没有 → 见 TestFullAppWiring
        的 404 断言。这里钉住「admin_ui 里彻底没有这条写路径」。）
        """
        r = self.client.post("/admin/rules", data={"equity": "999", "risk_pct": "0.02",
                                                   "leverage": "5", "stop_min": "0.015",
                                                   "stop_max": "0.1", "k": TOKEN})
        self.assertEqual(r.status_code, 405, "旧 POST 不该再有任何实现")


class TestFullAppWiring(unittest.TestCase):
    """挂到真实的 app.main 上，验证合并没有弄坏别的东西。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="admin_full_")
        os.environ["DB_PATH"] = os.path.join(self.tmp, "full.db")
        os.environ["ENABLE_SCHEDULER"] = "false"
        os.environ["ADMIN_TOKEN"] = TOKEN
        os.environ["TOKEN_SECRET"] = "full-secret"
        os.environ["BASE_URL"] = "https://full.example"
        os.environ["DRY_RUN"] = "true"
        os.environ["HL_ACCOUNT_ADDRESS"] = ADDR
        # app.main 在 import 时就会构造真实 HlInfo 并打网络，换成假的避免真请求
        import app.hl.info as _hi
        self._real_hl_info_cls = _hi.HlInfo
        _hi.HlInfo = _FakeHlInfo

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        import app.hl.info as _hi
        _hi.HlInfo = self._real_hl_info_cls
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key in ("DB_PATH", "ENABLE_SCHEDULER", "ADMIN_TOKEN", "TOKEN_SECRET",
                    "BASE_URL", "DRY_RUN", "HL_ACCOUNT_ADDRESS"):
            os.environ.pop(key, None)

    def _mod(self):
        # Config 的默认值在类体里读 env，所以必须连 app.config 一起 reload
        importlib.reload(importlib.import_module("app.config"))
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        return mod

    def test_all_routes_registered(self):
        mod = self._mod()
        paths = set(mod.app.openapi().get("paths", {}).keys())
        for p in ("/health", "/admin", "/admin/switch/kill",
                  "/admin/hl/agent/renew", "/admin/hl/agent/fallback/clear",
                  "/admin/rules", "/admin/logs", "/admin/tick",
                  "/admin/hl/kill", "/c/{pid}",
                  "/hl", "/api/hl/status", "/api/hl/equity", "/api/hl/snapshot"):
            self.assertIn(p, paths, f"路由 {p} 未注册")
        # 2026-10-30 面板精简：这几个写端点的**全部方法**都不该再存在（一律 404/405）
        spec = mod.app.openapi().get("paths", {})
        for gone in ("/admin/params", "/admin/switch/mode", "/admin/switch/strategy",
                     "/admin/switch/notify", "/admin/switch/dd_reset", "/admin/hl/mode"):
            self.assertNotIn("post", {m.lower() for m in spec.get(gone, {})},
                             f"{gone} 的 POST 必须已删除")
        self.assertNotIn("post", {m.lower() for m in spec.get("/admin/rules", {})},
                         "POST /admin/rules 必须已删除")

    def test_admin_panel_200_and_old_status_page_gone(self):
        mod = self._mod()
        with TestClient(mod.app) as c:
            r = c.get("/admin", params={"k": TOKEN})
            self.assertEqual(r.status_code, 200)
            self.assertIn("双均线交易台", r.text)
            for tab in ("overview", "records"):
                self.assertIn(f"tab={tab}", r.text)
            self.assertNotIn("tab=params", r.text)

    def test_hl_302_and_api_status_200_in_full_app(self):
        mod = self._mod()
        with TestClient(mod.app) as c:
            r = c.get("/hl", params={"k": TOKEN}, follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers["location"], f"/admin?tab=overview&k={TOKEN}")
            r = c.get("/api/hl/status", params={"k": TOKEN})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["schema_version"], 1)

    def test_old_endpoints_redirect_in_full_app(self):
        mod = self._mod()
        with TestClient(mod.app) as c:
            # /admin/rules 的旧书签 → 总览（参数页已删）；/admin/logs → 记录页（记录页还在）
            for path, tab in (("/admin/rules", "overview"), ("/admin/logs", "records")):
                r = c.get(path, params={"k": TOKEN}, follow_redirects=False)
                self.assertEqual(r.status_code, 302, path)
                self.assertEqual(r.headers["location"], f"/admin?tab={tab}&k={TOKEN}")

    def test_removed_write_endpoints_404(self):
        """被删的写端点一律 404（不设兼容层，也不留半截实现）。"""
        mod = self._mod()
        with TestClient(mod.app) as c:
            for path, data in (
                    ("/admin/rules", {"equity": "777", "risk_pct": "0.03", "k": TOKEN}),
                    ("/admin/params", {"equity": "777", "k": TOKEN}),
                    ("/admin/switch/mode", {"mode": "dry", "k": TOKEN}),
                    ("/admin/switch/strategy", {"enable_long": "0", "k": TOKEN}),
                    ("/admin/switch/notify", {"fill_push": "1", "k": TOKEN}),
                    ("/admin/switch/dd_reset", {"k": TOKEN}),
                    ("/admin/hl/mode", {"mode": "off", "k": TOKEN})):
                r = c.post(path, data=data, follow_redirects=False)
                # /admin/rules 还留着 GET（旧书签 302），所以它的 POST 是 405；
                # 其余被删端点连路径都没有 → 404。两者都等于「没有写实现」。
                want = (404, 405) if path == "/admin/rules" else (404,)
                self.assertIn(r.status_code, want, f"{path} 应 404（已删除）")
            # 生效值一个都没被碰（参数/模式/开关的 DB 读路径照旧生效）
            self.assertNotEqual(mod.current_config().equity, 777.0)
            self.assertEqual(mod.hl_mode(), "off")

    def test_removed_endpoints_do_not_touch_config(self):
        """参数/开关写路径删除后，配置读路径照旧：DB 里的值仍是生效值。

        这条防的是「删端点时顺手动了 apply_db / get_config」——那样面板看似没写，
        实际生效值变了。写法：直改 DB（模拟未来 runbook 的容器内 sqlite 操作），
        断言 current_config() 立刻跟上。
        """
        mod = self._mod()
        with TestClient(mod.app) as c:
            mod.db.set_config("equity", 250.0)
            mod.db.set_config("risk_pct", 0.03)
            mod.db.set_config("enable_shorts", 0)
            mod.db.set_config("hl_exec_mode", "dry")
            self.assertEqual(mod.current_config().equity, 250.0)
            self.assertAlmostEqual(mod.current_config().risk_pct, 0.03, places=6)
            self.assertFalse(mod.current_config().enable_shorts)
            self.assertEqual(mod.hl_mode(), "dry")
            # 页面上「管理」卡照实反映生效模式（只读徽章）
            r = c.get("/admin", params={"k": TOKEN})
            self.assertEqual(r.status_code, 200)
            self.assertIn("影子", r.text)

    def test_equity_mode_db_overrides_env(self):
        """Config 现在能覆盖 equity_mode / equity_min（面板资金组要真的生效）。"""
        mod = self._mod()
        mod.db.set_config("equity_mode", "fixed")
        mod.db.set_config("equity_min", 55.0)
        self.assertEqual(mod.current_config().equity_mode, "fixed")
        self.assertEqual(mod.current_config().equity_min, 55.0)
        self.assertEqual(mod.hl_params().equity_mode, "fixed")

    def test_confirm_page_untouched(self):
        mod = self._mod()
        pid = mod.db.create_position(symbol="BTC_USDT", signal_bar_ms=1_700_000_200_000,
                                     status="open", side=1, entry_price=100_000.0,
                                     stop_price=97_000.0, stop_dist=0.03, contracts=6,
                                     coin_qty=0.0006, notional=60.0, risk_u=18.0,
                                     equity_at_entry=1000.0)
        tok = mod.engine.sign_token(mod.config.token_secret, pid, "view")
        with TestClient(mod.app) as c:
            r = c.get(f"/c/{pid}", params={"a": "view", "t": tok})
            self.assertEqual(r.status_code, 200)
            self.assertIn("持仓中", r.text)
            # P3 命名同步：确认页 title 用「双均线交易台」，工程名/旧面板头不再出现
            self.assertIn("双均线交易台", r.text)
            self.assertNotIn("crypto-ma-alert", r.text)

    def test_health_still_reports_hl_mode(self):
        # 无 k 的 /health 不再暴露 hl_mode/hl_kill（公网站点）；带 k 才给
        mod = self._mod()
        tok = mod.config.admin_token
        with TestClient(mod.app) as c:
            j = c.get("/health").json()
            self.assertEqual(j["status"], "ok")
            self.assertNotIn("hl_mode", j)
            self.assertNotIn("hl_kill", j)
            j2 = c.get(f"/health?k={tok}").json()
            self.assertEqual(j2["status"], "ok")
            self.assertIn("hl_mode", j2)
            self.assertIn("hl_kill", j2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
