"""面板快照 · 阶段 B 的回归测试（全部离线：不触网、不下单）。

钉住四条约束：

1. **表**：写进去的行能原样读回来（含 why），同一 ts 覆盖，只留最近 N 行；
   坏 JSON 按「没有快照」处理（不许把解析错误变成面板 500）。
2. **读数层**：快照在有效期内 → `snapshot()`/`all_mids()` 一次都不碰 `fallback`
   （这就是「面板零网络零重算」的可测形式）；缺失/过期 → 回落且不抛异常。
3. **写后失效**：面板写操作（参数/模式/kill）后，下一次渲染走实时，之后恢复读快照。
4. **接线**：`capture_panel_snapshot()` 只读交易所并落一行；没地址/交易所报错时返回 False
   且**不影响交易**（异常不外抛）。`_admin_deps()` 默认拿快照读取器，`PANEL_SNAPSHOT=0`
   退回 A1 的 `PanelInfo`。
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB                                            # noqa: E402
from app.panel_store import DEFAULT_STALE_S, PanelSnapshotStore, SnapshotInfo  # noqa: E402

NOW = 1_760_000_000.0


class FakeClock:
    def __init__(self, t0: float = NOW) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakePanelInfo:
    """`PanelInfo` 替身：记录 snapshot/all_mids/invalidate 被调了几次。"""

    base_url = "https://fake.hl"

    def __init__(self) -> None:
        self.snapshots = 0
        self.mids = 0
        self.invalidations = 0

    @property
    def total(self) -> int:
        return self.snapshots + self.mids

    def snapshot(self, address, mids=None):
        self.snapshots += 1
        if mids is None:
            self.mids += 1
        return {"ts": 1.0, "address": address, "equity": 42.0, "positions": [],
                "open_orders": []}

    def all_mids(self):
        self.mids += 1
        return {"BTC": 1.0}

    def invalidate(self):
        self.invalidations += 1

    def reset(self):
        self.snapshots = 0
        self.mids = 0


def snap_of(equity: float = 100.0, **kw) -> dict:
    out = {"ts": NOW, "address": "0xADDR", "equity": equity, "equity_total": equity,
           "withdrawable": equity * 0.9, "margin_used": 0.0, "notional": 0.0,
           "positions": [{"coin": "BTC", "side": 1, "size": 0.01}],
           "open_orders": [{"coin": "BTC", "oid": 7, "is_buy": True}]}
    out.update(kw)
    return out


class TestStoreTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="panel_store_")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.store = PanelSnapshotStore(self.db)

    def tearDown(self):
        try:
            self.db.conn.close()
        except Exception:                                         # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_and_read_roundtrip(self):
        why = [{"symbol": "BTC_USDT", "step": "squeeze", "ok": False}]
        self.store.write(snap_of(equity=123.4), {"BTC": 90_000.0, "ETH": 3_000.0},
                         why=why, ts=NOW, source="tick")
        row = self.store.latest()
        self.assertIsNotNone(row)
        self.assertEqual(row["ts"], NOW)
        self.assertEqual(row["snapshot"]["equity"], 123.4)
        self.assertEqual(row["mids"]["ETH"], 3_000.0)
        self.assertEqual(row["why"], why)
        self.assertEqual(row["source"], "tick")

    def test_same_ts_overwrites(self):
        self.store.write(snap_of(equity=100.0), {"BTC": 1.0}, ts=NOW, source="tick")
        self.store.write(snap_of(equity=200.0), {"BTC": 1.0}, ts=NOW, source="monitor")
        self.assertEqual(self.store.latest()["snapshot"]["equity"], 200.0)
        self.assertEqual(self.store.latest()["source"], "monitor")

    def test_prune_keeps_newest(self):
        store = PanelSnapshotStore(self.db, keep=3)
        for i in range(6):
            store.write(snap_of(equity=float(i)), {"BTC": 1.0}, ts=NOW + i)
        self.assertEqual(store.prune(), 3)
        rows = list(self.db.conn.execute("SELECT ts FROM panel_snapshots ORDER BY ts DESC"))
        self.assertEqual([r["ts"] for r in rows], [NOW + 5, NOW + 4, NOW + 3])

    def test_latest_why_skips_rows_without_why(self):
        self.store.write(snap_of(), {"BTC": 1.0}, why=None, ts=NOW)
        self.store.write(snap_of(), {"BTC": 2.0}, why=[{"symbol": "ETH_USDT"}], ts=NOW + 60)
        self.store.write(snap_of(), {"BTC": 3.0}, why=None, ts=NOW + 120)   # 更新但没 why
        self.assertEqual(self.store.latest_why(), [{"symbol": "ETH_USDT"}])
        self.assertEqual(self.store.latest()["mids"]["BTC"], 3.0)

    def test_corrupt_row_is_treated_as_missing(self):
        self.db.conn.execute(
            "INSERT INTO panel_snapshots(ts, snapshot_json, mids_json, why_json, source,"
            " created_at) VALUES(?,?,?,?,?,?)", (NOW, "{not json", "[]", None, "x", NOW))
        self.db.conn.commit()
        self.assertIsNone(self.store.latest())                    # 解析失败 = 没快照
        self.assertIsNone(self.store.latest_why())


class TestSnapshotInfo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="panel_info_")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.store = PanelSnapshotStore(self.db)
        self.clock = FakeClock()
        self.fallback = FakePanelInfo()
        self.info = SnapshotInfo(self.store, self.fallback, clock=self.clock)

    def tearDown(self):
        try:
            self.db.conn.close()
        except Exception:                                         # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_snapshot_never_touches_fallback(self):
        self.store.write(snap_of(equity=555.0), {"BTC": 81_000.0}, ts=NOW)
        snap = self.info.snapshot("0xADDR")
        self.assertEqual(snap["equity"], 555.0)
        self.assertEqual(self.info.all_mids(), {"BTC": 81_000.0})
        self.assertEqual(self.fallback.total, 0, "快照有效期内一次都不该打交易所")
        self.assertIn("panel_snapshot_age_s", snap)
        self.assertEqual(snap["panel_snapshot_age_s"], 0.0)

    def test_missing_snapshot_falls_back(self):
        snap = self.info.snapshot("0xADDR")
        self.assertEqual(snap["equity"], 42.0)                    # 来自 fallback
        self.assertEqual(self.fallback.snapshots, 1)
        self.assertEqual(self.fallback.mids, 1)                   # snapshot 内部取价
        self.info.all_mids()
        self.assertEqual(self.fallback.mids, 2)

    def test_stale_snapshot_falls_back(self):
        self.store.write(snap_of(equity=999.0), {"BTC": 1.0}, ts=NOW)
        self.clock.advance(DEFAULT_STALE_S + 1)
        self.assertEqual(self.info.snapshot("0xADDR")["equity"], 42.0)
        self.assertEqual(self.fallback.snapshots, 1)
        self.assertGreater(self.info.age_s(), DEFAULT_STALE_S)

    def test_explicit_mids_is_passed_through(self):
        """执行器口径：调用方自带现价时必须直通实时客户端，绝不读快照。"""
        self.store.write(snap_of(equity=999.0), {"BTC": 1.0}, ts=NOW)
        self.info.snapshot("0xADDR", mids={"BTC": 5.0})
        self.assertEqual(self.fallback.snapshots, 1)

    def test_invalidate_forces_one_live_read_then_back_to_snapshot(self):
        self.store.write(snap_of(equity=777.0), {"BTC": 1.0}, ts=NOW)
        self.info.invalidate()
        self.assertEqual(self.info.snapshot("0xADDR")["equity"], 42.0)   # 写后走实时
        self.assertEqual(self.fallback.invalidations, 1)
        self.assertEqual(self.info.snapshot("0xADDR")["equity"], 777.0)  # 之后恢复读快照
        self.assertEqual(self.fallback.snapshots, 1)

    def test_empty_mids_fall_back(self):
        """快照里有读数但现价为空（行情临时不可用）：现价单独回落，不用空字典糊弄。"""
        self.store.write(snap_of(equity=5.0), {}, ts=NOW)
        self.assertEqual(self.info.all_mids(), {"BTC": 1.0})
        self.assertEqual(self.fallback.mids, 1)

    def test_attribute_passthrough(self):
        self.assertEqual(self.info.base_url, "https://fake.hl")


class CountingHlInfo(FakePanelInfo):
    """真实 HlInfo 的替身（app.hl.info.HlInfo 被换掉时用）。"""

    instances = []

    def __init__(self, *a, **kw):
        super().__init__()
        CountingHlInfo.instances.append(self)


class TestWiring(unittest.TestCase):
    """整应用：验证 capture 写表 + _admin_deps 的默认选择 + 面板零请求。"""

    ENV = {"ENABLE_SCHEDULER": "false", "ADMIN_TOKEN": "snap-tk",
           "TOKEN_SECRET": "snap-secret", "BASE_URL": "https://snap.example",
           "DRY_RUN": "true", "HL_ACCOUNT_ADDRESS": "0xSNAPADDR", "WHY_PREHEAT": "0"}

    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:                                       # pragma: no cover
            self.skipTest("fastapi 未安装")
        self.TestClient = TestClient
        self.tmp = tempfile.mkdtemp(prefix="panel_wiring_")
        self._backup = {k: os.environ.get(k) for k in
                        tuple(self.ENV) + ("DB_PATH", "PANEL_SNAPSHOT")}
        os.environ["DB_PATH"] = os.path.join(self.tmp, "w.db")
        os.environ.pop("PANEL_SNAPSHOT", None)                    # 默认开
        for k, v in self.ENV.items():
            os.environ[k] = v
        import app.hl.info as hl_info
        self._real_cls = hl_info.HlInfo
        hl_info.HlInfo = CountingHlInfo
        CountingHlInfo.instances = []
        importlib.reload(importlib.import_module("app.config"))
        self.mod = importlib.reload(importlib.import_module("app.main"))
        self.info = CountingHlInfo.instances[-1]

    def tearDown(self):
        import app.hl.info as hl_info
        hl_info.HlInfo = self._real_cls
        for key, val in self._backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _capture(self, why=None):
        """跑一次 capture，把 why 换成本地桩（绝不真的去拉 5000 根 K 线）。"""
        rows = why if why is not None else [{"symbol": "BTC_USDT", "ok": False}]
        with mock.patch("app.why.build_rows", return_value=rows):
            return self.mod.capture_panel_snapshot("tick", with_why=True)

    def test_capture_writes_row_and_panel_makes_no_hl_calls(self):
        self.assertTrue(self._capture())
        row = self.mod.panel_snapshot_store().latest()
        self.assertIsNotNone(row)
        self.assertEqual(row["why"], [{"symbol": "BTC_USDT", "ok": False}])
        self.info.reset()
        with self.TestClient(self.mod.app) as c:
            r = c.get("/admin", params={"k": "snap-tk"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.info.total, 0, f"面板应零请求：{self.info.snapshots} 次快照")
            # 状态条要如实标出「数据 X 秒前」
            self.assertIn("数据", r.text)
            # 快照模式没访问交易所 ⇒ 不能显示一个未实测的网络延迟（曾显示成「1 ms」）
            self.assertIn("HL 延迟 <b>—</b>", r.text)
            self.assertNotIn("HL 延迟 <b>0 ms</b>", r.text)

    def test_live_mode_still_shows_latency(self):
        """没有快照（回落实时）时延迟徽章照常给数字 —— 这条防止修上一条修过头。"""
        with self.TestClient(self.mod.app) as c:
            r = c.get("/admin", params={"k": "snap-tk"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("HL 延迟 <b>", r.text)
            self.assertIn(" ms</b>", r.text)

    def test_panel_without_snapshot_falls_back_and_still_renders(self):
        with self.TestClient(self.mod.app) as c:
            r = c.get("/admin", params={"k": "snap-tk"})
            self.assertEqual(r.status_code, 200)
            self.assertGreaterEqual(self.info.snapshots, 1, "没有快照时必须回落实时读取")

    def test_capture_without_address_returns_false(self):
        os.environ.pop("HL_ACCOUNT_ADDRESS", None)
        self.mod = importlib.reload(importlib.import_module("app.main"))
        self.assertFalse(self.mod.capture_panel_snapshot("tick", with_why=True))
        self.assertIsNone(self.mod.panel_snapshot_store().latest())

    def test_capture_swallows_exchange_errors(self):
        class Boom(CountingHlInfo):
            def all_mids(self):
                raise RuntimeError("exchange down")

        self.info.__class__ = Boom
        self.assertFalse(self.mod.capture_panel_snapshot("monitor", with_why=False))
        self.assertIsNone(self.mod.panel_snapshot_store().latest())

    def test_admin_deps_defaults_to_snapshot_reader(self):
        from app.panel_store import SnapshotInfo
        info = self.mod._admin_deps().hl_deps.info
        self.assertIsInstance(info, SnapshotInfo)

    def test_env_switch_falls_back_to_panel_info(self):
        from app.panel_cache import PanelInfo
        os.environ["PANEL_SNAPSHOT"] = "0"
        self.mod = importlib.reload(importlib.import_module("app.main"))
        self.assertIsInstance(self.mod._admin_deps().hl_deps.info, PanelInfo)

    def test_why_rows_prefers_snapshot(self):
        rows = [{"symbol": "ETH_USDT", "ok": True, "step": "from-snapshot"}]
        self.assertTrue(self._capture(why=rows))
        with mock.patch("app.why.build_rows", side_effect=AssertionError("不该现算")):
            self.assertEqual(self.mod._why_rows(), rows)

    def test_why_rows_recomputes_right_after_a_write(self):
        self.assertTrue(self._capture(why=[{"symbol": "OLD"}]))
        self.mod.invalidate_panel_cache()
        fresh = [{"symbol": "NEW"}]
        with mock.patch("app.why.build_rows", return_value=fresh):
            self.assertEqual(self.mod._why_rows(), fresh)

    def test_why_rows_without_snapshot_recomputes(self):
        fresh = [{"symbol": "LIVE"}]
        with mock.patch("app.why.build_rows", return_value=fresh):
            self.assertEqual(self.mod._why_rows(), fresh)


if __name__ == "__main__":
    unittest.main()
