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
        for p in ("/health", "/admin", "/admin/rules", "/admin/logs", "/c/{pid}"):
            self.assertIn(p, paths, f"路由 {p} 未注册")
        # HL 监控面板（feat/hl-executor）也要在
        for p in ("/hl", "/api/hl/status"):
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
            self.assertEqual((m["name"], m["short_name"]), ("双均线", "双均线"))
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

    def test_edit_pages_summary_and_correction(self):
        """回归：已确认/已平仓的仓位访问确认页应给只读摘要 + 修改按钮；显式修改能改正并重算 R。"""
        try:
            import fastapi                                            # noqa: F401
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        pid = mod.db.create_position(symbol="BTC_USDT", signal_bar_ms=1_700_000_200_000,
                                     status="open", side=1, entry_price=100_000.0,
                                     stop_price=97_000.0, stop_dist=0.03, contracts=6,
                                     coin_qty=0.0006, notional=60.0, risk_u=18.0,
                                     equity_at_entry=1000.0)
        mod.db.confirm_position(pid, 100_000.0, 6, 97_000.0, True)
        mod.db.close_position(pid, 103_000.0, "ma20", auto=True)
        sec = mod.config.token_secret
        with TestClient(mod.app) as client:
            # a=view → 只读摘要（含状态与两个修改入口），不是空白表单
            r = client.get(f"/c/{pid}", params={"a": "view", "t": mod.engine.sign_token(sec, pid, "view")})
            self.assertEqual(r.status_code, 200)
            self.assertIn("已平仓", r.text)
            self.assertIn("修改开仓记录", r.text)
            self.assertIn("修改平仓数据", r.text)
            self.assertNotIn("按以上数值确认开仓", r.text)
            # a=edit_close → 必须是平仓表单（含当前记录提示与预填离场价），不能又回到摘要页
            r = client.get(f"/c/{pid}", params={"a": "edit_close",
                                                "t": mod.engine.sign_token(sec, pid, "edit_close")})
            self.assertEqual(r.status_code, 200)
            self.assertIn("当前记录", r.text)
            self.assertIn("103000.00", r.text)
            # a=edit_close 表单 → 提交后更新离场价并重算 R
            r = client.post(f"/c/{pid}", data={"a": "close", "edit": "1",
                                               "t": mod.engine.sign_token(sec, pid, "close"),
                                               "exit_price": "104000", "exit_reason": "ma20"})
            self.assertEqual(r.status_code, 200)
            self.assertAlmostEqual(mod.db.position_by_id(pid)["r_multiple"], (104_000 - 100_000) / 3_000)
            # a=edit_open → 修改入场价，R 按新入场价重算，状态仍是 closed（不能被改回 open）
            r = client.post(f"/c/{pid}", data={"a": "open", "edit": "1",
                                               "t": mod.engine.sign_token(sec, pid, "open"),
                                               "actual_entry": "101000", "actual_stop": "98000",
                                               "actual_contracts": "6"})
            self.assertEqual(r.status_code, 200)
            row = mod.db.position_by_id(pid)
            self.assertEqual(row["status"], "closed")
            self.assertAlmostEqual(row["actual_entry"], 101_000.0)
            self.assertAlmostEqual(row["r_multiple"], (104_000 - 101_000) / 3_000)
            self.assertEqual(row["auto_closed"], 0, "用户改过之后应视为已确认")
            # 已录入完毕（auto_closed=0）→ 提醒消息里那条 a=close 链接应给只读摘要，不是表单
            r = client.get(f"/c/{pid}", params={"a": "close",
                                                "t": mod.engine.sign_token(sec, pid, "close")})
            self.assertEqual(r.status_code, 200)
            self.assertIn("修改平仓数据", r.text)
            self.assertNotIn("name='exit_price'", r.text)
            # 而 a=edit_close 仍必须是表单，否则改不了
            r = client.get(f"/c/{pid}", params={"a": "edit_close",
                                                "t": mod.engine.sign_token(sec, pid, "edit_close")})
            self.assertIn("name='exit_price'", r.text)

    def test_auto_closed_position_accepts_real_fill_once(self):
        """回归：系统自动按参考价关仓后，用户必须能补录真实成交价（覆盖一次），之后再提交才拒绝。"""
        try:
            import fastapi                                            # noqa: F401
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        pid = mod.db.create_position(symbol="BTC_USDT", signal_bar_ms=1_700_000_100_000,
                                     status="open", side=-1, entry_price=100_000.0,
                                     stop_price=103_000.0, stop_dist=0.03, contracts=6,
                                     coin_qty=0.0006, notional=60.0, risk_u=18.0,
                                     equity_at_entry=1000.0)
        mod.db.confirm_position(pid, 100_000.0, 6, 103_000.0, True)
        # 引擎自动关仓（参考价），auto_closed=1
        mod.db.close_position(pid, 99_000.0, "ma20", auto=True)
        self.assertEqual(mod.db.position_by_id(pid)["auto_closed"], 1)
        tok = mod.engine.sign_token(mod.config.token_secret, pid, "close")
        with TestClient(mod.app) as client:
            r = client.post(f"/c/{pid}", data={"a": "close", "t": tok,
                                               "exit_price": "98500", "exit_reason": "ma20"})
            self.assertEqual(r.status_code, 200, "自动关仓后应允许补录真实成交价")
            row = mod.db.position_by_id(pid)
            self.assertAlmostEqual(row["exit_price"], 98_500.0)
            self.assertAlmostEqual(row["r_multiple"], (100_000 - 98_500) / 3_000)
            self.assertEqual(row["auto_closed"], 0, "用户确认后应转为已确认状态")
            # 再提交一次必须拒绝，且不覆盖已确认的成交价
            r = client.post(f"/c/{pid}", data={"a": "close", "t": tok,
                                               "exit_price": "97000", "exit_reason": "ma20"})
            self.assertEqual(r.status_code, 400)
            self.assertAlmostEqual(mod.db.position_by_id(pid)["exit_price"], 98_500.0)

    def test_confirm_actions_reject_wrong_status(self):
        """回归：确认动作必须带状态守卫——closed 的仓位不能被重复平仓/确认/忽略。

        旧实现对任意状态都照单全收，重放旧页面即可覆盖 exit_price / r_multiple 污染统计。
        """
        try:
            import fastapi                                            # noqa: F401
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        pid = mod.db.create_position(symbol="BTC_USDT", signal_bar_ms=1_700_000_000_000,
                                     status="pending", side=-1, entry_price=100_000.0,
                                     stop_price=103_000.0, stop_dist=0.03, contracts=6,
                                     coin_qty=0.0006, notional=60.0, risk_u=18.0,
                                     equity_at_entry=1000.0)
        tok = mod.engine.sign_token(mod.config.token_secret, pid, "close")
        with TestClient(mod.app) as client:
            # pending → close 一次合法
            r = client.post(f"/c/{pid}", data={"a": "close", "t": tok,
                                               "exit_price": "95000", "exit_reason": "ma20"})
            self.assertEqual(r.status_code, 200)
            row = mod.db.position_by_id(pid)
            self.assertAlmostEqual(row["r_multiple"], (100_000 - 95_000) / 3_000)
            # closed 后重复 close / open / ignore 都必须 400，且原 r_multiple 不被覆盖
            r = client.post(f"/c/{pid}", data={"a": "close", "t": tok,
                                               "exit_price": "99000", "exit_reason": "ma20"})
            self.assertEqual(r.status_code, 400, "closed 仓位不能重复平仓")
            row = mod.db.position_by_id(pid)
            self.assertAlmostEqual(row["r_multiple"], (100_000 - 95_000) / 3_000)
            tok_open = mod.engine.sign_token(mod.config.token_secret, pid, "open")
            r = client.post(f"/c/{pid}", data={"a": "open", "t": tok_open,
                                               "actual_entry": "100000",
                                               "actual_stop": "103000",
                                               "actual_contracts": "6"})
            self.assertEqual(r.status_code, 400, "非 pending 仓位不能确认开仓")
            self.assertEqual(mod.db.position_by_id(pid)["status"], "closed")
            tok_ign = mod.engine.sign_token(mod.config.token_secret, pid, "ignore")
            r = client.post(f"/c/{pid}", data={"a": "ignore", "t": tok_ign})
            self.assertEqual(r.status_code, 400, "closed 仓位不能忽略")
            self.assertEqual(mod.db.position_by_id(pid)["status"], "closed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
