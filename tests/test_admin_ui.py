"""合并管理面板（app/admin_ui.py）的单元测试。

验收点逐条对应：
- 未授权 401
- 四个 Tab 各 200 且含关键区块串（账户总值 / 单笔风险 / 只通知 / 下单记录 …）
- 手机优先：无框架、无外部资源（参数页只有一段原生内联 JS 做资金口径显隐）、字号 ≥13px
- 改参数后 DB 值生效且页面回显
- 参数 POST **不再接受**方向/保护类开关（enable_long / enable_shorts /
  heartbeat_push / agent_expiry_cleanup），这四键改由「开关」页的
  「策略与保护」卡（/admin/switch/strategy）负责
- 非法值被拒（400）且**不写库**
- 切 live 缺凭据 → 400；切实盘必须二次确认
- kill switch 往返
- 旧链接 /hl、/admin/rules、/admin/logs → 3xx
- /api/hl/status 仍 200（合并没弄坏 JSON API）
- params Tab 在只读接口全挂时仍 200（降级成「无法取价」）

全部用临时 sqlite DB + 假 HlInfo，不触网。
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "panel-tk"

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.hl.views import DashboardDeps, build_router as build_hl_router
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = DashboardDeps = build_hl_router = None

NOW = 1_760_000_000.0
ADDR = "0xTESTADDRESS123456"

# 参数表单的默认值（与 Config 默认一致），用例里按需覆盖。
# 2026-10-19：enable_long / enable_shorts / heartbeat_push / agent_expiry_cleanup
# 四个开关已撤出参数页（迁到「开关」页），所以夹具里不再带它们。
PARAMS_OK = {
    "equity": "100", "risk_pct": "0.02", "leverage": "5",
    "stop_min": "0.015", "stop_max": "0.1", "short_regime_ma": "200",
    "equity_mode": "follow", "equity_min": "20",
}


def fake_snapshot(equity=100.68, with_pos=True):
    """与 HlInfo.snapshot 口径一致的固定快照。equity_total=100.68 → 生效基数 100。"""
    snap = {"ts": NOW, "address": ADDR, "equity_total": equity, "equity": equity,
            "spot_usdc": equity, "unified": True, "withdrawable": 90.0,
            "margin_used": 10.0, "notional": 500.0, "positions": [], "open_orders": []}
    if with_pos:
        snap["positions"] = [{
            "coin": "BTC", "side": 1, "size": 0.005, "entry_px": 99_000.0,
            "mark_px": 100_000.0, "position_value": 500.0, "liq_px": 80_000.0,
            "leverage": 5, "isolated": True, "margin_used": 100.0,
            "unrealized_pnl": 5.0, "roe": 0.05, "funding_since_open": -0.5,
        }]
        snap["open_orders"] = [{
            "coin": "BTC", "oid": 7, "is_buy": False, "sz": 0.005, "limit_px": 0.0,
            "reduce_only": True, "trigger_px": 96_000.0, "is_trigger": True,
            "timestamp": int(NOW * 1000),
        }]
    return snap


class FakeInfo:
    """HlInfo 替身。raise_=True 模拟只读接口全挂（面板必须仍 200）。"""

    def __init__(self, snapshot=None, mids=None, raise_=False):
        self._snap = snapshot if snapshot is not None else fake_snapshot()
        self._mids = dict(mids) if mids is not None else {"BTC": 100_000.0, "ETH": 3_000.0}
        self.raise_ = raise_

    def snapshot(self, address, mids=None):
        if self.raise_:
            raise RuntimeError("boom: network down")
        return self._snap

    def all_mids(self):
        if self.raise_:
            raise RuntimeError("boom: allMids down")
        return dict(self._mids)


class _FakeHlInfo:
    """顶掉 app.hl.info.HlInfo：app.main 一 import 就会构造真实客户端并打网络。"""

    def __init__(self, *args, **kwargs):
        self._mids = {}

    def snapshot(self, address, mids=None):
        return {"ts": NOW, "equity_total": 100.0, "equity": 100.0, "spot_usdc": 100.0,
                "unified": True, "withdrawable": 90.0, "margin_used": 10.0,
                "notional": 0.0, "positions": [], "open_orders": []}

    def all_mids(self):
        return {}


class BaseCase(unittest.TestCase):
    """临时 DB + 假 info + 只挂合并面板路由的 app。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="admin_ui_")
        # 让 RISK_PCT 走 env（面板要显示「来源 env」）；值与代码默认一致，不影响断言
        self._env_backup = {k: os.environ.get(k) for k in ("RISK_PCT", "STOP_MAX")}
        os.environ["RISK_PCT"] = "0.02"
        os.environ["STOP_MAX"] = "0.1"
        from app.db import DB
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.info = FakeInfo()
        # 口径 DB > env > 代码默认：config_fn 直接复用生产同一条路径
        self.cfg_cache = {"cfg": Config()}
        self.hl_deps = DashboardDeps(
            db=self.db, config_fn=self._cfg, info=self.info,
            address_fn=lambda: ADDR, mode_fn=lambda: self._mode(),
            base_url_fn=lambda: "https://api.hyperliquid-testnet.xyz",
            now_fn=lambda: NOW)
        self.deps = AdminDeps(
            db=self.db, config_fn=self._cfg, hl_deps=self.hl_deps,
            mode_fn=self._mode, token_fn=lambda: TOKEN,
            address_fn=lambda: ADDR, private_key_fn=lambda: "",
            mids_fn=lambda: self.info.all_mids())
        app = FastAPI()
        app.include_router(build_admin_router(self.deps))
        app.include_router(build_hl_router(self.hl_deps))
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, val in self._env_backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    # ---- 依赖替身 ----
    def _cfg(self):
        return self.cfg_cache["cfg"].apply_db(self.db.get_config())

    def _mode(self):
        # set_config 走 json.dumps，所以必须用 get_config_str 解掉引号
        return self.db.get_config_str("hl_exec_mode", "off") or "off"

    def swap_info(self, info):
        """换掉只读客户端替身（deps 和 hl_deps 都要换，否则回落路径还会用旧的）。"""
        self.info = info
        self.deps.hl_deps.info = info
        self.deps.mids_fn = lambda: info.all_mids()

    def get(self, path, **params):
        params.setdefault("k", TOKEN)
        return self.client.get(path, params=params, follow_redirects=False)

    def post(self, path, **data):
        """不跟随 303：用例要检查重定向目标本身。"""
        data.setdefault("k", TOKEN)
        return self.client.post(path, data=data, follow_redirects=False)

    def params_form(self, **over):
        d = dict(PARAMS_OK)
        d.update(over)
        return d

    def body(self, path="/admin", **params):
        r = self.get(path, **params)
        self.assertEqual(r.status_code, 200, f"{path} 应 200，实际 {r.status_code}")
        return r.text


