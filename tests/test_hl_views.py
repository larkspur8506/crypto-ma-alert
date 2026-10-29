"""Hyperliquid 监控页（app/hl/views.py）的单元测试。

覆盖点：
- /hl 返回 200、中文标题、自包含（零外部 CDN/JS）
- /api/hl/status JSON schema 与类型
- info 抛异常 / 未配置时仍 200 且 reachable=False
- has_stop 判定（reduce_only + is_trigger 挂单）
- curve（盈亏曲线 + TWR）从 hl_snapshots 30 天窗口算，出入金已剔除
- 对账 mismatch、kill switch、meta refresh 等

全部用临时 sqlite DB（app.db.DB(tmp_path)）+ 假 info 对象，不触网。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.hl.views import (DashboardDeps, build_router, render_dashboard,
                              status_payload)
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = None
    TestClient = None
    DashboardDeps = build_router = render_dashboard = status_payload = None

FIXED_NOW = 1760000000.0          # 2025-10-09 15:33:20 UTC
TODAY_START = 1759968000.0        # 2025-10-09 00:00:00 UTC
YESTERDAY = TODAY_START - 3600.0  # 前一天


def fake_snapshot(address="0xTESTADDRESS123456", with_stop=True, upnl=50.0, equity=1200.0,
                  extra_positions=(), extra_orders=()):
    """构造一个与 HlInfo.snapshot 口径一致的固定快照。"""
    positions = [{
        "coin": "BTC", "side": 1, "size": 0.05, "entry_px": 100_000.0,
        "mark_px": 101_000.0, "position_value": 5_050.0, "liq_px": 90_000.0,
        "leverage": 5, "isolated": True, "margin_used": 260.0,
        "unrealized_pnl": upnl, "roe": 0.02, "funding_since_open": -1.5,
    }] + list(extra_positions)
    orders = []
    if with_stop:
        orders.append({"coin": "BTC", "oid": 7, "is_buy": False, "sz": 0.05, "limit_px": 0.0,
                       "reduce_only": True, "trigger_px": 97_000.0, "is_trigger": True,
                       "timestamp": int(FIXED_NOW * 1000)})
    orders.extend(extra_orders)
    return {"ts": FIXED_NOW, "address": address, "equity": equity, "withdrawable": 900.0,
            "margin_used": 260.0, "notional": 1_300.0,
            "positions": positions, "open_orders": orders, "orders_trusted": True}


class FakeInfo:
    """HlInfo 替身：返回固定快照。raise_=True 模拟只读接口失败。"""

    def __init__(self, snapshot=None, raise_=False):
        self._snap = snapshot if snapshot is not None else fake_snapshot()
        self.raise_ = raise_
        self.calls = 0

    def snapshot(self, address, mids=None):
        self.calls += 1
        if self.raise_:
            raise RuntimeError("boom: network down")
        return self._snap


class BaseCase(unittest.TestCase):
    """每个用例一个干净的临时 DB + 假 info + FastAPI app。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="hl_views_")
        from app.db import DB
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.info = FakeInfo()
        self.deps = DashboardDeps(
            db=self.db, config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT", "ETH_USDT"]})(),
            info=self.info, address_fn=lambda: "0xTESTADDRESS123456",
            mode_fn=lambda: "live", now_fn=lambda: FIXED_NOW)
        app = FastAPI()
        app.include_router(build_router(self.deps))
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 取数辅助 ----
    def payload(self):
        from app.hl.views import status_payload
        return status_payload(self.deps)

    def api(self):
        return self.client.get("/api/hl/status").json()

    def insert_fill(self, tid, closed_pnl=0.0, fee=0.1, ts=None, coin="BTC"):
        self.db.hl_upsert_fill(
            tid=tid, symbol="BTC_USDT", coin=coin, side=1, sz=0.05, px=101_000.0,
            fee=fee, fee_token="USDT", dir="a", closed_pnl=closed_pnl, hash=tid,
            reduce_only=bool(closed_pnl), ts=int((ts if ts is not None else FIXED_NOW) * 1000),
            created_at=FIXED_NOW)

    def insert_position(self, status="open", coin="BTC"):
        pid = self.db.create_position(
            symbol="BTC_USDT" if coin == "BTC" else f"{coin}_USDT",
            signal_bar_ms=1_700_000_000_000, status=status, side=1,
            entry_price=100_000.0, stop_price=97_000.0, stop_dist=0.03, contracts=6,
            coin_qty=0.0006, notional=60.0, risk_u=18.0, equity_at_entry=1000.0)
        if status == "open":
            self.db.set_pos_hl(pid, hl_status="open", hl_stop_oid=7, hl_sz=0.05)
        return pid

    def add_stuck_order(self, bar_ms=1_700_000_001_000):
        oid = self.db.hl_record_intent("BTC_USDT", "BTC", bar_ms, "entry", 1, 0.05,
                                       status="sent")
        self.db.conn.execute(
            "UPDATE hl_orders SET created_at=?, updated_at=? WHERE id=?",
            (time.time() - 3600, time.time() - 3600, oid))
        self.db.conn.commit()


