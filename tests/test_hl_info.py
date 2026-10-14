"""app/hl/info.py 的单元测试（全部离线，注入假 opener）。"""
from __future__ import annotations

import json
import unittest

from app.hl.info import HlInfo, HlInfoError


class _Resp:
    """模拟 urllib 的 response（上下文管理器 + read()）。"""

    def __init__(self, payload, headers=None):
        self._body = json.dumps(payload).encode()
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, responses):
        """responses: 列表，元素为 dict（成功）或 Exception（抛错）。"""
        self.responses = list(responses)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(json.loads(req.data.decode()))
        item = self.responses.pop(0) if self.responses else {"ok": True}
        if isinstance(item, Exception):
            raise item
        return _Resp(item)


def _info(responses, retries=3, backoff=(0, 0, 0)):
    return HlInfo(base_url="https://api.test", retries=retries, backoff=backoff,
                  opener=_FakeOpener(responses))


class TestPost(unittest.TestCase):
    def test_payload_and_parse(self):
        f = _FakeOpener([{"universe": [{"name": "BTC", "szDecimals": 5}]}])
        info = HlInfo(base_url="https://api.test", opener=f)
        out = info.meta()
        self.assertEqual(out["universe"][0]["name"], "BTC")
        self.assertEqual(f.calls[0], {"type": "meta"})

    def test_retries_then_succeeds(self):
        f = _FakeOpener([OSError("boom"), {"result": "ok"}])
        info = HlInfo(base_url="https://api.test", retries=3, backoff=(0, 0, 0), opener=f)
        self.assertEqual(info.post({"type": "x"}), {"result": "ok"})
        self.assertEqual(len(f.calls), 2)

    def test_all_retries_fail_raises(self):
        f = _FakeOpener([OSError("boom"), OSError("boom"), OSError("boom")])
        info = HlInfo(base_url="https://api.test", retries=3, backoff=(0, 0, 0), opener=f)
        with self.assertRaises(HlInfoError):
            info.post({"type": "x"})
        self.assertEqual(len(f.calls), 3)


class TestSpecsAndMids(unittest.TestCase):
    def test_all_mids_filters_bad_numbers(self):
        info = _info([{"BTC": "83000.5", "ETH": "2500", "BAD": "n/a"}])
        self.assertEqual(info.all_mids(), {"BTC": 83000.5, "ETH": 2500.0})

    def test_specs_subset(self):
        info = _info([{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                                    {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]}])
        self.assertEqual(info.specs(["ETH"]), {"ETH": {"sz_decimals": 4, "max_leverage": 25,
                                                       "only_isolated": False}})

    def test_specs_skips_nameless(self):
        info = _info([{"universe": [{"szDecimals": 1}, {"name": "SOL", "szDecimals": 2}]}])
        self.assertEqual(list(info.specs()), ["SOL"])