class TestAuth(BaseCase):
    def test_no_token_is_401(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 401)

    def test_wrong_token_is_401(self):
        r = self.client.get("/admin", params={"k": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_unauthorized_post_is_401(self):
        r = self.client.post("/admin/params", data=self.params_form())
        self.assertEqual(r.status_code, 401)

    def test_cookie_auth_ok(self):
        self.get("/admin")                       # 带 k → 写 Cookie
        r = self.client.get("/admin")            # 之后只靠 Cookie
        self.assertEqual(r.status_code, 200)

    def test_unknown_tab_falls_back_to_overview(self):
        html = self.body("/admin", tab="javascript:void(0)")
        self.assertIn("账户总值", html)
        self.assertIn("tab=overview", html)

    def test_admin_token_missing_is_500(self):
        self.deps.token_fn = lambda: ""
        r = self.client.get("/admin", params={"k": TOKEN})
        self.assertEqual(r.status_code, 500)


class TestOverviewTab(BaseCase):
    def test_overview_200_with_key_blocks(self):
        html = self.body(tab="overview")
        for s in ("账户总值", "盈亏曲线（已剔除出入金）", "仓位", "交易所挂单"):
            self.assertIn(s, html)
        # 2026-10-19：状态卡在总览末尾（PR#29 搬多了，用户拍板只把「更新 API」表单留参数页）
        self.assertIn("<h2>API 有效期</h2>", html)
        # 2026-10-15 用户反馈：总览与「记录」页的执行器事件表完全重复 → 总览移除
        self.assertNotIn("执行器事件", html)
        # 2026-10-18 同一类操作：下单记录 / 最近成交 / 信号 三张历史表迁到「记录」页
        # （前两张记录页本来就有；信号是本次新增）。总览只剩"当下"信息。
        for gone in ("<h2>下单记录", "<h2>最近成交", "<h2>信号</h2>", "最近成交（最近"):
            self.assertNotIn(gone, html, f"总览不该再有历史表：{gone}")

    def test_overview_keeps_current_state_blocks(self):
        """迁走三张表 + API 有效期卡片后，总览的当下块必须还在（仓位/挂单/系统）。"""
        html = self.body(tab="overview")
        for keep in ("<h2>仓位</h2>", "<h2>交易所挂单</h2>", "<h2>系统</h2>",
                     "<h2>盈亏曲线（已剔除出入金）</h2>"):
            self.assertIn(keep, html, f"总览缺少当下块：{keep}")
        # API 有效期状态卡在总览末尾（只把写私钥的「更新 API」表单留在参数页）
        self.assertIn("<h2>API 有效期</h2>", html)
        # 三标的动态（用户钉死在 tabs 之后第一块）仍在
        self.assertIn("三标的动态", html)

    def test_overview_reuses_hl_body_without_duplicate_title(self):
        html = self.body(tab="overview")
        self.assertEqual(html.count("<h1>"), 1, "整页只能有一个 <h1>（hl 自带标题已剥掉）")
        # <h2> 区块是 hl/views._render_body 的产物，admin_ui 自己一个 <h2> 都不渲染
        # （记录页的五块用 <div class='sec'> 标题，不用 <h2>）
        for h2 in ("<h2>仓位</h2>", "<h2>盈亏曲线（已剔除出入金）</h2>", "<h2>交易所挂单</h2>"):
            self.assertIn(h2, html, f"总览应内嵌 _render_body 的 {h2}")
        # 下单记录已迁走 → 总览的 <h2> 计数也随之减少（防"删了又长回来"）
        self.assertNotIn("<h2>下单记录", html)

    def test_status_strip_is_always_present(self):
        for tab in ("overview", "params", "switch", "records"):
            html = self.body(tab=tab)
            for s in ("模式", "熔断", "对账", "HL 延迟", "更新"):
                self.assertIn(s, html, f"{tab} 缺状态条：{s}")

    def test_tabs_switch_via_querystring(self):
        html = self.body(tab="overview")
        for tab, label in (("overview", "总览"), ("params", "参数"),
                           ("switch", "开关"), ("records", "记录")):
            self.assertIn(f"/admin?tab={tab}&k={TOKEN}", html)
            self.assertIn(label, html)
        self.assertIn("class='on'", html)


class TestParamsTab(BaseCase):
    def test_params_200_with_key_blocks(self):
        html = self.body(tab="params")
        for s in ("策略", "执行与推送", "资金", "单笔风险", "杠杆", "止损距离",
                  "资金口径", "保存全部参数"):
            self.assertIn(s, html)
        # 2026-10-19：状态卡回总览末尾；参数页只留「更新 API（续期）」表单
        self.assertNotIn("<h2>API 有效期</h2>", html)
        self.assertIn("/admin/hl/agent/renew", html)
        self.assertNotIn("代理钱包地址", html)
        # 资金口径改成单选钮 + data-mode（配合原生 JS 显隐）
        self.assertIn("name='equity_mode'", html)
        self.assertIn("type='radio'", html)
        self.assertIn("data-mode=", html)
        # 方向开关迁到「开关」页 → 参数页不该再出现「允许做多」
        self.assertNotIn("允许做多", html)

    def test_params_shows_source_and_effective_value(self):
        self.db.set_config("equity", 100)
        html = self.body(tab="params")
        self.assertIn("来源 DB", html)
        self.assertIn("来源 env", html)
        self.assertIn("来源 默认", html)
        self.assertIn("当前 100", html)
        self.assertIn("name='equity' value='100'", html)

    def test_params_preview_computes_from_btc_price(self):
        # 基数 100.68 → floor 到 10 = 100；风险 2% = 2U；最紧止损 1.5% → 名义 133U
        html = self.body(tab="params")
        self.assertIn("按 BTC 现价 100,000", html)
        self.assertIn("单笔风险 2.00U", html)
        self.assertIn("名义 133U", html)
        self.assertIn("保证金 27U", html)
        self.assertIn("强平 多80,500/空119,500", html)

    def test_params_degrades_when_price_unavailable(self):
        self.swap_info(FakeInfo(mids={}))                 # 没有 BTC 报价
        html = self.body(tab="params")                    # 页面仍 200
        self.assertIn("无法取价", html)
        self.assertNotIn("按 BTC 现价", html)             # 影响预览整段降级，不再算数

    def test_params_200_when_info_raises_everywhere(self):
        self.swap_info(FakeInfo(raise_=True))             # 快照 + allMids 全挂
        html = self.body(tab="params")
        self.assertIn("无法取价", html)
        self.assertIn("保存全部参数", html)
        # overview 在 info 挂掉时也必须 200
        html2 = self.body(tab="overview")
        self.assertIn("账户总值", html2)

    def test_params_save_writes_db_and_echoes(self):
        r = self.post("/admin/params", **self.params_form(equity="250"))
        self.assertEqual(r.status_code, 303)
        self.assertIn("tab=params", r.headers["location"])
        self.assertEqual(self._cfg().equity, 250.0)
        html = self.body(tab="params")
        self.assertIn("name='equity' value='250'", html)

    def test_params_save_redirects_with_saved_msg(self):
        r = self.post("/admin/params", **self.params_form())
        self.assertIn("tab=params", r.headers["location"])
        self.assertIn("msg=", r.headers["location"])
        # 跳转回来的页面要显示「已保存」
        self.assertIn("已保存", self.body("/admin", tab="params", msg="已保存"))

    def test_params_equity_min_zero_now_rejected(self):
        """审计三轮 M6：面板校验与 Config._RANGES 对齐 —— equity_min=0 直接 400，
        不再「入库成功 + apply_db 静默忽略 + 页面显示已保存」。"""
        snap = self.db.get_config()
        r = self.post("/admin/params", **self.params_form(equity_min="0"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.db.get_config(), snap, "校验失败时绝不能写库")

    def test_params_missing_equity_mode_keeps_current(self):
        """审查高危回归：equity_mode 缺字段/空值（Form(\"follow\") 旧语义）不得静默刷成 follow。"""
        self.db.set_config("equity_mode", "fixed")
        form = {k: v for k, v in self.params_form().items() if k != "equity_mode"}
        r = self.post("/admin/params", **form)
        self.assertIn(r.status_code, (200, 303))
        self.assertEqual(self._cfg().equity_mode, "fixed")
        # 空串经 Starlette 变 None，与缺字段同口径：保持原值
        r = self.post("/admin/params", **self.params_form(equity_mode=""))
        self.assertIn(r.status_code, (200, 303))
        self.assertEqual(self._cfg().equity_mode, "fixed")

    def test_params_missing_funds_field_keeps_current(self):
        """审查高危回归：被 JS disabled 的资金字段不随表单提交 → 缺键 = 保持原值。"""
        self.db.set_config("equity", 250.0)
        self.db.set_config("equity_min", 33.0)
        # 表单只带 equity（equity_min 被 JS disabled 的形态）：equity 更新、equity_min 不动
        form = {k: v for k, v in self.params_form().items() if k != "equity_min"}
        r = self.post("/admin/params", **form)
        self.assertIn(r.status_code, (200, 303))
        self.assertEqual(self._cfg().equity_min, 33.0)
        self.assertEqual(self._cfg().equity, 100.0)
        # 反过来只带 equity_min：equity 保持上一笔的 100
        form = {k: v for k, v in self.params_form().items() if k != "equity"}
        r = self.post("/admin/params", **form)
        self.assertIn(r.status_code, (200, 303))
        self.assertEqual(self._cfg().equity, 100.0)

    def test_params_saves_modes_and_booleans(self):
        """参数页只管数字/口径：方向与保护类开关**逐键不变**（2026-10-19 事故回归）。

        事故：有人在参数页点「保存全部参数」，三个复选框未勾 → enable_long /
        enable_shorts / heartbeat_push 被整份写 0，线上方向开关被静默关闭。
        所以这里先预置成「非默认值」，再 POST 一张不含这四个键的表单，
        断言四键**一个都没被碰**。
        """
        for key, val in (("enable_long", 0), ("enable_shorts", 0),
                         ("heartbeat_push", 1), ("agent_expiry_cleanup", 0)):
            self.db.set_config(key, val)
        r = self.post("/admin/params", **self.params_form(equity_mode="fixed",
                                                          equity_min="20"))
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._cfg().equity_mode, "fixed")
        self.assertEqual(self._cfg().equity_min, 20.0)
        for key, want in (("enable_long", "0"), ("enable_shorts", "0"),
                          ("heartbeat_push", "1"), ("agent_expiry_cleanup", "0")):
            self.assertEqual(str(self.db.get_config_str(key)), want,
                             f"参数 POST 不许改 {key}")
        self.assertFalse(self._cfg().enable_long)
        self.assertFalse(self._cfg().enable_shorts)
        self.assertTrue(self._cfg().heartbeat_push)
        self.assertFalse(self._cfg().agent_expiry_cleanup)
        html = self.body(tab="params")
        # 单选钮：fixed 选中（不再是 <select> 的 value='fixed' selected）
        self.assertIn("name='equity_mode' value='fixed' checked", html)
        self.assertNotIn("允许做空", html, "方向开关已迁到「开关」页")

    def test_params_rejects_non_number_and_writes_nothing(self):
        snap_before = self.db.get_config()
        r = self.post("/admin/params", **self.params_form(risk_pct="abc"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("单笔风险", r.text)
        self.assertIn("不是数字", r.text)
        self.assertEqual(self.db.get_config(), snap_before, "校验失败时绝不能写库")
        self.assertEqual(self._cfg().risk_pct, 0.02)

    def test_params_rejects_out_of_range(self):
        snap_before = self.db.get_config()
        for over, needle in (
                ({"equity": "0"}, "账户权益基数"),
                ({"risk_pct": "5"}, "单笔风险"),
                ({"leverage": "0"}, "杠杆"),
                ({"equity_mode": "auto"}, "资金口径"),
                ({"short_regime_ma": "abc"}, "做空均线"),
                ({"equity_min": "-5"}, "资金下限")):
            r = self.post("/admin/params", **self.params_form(**over))
            self.assertEqual(r.status_code, 400, f"{over} 应被拒")
            self.assertIn(needle, r.text, f"{over} 的错误信息里应有「{needle}」")
        self.assertEqual(self.db.get_config(), snap_before, "校验失败时绝不能写库")

    def test_params_rejects_stop_min_above_stop_max(self):
        snap_before = self.db.get_config()
        r = self.post("/admin/params", **self.params_form(
            stop_min="0.09", stop_max="0.03"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能大于", r.text)
        self.assertEqual(self.db.get_config(), snap_before)

    def test_params_ignores_unknown_fields(self):
        data = self.params_form(**{"__proto__": "x"})
        r = self.post("/admin/params", **data)
        self.assertEqual(r.status_code, 303)


class TestSwitchTab(BaseCase):
    def test_switch_200_with_key_blocks(self):
        html = self.body(tab="switch")
        for s in ("只通知", "影子", "实盘", "kill switch", "推送开关",
                  "切到实盘会用真钱下单", "我已确认"):
            self.assertIn(s, html)

    def test_switch_live_without_confirm_is_400(self):
        self.deps.address_fn = lambda: ADDR
        self.deps.private_key_fn = lambda: "0xabc"
        r = self.post("/admin/switch/mode", mode="live")
        self.assertEqual(r.status_code, 400)
        self.assertIn("二次确认", r.text)
        self.assertEqual(self._mode(), "off", "校验失败不能改模式")

    def test_switch_live_missing_credentials_is_400(self):
        self.deps.address_fn = lambda: ""
        self.deps.private_key_fn = lambda: ""
        r = self.post("/admin/switch/mode", mode="live", confirm_live="1")
        self.assertEqual(r.status_code, 400)
        self.assertIn("HL_ACCOUNT_ADDRESS", r.text)
        self.assertIn("HL_PRIVATE_KEY", r.text)
        self.assertEqual(self._mode(), "off")

    def test_switch_live_with_confirm_and_credentials(self):
        self.deps.address_fn = lambda: ADDR
        self.deps.private_key_fn = lambda: "0xabc"
        r = self.post("/admin/switch/mode", mode="live", confirm_live="1")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._mode(), "live")
        html = self.body(tab="switch")
        self.assertIn("当前是<b>实盘</b>", html)

    def test_switch_dry_ok_and_clears_kill(self):
        self.db.hl_set_kill(True, "测试")
        r = self.post("/admin/switch/mode", mode="dry")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._mode(), "dry")
        self.assertFalse(self.db.hl_kill(), "切回 dry 应清掉 kill")

    def test_switch_rejects_bad_mode(self):
        r = self.post("/admin/switch/mode", mode="yolo")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._mode(), "off")

    def test_kill_roundtrip(self):
        self.assertFalse(self.db.hl_kill())
        r1 = self.post("/admin/switch/kill", on="1")
        self.assertEqual(r1.status_code, 303)
        self.assertTrue(self.db.hl_kill())
        r2 = self.post("/admin/switch/kill", on="0")
        self.assertEqual(r2.status_code, 303)
        self.assertFalse(self.db.hl_kill())
        html = self.body(tab="switch")
        self.assertIn("当前未触发", html)
        self.assertTrue(any(e["kind"] == "kill" for e in self.db.hl_recent_events(5)))

    def test_notify_switches_persist_new_keys(self):
        # 2026-10-19 审查中危回归：缺字段 = 保持原值。旧断言「没勾的键落 0」
        # 钉的正是会静默关推送的危险语义，随本次口径更新。
        self.db.set_config("fill_push", 1)
        self.post("/admin/switch/notify", heartbeat_push="1", report_push="1")
        self.assertEqual(self.db.get_config_str("heartbeat_push"), "1")
        self.assertEqual(self.db.get_config_str("report_push"), "1")
        # 没带的键保持原值（此前预置 1）
        self.assertEqual(self.db.get_config_str("fill_push"), "1")
        self.post("/admin/switch/notify", fill_push="1")
        self.assertEqual(self.db.get_config_str("heartbeat_push"), "1")
        self.assertEqual(self.db.get_config_str("report_push"), "1")
        self.assertEqual(self.db.get_config_str("fill_push"), "1")
        html = self.body(tab="switch")
        for s in ("每日心跳推送", "日报 / 周报推送", "成交回报推送"):
            self.assertIn(s, html)

    def test_switch_page_shows_credentials_state(self):
        html = self.body(tab="switch")
        self.assertIn("私钥：未配置", html)
        self.assertIn("已配置", html)          # 地址


class TestRecordsTab(BaseCase):
    def setUp(self):
        super().setUp()
        BAR = 1_700_000_000_000
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, "entry", 1, 0.005, px=100_000.0)
        self.db.hl_update_order("BTC_USDT", BAR, "entry", status="filled", oid=1,
                                filled_sz=0.005, avg_px=100_000.0)
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR + 100_000, "stop", -1, 0.005,
                                 status="sent")
        self.db.hl_upsert_fill(tid="t1", symbol="BTC_USDT", coin="BTC", side=1, sz=0.005,
                               px=100_000.0, fee=0.05, fee_token="USDT", dir="a",
                               closed_pnl=5.0, hash="t1", reduce_only=True,
                               ts=int(NOW * 1000), created_at=NOW)
        self.db.log_alert("BTC_USDT", BAR, "signal", "开多 BTC", True)
        self.db.hl_log_event("decision", "人工切换执行器模式 → dry", "BTC_USDT")

    def test_records_200_with_key_blocks(self):
        html = self.body(tab="records")
        for s in ("下单记录", "成交", "信号", "提醒日志", "执行器事件",
                  "hl_orders", "hl_fills"):
            self.assertIn(s, html)

    def test_records_signal_block_after_fills(self):
        """信号块必须在「成交」之后、提醒日志之前（与总览原顺序一致）。"""
        html = self.body(tab="records")
        i_orders = html.index("下单记录（hl_orders")
        i_fills = html.index("成交（hl_fills")
        i_signals = html.index("<h2>信号</h2>")
        i_alerts = html.index("提醒日志（alert_logs")
        i_events = html.index("执行器事件（hl_events")
        self.assertLess(i_orders, i_fills)
        self.assertLess(i_fills, i_signals, "信号块应紧跟「成交」之后")
        self.assertLess(i_signals, i_alerts)
        self.assertLess(i_alerts, i_events)
        # 信号块用 _signals_html 渲染（与总览同一函数），标题只出现一次
        self.assertEqual(html.count("<h2>信号</h2>"), 1, "信号标题重复渲染了")

    def test_records_signal_block_renders_rows(self):
        # 信号数据源是 positions 表（payload["signals"]），不是 hl_orders
        self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1_700_000_000_000,
                                status="open", side=1, entry_price=100_000.0,
                                stop_price=97_000.0, stop_dist=0.03, contracts=6,
                                coin_qty=0.0006, notional=60.0, risk_u=18.0,
                                equity_at_entry=1000.0)
        html = self.body(tab="records")
        seg = html.split("<h2>信号</h2>", 1)[1].split("<h2>", 1)[0]
        self.assertIn("<th>信号 bar</th>", seg)
        self.assertIn("BTC", seg)
        self.assertIn("持仓中", seg)                    # status=open → _ST_TXT
        self.assertNotIn("暂无信号", seg)

    def test_records_shows_rows(self):
        html = self.body(tab="records")
        self.assertIn("开仓", html)
        self.assertIn("BTC", html)
        self.assertIn("100,000.00", html)
        self.assertIn("开多 BTC", html)
        self.assertIn("人工切换执行器模式", html)
        self.assertNotIn("暂无下单记录", html)

    def test_records_times_use_display_tz(self):
        html = self.body(tab="records")
        self.assertIn("北京时间", html)
        # NOW = 2025-10-09 08:53 UTC → 北京时间 10-09 16:53
        self.assertIn("10-09 16:53", html)
        self.assertNotIn("10-09 08:53", html)

    def test_records_default_20_rows_and_switches(self):
        """默认每页 20 行（手机优先）；?n=10 / ?n=50 可切；非法值回落默认。"""
        for i in range(60):
            self.db.log_alert("BTC_USDT", 1_700_000_000_000 + i, "signal", f"REC-{i:03d}", True)
        html = self.body(tab="records")
        self.assertIn("REC-059", html)
        self.assertIn("REC-040", html)
        self.assertNotIn("REC-039", html, "默认应只渲染 20 行")
        html10 = self.body(tab="records", n=10)
        self.assertIn("REC-050", html10)
        self.assertNotIn("REC-049", html10)
        html50 = self.body(tab="records", n=50)
        self.assertIn("REC-010", html50)
        self.assertNotIn("REC-009", html50, "?n=50 应渲染 50 行")
        html_bad = self.body(tab="records", n="999")
        self.assertIn("REC-040", html_bad)
        self.assertNotIn("REC-039", html_bad, "非法 n 应回落默认 20")

    def test_records_untouched_by_render_errors(self):
        class BrokenDB:
            """把 db 换成一个坏对象：面板必须降级成空表而不是 500。"""
        class FakeBroken:
            def __getattr__(self, name):
                raise RuntimeError("boom: db 挂了")
        self.deps.db = FakeBroken()
        html = self.body(tab="records")
        for s in ("暂无下单记录", "暂无成交记录", "暂无信号",
                  "暂无提醒日志", "暂无执行器事件"):
            self.assertIn(s, html)