# ---------------------------------------------------------------- /hl 页面
class TestHtmlPage(BaseCase):
    def test_hl_page_200_with_chinese_title(self):
        r = self.client.get("/hl")
        self.assertEqual(r.status_code, 200)
        self.assertIn("双均线交易台", r.text)
        self.assertIn("<!doctype html>", r.text.lower())
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/html")

    def test_hl_page_has_all_sections(self):
        self.insert_position()
        self.insert_fill("f1", closed_pnl=25.0)
        self.db.hl_log_event("decision", "双均线金叉开多", symbol="BTC_USDT")
        self.db.mark_feed("BTC_USDT", "binance", True)
        r = self.client.get("/hl")
        self.assertEqual(r.status_code, 200)
        # P3 R1：正常态（1 仓 + 1 条 reduce-only 触发单）挂单表整块不渲染（止损由仓位表表达）
        for text in ("账户", "盈亏曲线（已剔除出入金）", "仓位", "执行器"):
            self.assertIn(text, r.text, f"缺少区块：{text}")
        self.assertNotIn("交易所挂单", r.text, "正常态挂单表不该渲染（P3 R1）")
        # 2026-10-18：下单记录 / 最近成交 / 信号 三张历史表已从总览（含 /hl 这张
        # 独立页，同一份 _render_body）迁到 /admin?tab=records → 这里断言"不再出现"。
        # 用 <h2> 锚点：「信号源固定 hl-candle」这类正文里也有「信号」二字
        for gone in ("<h2>下单记录", "<h2>最近成交", "<h2>信号</h2>"):
            self.assertNotIn(gone, r.text, f"总览不该再有历史表：{gone}")
        # 数据真的渲染进去了
        self.assertIn("BTC", r.text)
        self.assertIn("多", r.text)

    def test_hl_page_is_self_contained_no_external_resources(self):
        self.insert_position()
        self.insert_fill("f1")
        html = self.client.get("/hl").text
        for bad in ("http://cdn", "https://unpkg", "<script src", "https://cdn",
                    "https://unpkg.com", "cdnjs.cloudflare", "//fonts.googleapis"):
            self.assertNotIn(bad, html, f"出现外链资源：{bad}")
        self.assertNotIn("<script", html, "页面不允许内联 <script>")
        self.assertIn("<style>", html, "样式必须内联")

    def test_meta_refresh_for_valid_intervals(self):
        for n in (10, 30, 60):
            html = self.client.get(f"/hl?refresh={n}").text
            self.assertIn(f"http-equiv='refresh' content='{n}'", html, f"refresh={n} 未生效")

    def test_meta_refresh_ignored_for_invalid_interval(self):
        html = self.client.get("/hl?refresh=600").text
        self.assertNotIn("http-equiv='refresh'", html)
        html = self.client.get("/hl?refresh=ab").text
        self.assertNotIn("http-equiv='refresh'", html)

    def test_hl_page_shows_kill_switch_and_reason(self):
        self.db.hl_set_kill(True, "回撤超阈值")
        html = self.client.get("/hl").text
        self.assertIn("熔断 <b>已触发</b>", html)
        self.assertIn("回撤超阈值", html)

    def test_hl_page_kill_off_shows_未触发(self):
        html = self.client.get("/hl").text
        self.assertIn("熔断 <b>未触发</b>", html)

    def test_hl_page_svg_polylines_for_pnl_curve(self):
        # 2026-10-22（feat/pnl-curve）：曲线源从 equity 换成「剔除出入金的 pnl」，
        # 且读侧要求「本地有流水」或「成功同步过水位」才画（否则给提示，不画误导线）。
        now = FIXED_NOW
        self.db.set_config("hl_flows_synced_at", str(int(now)))
        for i in range(10):
            self.db.hl_add_snapshot({"ts": now - (9 - i) * 3600, "equity": 1000.0 + i * 10,
                                     "positions": []})
        html = self.client.get("/hl").text
        self.assertIn("<svg", html)
        self.assertIn("<polyline", html)
        self.assertIn("盈亏曲线（已剔除出入金）", html)
        self.assertNotIn("暂无数据（等执行器落快照", html)

    def test_hl_page_svg_placeholder_when_no_curve(self):
        self.db.set_config("hl_flows_synced_at", str(int(FIXED_NOW)))   # 同步过，但没快照
        html = self.client.get("/hl").text
        self.assertIn("暂无数据（等执行器落快照", html)
        self.assertNotIn("<polyline", html)

    def test_hl_page_info_raises_still_200_with_banner(self):
        self.info.raise_ = True
        r = self.client.get("/hl")
        self.assertEqual(r.status_code, 200)
        self.assertIn("未配置 / 读取失败", r.text)
        self.assertIn("boom: network down", r.text)
        self.assertIn("双均线交易台", r.text)

    def test_hl_page_unconfigured_info_and_address(self):
        from app.hl.views import DashboardDeps, build_router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(build_router(DashboardDeps(
            db=self.db, config_fn=lambda: None, info=None,
            address_fn=lambda: "", mode_fn=lambda: "off")))
        r = TestClient(app).get("/hl")
        self.assertEqual(r.status_code, 200)
        self.assertIn("未配置 / 读取失败", r.text)
        self.assertIn("钱包地址", r.text)

    def test_hl_page_funding_and_today_realized_rendered(self):
        self.insert_fill("f1", closed_pnl=42.0, fee=0.25, ts=FIXED_NOW)
        html = self.client.get("/hl").text
        self.assertIn("+42.00 U", html)      # 今日已实现
        self.assertIn("-0.25 U", html)       # 累计手续费（负号）
        # 成交表（含手续费 4 位小数）2026-10-18 已迁到 /admin?tab=records，
        # 那条断言在 tests/test_admin_ui.py TestRecordsTab 里覆盖。
        # 资金费按收付视角渲染（正=收入）：快照里 sinceOpen=-1.5 = 收 1.5U
        self.assertIn("+1.50 U", html)


