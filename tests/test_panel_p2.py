"""面板 P2 优化的回归测试（规格：spec-panel-p2.md，2026-10-25）。

四项功能逐项钉词面（铁律 7：断言看渲染 HTML，不看源码；铁律 8：/hl 与 /admin
双入口各断言一遍 —— P1 复核教训：只打 /hl 的用例，/admin 没接参数上线即失效）：
1. 账户卡分层 + 消重复：顶层 3 关键数、次级项一项不少；「浮盈浮亏」「资金费（持仓期内」
   从账户卡删除（每仓浮盈/资金费由仓位表唯一承载）。
2. 仓位表浮盈加 ROE%（浮盈 ÷ 保证金，对齐 HL 网页习惯；保证金 0 只显 U）+
   窄屏关键列（media query 隐藏列，被隐藏字段以 tr.subm muted 小字行补足，零 JS）。
3. 三标的动态 + 数据源健康 + 已处理到 合并为一张三标的卡（每标的加数据源/最后成功/
   已处理到小字行；两个旧区块标题整页 0 处）。
4. 记录页两个过滤池扩到 max(n*6, 2000)：池外真实事件重新可见。

全部离线：临时 sqlite + 假 info，不触网。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NOW = 1760000000.0
TOKEN = "p2-tk"
ADDR = "0xTestAccount000000000000000000000000"

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.db import DB
    from app.hl.views import DashboardDeps, build_router
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = DB = None
    DashboardDeps = build_router = None


def fake_snapshot(equity=995.06, positions=None):
    """统一账户快照（面板渲染零网络）。默认空仓。"""
    return {"ts": NOW, "address": ADDR, "equity_total": equity, "equity": equity,
            "spot_usdc": 839.34, "unified": True, "withdrawable": 0.0,
            "spot_hold": 15.0, "margin_used": 155.72, "notional": 778.60,
            "unrealized_pnl": 0.0, "today_realized": 2.10,
            "total_realized": -14.38, "funding_total": 0.57, "fees_total": 1.23,
            "positions": positions or [], "open_orders": [],
            "orders_trusted": True}


class FakeInfo:
    def all_mids(self):
        return {}

    def __init__(self, snap=None):
        self._snap = snap if snap is not None else fake_snapshot()

    def snapshot(self, address, mids=None):
        return self._snap


def held_position(margin=155.72, upnl=7.51, entry=100_000.0, mark=101_000.0,
                  liq=96_917.45, funding=-0.57, coin="BTC"):
    """一个持仓中的 BTC 空仓快照行（字段名与 HlInfo.snapshot 同口径）。"""
    return {"coin": coin, "side": -1, "size": 0.00905, "entry_px": entry,
            "mark_px": mark, "position_value": 908.0, "liq_px": liq,
            "leverage": 5, "isolated": True, "margin_used": margin,
            "unrealized_pnl": upnl, "funding_since_open": funding}


class BaseCase(unittest.TestCase):
    """临时 DB + 同一个 app 挂 /hl 与 /admin 两套路由（模板：tests/test_panel_p1.py）。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="panel_p2_")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.info = FakeInfo()
        self.hl_deps = DashboardDeps(
            db=self.db, config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT"]})(),
            info=self.info, address_fn=lambda: ADDR, mode_fn=lambda: "live",
            now_fn=lambda: NOW)
        self.deps = AdminDeps(
            db=self.db, config_fn=lambda: Config(), hl_deps=self.hl_deps,
            mode_fn=lambda: "live", token_fn=lambda: TOKEN,
            address_fn=lambda: ADDR, private_key_fn=lambda: "",
            mids_fn=lambda: self.info.all_mids())
        app = FastAPI()
        app.include_router(build_router(self.hl_deps))
        app.include_router(build_admin_router(self.deps))
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 取数辅助：双入口是本单铁律，每个用例两个入口都要断言 ----
    def hl(self, **params):
        return self.client.get("/hl", params=params or None).text

    def admin(self, tab="overview", **params):
        p = {"tab": tab, "k": TOKEN}
        p.update(params)
        return self.client.get("/admin", params=p).text

    def both(self, tab="overview", **params):
        """（/hl 渲染, /admin 渲染）——总览类用例统一走这里。"""
        return self.hl(**params), self.admin(tab=tab, **params)

    @staticmethod
    def account_seg(html: str) -> str:
        """账户卡片段（顶层/次级/重复词断言只看这块，避免命中仓位表同名列）。"""
        return html.split("<div class='sec'>账户</div>", 1)[1].split("<h2>盈亏曲线", 1)[0]

    @staticmethod
    def pos_seg(html: str) -> str:
        """仓位表片段。"""
        return html.split("<h2>仓位</h2>", 1)[1].split("</table>", 1)[0]


