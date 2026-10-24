"""记录页列表子页（feat/list-subpages）的单元测试。

规格：spec-list-subpages.md「测试要求」7 条，全部真跑 HTTP 层（TestClient + 临时
sqlite + 假 HlInfo，不触网）：
1. 路由与鉴权：六个子页 + /admin/export 无 k → 401；有 k → 200。
2. 游标分页：35 行、rows=10 → 4 页；before/after 链接目标行正确；翻页期间插入
   新行不影响当前页内容（游标稳定性，变异可验）。
3. 筛选器：单字段、组合、日期边界（含/不含）；筛选在翻页链接中原样携带。
4. CSV：表头、行数（同筛选全量）、北京时间列、UTF-8 BOM、超大表流式不炸（2000 行）。
5. 资金流水子页：跨页余额连续；筛选后余额口径标注出现。
6. 主记录页回归：块位置、10/20/50 链接、「全部 ›」链接指向正确。
7. 信号表：主页面行为与改动前一致、子页筛选正常。

零 JS / 零外部资源是面板铁律，顺手在每个子页响应上验一遍（无 <script、无 http 外链）。
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "subpage-tk"
TZ8 = dt.timezone(dt.timedelta(hours=8))          # 北京时间（默认 DISPLAY_TZ）
# 固定的两个北京日期，日期边界测试用（不依赖运行时刻）
D1 = dt.datetime(2025, 10, 9, 12, 0, 0, tzinfo=TZ8)
D2 = dt.datetime(2025, 10, 10, 0, 0, 30, tzinfo=TZ8)
D1_S, D2_S = D1.timestamp(), D2.timestamp()
D1_STR, D2_STR = "2025-10-09", "2025-10-10"
BAR = 1_700_000_000_000

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.db import DB
    from app.hl.views import DashboardDeps
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = DB = DashboardDeps = None

SLUGS = ("orders", "fills", "ledger", "events", "alerts", "positions")


def fake_snapshot(equity=100.0):
    """与 HlInfo.snapshot 口径一致的空仓快照（信号块只吃 DB 侧 positions 行）。"""
    return {"ts": D1_S, "address": "0xTEST", "equity_total": equity, "equity": equity,
            "spot_usdc": equity, "unified": True, "withdrawable": 90.0,
            "margin_used": 10.0, "notional": 0.0, "positions": [], "open_orders": []}


class FakeInfo:
    """HlInfo 替身（只读接口，面板渲染零网络）。"""

    def snapshot(self, address, mids=None):
        return fake_snapshot()

    def all_mids(self):
        return {"BTC": 100_000.0, "ETH": 3_000.0}


def fmt2(v: float) -> str:
    return f"{v:,.2f}"


class BaseCase(unittest.TestCase):
    """临时 DB + 假 info + 只挂合并面板路由的 app。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="list_sub_")
        from app.db import DB as _DB
        self.db = _DB(os.path.join(self.tmp, "t.db"))
        self.deps = AdminDeps(
            db=self.db, config_fn=lambda: Config(), hl_deps=DashboardDeps(
                db=self.db, config_fn=lambda: Config(), info=FakeInfo(),
                address_fn=lambda: "", mode_fn=lambda: "off"),
            mode_fn=lambda: "off", token_fn=lambda: TOKEN)
        app = FastAPI()
        app.include_router(build_admin_router(self.deps))
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 造数助手（只走既有写路径 / 测试专用的批量直插，不新增写逻辑） ----
    def add_orders(self, n, coin_cycle=("BTC", "ETH"), purpose_cycle=("entry", "stop", "exit"),
                   bar0=None):
        """hl_orders：幂等键 symbol+bar_ms+purpose，bar_ms 逐行递增保证不撞。

        多轮插桩必须换 bar0（幂等键 INSERT OR IGNORE，同键静默不插 → 用例空转）。
        """
        ids = []
        for i in range(n):
            ids.append(self.db.hl_record_intent(
                "BTC_USDT", coin_cycle[i % len(coin_cycle)],
                (bar0 if bar0 is not None else BAR) + i,
                purpose_cycle[i % len(purpose_cycle)], 1, 1.0, 100.0, status="filled"))
        return ids

    def add_fills(self, n=None, ts_list=None):
        rows = ts_list if ts_list is not None else [D1_S + i * 60 for i in range(n or 0)]
        out = []
        for i, ts in enumerate(rows):
            self.db.hl_upsert_fill(
                tid=f"t{i}", symbol="BTC_USDT", coin="BTC", side=1, sz=0.01, px=100.0,
                fee=0.01, fee_token="USDC", fee_quote=0.01, dir="Open Long",
                closed_pnl=1.5, ts=ts * 1000.0, created_at=D1_S)
            out.append(i)
        return out

    def add_events(self, n, kind="decision"):
        for i in range(n):
            self.db.hl_log_event(kind, f"事件 {i}", "BTC_USDT")

    def add_alerts(self, n, kind="entry"):
        for i in range(n):
            self.db.log_alert("BTC_USDT", BAR + i, kind, f"提醒 {i}", True)

    def add_positions(self, rows):
        """positions：测试专用直插（与 tests/test_fill_pnl.py 等既有做法一致）。

        UNIQUE(symbol, signal_bar_ms) —— bar 逐行递增避免撞幂等键。
        """
        for i, (symbol, status, side) in enumerate(rows):
            self.db.conn.execute(
                "INSERT INTO positions(symbol, signal_bar_ms, status, side, entry_price,"
                " stop_price, stop_dist, contracts, coin_qty, notional, risk_u, created_at,"
                " opened_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, BAR + i, status, side, 100.0, 98.0, 0.02, 1, 0.01, 100.0, 2.0,
                 D1_S, D1_S))
        self.db.conn.commit()

    def add_flows(self, amounts, ts_list=None):
        """hl_flows（v_ledger 的出入金源）：amount 带符号，ts 秒。"""
        base = ts_list if ts_list is not None else [D1_S + i * 60 for i in range(len(amounts))]
        for i, amt in enumerate(amounts):
            self.db.hl_add_flow({"ts": base[i], "kind": "deposit" if amt >= 0 else "withdraw",
                                 "amount": float(amt), "hash": f"h{i}",
                                 "counterparty": "", "dex": "", "created_at": base[i]})

    # ---- 解析助手 ----
    def tbody(self, html_text):
        m = re.search(r"<tbody>(.*?)</tbody>", html_text, re.S)
        self.assertIsNotNone(m, "页面上没有表格")
        return m.group(1)

    def links(self, html_text, label):
        m = re.search(r"<a class='[^']*' href='([^']*)'>\s*" + re.escape(label), html_text)
        return m.group(1) if m else None

    def ledger_pairs(self, html_text):
        """资金流水行的 (金额, 余额) 序列（展示序，最新在前）。"""
        body = self.tbody(html_text)
        return [(float(a.replace(",", "")), float(b.replace(",", "")))
                for a, b in re.findall(
                    r"<td class='num[^']*'>([+—0-9,.]+)</td><td class='num'>([0-9,.]+)</td>",
                    body)]


