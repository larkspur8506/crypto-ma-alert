"""面板 P3 减法轮的回归测试（规格：spec-panel-p3.md，2026-10-25）。

七项逐项钉词面（铁律 6：断言看渲染 HTML，不看源码；铁律 7：/hl 与 /admin
双入口各断言一遍 —— P1 复核教训）：
0. 命名与头部：产品名「双均线交易台」+ 副题；title/webmanifest/确认页同步；
   旧词面（crypto-ma-alert 管理面板 / Hyperliquid 执行器监控）渲染层整页 0 处。
1. 挂单表按需渲染：正常态（每仓恰好一条 reduce-only 触发单）不渲染；孤儿/多单/
   非止损单/挂单不可信（fail-open）才渲染。
2. 系统卡瘦身改名「执行器」：对账/熔断/卡单三行删除（状态条唯一出处）；
   卡单徽章只在 >0 时出现。
3. API 有效期降级：状态条徽章（唯一常驻出处）+ 总览末尾 <details>（≤14 天 open）。
4. 账户卡 acct-sub 收进 <details>「资金明细」。
5. 收益主从：「累计已实现」整页唯一出处=绩效卡 U 对照行；R/U 主从图例。
6. 记录页锚点导航：六个零 JS 跳转链接，href 与 id 一一配对、无重复 id。
   （P4 收尾轮已整行删除 —— 断言反向钉死：锚点词面渲染层 0 处，见
   TestRecordAnchorsRemoved 与 tests/test_panel_p4.py。）

全部离线：临时 sqlite + 假 info，不触网。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NOW = 1760000000.0
TOKEN = "p3-tk"
ADDR = "0xTestAccount000000000000000000000000"
PRODUCT = "双均线交易台"
SUBTITLE = "BTC / ETH / SOL · 4h 密集突破 · Hyperliquid 自动执行"

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.db import DB
    from app.hl import agent as agent_mod
    from app.hl.views import DashboardDeps, build_router
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = DB = agent_mod = None
    DashboardDeps = build_router = None


def fake_snapshot(equity=995.06, positions=None, open_orders=None, orders_trusted=True):
    """统一账户快照（面板渲染零网络）。默认空仓无挂单。"""
    return {"ts": NOW, "address": ADDR, "equity_total": equity, "equity": equity,
            "spot_usdc": 839.34, "unified": True, "withdrawable": 0.0,
            "spot_hold": 15.0, "margin_used": 155.72, "notional": 778.60,
            "unrealized_pnl": 0.0, "today_realized": 2.10,
            "total_realized": -14.38, "funding_total": 0.57, "fees_total": 1.23,
            "positions": positions or [], "open_orders": open_orders or [],
            "orders_trusted": orders_trusted}


def held_position(coin="BTC", margin=155.72, upnl=7.51, entry=100_000.0, mark=101_000.0,
                  liq=96_917.45, funding=-0.57):
    """一个持仓中的空头快照行（字段名与 HlInfo.snapshot 同口径）。"""
    return {"coin": coin, "side": -1, "size": 0.00905, "entry_px": entry,
            "mark_px": mark, "position_value": 908.0, "liq_px": liq,
            "leverage": 5, "isolated": True, "margin_used": margin,
            "unrealized_pnl": upnl, "funding_since_open": funding}


def stop_order(coin="BTC", trigger=97_000.0):
    """该仓的 reduce-only 触发止损单（正常态的构成件）。"""
    return {"coin": coin, "oid": 7, "is_buy": True, "sz": 0.00905, "limit_px": 0.0,
            "reduce_only": True, "trigger_px": trigger, "is_trigger": True,
            "timestamp": int(NOW * 1000)}


class BaseCase(unittest.TestCase):
    """临时 DB + 同一个 app 挂 /hl 与 /admin 两套路由（模板：tests/test_panel_p2.py）。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="panel_p3_")
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
            mids_fn=lambda: self.info.all_mids(),
            # 与生产装配同口径：状态条徽章（payload["agent"]）与「API 有效期」details
            # 读同一份 hl_agent_state DB 缓存 —— 两处永远是同一个数。
            agent_state_fn=lambda: agent_mod.read_state(self.db),
            cleanup_enabled_fn=lambda: True)
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
        """账户卡片段（折叠断言只看这块，避免命中仓位表同名列）。"""
        return html.split("<div class='sec'>账户</div>", 1)[1].split("策略绩效（上线以来）", 1)[0]

    @staticmethod
    def executor_seg(html: str) -> str:
        """「执行器」卡片段（到下一个 <h2> 或页尾为止）。"""
        seg = html.split("<h2>执行器</h2>", 1)[1]
        return seg.split("<h2>", 1)[0]