# ---------------------------------------------------------------- 功能一：账户卡
class TestAccountLayer(BaseCase):
    TOP = ("账户总值", "可提余额")   # P3 R5：「累计已实现」从顶层删除（并入绩效卡 U 对照行）
    SUB = ("钱包地址", "资金分布（统一账户）", "持仓名义", "今日已实现", "净手续费（付出）")

    def test_top3_and_secondary_items_all_present(self):
        for html in self.both():
            seg = self.account_seg(html)
            for w in self.TOP:
                self.assertIn(w, seg, f"顶层关键数缺「{w}」（/hl 与 /admin 都要验）")
            for w in self.SUB:
                self.assertIn(w, seg, f"次级项缺「{w}」（搬位置不是删内容）")
            # 顶层大字结构（.acct-top）与次级 muted 小字（.acct-sub）都在
            self.assertIn("acct-top", seg)
            self.assertIn("acct-sub", seg)
            # P3 R4：次级项收进 <details>「资金明细」，默认收起
            self.assertIn("<details class='acct-details'><summary>资金明细</summary>", seg)

    def test_top3_values_match_old_caliber(self):
        # 累计/今日已实现是交易所 closedPnl 口径，来源是 hl_fills（不是快照字段）：
        # 落一笔 closed_pnl=-14.38 的平仓成交，让绩效卡「累计已实现」U 对照行有真值可钉。
        self.db.hl_upsert_fill(tid="f1", symbol="BTC_USDT", coin="BTC", side=-1,
                               sz=0.00905, px=101_000.0, fee=0.0, fee_token="USDT",
                               dir="Close Short", closed_pnl=-14.38, hash="0xf1",
                               reduce_only=True, ts=int(NOW * 1000), created_at=NOW)
        for html in self.both():
            seg = self.account_seg(html)
            self.assertIn("账户总值 <b>995.06 U</b>", seg)
            self.assertNotIn("累计已实现", seg, "P3 R5：账户卡顶层不再有「累计已实现」")
            self.assertIn("-14.38 U", seg, "今日已实现为交易所 closedPnl 口径（负数如实）")
            self.assertIn("824.34 U", seg, "统一账户可提 = 现货 − 仓位/挂单占用")
            self.assertNotIn("提现收 1U 手续费", seg, "手续费说明已删（2026-10-29）")
            self.assertNotIn("（提现收 1U 手续费）", html)
            # 全页唯一出处：盈亏曲线区块里的「上线以来」行
            curve_seg = html.split("<h2>盈亏曲线", 1)[1]
            self.assertIn("累计已实现", curve_seg)
            self.assertRegex(curve_seg, "累计已实现 <b class='[^']*'>-14\.38 U</b>",
                             "U 对照 = 交易所 closedPnl 口径（负数如实）")

    def test_dedup_rows_gone_from_account_but_position_columns_stay(self):
        self.info._snap = fake_snapshot(positions=[held_position()])
        for html in self.both():
            seg = self.account_seg(html)
            self.assertNotIn("浮盈浮亏", seg, "账户卡的「浮盈浮亏」行已删（每仓浮盈由仓位表唯一承载）")
            self.assertNotIn("资金费（持仓期内", seg, "账户卡的「资金费」行已删（同数字仓位表已唯一出现）")
            pos = self.pos_seg(html)
            self.assertIn("<th class='num'>浮盈浮亏</th>", pos, "仓位表浮盈列保留")
            self.assertIn("<th class='num hidem'>本单资金费</th>", pos, "仓位表资金费列保留")
            self.assertIn("+0.57 U", pos, "每仓资金费仍由仓位表渲染（fixture 收 0.57）")
            self.assertIn("+7.51 U", pos, "每仓浮盈仍由仓位表渲染")

    def test_spot_read_failure_warning_stays(self):
        snap = fake_snapshot()
        snap["spot_ok"] = False
        self.info._snap = snap
        for html in self.both():
            self.assertIn("⚠️ 现货余额读取失败", self.account_seg(html),
                          "现货读不到 → 总值不可信，这句必须喊出来")

    def test_non_unified_keeps_margin_used(self):
        snap = fake_snapshot()
        snap["unified"] = False
        snap["withdrawable"] = 839.34
        self.info._snap = snap
        for html in self.both():
            seg = self.account_seg(html)
            self.assertIn("已用保证金", seg)
            self.assertNotIn("资金分布（统一账户）", seg)


