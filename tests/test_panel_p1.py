"""面板 P1 优化的回归测试（规格：spec-panel-p1.md，2026-10-25）。

四项功能逐项钉词面（铁律 10：断言看渲染 HTML，不看源码）：
1. 总览「策略绩效（上线以来）」卡：positions 只读聚合（笔数/均R/胜率/多空分解/累计R），
   合成行（signal_bar_ms 不在 4h 网格）滤除、r_multiple=NULL 不计入、N=0 不除零。
2. 盈亏曲线时间切换：GET span=7/30/all（缺参/非法回落 7），零 JS 链接带 k 与 refresh；
   标题/摘要行/SVG 随窗口重算；「全部」注明快照保留期。
3. 记录页降噪：主页提醒日志滤心跳、执行器事件滤心跳+「对账一致」；子页不变。
4. 小词面：信号表状态列不再带出原始值 closed；资金流水备注币名去重。

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
DAY = 86400.0
TOKEN = "p1-tk"
ADDR = "0xTestAccount000000000000000000000000"
BAR_MS = 14_400_000                       # 4h 网格（UTC 对齐）
BAR_GRID = BAR_MS * (1_700_000_000_000 // BAR_MS + 1)   # 网格上的合法信号 bar
BAR_OFF = BAR_GRID + 3_600_000            # 彩排合成行：偏离网格 1h

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.db import DB
    from app.hl.views import DashboardDeps, build_router, render_dashboard, status_payload
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = DB = None
    DashboardDeps = build_router = render_dashboard = status_payload = None


def fake_snapshot(equity=1000.0):
    """空仓快照（面板渲染零网络）。"""
    return {"ts": NOW, "address": ADDR, "equity_total": equity, "equity": equity,
            "spot_usdc": equity, "unified": True, "withdrawable": 90.0,
            "margin_used": 10.0, "notional": 0.0, "positions": [], "open_orders": []}


class FakeInfo:
    def all_mids(self):
        return {}

    def snapshot(self, address, mids=None):
        return fake_snapshot()


class BaseCase(unittest.TestCase):
    """临时 DB + 同一个 app 挂 /hl 与 /admin 两套路由（P1 两页都要断言）。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="panel_p1_")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self._bar_i = 0
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

    # ---- 取数辅助 ----
    def hl(self, **params):
        return self.client.get("/hl", params=params or None).text

    def admin(self, tab="records", **params):
        p = {"tab": tab, "k": TOKEN}
        p.update(params)
        return self.client.get("/admin", params=p).text

    def closed_pos(self, side=1, entry=100_000.0, stop=97_000.0, exit_px=103_000.0,
                   bar_ms=None):
        """造一笔已平仓仓位（close_position 按 entry/stop/exit 算 r_multiple）。

        bar_ms 缺省时自动取 4h 网格上的下一个槽位 —— positions 表
        UNIQUE(symbol, signal_bar_ms)，同 bar 的第二笔会撞唯一键变成改账。
        """
        if bar_ms is None:
            bar_ms = BAR_GRID + self._bar_i * BAR_MS
            self._bar_i += 1
        pid = self.db.create_position(
            symbol="BTC_USDT" if side == 1 else "ETH_USDT",
            signal_bar_ms=bar_ms, status="open", side=side,
            entry_price=entry, stop_price=stop, stop_dist=0.03, contracts=6,
            coin_qty=0.0006, notional=60.0, risk_u=18.0, equity_at_entry=1000.0)
        self.db.close_position(pid, exit_px, "stop")
        return pid

    @staticmethod
    def signal_seg(html: str) -> str:
        """记录页「信号」表片段（状态列断言只看这块，避免命中页面其它词）。"""
        return html.split("<h2>信号</h2>", 1)[1].split("</table>", 1)[0]


