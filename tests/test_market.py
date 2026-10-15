"""行情取数测试：**实盘信号必须来自交易场所自己的 K 线**（HL），失败即 fail-closed。

背景（用户拍板 2026-10-14）：下单/止损/成交都在 HL，信号却拿别家 K 线 = 拿 A 家行情做
B 家交易。实测两源信号一致率 97~98%、方向无翻转，但有 2~3% 边缘信号会因换源出现/消失。
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import market                                   # noqa: E402
from app.market import FeedError                         # noqa: E402
from app.strategy import Bar, MIN_BARS_REQUIRED          # noqa: E402

IV = 4 * 3600 * 1000
NOW = int(time.time() * 1000)


def hl_rows(n: int = MIN_BARS_REQUIRED + 5, last_ms: int | None = None) -> list:
    """造 HL candleSnapshot 的原始返回（字段 t/o/h/l/c/v，字符串价格）。"""
    end = last_ms if last_ms is not None else (NOW // IV) * IV
    out = []
    for i in range(n):
        t = end - (n - 1 - i) * IV
        px = 83000.0 + i
        out.append({"t": t, "o": f"{px}", "h": f"{px + 10}", "l": f"{px - 10}",
                    "c": f"{px + 5}", "v": "1.5"})
    return out


class TestHlSource(unittest.TestCase):
    def setUp(self):
        self.saved = market._hl_post

    def tearDown(self):
        market._hl_post = self.saved

    def _stub(self, rows):
        self.payloads = []

        def fake(payload, timeout=20):
            self.payloads.append(payload)
            return rows
        market._hl_post = fake

    def test_parses_and_sorts_bars(self):
        rows = hl_rows(10)
        rows.reverse()                            # 故意倒序：必须自己排好
        self._stub(rows)
        bars = market._hyperliquid("BTC_USDT")
        self.assertEqual(len(bars), 10)
        self.assertTrue(all(bars[i].open_ms < bars[i + 1].open_ms for i in range(9)))
        self.assertIsInstance(bars[0], Bar)
        self.assertAlmostEqual(bars[0].open, float(rows[-1]["o"]))
        self.assertAlmostEqual(bars[0].high, float(rows[-1]["h"]))

    def test_requests_hl_coin_name_not_mexc_symbol(self):
        self._stub(hl_rows(5))
        market._hyperliquid("BTC_USDT")
        req = self.payloads[0]["req"]
        self.assertEqual(req["coin"], "BTC")      # HL 只认合约名
        self.assertEqual(req["interval"], "4h")
        self.assertEqual(self.payloads[0]["type"], "candleSnapshot")

    def test_coin_map_env_override(self):
        os.environ["HL_COIN_MAP"] = "BTC_USDT:kPEPE"
        try:
            self.assertEqual(market.hl_coin("BTC_USDT"), "kPEPE")
        finally:
            os.environ.pop("HL_COIN_MAP", None)
        self.assertEqual(market.hl_coin("ETH_USDT"), "ETH")

    def test_eth_mapping(self):
        self.assertEqual(market.hl_coin("ETH_USDT"), "ETH")
        # SOL 已登记进注册表（feat/add-sol）；旧版「未命中取 symbol 前缀」的静默兜底
        # 已在 feat/coin-failclosed 删除，未注册一律抛 FeedError（见 test_coin_failclosed.py）
        self.assertEqual(market.hl_coin("SOL_USDT"), "SOL")
        with self.assertRaises(FeedError):
            market.hl_coin("1000PEPE_USDT")


class TestFetchPolicy(unittest.TestCase):
    """主源=HL，失败必须 fail-closed（绝不换别家数据下单）。"""

    def setUp(self):
        self.saved_src = market.SOURCES
        self.saved_diag = market.DIAGNOSTIC_SOURCES

    def tearDown(self):
        market.SOURCES = self.saved_src
        market.DIAGNOSTIC_SOURCES = self.saved_diag

    def _bars(self, n=MIN_BARS_REQUIRED + 5):
        return [Bar(open_ms=NOW - (n - i) * IV, open=83000.0, high=83100.0, low=82900.0,
                    close=83050.0, volume=1.0) for i in range(n)]

    def test_primary_source_is_hl(self):
        self.assertEqual([n for n, _ in market.SOURCES], ["hl-candle"])
        self.assertIn("mexc-contract", [n for n, _ in market.DIAGNOSTIC_SOURCES])

    def test_fetch_uses_hl(self):
        market.SOURCES = [("hl-candle", lambda s: self._bars())]
        market.DIAGNOSTIC_SOURCES = [("mexc-contract", lambda s: (_ for _ in ()).throw(
            AssertionError("不能用到诊断源")))]
        feed = market.fetch("BTC_USDT", now_ms=NOW)
        self.assertEqual(feed.source, "hl-candle")

    def test_hl_failure_does_not_fall_back(self):
        """HL 挂了 → 必须抛 FeedError（宁可停手，不用别家数据）"""
        market.SOURCES = [("hl-candle", lambda s: (_ for _ in ()).throw(
            RuntimeError("HL 502")))]
        market.DIAGNOSTIC_SOURCES = [("mexc-contract", lambda s: self._bars())]
        with self.assertRaises(FeedError) as ctx:
            market.fetch("BTC_USDT", now_ms=NOW)
        self.assertIn("所有数据源均失败", str(ctx.exception))

    def test_fallback_only_when_explicitly_allowed(self):
        market.SOURCES = [("hl-candle", lambda s: (_ for _ in ()).throw(RuntimeError("down")))]
        market.DIAGNOSTIC_SOURCES = [("mexc-contract", lambda s: self._bars())]
        feed = market.fetch("BTC_USDT", now_ms=NOW, allow_fallback=True)
        self.assertEqual(feed.source, "mexc-contract")

    def test_stale_data_rejected(self):
        old = self._bars()
        stale = [Bar(open_ms=b.open_ms - 10 * IV, open=b.open, high=b.high, low=b.low,
                     close=b.close, volume=b.volume) for b in old]
        market.SOURCES = [("hl-candle", lambda s: [stale, None][0])]
        with self.assertRaises(FeedError) as ctx:
            market.fetch("BTC_USDT", now_ms=NOW)
        self.assertIn("陈旧", str(ctx.exception))

    def test_short_series_rejected(self):
        market.SOURCES = [("hl-candle", lambda s: self._bars(10))]
        with self.assertRaises(FeedError):
            market.fetch("BTC_USDT", now_ms=NOW)


class TestHlSourceLive(unittest.TestCase):
    """真网冒烟：HL 公开接口真的能给出足够长、列对齐的 4h 序列（CI 里可能无网 → 跳过）。"""

    @unittest.skipIf(os.environ.get("SKIP_NET") == "1", "SKIP_NET=1")
    def test_real_hl_candles(self):
        try:
            bars = market._hyperliquid("BTC_USDT")
        except Exception as e:                        # noqa: BLE001
            self.skipTest(f"无外网或 HL 不可达：{e}")
        self.assertGreaterEqual(len(bars), MIN_BARS_REQUIRED + 1)
        self.assertTrue(all(bars[i].open_ms % IV == 0 for i in range(3)))
        self.assertTrue(all(bars[i].open_ms < bars[i + 1].open_ms for i in range(len(bars) - 1)))
        market.validate("BTC_USDT", bars, now_ms=int(time.time() * 1000))   # 不抛即通过


if __name__ == "__main__":
    unittest.main()
