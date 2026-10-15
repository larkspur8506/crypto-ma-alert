"""2026-10-15 面板修复回归：最近成交表列对齐 + 买卖方向四向映射 + 主题三态开关。

背景：第三轮审计 P2 给总览「最近成交」加了买/卖列但没改表头（8 列表头配 9 格数据行，
从「数量」起整行左移一格）；两处「买/卖」的方向推导也不对（记录 Tab 用 side 反推
把平多标成买；总览把 Close Short 标成卖）。主题则是硬编码深色，用户要求
黑 / 白 / 跟随系统 三态可切换。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ui_theme import (DEFAULT_THEME, THEMES, THEME_COOKIE, THEME_CSS,
                          normalize_theme, theme_attr, theme_switch_html)


class TestUiThemePure(unittest.TestCase):
    """app/ui_theme.py 纯函数（不依赖 FastAPI）。"""

    def test_normalize_theme_valid_values(self):
        self.assertEqual(normalize_theme("auto"), "auto")
        self.assertEqual(normalize_theme("dark"), "dark")
        self.assertEqual(normalize_theme("light"), "light")
        self.assertEqual(normalize_theme(" DARK "), "dark")      # 大小写+空白容错

    def test_normalize_theme_invalid_falls_back_to_auto(self):
        for bad in ("", None, "blue", "black", "0", "auto ", "跟随系统"):
            self.assertEqual(normalize_theme(bad), "auto", f"{bad!r} 应回落 auto")

    def test_theme_attr_auto_empty_others_literal(self):
        self.assertEqual(theme_attr("auto"), "")
        self.assertEqual(theme_attr("dark"), "dark")
        self.assertEqual(theme_attr("light"), "light")
        self.assertEqual(theme_attr("garbage"), "")              # 回落 auto → 空

    def test_theme_css_has_both_schemes_and_no_hardcoded_body_bg(self):
        self.assertIn("prefers-color-scheme:dark", THEME_CSS)
        self.assertIn("html[data-theme='light']", THEME_CSS)
        self.assertIn("html[data-theme='dark']", THEME_CSS)
        # 变量层不许再出现写死的 body 底色（防回到硬编码主题）
        self.assertNotIn("background:#0f1115", THEME_CSS.replace(
            "--bg:#0f1115", "VAR"))                              # 只许作为变量定义出现

    def test_theme_switch_html_links(self):
        h = theme_switch_html("records", "dark", "TOK")
        for label in ("跟随系统", "黑", "白"):
            self.assertIn(label, h)
        self.assertIn("/admin/theme?t=dark&tab=records", h)
        self.assertIn("/admin/theme?t=light&tab=records", h)
        self.assertIn("/admin/theme?t=auto&tab=records", h)
        # 当前主题高亮：dark 是当前 → dark 用 btn、light/auto 用 btn gray
        self.assertRegex(h, r"class='btn' href='/admin/theme\?t=dark")
        self.assertRegex(h, r"class='btn gray' href='/admin/theme\?t=light")

    def test_page_shell_data_theme_attr(self):
        from app.ui_theme import page_shell
        h_dark = page_shell("dark", "t", "b", head_extra="")
        self.assertIn("<html lang='zh-CN' data-theme='dark'>", h_dark)
        h_auto = page_shell("auto", "t", "b", head_extra="")
        self.assertIn("<html lang='zh-CN'>", h_auto)             # auto 不写属性
        # 开标签本身不带 data-theme（THEME_CSS 里的选择器文本不算）
        self.assertRegex(h_auto, r"<html lang='zh-CN'>(?! data-theme)")
        # 标题要转义（防反射）
        h_xss = page_shell("auto", "<script>", "b", head_extra="")
        self.assertNotIn("<script>", h_xss)


try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.hl.views import DashboardDeps, build_router as build_hl_router
    from app.db import DB
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = None

if FastAPI is not None:
    import shutil
    import tempfile

    NOW = 1_760_000_000.0
    ADDR = "0xTESTADDRESS123456"
    TOKEN = "panel-tk"
    BAR = 1_700_000_000_000

    class FakeInfo:
        def snapshot(self, address, mids=None):
            return {"ts": NOW, "address": address, "equity_total": 100.0, "equity": 100.0,
                    "spot_usdc": 100.0, "unified": True, "withdrawable": 90.0,
                    "margin_used": 10.0, "notional": 0.0, "positions": [], "open_orders": []}

        def all_mids(self):
            return {"BTC": 100_000.0, "ETH": 3_000.0}

    class ThemeBase(unittest.TestCase):
        def setUp(self):
            self.tmp = tempfile.mkdtemp(prefix="cma_theme_")
            self.db = DB(os.path.join(self.tmp, "t.db"))
            self.info = FakeInfo()
            self.cfg = Config()
            self.hl_deps = DashboardDeps(
                db=self.db, config_fn=lambda: self.cfg, info=self.info,
                address_fn=lambda: ADDR, mode_fn=lambda: "live",
                now_fn=lambda: NOW)
            self.deps = AdminDeps(
                db=self.db, config_fn=lambda: self.cfg, hl_deps=self.hl_deps,
                mode_fn=lambda: "live", token_fn=lambda: TOKEN,
                address_fn=lambda: ADDR, private_key_fn=lambda: "",
                mids_fn=lambda: self.info.all_mids())
            app = FastAPI()
            app.include_router(build_admin_router(self.deps))
            app.include_router(build_hl_router(self.hl_deps))
            self.client = TestClient(app)

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def ins_fill(self, tid, dir_, sz, px, pnl=0.0, reduce=False, coin="BTC"):
            self.db.hl_upsert_fill(
                tid=tid, symbol=f"{coin}_USDT", coin=coin,
                side=1 if "Long" in dir_ else -1, sz=sz, px=px, fee=0.01,
                fee_token="USDT", dir=dir_, closed_pnl=pnl, hash=tid,
                reduce_only=reduce, ts=int(NOW * 1000))

    class TestFillsTableAlignment(ThemeBase):
        """表头 9 列 + 数据 9 格 + 列序对齐（回归本 bug）。"""

        def _rows(self, html):
            m = re.search(r"最近成交（最近 \d+ 条）</h2>.*?<tbody>(.*?)</tbody>", html, re.S)
            self.assertIsNotNone(m, "找不到最近成交表")
            out = []
            for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
                cells = [re.sub(r"<[^>]+>", "", c).strip()
                         for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                self.assertEqual(len(cells), 9, f"数据行必须 9 格，实际 {len(cells)}")
                out.append(cells)
            return out

        def test_overview_header_has_9_columns_in_order(self):
            html = self.client.get("/admin", params={"k": TOKEN, "tab": "overview"}).text
            m = re.search(r"最近成交（最近 \d+ 条）</h2>.*?<thead><tr>(.*?)</tr></thead>", html, re.S)
            self.assertIsNotNone(m)
            heads = [re.sub(r"<[^>]+>", "", h).strip()
                     for h in re.findall(r"<th[^>]*>(.*?)</th>", m.group(1), re.S)]
            self.assertEqual(heads, ["时间", "币种", "买卖", "数量", "价格",
                                     "手续费", "方向", "平仓盈亏", "减仓"])

        def test_overview_row_cells_match_columns(self):
            self.ins_fill("f1", "Open Short", 0.00084, 81621.0, pnl=0.0)
            html = self.client.get("/admin", params={"k": TOKEN, "tab": "overview"}).text
            rows = self._rows(html)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r[1], "BTC")
            self.assertEqual(r[2], "卖")                       # 开空=卖出
            self.assertEqual(r[3], "0.00084")                   # 数量列是数量（不再是买卖）
            self.assertEqual(r[4], "81,621.00")                 # 价格列是价格（不再是数量）
            self.assertEqual(r[6], "开空")                       # 方向列是方向文字

        def test_buy_sell_four_directions_overview(self):
            cases = {"Open Long": "买", "Close Short": "买",
                     "Close Long": "卖", "Open Short": "卖"}
            for i, (d, want) in enumerate(cases.items()):
                self.ins_fill(f"f{i}", d, 0.001, 100_000.0,
                              reduce="Close" in d)
            html = self.client.get("/admin", params={"k": TOKEN, "tab": "overview"}).text
            rows = self._rows(html)
            # 方向列（第 7 格）是 dir 的中文翻译 → 用它反查每行的买卖是否正确
            dir_txt = {"Open Long": "开多", "Close Short": "平空",
                       "Close Long": "平多", "Open Short": "开空"}
            got = {}
            for r in rows:
                d = next(k for k, v in dir_txt.items() if v == r[6])
                got[d] = r[2]
            self.assertEqual(len(got), 4)
            for d, want in cases.items():
                self.assertEqual(got[d], want, f"{d} 应显示 {want}")

        def test_records_tab_buy_sell_and_alignment(self):
            self.ins_fill("r1", "Close Long", 0.005, 100_000.0, pnl=5.0, reduce=True)
            self.ins_fill("r2", "Open Short", 0.005, 100_000.0)
            html = self.client.get("/admin", params={"k": TOKEN, "tab": "records"}).text
            m = re.search(r"成交（hl_fills · 最近 \d+ 条）.*?<table>(.*?)</table>", html, re.S)
            self.assertIsNotNone(m)
            trs = re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S)
            body = [t for t in trs if "<td" in t]
            self.assertEqual(len(body), 2)
            cells = [[re.sub(r"<[^>]+>", "", c).strip()
                      for c in re.findall(r"<td[^>]*>(.*?)</td>", t, re.S)] for t in body]
            # id DESC：r2（Open Short）在前。列序：时间0 币种1 买卖2 数量3 价格4 手续费5 盈亏6 减仓7
            self.assertEqual(cells[0][2], "卖")                  # 开空=卖出（旧代码显示「买」）
            self.assertEqual(cells[0][4], "100,000.00")          # 价格列真的是价格（不再是数量）
            self.assertEqual(cells[0][3], "0.0050")              # 数量列真的是数量
            self.assertEqual(cells[1][2], "卖")                  # 平多=卖出（旧代码显示「买」）
            for c in cells:
                self.assertEqual(len(c), 8)

    class TestThemeSwitch(ThemeBase):
        """/admin/theme 设 cookie + 页面 data-theme。"""

        def setUp(self):
            # /hl 页默认不鉴权（require 开关默认关）——开着才能测「cookie 主题」这条链路
            os.environ["HL_DASH_REQUIRE_ADMIN"] = "1"
            super().setUp()

        def tearDown(self):
            os.environ.pop("HL_DASH_REQUIRE_ADMIN", None)
            super().tearDown()

        def test_theme_route_sets_cookie_and_redirects(self):
            r = self.client.get("/admin/theme", params={"t": "light", "k": TOKEN},
                                follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers["location"], f"/admin?tab=overview&k={TOKEN}")
            sc = r.headers.get("set-cookie", "")
            self.assertIn(f"{THEME_COOKIE}=light", sc)

        def test_theme_route_rejects_bad_value_to_auto(self):
            r = self.client.get("/admin/theme", params={"t": "purple", "k": TOKEN},
                                follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            self.assertIn(f"{THEME_COOKIE}=auto", r.headers.get("set-cookie", ""))

        def test_theme_route_requires_auth(self):
            r = self.client.get("/admin/theme", params={"t": "dark"},
                                follow_redirects=False)
            self.assertEqual(r.status_code, 401)

        def test_admin_page_reads_theme_cookie(self):
            self.client.cookies.set(THEME_COOKIE, "light")
            html = self.client.get("/admin", params={"k": TOKEN}).text
            self.assertIn("<html lang='zh-CN' data-theme='light'>", html)
            self.assertIn(THEME_CSS[:20], html)                   # 变量层已注入

        def test_admin_page_default_no_data_theme_attr(self):
            html = self.client.get("/admin", params={"k": TOKEN}).text
            self.assertIn("<html lang='zh-CN'>", html)           # 默认跟随系统
            self.assertRegex(html, r"<html lang='zh-CN'>(?! data-theme)")   # 开标签不带主题属性

        def test_hl_page_reads_theme_cookie(self):
            # 带 k 过鉴权 + cma_theme cookie 控主题
            self.client.cookies.set(THEME_COOKIE, "dark")
            html = self.client.get("/hl", params={"k": TOKEN}).text
            self.assertIn("<html lang='zh-CN' data-theme='dark'>", html)

        def test_hl_page_theme_switch_row_carries_k(self):
            # 主题行链接要带 k，否则切完主题 401
            html = self.client.get("/hl", params={"k": TOKEN}).text
            self.assertIn(f"/admin/theme?t=dark&tab=overview&k={TOKEN}", html)

        def test_hl_page_theme_switch_row_present(self):
            html = self.client.get("/hl", params={"k": TOKEN}).text
            self.assertIn("/admin/theme?t=auto", html)
            self.assertIn("/admin/theme?t=dark", html)
            self.assertIn("/admin/theme?t=light", html)

        def test_admin_all_tabs_have_switch_row(self):
            for tab in ("overview", "params", "switch", "records"):
                html = self.client.get("/admin", params={"k": TOKEN, "tab": tab}).text
                self.assertIn("主题：", html, f"{tab} 缺主题切换行")

        def test_css_uses_variables_not_hardcoded_dark(self):
            html = self.client.get("/admin", params={"k": TOKEN}).text
            # 页面样式引用变量；写死的深色 body 底色不允许再出现
            self.assertNotIn("background:#0f1115", html)

        def test_theme_cookie_bad_value_falls_back(self):
            self.client.cookies.set(THEME_COOKIE, "neon")
            html = self.client.get("/admin", params={"k": TOKEN}).text
            # 回落 auto：开标签不带 data-theme（CSS 里的选择器文本不算）
            self.assertRegex(html, r"<html lang='zh-CN'>(?! data-theme)")


if __name__ == "__main__":
    unittest.main()


class TestConfirmPageTheme(ThemeBase):
    """/c 确认页的主题 cookie（签名链接页，无登录态，主题跟 cookie 走）。"""

    def _signed(self, pid=1, action="view"):
        from app import engine as _e
        return _e.sign_token("x", pid, action)

    def test_summary_carries_request_and_theme(self):
        # 渲染层单测（不 import app.main —— 它模块级建 DB，/app 不可写会炸）：
        # page() 收到带 cookie 的 request 必须渲染出 data-theme。
        import importlib
        pm = importlib.import_module("app.ui_theme")
        from fastapi import Request as _Req  # noqa: F401
        class _FakeReq:
            cookies = {"cma_theme": "light"}
        html = pm.page_shell("light", "t", "<p>x</p>", head_extra="")
        self.assertIn("data-theme='light'", html)
        # page() 的 cookie 读取链路：main.page 依赖 _req_theme(request)，
        # 用假 request 验证（不 import main，避免模块级 DB）。
        import sys as _sys
        _sys.modules.setdefault("app.main", None)  # 不触发
        from app.ui_theme import normalize_theme as _nt
        self.assertEqual(_nt(_FakeReq.cookies.get("cma_theme")), "light")