# ---------------------------------------------------------------- 功能一：绩效卡
class TestPerfCard(BaseCase):
    LEGEND = "口径：按已平仓记录 · R = 盈亏 ÷ 开仓时计划风险 · 不含未平仓"

    def test_card_between_account_and_curve_with_legend(self):
        self.closed_pos()                       # 多 +1.0
        html = self.hl()
        self.assertIn("策略绩效（上线以来）", html)
        self.assertIn(self.LEGEND, html, "口径必须落可见文字（iOS 无悬停）")
        i_acc = html.index("累计已实现")
        i_perf = html.index("策略绩效（上线以来）")
        i_curve = html.index("<h2>盈亏曲线（已剔除出入金）</h2>")
        self.assertLess(i_acc, i_perf, "绩效卡必须在「账户」卡之后")
        self.assertLess(i_perf, i_curve, "绩效卡必须在「盈亏曲线」之前")

    def test_stats_line_side_split_and_values(self):
        self.closed_pos(side=1, exit_px=103_000.0)            # 多 r=+1.0
        self.closed_pos(side=1, exit_px=97_000.0)             # 多 r=-1.0
        self.closed_pos(side=-1, entry=3_000.0, stop=3_100.0,
                        exit_px=2_900.0)                      # 空 r=+1.0
        html = self.hl()
        self.assertIn("已平仓 <b>3</b> 笔（多 2 · 空 1）", html)
        self.assertIn("均R <b class=\"pos\">+0.33</b>", html)
        self.assertIn("胜率 67%（赢 2 笔）", html)
        self.assertIn("累计 R <b class=\"pos\">+1.0</b>", html)

    def test_synthetic_rows_filtered_by_4h_grid(self):
        self.closed_pos()                                     # 网格行：计入
        self.closed_pos(exit_px=106_000.0, bar_ms=BAR_OFF)    # 合成行 r=+2.0：滤除
        html = self.hl()
        # 合成行若混入：均R 会变 +0.67、累计 R +3.0 —— 词面必须钉在只算网格行。
        self.assertIn("已平仓 <b>1</b> 笔（多 1 · 空 0）", html)
        self.assertIn("均R <b class=\"pos\">+1.00</b>", html)
        self.assertIn("累计 R <b class=\"pos\">+1.0</b>", html)

    def test_null_r_multiple_not_counted(self):
        pid = self.closed_pos()
        self.db.conn.execute("UPDATE positions SET r_multiple=NULL WHERE id=?", (pid,))
        self.db.conn.commit()
        html = self.hl()
        self.assertIn("暂无已平仓记录", html, "r=NULL 的行不计入笔数（不猜 0）")
        self.assertNotIn("均R", html)

    def test_zero_closed_no_division(self):
        html = self.hl()
        self.assertIn("策略绩效（上线以来）", html)
        self.assertIn("暂无已平仓记录", html, "N=0 不除零、不编数")
        self.assertNotIn("胜率", html)

    def test_payload_carries_perf_stats(self):
        self.closed_pos()
        self.closed_pos(side=-1, entry=3_000.0, stop=3_100.0, exit_px=2_900.0)
        perf = status_payload(self.hl_deps)["perf"]
        self.assertEqual(perf["n"], 2)
        self.assertEqual(perf["long"], 1)
        self.assertEqual(perf["short"], 1)
        self.assertEqual(perf["wins"], 2)
        self.assertAlmostEqual(perf["avg_r"], 1.0)
        self.assertAlmostEqual(perf["total_r"], 2.0)


