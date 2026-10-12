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


if __name__ == "__main__":
    unittest.main(verbosity=2)