class TestSnapshot(unittest.TestCase):
    def _state(self):
        return {
            "marginSummary": {"accountValue": "1000.5", "totalMarginUsed": "120", "totalNtlPos": "600"},
            "withdrawable": "880.5",
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "-0.05", "entryPx": "83000", "positionValue": "4150",
                              "liquidationPx": "90000", "marginUsed": "83", "unrealizedPnl": "-12.5",
                              "returnOnEquity": "-0.15",
                              "leverage": {"type": "isolated", "value": 50},
                              "cumFunding": {"sinceOpen": "-1.25"}}},
                {"position": {"coin": "ETH", "szi": "0", "entryPx": "2500",
                              "leverage": {"type": "cross", "value": 10}}},
            ],
        }

    def test_short_position_normalized(self):
        # 调用顺序：user_state → all_mids → open_orders
        info = _info([self._state(), {"BTC": 83100.0, "ETH": 2500.0}, []])
        snap = info.snapshot("0xabc")
        self.assertEqual(len(snap["positions"]), 1)          # 零仓被过滤
        p = snap["positions"][0]
        self.assertEqual(p["coin"], "BTC")
        self.assertEqual(p["side"], -1)                      # szi 为负 = 空
        self.assertAlmostEqual(p["size"], 0.05)
        self.assertEqual(p["leverage"], 50)
        self.assertTrue(p["isolated"])
        self.assertAlmostEqual(p["mark_px"], 83100.0)        # mark 来自 allMids
        self.assertAlmostEqual(p["funding_since_open"], -1.25)
        self.assertAlmostEqual(snap["equity"], 1000.5)
        self.assertAlmostEqual(snap["withdrawable"], 880.5)
        self.assertAlmostEqual(snap["margin_used"], 120.0)

    def test_open_orders_normalized(self):
        info = _info([self._state(), {"BTC": 83100.0}, [
            {"coin": "BTC", "oid": 42, "side": "A", "sz": "0.05", "limitPx": "90000",
             "reduceOnly": True, "triggerPx": "90000", "isTrigger": True, "timestamp": 1},
            {"coin": "ETH", "oid": 43, "side": "B", "sz": "1", "limitPx": "2400",
             "reduceOnly": False, "triggerPx": None, "isTrigger": False, "timestamp": 2},
        ], {"BTC": 83100.0}])
        snap = info.snapshot("0xabc")
        o1, o2 = snap["open_orders"]
        self.assertFalse(o1["is_buy"])            # side A = 卖
        self.assertTrue(o1["reduce_only"] and o1["is_trigger"])
        self.assertAlmostEqual(o1["trigger_px"], 90000.0)
        self.assertTrue(o2["is_buy"])
        self.assertEqual(o2["trigger_px"], 0.0)   # None → 0

    def test_mark_fallback_from_position_value(self):
        info = _info([{
            "marginSummary": {"accountValue": "1", "totalMarginUsed": "0", "totalNtlPos": "0"},
            "withdrawable": "1",
            "assetPositions": [{"position": {"coin": "BTC", "szi": "0.05", "entryPx": "80000",
                                             "positionValue": "4150", "leverage": {"type": "isolated", "value": 5}}}],
        }, [], {}])   # allMids 为空 → 用 positionValue/size 兜底
        snap = info.snapshot("0xabc")
        self.assertAlmostEqual(snap["positions"][0]["mark_px"], 83000.0)

    def test_all_mids_failure_does_not_break_snapshot(self):
        info = _info([{"marginSummary": {"accountValue": "5"}, "withdrawable": "5",
                       "assetPositions": []}, OSError("net"), OSError("net"), OSError("net")],
                     retries=3, backoff=(0, 0, 0))
        snap = info.snapshot("0xabc")
        self.assertEqual(snap["positions"], [])
        self.assertAlmostEqual(snap["equity"], 5.0)

    def test_mids_can_be_passed_in(self):
        f = _FakeOpener([self._state(), [], {"balances": []}])   # state + openOrders + 现货
        info = HlInfo(base_url="https://api.test", opener=f)
        snap = info.snapshot("0xabc", mids={"BTC": 84000.0})
        self.assertAlmostEqual(snap["positions"][0]["mark_px"], 84000.0)
        self.assertEqual(len(f.calls), 3)
        self.assertEqual([c["type"] for c in f.calls],
                         ["clearinghouseState", "frontendOpenOrders", "spotClearinghouseState"])

    def test_empty_response(self):
        info = _info([{}])
        snap = info.snapshot("0xabc")
        self.assertEqual(snap["positions"], [])
        self.assertEqual(snap["equity"], 0.0)


