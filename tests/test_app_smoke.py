"""应用装配冒烟测试：import app.main 必须成功，且关键路由可调用。

这层专门用来抓「单测覆盖不到、只在装配时才炸」的问题
（例：DB() 少传 path 参数 → 容器启动即崩，而单测全绿）。
需要 fastapi/apscheduler 已安装（CI 里先 pip install -r requirements.txt）。
"""
import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAppAssembly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DB_PATH"] = os.path.join(self.tmp, "smoke.db")
        os.environ["ENABLE_SCHEDULER"] = "false"
        os.environ["ADMIN_TOKEN"] = "smoke-admin"
        os.environ["TOKEN_SECRET"] = "smoke-secret"
        os.environ["BASE_URL"] = "https://smoke.example"
        os.environ["DRY_RUN"] = "true"

    def test_import_and_routes(self):
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        h = mod.health()
        self.assertEqual(h["status"], "ok")
        self.assertIn("BTC_USDT", h["symbols"])
        # 关键路由已注册。用 OpenAPI schema 取路径：跨 FastAPI 版本最稳，
        # 也顺带验证 include_router 进来的子路由确实生效（早期版本 app.routes 里是嵌套对象）。
        paths = self._route_paths(mod.app)
        for p in ("/health", "/admin", "/admin/rules", "/admin/logs"):
            self.assertIn(p, paths, f"路由 {p} 未注册")
        # 2026-10-30 PR B：人工确认页退役 —— 路由必须真的不在（防止有人「顺手加回」）
        self.assertNotIn("/c/{pid}", paths, "确认页已删除，不该再注册")
        # HL 监控面板（feat/hl-executor）也要在
        for p in ("/hl", "/api/hl/status", "/api/hl/why"):
            self.assertIn(p, paths, f"HL 路由 {p} 未注册")
        # 到期保护：清空手工兜底的显式入口
        self.assertIn("/admin/hl/agent/fallback/clear", paths)

    def test_home_screen_icon_routes(self):
        """主屏图标 / favicon / manifest：五条路由注册 + 真的能取到字节。"""
        try:
            import fastapi                                            # noqa: F401
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        paths = self._route_paths(mod.app)
        for p in ("/apple-touch-icon.png", "/favicon.ico", "/icon-192.png",
                  "/icon-512.png", "/site.webmanifest"):
            self.assertIn(p, paths, f"图标路由 {p} 未注册")
        expect = {
            "/apple-touch-icon.png": "image/png",
            "/icon-192.png": "image/png",
            "/icon-512.png": "image/png",
            "/favicon.ico": "image/x-icon",
            "/site.webmanifest": "application/manifest+json",
        }
        with TestClient(mod.app) as c:
            for p, ctype in expect.items():
                r = c.get(p)
                self.assertEqual(r.status_code, 200, f"{p} 应 200")
                self.assertEqual(r.headers["content-type"], ctype, p)
                self.assertTrue(r.content, f"{p} 返回空内容")
                # 图标缓存一周（反代/CDN 不每次回源）
                self.assertEqual(r.headers.get("cache-control"), "public, max-age=604800", p)
            # manifest 内容要和静态文件一致（name/short_name/颜色/两个 any 图标）
            m = c.get("/site.webmanifest").json()
            self.assertEqual((m["name"], m["short_name"]), ("双均线交易台", "双均线交易台"))
            self.assertEqual(m["start_url"], "/")
            self.assertEqual(m["display"], "browser")
            self.assertEqual((m["background_color"], m["theme_color"]), ("#0d1117", "#0d1117"))
            self.assertEqual(sorted(i["sizes"] for i in m["icons"]), ["192x192", "512x512"])
            self.assertTrue(all(i["purpose"] == "any" for i in m["icons"]))

    def test_agent_check_passes_mode_and_handles_off(self):
        """装配级：_agent_check 必须把当前模式传下去，off 下不喊「清仓失败」。"""
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        seen = {}

        class Info:
            def extra_agents(self, address):
                return [{"name": "Hermes", "address": "0xabc",
                         "validUntil": int(__import__("time").time() * 1000) + 86_400_000}]
        mod.get_hl_info = lambda: Info()
        mod.db.set_config("hl_exec_mode", "off")

        def fake_check(db, cfg, notifier, info, **kw):
            seen.update(kw)
            return {"level": "cleanup", "source": "api"}

        from app.hl import agent as agent_mod
        orig = agent_mod.check_once
        agent_mod.check_once = fake_check
        try:
            mod._agent_check()
        finally:
            agent_mod.check_once = orig
        self.assertEqual(seen.get("mode"), "off", "_agent_check 必须把实际模式传下去")
        self.assertTrue(callable(seen.get("cleanup")), "清仓器必须接线")

    @staticmethod
    def _route_paths(app) -> set:
        return set(app.openapi().get("paths", {}).keys())

    def test_heartbeat_is_silent_beacon_by_default(self):
        """心跳默认只落库不推送（避免与每日巡检重复），但必须留下 kind='heartbeat_local' 供看守判断。"""
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        pushed = []

        class Boom:
            def send_message(self, text):
                pushed.append(text)
                return True

        mod.notifier = Boom()
        mod.db.set_config("heartbeat_push", 0)
        mod._job_heartbeat()
        self.assertEqual(pushed, [], "默认不应推送心跳")
        kinds = [r["kind"] for r in mod.db.recent_alerts(5)]
        self.assertIn("heartbeat_local", kinds)
        # 打开开关后应恢复推送
        mod.db.set_config("heartbeat_push", 1)
        mod._job_heartbeat()
        self.assertEqual(len(pushed), 1, "开关打开后应推送")
        self.assertIn("heartbeat", [r["kind"] for r in mod.db.recent_alerts(5)])

    def test_config_db_override_path(self):
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装")
        mod = importlib.import_module("app.main")
        mod.db.set_config("equity", 2000)
        self.assertEqual(mod.current_config().equity, 2000.0)

# 2026-10-30 PR B 移除的用例（全部钉 POST/GET /c/{pid} 的行为）：
#   - test_edit_pages_summary_and_correction（摘要页 + 修改开/平仓表单）
#   - test_auto_closed_position_accepts_real_fill_once（自动关仓后补录真实成交价）
#   - test_confirm_actions_reject_wrong_status（确认动作的状态守卫）
# 确认页与 confirm_post 已删，这些路径不存在了；记录/核对一律走面板「记录」页的 hl_fills。
# 装配层保留上面的「/c/{pid} 不许再注册」反向断言作回归。


if __name__ == "__main__":
    unittest.main(verbosity=2)
