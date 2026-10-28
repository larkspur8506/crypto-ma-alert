"""面板打开速度 · 阶段 A 的回归测试（全部离线，不触网、不下单）。

三个交付点，各有一条「钉住约束」的核心断言：

A1 面板读数缓存（`app/panel_cache.py` + `main._admin_deps`）
    - 面板连续两次渲染 /admin 只打一轮 HL（4 个请求），而 `/api/hl/status` 与执行器
      拿到的真身**仍然每次都打** —— 这条是本次改动的红线：缓存绝不能拦住执行器。
    - 键含地址；写操作（参数/kill）成功后缓存立刻失效。
A2 K 线 TTL 900 + 启动预热
    - TTL 常量就是 900（不是「大概」）；预热只填 K 线、不算六步；预热后首个渲染不再现取。
    - 预热线程是守护线程且**不阻塞** startup；env 开关默认开、能关。
A3 gzip
    - 带 Accept-Encoding: gzip 时面板响应带 content-encoding: gzip；小响应不压；
      CSV 流式导出仍然正常（不压坏流式端点）。
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import why as why_mod                                  # noqa: E402
from app.config import Config                                   # noqa: E402
from app.panel_cache import DEFAULT_TTL_S, PanelInfo, TtlCache   # noqa: E402

TOKEN = "speed-tk"
ADDR = "0xSPEEDTESTADDRESS"
NOW = 1_760_000_000.0


# ---------------------------------------------------------------- 夹具
class FakeClock:
    """可控时钟：TTL 边界不该靠真的 sleep（CI 会变慢也会变脆）。"""

    def __init__(self, t0: float = NOW) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class CountingInfo:
    """HlInfo 替身：按「真实 HL 请求条数」计数。

    `snapshot()` 内部在真实实现里就是 4 个请求（clearinghouseState / allMids /
    frontendOpenOrders / spotClearinghouseState），这里逐条打点，才能证明「一页 4 次」
    这件事是可测的，而不是只看 snapshot 被调了几次。
    """

    base_url = "https://fake.hl"

    def __init__(self) -> None:
        self.calls = {"clearinghouse": 0, "mids": 0, "orders": 0, "spot": 0}
        self.snapshots = 0

    @property
    def total(self) -> int:
        return sum(self.calls.values())

    def reset(self) -> None:
        for k in self.calls:
            self.calls[k] = 0
        self.snapshots = 0

    def all_mids(self):
        self.calls["mids"] += 1
        return {"BTC": 100_000.0, "ETH": 3_000.0, "SOL": 150.0}

    def snapshot(self, address, mids=None):
        self.calls["clearinghouse"] += 1
        if mids is None:
            self.all_mids()                    # 与真身一致：不传 mids 就自己取一次
        self.calls["orders"] += 1
        self.calls["spot"] += 1
        self.snapshots += 1
        return {"ts": NOW, "address": address, "equity": 100.0, "equity_total": 100.0,
                "spot_usdc": 100.0, "unified": True, "withdrawable": 90.0,
                "margin_used": 10.0, "notional": 0.0, "positions": [], "open_orders": []}


class NoMidsInfo(CountingInfo):
    """不接受 `mids=` 的替身（老接口口径）：包装层必须回落，不能抛 TypeError。"""

    def snapshot(self, address=None):
        return CountingInfo.snapshot(self, address)


# ---------------------------------------------------------------- A1：缓存本身
class TestPanelInfoCache(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.raw = CountingInfo()
        self.info = PanelInfo(self.raw, ttl_s=DEFAULT_TTL_S, clock=self.clock)

    def test_ttl_is_ten_seconds(self):
        # 常数有名字就该被钉住：改了它要顺手改这里的理由说明
        self.assertEqual(DEFAULT_TTL_S, 10.0)

    def test_two_renders_within_ttl_hit_hl_once(self):
        a = self.info.snapshot(ADDR)
        b = self.info.snapshot(ADDR)
        self.assertEqual(a["equity"], b["equity"])
        self.assertEqual(self.raw.snapshots, 1)
        self.assertEqual(self.raw.total, 4, "一次渲染 4 个 HL 请求（cold 与线上一致）")

    def test_mids_shared_between_snapshot_and_preview(self):
        """冷启动不能因为多缓存一层反而多打一次 allMids：snapshot 复用同一份。"""
        self.info.snapshot(ADDR)
        self.info.all_mids()
        self.assertEqual(self.raw.calls["mids"], 1)

    def test_expiry_refetches(self):
        self.info.snapshot(ADDR)
        self.clock.advance(9.9)
        self.info.snapshot(ADDR)
        self.assertEqual(self.raw.snapshots, 1)
        self.clock.advance(0.2)                # 10.1s > 10s
        self.info.snapshot(ADDR)
        self.assertEqual(self.raw.snapshots, 2)

    def test_key_includes_address(self):
        self.info.snapshot("0xAAA")
        self.info.snapshot("0xBBB")
        self.assertEqual(self.raw.snapshots, 2)

    def test_explicit_mids_bypasses_cache(self):
        """执行器口径：调用方自带现价时必须真打（否则等于喂旧价给下单判定）。"""
        self.info.snapshot(ADDR, mids={"BTC": 1.0})
        self.info.snapshot(ADDR, mids={"BTC": 1.0})
        self.assertEqual(self.raw.snapshots, 2)
        self.assertEqual(len(self.info), 0, "显式带 mids 的调用不许写进缓存")

    def test_invalidate_forces_refetch(self):
        self.info.snapshot(ADDR)
        self.info.invalidate()
        self.info.snapshot(ADDR)
        self.assertEqual(self.raw.snapshots, 2)

    def test_returned_value_is_a_copy(self):
        """面板会往下传这份 dict，改到它不能污染缓存（否则第二个 Tab 看到被改过的数）。"""
        snap = self.info.snapshot(ADDR)
        snap["equity"] = -1
        self.assertEqual(self.info.snapshot(ADDR)["equity"], 100.0)

    def test_delegates_unknown_attributes(self):
        self.assertEqual(self.info.base_url, "https://fake.hl")

    def test_raw_passthrough_still_uncached(self):
        """红线：真身不在缓存里 —— 同一对象连打两次就是两次请求（执行器/对账走的就是它）。"""
        self.raw.snapshot(ADDR)
        self.raw.snapshot(ADDR)
        self.assertEqual(self.raw.snapshots, 2)

    def test_snapshot_without_mids_kwarg(self):
        info = PanelInfo(NoMidsInfo(), ttl_s=DEFAULT_TTL_S, clock=self.clock)
        self.assertEqual(info.snapshot(ADDR)["equity"], 100.0)

    def test_ttl_zero_disables_cache(self):
        cache = TtlCache(ttl_s=0, clock=self.clock)
        calls = []
        for _ in range(2):
            cache.get_or_call("k", lambda: calls.append(1) or "v")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(cache), 0)


# ---------------------------------------------------------------- A2：K 线 TTL / 预热
def _bars(n: int = 320, px: float = 100.0):
    from app import strategy as S
    step = 14_400_000
    out = []
    for i in range(n):
        out.append(S.Bar(open_ms=NOW * 1000 + i * step, open=px, high=px * 1.001,
                         low=px * 0.999, close=px, volume=1.0))
    return out


class _Feed:
    def __init__(self, bars):
        self.bars = bars
        self.source = "fake"


class TestWhyFeedTtlAndPreheat(unittest.TestCase):
    def setUp(self):
        why_mod.clear_cache()
        self.cfg = Config()
        self.cfg.symbols = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]
        self.fetches = []
        self.tmp = tempfile.TemporaryDirectory()
        from app.db import DB
        self.db = DB(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        why_mod.clear_cache()
        self.tmp.cleanup()

    def _fetch(self, bars=None):
        def fetch(sym, now=None):
            self.fetches.append(sym)
            return _Feed(bars if bars is not None else _bars())
        return fetch

    def test_feed_ttl_is_900(self):
        # 300 → 900 是本次 A2 的核心改动；值本身要能被断言（避免「文档说 900，代码还是 300」）
        self.assertEqual(why_mod.FEED_TTL_S, 900.0)

    def test_preheat_fills_feed_cache_only(self):
        bad = why_mod.preheat(self.cfg, fetch=self._fetch())
        self.assertEqual(bad, [])
        self.assertEqual(sorted(self.fetches), ["BTC_USDT", "ETH_USDT", "SOL_USDT"])
        self.assertEqual(sorted(why_mod._FEED_CACHE), ["BTC_USDT", "ETH_USDT", "SOL_USDT"])
        # 只热 K 线：六步缓存为空（预热不算判定，参数变了才不会留下旧结论）
        self.assertEqual(dict(why_mod._WHY_CACHE), {})

    def test_preheat_reports_failures_without_raising(self):
        def fetch(sym, now=None):
            raise RuntimeError("network down")
        bad = why_mod.preheat(self.cfg, fetch=fetch)
        self.assertEqual(len(bad), 3)
        self.assertTrue(all("network down" in b for b in bad), bad)

    def test_first_render_after_preheat_does_not_refetch(self):
        """预热的意义：首个访问者不该再等 10s 现取。这里用「取数必炸」证明一次都没取。"""
        why_mod.preheat(self.cfg, fetch=self._fetch())

        def boom(sym, now=None):
            raise AssertionError("预热后不该再取行情")

        rows = why_mod.build_rows(self.db, self.cfg, symbols=["BTC_USDT"], fetch=boom)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["error"])
        self.assertGreater(rows[0]["bars"], 0)

    def test_feed_cache_stale_after_ttl(self):
        bars = _bars()
        why_mod._FEED_CACHE["BTC_USDT"] = (time.time() - 880.0, bars)
        rows = why_mod.build_rows(self.db, self.cfg, symbols=["BTC_USDT"],
                                  fetch=self._fetch())
        self.assertIsNone(rows[0]["error"])
        self.assertEqual(self.fetches, [], "880s < 900s 应命中缓存")
        why_mod._FEED_CACHE["BTC_USDT"] = (time.time() - 910.0, bars)
        rows = why_mod.build_rows(self.db, self.cfg, symbols=["BTC_USDT"],
                                  fetch=self._fetch())
        self.assertIsNone(rows[0]["error"])
        self.assertEqual(self.fetches, ["BTC_USDT"], "过 900s 必须重取")


# ---------------------------------------------------------------- 全应用：A1 接线 / A2 开关 / A3 gzip
class CountingHlInfo:
    """装进 app.main 的 HlInfo 替身（main 在 import 时会构造一个真身，必须换掉）。"""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.base_url = str(kwargs.get("base_url") or "https://fake.hl")
        self.calls = {"clearinghouse": 0, "mids": 0, "orders": 0, "spot": 0}
        self.snapshots = 0
        CountingHlInfo.instances.append(self)

    @property
    def total(self) -> int:
        return sum(self.calls.values())

    def reset(self) -> None:
        for k in self.calls:
            self.calls[k] = 0
        self.snapshots = 0

    def all_mids(self):
        self.calls["mids"] += 1
        return {"BTC": 100_000.0, "ETH": 3_000.0, "SOL": 150.0}

    def snapshot(self, address, mids=None):
        self.calls["clearinghouse"] += 1
        if mids is None:
            self.all_mids()
        self.calls["orders"] += 1
        self.calls["spot"] += 1
        self.snapshots += 1
        return {"ts": NOW, "address": address, "equity": 100.0, "equity_total": 100.0,
                "spot_usdc": 100.0, "unified": True, "withdrawable": 90.0,
                "margin_used": 10.0, "notional": 0.0, "positions": [], "open_orders": [],
                "orders_trusted": True}

    def extra_agents(self, address):
        return []

    def agent_status(self, address):
        return {"agents": [], "max_until": None}


class TestFullAppPanelSpeed(unittest.TestCase):
    """整应用（含 middleware/lifespan）：用真实路由验证 A1/A2/A3 的接线。"""

    ENV = {
        "ENABLE_SCHEDULER": "false",
        "ADMIN_TOKEN": TOKEN,
        "TOKEN_SECRET": "speed-secret",
        "BASE_URL": "https://speed.example",
        "DRY_RUN": "true",
        "HL_ACCOUNT_ADDRESS": ADDR,
        "WHY_PREHEAT": "0",          # 预热由专门用例验证；整应用默认关，否则 CI 会真出网
    }

    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:                                   # pragma: no cover
            self.skipTest("fastapi 未安装")
        self.TestClient = TestClient
        self.tmp = tempfile.mkdtemp(prefix="panel_speed_")
        self._env_backup = {k: os.environ.get(k) for k in
                            tuple(self.ENV) + ("DB_PATH", "WHY_PREHEAT")}
        os.environ["DB_PATH"] = os.path.join(self.tmp, "speed.db")
        for k, v in self.ENV.items():
            os.environ[k] = v
        import app.hl.info as hl_info
        self._real_cls = hl_info.HlInfo
        hl_info.HlInfo = CountingHlInfo
        CountingHlInfo.instances = []
        self.mod = self._reload()
        self.info = CountingHlInfo.instances[-1]

    def tearDown(self):
        import app.hl.info as hl_info
        hl_info.HlInfo = self._real_cls
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, val in self._env_backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _reload(self):
        importlib.reload(importlib.import_module("app.config"))
        return importlib.reload(importlib.import_module("app.main"))

    # ---- A1：面板吃缓存，JSON API 与执行器走实时
    def test_two_panel_renders_hit_hl_once(self):
        with self.TestClient(self.mod.app) as c:
            r1 = c.get("/admin", params={"k": TOKEN})
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(self.info.total, 4, "冷启动 4 个 HL 请求（与线上一致）")
            self.info.reset()
            r2 = c.get("/admin", params={"k": TOKEN, "tab": "params"})
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(self.info.total, 0,
                             f"TTL 内第二次渲染不该打 HL：{self.info.calls}")

    def test_all_four_tabs_share_one_snapshot(self):
        with self.TestClient(self.mod.app) as c:
            for tab in ("overview", "params", "switch", "records"):
                self.assertEqual(c.get("/admin", params={"k": TOKEN, "tab": tab}).status_code, 200)
            self.assertEqual(self.info.snapshots, 1, f"四个 Tab 只该取一次快照：{self.info.calls}")
            self.assertEqual(self.info.total, 4)

    def test_json_api_is_not_cached(self):
        """/api/hl/* 保持逐次现取（运维 curl 与巡检脚本依赖它读到「现在」）。"""
        with self.TestClient(self.mod.app) as c:
            for _ in range(2):
                self.assertEqual(c.get("/api/hl/status", params={"k": TOKEN}).status_code, 200)
            self.assertEqual(self.info.snapshots, 2)

    def test_executor_info_is_not_wrapped(self):
        """执行器/看护拿的是 get_hl_info() 真身：两次 snapshot 就是两次请求。"""
        raw = self.mod.get_hl_info()
        self.assertNotIsInstance(raw, PanelInfo)
        panel = self.mod.panel_hl_info()
        self.assertIsInstance(panel, PanelInfo)
        self.assertIs(panel.raw, raw)
        self.assertIs(self.mod._hl_deps().info, raw, "面板以外的依赖必须用真身")
        self.assertIsNot(self.mod._admin_deps().hl_deps.info, raw, "面板依赖才用包装")
        raw.snapshot(ADDR)
        raw.snapshot(ADDR)
        self.assertEqual(self.info.snapshots, 2)

    def test_write_invalidates_panel_cache(self):
        with self.TestClient(self.mod.app) as c:
            self.assertEqual(c.get("/admin", params={"k": TOKEN}).status_code, 200)
            self.info.reset()
            r = c.post("/admin/switch/kill", data={"on": "1", "k": TOKEN},
                       follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(self.info.total, 0, "写操作本身不该打 HL")
            self.assertEqual(c.get("/admin", params={"k": TOKEN}).status_code, 200)
            self.assertGreaterEqual(self.info.snapshots, 1, "写成功后必须重新取快照")

    # ---- A2：预热开关与 lifespan 接线
    def test_preheat_env_default_on(self):
        os.environ.pop("WHY_PREHEAT", None)
        self.assertTrue(self.mod._why_preheat_on())
        for off in ("0", "false", "no", "off", " OFF "):
            os.environ["WHY_PREHEAT"] = off
            self.assertFalse(self.mod._why_preheat_on(), off)
        os.environ["WHY_PREHEAT"] = "1"
        self.assertTrue(self.mod._why_preheat_on())

    def test_start_preheat_does_not_block(self):
        started = []

        def slow():
            started.append(True)
            time.sleep(0.6)

        self.mod._job_why_preheat = slow
        os.environ["WHY_PREHEAT"] = "1"
        t0 = time.monotonic()
        th = self.mod._start_why_preheat()
        elapsed = time.monotonic() - t0
        self.assertIsNotNone(th)
        self.assertLess(elapsed, 0.3, f"startup 被预热阻塞了 {elapsed:.2f}s")
        self.assertTrue(th.daemon, "预热线程必须是守护线程，不能拖住进程退出")
        th.join(3.0)
        self.assertEqual(started, [True])

    def test_start_preheat_disabled_returns_none(self):
        os.environ["WHY_PREHEAT"] = "0"
        self.assertIsNone(self.mod._start_why_preheat())

    def test_lifespan_calls_preheat_hook(self):
        calls = []
        self.mod._start_why_preheat = lambda: calls.append(1)
        with self.TestClient(self.mod.app):
            pass
        self.assertEqual(calls, [1], "lifespan 里必须真的调了预热钩子")

    # ---- A3：gzip
    def test_panel_html_is_gzipped(self):
        with self.TestClient(self.mod.app) as c:
            r = c.get("/admin", params={"k": TOKEN},
                      headers={"Accept-Encoding": "gzip"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("content-encoding"), "gzip")
            self.assertIn("双均线交易台", r.text)      # httpx 已解压，正文完好

    def test_panel_html_not_gzipped_without_accept(self):
        with self.TestClient(self.mod.app) as c:
            r = c.get("/admin", params={"k": TOKEN},
                      headers={"Accept-Encoding": "identity"})
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.headers.get("content-encoding"))

    def test_small_response_not_gzipped(self):
        """/health 这种几百字节的 JSON 压了反而多一层头，min_size=1024 要挡住它。"""
        with self.TestClient(self.mod.app) as c:
            r = c.get("/health", headers={"Accept-Encoding": "gzip"})
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.headers.get("content-encoding"))

    def test_csv_export_survives_gzip(self):
        """流式导出不能被 gzip 中间件压坏（Starlette 按 chunk 增量压缩）。"""
        with self.TestClient(self.mod.app) as c:
            r = c.get("/admin/export", params={"table": "positions", "k": TOKEN},
                      headers={"Accept-Encoding": "gzip"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.text.startswith("\ufeff"), repr(r.text[:40]))
            self.assertIn("content-disposition", {k.lower() for k in r.headers})


if __name__ == "__main__":                                        # pragma: no cover
    unittest.main()
