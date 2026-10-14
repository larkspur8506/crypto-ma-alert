"""一次性私钥录入页（app/hl/keyform.py）的单元测试。

重点保证四件事：
1. 令牌一次性、会过期、库里只存哈希
2. 私钥**不回显、不落库**（响应体里绝不能出现私钥本体，DB 里也不能）
3. 格式不对 / 已存在时不写文件（且不动原文件）
4. 写入成功会备份、且只写那一个专用 env 文件
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.db import DB
from app.hl import keyform
from app.hl.keyform import KeyFormDeps, build_router

KEY = "0x" + "ab" * 32
KEY2 = "0x" + "cd" * 32
PLACEHOLDER = "# HL_PRIVATE_KEY=\nHBAR=x\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.env_path = str(Path(self.tmp.name) / "hl-secrets.env")
        Path(self.env_path).write_text(PLACEHOLDER)
        self.deps = KeyFormDeps(db=self.db, env_path=self.env_path)

    def tearDown(self):
        self.tmp.cleanup()

    def read_env(self):
        return Path(self.env_path).read_text()


class TestToken(Base):
    def test_issue_check_consume(self):
        tok, exp = keyform.issue_token(self.db, ttl_minutes=15)
        self.assertTrue(exp > 0)
        self.assertEqual(keyform.check_token(self.db, tok), (True, ""))
        keyform.consume_token(self.db)
        ok, why = keyform.check_token(self.db, tok)
        self.assertFalse(ok)
        self.assertIn("没有待用", why)

    def test_token_stored_hashed_not_plain(self):
        tok, _ = keyform.issue_token(self.db)
        raw = str(self.db.get_config().get(keyform.CFG_HASH))
        self.assertNotIn(tok, raw)                     # 库里不该有明文
        self.assertIn(keyform._hash(tok), raw)

    def test_wrong_and_missing_token(self):
        keyform.issue_token(self.db)
        self.assertFalse(keyform.check_token(self.db, "wrong")[0])
        self.assertFalse(keyform.check_token(self.db, "")[0])
        self.assertFalse(keyform.check_token(self.db, None)[0])

    def test_expired_token(self):
        tok, _ = keyform.issue_token(self.db, ttl_minutes=-1)
        ok, why = keyform.check_token(self.db, tok)
        self.assertFalse(ok)
        self.assertIn("过期", why)

    def test_state_reports_expiry(self):
        keyform.issue_token(self.db, ttl_minutes=10)
        st = keyform.token_state(self.db)
        self.assertTrue(st["issued"])
        self.assertGreater(st["expires_in_s"], 0)
        keyform.consume_token(self.db)
        self.assertFalse(keyform.token_state(self.db)["issued"])


class TestWriteFile(Base):
    def test_write_ok_and_backup(self):
        ok, info = keyform.write_key(KEY, self.env_path)
        self.assertTrue(ok)
        self.assertIn(KEY[:6] + "\u2026" + KEY[-4:], info)   # 脱敏摘要（前 6…后 4）
        self.assertNotIn(KEY, info)                    # 不回显全文
        txt = self.read_env()
        self.assertIn(f"HL_PRIVATE_KEY={KEY}", txt)
        self.assertIn("HBAR=x", txt)                   # 其他行不受影响
        self.assertEqual(os.stat(self.env_path).st_mode & 0o777, 0o600)
        self.assertTrue(list(Path(self.tmp.name).glob("hl-secrets.env.bak-*")))

    def test_refuse_bad_format_without_touching_file(self):
        before = self.read_env()
        for bad in ("", "0xabc", "abc" * 22, KEY + "\nEVIL=1"):
            ok, why = keyform.write_key(bad, self.env_path)
            self.assertFalse(ok, bad)
            self.assertEqual(self.read_env(), before)

    def test_refuse_when_key_already_present(self):
        ok, _ = keyform.write_key(KEY, self.env_path)
        self.assertTrue(ok)
        ok2, why2 = keyform.write_key(KEY2, self.env_path)
        self.assertFalse(ok2)
        self.assertIn("已经存在", why2)
        self.assertIn(f"HL_PRIVATE_KEY={KEY}", self.read_env())     # 原值没被覆盖

    def test_has_key_ignores_commented_line(self):
        self.assertFalse(keyform.has_key(self.env_path))
        Path(self.env_path).write_text("# HL_PRIVATE_KEY=" + KEY + "\n")
        self.assertFalse(keyform.has_key(self.env_path))
        Path(self.env_path).write_text("HL_PRIVATE_KEY=" + KEY + "\n")
        self.assertTrue(keyform.has_key(self.env_path))

    def test_missing_placeholder_line(self):
        Path(self.env_path).write_text("HBAR=x\n")
        ok, why = keyform.write_key(KEY, self.env_path)
        self.assertFalse(ok)
        self.assertIn("占位行", why)


class TestHttp(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.env_path = str(Path(self.tmp.name) / "hl-secrets.env")
        Path(self.env_path).write_text(PLACEHOLDER)
        self.deps = KeyFormDeps(db=self.db, env_path=self.env_path)
        app = FastAPI()
        app.include_router(build_router(self.deps))
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_without_token_is_403(self):
        self.assertEqual(self.client.get("/admin/hl/key").status_code, 403)
        self.assertEqual(self.client.get("/admin/hl/key?t=nope").status_code, 403)

    def test_get_with_token_shows_form(self):
        tok, _ = keyform.issue_token(self.db)
        r = self.client.get(f"/admin/hl/key?t={tok}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("name='key'", r.text)
        self.assertIn("type='password'", r.text)

    def test_post_writes_and_burns_token_and_never_echoes_key(self):
        tok, _ = keyform.issue_token(self.db)
        r = self.client.post("/admin/hl/key", data={"t": tok, "key": KEY})
        self.assertEqual(r.status_code, 200)
        self.assertIn("已写入", r.text)
        self.assertNotIn(KEY, r.text)                       # 响应体里不能有私钥
        self.assertIn(f"HL_PRIVATE_KEY={KEY}", Path(self.env_path).read_text())
        self.assertNotIn(KEY, str(self.db.get_config()))    # 私钥绝不落库
        # 令牌用完即废
        self.assertEqual(self.client.get(f"/admin/hl/key?t={tok}").status_code, 403)

    def test_post_bad_key_reports_and_keeps_token(self):
        tok, _ = keyform.issue_token(self.db)
        r = self.client.post("/admin/hl/key", data={"t": tok, "key": "0xzz"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("格式不对", r.text)
        self.assertNotIn("HL_PRIVATE_KEY=0xzz", Path(self.env_path).read_text())
        self.assertEqual(self.client.get(f"/admin/hl/key?t={tok}").status_code, 200)  # 令牌仍在

    def test_page_says_already_set(self):
        keyform.write_key(KEY, self.env_path)
        tok, _ = keyform.issue_token(self.db)
        r = self.client.get(f"/admin/hl/key?t={tok}")
        self.assertIn("已存在私钥", r.text)

    def test_page_says_no_token_when_none_issued(self):
        Path(self.env_path).write_text(PLACEHOLDER)
        self.deps.db.set_config(keyform.CFG_HASH, "")
        r = self.client.get("/admin/hl/key?t=whatever")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