# ---------------------------------------------------------------- JSON API
class TestStatusPayload(BaseCase):
    TOP_KEYS = ("schema_version", "ts", "mode", "kill", "kill_reason", "network", "account",
                "positions", "orders_open", "orders_recent", "fills", "signals", "system",
                "curve", "events")

    def test_schema_version_and_top_level_keys(self):
        p = self.payload()
        for k in self.TOP_KEYS:
            self.assertIn(k, p, f"缺少顶层字段 {k}")
        self.assertEqual(p["schema_version"], 1)
        self.assertIsInstance(p["schema_version"], int)
        self.assertIsInstance(p["ts"], (int, float))
        self.assertEqual(p["mode"], "live")
        self.assertIsInstance(p["kill"], bool)
        self.assertIsInstance(p["kill_reason"], str)
        for k in ("network", "account", "system", "curve"):
            self.assertIsInstance(p[k], dict, k)
        for k in ("positions", "orders_open", "orders_recent", "fills", "signals", "events"):
            self.assertIsInstance(p[k], list, k)

    def test_network_and_account_field_types(self):
        p = self.payload()
        n = p["network"]
        self.assertEqual(n["base_url"], "https://api.hyperliquid.xyz")
        self.assertIsInstance(n["testnet"], bool)
        self.assertTrue(n["reachable"])
        self.assertIsInstance(n["latency_ms"], (int, float))
        self.assertIsInstance(n["error"], str)
        self.assertEqual(n["error"], "")
        a = p["account"]
        self.assertEqual(a["address"], "0xTESTADDRESS123456")
        self.assertAlmostEqual(a["equity"], 1200.0)
        self.assertAlmostEqual(a["withdrawable"], 900.0)
        self.assertAlmostEqual(a["margin_used"], 260.0)
        self.assertAlmostEqual(a["notional"], 1300.0)
        self.assertAlmostEqual(a["unrealized_pnl"], 50.0)
        # 资金费取「收付视角」（正=收入）：cumFunding.sinceOpen 是成本视角，故取反
        self.assertAlmostEqual(a["funding_total"], 1.5)
        for k in ("today_realized", "total_realized", "fees_total"):
            self.assertIsInstance(a[k], (int, float), k)

    def test_testnet_detection_from_base_url(self):
        from app.hl.views import DashboardDeps, status_payload
        deps = DashboardDeps(db=self.db, config_fn=lambda: None, info=self.info,
                             address_fn=lambda: "0xA", mode_fn=lambda: "dry",
                             base_url_fn=lambda: "https://api.hyperliquid-testnet.xyz")
        n = status_payload(deps)["network"]
        self.assertTrue(n["testnet"])
        self.assertIn("testnet", n["base_url"])

    def test_position_fields_and_types(self):
        p = self.payload()
        self.assertEqual(len(p["positions"]), 1)
        pos = p["positions"][0]
        for k, want in (("coin", str), ("size", float), ("entry_px", float),
                        ("mark_px", float), ("position_value", float), ("liq_px", float),
                        ("margin_used", float), ("unrealized_pnl", float),
                        ("stop_px", float), ("stop_dist_pct", float),
                        ("r_unrealized", float)):
            self.assertIn(k, pos, f"positions 缺少 {k}")
            self.assertIsInstance(pos[k], want, f"{k} 类型应为 {want}")
        for k in ("side", "leverage"):
            self.assertIsInstance(pos[k], int, k)
        for k in ("isolated", "has_stop"):
            self.assertIsInstance(pos[k], bool, k)
        self.assertEqual(pos["coin"], "BTC")
        self.assertEqual(pos["side"], 1)
        self.assertAlmostEqual(pos["size"], 0.05)
        self.assertAlmostEqual(pos["entry_px"], 100_000.0)
        self.assertAlmostEqual(pos["mark_px"], 101_000.0)
        self.assertAlmostEqual(pos["liq_px"], 90_000.0)
        self.assertAlmostEqual(pos["unrealized_pnl"], 50.0)
        self.assertEqual(pos["leverage"], 5)
        self.assertTrue(pos["isolated"])

    def test_has_stop_true_from_reduce_only_trigger_order(self):
        p = self.payload()
        pos = p["positions"][0]
        self.assertTrue(pos["has_stop"])
        self.assertAlmostEqual(pos["stop_px"], 97_000.0)
        # 止损距离 = |97000-101000| / 101000
        self.assertAlmostEqual(pos["stop_dist_pct"], 4_000.0 / 101_000.0)
        # R = 浮盈 / (|入场-止损| × 数量) = 50 / (3000 × 0.05)
        self.assertAlmostEqual(pos["r_unrealized"], round(50.0 / 150.0, 4))

    def test_has_stop_false_when_only_limit_order_exists(self):
        self.info._snap = fake_snapshot(
            with_stop=False,
            extra_orders=[{"coin": "BTC", "oid": 9, "is_buy": False, "sz": 0.05,
                           "limit_px": 96_000.0, "reduce_only": True, "trigger_px": 0.0,
                           "is_trigger": False, "timestamp": 0}])
        pos = self.payload()["positions"][0]
        self.assertFalse(pos["has_stop"])
        self.assertIsNone(pos["stop_px"])

    def test_has_stop_false_when_order_not_reduce_only(self):
        self.info._snap = fake_snapshot(
            with_stop=False,
            extra_orders=[{"coin": "BTC", "oid": 9, "is_buy": True, "sz": 0.05,
                           "limit_px": 0.0, "reduce_only": False, "trigger_px": 96_000.0,
                           "is_trigger": True, "timestamp": 0}])
        pos = self.payload()["positions"][0]
        self.assertFalse(pos["has_stop"], "非 reduce_only 的触发单不算止损")
        self.assertIsNone(pos["stop_px"])

    def test_position_matches_db_row_for_signal_and_id(self):
        pid = self.insert_position()
        pos = self.payload()["positions"][0]
        self.assertEqual(pos["position_id"], pid)
        self.assertEqual(pos["signal_bar_ms"], 1_700_000_000_000)

    def test_position_without_db_row_has_null_refs(self):
        pos = self.payload()["positions"][0]
        self.assertIsNone(pos["position_id"])
        self.assertIsNone(pos["signal_bar_ms"])

    def test_orders_open_passthrough(self):
        p = self.payload()
        self.assertEqual(len(p["orders_open"]), 1)
        o = p["orders_open"][0]
        self.assertEqual(o["coin"], "BTC")
        self.assertEqual(o["oid"], 7)
        self.assertFalse(o["is_buy"])
        self.assertTrue(o["reduce_only"])
        self.assertTrue(o["is_trigger"])
        self.assertAlmostEqual(o["sz"], 0.05)
        self.assertAlmostEqual(o["trigger_px"], 97_000.0)

    def test_today_realized_only_counts_utc_day(self):
        # 日界 = 北京时间 0 点（与展示层一致）。FIXED_NOW=15:33 UTC=北京 23:33；
        # YESTERDAY=前一日 23:00 UTC=北京次日 07:00 → 仍在北京「今日」内。
        self.insert_fill("f_today", closed_pnl=40.0, ts=FIXED_NOW)
        self.insert_fill("f_yesterday", closed_pnl=-15.0, ts=YESTERDAY)
        self.insert_fill("f_older", closed_pnl=5.0, ts=TODAY_START - 9 * 86400)
        a = self.payload()["account"]
        self.assertAlmostEqual(a["today_realized"], 25.0, places=6)
        self.assertAlmostEqual(a["total_realized"], 40.0 - 15.0 + 5.0, places=6)
        self.assertEqual(len(self.payload()["fills"]), 3)

    def test_fees_total_sums_all_fills(self):
        self.insert_fill("f1", fee=0.10)
        self.insert_fill("f2", fee=0.25, ts=YESTERDAY)
        a = self.payload()["account"]
        self.assertAlmostEqual(a["fees_total"], 0.35, places=6)

    def test_fill_fields_and_types(self):
        self.insert_fill("f1", closed_pnl=12.5, fee=0.2, ts=FIXED_NOW)
        f = self.payload()["fills"][0]
        self.assertEqual(f["coin"], "BTC")
        self.assertEqual(f["side"], 1)
        self.assertEqual(f["dir"], "a")
        self.assertAlmostEqual(f["sz"], 0.05)
        self.assertAlmostEqual(f["px"], 101_000.0)
        self.assertAlmostEqual(f["fee"], 0.2)
        self.assertAlmostEqual(f["closed_pnl"], 12.5)
        self.assertIsInstance(f["reduce_only"], bool)
        self.assertIsInstance(f["ts"], (int, float))
        self.assertAlmostEqual(f["ts"], FIXED_NOW * 1000)

    def test_curve_windowed_30d_and_filters_older(self):
        """2026-10-22：payload 的 curve 只有一条 30 天序列（pnl + twr），不再分 7d/30d。

        展示层（近 7 天优先）在 views._curve_window 里切，JSON 只给原始 30 天。
        """
        now = FIXED_NOW
        for i in range(40):                     # 最近 40h
            self.db.hl_add_snapshot({"ts": now - i * 3600, "equity": 1000.0 + i, "positions": []})
        self.db.hl_add_snapshot({"ts": now - 8 * 86400, "equity": 7000.0, "positions": []})
        self.db.hl_add_snapshot({"ts": now - 20 * 86400, "equity": 8000.0, "positions": []})
        self.db.hl_add_snapshot({"ts": now - 60 * 86400, "equity": 1.0, "positions": []})

        curve = self.payload()["curve"]
        pnl, twr = curve["pnl"], curve["twr"]
        self.assertEqual(len(pnl), 42, "30d 应含 40h + 8d + 20d 两点（60 天前的丢掉）")
        self.assertEqual(len(twr), len(pnl), "pnl 与 twr 同一条时间轴")
        for pts in (pnl, twr):
            self.assertTrue(all(isinstance(x, list) and len(x) == 2 for x in pts))
            self.assertTrue(all(isinstance(x[0], (int, float))
                                and isinstance(x[1], (int, float)) for x in pts))
        # 2026-10-22：pnl 改为「相对期初权益的增量」（期初权益 = 窗口内第一条快照，
        # 这里 = 8000），所以最早一点是 0，后续是相减后的真实盈亏。
        self.assertAlmostEqual(pnl[0][1], 0.0, msg="期初权益是基线，最早一点为 0")
        self.assertAlmostEqual(pnl[1][1], -1000.0, msg="8 天点：7000 − 8000")
        self.assertAlmostEqual(pnl[-1][1], -7000.0, msg="最新点：1000 − 8000")
        self.assertAlmostEqual(twr[0][1], 100.0, msg="指数起点 100")
        self.assertAlmostEqual(curve["meta"]["equity_base"], 8000.0,
                               msg="期初权益写进 meta 便于核对")
        self.assertNotIn(1.0, [x[1] for x in pnl], "60 天前的点应被过滤掉")
        meta = curve["meta"]
        for k in ("net_deposit", "pnl_total", "pnl_7d", "max_dd_pnl",
                  "twr_total_pct", "flows_ok", "flows_note"):
            self.assertIn(k, meta, f"curve.meta 缺少 {k}")
        self.assertIsInstance(meta["flows_ok"], bool)

    def test_signals_from_positions_table(self):
        self.insert_position(status="open")
        pid2 = self.db.create_position(
            symbol="ETH_USDT", signal_bar_ms=1_700_001_000_000, status="closed", side=-1,
            entry_price=3_000.0, stop_price=3_100.0, stop_dist=0.033, contracts=4,
            coin_qty=0.4, notional=1200.0, risk_u=40.0)
        self.db.close_position(pid2, 2_900.0, "ma20")
        signals = self.payload()["signals"]
        self.assertEqual(len(signals), 2)
        got = {s["symbol"]: s for s in signals}
        self.assertEqual(set(got), {"BTC_USDT", "ETH_USDT"})
        self.assertEqual(got["BTC_USDT"]["status"], "open")
        self.assertEqual(got["BTC_USDT"]["side"], 1)
        self.assertAlmostEqual(got["BTC_USDT"]["entry"], 100_000.0)
        self.assertAlmostEqual(got["BTC_USDT"]["stop"], 97_000.0)
        self.assertEqual(got["BTC_USDT"]["contracts"], 6)
        self.assertEqual(got["BTC_USDT"]["bar_ms"], 1_700_000_000_000)
        self.assertEqual(got["ETH_USDT"]["status"], "closed")
        self.assertEqual(got["ETH_USDT"]["side"], -1)

    def test_orders_recent_from_hl_orders(self):
        oid_row = self.db.hl_record_intent("BTC_USDT", "BTC", 1_700_000_000_000, "entry", 1,
                                           0.05, px=100_000.0, status="resting")
        self.db.hl_update_order("BTC_USDT", 1_700_000_000_000, "entry",
                                status="resting", oid=77)
        self.assertEqual(oid_row, 1)
        self.db.hl_log_event("order", "已挂单", symbol="BTC_USDT")
        p = self.payload()
        self.assertEqual(len(p["orders_recent"]), 1)
        o = p["orders_recent"][0]
        self.assertEqual(o["symbol"], "BTC_USDT")
        self.assertEqual(o["coin"], "BTC")
        self.assertEqual(o["purpose"], "entry")
        self.assertEqual(o["status"], "resting")
        self.assertAlmostEqual(o["sz"], 0.05)
        self.assertEqual(o["oid"], 77)
        self.assertIn("updated_at", o)
        self.assertEqual(len(p["events"]), 1)
        self.assertEqual(p["events"][0]["kind"], "order")
        self.assertEqual(p["events"][0]["symbol"], "BTC_USDT")
        self.assertIn("已挂单", p["events"][0]["message"])
        ev_at = p["system"]["executor_last_event_at"]
        self.assertIsInstance(ev_at, (int, float))
        self.assertAlmostEqual(ev_at, p["events"][0]["ts"], places=6)