class AuthAndRoutes(BaseCase):

    def test_six_subpages_and_export_auth(self):
        """规格测试 1：六个子页 + export 无 k → 401；有 k → 200。"""
        for slug in SLUGS:
            r = self.client.get(f"/admin/list/{slug}")
            self.assertEqual(r.status_code, 401, slug)
            r = self.client.get(f"/admin/list/{slug}", params={"k": TOKEN})
            self.assertEqual(r.status_code, 200, slug)
            self.assertIn("导出 CSV", r.text, slug)
            self.assertIn("筛选", r.text, slug)
        r = self.client.get("/admin/export", params={"table": "orders"})
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/admin/export", params={"table": "orders", "k": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn("attachment", r.headers["content-disposition"])
        # 未知 slug / 表名 → 404（不能把任意表名漏出去）
        self.assertEqual(self.client.get("/admin/list/nope", params={"k": TOKEN}).status_code, 404)
        self.assertEqual(self.client.get("/admin/export",
                                         params={"table": "hl_orders", "k": TOKEN}).status_code, 404)

    def test_subpages_zero_js_no_external(self):
        """面板铁律：子页零 JS、无外部资源。"""
        for slug in SLUGS:
            r = self.client.get(f"/admin/list/{slug}", params={"k": TOKEN})
            self.assertNotIn("<script", r.text, slug)
            self.assertNotIn("http://", r.text.replace("http://localhost", ""), slug)
            self.assertNotIn("https://", r.text, slug)

    def test_rows_switch_keeps_filters(self):
        """/admin/list/rows 设 cookie 并跳回本子页（带原筛选参数）。"""
        r = self.client.get("/admin/list/rows",
                            params={"n": "50", "back": f"/admin/list/orders?coin=BTC&k={TOKEN}",
                                    "k": TOKEN}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("coin=BTC", r.headers["location"])
        self.assertIn(f"k={TOKEN}", r.headers["location"])
        self.assertIn("cma_rows=50", r.headers["set-cookie"])
        # 防开放跳转：back 不是本面板子页时一律回记录页
        r = self.client.get("/admin/list/rows",
                            params={"n": "10", "back": "https://evil.example/x", "k": TOKEN},
                            follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("/admin?tab=records"))
        # 筛选 GET 表单补隐藏 n（与翻页链接口径一致）：提交筛选不掉回 cookie 行数
        page = self.client.get("/admin/list/orders", params={"k": TOKEN, "n": "20"}).text
        self.assertIn("<input type='hidden' name='n' value='20'>", page,
                      "筛选表单缺 n 参数，提交后会掉回 cookie 行数")

    def test_rows_switch_deep_link_keeps_k(self):
        """审查 M3：无 cookie 只带 k 的深链，切行数后 302 必须带回 k，否则 401。"""
        self.add_orders(2)
        page = self.client.get("/admin/list/orders", params={"k": TOKEN, "n": "10"}).text
        m = re.search(r"href='(/admin/list/rows\?[^']*)'", page)
        self.assertIsNotNone(m, "子页缺行数切换链接")
        rr = self.client.get(m.group(1), follow_redirects=False)
        self.assertEqual(rr.status_code, 302)
        self.assertIn(f"k={TOKEN}", rr.headers["location"],
                      "行数切换 302 丢了 k，深链会被打回 401")
        self.assertEqual(self.client.get(rr.headers["location"]).status_code, 200,
                         "深链 → 切行数 → 回子页全链路应 200")


class CursorPagination(BaseCase):

    def test_35_rows_four_pages(self):
        """规格测试 2：35 行、rows=10 → 4 页；页眉序号与 下一页/上一页 链接正确。"""
        self.add_orders(35)
        r1 = self.client.get("/admin/list/orders", params={"k": TOKEN, "n": "10"}).text
        self.assertIn("第 1–10 条 / 共 35 条", r1)
        self.assertEqual(self.tbody(r1).count("<tr>"), 10)
        older = self.links(r1, "下一页（更旧）")
        self.assertIsNotNone(older)
        self.assertIn("before=", older)
        r2 = self.client.get(older).text
        self.assertIn("第 11–20 条 / 共 35 条", r2)
        newer = self.links(r2, "‹ 上一页（更新）")
        self.assertIsNotNone(newer)
        self.assertIn("after=", newer)
        # 上一页回到第一页（同一组行）
        r1b = self.client.get(newer).text
        self.assertIn("第 1–10 条 / 共 35 条", r1b)
        self.assertEqual(self.tbody(r1b), self.tbody(r1))
        # 翻到最后一页：5 行、没有「下一页」
        page = r2
        for expect in ("第 21–30 条 / 共 35 条",):
            page = self.client.get(self.links(page, "下一页（更旧）")).text
            self.assertIn(expect, page)
        r4 = self.client.get(self.links(page, "下一页（更旧）")).text
        self.assertIn("第 31–35 条 / 共 35 条", r4)
        self.assertEqual(self.tbody(r4).count("<tr>"), 5)
        self.assertIsNone(self.links(r4, "下一页（更旧）"))
        self.assertIn("回到最新", r4)

    def test_cursor_stable_under_new_inserts(self):
        """规格测试 2（变异可验）：翻页期间插入新行，已翻到的页内容一字不变。"""
        self.add_orders(35)
        r1 = self.client.get("/admin/list/orders", params={"k": TOKEN, "n": "10"}).text
        older = self.links(r1, "下一页（更旧）")
        page2 = self.client.get(older).text
        before_body = self.tbody(page2)
        # 中间插入 3 条新订单（id 更大 = 更新方向最外侧）。
        # bar0 必须换：幂等键 symbol+bar_ms+purpose 是 INSERT OR IGNORE，同键静默
        # 不插会让下面的稳定性断言变成同内容自比恒真（PR #54 审查 M1）。
        new_ids = self.add_orders(3, bar0=BAR + 1000)
        self.assertEqual(len(set(new_ids)), 3, "插桩撞幂等键，返回 id 重复")
        self.assertEqual(self.db.list_subpage_count("orders"), 38,
                         "插桩没真插入 3 行（同键 INSERT OR IGNORE 空转）")
        page2_after = self.client.get(older).text
        self.assertEqual(self.tbody(page2_after), before_body,
                         "before 游标页被新插入的行漂移了（OFFSET 行为，禁止）")
        # after 方向同样稳定：page2 的「上一页」在插入后仍返回原来的第一页
        r1_body = self.tbody(r1)
        newer = self.links(page2, "‹ 上一页（更新）")
        after_body = self.tbody(self.client.get(newer).text)
        self.assertEqual(after_body, r1_body,
                         "after 游标页被新插入的行挤占了（DESC LIMIT 行为，禁止）")
        # 资金流水的 (ts, ref) 游标同理（行数选项只有 10/20/50，用 n=10）
        self.add_flows([1.0 * (i + 1) for i in range(12)])
        rl1 = self.client.get("/admin/list/ledger", params={"k": TOKEN, "n": "10"}).text
        older_l = self.links(rl1, "下一页（更旧）")
        lpage2 = self.client.get(older_l).text
        self.add_flows([99.0], ts_list=[D2_S])
        self.assertEqual(self.tbody(self.client.get(older_l).text), self.tbody(lpage2))


class Filters(BaseCase):

    def test_single_and_combined_filters(self):
        """规格测试 3：单字段、组合筛选；筛选在翻页链接中原样携带。"""
        self.add_orders(15, coin_cycle=("BTC",), purpose_cycle=("entry",))
        self.add_orders(10, coin_cycle=("ETH",), purpose_cycle=("stop",))
        r = self.client.get("/admin/list/orders",
                            params={"k": TOKEN, "n": "10", "coin": "BTC"}).text
        self.assertIn("共 15 条", r)
        self.assertEqual(self.tbody(r).count("<td>BTC</td>"), 10)
        self.assertEqual(self.tbody(r).count("<td>ETH</td>"), 0)
        older = self.links(r, "下一页（更旧）")
        self.assertIn("coin=BTC", older, "翻页链接丢了筛选条件")
        r2 = self.client.get(older).text
        self.assertIn("第 11–15 条 / 共 15 条", r2)
        # 组合：coin + purpose
        r = self.client.get("/admin/list/orders",
                            params={"k": TOKEN, "coin": "ETH", "purpose": "stop"}).text
        self.assertIn("共 10 条", r)
        self.assertEqual(self.tbody(r).count("<td>ETH</td>"), 10)
        # 下拉框回显当前值 + 选项来自 DB 去重值
        self.assertIn("<option value='ETH' selected>", r)

    def test_date_boundaries(self):
        """规格测试 3：日期边界（含端点；from 含当日、to 含当日，跨日行不含）。"""
        # fills：三行 —— D1 中午、D2 0 点 30 秒、D1 前一天
        day0 = self.client  # noqa: F841（占位，防误删）
        self.add_fills(ts_list=[D1_S - 86400, D1_S, D2_S])
        def count(**params):
            r = self.client.get("/admin/list/fills",
                                params={"k": TOKEN, **params}).text
            return self.tbody(r).count("<tr>")
        self.assertEqual(count(df=D1_STR), 2, "from 应含 D1 当天（D1 与 D2 两行）")
        self.assertEqual(count(dt=D1_STR), 2, "to 应含 D1 当天（前一天与 D1 两行）")
        self.assertEqual(count(df=D1_STR, dt=D1_STR), 1, "闭区间只留 D1 当天那一行")
        self.assertEqual(count(df=D2_STR), 1)
        self.assertEqual(count(df="2025-10-08", dt="2025-10-08"), 1, "前一天单独可查")
        # 坏日期 fail-soft：不 500、不当成边界
        r = self.client.get("/admin/list/fills",
                            params={"k": TOKEN, "df": "not-a-date"}).text
        self.assertEqual(self.tbody(r).count("<tr>"), 3)
        # ledger（ts 秒）同一条边界逻辑
        self.add_flows([1.0, 2.0, 3.0], ts_list=[D1_S - 86400, D1_S, D2_S])
        r = self.client.get("/admin/list/ledger",
                            params={"k": TOKEN, "df": D1_STR, "dt": D1_STR}).text
        self.assertEqual(self.tbody(r).count("<tr>"), 2)

    def test_ledger_position_id_filter(self):
        """资金流水的关联单筛选（数字输入）。"""
        self.add_flows([5.0, -2.0])
        # 关联单来自 v_ledger 的 positions 归集；造一条带 position_id 的成交行
        self.db.hl_upsert_fill(tid="pos1", symbol="BTC_USDT", coin="BTC", side=1, sz=0.01,
                               px=100.0, fee=0.0, fee_token="USDC", fee_quote=0.0,
                               dir="Close Long", closed_pnl=7.0, ts=D1_S * 1000.0,
                               created_at=D1_S)
        self.add_positions([("BTC_USDT", "closed", 1)])
        pid = self.db.conn.execute("SELECT id FROM positions LIMIT 1").fetchone()["id"]
        r = self.client.get("/admin/list/ledger",
                            params={"k": TOKEN, "position_id": str(pid)}).text
        self.assertIn("共 1 条", r)
        # 敲坏的非数字按「没筛」处理
        r = self.client.get("/admin/list/ledger",
                            params={"k": TOKEN, "position_id": "abc"}).text
        self.assertIn("共 3 条", r)


class CsvExport(BaseCase):

    def test_csv_header_rows_bom_beijing(self):
        """规格测试 4：表头、全量行数、北京时间列、UTF-8 BOM。"""
        self.add_fills(ts_list=[D1_S, D2_S])
        r = self.client.get("/admin/export", params={"table": "fills", "k": TOKEN})
        self.assertEqual(r.status_code, 200)
        text = r.text
        self.assertTrue(text.startswith("\ufeff"), "CSV 缺 UTF-8 BOM")
        lines = text.lstrip("\ufeff").rstrip("\n").split("\n")
        self.assertEqual(lines[0], "id,成交时间,币种,方向,数量,价格,手续费U,平仓盈亏U,减仓")
        self.assertEqual(len(lines) - 1, 2, "导出必须是同筛选条件下的全量（不分页）")
        # 时间列北京时间（id DESC 序：第一行数据是 D2，第二行是 D1）
        self.assertIn(dt.datetime.fromtimestamp(D1_S, TZ8).strftime("%Y-%m-%d %H:%M:%S"),
                      lines[2], "时间列不是北京时间")
        self.assertIn(dt.datetime.fromtimestamp(D2_S, TZ8).strftime("%Y-%m-%d %H:%M:%S"),
                      lines[1], "时间列不是北京时间")
        # 筛选后的导出同样只含匹配行
        r = self.client.get("/admin/export",
                            params={"table": "fills", "k": TOKEN, "df": D2_STR}).text
        self.assertEqual(len(r.lstrip("\ufeff").rstrip("\n").split("\n")) - 1, 1)

    def test_csv_formula_injection_sanitized(self):
        """审查 M2：以 = + - @ 开头的自由文本单元格加 ' 前缀，数值列原样。"""
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, "entry", 1, 1.0, 100.0,
                                 status="failed", error="=2+5")
        self.db.hl_log_event("decision", "-1+1|cmd", "BTC_USDT")
        self.add_flows([-5.0])
        r = self.client.get("/admin/export", params={"table": "orders", "k": TOKEN})
        self.assertTrue(r.text.rstrip("\n").endswith("'=2+5"),
                        "orders.error 以 = 开头未消毒，Excel 会当公式执行")
        r = self.client.get("/admin/export", params={"table": "events", "k": TOKEN})
        self.assertTrue(r.text.rstrip("\n").endswith("'-1+1|cmd"),
                        "events.message 以 - 开头未消毒")
        # 数值列/负数金额不动
        r = self.client.get("/admin/export", params={"table": "ledger", "k": TOKEN})
        self.assertIn("-5.0", r.text)
        self.assertNotIn("'-5.0", r.text, "数值列被误加前缀")

    def test_csv_streaming_large_table(self):
        """规格测试 4：超大表（2000 行）流式导出不炸、行数完整。"""
        # 测试专用批量直插（单事务；走 hl_log_event 会 2000 次 commit，没必要）
        self.db.conn.executemany(
            "INSERT INTO hl_events(kind, symbol, message, created_at) VALUES(?,?,?,?)",
            [("decision", "BTC_USDT", f"批量事件 {i}", D1_S + i) for i in range(2000)])
        self.db.conn.commit()
        r = self.client.get("/admin/export", params={"table": "events", "k": TOKEN})
        self.assertEqual(r.status_code, 200)
        lines = r.text.lstrip("\ufeff").rstrip("\n").split("\n")
        self.assertEqual(lines[0], "id,时间,类型,标的,内容")
        self.assertEqual(len(lines) - 1, 2000, "流式导出丢了行")


