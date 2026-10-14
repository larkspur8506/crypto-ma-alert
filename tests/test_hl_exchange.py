"""app/hl/exchange.py 的单元测试（离线；不触碰真实 SDK/网络）。

要点：dry_run 路径、响应归一化、cloid 幂等键、滑点限价、凭据缺失时 fail-closed。
"""
from __future__ import annotations

import unittest

from app.hl.exchange import (HlExchange, make_cloid, parse_order_response, STATUS_DRY,
                             STATUS_ERROR, STATUS_FILLED, STATUS_RESTING)


class _FakeInfo:
    def __init__(self, mids=None, orders=None):
        # 注意：mids={} 要保留（空 mid 场景），只有 None 才用默认值
        self._mids = {"BTC": 83000.0} if mids is None else dict(mids)
        self._orders = orders or []

    def all_mids(self):
        return dict(self._mids)

    def open_orders(self, address):
        return list(self._orders)


class _FakeSdk:
    """模拟官方 Exchange：记录调用并按预设脚本返回。"""

    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])

    def _next(self, name, *a, **kw):
        self.calls.append((name, a, kw))
        return self.script.pop(0) if self.script else {"status": "ok",
                                                      "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}

    def update_leverage(self, *a, **kw):
        return self._next("update_leverage", *a, **kw)

    def order(self, *a, **kw):
        return self._next("order", *a, **kw)

    def cancel(self, *a, **kw):
        return self._next("cancel", *a, **kw)

    def schedule_cancel(self, *a, **kw):
        return self._next("schedule_cancel", *a, **kw)


def _ok_order(oid=7, avg_px="83010.5", sz="0.012"):
    return {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": sz, "avgPx": avg_px, "oid": oid}}]}}}


def _resting(oid=9):
    return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


class TestCloid(unittest.TestCase):
    def test_deterministic_and_shape(self):
        c = make_cloid("BTC_USDT", 1791912780000, "entry")
        self.assertTrue(c.startswith("0x"))
        self.assertEqual(len(c), 34)
        self.assertEqual(c, make_cloid("BTC_USDT", 1791912780000, "entry"))

    def test_varies_by_key(self):
        a = make_cloid("BTC_USDT", 1, "entry")
        self.assertNotEqual(a, make_cloid("BTC_USDT", 1, "stop"))
        self.assertNotEqual(a, make_cloid("ETH_USDT", 1, "entry"))
        self.assertNotEqual(a, make_cloid("BTC_USDT", 2, "entry"))

    def test_sdk_accepts_generated_cloid(self):
        """生成的 cloid 必须能被官方 SDK 的 Cloid 校验（16 字节 hex）。"""
        ex = HlExchange(sdk=_FakeSdk(), account_address="0xabc", private_key="0xkey")
        cl = ex._cloid("BTC_USDT", 123, "entry")           # noqa: SLF001
        self.assertEqual(len(str(cl)), 34)
        self.assertTrue(str(cl).startswith("0x"))


class TestParseOrderResponse(unittest.TestCase):
    def test_filled(self):
        out = parse_order_response(_ok_order())
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_FILLED)
        self.assertEqual(out["oid"], 7)
        self.assertAlmostEqual(out["avg_px"], 83010.5)
        self.assertAlmostEqual(out["filled_sz"], 0.012)

    def test_resting(self):
        out = parse_order_response(_resting(11))
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_RESTING)
        self.assertEqual(out["oid"], 11)

    def test_error_envelope(self):
        out = parse_order_response({"status": "err", "response": "Insufficient margin"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], STATUS_ERROR)
        self.assertIn("Insufficient", out["error"])

    def test_status_level_error(self):
        out = parse_order_response({"status": "ok", "response": {"data": {"statuses": [
            {"error": "Order has invalid price"}]}}})
        self.assertFalse(out["ok"])
        self.assertIn("invalid price", out["error"])

    def test_unknown_shapes(self):
        for bad in (None, "oops", {}, {"status": "ok"}, {"status": "ok", "response": {}},
                    {"status": "ok", "response": {"data": {"statuses": []}}},
                    {"status": "ok", "response": {"data": {"statuses": [{"weird": 1}]}}}):
            out = parse_order_response(bad)
            self.assertFalse(out["ok"], msg=f"{bad!r} 应判定为失败")


class TestDryRun(unittest.TestCase):
    def test_all_writes_short_circuit(self):
        sdk = _FakeSdk()
        ex = HlExchange(sdk=sdk, account_address="0xabc", private_key="0xkey", dry_run=True)
        self.assertEqual(ex.update_leverage("BTC", 5)["status"], STATUS_DRY)
        self.assertEqual(ex.place_order("BTC", True, 0.01, 83000)["status"], STATUS_DRY)
        self.assertTrue(ex.cancel("BTC", 1)["ok"])
        r = ex.schedule_cancel(minutes=30)
        self.assertEqual(r["status"], STATUS_DRY)
        self.assertIn("scheduleCancel", r["raw"])
        self.assertEqual(sdk.calls, [])          # 一个真实调用都没发生

    def test_dry_run_keeps_request_shape(self):
        ex = HlExchange(sdk=_FakeSdk(), dry_run=True)
        r = ex.place_order("BTC", False, 0.5, 82000, reduce_only=True, trigger_px=82500)
        self.assertEqual(r["raw"]["order_type"],
                         {"trigger": {"triggerPx": 82500, "isMarket": True, "tpsl": "sl"}})
        self.assertTrue(r["raw"]["reduce_only"])