class TestSystemAndReconcile(BaseCase):
    def test_system_field_types(self):
        s = self.payload()["system"]
        for k in ("feed", "last_bar", "executor_last_event_at", "stuck_orders", "mismatch"):
            self.assertIn(k, s, f"system 缺少 {k}")
        self.assertIsInstance(s["feed"], list)
        self.assertIsInstance(s["last_bar"], dict)
        self.assertIsInstance(s["stuck_orders"], int)
        self.assertIsInstance(s["mismatch"], bool)

    def test_mismatch_false_when_db_matches_snapshot(self):
        self.insert_position()      # BTC open + hl_stop_oid=7；快照有 BTC 和 BTC 触发单
        self.assertFalse(self.payload()["system"]["mismatch"])

    def test_mismatch_true_when_db_open_missing_from_snapshot(self):
        self.insert_position()
        self.insert_position(coin="ETH")     # DB 有 ETH open，快照里没有
        self.assertTrue(self.payload()["system"]["mismatch"])

    def test_mismatch_true_when_stop_oid_recorded_but_no_trigger_order(self):
        self.insert_position()
        self.info._snap = fake_snapshot(with_stop=False)
        self.assertTrue(self.payload()["system"]["mismatch"])

    def test_mismatch_true_when_snapshot_has_untracked_position(self):
        self.info._snap = fake_snapshot(
            extra_positions=[{"coin": "ETH", "side": -1, "size": 1.0, "entry_px": 3000.0,
                              "mark_px": 3010.0, "position_value": 3010.0, "liq_px": 2500.0,
                              "leverage": 3, "isolated": True, "margin_used": 100.0,
                              "unrealized_pnl": -10.0, "roe": -0.1, "funding_since_open": 0.0}])
        self.assertTrue(self.payload()["system"]["mismatch"])

    def test_stuck_orders_counted(self):
        self.db.hl_record_intent("BTC_USDT", "BTC", 1_700_000_000_000, "entry", 1, 0.05,
                                 status="resting")
        self.assertEqual(self.payload()["system"]["stuck_orders"], 0)
        self.add_stuck_order()
        self.assertEqual(self.payload()["system"]["stuck_orders"], 1)
        self.assertTrue(self.payload()["system"]["mismatch"])

    def test_feed_status_and_last_bar(self):
        self.db.mark_feed("BTC_USDT", "binance", True)
        self.db.mark_feed("ETH_USDT", "binance", False, error="HTTP 429")
        self.db.set_last_bar("BTC_USDT", 1_700_000_000_000)
        s = self.payload()["system"]
        self.assertEqual(len(s["feed"]), 2)
        feeds = {f["symbol"]: f for f in s["feed"]}
        self.assertTrue(feeds["BTC_USDT"]["ok"])
        self.assertEqual(feeds["BTC_USDT"]["source"], "binance")
        self.assertFalse(feeds["ETH_USDT"]["ok"])
        self.assertIn("429", str(feeds["ETH_USDT"]["last_error"]))
        self.assertEqual(s["last_bar"]["BTC_USDT"], 1_700_000_000_000)
        self.assertIsNone(s["last_bar"]["ETH_USDT"])


