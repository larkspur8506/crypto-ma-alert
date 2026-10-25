"""面板 P4 收尾轮的回归测试（规格：spec-panel-p4.md，2026-10-25）。

三项功能逐项钉词面（铁律：断言看渲染 HTML、剥 head 后再定位；双入口 = /admin 的
?k= 查询串 与 cma_admin cookie 两条取页面路径，渲染类用例两个入口都断言）：

1. 记录页锚点导航删除：href='#cma- 与 id='cma- 渲染层 0 处，页顶直接是「资金流水」。
2. 参数页低频折叠：<details>「API 续期与兜底（低频）」包住「更新 API（续期）」表单 +
   「API 有效期」组（手工兜底字段 + 清除兜底按钮）；兜底字段用 form 属性继续归属
   「保存全部参数」表单；折叠态（POST 不带键）保存成功且该键保持原值。
3. 开关页：说明折叠（>1 行收进 details.sw-help、≤1 行保留可见）+ 应急操作卡
   （kill switch + 重置回撤基线，红框独立卡置页尾）+ 顺序 执行模式→推送→策略与保护→应急；
   执行模式卡警告 banner 不折叠；hidden+checkbox 双字段结构保留；POST 语义回归。

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
    from app.admin_ui import (NOTIFY_KEYS, STRATEGY_KEYS, AdminDeps,
                              build_admin_router, _hint_multi_line)
    from app.config import Config
    from app.db import DB
    from app.hl.views import DashboardDeps
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = DB = None
    DashboardDeps = NOTIFY_KEYS = STRATEGY_KEYS = _hint_multi_line = None


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

    def post(self, path, **data):
        data.setdefault("k", TOKEN)
        return self.client.post(path, data=data, follow_redirects=False)


# ---------------------------------------------------------------- 功能一：锚点删除
class TestRecordAnchorsGone(BaseCase):

    def test_anchor_wordmarks_zero_both_entries(self):
        for html in (self.admin(tab="records"), self.admin_cookie("records")):
            self.assertEqual(html.count("href='#cma-"), 0, "锚点链接 0 处")
            self.assertEqual(html.count("id='cma-"), 0, "区块容器死 id 0 处")
            self.assertNotIn("<a href='#", html, "不残留任何页内跳转链接")
            # 页面顶部直接是「资金流水」卡（资金流水仍在 <h2>记录</h2> 之前）
            self.assertLess(html.index("资金流水"), html.index("<h2>记录</h2>"))


# ---------------------------------------------------------------- 功能二：参数页低频折叠
def _lowfreq_seg(params_html: str) -> str:
    """「API 续期与兜底（低频）」details 的整段（到其后的资金口径 <script> 为止）。

    不能拿第一个 </details> 当终点 —— 里面的「更新 API（续期）」自带一层
    <details class="msgdetails">，嵌套 details 会让「最近一个闭合标签」切早。
    """
    i = params_html.index("<details class='lowfreq'>")
    return params_html[i:params_html.index("<script>", i)]


class TestParamsLowFreqDetails(BaseCase):

    def test_details_wraps_renew_and_fallback(self):
        self.db.set_config("hl_agent_valid_until_ms", "1830000000000")
        for html in (self.admin(tab="params"), self.admin_cookie("params")):
            self.assertIn("<details class='lowfreq'><summary>API 续期与兜底（低频）</summary>", html)
            self.assertLess(html.index("保存全部参数"),
                            html.index("API 续期与兜底（低频）"), "details 在「保存全部参数」之后")
            seg = _lowfreq_seg(html)
            # 组内字段一个不少：续期表单 + 兜底字段 + 清除兜底按钮都在同一个 details 里
            self.assertIn("更新 API（续期）", seg)
            self.assertIn("action='/admin/hl/agent/renew'", seg)
            self.assertIn("type='password' name='privkey'", seg)
            self.assertIn("<div class='sec'>API 有效期</div>", seg)
            self.assertIn("name='hl_agent_valid_until_ms'", seg)
            self.assertIn("action='/admin/hl/agent/fallback/clear'", seg)
            self.assertIn("清除兜底", seg)
            # 组卡说明原文保留（折叠只是藏进可展开处，不删说明文字）
            self.assertIn("接口查不到有效期时的手工兜底；到期前要不要自动清仓在「开关」页", seg)
            # 主 form 四组与「影响预览」不动
            for grp in ("改动影响预览", "策略", "执行与推送", "风控阈值", "资金"):
                self.assertIn(grp, html)
            self.assertLess(html.index("改动影响预览"), html.index("保存全部参数"))

    def test_fallback_field_still_belongs_to_params_form(self):
        html = self.admin(tab="params")
        self.assertIn("<form method='post' action='/admin/params' id='params-form'>", html)
        # form 属性反挂：details 在主 form 之外，字段仍随「保存全部参数」提交
        self.assertIn("<input form='params-form' type='text' inputmode='numeric' "
                      "name='hl_agent_valid_until_ms'", html)

    def test_collapsed_post_keeps_fallback_value(self):
        """折叠态提交不丢键：POST 不带 hl_agent_valid_until_ms → 保存成功、键保持原值。"""
        self.db.set_config("hl_agent_valid_until_ms", "1830000000000")
        r = self.post("/admin/params", risk_pct="0.02", leverage="5", stop_min="0.02",
                      stop_max="0.08", short_regime_ma="200")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("hl_agent_valid_until_ms")),
                         "1830000000000", "折叠态（缺键）保存后兜底值必须原样保留")
        # 字段本身仍可提交：带上键 → 落新值（POST 字段名不变）
        r = self.post("/admin/params", risk_pct="0.02", leverage="5", stop_min="0.02",
                      stop_max="0.08", short_regime_ma="200",
                      hl_agent_valid_until_ms="1840000000000")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("hl_agent_valid_until_ms")),
                         "1840000000000")


# ------------------------------------------------ 功能三：开关页折叠 + 应急分组
class TestSwitchFoldAndEmergency(BaseCase):

    def test_card_order_common_first(self):
        for html in (self.admin(tab="switch"), self.admin_cookie("switch")):
            idx = [html.index(x) for x in ("执行模式", "推送开关", "策略与保护", "应急操作")]
            self.assertEqual(idx, sorted(idx), f"顺序应为 执行模式→推送→策略与保护→应急：{idx}")

    def test_sw_help_fold_criterion(self):
        """>1 行说明收进 details.sw-help、≤1 行保留可见；原文一字不少。"""
        html = self.admin(tab="switch")
        # 渲染数量 = 判据算出的数量（9 个策略开关全为长说明 + fill_push + 执行模式说明段）
        expected = sum(1 for k in STRATEGY_KEYS if _hint_multi_line(k[2]))
        expected += sum(1 for _k, _l, desc, _d in NOTIFY_KEYS if _hint_multi_line(desc))
        expected += 1   # 执行模式卡的说明段（地址/私钥来源）
        self.assertEqual(html.count("<details class='sw-help'>"), expected)
        # 短说明（≤1 行）保留可见：不在 sw-help 里，但原文还在页面上
        for key, _l, desc, _d in NOTIFY_KEYS:
            if _hint_multi_line(desc):
                self.assertIn(f"<details class='sw-help'><summary>说明</summary>"
                              f"<div class='hint'>&nbsp;&nbsp;{desc}</div></details>", html)
            else:
                self.assertNotIn(f"<details class='sw-help'><summary>说明</summary>"
                                 f"<div class='hint'>&nbsp;&nbsp;{desc}</div></details>", html,
                                 f"{key} 是一行短说明，不应折叠")
                self.assertIn(desc, html)
        # 长说明原文仍可展开（details 内含原文词面，不是被删了）
        long_hint = STRATEGY_KEYS[0][2]
        self.assertIn(f"<details class='sw-help'><summary>说明</summary>"
                      f"<div class='hint'>{long_hint}</div></details>", html)

    def test_switch_rows_show_name_and_state(self):
        html = self.admin(tab="switch")
        for key, label, *_rest in STRATEGY_KEYS:
            self.assertIn(label, html)
            self.assertIn(f"name='{key}'", html)
        for key, label, _desc, _d in NOTIFY_KEYS:
            self.assertIn(f"<b>{label}</b>", html)
            self.assertIn(f"name='{key}'", html)
        self.assertIn("当前 开", html)
        self.assertIn("当前 关", html)

    def test_mode_card_banner_not_folded(self):
        """执行模式卡的警告 banner 与确认流程是安全文案，不折叠。"""
        html = self.admin(tab="switch")
        seg = html.split("<div class='sec'>执行模式</div>", 1)[1].split("推送开关", 1)[0]
        self.assertIn("切到实盘会用真钱下单", seg)
        self.assertIn("我已确认：切实盘会真下单", seg)
        self.assertLess(seg.index("切到实盘会用真钱下单"),
                        seg.index("<details"), "警告 banner 必须在卡内第一个折叠之前")

    def test_emergency_card_grouped_and_last(self):
        for html in (self.admin(tab="switch"), self.admin_cookie("switch")):
            self.assertIn("<div class='card danger'><div class='sec'>应急操作</div>", html,
                          "应急操作独立卡 + 红框类")
            self.assertIn("两个动作都会直接影响交易，慎点", html)
            self.assertLess(html.index("策略与保护"), html.index("应急操作"),
                            "应急卡置页尾，与日常开关物理隔开")
            seg = html.split("<div class='card danger'>", 1)[1]
            self.assertIn("action='/admin/switch/kill'", seg)
            self.assertIn("action='/admin/switch/dd_reset'", seg)
            self.assertIn("重置回撤基线", seg)
            # 应急卡是最后一张卡：dd_reset 表单闭合后不再有新卡（页脚只有主题/行数）
            after = seg[seg.index("</form></div>") + len("</form></div>"):]
            self.assertNotIn("<div class='card'", after, "应急卡之后不应再有卡")


class TestSwitchPostSemantics(BaseCase):
    """折叠/分组后 POST 语义逐条回归：结构不动、语义链不破（PR #29 血泪）。"""

    def test_strategy_hidden_checkbox_structure_kept(self):
        html = self.admin(tab="switch")
        for key, *_rest in STRATEGY_KEYS:
            self.assertIn(f"<input type='hidden' name='{key}' value='0'>", html,
                          f"{key} 的 hidden 兜底必须在")
            self.assertIn(f"<input type='checkbox' name='{key}' value='1'", html)

    def test_strategy_unchecked_post_turns_off(self):
        """未勾选（只提交 hidden 的 0）→ 关；同名双值（0 再 1）→ 开。"""
        self.db.set_config("enable_long", 1)
        r = self.post("/admin/switch/strategy", enable_long="0")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("enable_long")), "0")
        r = self.client.post(
            "/admin/switch/strategy",
            content=f"k={TOKEN}&enable_long=0&enable_long=1",
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("enable_long")), "1")

    def test_notify_missing_key_keeps_value(self):
        """推送开关（无 hidden 兜底）：缺字段 = 保持原值，折叠不改语义。"""
        self.db.set_config("heartbeat_push", 1)
        self.db.set_config("fill_push", 0)
        r = self.post("/admin/switch/notify", fill_push="1")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("heartbeat_push")), "1")
        self.assertEqual(str(self.db.get_config_str("fill_push")), "1")

    def test_kill_and_dd_reset_routes_unchanged(self):
        r = self.post("/admin/switch/kill", on="1")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.db.hl_kill(), "kill 触发照常")
        r = self.post("/admin/switch/kill", on="0")
        self.assertEqual(r.status_code, 303)
        self.assertFalse(self.db.hl_kill(), "kill 解除照常")
        r = self.post("/admin/switch/dd_reset")
        self.assertEqual(r.status_code, 303, "重置回撤基线路由照常（无锚点时也不 5xx）")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
