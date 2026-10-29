"""面板展示层字段修复的回归（fix/panel-fields）。

覆盖 6 处用户反馈的字段错误/口径缺失：

1. 资金费符号：HL `cumFunding.sinceOpen` 是**成本视角**（付出为正，空头收钱时为负），
   面板按**收付视角**渲染（正=收入）→ views 计算处取反，标签写明「正=收入」。
   同一个取反口径必须同时喂给 /api/hl/status 与 /api/hl/equity（同一消费者语义）。
2. 手续费标签：「正=收费」物理上不存在（HL 手续费恒为付出）→ 改「净手续费（付出）」；
   数字逻辑（-fees，付出显示为负）不动。
3. 数据源健康表头：原来「来源」列格子里渲染的是 正常/失败 → 表头改「状态」并挂口径 title。
4. 挂单表列名：值取的是挂单时间（od['timestamp']）→ 表头「触发时间」改「时间」。
5. `admin_ui._ALERT_KIND_TXT` 补 `tick_error`（批次 registry 新增的 kind，之前渲染英文原文）。
6. 口径 title：今日/累计已实现（closedPnl 未扣手续费与资金费）、卡单数（持续 3 分钟才计入）。

全部用临时 sqlite DB + 假 info，不触网。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.hl.views import DashboardDeps, build_router
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    DashboardDeps = build_router = None

FIXED_NOW = 1760000000.0          # 2025-10-09 15:33:20 UTC

CLOSED_PNL_TITLE = "交易所 closedPnl 口径，未扣手续费与资金费"
STUCK_TITLE = "持续 3 分钟未推进才计入"


def fake_snapshot(funding_since_open=-0.02):
    """与 HlInfo.snapshot 同口径的固定快照。

    funding_since_open 默认 -0.02 = 空头在正费率下**收钱**（成本视角为负）。
    """
    return {
        "ts": FIXED_NOW, "address": "0xTESTADDRESS123456", "equity": 1200.0,
        "withdrawable": 900.0, "margin_used": 260.0, "notional": 1300.0,
        "positions": [{
            "coin": "BTC", "side": -1, "size": 0.05, "entry_px": 100_000.0,
            "mark_px": 99_000.0, "position_value": 4_950.0, "liq_px": 110_000.0,
            "leverage": 5, "isolated": True, "margin_used": 260.0,
            "unrealized_pnl": 50.0, "roe": 0.02,
            "funding_since_open": funding_since_open,
        }],
        "open_orders": [{
            "coin": "BTC", "oid": 7, "is_buy": True, "sz": 0.05, "limit_px": 0.0,
            "reduce_only": True, "trigger_px": 103_000.0, "is_trigger": True,
            "timestamp": int(FIXED_NOW * 1000),
        }],
        "orders_trusted": True,
    }


class FakeInfo:
    """HlInfo 替身：固定快照，不触网。"""

    def __init__(self, snapshot=None):
        self._snap = snapshot if snapshot is not None else fake_snapshot()

    def snapshot(self, address, mids=None):
        return self._snap


class _Cfg:
    symbols = ["BTC_USDT", "ETH_USDT"]


class PanelFieldsBase(unittest.TestCase):
    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="panel_fields_")
        from app.db import DB
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.info = FakeInfo()
        self.deps = DashboardDeps(
            db=self.db, config_fn=lambda: _Cfg(), info=self.info,
            address_fn=lambda: "0xTESTADDRESS123456", mode_fn=lambda: "live",
            now_fn=lambda: FIXED_NOW)
        app = FastAPI()
        app.include_router(build_router(self.deps))
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def payload(self):
        from app.hl.views import status_payload
        return status_payload(self.deps)

    def html(self):
        r = self.client.get("/hl")
        self.assertEqual(r.status_code, 200)
        return r.text

    def insert_fill(self, tid, closed_pnl=0.0, fee=0.0):
        self.db.hl_upsert_fill(tid=tid, symbol="BTC_USDT", coin="BTC", side=-1, sz=0.05,
                              px=99_000.0, fee=fee, fee_token="USDT", dir="Close Short",
                              closed_pnl=closed_pnl, hash=tid, reduce_only=True,
                              ts=int(FIXED_NOW * 1000), created_at=FIXED_NOW)


# ---------------------------------------------------------------- 1. 资金费符号
class TestFundingSign(PanelFieldsBase):
    def test_short_receiving_renders_positive(self):
        """空头收钱（sinceOpen=-0.02）在面板上必须显示 +0.02（收入），不是支出。

        P2 消重后账户卡的「资金费」聚合行已删除，收付视角由仓位表
        「本单资金费」列唯一承载（每仓一份，不再同屏第二遍）。
        """
        html = self.html()
        self.assertIn("本单资金费", html)          # 列还在（仓位表唯一承载）
        self.assertIn(">+0.02 U<", html)
        self.assertNotIn(">-0.02 U<", html)
        self.assertNotIn("资金费（持仓期内", html)  # 账户卡聚合行已删（P2 消重）

    def test_paying_funding_renders_negative(self):
        """付出资金费（sinceOpen=+0.03）→ 面板显示 -0.03。"""
        self.info._snap = fake_snapshot(funding_since_open=0.03)
        html = self.html()
        self.assertIn(">-0.03 U<", html)

    def test_payload_funding_total_flipped(self):
        """payload 的 funding_total 与面板同口径（取反后的收付视角）。"""
        p = self.payload()
        self.assertAlmostEqual(p["account"]["funding_total"], 0.02)

    def test_json_consumers_share_flipped_convention(self):
        """/api/hl/status 与 /api/hl/equity 是同一取反口径（正=收入）。"""
        st = self.client.get("/api/hl/status")
        self.assertEqual(st.status_code, 200)
        self.assertAlmostEqual(st.json()["account"]["funding_total"], 0.02)
        eq = self.client.get("/api/hl/equity")
        self.assertEqual(eq.status_code, 200)
        self.assertAlmostEqual(eq.json()["funding_total"], 0.02)

    def test_old_label_gone(self):
        self.assertNotIn("资金费率（持仓期内）", self.html())


# ---------------------------------------------------------------- 2. 手续费标签
class TestFeeLabel(PanelFieldsBase):
    def test_label_and_negative_number(self):
        self.insert_fill("f1", closed_pnl=42.0, fee=0.25)
        html = self.html()
        self.assertIn("净手续费（付出）", html)
        self.assertIn(">-0.25 U<", html)          # 付出显示为负（数字逻辑不动）
        self.assertNotIn("净手续费（正=收费）", html)


# ---------------------------------------------------------------- 3/4. 表头
class TestTableHeaders(PanelFieldsBase):
    def test_feed_status_merged_into_symbol_card(self):
        """P2 合并：数据源健康表已删，每标的的数据源行进「三标的动态」卡。"""
        self.db.mark_feed("BTC_USDT", "binance", True)
        self.db.set_symbol_state("BTC_USDT", price=100.0)   # 让三标的卡渲染出该标的
        html = self.html()
        # 2026-10-29 精简：数据源正常时不渲染小字行（异常才出现，见 _feed_line）
        self.assertNotIn("数据源 binance 正常", html)
        # 旧表整块删除（不是折叠）：两个区块标题整页 0 处
        self.assertNotIn("数据源健康", html)
        self.assertNotIn("<div class='sec'>已处理到</div>", html)

    def test_orders_table_last_header_is_time(self):
        # P3 R1：挂单表只在异常态渲染 —— 加一条孤儿挂单把 fixture 推进异常态再钉表头
        snap = fake_snapshot()
        snap["open_orders"].append({"coin": "ETH", "oid": 8, "is_buy": True, "sz": 0.5,
                                    "limit_px": 0.0, "reduce_only": False,
                                    "trigger_px": 0.0, "is_trigger": False,
                                    "timestamp": int(FIXED_NOW * 1000)})
        self.info._snap = snap
        html = self.html()
        self.assertIn("<h2>交易所挂单</h2>", html)
        self.assertIn("<th class='num'>时间</th>", html)
        self.assertNotIn("触发时间", html)
        self.assertIn("<th class='num'>限价</th><th class='num'>触发价</th>", html)


# ---------------------------------------------------------------- 5. tick_error 映射
class TestAlertKindTxt(PanelFieldsBase):
    def test_mapping_has_tick_error(self):
        from app.admin_ui import _ALERT_KIND_TXT
        self.assertEqual(_ALERT_KIND_TXT.get("tick_error"), "处理异常告警")

    def test_records_tab_renders_chinese_kind(self):
        """提醒日志「类型」列走中文映射，不再渲染 tick_error 英文原文。"""
        from app.admin_ui import AdminDeps, render_records
        self.db.log_alert("BTC_USDT", None, "tick_error", "处理异常：SOL_USDT KeyError", True)
        deps = AdminDeps(db=self.db, config_fn=lambda: _Cfg(), hl_deps=self.deps,
                         mode_fn=lambda: "live", token_fn=lambda: "tk")
        body = render_records(deps)
        self.assertIn("处理异常告警", body)
        self.assertNotIn(">tick_error<", body)


# ---------------------------------------------------------------- 6. 口径 title
class TestCaliberTitles(PanelFieldsBase):
    def test_closed_pnl_and_stuck_titles(self):
        html = self.html()
        # P3 R5：「累计已实现」移入绩效卡 U 对照行，其 title 只剩「今日已实现」一处
        self.assertEqual(html.count(f"title='{CLOSED_PNL_TITLE}'"), 1)   # 今日
        # P3 R2：卡单移状态条徽章（只在 >0 时出现）—— 默认 fixture 无卡单 → 词面 0 处
        self.assertNotIn(f"title='{STUCK_TITLE}'", html)
        # P2 分层后「今日已实现」仍是账户卡次级的可见词面；「累计已实现」唯一出处=绩效卡
        self.assertIn("今日已实现", html)
        self.assertIn("累计已实现", html)
        self.assertNotIn("卡在 intent/sent 的下单", html)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
