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
        # 关键路由已注册
        paths = {r.path for r in mod.app.routes}
        for p in ("/health", "/admin", "/admin/rules", "/admin/logs", "/c/{pid}"):
            self.assertIn(p, paths, f"路由 {p} 未注册")

    def test_config_db_override_path(self):
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装")
        mod = importlib.import_module("app.main")
        mod.db.set_config("equity", 2000)
        self.assertEqual(mod.current_config().equity, 2000.0)

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
