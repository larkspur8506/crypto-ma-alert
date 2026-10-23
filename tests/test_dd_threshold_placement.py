"""回撤阈值从「开关」页迁到参数页「风控阈值」组（2026-10-23）的定点回归测试。

背景（用户指出 + 分类结论）：回撤阈值是**数值型保护阈值**，不是布尔开关。
同类的 `hl_churn_max`（撤单/重挂熔断阈值）、`hl_close_grace_s`（平仓宽限秒数）
当年就放在参数页，且代码里留着理由「它是个数值（0=关），和 risk_pct 一样属于策略参数」。
回撤阈值是唯一的例外 —— 那是上一轮做 DD Guard 时「顺手放进已有卡片」的连带结果。

本文件钉住迁移后的**位置**与**语义**两件事：

  1. 位置：参数页含两个输入框；开关页**不含**（hl_dd_guard 开关本身仍在开关页）
  2. 语义（迁移时最容易搞坏的三条）：
     a. 留空 = 保持原值，**绝不写 0**（0 在这两个键上是「回到档位自动」）
     b. 矛盾/越界 → **两条一起拒收**，保留原值
     c. 拒收**降级为提示**、不 400 —— 参数页 POST 是「全有或全无」，
        若 DD 拒收也 400，用户顺手填错的阈值会把同一次提交里的其它参数一起丢掉
  3. 兼容性：老书签/脚本仍打 /admin/switch/strategy 且带这两个键 → 仍然生效
     （迁移不能把脚本化调用变成静默失效）

全部离线：临时 sqlite DB + 假 HlInfo，不触网。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "ddp-tk"

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.hl.views import DashboardDeps
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = DashboardDeps = None

NOW = 1_760_000_000.0
ADDR = "0xDDPTESTADDRESS"

PARAMS_OK = {
    "equity": "100", "risk_pct": "0.02", "leverage": "5",
    "stop_min": "0.015", "stop_max": "0.1", "short_regime_ma": "200",
    "equity_mode": "follow", "equity_min": "20",
}


def fake_snapshot(equity=100.68):
    return {"ts": NOW, "address": ADDR, "equity_total": equity, "equity": equity,
            "spot_usdc": equity, "spot_ok": True, "unified": True,
            "withdrawable": equity, "margin_used": 0.0, "notional": 0.0,
            "positions": [], "open_orders": [], "orders_trusted": True}


class FakeInfo:
    def __init__(self, snap=None):
        self.snap = snap or fake_snapshot()

    def snapshot(self, address, mids=None):
        return dict(self.snap)

    def all_mids(self):
        return {"BTC": 83000.0}

    def specs(self, coins=None):
        return {"BTC": {"sz_decimals": 5, "max_leverage": 40, "only_isolated": False}}

    def ledger(self, *a, **k):
        return []

    def open_orders(self, *a, **k):
        return []

    def positions(self, *a, **k):
        return []


class Base(unittest.TestCase):
    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 不可用")
        from app.db import DB
        self.tmp = tempfile.mkdtemp(prefix="ddp-")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.info = FakeInfo()
        self.cfg_cache = {"cfg": Config()}
        app = FastAPI()
        hl_deps = DashboardDeps(
            db=self.db, config_fn=self._cfg, info=self.info,
            address_fn=lambda: ADDR, mode_fn=lambda: "off",
            base_url_fn=lambda: "https://api.hyperliquid-testnet.xyz",
            now_fn=lambda: NOW)
        deps = AdminDeps(
            db=self.db,
            config_fn=lambda: self._cfg(),
            hl_deps=hl_deps,
            mode_fn=lambda: "off",
            token_fn=lambda: TOKEN,
            address_fn=lambda: ADDR,
            private_key_fn=lambda: "0xpriv",
            mids_fn=lambda: self.info.all_mids(),
        )
        app.include_router(build_admin_router(deps))
        self.client = TestClient(app)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        cfg = self.cfg_cache["cfg"]
        return cfg.apply_db(self.db.get_config())

    def get(self, path, **params):
        params.setdefault("k", TOKEN)
        return self.client.get(path, params=params, follow_redirects=False)

    def post(self, path, **data):
        data.setdefault("k", TOKEN)
        return self.client.post(path, data=data, follow_redirects=False)

    def body(self, path="/admin", **params):
        r = self.get(path, **params)
        self.assertEqual(r.status_code, 200, f"{path} 应 200，实际 {r.status_code}")
        return r.text

    def params_form(self, **over):
        d = dict(PARAMS_OK)
        d.update(over)
        return d

    def loc_msg(self, r):
        """取重定向里的 msg 参数（已解码）。"""
        loc = r.headers.get("location", "")
        for part in loc.split("&"):
            if part.startswith("msg="):
                return unquote(part[4:])
        return ""


# ================================================================ 1. 位置
class TestPlacement(Base):
    def test_params_page_has_dd_threshold_fields(self):
        html = self.body(tab="params")
        self.assertIn("风控阈值", html)
        self.assertIn("name='hl_dd_warn_pct'", html)
        self.assertIn("name='hl_dd_kill_pct'", html)

    def test_switch_page_no_longer_has_dd_threshold_fields(self):
        """迁移的核心断言：开关页只剩布尔开关，两个数值框不在了。"""
        html = self.body(tab="switch")
        self.assertNotIn("name='hl_dd_warn_pct'", html)
        self.assertNotIn("name='hl_dd_kill_pct'", html)
        self.assertNotIn("回撤阈值覆盖", html)

    def test_switch_page_keeps_dd_guard_boolean_switch(self):
        """hl_dd_guard 是布尔开关（on/off），仍然留在开关页。"""
        html = self.body(tab="switch")
        self.assertIn("name='hl_dd_guard'", html)
        self.assertIn("回撤熔断", html)
        # 开关描述要指路到参数页，否则用户找不到阈值
        self.assertIn("参数页", html)

    def test_dd_threshold_group_sits_between_execution_and_funds(self):
        """组顺序：执行与推送 → 风控阈值 → 资金。"""
        html = self.body(tab="params")
        self.assertLess(html.index("执行与推送"), html.index("风控阈值"))
        self.assertLess(html.index("风控阈值"), html.index("资金"))

    def test_params_form_posts_dd_fields_to_params_endpoint(self):
        """两个输入框必须在参数页那张 form 里（form 外的控件浏览器不提交）。"""
        html = self.body(tab="params")
        form_at = html.index("<form method='post' action='/admin/params'>")
        end_at = html.index("</form>", form_at)
        seg = html[form_at:end_at]
        self.assertIn("name='hl_dd_warn_pct'", seg)
        self.assertIn("name='hl_dd_kill_pct'", seg)


# ================================================================ 2. 语义
class TestSemantics(Base):
    def test_valid_pair_saves_through_params_endpoint(self):
        r = self.post("/admin/params", **self.params_form(
            hl_dd_warn_pct="0.35", hl_dd_kill_pct="0.45"))
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._cfg().hl_dd_warn_pct, 0.35)
        self.assertEqual(self._cfg().hl_dd_kill_pct, 0.45)

    def test_empty_keeps_existing_value(self):
        """留空 = 保持原值，**绝不写 0**（0 在这两个键上是「回到档位自动」）。"""
        self.db.set_config("hl_dd_warn_pct", 0.35)
        self.db.set_config("hl_dd_kill_pct", 0.45)
        r = self.post("/admin/params", **self.params_form(
            hl_dd_warn_pct="", hl_dd_kill_pct=""))
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._cfg().hl_dd_warn_pct, 0.35)
        self.assertEqual(self._cfg().hl_dd_kill_pct, 0.45)

    def test_missing_field_keeps_existing_value(self):
        """缺字段（脚本/旧书签只带部分键）= 保持原值。"""
        self.db.set_config("hl_dd_warn_pct", 0.35)
        self.db.set_config("hl_dd_kill_pct", 0.45)
        form = {k: v for k, v in self.params_form().items()
                if k not in ("hl_dd_warn_pct", "hl_dd_kill_pct")}
        r = self.post("/admin/params", **form)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._cfg().hl_dd_warn_pct, 0.35)
        self.assertEqual(self._cfg().hl_dd_kill_pct, 0.45)

    def test_zero_means_back_to_auto_tier(self):
        """显式 0 = 回到档位自动（这是合法值，不是「关闭」）。"""
        self.db.set_config("hl_dd_warn_pct", 0.35)
        self.db.set_config("hl_dd_kill_pct", 0.45)
        r = self.post("/admin/params", **self.params_form(
            hl_dd_warn_pct="0", hl_dd_kill_pct="0"))
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._cfg().hl_dd_warn_pct, 0.0)
        self.assertEqual(self._cfg().hl_dd_kill_pct, 0.0)

    def test_inconsistent_pair_rejected_both_kept(self):
        """矛盾（warn >= kill）→ 两条一起拒收、保留原值。"""
        self.db.set_config("hl_dd_warn_pct", 0.35)
        self.db.set_config("hl_dd_kill_pct", 0.45)
        r = self.post("/admin/params", **self.params_form(
            hl_dd_warn_pct="0.45", hl_dd_kill_pct="0.35"))
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._cfg().hl_dd_warn_pct, 0.35)
        self.assertEqual(self._cfg().hl_dd_kill_pct, 0.45)

    def test_out_of_range_rejected(self):
        for warn, kill in (("0.99", "0.40"), ("0.30", "0.99"), ("abc", "0.40")):
            self.db.set_config("hl_dd_warn_pct", 0.35)
            self.db.set_config("hl_dd_kill_pct", 0.45)
            r = self.post("/admin/params", **self.params_form(
                hl_dd_warn_pct=warn, hl_dd_kill_pct=kill))
            self.assertEqual(r.status_code, 303, f"{warn}/{kill} 不该 400")
            self.assertEqual(self._cfg().hl_dd_warn_pct, 0.35, f"{warn}/{kill} 应拒收")
            self.assertEqual(self._cfg().hl_dd_kill_pct, 0.45, f"{warn}/{kill} 应拒收")

    def test_rejection_does_not_discard_other_params(self):
        """**迁移最容易搞坏的一条**：参数页 POST 是「全有或全无」，
        若 DD 拒收也 400，用户顺手填错的阈值会把同一次提交里的其它参数一起丢掉
        （静默丢改动，比「阈值没生效」严重得多）。所以拒收必须降级为提示：
        其余参数照存、两个阈值不落库、页面上有说明。"""
        r = self.post("/admin/params", **self.params_form(
            equity="250", hl_dd_warn_pct="0.45", hl_dd_kill_pct="0.35"))
        self.assertEqual(r.status_code, 303, "DD 拒收不该 400（否则连累其它参数）")
        self.assertEqual(self._cfg().equity, 250.0, "其它参数必须照存")
        # 提示语要说清「你的值没生效」
        msg = self.loc_msg(r)
        self.assertTrue(msg, "拒收时必须给一句人话说明")
        self.assertIn("没生效", msg)
        self.assertIn("回撤", msg)

    def test_rejection_message_visible_on_redirected_page(self):
        r = self.post("/admin/params", **self.params_form(
            hl_dd_warn_pct="0.45", hl_dd_kill_pct="0.35"))
        html = self.body("/admin", tab="params", msg=self.loc_msg(r))
        self.assertIn("没生效", html)

    def test_successful_save_has_no_rejection_wording(self):
        r = self.post("/admin/params", **self.params_form(
            hl_dd_warn_pct="0.35", hl_dd_kill_pct="0.45"))
        msg = self.loc_msg(r)
        self.assertNotIn("没生效", msg)
        self.assertNotIn("被拒收", msg)

    def test_tier_judged_on_new_risk_pct_not_old(self):
        """**迁移引入的新风险**：档位联动依赖 risk_pct，两层必须判在同一个档位上。

        面板层用本次提交后的 risk_pct，装配层 `Config.apply_db` 也用同一次提交后的
        risk_pct。若面板层用**旧** risk_pct，就会出现「面板放行、装配拒收」：
        同时改 risk_pct 和回撤阈值、且只填了其中一条线时，留空那条按档位补出来的
        值在旧档位下成立、在新档位下不成立 → 面板显示的值不生效。

        场景：risk_pct 0.0133（1.5% 档 → 25/30）→ 改成 0.03（3% 档 → 40/50），
        同时只填熔断线 0.30：
          旧档位下 w=0.25 < k=0.30 成立 → 面板放行
          新档位下 w=0.40 > k=0.30 不成立 → 装配层拒收
        → 正确行为是**面板也判拒收**（两个层结论一致），不写库。
        """
        self.db.set_config("risk_pct", 0.0133)
        r = self.post("/admin/params", **self.params_form(
            risk_pct="0.03", hl_dd_warn_pct="", hl_dd_kill_pct="0.30"))
        self.assertEqual(r.status_code, 303)
        # 熔断线不该被写进去（新档位下 0.30 与档位告警线 0.40 矛盾）
        self.assertEqual(self._cfg().hl_dd_kill_pct, 0.0,
                         "新档位下 0.30 应被判拒收（与装配层同结论）")
        # risk_pct 照存（它是另一个字段，DD 拒收不连累它）
        self.assertAlmostEqual(self._cfg().risk_pct, 0.03, places=6)
        self.assertIn("没生效", self.loc_msg(r))

    def test_tier_consistency_both_layers_agree(self):
        """两个都填时与档位无关，两层必然一致（回归钉住）。"""
        self.db.set_config("risk_pct", 0.0133)
        r = self.post("/admin/params", **self.params_form(
            risk_pct="0.03", hl_dd_warn_pct="0.35", hl_dd_kill_pct="0.45"))
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._cfg().hl_dd_warn_pct, 0.35)
        self.assertEqual(self._cfg().hl_dd_kill_pct, 0.45)


# ================================================================ 3. 兼容性
class TestCompatibility(Base):
    def test_old_switch_endpoint_still_accepts_dd_keys(self):
        """老书签/脚本仍打 /admin/switch/strategy 且带这两个键 → 仍要生效。
        迁移不能把脚本化调用变成静默失效。"""
        r = self.post("/admin/switch/strategy",
                      hl_dd_warn_pct="0.35", hl_dd_kill_pct="0.45")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._cfg().hl_dd_warn_pct, 0.35)
        self.assertEqual(self._cfg().hl_dd_kill_pct, 0.45)

    def test_old_switch_endpoint_rejection_still_degrades(self):
        """老入口上矛盾阈值也要降级为提示（不 400、不写库）。"""
        r = self.post("/admin/switch/strategy",
                      hl_dd_warn_pct="0.45", hl_dd_kill_pct="0.35")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._cfg().hl_dd_warn_pct, 0.0)
        self.assertEqual(self._cfg().hl_dd_kill_pct, 0.0)

    def test_switch_page_dd_fields_gone_but_guard_toggle_still_works(self):
        """开关页保留 hl_dd_guard 开关，且它仍能开关。"""
        self.db.set_config("hl_dd_guard", 0)
        html = self.body(tab="switch")
        self.assertIn("name='hl_dd_guard'", html)
        r = self.post("/admin/switch/strategy", hl_dd_guard="1")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self._cfg().hl_dd_guard)

    def test_params_page_shows_effective_threshold_and_override_is_blank(self):
        """输入框只回填「覆盖值」，不回填自动推导值（否则一保存就变显式覆盖）。"""
        html = self.body(tab="params")
        self.assertIn("name='hl_dd_warn_pct' value=''", html)
        # 当前生效值写在小字里（risk_pct=2% 档 → 30/40）
        self.assertIn("告警 30% / 熔断 40%", html)


if __name__ == "__main__":
    unittest.main()
