"""面板「更新 API」（校验新私钥 → 热切换）的测试。

覆盖：
- 成功：写 DB（hl_private_key）+ 刷新 hl_agent_state + 推送 agent_renew + 303 回总览
- 格式错 / 未授权（extraAgents 无同名行）/ 地址不匹配 / 授权已过期 / 同地址重复提交
- 授权接口取不到 → 拒收（不能盲写）
- **脱敏**：随机生成真 key 走完整流程，断言 HTTP 响应、alert_logs、hl_events、
  全部日志输出里都不含该 key 的任意 ≥8 字符子串（DB 的 hl_private_key 除外）
- 热切换生效：hl_params() 下一轮就用新私钥（不重启）
- 面板入口：<details> 折叠表单特征串

只依赖相对路径与临时 DB；不 import app.main（模块级建 DB）。
"""
from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account

from app.db import DB
from app.hl import agent as A
from app.hl.agent import CFG_PRIVATE_KEY

ROOT = Path(__file__).resolve().parent.parent
NOW_MS = 1_792_296_000_000
DAY_MS = 86_400_000
MAIN_ADDR = "0x2a41178e0088c91a7b536c258200a1178a52ec3d"


def new_key():
    """真随机 keypair（不走链、不联网）。返回 (私钥, 地址)。"""
    acct = Account.create()
    key = acct.key.hex()
    if not key.startswith("0x"):
        key = "0x" + key
    return key, acct.address


class FakeInfo:
    def __init__(self, agents=None, raise_agents=None):
        self._agents = agents or []
        self.raise_agents = raise_agents
        self.calls = 0

    def extra_agents(self, address):
        self.calls += 1
        if self.raise_agents:
            from app.hl.info import HlInfoError
            raise HlInfoError(self.raise_agents)
        return list(self._agents)


class Recorder:
    """记录所有推送文案（用于脱敏断言）。"""

    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = Recorder()
        self.events = []
        from app.config import Config
        self.cfg = Config()

    def tearDown(self):
        self.tmp.cleanup()

    def renew(self, key, agents=None, raise_agents=None, current_key=""):
        info = FakeInfo(agents=agents, raise_agents=raise_agents)
        return A.renew_key(self.db, self.cfg, self.notifier, info, key,
                           address=MAIN_ADDR, current_key=current_key,
                           now_ms=NOW_MS,
                           log_event=lambda kind, msg, **kw: self.events.append((kind, msg)))

    def authorized(self, key, days=180, name="Hermes", until=None):
        """构造「已在 HL 授权同名 Hermes」的 extraAgents 返回。"""
        _k, addr = (None, key) if str(key).startswith("0x") and len(key) == 42 else new_key()
        return [{"name": name, "address": addr,
                 "validUntil": until if until is not None else NOW_MS + days * DAY_MS}]


# ---------------------------------------------------------------- 成功路径
class TestRenewSuccess(Base):
    def test_writes_db_refreshes_state_and_pushes(self):
        key, addr = new_key()
        ok, msg = self.renew(key, agents=self.authorized(addr))
        self.assertTrue(ok, msg)
        self.assertEqual(str(self.db.get_config_str(CFG_PRIVATE_KEY)), key,
                         "新私钥要落 DB（执行器要用）")
        st = A.read_state(self.db)
        self.assertEqual(st["address"], addr)
        self.assertEqual(st["source"], "api")
        self.assertEqual(st["level"], "ok")
        self.assertEqual(st["cleanup"], "none", "换 key 后清仓状态归零")
        self.assertTrue(any("已更新" in m for m in self.notifier.messages))
        kinds = [r["kind"] for r in self.db.recent_alerts(5)]
        self.assertIn("agent_renew", kinds)
        self.assertTrue(self.events, "要记一条 decision 事件")

    def test_pushed_message_has_addr_and_bj_date(self):
        key, addr = new_key()
        self.renew(key, agents=self.authorized(addr))
        msg = [m for m in self.notifier.messages if "已更新" in m][0]
        self.assertIn(addr[:6], msg)
        self.assertIn("北京时间", msg)
        self.assertIn("2027", msg)          # 180 天后落在 2027 年

    def test_success_resets_previous_cleanup_done(self):
        key, addr = new_key()
        A.write_state(self.db, {"cleanup": "done", "address": "0xold", "source": "api",
                                "valid_until_ms": NOW_MS + DAY_MS, "days_left": 1})
        ok, _msg = self.renew(key, agents=self.authorized(addr))
        self.assertTrue(ok)
        self.assertEqual(A.read_state(self.db)["cleanup"], "none",
                         "续期成功后「已清仓」必须清掉，否则面板一直显示已清仓")

    def test_hot_switch_via_hl_params(self):
        """关键：写 DB 后 build_executor 下一轮就用新私钥（hl_params 读 DB > env）。"""
        key, addr = new_key()
        self.renew(key, agents=self.authorized(addr))
        src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        m = re.search(r"def hl_private_key.*?(?=\ndef )", src, re.S)
        self.assertIsNotNone(m)
        self.assertIn('get_config_str("hl_private_key"', m.group(0))
        # hl_params 用 hl_private_key()，且私钥不再直接读 env
        p = re.search(r"def hl_params.*?(?=\n_hl_info|\ndef )", src, re.S).group(0)
        self.assertIn("private_key=hl_private_key()", p)
        self.assertNotIn('private_key=_env("HL_PRIVATE_KEY")', p)