class LedgerSubpage(BaseCase):

    def test_balance_continuous_across_pages(self):
        """规格测试 5：余额全量正序预计算后切片 —— 跨页连续（不是页内现算）。"""
        # 12 条流水，amount = 1..12 → 全量余额 1,3,6,…,78
        self.add_flows([float(i + 1) for i in range(12)])
        r1 = self.client.get("/admin/list/ledger", params={"k": TOKEN, "n": "10"}).text
        self.assertIn("第 1–10 条 / 共 12 条", r1)
        p1 = self.ledger_pairs(r1)
        self.assertEqual(len(p1), 10)
        self.assertEqual(p1[0][1], 78.0, "最新一行的余额应为 78")
        r2 = self.client.get(self.links(r1, "下一页（更旧）")).text
        p2 = self.ledger_pairs(r2)
        self.assertEqual(len(p2), 2)
        # 跨页连续：第 2 页最新行的余额 = 第 1 页最旧行的余额 − 该行金额
        self.assertAlmostEqual(p2[0][1], p1[-1][1] - p1[-1][0], places=6)
        self.assertEqual(p2[0][1], 3.0, "第 2 页最新行（正序第 2 条）余额应为 1+2")
        self.assertEqual(p2[1][1], 1.0, "最早一条余额 = 期初 0 + 第一条")

    def test_filtered_balance_note_and_recalc(self):
        """规格测试 5：筛选后余额按「期初 0 + 筛选后全集」重算，且口径标注可见。"""
        self.add_flows([10.0, 20.0, -5.0])   # 两笔入金 + 一笔出金
        r = self.client.get("/admin/list/ledger",
                            params={"k": TOKEN, "kind": "deposit"}).text
        self.assertIn("余额按当前筛选范围累计", r, "筛选态必须出现余额口径标注")
        self.assertIn("共 2 条", r)
        pairs = self.ledger_pairs(r)
        self.assertEqual(pairs[-1][1], 10.0, "筛选态余额应从期初 0 重算（第一笔入金 = 10）")
        self.assertEqual(pairs[0][1], 30.0)
        # 无筛选时不出现该标注（全量口径就是账户口径）
        r = self.client.get("/admin/list/ledger", params={"k": TOKEN}).text
        self.assertNotIn("余额按当前筛选范围累计", r)