class TestFailureModes(BaseCase):
    def test_info_raises_still_200_and_unreachable(self):
        self.info.raise_ = True
        r = self.client.get("/api/hl/status")
        self.assertEqual(r.status_code, 200)
        p = r.json()
        self.assertFalse(p["network"]["reachable"])
        self.assertIn("boom", p["network"]["error"])
        self.assertEqual(p["network"]["base_url"], "https://api.hyperliquid.xyz")
        self.assertEqual(p["positions"], [])
        self.assertEqual(p["orders_open"], [])
        for k, v in p["account"].items():
            if k == "address":
                self.assertEqual(v, "0xTESTADDRESS123456")
            elif isinstance(v, bool):
                continue          # spot_ok 等标志位不是金额
            else:
                self.assertEqual(v, 0.0, f"account.{k} 应为 0")
        self.assertEqual(p["account"]["funding_total"], 0.0)

    def test_html_and_json_endpoints_match_on_failure(self):
        self.info.raise_ = True
        html = self.client.get("/hl").text
        js = self.client.get("/api/hl/status").json()
        self.assertIn("boom: network down", html)
        self.assertFalse(js["network"]["reachable"])

    def test_empty_address_is_unreachable(self):
        from app.hl.views import status_payload
        deps = DashboardDeps(db=self.db, config_fn=lambda: None, info=self.info,
                             address_fn=lambda: "", mode_fn=lambda: "live")
        n = status_payload(deps)["network"]
        self.assertFalse(n["reachable"])
        self.assertEqual(n["error"], "未配置钱包地址")
        self.assertEqual(status_payload(deps)["account"]["equity"], 0.0)

    def test_none_info_is_unreachable(self):
        from app.hl.views import status_payload
        deps = DashboardDeps(db=self.db, config_fn=lambda: None, info=None,
                             address_fn=lambda: "0xA", mode_fn=lambda: "live")
        n = status_payload(deps)["network"]
        self.assertFalse(n["reachable"])
        self.assertEqual(n["error"], "未配置只读客户端")

    def test_broken_db_methods_do_not_raise(self):
        class Broken:
            def __getattr__(self, name):
                raise RuntimeError("db gone")

            def hl_kill(self):
                raise RuntimeError("db gone")

            def get_config(self):
                raise RuntimeError("db gone")

        from app.hl.views import status_payload, render_dashboard
        deps = DashboardDeps(db=Broken(), config_fn=lambda: None, info=self.info,
                             address_fn=lambda: "0xA", mode_fn=lambda: "live")
        p = status_payload(deps)
        self.assertEqual(p["schema_version"], 1)
        self.assertEqual(p["positions"], [dict(p["positions"][0])])
        self.assertEqual(p["fills"], [])
        self.assertEqual(p["signals"], [])
        self.assertEqual(p["system"]["stuck_orders"], 0)
        self.assertIsInstance(render_dashboard(p), str)

    def test_broken_config_fn_does_not_raise(self):
        from app.hl.views import status_payload
        deps = DashboardDeps(db=self.db, config_fn=lambda: (_ for _ in ()).throw(ValueError("x")),
                             info=self.info, address_fn=lambda: "0xA",
                             mode_fn=lambda: "off")
        p = status_payload(deps)
        self.assertEqual(p["schema_version"], 1)
        self.assertEqual(p["system"]["last_bar"], {})

    def test_render_dashboard_tolerates_garbage_payload(self):
        from app.hl.views import render_dashboard
        for bad in ({}, {"positions": None, "account": None}, {"curve": "oops"},
                    {"system": {"mismatch": "nope"}}, {"positions": [{"coin": None}]}):
            out = render_dashboard(bad)
            self.assertIsInstance(out, str)
            self.assertIn("双均线交易台", out)
            self.assertNotIn("http://cdn", out)


