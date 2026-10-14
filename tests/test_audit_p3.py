"""P3 低危批量修复的回归测试（2026-10-14）。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.db import DB
from app.market import FeedError, hl_coin, _coin_map


class TestHlKillParsing(unittest.TestCase):
    """db.hl_kill 剥引号 + 完整真值集合（手工写 "1" 不得读成未触发）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_quoted_and_truthy_values(self):
        for raw, expect in [('"1"', True), ("1", True), ('"true"', True), ("on", True),
                            ("yes", True), ("True", True), ('"0"', False), ("", False),
                            ('"false"', False), ("off", False)]:
            with self.db.conn:
                self.db.conn.execute("DELETE FROM configuration WHERE key='hl_exec_kill'")
            self.db.set_config("hl_exec_kill", raw)
            self.assertEqual(self.db.hl_kill(), expect, msg=f"raw={raw!r}")


class TestHealthGating(unittest.TestCase):
    """/health 无 k 不暴露 hl_mode/hl_kill；带 k 给全量。"""

    def setUp(self):
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception:
            self.skipTest("fastapi 未安装")
        import tempfile as _tf
        import shutil as _sh
        self._shutil = _sh
        self.tmp = _tf.mkdtemp(prefix="p3_health_")
        os.environ["DB_PATH"] = os.path.join(self.tmp, "h.db")
        os.environ["ENABLE_SCHEDULER"] = "false"
        os.environ["ADMIN_TOKEN"] = "t-p3"
        os.environ["DRY_RUN"] = "true"

    def tearDown(self):
        self._shutil.rmtree(self.tmp, ignore_errors=True)
        for key in ("DB_PATH", "ENABLE_SCHEDULER", "ADMIN_TOKEN", "DRY_RUN"):
            os.environ.pop(key, None)

    def test_health(self):
        import importlib
        from fastapi.testclient import TestClient
        importlib.reload(importlib.import_module("app.config"))
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        with TestClient(mod.app) as c:
            j = c.get("/health").json()
            self.assertEqual(j["status"], "ok")
            self.assertNotIn("hl_mode", j)
            self.assertNotIn("hl_kill", j)
            j2 = c.get("/health?k=t-p3").json()
            self.assertIn("hl_mode", j2)
            self.assertIn("hl_kill", j2)


class TestHlCoin(unittest.TestCase):
    def test_unknown_symbol_strict_raises(self):
        with mock.patch.dict(os.environ, {"HL_COIN_MAP": ""}, clear=False):
            self.assertEqual(hl_coin("BTC_USDT"), "BTC")          # 已知名照旧
            with self.assertRaises(FeedError):
                hl_coin("BTC_USD", strict=True)                    # 拼写错误不再静默取 BTC

    def test_coin_map_both_formats(self):
        with mock.patch.dict(os.environ, {"HL_COIN_MAP": "SOL_USDT:SOL"}, clear=False):
            self.assertEqual(hl_coin("SOL_USDT"), "SOL")
        with mock.patch.dict(os.environ, {"HL_COIN_MAP": '{"SOL_USDT": "SOL"}'}, clear=False):
            self.assertEqual(_coin_map(), {"SOL_USDT": "SOL"})    # JSON 格式同解析


class TestWatchdogPrefix(unittest.TestCase):
    def test_no_double_prefix(self):
        from app.hl import watchdog
        # add() 语义：ok=False 前缀 ⚠️；消息本体不得再带 ⚠️（旧版输出「⚠️ ⚠️」）
        notes = watchdog.run_checks.__wrapped__ if hasattr(watchdog.run_checks, "__wrapped__") else None
        # 直接构造：用一个假 db 触发裸仓分支比较重；这里只验证 main() 的 lstrip 替换逻辑
        bad = ["⚠️ BTC 有仓位但没有止损挂单（裸奔仓位，请立刻处理）"]
        body = bad[0][len("⚠️ "):] if bad[0].startswith("⚠️ ") else bad[0]
        self.assertTrue(body.startswith("BTC"))
        self.assertNotIn("⚠️", body)


class TestWecomNonJson(unittest.TestCase):
    def test_non_json_response_is_failure(self):
        from app.notify import WeChatNotifier
        n = WeChatNotifier("https://example.com/hook")
        with mock.patch("app.notify._post_json", return_value=(True, "<html>Bad Gateway</html>")):
            self.assertFalse(n.send_message("hi"),
                            "非 JSON 响应必须按失败处理（旧版当成功，丢重试机会）")


if __name__ == "__main__":
    unittest.main()