class TestEndpoints(unittest.TestCase):
    def test_payload_shapes(self):
        f = _FakeOpener([[], [], [], [], [], [], []])
        info = HlInfo(base_url="https://api.test", opener=f)
        info.open_orders("0x1")
        info.user_fills("0x1")
        info.user_fills_by_time("0x1", 1, 2)
        info.user_funding_history("0x1", 1)
        info.candles("BTC", "4h", 1, 2)
        info.funding_history("BTC", 1)
        want_types = ["frontendOpenOrders", "userFills", "userFillsByTime", "userFunding",
                      "candleSnapshot", "fundingHistory"]
        got = [c["type"] for c in f.calls]
        self.assertEqual(got, want_types)
        self.assertEqual(f.calls[4]["req"], {"coin": "BTC", "interval": "4h",
                                             "startTime": 1, "endTime": 2})


class TestUnifiedAccount(unittest.TestCase):
    """统一账户（HL 新账户默认）：余额在现货状态里，永续 accountValue 恒为 0。

    官方文档 Important details #3：「对 API 用户，统一账户/组合保证金把余额和占用
    显示在现货状态里，各个 perp dex 的用户状态没有意义」。
    """

    STATE = {"marginSummary": {"accountValue": "0.0", "totalMarginUsed": "0.0",
                              "totalNtlPos": "0.0"},
             "withdrawable": "0.0", "assetPositions": []}

    def test_unified_equity_comes_from_spot(self):
        f = _FakeOpener([self.STATE, [], {"balances": [{"coin": "USDC", "total": "100.79", "hold": "0.0"},
                                                       {"coin": "HYPE", "total": "3.0", "hold": "0.0"}]}])
        info = HlInfo(base_url="https://api.test", opener=f, unified=True)
        snap = info.snapshot("0xabc", mids={})
        self.assertEqual(snap["equity"], 0.0)                 # 永续口径（统一账户下为 0）
        self.assertAlmostEqual(snap["equity_total"], 100.79)  # 总值 = 现货 USDC
        self.assertAlmostEqual(snap["spot_usdc"], 100.79)
        self.assertTrue(snap["unified"])
        self.assertEqual([b["coin"] for b in snap["spot_balances"]], ["USDC", "HYPE"])

    def test_unified_total_adds_unrealized_pnl(self):
        state = dict(self.STATE)
        state["assetPositions"] = [{"position": {"coin": "BTC", "szi": "0.01", "entryPx": "83000",
                                                "positionValue": "830", "liquidationPx": "70000",
                                                "marginUsed": "166", "unrealizedPnl": "-7.5",
                                                "leverage": {"type": "isolated", "value": 5},
                                                "cumFunding": {"sinceOpen": "0.01"}}}]
        f = _FakeOpener([state, [], {"balances": [{"coin": "USDC", "total": "100.79", "hold": "83.0"}]}])
        info = HlInfo(base_url="https://api.test", opener=f, unified=True)
        snap = info.snapshot("0xabc", mids={"BTC": 82500.0})
        self.assertAlmostEqual(snap["equity_total"], 100.79 - 7.5, places=6)
        self.assertAlmostEqual(snap["spot_balances"][0]["hold"], 83.0)   # 占用照实显示

    def test_standard_mode_keeps_perp_equity_and_skips_spot_call(self):
        state = dict(self.STATE)
        state["marginSummary"] = {"accountValue": "250.0", "totalMarginUsed": "10.0", "totalNtlPos": "0.0"}
        f = _FakeOpener([state, []])                    # 不该再去读现货
        info = HlInfo(base_url="https://api.test", opener=f, unified=False)
        snap = info.snapshot("0xabc", mids={})
        self.assertEqual(snap["equity_total"], 250.0)
        self.assertEqual(snap["spot_usdc"], 0.0)
        self.assertEqual([c["type"] for c in f.calls], ["clearinghouseState", "frontendOpenOrders"])

    def test_spot_read_failure_does_not_break_snapshot(self):
        f = _FakeOpener([self.STATE, [], OSError("spot down"), OSError("spot down"), OSError("spot down")])
        info = HlInfo(base_url="https://api.test", opener=f, retries=3, backoff=(0, 0, 0), unified=True)
        snap = info.snapshot("0xabc", mids={})
        self.assertEqual(snap["equity_total"], 0.0)      # 读不到就按 0，不抛异常、不影响持仓读取
        self.assertEqual(snap["positions"], [])