class MainRecordsRegression(BaseCase):

    def test_records_page_blocks_and_links(self):
        """规格测试 6：块位置不变、10/20/50 链接、「全部 ›」指向正确。"""
        self.add_orders(2)
        self.add_fills(n=2)
        self.add_alerts(2)
        self.add_events(2)
        self.add_positions([("BTC_USDT", "open", 1)])
        r = self.client.get("/admin", params={"k": TOKEN, "tab": "records"}).text
        # 块顺序：资金流水 → 记录 → 下单 → 成交 → 信号 → 提醒 → 事件
        order = [r.index("<h2>资金流水"), r.index("<h2>记录</h2>"),
                 r.index("下单记录（hl_orders"), r.index("成交（hl_fills"),
                 r.index("<h2>信号</h2>"), r.index("提醒日志（alert_logs"),
                 r.index("执行器事件（hl_events")]
        self.assertEqual(order, sorted(order), "记录页块顺序被改变了")
        # 「全部 ›」六条，指向各自的子页
        for slug in SLUGS:
            self.assertIn(f"href='/admin/list/{slug}?k={TOKEN}'",
                          r, f"缺 {slug} 的「全部 ›」入口")
        self.assertEqual(r.count("全部 ›"), 6)
        # 既有行数选择器不变
        for n in ("10", "20", "50"):
            self.assertIn(f"/admin/rows?n={n}&tab=records", r)
        # 既有文案/断言不回退
        self.assertIn("每页行数", r)
        # 成交流水已入库（v_ledger union hl_fills）→ 不应再显示空态
        self.assertNotIn("暂无流水", r, "有成交时空态文案仍在")

    def test_records_page_empty_ok(self):
        """空库时主记录页与六个子页都照常 200（fail-soft 回归）。"""
        r = self.client.get("/admin", params={"k": TOKEN, "tab": "records"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("暂无流水", r.text)
        for slug in SLUGS:
            rr = self.client.get(f"/admin/list/{slug}", params={"k": TOKEN})
            self.assertEqual(rr.status_code, 200, slug)


class SignalsTable(BaseCase):

    def test_subpage_filters(self):
        """规格测试 7：positions 子页按 symbol / status 筛选。"""
        self.add_positions([("BTC_USDT", "open", 1), ("BTC_USDT", "closed", 1),
                            ("ETH_USDT", "open", -1), ("ETH_USDT", "pending", 1)])
        r = self.client.get("/admin/list/positions",
                            params={"k": TOKEN, "symbol": "BTC_USDT"}).text
        self.assertIn("共 2 条", r)
        self.assertNotIn("<td>ETH</td>", self.tbody(r))
        r = self.client.get("/admin/list/positions",
                            params={"k": TOKEN, "symbol": "ETH_USDT",
                                    "status": "open"}).text
        self.assertIn("共 1 条", r)
        self.assertIn("<td>空</td>", self.tbody(r), "side=-1 应显示「空」")

    def test_main_page_behavior_unchanged(self):
        """规格测试 7：主记录页信号块行为与改动前一致（按每页行数收口的既有拍板）。

        注：spec 写「信号表主记录页全量不截断」，但现网代码自 2026-10-18 迁移起就是
        「按每页行数收口」（commit 357c970，注释在 render_records）——按铁律
        「不影响现有功能行为」，本 PR 不改这个行为，只加「全部 ›」入口。
        """
        self.add_positions([("BTC_USDT", "open", 1) for _ in range(12)])
        # 显式 n=10（默认 cookie 是 20，12 行会全显）：收口行为 = 按每页行数切片
        r = self.client.get("/admin", params={"k": TOKEN, "tab": "records", "n": "10"}).text
        seg = r.split("<h2>信号</h2>", 1)[1].split("<h2>", 1)[0].split("</table>")[0]
        self.assertEqual(seg.count("<tr>") - 1, 10, "信号块行数行为被改变了（应仍为每页行数）")
        # 子页才是全量 + 筛选浏览
        r = self.client.get("/admin/list/positions", params={"k": TOKEN}).text
        self.assertIn("共 12 条", r)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