class TestMobileNoJs(BaseCase):
    def test_only_inline_native_script_on_params(self):
        """无框架、无外部资源：唯一的 <script> 是参数页资金口径显隐（内联原生 JS）。

        （2026-10-19 之前所有 Tab 都是零 JS；资金口径显隐是用户明确要求的内联
        原生 JS，无 JS 时两个资金字段都可见 —— 回退可用。）
        """
        for tab in ("overview", "switch", "records"):
            html = self.body(tab=tab)
            self.assertNotIn("<script", html.lower(), f"{tab} 不该有 <script>")
            self.assertNotIn("javascript:", html.lower())
        html = self.body(tab="params")
        self.assertEqual(html.lower().count("<script"), 1, "参数页只允许一段内联脚本")
        self.assertIn("input[name='equity_mode']:checked", html)
        self.assertIn("data-mode", html)
        # 内联脚本必须是原生（没有任何库/CND/import/src）
        self.assertNotIn("<script src", html.lower())
        self.assertNotIn("import ", html.lower())

    def test_no_external_resources(self):
        """页面必须零外部资源：只允许根相对的自家 <link>（图标/manifest 就是这套）。

        口径：<link> 不是禁用，**跨域**的 <link> 才是。所以先剔除 href 以 / 开头的
        同源链接，剩下的（http://、//cdn…、相对路径）任一条出现都算外部资源。
        """
        import re
        for tab in ("overview", "params", "switch", "records"):
            html = self.body(tab=tab).lower()
            for bad in (" src=", "<img", "cdn.", "<iframe"):
                self.assertNotIn(bad, html, f"{tab} 含外部资源 {bad}")
            links = re.findall(r"<link [^>]*>", html)
            for tag in links:
                self.assertIn("href='/", tag, f"{tab} 的 <link> 必须根相对（同源）：{tag}")
                self.assertNotIn("http", tag, f"{tab} 的 <link> 不许带协议/域名：{tag}")

    def test_font_size_at_least_13px(self):
        html = self.body(tab="params")
        self.assertIn("font-size:13px", html)
        self.assertIn("table{font-size:13px}", html)
        self.assertIn("font-size:13.5px", html)

    def test_mobile_viewport_and_horizontal_scroll(self):
        html = self.body(tab="records")
        self.assertIn("width=device-width,initial-scale=1", html)
        self.assertIn("overflow-x:auto", html)

    def test_head_has_home_screen_icon_links(self):
        """手机「添加到主屏」：面板 <head> 必须带 apple-touch-icon / icon / manifest / theme-color。"""
        html = self.body(tab="overview")
        self.assertIn("<link rel='apple-touch-icon' sizes='180x180' href='/apple-touch-icon.png'>", html)
        self.assertIn("<link rel='manifest' href='/site.webmanifest'>", html)
        self.assertIn("<meta name='theme-color' content='#0d1117'>", html)
        # 路径是根相对的（反代换域名后照样同源），不能是写死域名或 ../ 相对
        self.assertNotIn("apple-touch-icon.png' href='http", html)
        # 必须落在 <head> 里，不能跑到 body
        self.assertLess(html.index("apple-touch-icon"), html.index("</head>"))

    def test_page_renders_home_screen_icons(self):
        """_page() 本身（不经 HTTP）也要带 icon 链接 —— 确认页/HL 页共用同一份常量。"""
        import app.admin_ui as admin_mod
        html = bytes(admin_mod._page("tk", "overview", "", "<div>x</div>").body).decode()
        self.assertIn("rel='apple-touch-icon'", html)
        self.assertIn("href='/icon-192.png'", html)
        self.assertIn("href='/icon-512.png'", html)
        self.assertIn("href='/favicon.ico'", html)

    def test_dark_theme_matches_existing_panel(self):
        html = self.body(tab="overview")
        for c in ("#0f1115", "#171a21", "#262a33", "#e6e6e6"):
            self.assertIn(c, html)

    def test_no_emoji_spray_in_status_strip(self):
        html = self.body(tab="overview")
        strip = html.split("<div class='bar'>")[1].split("</div>")[0]
        self.assertEqual(strip.count("&#9679;"), 5)      # 五个状态徽标各一个点