# ---------------------------------------------------------------- 功能二：仓位表
class TestPositionRoEAndNarrow(BaseCase):
    def setUp(self):
        super().setUp()
        self.info._snap = fake_snapshot(positions=[held_position()])

    def test_roe_percent_value(self):
        # 手算：ROE = 浮盈 ÷ 保证金 = 7.51 / 155.72 = 4.8224...% → +4.82%
        for html in self.both():
            pos = self.pos_seg(html)
            self.assertIn("+7.51 U · +4.82%", pos, "ROE% = 浮盈 ÷ 本仓保证金（HL 网页口径）")
            self.assertIn("浮盈 % = 浮盈 ÷ 保证金（ROE）", html, "口径落可见图例")

    def test_zero_margin_shows_u_only(self):
        self.info._snap = fake_snapshot(positions=[held_position(margin=0.0)])
        for html in self.both():
            pos = self.pos_seg(html)
            self.assertIn("+7.51 U</td>", pos, "保证金 0：只显 U")
            self.assertNotIn("+7.51 U · ", pos, "除数没了就不编百分比")

    def test_narrow_css_rules_shipped(self):
        # media query 类存在（渲染 HTML 的 <style> 里断言，零 JS、纯 CSS 显隐）
        for html in self.both():
            self.assertIn("tr.subm{display:none}", html, "宽屏隐藏小字行")
            self.assertIn(".hidem{display:none}tr.subm{display:table-row}", html,
                          "窄屏：隐藏列、显示小字行")

    def test_hidden_fields_in_sub_row_match_table(self):
        for html in self.both():
            pos = self.pos_seg(html)
            m = re.search(r"<tr class='subm'><td colspan='13'>([^<]+)</td></tr>", pos)
            self.assertIsNotNone(m, "每个持仓行下要有 muted 小字行（窄屏补足隐藏字段）")
            sub = m.group(1)
            # 小字与表格同一事实：数值逐项一致（fixture：0.00905 / 5x 逐 / 155.72 / 96,917.45 / +0.57）
            self.assertIn("数量 0.00905", sub)
            self.assertIn("杠杆 5x 逐", sub)
            self.assertIn("保证金 155.72 U", sub)
            self.assertIn("强平 96,917.45", sub)
            self.assertIn("本单资金费 +0.57 U", sub)

    def test_wide_screen_keeps_13_columns(self):
        for html in self.both():
            thead = self.pos_seg(html).split("<thead>", 1)[1].split("</thead>", 1)[0]
            self.assertEqual(thead.count("<th"), 13, "宽屏表格不变：仍是 13 列")
            # 窄屏隐藏的恰好是 5 列：数量/杠杆/保证金/强平价/本单资金费
            self.assertEqual(thead.count("hidem"), 5)

    def test_no_script_added(self):
        for html in self.both():
            self.assertNotIn("<script", html.lower(), "零 JS")