class TestLivePaths(unittest.TestCase):
    def test_fail_closed_without_credentials(self):
        # dry_run=True 且无凭据：允许「影子」成功（不触网），但仍标记为未配置
        dry = HlExchange(sdk=None, account_address="", private_key="", dry_run=True)
        self.assertFalse(dry.available)
        self.assertEqual(dry.place_order("BTC", True, 0.01, 83000)["status"], STATUS_DRY)
        # dry_run=False 且无凭据 → 必须 fail-closed，绝不返回成功
        ex = HlExchange(sdk=None, account_address="", private_key="", dry_run=False)
        r = ex.place_order("BTC", True, 0.01, 83000)
        self.assertFalse(r["ok"])
        self.assertIn("缺少", r["error"])
        self.assertIn("缺少", ex.init_error)
        self.assertFalse(ex.update_leverage("BTC", 5)["ok"])
        self.assertFalse(ex.schedule_cancel(minutes=1)["ok"])

    def test_place_order_passes_cloid_and_params(self):
        sdk = _FakeSdk([_ok_order()])
        ex = HlExchange(sdk=sdk, account_address="0xabc", private_key="0xkey", dry_run=False)
        cl = ex._cloid("BTC_USDT", 1, "entry")             # noqa: SLF001
        res = ex.place_order("BTC", True, 0.02, 83100.0, cloid=cl)
        self.assertTrue(res["ok"])
        name, args, kw = sdk.calls[-1]
        self.assertEqual(name, "order")
        self.assertEqual(args[0], "BTC")
        self.assertTrue(args[1])
        self.assertAlmostEqual(args[2], 0.02)
        self.assertAlmostEqual(args[3], 83100.0)
        self.assertEqual(args[4], {"limit": {"tif": "Ioc"}})
        self.assertFalse(kw["reduce_only"])
        self.assertIs(kw["cloid"], cl)

    def test_sdk_exception_normalized(self):
        class Boom(_FakeSdk):
            def order(self, *a, **kw):
                raise RuntimeError("connection reset")

        ex = HlExchange(sdk=Boom(), account_address="0xabc", private_key="0xkey", dry_run=False)
        r = ex.place_order("BTC", True, 0.01, 83000)
        self.assertFalse(r["ok"])
        self.assertIn("connection reset", r["error"])

    def test_update_leverage_and_cancel_ok(self):
        sdk = _FakeSdk([{"status": "ok"}, {"status": "ok"}])
        ex = HlExchange(sdk=sdk, account_address="0xabc", private_key="0xkey", dry_run=False)
        self.assertTrue(ex.update_leverage("BTC", 5, cross=False)["ok"])
        self.assertTrue(ex.cancel("BTC", 123)["ok"])
        self.assertEqual(sdk.calls[0][1], (5, "BTC"))
        self.assertFalse(sdk.calls[0][2]["is_cross"])

    def test_cancel_error_reported(self):
        sdk = _FakeSdk([{"status": "err", "response": "Order does not exist"}])
        ex = HlExchange(sdk=sdk, account_address="0xabc", private_key="0xkey", dry_run=False)
        r = ex.cancel("BTC", 5)
        self.assertFalse(r["ok"])
        self.assertIn("does not exist", r["error"])

    def test_cancel_all_uses_open_orders(self):
        info = _FakeInfo(orders=[{"coin": "BTC", "oid": 1}, {"coin": "ETH", "oid": 2}])
        sdk = _FakeSdk([{"status": "ok"}])
        ex = HlExchange(sdk=sdk, info=info, account_address="0xabc", private_key="0xkey", dry_run=False)
        r = ex.cancel_all("BTC")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["raw"]), 1)                  # 只撤 BTC 那一条
        self.assertEqual(sdk.calls[0][1], ("BTC", 1))

    def test_schedule_cancel_passes_future_ms(self):
        sdk = _FakeSdk([{"status": "ok"}])
        ex = HlExchange(sdk=sdk, account_address="0xabc", private_key="0xkey", dry_run=False)
        r = ex.schedule_cancel(time_ms=1791999999000)
        self.assertTrue(r["ok"])
        self.assertEqual(sdk.calls[0][1], (1791999999000,))


class TestPricing(unittest.TestCase):
    def test_mid_and_slippage(self):
        ex = HlExchange(info=_FakeInfo({"BTC": 83000.0}), dry_run=True)
        self.assertAlmostEqual(ex.mid("BTC"), 83000.0)
        self.assertAlmostEqual(ex.slippage_limit("BTC", True), 83000.0 * 1.003)
        self.assertAlmostEqual(ex.slippage_limit("BTC", False), 83000.0 * 0.997)

    def test_missing_mid_raises(self):
        from app.hl.info import HlInfoError
        ex = HlExchange(info=_FakeInfo({}), dry_run=True)
        with self.assertRaises(HlInfoError):
            ex.slippage_limit("BTC", True)


if __name__ == "__main__":
    unittest.main()