# ---------------------------------------------------------------- 功能二：span 切换
class TestCurveSpan(BaseCase):
    def setUp(self):
        super().setUp()
        # 快照：20d/8d/6d/4d/2d 各一点（span=7 只有 3 点、实际跨 4 天；30/all 5 点）。
        for days, eq in ((20, 800.0), (8, 840.0), (6, 860.0), (4, 880.0), (2, 900.0)):
            self.db.hl_add_snapshot({"ts": NOW - days * DAY, "equity": eq, "positions": []})
        # 流水：20d 前 +901.80（基线内）、3d 前 +100.79（7 天窗内）。
        self.db.hl_add_flow({"ts": NOW - 20 * DAY, "kind": "send_in", "amount": 901.80,
                             "hash": "0xa1", "counterparty": "x", "dex": ""})
        self.db.hl_add_flow({"ts": NOW - 3 * DAY, "kind": "send_in", "amount": 100.79,
                             "hash": "0xa2", "counterparty": "x", "dex": ""})
        self.db.set_config("hl_flows_synced_at", str(int(NOW)))

    def _w(self, pts, span):
        cut = pts[-1][0] - span * DAY
        sel = [v for t, v in pts if t >= cut]
        return sel if len(sel) >= 2 else [v for _, v in pts]

    def test_default_is_7d_and_actual_days_in_title(self):
        for html, tag in ((self.hl(), "缺参"), (self.hl(span="7"), "span=7"),
                          (self.hl(span="abc"), "非法值")):
            # 窗口以最后一点为基准回切 7 天 → 4 个快照（8d/6d/4d/2d），实际跨 6 天，
            # 标题按实际首尾写「近 6 天」而不是编「近 7 天」。
            self.assertIn("近 6 天（4 个快照）", html, f"{tag} 应回落 7 天窗（标题按实际首尾换算）")
            self.assertNotIn("近 18 天", html, f"{tag} 不该把 30 天数据画进 7 天窗")

    def test_span30_recalculates_title_and_summary(self):
        html = self.hl(span="30")
        self.assertIn("近 18 天（5 个快照）", html, "span=30 标题必须变化（按实际首尾）")
        p = status_payload(self.hl_deps)["curve"]
        pnl = self._w(p["pnl"], 30)
        self.assertIn(f"累计盈亏 {pnl[-1] - pnl[0]:+,.2f}U", html, "摘要行要按窗口重算")

    def test_span_all_shows_retention_note_and_meta_match(self):
        html = self.hl(span="all")
        self.assertIn("全部（5 个快照）", html)
        self.assertIn("快照保留约 30 天，更早数据已清理", html, "「全部」必须注明保留期")
        meta = status_payload(self.hl_deps)["curve"]["meta"]
        self.assertIn(f"累计盈亏 {meta['pnl_total']:+,.2f}U", html)
        self.assertIn(f"净入金 {meta['net_deposit']:,.2f}U", html)
        # 7/30 天窗不出现保留期注（那是「全部」的口径说明）
        self.assertNotIn("快照保留约 30 天", self.hl())
        self.assertNotIn("快照保留约 30 天", self.hl(span="30"))

    def test_summary_numbers_follow_window(self):
        p = status_payload(self.hl_deps)["curve"]
        meta = p["meta"]
        # 7 天窗：净入金只算窗内流水；30/全部 = 全期。
        self.assertAlmostEqual(meta["net_deposit_span"]["7"], 100.79)
        self.assertAlmostEqual(meta["net_deposit_span"]["30"], 1002.59)
        html7 = self.hl()
        self.assertIn("净入金 100.79U", html7)
        html30 = self.hl(span="30")
        self.assertIn("净入金 1,002.59U", html30)

    def test_span_links_zero_js_carry_k_and_refresh(self):
        html = self.hl(span="30", k="sek", refresh=10)
        self.assertNotIn("<script", html.lower(), "时间切换必须是纯链接（零 JS）")
        for val in ("7", "30", "all"):
            self.assertIn(f"href='/admin?tab=overview&span={val}&refresh=10&k=sek'", html,
                          "链接必须指向面板单一入口并带回 k 与 refresh（PR #54 教训）")
        self.assertIn("✅ 近 30 天", html, "当前项要标 ✅")
        self.assertNotIn("✅ 近 7 天", html)

    def test_admin_overview_honors_span_and_links(self):
        # 主线程复核补的集成用例：总览在 /admin 渲染，span 必须由 /admin 路由注入。
        # 缺这个的话 ?span= 被静默忽略（恒 7 天）、链接还会跳出面板落到 /hl 旧页。
        self.assertIn("近 6 天（4 个快照）", self.admin(tab="overview"), "缺参回落 7 天窗")
        html30 = self.admin(tab="overview", span="30")
        self.assertIn("近 18 天（5 个快照）", html30, "span=30 在 /admin 上必须生效")
        self.assertIn("净入金 1,002.59U", html30, "摘要行随窗口重算")
        # 链接留在面板内并带回 k（无 cookie 深链也不 401）。
        self.assertIn("href='/admin?tab=overview&span=all&k=", html30)
        self.assertIn("✅ 近 30 天", html30)

    def test_dd_line_unchanged_by_span(self):
        # 「策略回撤」行是 hl_dd_state 峰值口径，不随 span 变（规格明令不动）。
        h7 = self.hl()
        h30 = self.hl(span="30")
        line = "（剔除出入金，峰值以来；按流水同步节奏更新）"
        self.assertIn(line, h7)
        self.assertIn(line, h30)