class TestOtherRoutes(BaseCase):
    def test_api_status_route_schema(self):
        r = self.client.get("/api/hl/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["schema_version"], 1)
        self.assertEqual(r.headers["content-type"].split(";")[0], "application/json")

    def test_api_equity_route(self):
        self.insert_fill("f1", closed_pnl=33.0, ts=FIXED_NOW)
        self.db.hl_add_snapshot({"ts": FIXED_NOW, "equity": 1234.0, "positions": []})
        r = self.client.get("/api/hl/equity")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for k in ("ts", "mode", "kill", "reachable", "equity", "unrealized_pnl",
                  "today_realized", "total_realized", "curve"):
            self.assertIn(k, j, f"equity 接口缺少 {k}")
        self.assertAlmostEqual(j["equity"], 1200.0)
        self.assertAlmostEqual(j["today_realized"], 33.0)
        self.assertEqual(j["mode"], "live")
        self.assertTrue(j["reachable"])
        self.assertIsInstance(j["curve"], dict)

    def test_api_snapshot_route(self):
        r = self.client.get("/api/hl/snapshot")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(set(j), {"ts", "network", "account", "positions", "orders_open"})
        self.assertTrue(j["network"]["reachable"])
        self.assertEqual(len(j["positions"]), 1)
        self.assertEqual(j["positions"][0]["coin"], "BTC")
        self.assertEqual(len(j["orders_open"]), 1)

    def test_optional_admin_token_gate(self):
        os.environ["HL_DASH_REQUIRE_ADMIN"] = "1"
        try:
            from app.hl.views import DashboardDeps, build_router
            from fastapi import FastAPI
            deps = DashboardDeps(db=self.db, config_fn=lambda: type("C", (),
                                                          {"symbols": [], "admin_token": "secret"})(),
                                 info=self.info, address_fn=lambda: "0xA",
                                 mode_fn=lambda: "live")
            app = FastAPI()
            app.include_router(build_router(deps))
            c = TestClient(app)
            self.assertEqual(c.get("/hl").status_code, 401)
            self.assertEqual(c.get("/api/hl/status").status_code, 401)
            self.assertEqual(c.get("/hl", params={"k": "secret"}).status_code, 200)
            self.assertEqual(c.get("/api/hl/status", params={"k": "secret"}).json()["schema_version"], 1)
        finally:
            os.environ.pop("HL_DASH_REQUIRE_ADMIN", None)


class TestRenderOnly(BaseCase):
    """render_dashboard 单独验证（不经过 HTTP）。"""

    def test_render_dashboard_contains_chinese_titles(self):
        from app.hl.views import render_dashboard
        html = render_dashboard(self.payload())
        for text in ("双均线交易台", "账户", "仓位", "盈亏曲线（已剔除出入金）", "执行器"):
            self.assertIn(text, html)

    def test_render_dashboard_stop_marks(self):
        from app.hl.views import render_dashboard
        self.insert_position()
        html = render_dashboard(self.payload())
        self.assertIn("10004", html)      # &#10004; ✓
        self.insert_position(coin="ETH")  # ETH 有仓但无止损单 → 另一行显示 ❌
        self.info._snap = fake_snapshot(with_stop=False)
        html = render_dashboard(self.payload())
        self.assertIn("10060", html)      # &#10060; ✗

    def test_render_dashboard_pnl_colors(self):
        from app.hl.views import render_dashboard
        self.insert_position()
        good = render_dashboard(self.payload())
        self.assertIn("pos", good)
        self.info._snap = fake_snapshot(upnl=-30.0)
        bad = render_dashboard(self.payload())
        self.assertIn("neg", bad)
        self.assertIn("-30.00", bad)

    def test_render_dashboard_includes_meta_refresh(self):
        from app.hl.views import render_dashboard
        p = self.payload()
        p["refresh"] = 60
        self.assertIn("http-equiv='refresh' content='60'", render_dashboard(p))
        p["refresh"] = 999
        self.assertNotIn("http-equiv='refresh'", render_dashboard(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------- 显示口径
class TestDisplayNames(BaseCase):
    """面板显示 HL 侧合约名（BTC/ETH）；内部键（幂等键、DB symbol）仍是 MEXC 名。"""

    def test_disp_maps_and_strips_suffix(self):
        from app.hl.views import _disp, _disp_text, _coin_of
        self.assertEqual(_disp("BTC_USDT"), "BTC")
        self.assertEqual(_disp("ETH_USDT"), "ETH")
        self.assertEqual(_disp("SOL_USDC"), "SOL")      # 未在映射表里的后缀兜底
        self.assertEqual(_disp("BTC"), "BTC")           # 已经是 HL 名 → 原样
        self.assertEqual(_disp(""), "")
        self.assertEqual(_disp(None), "")
        self.assertEqual(_coin_of("BTC_USDT"), "BTC")
        self.assertEqual(_disp_text("BTC_USDT 开仓完成，ETH_USDT 无仓"), "BTC 开仓完成，ETH 无仓")
        self.assertEqual(_disp_text(None), "")

    def test_json_row_carries_coin_and_keeps_internal_symbol(self):
        self.insert_position(status="open", coin="BTC")
        p = self.payload()
        sg = (p.get("signals") or [None])[0]
        self.assertIsNotNone(sg)
        self.assertEqual(sg.get("coin"), "BTC")
        self.assertEqual(sg.get("symbol"), "BTC_USDT")   # 内部键必须保留
        for fd in (p.get("system") or {}).get("feed") or []:
            self.assertIn("coin", fd)
            self.assertNotIn("_USDT", fd["coin"])

    def test_page_never_shows_mexc_pair_names(self):
        self.insert_position(status="open", coin="BTC")
        self.db.hl_log_event("order", "BTC_USDT 开仓完成：0.05 @ 101,000", symbol="BTC_USDT")
        html = render_dashboard(self.payload())      # 参数是 payload，不是 deps
        self.assertIn(">BTC<", html)
        for bad in ("BTC_USDT", "ETH_USDT"):
            self.assertNotIn(bad, html, f"页面上不该出现信号层合约名 {bad}")

    def test_latency_badge_labels_scope(self):
        html = render_dashboard(self.payload())
        self.assertIn("HL 延迟", html)          # 说清是服务器→HL 的延迟
        self.assertIn("与你的浏览器到本站的延迟无关", html)


class TestWithdrawableDisplay(BaseCase):
    """统一账户下面板的「可提余额」不能直接显示永续口径的 0 —— 会被读成「没钱」。

    实测（2026-10-14）：统一账户的钱在现货状态里，clearinghouseState.withdrawable 恒为 0，
    而现货 USDC 才是真实可提（HL 提现固定扣 1U 手续费）。
    """

    def _html(self):
        return self.client.get("/hl").text

    def test_unified_shows_spot_balance_not_zero(self):
        snap = dict(self.info._snap)
        snap.update({"unified": True, "withdrawable": 0.0, "spot_usdc": 100.471124,
                     "equity_total": 100.471124})
        self.info._snap = snap
        html = self._html()
        # 精简后的口径：标签短、手续费后缀、口径细节在 title 悬停（P2 后顶层三数之一）
        self.assertIn("可提余额", html)
        self.assertIn("100.47 U", html)
        self.assertIn("（提现收 1U 手续费）", html)
        self.assertIn("统一账户下的实际可提", html)
        # 2026-10-15：一行说清「资金分布（统一账户）」，去掉看不懂的「永续口径」，
        # 且合约侧占用与「已用保证金」合并（不再重复一列）
        self.assertIn("资金分布（统一账户）", html)
        self.assertIn("合约占用", html)
        self.assertNotIn("永续口径", html)
        # 「已用保证金」那一列在统一账户下不再单列（数字已并入上一行）；
        # title 提示里仍会提到这个名字，所以只断言**不再有该行**
        self.assertNotIn("<span class='k'>已用保证金</span>", html)

    def test_non_unified_keeps_perp_withdrawable(self):
        snap = dict(self.info._snap)
        snap.update({"unified": False, "withdrawable": 88.5})
        self.info._snap = snap
        html = self._html()
        self.assertIn("可提余额", html)
        self.assertIn("88.50", html)


class TestLatencyFromSnapshot(BaseCase):
    """HL 延迟的两条来源必须分得清（阶段 B 之后面板渲染不访问交易所）。

    - 实时读数（/api/hl/status、无快照回落）：延迟 = 本次请求实测，`latency_from_snapshot=False`；
    - 调度器快照：延迟 = 写入快照时那一轮的实测（`panel_net`），带采样距今多久，
      `latency_from_snapshot=True` —— 页面据此说明「不是此刻测的」。
    """

    def _with_panel_net(self, net):
        snap = dict(self.info._snap)
        # 读数来自快照时 SnapshotInfo 一定会打上这个年龄标记（面板据此决定显不显示）
        snap["panel_snapshot_age_s"] = 12.0
        if net is None:
            snap.pop("panel_net", None)
        else:
            snap["panel_net"] = net
        self.info._snap = snap

    def test_snapshot_latency_used_with_age(self):
        self._with_panel_net({"latency_ms": 456.7, "ts": FIXED_NOW - 300.0, "source": "monitor"})
        n = self.payload()["network"]
        self.assertTrue(n["latency_from_snapshot"])
        self.assertAlmostEqual(n["latency_ms"], 456.7)
        self.assertAlmostEqual(n["latency_age_s"], 300.0)

    def test_live_path_has_no_snapshot_flags(self):
        n = self.payload()["network"]
        self.assertFalse(n["latency_from_snapshot"])
        self.assertIsNone(n["latency_age_s"])
        self.assertIsInstance(n["latency_ms"], (int, float))

    def test_zero_or_missing_latency_is_unknown(self):
        for net in (None, {"latency_ms": 0.0, "ts": FIXED_NOW},
                    {"latency_ms": -3, "ts": FIXED_NOW}, {"ts": FIXED_NOW}):
            self._with_panel_net(net)
            n = self.payload()["network"]
            self.assertFalse(n["latency_from_snapshot"], f"不该认 {net} 为实测值")
            self.assertIsNone(n["latency_age_s"])

    def test_page_renders_snapshot_latency_and_scope(self):
        self._with_panel_net({"latency_ms": 456.7, "ts": FIXED_NOW - 300.0})
        html = render_dashboard(self.payload())
        self.assertIn("HL 延迟 <b>457 ms</b>", html)
        self.assertIn("调度器上一轮读 Hyperliquid 的耗时", html)
        self.assertIn("本次渲染没有访问交易所", html)

    def test_page_without_measurement_shows_dash_not_fake_ms(self):
        self._with_panel_net({"latency_ms": 0.0, "ts": FIXED_NOW})
        html = render_dashboard(self.payload())
        self.assertIn("HL 延迟 <b>—</b>", html)
        self.assertNotIn("HL 延迟 <b>0 ms</b>", html)