# ---------------------------------------------------------------- 校验失败
class TestRenewRejects(Base):
    def test_bad_format(self):
        for bad in ("", "0x123", "not-a-key", "0x" + "z" * 64,
                    "0x" + "1" * 63, "0x" + "1" * 65):
            ok, msg = self.renew(bad, agents=[])
            self.assertFalse(ok, bad)
            self.assertTrue(msg)
        self.assertIsNone(self.db.get_config_str(CFG_PRIVATE_KEY) or None,
                          "格式错的私钥绝不能落库")

    def test_newline_rejected(self):
        """中间夹换行（粘了两行）必须拒；首尾换行是复制常态，strip 掉即可。"""
        key, addr = new_key()
        ok, msg = self.renew(key[:20] + "\n" + key[20:], agents=self.authorized(addr))
        self.assertFalse(ok)
        self.assertIn("换行", msg)
        self.assertIsNone(self.db.get_config_str(CFG_PRIVATE_KEY) or None)
        ok2, _msg2 = self.renew(key + "\n", agents=self.authorized(addr))
        self.assertTrue(ok2, "首尾换行应被 strip 掉后正常保存")

    def test_not_authorized_no_matching_name(self):
        key, _addr = new_key()
        ok, msg = self.renew(key, agents=[])
        self.assertFalse(ok)
        self.assertIn("Hermes", msg)
        self.assertIn("HL 网页", msg)
        self.assertIsNone(self.db.get_config_str(CFG_PRIVATE_KEY) or None)

    def test_name_mismatch_not_accepted(self):
        """授权列表里有别的名字（同地址也不行）→ 拒收，必须用同名 Hermes。"""
        key, addr = new_key()
        ok, msg = self.renew(key, agents=[{"name": "gridbot", "address": addr,
                                          "validUntil": NOW_MS + 10 * DAY_MS}])
        self.assertFalse(ok)
        self.assertIn("Hermes", msg)
        self.assertIsNone(self.db.get_config_str(CFG_PRIVATE_KEY) or None)

    def test_address_mismatch(self):
        key, _addr = new_key()
        _k2, other = new_key()
        ok, msg = self.renew(key, agents=[{"name": "Hermes", "address": other,
                                          "validUntil": NOW_MS + 10 * DAY_MS}])
        self.assertFalse(ok)
        self.assertIn("不是同一个", msg)
        self.assertIsNone(self.db.get_config_str(CFG_PRIVATE_KEY) or None)

    def test_expired_authorization(self):
        key, addr = new_key()
        ok, msg = self.renew(key, agents=[{"name": "Hermes", "address": addr,
                                          "validUntil": NOW_MS - DAY_MS}])
        self.assertFalse(ok)
        self.assertIn("失效", msg)
        self.assertIsNone(self.db.get_config_str(CFG_PRIVATE_KEY) or None)

    def test_null_valid_until_rejected(self):
        """validUntil 为空 = 不确定是否有效 → 保守拒收（不能盲写）。"""
        key, addr = new_key()
        ok, msg = self.renew(key, agents=[{"name": "Hermes", "address": addr,
                                          "validUntil": None}])
        self.assertFalse(ok)
        self.assertIn("失效", msg)

    def test_same_address_resubmit(self):
        key, addr = new_key()
        ok, _msg = self.renew(key, agents=self.authorized(addr))
        self.assertTrue(ok)
        # 再提交同一把：应提示「就是当前这把」
        ok2, msg2 = self.renew(key, agents=self.authorized(addr))
        self.assertFalse(ok2)
        self.assertIn("当前", msg2)
        self.assertIn("未变", msg2)

    def test_same_address_via_current_key_fallback(self):
        """extraAgents 里没有同名行时，靠 current_key 派生地址也能判出「同一把」。"""
        key, addr = new_key()
        ok, msg = self.renew(key, agents=[], current_key=key)
        self.assertFalse(ok)
        self.assertIn("当前", msg)

    def test_first_time_config_allowed(self):
        """L6 回归：库里/参数都没有「当前私钥」= 首次配置 → 跳过「同地址」检查。

        以前拿 `state.address`（= HL 授权列表里的地址）当「当前」：首次配置时
        它恰好就是新 key 的地址，于是被自己挡住 —— 「这就是当前正在用的那把」。
        """
        key, addr = new_key()
        A.write_state(self.db, {"address": addr, "valid_until_ms": NOW_MS + 100 * DAY_MS,
                                "days_left": 100, "checked_ms": NOW_MS,
                                "cleanup": "none", "source": "api", "level": "ok"})
        self.assertIsNone(self.db.get_config_str(CFG_PRIVATE_KEY) or None)
        ok, msg = self.renew(key, agents=self.authorized(addr), current_key="")
        self.assertTrue(ok, f"首次配置被自己挡住了：{msg}")
        self.assertEqual(str(self.db.get_config_str(CFG_PRIVATE_KEY)), key)
        self.assertEqual(A.read_state(self.db)["address"], addr)

    def test_same_address_detected_via_db_key(self):
        """当前私钥存在 DB 里（面板更新过）→ 提交同一把必须被识别成「未变」。"""
        key, addr = new_key()
        self.db.set_config(CFG_PRIVATE_KEY, key)
        ok, msg = self.renew(key, agents=self.authorized(addr), current_key="")
        self.assertFalse(ok)
        self.assertIn("未变", msg)

    def test_duplicate_names_accepts_new_key(self):
        """M6 回归：同名多行（旧行已过期 + 新行有效）→ renew 必须接受新私钥。

        pick_agent 取「有效里最大」，但若同名的旧行排在前面、且旧行恰好就是
        当前私钥，以前会把续期误判成「填了同一把」或「地址不匹配」。
        """
        old_key, old_addr = new_key()
        new_k, new_addr = new_key()
        # 库里当前用的是旧 key；HL 上同名两行：旧行（地址=旧 key，已过期）+ 新行（有效）
        self.db.set_config(CFG_PRIVATE_KEY, old_key)
        agents = [{"name": "Hermes", "address": old_addr, "validUntil": NOW_MS - DAY_MS},
                  {"name": "Hermes", "address": new_addr,
                   "validUntil": NOW_MS + 170 * DAY_MS}]
        ok, msg = self.renew(new_k, agents=agents, current_key="")
        self.assertTrue(ok, f"同名多行时续期被拒：{msg}")
        self.assertEqual(str(self.db.get_config_str(CFG_PRIVATE_KEY)), new_k)
        st = A.read_state(self.db)
        self.assertEqual(st["address"], new_addr)
        self.assertEqual(st["level"], "ok", "续期后状态必须是正常（不能读到旧行的过期时间）")

    def test_duplicate_names_expired_row_still_rejected(self):
        """同名旧行 + 新地址但新行已过期 → 仍要拒收（别为了「能存」放宽有效期检查）。"""
        new_k, new_addr = new_key()
        agents = [{"name": "Hermes", "address": new_addr, "validUntil": NOW_MS - DAY_MS}]
        ok, msg = self.renew(new_k, agents=agents)
        self.assertFalse(ok)
        self.assertIn("失效", msg)

    def test_api_failure_rejects_instead_of_writing(self):
        key, _addr = new_key()
        ok, msg = self.renew(key, raise_agents="boom")
        self.assertFalse(ok)
        self.assertIn("没保存任何东西", msg)
        self.assertIsNone(self.db.get_config_str(CFG_PRIVATE_KEY) or None)

    def test_no_address_configured_rejects(self):
        key, _addr = new_key()
        info = FakeInfo(agents=[])
        ok, msg = A.renew_key(self.db, self.cfg, self.notifier, info, key,
                              address="", now_ms=NOW_MS)
        self.assertFalse(ok)
        self.assertIn("没保存任何东西", msg)

    def test_rejects_do_not_push_renew_alert(self):
        key, _addr = new_key()
        self.renew(key, agents=[])
        kinds = [r["kind"] for r in self.db.recent_alerts(10)]
        self.assertNotIn("agent_renew", kinds)


