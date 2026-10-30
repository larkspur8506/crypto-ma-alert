"""面板 P4 收尾轮的回归测试（规格：spec-panel-p4.md，2026-10-25）。

**2026-10-30 面板精简（feat/panel-slim）后只剩功能一**：参数页与开关页整体删除，
原「功能二（参数页低频折叠）」「功能三（开关页折叠 + 应急操作卡）」所钉的对象已不存在，
用例随功能删除；「管理」卡（kill / 更新 API / 清除兜底）的回归见
tests/test_admin_ui.py::TestAdminCard。

保留项逐条钉词面（铁律：断言看渲染 HTML；双入口 = /admin 的 ?k= 查询串 与
cma_admin cookie 两条取页面路径，渲染类用例两个入口都断言）：

1. 记录页锚点导航删除：href='#cma- 与 id='cma- 渲染层 0 处，页顶直接是「资金流水」。

全部离线：临时 sqlite + 假 info，不触网（模板：tests/test_panel_p3.py）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NOW = 1760000000.0
TOKEN = "p4-tk"
ADDR = "0xTestAccount000000000000000000000000"

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.db import DB
    from app.hl.views import DashboardDeps
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = DB = None
    DashboardDeps = None


class BaseCase(unittest.TestCase):
    """临时 DB + 同一个 app 挂 /hl 与 /admin 两套路由（模板：tests/test_panel_p3.py）。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="panel_p4_")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.hl_deps = DashboardDeps(
            db=self.db, config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT"]})(),
            info=None, address_fn=lambda: ADDR, mode_fn=lambda: "off",
            now_fn=lambda: NOW)
        self.deps = AdminDeps(
            db=self.db, config_fn=lambda: Config().apply_db(self.db.get_config()),
            hl_deps=self.hl_deps,
            mode_fn=lambda: "off", token_fn=lambda: TOKEN,
            address_fn=lambda: ADDR, private_key_fn=lambda: "",
            cleanup_enabled_fn=lambda: True)
        app = FastAPI()
        app.include_router(build_admin_router(self.deps))
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 取数辅助：双入口 = ?k= 查询串 与 cookie，渲染类用例两个入口都断言 ----
    def admin(self, tab="overview", **params):
        p = {"tab": tab, "k": TOKEN}
        p.update(params)
        return self.client.get("/admin", params=p).text

    def admin_cookie(self, tab):
        """同一页面的第二条入口：token 走 cma_admin cookie（深链/书签场景）。"""
        self.client.cookies.set("cma_admin", TOKEN)
        try:
            return self.client.get("/admin", params={"tab": tab}).text
        finally:
            self.client.cookies.clear()


# ---------------------------------------------------------------- 功能一：锚点删除
class TestRecordAnchorsGone(BaseCase):

    def test_anchor_wordmarks_zero_both_entries(self):
        for html in (self.admin(tab="records"), self.admin_cookie("records")):
            self.assertEqual(html.count("href='#cma-"), 0, "锚点链接 0 处")
            self.assertEqual(html.count("id='cma-"), 0, "区块容器死 id 0 处")
            self.assertNotIn("<a href='#", html, "不残留任何页内跳转链接")
            # 页面顶部直接是「资金流水」卡（资金流水仍在 <h2>记录</h2> 之前）
            self.assertLess(html.index("资金流水"), html.index("<h2>记录</h2>"))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