class TestRealOrderPayloads(unittest.TestCase):
    """用交易所**真实返回**（2026-10-14 探针抓下来的）做回归：触发止损单必须被认出来。

    坑：`openOrders` 对触发单不返回 isTrigger/triggerPx（触发价只在 limitPx 里），
    只读它会把「有仓 + 已挂止损」判成裸奔 → 面板 ❌ / 看守误报 / 对账直接拉 kill switch。
    """

    REAL_FRONTEND_STOP = {"coin": "BTC", "side": "A", "limitPx": "81843.0", "sz": "0.00014",
                          "oid": 573830864866, "timestamp": 1791955204194, "origSz": "0.00014",
                          "triggerCondition": "Price below 81843", "isTrigger": True,
                          "triggerPx": "81843.0", "children": [], "isPositionTpsl": False,
                          "reduceOnly": True, "orderType": "Stop Market", "tif": None,
                          "cloid": "0xcebaa2b0b8a3b809a14741eeb6cbcf4b"}
    REAL_LEGACY_OPEN = {"coin": "BTC", "side": "A", "limitPx": "81843.0", "sz": "0.00014",
                        "oid": 573830864866, "timestamp": 1791955204194, "origSz": "0.00014",
                        "reduceOnly": True, "cloid": "0xcebaa2b0b8a3b809a14741eeb6cbcf4b"}

    def _snap(self, order, fallback=False):
        state = {"marginSummary": {"accountValue": "0.0"}, "withdrawable": "0.0", "assetPositions": []}
        script = [state, [order], {"balances": []}]
        if fallback:                     # frontendOpenOrders 失败 → 回落 openOrders
            script = [state, OSError("frontend down"), [order], {"balances": []}]
        f = _FakeOpener(script)
        info = HlInfo(base_url="https://api.test", opener=f, unified=True,
                      retries=1 if fallback else 3, backoff=(0, 0, 0))
        return info.snapshot("0xabc", mids={}), f

    def test_trigger_stop_detected_as_has_stop(self):
        snap, f = self._snap(self.REAL_FRONTEND_STOP)
        self.assertEqual(f.calls[1]["type"], "frontendOpenOrders")
        o = snap["open_orders"][0]
        self.assertTrue(o["is_trigger"])
        self.assertTrue(o["reduce_only"])
        self.assertAlmostEqual(o["trigger_px"], 81843.0)
        self.assertEqual(o["order_type"], "Stop Market")
        has_stop = any(x["coin"] == "BTC" and x["reduce_only"] and x["is_trigger"]
                       for x in snap["open_orders"])
        self.assertTrue(has_stop, "真实止损单必须被判成 has_stop")

    def test_non_trigger_limit_order_is_not_has_stop(self):
        snap, _ = self._snap(dict(self.REAL_FRONTEND_STOP, isTrigger=False, triggerPx=None,
                                  orderType="Limit", reduceOnly=False))
        has_stop = any(x["coin"] == "BTC" and x["reduce_only"] and x["is_trigger"]
                       for x in snap["open_orders"])
        self.assertFalse(has_stop)

    def test_fallback_to_open_orders_keeps_known_gap(self):
        """回落老接口时 isTrigger 缺失（已知差异）：此时 has_stop 会是 False ——
        所以只在 frontendOpenOrders 真的不可用时才会走到这里，且会打 WARNING。"""
        snap, f = self._snap(self.REAL_LEGACY_OPEN, fallback=True)
        self.assertEqual([c["type"] for c in f.calls],
                         ["clearinghouseState", "frontendOpenOrders", "openOrders", "spotClearinghouseState"])
        self.assertFalse(snap["open_orders"][0]["is_trigger"])


if __name__ == "__main__":
    unittest.main()