# ---------------------------------------------------------------- 脱敏（硬性）
class TestNoKeyLeak(Base):
    """随机真 key 走完整流程，断言响应/告警/事件/日志都不含私钥片段。"""

    MIN_FRAG = 8

    def _fragments(self, key: str, n: int = MIN_FRAG):
        """key 的所有长度 n 子串（含 0x 前缀与纯 hex 两类；覆盖「只泄一半」的情况）。"""
        body = key[2:] if key.startswith("0x") else key
        out = set()
        for src in (key, body):
            for i in range(0, len(src) - n + 1):
                out.add(src[i:i + n])
        return out

    def _assert_clean(self, text: str, frags: set, where: str):
        hits = [f for f in frags if f in text]
        self.assertEqual(hits, [], f"{where} 里出现了私钥片段（脱敏失败）：{hits[:3]}")

    def test_success_path_no_leak(self):
        key, addr = new_key()
        frags = self._fragments(key)
        logs = []

        class Collect(logging.Handler):
            def emit(self, record):
                try:
                    logs.append(record.getMessage())
                except Exception:                                 # noqa: BLE001
                    pass

        h = Collect(level=logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        try:
            ok, msg = self.renew(key, agents=self.authorized(addr))
        finally:
            root.removeHandler(h)
            root.setLevel(old_level)
        self.assertTrue(ok, msg)
        self._assert_clean(msg, frags, "返回文案")
        self._assert_clean("\n".join(self.notifier.messages), frags, "推送内容")
        self._assert_clean("\n".join(m for _k, m in self.events), frags, "hl_events")
        rows = self.db.recent_alerts(20)
        self._assert_clean("\n".join(str(r["message"]) for r in rows), frags, "alert_logs")
        self._assert_clean("\n".join(logs), frags, "日志输出")
        # DB 里存的就是它（唯一允许的位置）
        self.assertEqual(str(self.db.get_config_str(CFG_PRIVATE_KEY)), key)

    def test_failure_paths_no_leak(self):
        """各种失败路径（格式错/未授权/地址不匹配/过期/同地址）同样不许泄。"""
        key, addr = new_key()
        frags = self._fragments(key)
        logs = []

        class Collect(logging.Handler):
            def emit(self, record):
                try:
                    logs.append(record.getMessage())
                except Exception:                                 # noqa: BLE001
                    pass

        h = Collect(level=logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        outs = []
        try:
            outs.append(self.renew(key, agents=[]))                       # 未授权
            outs.append(self.renew(key, agents=[{
                "name": "Hermes", "address": new_key()[1],
                "validUntil": NOW_MS + DAY_MS * 10}]))                    # 地址不匹配
            outs.append(self.renew(key, agents=[{
                "name": "Hermes", "address": addr,
                "validUntil": NOW_MS - DAY_MS}]))                         # 已过期
            outs.append(self.renew("0x" + key[2:63], agents=[]))          # 截断
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        for ok, msg in outs:
            self.assertFalse(ok)
            self._assert_clean(str(msg), frags, "失败响应")
        self._assert_clean("\n".join(logs), frags, "失败路径日志")
        self._assert_clean("\n".join(self.notifier.messages), frags, "失败路径推送")

    def test_log_message_of_write_failure_no_leak(self):
        """写库抛异常时也不能把私钥带进日志/返回值。"""
        key, addr = new_key()
        frags = self._fragments(key)

        class BoomDB:
            def set_config(self, k, v):
                raise RuntimeError(f"disk full, value={v}")     # 故意把值带进异常

            def get_config_str(self, k, default=""):
                return default

            def was_alerted(self, *a, **kw):
                return False

            def log_alert(self, *a, **kw):
                return None

        ok, msg = A.renew_key(BoomDB(), self.cfg, self.notifier, FakeInfo(
            agents=self.authorized(addr)), key, address=MAIN_ADDR, now_ms=NOW_MS)
        self.assertFalse(ok)
        self._assert_clean(str(msg), frags, "写库失败响应")


# ---------------------------------------------------------------- 面板入口
class TestRenewPanel(Base):
    def _client(self, agent_state=None, renew=None, cleanup_on=True):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:                                       # pragma: no cover
            self.skipTest("fastapi 未安装")
        from app.admin_ui import AdminDeps, build_admin_router
        from app.hl.views import DashboardDeps
        from app.config import Config
        db = self.db
        cfg_fn = lambda: Config().apply_db(db.get_config())
        hl = DashboardDeps(db=db, config_fn=cfg_fn, info=None,
                           address_fn=lambda: MAIN_ADDR, mode_fn=lambda: "live",
                           base_url_fn=lambda: "https://api.hyperliquid.xyz")
        deps = AdminDeps(db=db, config_fn=cfg_fn, hl_deps=hl,
                         mode_fn=lambda: "live", token_fn=lambda: "tk",
                         agent_state_fn=(lambda: agent_state) if agent_state is not None
                         else (lambda: A.read_state(db)),
                         renew_fn=renew,
                         cleanup_enabled_fn=lambda: cleanup_on)
        app = FastAPI()
        app.include_router(build_admin_router(deps))
        return TestClient(app)

    def test_overview_has_status_card(self):
        """状态卡（地址/到期日/剩余/状态词）在总览 —— 只把「更新 API」表单留参数页。"""
        A.write_state(self.db, {"address": "0xc1ee4519ce893070c83cf90b324c6c8e5484be22",
                                "valid_until_ms": NOW_MS + 100 * DAY_MS,
                                "days_left": 100, "checked_ms": NOW_MS,
                                "cleanup": "none", "source": "api", "level": "ok"})
        html = self._client().get("/admin", params={"k": "tk", "tab": "overview"}).text
        self.assertIn("代理钱包地址", html)
        self.assertIn("0xc1ee…be22", html, "地址要前 6 + 后 4 截断")
        self.assertIn("到期日", html)
        self.assertIn("还剩 100 天", html)
        self.assertIn("正常", html)
        self.assertIn("到期前自动清仓", html)
        # P3 R3：状态卡降级为总览末尾 <details>（100 天 → 默认收起）
        self.assertIn("<details class='card agent-card'><summary>API 有效期</summary>", html)
        # 状态卡里不再有写操作入口（续期表单已迁参数页）
        self.assertNotIn("action='/admin/hl/agent/renew'", html)

    def test_params_tab_has_renew_block(self):
        """「更新 API（续期）」折叠表单在参数页：原生 <details>，无地址/状态行。"""
        html = self._client().get("/admin", params={"k": "tk", "tab": "params"}).text
        self.assertIn("更新 API（续期）", html)
        # 折叠表单：原生 <details class="msgdetails">
        self.assertIn('<details class="msgdetails"><summary>在 HL 网页续期后粘贴新私钥',
                      html)
        self.assertIn("action='/admin/hl/agent/renew'", html)
        self.assertIn("type='password' name='privkey'", html)
        self.assertIn("More → API", html)
        self.assertIn("同名 Hermes", html)
        self.assertNotIn("代理钱包地址", html)

    def test_card_position_at_end_of_pages(self):
        """状态卡在总览末尾（「系统」卡之后）；续期表单在参数页保存按钮之后。"""
        A.write_state(self.db, {"address": "0xabc", "valid_until_ms": NOW_MS + 10 * DAY_MS,
                                "days_left": 10, "checked_ms": NOW_MS,
                                "cleanup": "none", "source": "api", "level": "near"})
        c = self._client()
        ov = c.get("/admin", params={"k": "tk", "tab": "overview"}).text
        # 总览末尾：账户卡与「执行器」区块之后（P3 R2 系统→执行器；P3 R3 卡降级 details）
        self.assertLess(ov.index("账户</div>"), ov.index("<summary>API 有效期</summary>"))
        self.assertLess(ov.index("<h2>执行器</h2>"), ov.index("<summary>API 有效期</summary>"))
        # 2026-10-29 与仓位表对换：三标的动态在账户卡之后（仓位表移到最顶）
        if "三标的动态" in ov:
            self.assertGreater(ov.index("三标的动态"), ov.index("账户</div>"))
        # 参数页：续期小区块在「保存全部参数」之后
        html = c.get("/admin", params={"k": "tk", "tab": "params"}).text
        self.assertLess(html.index("保存全部参数"), html.index("更新 API（续期）"))
        self.assertNotIn("<h2>API 有效期</h2>", html)

    def test_state_word_mapping(self):
        """L4：用精确锚点断言（「状态」那一行的 `<span class='v'>`），不再用恒真的 assertIn。"""
        pat = "<span class='k'>状态</span><span class='v[^']*'>%s</span>"
        for level, word in (("ok", "正常"), ("near", "临期"), ("urgent", "临界"),
                            ("expired", "已失效"), ("unknown", "未知")):
            A.write_state(self.db, {"address": "0xabc",
                                    "valid_until_ms": NOW_MS + 5 * DAY_MS,
                                    "days_left": 5, "checked_ms": NOW_MS,
                                    "cleanup": "none", "source": "api", "level": level})
            html = self._client().get("/admin", params={"k": "tk", "tab": "overview"}).text
            self.assertRegex(html, pat % word,
                             f"level={level} 时状态词应精确渲染成 {word}")

    def test_no_expiry_card_shows_unlimited(self):
        """H4：不限期授权 → 面板显示「不限期」，剩余写「不会到期」。"""
        A.write_state(self.db, {"address": "0xabc", "valid_until_ms": None,
                                "days_left": None, "checked_ms": NOW_MS,
                                "cleanup": "none", "source": "api", "level": "no_expiry"})
        html = self._client().get("/admin", params={"k": "tk", "tab": "overview"}).text
        self.assertRegex(html, "<span class='k'>状态</span><span class='v[^']*'>不限期</span>")
        self.assertIn("不会到期", html)
        self.assertIn("不限期（HL 授权没设到期）", html)

    def test_skipped_state_shown(self):
        """M5：off 模式清仓 skipped → 面板明确写「未自动清仓（执行器 off）」。"""
        A.write_state(self.db, {"address": "0xabc", "valid_until_ms": NOW_MS + 2 * DAY_MS,
                                "days_left": 2, "checked_ms": NOW_MS,
                                "cleanup": "skipped", "source": "api", "level": "cleanup"})
        html = self._client().get("/admin", params={"k": "tk", "tab": "overview"}).text
        self.assertIn("未自动清仓（执行器 off）", html)
        self.assertNotIn("清仓失败", html)

    def test_cleanup_done_shows_cleared(self):
        A.write_state(self.db, {"address": "0xabc", "valid_until_ms": NOW_MS + 2 * DAY_MS,
                                "days_left": 2, "checked_ms": NOW_MS,
                                "cleanup": "done", "source": "api", "level": "cleanup"})
        html = self._client().get("/admin", params={"k": "tk", "tab": "overview"}).text
        self.assertIn("已清仓", html)

    def test_cleanup_failed_shows_retry(self):
        A.write_state(self.db, {"address": "0xabc", "valid_until_ms": NOW_MS + 2 * DAY_MS,
                                "days_left": 2, "checked_ms": NOW_MS,
                                "cleanup": "failed", "source": "api", "level": "cleanup"})
        html = self._client().get("/admin", params={"k": "tk", "tab": "overview"}).text
        self.assertIn("清仓失败", html)

    def test_unknown_state_renders_without_network(self):
        """state 空 → 显示未知与「下一个整点」，且不调用任何 info 方法。"""
        html = self._client().get("/admin", params={"k": "tk", "tab": "overview"}).text
        self.assertIn("<summary>API 有效期</summary>", html)
        self.assertIn("未知", html)
        self.assertIn("下一个整点", html)

    def test_switch_off_shown(self):
        A.write_state(self.db, {"address": "0xabc", "valid_until_ms": NOW_MS + 2 * DAY_MS,
                                "days_left": 2, "checked_ms": NOW_MS,
                                "cleanup": "none", "source": "api", "level": "cleanup"})
        html = self._client(cleanup_on=False).get("/admin", params={"k": "tk", "tab": "overview"}).text
        self.assertIn("已关闭", html)

    def test_renew_endpoint_success_303_to_overview(self):
        key, addr = new_key()
        seen = {}

        def fake_renew(k):
            seen["key"] = k
            return True, "API 已更新：新地址 0xaaaa…bbbb"

        r = self._client(renew=fake_renew).post(
            "/admin/hl/agent/renew", data={"privkey": key, "k": "tk"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("tab=overview", r.headers["location"])
        self.assertIn("k=tk", r.headers["location"])
        self.assertEqual(seen["key"], key)

    def test_renew_endpoint_failure_400(self):
        def fake_renew(k):
            return False, "HL 授权列表里没有名字叫 Hermes 的 API 钱包"

        r = self._client(renew=fake_renew).post(
            "/admin/hl/agent/renew", data={"privkey": "0x" + "1" * 64, "k": "tk"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Hermes", r.text)

    def test_renew_endpoint_requires_token(self):
        r = self._client(renew=lambda k: (True, "x")).post(
            "/admin/hl/agent/renew", data={"privkey": "0x" + "1" * 64},
            follow_redirects=False)
        self.assertEqual(r.status_code, 401)

    def test_renew_endpoint_not_wired(self):
        r = self._client().post("/admin/hl/agent/renew",
                                data={"privkey": "0x" + "1" * 64, "k": "tk"},
                                follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertIn("未接线", r.text)

    def test_renew_endpoint_swallows_exception_without_key(self):
        key, _addr = new_key()

        def boom(k):
            raise RuntimeError(f"oops {k}")

        r = self._client(renew=boom).post(
            "/admin/hl/agent/renew", data={"privkey": key, "k": "tk"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        for i in range(0, len(key) - 8 + 1):
            self.assertNotIn(key[i:i + 8], r.text, "响应体不许含私钥片段")

    def test_renew_endpoint_full_flow_no_leak(self):
        """端到端：面板 → renew_key（真校验）→ DB，响应体不含私钥。"""
        key, addr = new_key()

        def real_renew(k):
            return A.renew_key(self.db, self.cfg, self.notifier, FakeInfo(
                agents=self.authorized(addr)), k, address=MAIN_ADDR, now_ms=NOW_MS)

        r = self._client(renew=real_renew).post(
            "/admin/hl/agent/renew", data={"privkey": key, "k": "tk"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        body = r.text + r.headers.get("location", "")
        for i in range(0, len(key) - 8 + 1):
            self.assertNotIn(key[i:i + 8], body)
        self.assertEqual(str(self.db.get_config_str(CFG_PRIVATE_KEY)), key)


# ---------------------------------------------------------------- 单元：派生地址
class TestDeriveAddress(unittest.TestCase):
    def test_valid_key_derives_checksum_addr(self):
        key, addr = new_key()
        got, why = A.derive_address(key)
        self.assertEqual(got, addr)
        self.assertEqual(why, "")

    def test_bad_keys(self):
        for bad in ("", "0x", "0xzz", "0x" + "1" * 63, "0X" + "1" * 64):
            got, why = A.derive_address(bad)
            self.assertIsNone(got, bad)
            self.assertTrue(why)

    def test_error_message_never_echoes_key(self):
        key, _addr = new_key()
        _got, why = A.derive_address("0x" + key[2:])
        self.assertNotIn(key[2:20], why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