class TestLegacyRedirects(BaseCase):
    def test_hl_redirects_302_and_keeps_k(self):
        r = self.client.get("/hl", params={"k": TOKEN, "refresh": 30}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], f"/admin?tab=overview&k={TOKEN}")

    def test_admin_rules_redirects_3xx(self):
        r = self.get("/admin/rules")
        self.assertIn(r.status_code, (301, 302, 303))
        self.assertEqual(r.headers["location"], f"/admin?tab=params&k={TOKEN}")

    def test_admin_logs_redirects_3xx(self):
        r = self.get("/admin/logs")
        self.assertIn(r.status_code, (301, 302, 303))
        self.assertEqual(r.headers["location"], f"/admin?tab=records&k={TOKEN}")

    def test_redirect_without_token_passes_through(self):
        r = self.client.get("/hl", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin?tab=overview", r.headers["location"])

    def test_api_hl_status_still_200(self):
        r = self.client.get("/api/hl/status")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["schema_version"], 1)
        for k in ("ts", "mode", "kill", "network", "account", "positions",
                  "orders_open", "orders_recent", "fills", "signals", "system",
                  "curve", "events"):
            self.assertIn(k, j, f"JSON schema 少了 {k}")
        self.assertIn("equity", j["account"])

    def test_api_hl_equity_and_snapshot_still_200(self):
        for path in ("/api/hl/equity", "/api/hl/snapshot"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("ts", r.json())

    def test_legacy_rules_post_still_writes_db(self):
        # /admin/rules 的 POST 由 app.main 保留；本用例只挂 admin_ui 路由 → 405
        r = self.client.post("/admin/rules", data={"equity": "999", "risk_pct": "0.02",
                                                   "leverage": "5", "stop_min": "0.015",
                                                   "stop_max": "0.1", "k": TOKEN})
        self.assertEqual(r.status_code, 405, "旧 POST 不在这个路由里，由 app.main 提供")


class TestFullAppWiring(unittest.TestCase):
    """挂到真实的 app.main 上，验证合并没有弄坏别的东西。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="admin_full_")
        os.environ["DB_PATH"] = os.path.join(self.tmp, "full.db")
        os.environ["ENABLE_SCHEDULER"] = "false"
        os.environ["ADMIN_TOKEN"] = TOKEN
        os.environ["TOKEN_SECRET"] = "full-secret"
        os.environ["BASE_URL"] = "https://full.example"
        os.environ["DRY_RUN"] = "true"
        os.environ["HL_ACCOUNT_ADDRESS"] = ADDR
        # app.main 在 import 时就会构造真实 HlInfo 并打网络，换成假的避免真请求
        import app.hl.info as _hi
        self._real_hl_info_cls = _hi.HlInfo
        _hi.HlInfo = _FakeHlInfo

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        import app.hl.info as _hi
        _hi.HlInfo = self._real_hl_info_cls
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key in ("DB_PATH", "ENABLE_SCHEDULER", "ADMIN_TOKEN", "TOKEN_SECRET",
                    "BASE_URL", "DRY_RUN", "HL_ACCOUNT_ADDRESS"):
            os.environ.pop(key, None)

    def _mod(self):
        # Config 的默认值在类体里读 env，所以必须连 app.config 一起 reload
        importlib.reload(importlib.import_module("app.config"))
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        return mod

    def test_all_routes_registered(self):
        mod = self._mod()
        paths = set(mod.app.openapi().get("paths", {}).keys())
        for p in ("/health", "/admin", "/admin/params", "/admin/switch/mode",
                  "/admin/switch/strategy", "/admin/switch/notify", "/admin/switch/kill",
                  "/admin/rules", "/admin/logs", "/admin/tick",
                  "/admin/hl/mode", "/admin/hl/kill", "/c/{pid}",
                  "/hl", "/api/hl/status", "/api/hl/equity", "/api/hl/snapshot"):
            self.assertIn(p, paths, f"路由 {p} 未注册")

    def test_admin_panel_200_and_old_status_page_gone(self):
        mod = self._mod()
        with TestClient(mod.app) as c:
            r = c.get("/admin", params={"k": TOKEN})
            self.assertEqual(r.status_code, 200)
            self.assertIn("crypto-ma-alert 管理面板", r.text)
            for tab in ("overview", "params", "switch", "records"):
                self.assertIn(f"tab={tab}", r.text)

    def test_hl_302_and_api_status_200_in_full_app(self):
        mod = self._mod()
        with TestClient(mod.app) as c:
            r = c.get("/hl", params={"k": TOKEN}, follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers["location"], f"/admin?tab=overview&k={TOKEN}")
            r = c.get("/api/hl/status", params={"k": TOKEN})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["schema_version"], 1)

    def test_old_endpoints_redirect_in_full_app(self):
        mod = self._mod()
        with TestClient(mod.app) as c:
            for path, tab in (("/admin/rules", "params"), ("/admin/logs", "records")):
                r = c.get(path, params={"k": TOKEN}, follow_redirects=False)
                self.assertEqual(r.status_code, 302, path)
                self.assertEqual(r.headers["location"], f"/admin?tab={tab}&k={TOKEN}")

    def test_legacy_rules_post_compatibility(self):
        mod = self._mod()
        with TestClient(mod.app) as c:
            # 2026-10-19 事故回归：legacy POST **缺字段 = 保持原值**（显式 0 才关）。
            # 旧断言 assertFalse(enable_shorts) 钉的正是「不传 = 0」这条危险语义。
            r = c.post("/admin/rules", data={
                "equity": "777", "risk_pct": "0.03", "leverage": "4",
                "stop_min": "0.02", "stop_max": "0.09",
                "short_regime_ma": "120", "k": TOKEN},
                follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("tab=params", r.headers["location"])
            self.assertEqual(mod.current_config().equity, 777.0)
            self.assertEqual(mod.current_config().risk_pct, 0.03)
            self.assertEqual(mod.current_config().short_regime_ma, 120)
            self.assertTrue(mod.current_config().enable_shorts, "缺字段不许把做空关掉")
            # 显式 0 仍然要能关掉（保留兼容能力）
            r = c.post("/admin/rules", data={
                "equity": "777", "risk_pct": "0.03", "leverage": "4",
                "stop_min": "0.02", "stop_max": "0.09", "short_regime_ma": "120",
                "enable_long": "0", "enable_shorts": "0", "k": TOKEN},
                follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertFalse(mod.current_config().enable_shorts)
            self.assertFalse(mod.current_config().enable_long)

    def test_legacy_rules_validates_before_writing(self):
        """审查中危回归：先全部校验、后统一写库 —— 非法值 400 时不能有半截落库；
        short_regime_ma 缺字段 = 保持原值（旧缺省 200 会静默重置做空均线）。"""
        mod = self._mod()
        with TestClient(mod.app) as c:
            keys = ("equity", "enable_long", "short_regime_ma")
            before = {k: mod.db.get_config().get(k) for k in keys}
            r = c.post("/admin/rules", data={
                "equity": "888", "risk_pct": "0.03", "leverage": "4",
                "stop_min": "0.02", "stop_max": "0.09", "short_regime_ma": "abc",
                "enable_long": "0", "k": TOKEN}, follow_redirects=False)
            self.assertEqual(r.status_code, 400)
            self.assertEqual({k: mod.db.get_config().get(k) for k in keys}, before,
                             "400 时不能有半截写库")
            r = c.post("/admin/rules", data={
                "equity": "777", "risk_pct": "0.03", "leverage": "4",
                "stop_min": "0.02", "stop_max": "0.09", "k": TOKEN},
                follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(mod.db.get_config().get("short_regime_ma"),
                             before["short_regime_ma"], "缺 ma 字段不许重置为 200")

    def test_legacy_hl_mode_and_kill_curl_compat(self):
        mod = self._mod()
        with TestClient(mod.app) as c:
            r = c.post("/admin/hl/mode", data={"mode": "dry", "k": TOKEN},
                       follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(mod.hl_mode(), "dry")
            self.assertIn("tab=switch", r.headers["location"])
            r = c.post("/admin/hl/kill", data={"on": "1", "k": TOKEN},
                       follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertTrue(mod.db.hl_kill())

    def test_equity_mode_db_overrides_env(self):
        """Config 现在能覆盖 equity_mode / equity_min（面板资金组要真的生效）。"""
        mod = self._mod()
        mod.db.set_config("equity_mode", "fixed")
        mod.db.set_config("equity_min", 55.0)
        self.assertEqual(mod.current_config().equity_mode, "fixed")
        self.assertEqual(mod.current_config().equity_min, 55.0)
        self.assertEqual(mod.hl_params().equity_mode, "fixed")

    def test_confirm_page_untouched(self):
        mod = self._mod()
        pid = mod.db.create_position(symbol="BTC_USDT", signal_bar_ms=1_700_000_200_000,
                                     status="open", side=1, entry_price=100_000.0,
                                     stop_price=97_000.0, stop_dist=0.03, contracts=6,
                                     coin_qty=0.0006, notional=60.0, risk_u=18.0,
                                     equity_at_entry=1000.0)
        tok = mod.engine.sign_token(mod.config.token_secret, pid, "view")
        with TestClient(mod.app) as c:
            r = c.get(f"/c/{pid}", params={"a": "view", "t": tok})
            self.assertEqual(r.status_code, 200)
            self.assertIn("持仓中", r.text)
            self.assertNotIn("crypto-ma-alert 管理面板", r.text)

    def test_health_still_reports_hl_mode(self):
        # 无 k 的 /health 不再暴露 hl_mode/hl_kill（公网站点）；带 k 才给
        mod = self._mod()
        tok = mod.config.admin_token
        with TestClient(mod.app) as c:
            j = c.get("/health").json()
            self.assertEqual(j["status"], "ok")
            self.assertNotIn("hl_mode", j)
            self.assertNotIn("hl_kill", j)
            j2 = c.get(f"/health?k={tok}").json()
            self.assertEqual(j2["status"], "ok")
            self.assertIn("hl_mode", j2)
            self.assertIn("hl_kill", j2)


class TestSwitchFlagParsing(BaseCase):
    """开关的取值解析：字符串 '0' 必须落 0。

    只写 `1 if val else 0` 时，字符串 "0" 在 Python 里是真值 → 想把推送「关掉」的调用
    反而会打开它（fail-dangerous），这是我在独立验收里抓到的真缺陷。
    """

    def test_notify_off_string_stores_zero(self):
        r = self.post("/admin/switch/notify", fill_push="0", heartbeat_push="0", report_push="1")
        self.assertIn(r.status_code, (200, 303))
        for key, want in (("fill_push", "0"), ("heartbeat_push", "0"), ("report_push", "1")):
            got = str(self.db.get_config_str(key))
            self.assertIn(want, got, f"{key} 落了 {got!r}")

    def test_notify_false_word_stores_zero(self):
        self.post("/admin/switch/notify", fill_push="false")
        self.assertIn("0", str(self.db.get_config_str("fill_push")))

    def test_params_bool_zero_string_stores_zero(self):
        """方向开关现在归 /admin/switch/strategy：字符串 '0' 必须落 0。"""
        self.db.set_config("enable_shorts", 1)
        r = self.post("/admin/switch/strategy", enable_shorts="0")
        self.assertIn(r.status_code, (200, 303))
        self.assertIn("tab=switch", r.headers["location"])
        self.assertEqual(str(self.db.get_config_str("enable_shorts")), "0")
        self.assertFalse(self._cfg().enable_shorts)

    def test_params_bool_one_still_stores_one(self):
        self.db.set_config("enable_long", 0)
        r = self.post("/admin/switch/strategy", enable_long="1")
        self.assertIn(r.status_code, (200, 303))
        self.assertEqual(str(self.db.get_config_str("enable_long")), "1")
        self.assertTrue(self._cfg().enable_long)

    def test_strategy_weird_value_direction_is_fail_closed(self):
        """方向开关：识别不了的值按「关」（安全侧；旧参数页语义本就如此）。"""
        self.db.set_config("enable_long", 1)
        r = self.post("/admin/switch/strategy", enable_long="weird")
        self.assertIn(r.status_code, (200, 303))
        self.assertEqual("0", str(self.db.get_config_str("enable_long")))

    def test_strategy_weird_value_cleanup_is_fail_open(self):
        """到期自动清仓：识别不了的值按「开」（沿用旧 _cleanup_on 的 H3 取舍）。

        漏清仓的代价（到期后仓位想平也平不了）远大于误开（可能提前平一笔）。
        面板本身只提交 0/1，这条只防脚本发垃圾值时把保护静默关掉。
        """
        self.db.set_config("agent_expiry_cleanup", 0)
        r = self.post("/admin/switch/strategy", agent_expiry_cleanup="weird")
        self.assertIn(r.status_code, (200, 303))
        self.assertEqual("1", str(self.db.get_config_str("agent_expiry_cleanup")))

    def test_strategy_explicit_zero_still_turns_cleanup_off(self):
        self.db.set_config("agent_expiry_cleanup", 1)
        self.post("/admin/switch/strategy", agent_expiry_cleanup="0")
        self.assertEqual("0", str(self.db.get_config_str("agent_expiry_cleanup")))

    def test_strategy_empty_post_redirects_without_saved_msg(self):
        """审查中危：什么都没带 → 不写库、不记事件，也不能弹「已保存」假横幅。"""
        r = self.post("/admin/switch/strategy", k=TOKEN)
        self.assertIn(r.status_code, (200, 303))
        loc = unquote(r.headers["location"])
        self.assertIn("无改动", loc)
        self.assertNotIn("已保存", loc)

    def test_notify_partial_post_keeps_missing_keys(self):
        """审查中危回归：推送开关缺字段 = 保持原值（旧行为缺键 → 0）。"""
        for key in ("heartbeat_push", "report_push", "fill_push"):
            self.db.set_config(key, 1)
        self.post("/admin/switch/notify", fill_push="0")
        self.assertEqual("0", str(self.db.get_config_str("fill_push")))
        self.assertEqual("1", str(self.db.get_config_str("heartbeat_push")))
        self.assertEqual("1", str(self.db.get_config_str("report_push")))


class TestStrategyCard(BaseCase):
    """开关页「策略与保护」卡 + /admin/switch/strategy（2026-10-19 迁入）。

    事故背景：2026-10-18/19 有人在参数页提交「保存全部参数」，三个复选框未勾 →
    enable_long / enable_shorts / heartbeat_push 被整份写 0，线上方向开关被静默
    关闭。修复：方向/保护类开关撤出参数页，改由本卡负责，且：
      - 未勾选（只提交 hidden 的 0）= 关（必须能真的关掉）
      - 字段完全缺失（脚本/旧书签只带一个键）= 其余键保持原值
    """

    def test_switch_page_has_strategy_card_with_three_checkboxes(self):
        self.db.set_config("enable_long", 0)
        self.db.set_config("enable_shorts", 1)
        self.db.set_config("agent_expiry_cleanup", 1)
        html = self.body(tab="switch")
        self.assertIn("策略与保护", html)
        self.assertIn("action='/admin/switch/strategy'", html)
        for key, label in (("enable_long", "允许做多"), ("enable_shorts", "允许做空"),
                           ("agent_expiry_cleanup", "到期前自动清仓")):
            self.assertIn(label, html)
            self.assertIn(f"name='{key}'", html)
            # hidden 兜底（未勾选也要提交 0）
            self.assertIn(f"<input type='hidden' name='{key}' value='0'>", html)
        # 状态反映 cfg：做多关、做空开、清仓开
        self.assertNotRegex(html, r"name='enable_long' value='1' checked")
        self.assertRegex(html, r"name='enable_shorts' value='1' checked")
        self.assertRegex(html, r"name='agent_expiry_cleanup' value='1' checked")
        self.assertIn("当前 关", html)
        # 卡片在「执行模式」之后、「推送开关」之前
        self.assertLess(html.index("执行模式"), html.index("策略与保护"))
        self.assertLess(html.index("策略与保护"), html.index("推送开关"))

    def test_unchecked_hidden_zero_really_turns_off_and_back_on(self):
        """未勾选（浏览器只发 hidden=0）→ 关；勾选（0 再 1）→ 开。"""
        self.db.set_config("enable_long", 1)
        self.db.set_config("enable_shorts", 1)
        # 未勾选：只提交 hidden 的 0
        r = self.post("/admin/switch/strategy", enable_long="0", enable_shorts="0",
                      agent_expiry_cleanup="0")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("enable_long")), "0")
        self.assertEqual(str(self.db.get_config_str("enable_shorts")), "0")
        self.assertEqual(str(self.db.get_config_str("agent_expiry_cleanup")), "0")
        self.assertFalse(self._cfg().enable_long)
        html = self.body(tab="switch")
        self.assertNotRegex(html, r"name='enable_long' value='1' checked")
        # 改回开：浏览器提交 hidden=0 + checkbox=1（同名多值取最后一个）
        r = self.client.post(
            "/admin/switch/strategy",
            content="k={}&enable_long=0&enable_long=1".format(TOKEN),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("enable_long")), "1")
        self.assertTrue(self._cfg().enable_long)
        # 事件要落库，写清是哪个键
        msgs = [e["message"] for e in self.db.hl_recent_events(5)]
        self.assertTrue(any("策略与保护" in m for m in msgs), msgs)

    def test_partial_post_keeps_other_keys(self):
        """缺字段 = 不变：只带一个键时其余两键绝不能被顺手写成 0。"""
        self.db.set_config("enable_long", 1)
        self.db.set_config("enable_shorts", 1)
        self.db.set_config("agent_expiry_cleanup", 1)
        r = self.post("/admin/switch/strategy", enable_shorts="0")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("enable_shorts")), "0")
        self.assertEqual(str(self.db.get_config_str("enable_long")), "1",
                         "没提交的键必须保持原值")
        self.assertEqual(str(self.db.get_config_str("agent_expiry_cleanup")), "1",
                         "没提交的键必须保持原值")

    def test_empty_post_writes_nothing(self):
        self.db.set_config("enable_long", 1)
        before = self.db.get_config()
        r = self.post("/admin/switch/strategy")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.get_config(), before, "什么都没带时不许写库")

    def test_params_post_cannot_touch_direction_switches(self):
        """参数 POST 带上这些键也不会生效（参数页已不接受这四个键）。"""
        for key in ("enable_long", "enable_shorts", "heartbeat_push",
                    "agent_expiry_cleanup"):
            self.db.set_config(key, 1)
        form = self.params_form()
        form.update({"enable_long": "0", "enable_shorts": "0",
                     "heartbeat_push": "0", "agent_expiry_cleanup": "0"})
        r = self.post("/admin/params", **form)
        self.assertEqual(r.status_code, 303)
        for key in ("enable_long", "enable_shorts", "heartbeat_push",
                    "agent_expiry_cleanup"):
            self.assertEqual(str(self.db.get_config_str(key)), "1",
                             f"参数 POST 不该改 {key}")

    def test_strategy_post_requires_token(self):
        r = self.client.post("/admin/switch/strategy", data={"enable_long": "0"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