# ---------------------------------------------------------------- 功能三：三标的合并
class TestSymbolCardMerge(BaseCase):
    BAR_MS = 1_700_000_000_000

    def setUp(self):
        super().setUp()
        self.db.set_symbol_state("BTC_USDT", price=100_000.0)
        self.db.set_last_bar("BTC_USDT", self.BAR_MS)
        self.db.mark_feed("BTC_USDT", "hl-candle", True)

    def test_merged_line_present_and_old_tables_gone(self):
        for html in self.both():
            # 2026-10-29 精简：数据源正常时不再渲染小字行（异常才出现）
            self.assertNotIn("数据源 hl-candle 正常", html)
            self.assertNotIn("<div class='sec'>数据源健康</div>", html,
                             "「数据源健康」旧表已删（整页 0 处）")
            self.assertNotIn("<div class='sec'>已处理到</div>", html,
                             "「已处理到」旧表已删（整页 0 处）")
            # 区块标题锚点不动、头部小字保留
            self.assertIn("<h2>三标的动态", html)
            self.assertIn("每 30 分钟随信号 tick 更新", html)

    def test_feed_failure_is_prominent(self):
        # 先清掉 setUp 里的正常行再标失败：mark_feed 失败分支不清 last_ok_at
        #（沿用旧口径），所以要看到失败态必须从零开始。
        self.db.conn.execute("DELETE FROM feed_health")
        self.db.conn.commit()
        self.db.mark_feed("BTC_USDT", "hl-candle", False, error="HTTP 503")
        for html in self.both():
            self.assertIn("数据源 hl-candle 失败（最后错误 ", html)
            self.assertRegex(html, r'whyline neg">数据源 hl-candle 失败（最后错误 \d{2}:\d{2}）',
                             "失败态用 neg 样式醒目（fail-closed 是最该看见的异常）")

    def test_missing_feed_shown_truthfully(self):
        self.db.conn.execute("DELETE FROM feed_health")
        self.db.conn.commit()
        for html in self.both():
            self.assertIn("数据源 无记录 · 已处理到 ", self.account_seg_and_sym(html),
                          "字段缺失如实显示，不编「正常」")

    def account_seg_and_sym(self, html):
        return html.split("三标的动态", 1)[1]


# ---------------------------------------------------------------- 功能四：过滤池扩容
class TestEventPoolWidened(BaseCase):
    N_ROUTINE = 130                     # > 旧池 120：例行行全灌满也能露出池外真实事件

    def test_real_event_visible_beyond_routine_wall(self):
        # fixture：1 条 decision 最先落库（最老），随后 130 条例行心跳把旧池（120）灌满
        # —— 旧逻辑池外 → 主页恒空态；扩到 2000 后必须可见。
        self.db.hl_log_event("decision", "人工切换执行器模式 → dry", "BTC_USDT")
        for i in range(self.N_ROUTINE):
            self.db.hl_log_event("heartbeat", f"执行器心跳 {i}")
        html = self.admin(tab="records")
        self.assertIn("人工切换执行器模式 → dry", html,
                      "执行器事件池扩容后，池外真实事件回到主页")
        self.assertIn("执行器事件（hl_events · 最近 1 条）", html, "标题行数仍如实")

    def test_alert_pool_widened_same_way(self):
        self.db.log_alert("BTC_USDT", 1_700_000_000_000, "entry", "开多 BTC（真实提醒）", True)
        for i in range(self.N_ROUTINE):
            self.db.log_alert(None, None, "heartbeat", f"每日心跳推送 {i}", True)
        html = self.admin(tab="records")
        self.assertIn("开多 BTC（真实提醒）", html, "提醒日志池同一修法")
        self.assertIn("提醒日志（alert_logs · 最近 1 条）", html)

    def test_empty_state_logic_kept(self):
        # 池里全是例行行：仍显示「被滤光」而非「没有数据」（真没有 vs 被滤光两种文案）
        for i in range(self.N_ROUTINE):
            self.db.hl_log_event("heartbeat", f"执行器心跳 {i}")
        html = self.admin(tab="records")
        self.assertIn("该时间窗内只有心跳 / 对账一致例行行", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