# ---------------------------------------------------------------- 功能三：记录页降噪
class TestRecordsNoise(BaseCase):
    def _seed(self):
        # 提醒：3 条真实 + 2 条心跳（推送/静默）
        self.db.log_alert("BTC_USDT", 1_700_000_000_000, "entry", "开多 BTC", True)
        self.db.log_alert("BTC_USDT", 1_700_000_006_000, "exit", "离场成交", True)
        self.db.log_alert("BTC_USDT", 1_700_000_012_000, "tick_error", "处理异常", True)
        self.db.log_alert(None, None, "heartbeat", "每日心跳推送", True)
        self.db.log_alert(None, None, "heartbeat_local", "心跳（静默）", True)
        # 事件：1 决策 + 1 心跳 + 1 对账一致（例行）+ 1 对账异常（必须照常显示）
        self.db.hl_log_event("decision", "人工切换执行器模式 → dry", "BTC_USDT")
        self.db.hl_log_event("heartbeat", "执行器心跳")
        self.db.hl_log_event("reconcile", "对账一致")
        self.db.hl_log_event("reconcile", "对账异常：BTC 数量超差 → 升级", "BTC_USDT")

    def test_main_page_hides_heartbeat_alerts(self):
        self._seed()
        html = self.admin(tab="records")
        self.assertNotIn("心跳（静默）", html)
        self.assertNotIn("心跳（推送）", html)
        self.assertNotIn("每日心跳推送", html)
        # 标题行数 = 实际渲染行数（铁律 8）：3 条真实提醒
        self.assertIn("提醒日志（alert_logs · 最近 3 条）", html)
        for msg in ("开多 BTC", "离场成交", "处理异常"):
            self.assertIn(msg, html, "真实提醒一条不能少")
        self.assertIn("主页已隐藏心跳类例行行", html, "滤了什么必须写出来")

    def test_main_page_hides_heartbeat_and_reconcile_ok_events(self):
        self._seed()
        html = self.admin(tab="records")
        self.assertIn("执行器事件（hl_events · 最近 2 条）", html)
        seg = html.split("执行器事件（hl_events", 1)[1].split("<thead>", 1)[1].split("</table>", 1)[0]
        self.assertNotIn("对账一致", seg)
        self.assertNotIn("心跳", seg, "心跳行整行隐藏")
        self.assertIn("对账异常", html, "对账异常不是例行行，必须照常显示")
        self.assertIn("人工切换执行器模式", html)
        self.assertIn("主页已隐藏「心跳」与「对账一致」例行行", html)

    def test_subpages_keep_full_rows(self):
        self._seed()
        alerts_sub = self.client.get("/admin/list/alerts", params={"k": TOKEN}).text
        self.assertIn("心跳（静默）", alerts_sub, "子页不过滤：心跳仍可见")
        self.assertIn("每日心跳推送", alerts_sub)
        events_sub = self.client.get("/admin/list/events", params={"k": TOKEN}).text
        # 子页消息渲染在 <span title=…> 里，断言看消息词面而非 td 形状
        self.assertIn(">对账一致<", events_sub, "子页不过滤：对账一致仍可见")
        self.assertIn("<td>心跳</td>", events_sub)

    def test_all_noise_rows_still_truthful_header(self):
        # 全库只有心跳时：标题不带行数、空表文案说明「被滤光」而不是「没有数据」。
        self.db.log_alert(None, None, "heartbeat", "每日心跳推送", True)
        self.db.log_alert(None, None, "heartbeat_local", "心跳（静默）", True)
        html = self.admin(tab="records")
        self.assertNotIn("最近 0 条", html)
        self.assertIn("该时间窗内只有心跳类例行记录", html)


# ---------------------------------------------------------------- 功能四：小词面
class TestWordFixes(BaseCase):
    def test_signal_status_no_raw_closed(self):
        # 交易所已平仓自动关账路径：status 与 hl_status 都会变成 closed。
        pid = self.closed_pos()
        self.db.set_pos_hl(pid, hl_status="closed")
        seg = self.signal_seg(self.admin(tab="records"))
        self.assertIn("已平仓", seg)
        self.assertNotIn("closed", seg, "状态列不许再带出原始值 closed（两种写法同现）")
        self.assertEqual(seg.count("已平仓"), 1, "同词不重复渲染第二行")

    def test_signal_hl_status_closed_while_open_still_annotated(self):
        # status 还是 open 但执行器已见平仓：第二行给出中文「已平仓」（信息不丢）。
        pid = self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=BAR_GRID, status="open", side=1,
            entry_price=100_000.0, stop_price=97_000.0, stop_dist=0.03, contracts=6,
            coin_qty=0.0006, notional=60.0, risk_u=18.0, equity_at_entry=1000.0)
        self.db.set_pos_hl(pid, hl_status="closed")
        seg = self.signal_seg(self.admin(tab="records"))
        self.assertIn("持仓中", seg)
        self.assertIn("已平仓", seg)
        self.assertNotIn("closed", seg)

    def test_ledger_remark_coin_dedup(self):
        self.db.hl_add_funding({"ts": NOW - 3600, "coin": "BTC", "amount_usd": 0.009152,
                                "position_id": None,
                                "ref": "fund:1700000250000|BTC|+0.009152"})
        html = self.admin(tab="records")
        row = next(l for l in html.split("<tr>") if "+0.009152" in l)
        self.assertIn("+0.009152", row, "数量必须还在")
        self.assertEqual(row.count("BTC"), 1, "币名只出现一次（曾渲染成 BTC · |BTC|+0.009152）")
        self.assertNotIn("|BTC|", row)
        self.assertNotIn("|BTC|", html)
        # hash 类 ref（不含 |币名| 段）原样保留
        self.db.hl_add_funding({"ts": NOW - 7200, "coin": "ETH", "amount_usd": 0.02,
                                "position_id": None, "ref": "fund:0xabc123def4567890"})
        html2 = self.admin(tab="records")
        self.assertIn("def4567890", html2, "hash 类截尾不受去重影响")


if __name__ == "__main__":
    unittest.main(verbosity=2)