class FakeInfo:
    def all_mids(self):
        return {}

    def __init__(self, snap=None):
        self._snap = snap if snap is not None else fake_snapshot()

    def snapshot(self, address, mids=None):
        return self._snap


# ---------------------------------------------------------------- 功能零：命名与头部
class TestNaming(BaseCase):

    def test_title_h1_subtitle_both_entries(self):
        for html in self.both():
            self.assertIn(f"<title>{PRODUCT} · 总览</title>", html)
            self.assertIn(f"<h1>{PRODUCT}</h1>", html)
            self.assertIn(SUBTITLE, html)

    def test_old_names_gone_from_rendered_pages(self):
        for html in self.both():
            for gone in ("crypto-ma-alert", "Hyperliquid 执行器监控", "管理面板"):
                self.assertNotIn(gone, html, f"旧词面「{gone}」渲染层应整页 0 处")

    def test_webmanifest_pwa_name(self):
        with open(os.path.join("app", "static", "site.webmanifest"), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m["name"], PRODUCT)
        self.assertEqual(m["short_name"], PRODUCT)

    def test_records_subpage_title_synced(self):
        r = self.client.get("/admin/list/orders", params={"k": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"<title>{PRODUCT} · 下单记录</title>", r.text)
        self.assertIn(SUBTITLE, r.text)


# ---------------------------------------------------------------- 功能一：挂单按需渲染
class TestOrdersOnDemand(BaseCase):

    def test_normal_state_renders_no_orders_table(self):
        """正常态：每仓恰好一条 reduce-only 触发单 → 挂单表整块不渲染，止损仍在仓位表。"""
        self.info._snap = fake_snapshot(positions=[held_position()], open_orders=[stop_order()])
        for html in self.both():
            self.assertNotIn("交易所挂单", html, "正常态挂单表不该渲染（P3 R1）")
            self.assertIn("&#10004;&#65039;", html, "止损信息由仓位表「止损 ✔️」列唯一表达")
            self.assertIn("97,000.00", html)

    def test_empty_state_renders_no_orders_table(self):
        """空仓 + 无挂单 = 一致 → 也不渲染（旧版渲染「交易所无挂单」空表行）。"""
        for html in self.both():
            self.assertNotIn("交易所挂单", html)

    def test_orphan_order_renders_table(self):
        """孤儿挂单（无仓却有挂单）→ 异常态，完整挂单表照常出现。"""
        self.info._snap = fake_snapshot(positions=[held_position()],
                                        open_orders=[stop_order(), stop_order(coin="ETH")])
        for html in self.both():
            self.assertIn("交易所挂单", html)
            self.assertIn("ETH", html.split("<h2>交易所挂单</h2>", 1)[1].split("</table>", 1)[0])

    def test_extra_non_stop_order_renders_table(self):
        """仓位之外还有非止损挂单 → 异常态。"""
        extra = {"coin": "BTC", "oid": 9, "is_buy": True, "sz": 0.001, "limit_px": 90_000.0,
                 "reduce_only": False, "trigger_px": 0.0, "is_trigger": False,
                 "timestamp": int(NOW * 1000)}
        self.info._snap = fake_snapshot(positions=[held_position()],
                                        open_orders=[stop_order(), extra])
        for html in self.both():
            self.assertIn("交易所挂单", html)

    def test_untrusted_orders_fail_open(self):
        """挂单不可信（orders_trusted=False）→ 判不清就渲染（fail-open：宁多勿漏）。"""
        self.info._snap = fake_snapshot(positions=[held_position()], open_orders=[],
                                        orders_trusted=False)
        for html in self.both():
            self.assertIn("交易所挂单", html)
            self.assertIn("无法确认", html, "止损列显「? 无法确认」")


# ---------------------------------------------------------------- 功能二：系统卡瘦身
class TestExecutorCard(BaseCase):

    def test_executor_card_slim_and_renamed(self):
        self.db.hl_log_event("decision", "双均线金叉开多", symbol="BTC_USDT")
        for html in self.both():
            self.assertIn("<h2>执行器</h2>", html)
            seg = self.executor_seg(html)
            self.assertIn("执行器最后事件", seg)
            self.assertIn("交易所 api.hyperliquid.xyz · 主网", seg)
            # 对账/熔断/卡单三行删除 —— 状态条徽章是唯一出处（卡内 0 处）
            for gone in ("对账", "熔断", "卡在 intent/sent"):
                self.assertNotIn(gone, seg, f"「{gone}」不该再出现在执行器卡内")
            self.assertNotIn("<h2>系统</h2>", html)

    def test_stuck_badge_only_when_positive(self):
        for html in self.both():
            self.assertNotIn("卡单", html, "正常态状态条不出现「卡单」字样")
            self.assertNotIn("卡在 intent/sent", html)
        oid = self.db.hl_record_intent("BTC_USDT", "BTC", 123, "entry", 1, 0.001)
        self.db.conn.execute("UPDATE hl_orders SET created_at=? WHERE id=?",
                             (NOW - 600, oid))          # 600s > 180s 阈值 → 卡单
        self.db.conn.commit()
        for html in self.both():
            self.assertIn("卡单 <b>1 笔</b>", html)
            self.assertIn("持续 3 分钟未推进才计入", html, "口径随迁到徽章 title")


# ---------------------------------------------------------------- 功能三：API 有效期降级
class TestAgentBadgeAndDetails(BaseCase):

    def _write_state(self, days, level="ok", cleanup="none"):
        agent_mod.write_state(self.db, {
            "valid_until_ms": int(NOW * 1000) + days * 86_400_000,
            "checked_ms": int(NOW * 1000), "days_left": days, "level": level,
            "cleanup": cleanup, "address": ADDR, "source": "api"})

    def test_badge_on_both_strips(self):
        self._write_state(169)
        for html in self.both():
            self.assertIn("API <b>169 天</b>", html, "状态条徽章是唯一常驻出处")

    def test_badge_colors_by_days_left(self):
        self._write_state(20)
        self.assertIn("API <b>20 天</b>", self.hl())
        self._write_state(10, level="near")
        for html in self.both():
            self.assertIn("class='warn'>&#9679;</span> API <b>10 天</b>", html)
        self._write_state(5, level="urgent")
        for html in self.both():
            self.assertIn("class='neg'>&#9679;</span> API <b>5 天</b>", html)
        self._write_state(5, level="urgent", cleanup="done")
        for html in self.both():
            self.assertIn("API <b>已清仓</b>", html)

    def test_details_card_collapsed_by_default_open_when_near(self):
        self._write_state(169)
        admin_html = self.admin()
        self.assertIn("<details class='card agent-card'><summary>API 有效期</summary>",
                      admin_html, "默认收起")
        # 四行信息 + 自动清仓开关状态一项不丢（逐项对照）
        for w in ("代理钱包地址", "到期日", "剩余", "状态", "到期前自动清仓", "已开启"):
            self.assertIn(w, admin_html)
        # /hl 端本来就没有这张卡，徽章已覆盖常驻出处
        self.assertNotIn("API 有效期</summary>", self.hl())
        self._write_state(10, level="near")
        self.assertIn("<details class='card agent-card' open><summary>API 有效期</summary>",
                      self.admin(), "剩余 ≤14 天服务端渲染 open（零 JS 自动展开）")


# ---------------------------------------------------------------- 功能四：账户卡折叠
class TestAccountFold(BaseCase):

    def test_acct_sub_folded_into_details(self):
        for html in self.both():
            seg = self.account_seg(html)
            self.assertIn("<details class='acct-details'><summary>资金明细</summary>", seg)
            self.assertIn("acct-sub", seg)
            self.assertLess(seg.index("资金明细"), seg.index("acct-sub"),
                            "次级小字行收进 details 内")
            self.assertIn("账户总值", seg.split("<details", 1)[0], "顶层关键数常驻")
            self.assertIn("可提余额", seg.split("<details", 1)[0])


# ---------------------------------------------------------------- 功能五：收益主从
class TestRealizedSingleSource(BaseCase):

    def test_realized_word_appears_exactly_once(self):
        self.db.hl_upsert_fill(tid="f1", symbol="BTC_USDT", coin="BTC", side=-1,
                               sz=0.00905, px=101_000.0, fee=0.0, fee_token="USDT",
                               dir="Close Short", closed_pnl=-14.38, hash="0xf1",
                               reduce_only=True, ts=int(NOW * 1000), created_at=NOW)
        for html in self.both():
            self.assertEqual(html.count("累计已实现"), 1,
                             "「累计已实现」整页只出现 1 处（盈亏曲线「上线以来」行）")
            self.assertRegex(html, "累计已实现 <b class='[^']*'>-14\.38 U</b>")
            self.assertIn("已实现为交易所口径，未扣手续费与资金费", html,
                          "口径说明唯一幸存处 = 账户卡注脚")

    def test_ru_legend_removed(self):
        # 2026-10-29 用户要求删除该段口径说明
        for html in self.both():
            self.assertNotIn("两者口径不同，不直接换算", html)
            self.assertNotIn("R = 策略口径", html)


# ---------------------------------------------- 功能一（P4 反向）：锚点导航已删
class TestRecordAnchorsRemoved(BaseCase):
    """P4 把 P3 R6 的锚点导航整行删除（用户拍板「手机上多余」）。

    原正向用例（href 与 id 一一配对等）随口径删除；这里反向钉死：记录页
    href='#cma- 与 id='cma- 都是 0 处，不留死链接也不留死 id。
    """

    def test_records_page_has_no_anchor_navigation(self):
        html = self.admin(tab="records")
        self.assertEqual(html.count("href='#cma-"), 0, "锚点链接应 0 处")
        self.assertEqual(html.count("id='cma-"), 0, "区块容器 id 应 0 处")
        self.assertNotIn("<a href='#", html, "记录页不应残留任何页内跳转链接")
        # 页顶直接是「资金流水」卡（资金流水 <h2> 仍在 <h2>记录</h2> 之前）
        self.assertLess(html.index("资金流水"), html.index("<h2>记录</h2>"))


# ---------------------------------------------------------------- 冒烟：总览块序
class TestOverviewOrderSmoke(BaseCase):

    def test_admin_overview_block_order(self):
        html = self.admin()
        order = ["<h2>仓位</h2>", "<div class='sec'>账户</div>",
                 "<h2>盈亏曲线（已剔除出入金）</h2>", "三标的动态", "<h2>执行器</h2>",
                 "<summary>API 有效期</summary>"]
        idx = [html.index(m) for m in order]
        self.assertEqual(idx, sorted(idx), f"总览块序错乱：{order}")

    def test_hl_overview_block_order(self):
        html = self.hl()
        order = [f"<h1>{PRODUCT}</h1>", "<h2>仓位</h2>", "<div class='sec'>账户</div>",
                 "<h2>盈亏曲线（已剔除出入金）</h2>", "三标的动态", "<h2>执行器</h2>"]
        idx = [html.index(m) for m in order]
        self.assertEqual(idx, sorted(idx), f"/hl 块序错乱：{order}")
        self.assertNotIn("<summary>API 有效期</summary>", html)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
