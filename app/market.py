"""行情取数：**实盘信号用交易场所自己的 K 线**（HL），其他源只作诊断（纯 stdlib）。

为什么主源是 HL：下单、止损、成交全在 HL 进行，信号却拿别家交易所的 K 线 = 拿 A 家行情做
B 家交易。实测（2024-07~2026-10 同窗口、同一套线上规则）：HL 与 Binance 的 4h 收盘价中位差
4 bps、最大 24 bps，97~98% 的信号一致、方向无翻转，但有 **2~3% 的边缘信号会因换源而出现/
消失** —— 擦着均线边界的那些笔，由谁的数据决定。所以实盘必须与交易场所同源。

- 主源：`POST https://api.hyperliquid.xyz/info` {"type":"candleSnapshot"}（公开、无鉴权，
  4h，返回最近 5000 根 = 约 833 天，**最后一根是未收盘的当前 bar**）。
- **fail-closed**：主源失败就抛 FeedError → 引擎转成「数据源异常」提醒并跳过本轮，
  **绝不悄悄用别家交易所的数据去下单**。降级链只在 `allow_fallback=True`（诊断/展示）时启用。
- 校验：根数 ≥ MIN_BARS_REQUIRED、时间单调、最后一根已收盘且新鲜。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from app.strategy import Bar, MIN_BARS_REQUIRED

INTERVAL_MS = 4 * 3600 * 1000
STALE_TOLERANCE_MS = 2 * INTERVAL_MS      # 最后一根收盘距今超过这个值即判数据源异常


class FeedError(RuntimeError):
    pass


@dataclass
class Feed:
    symbol: str
    source: str
    bars: List[Bar]                       # 含未收盘的最后一根
    fetched_at_ms: int

    @property
    def closed(self) -> List[Bar]:
        """已收盘 bar（丢掉最后一根进行中的）。"""
        return self.bars[:-1]

    @property
    def current_open(self) -> float:
        """进行中那根的开盘价 = 次根开盘价（入场参考价）。"""
        return self.bars[-1].open

    @property
    def last_closed_ms(self) -> int:
        return self.bars[-2].open_ms

    @property
    def last_closed_age_ms(self) -> int:
        return self.fetched_at_ms - (self.last_closed_ms + INTERVAL_MS)


def _http_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-ma-alert/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------- HL（实盘主源）
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
# 信号层的键是 MEXC 风格（BTC_USDT），HL 只认合约名（BTC/ETH/SOL）
HL_COIN_NAMES = {"BTC_USDT": "BTC", "ETH_USDT": "ETH", "SOL_USDT": "SOL"}


def _coin_map() -> dict:
    """解析 HL_COIN_MAP：兼容 "BTC_USDT:BTC,ETH_USDT:ETH" 与 JSON 两种格式
    （main.py 用 json.loads、这里用逗号格式 —— 两处口径必须一致，否则信号层与
    执行器会对同一个 symbol 解析出不同的 coin）。"""
    raw = os.environ.get("HL_COIN_MAP", "").strip()
    if not raw:
        return {}
    m: dict = {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k).strip(): str(v).strip() for k, v in data.items() if str(v).strip()}
        except (ValueError, TypeError):
            pass
    for pair in raw.split(","):
        k, _, v = pair.partition(":")
        if k.strip() and v.strip():
            m[k.strip()] = v.strip()
    return m


def hl_coin(symbol: str, strict: bool = False) -> str:
    """BTC_USDT → BTC。可用 HL_COIN_MAP 覆盖（格式见 _coin_map）。

    strict=True（信号路径）：未知 symbol 抛 FeedError —— 静默取 symbol 前缀
    会让 BTC_USD 这类拼写错误拿到 BTC 的数据、在错误的标的上算信号。
    """
    m = _coin_map()
    if symbol in m:
        return m[symbol]
    if symbol in HL_COIN_NAMES:
        return HL_COIN_NAMES[symbol]
    if strict:
        raise FeedError(f"{symbol}: 未知的 symbol（不在 HL_COIN_NAMES / HL_COIN_MAP）")
    return symbol.split("_")[0]


def _hl_post(payload: dict, timeout: int = 20):
    req = urllib.request.Request(
        HL_INFO_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "crypto-ma-alert/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _hyperliquid(symbol: str) -> List[Bar]:
    """HL 官方 4h K 线（公开、无鉴权）。字段 t/o/h/l/c/v（毫秒 + 字符串价格）。"""
    rows = _hl_post({"type": "candleSnapshot",
                     "req": {"coin": hl_coin(symbol, strict=True), "interval": "4h", "startTime": 0}})
    bars = [Bar(open_ms=int(c["t"]), open=float(c["o"]), high=float(c["h"]),
                low=float(c["l"]), close=float(c["c"]), volume=float(c["v"]))
            for c in rows]
    bars.sort(key=lambda b: b.open_ms)
    return bars


def _mexc_contract(symbol: str, limit: int = 2000) -> List[Bar]:
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?interval=Hour4"
    d = _http_json(url)
    if not d.get("success"):
        raise FeedError(f"mexc contract: {d.get('message') or d.get('code')}")
    k = d["data"]
    ts = k["time"]
    return [Bar(open_ms=int(ts[i]) * 1000, open=float(k["open"][i]), high=float(k["high"][i]),
                low=float(k["low"][i]), close=float(k["close"][i]), volume=float(k["vol"][i]))
            for i in range(len(ts))]


def _mexc_spot(symbol: str) -> List[Bar]:
    spot = symbol.replace("_", "")
    url = f"https://api.mexc.com/api/v3/klines?symbol={spot}&interval=4h&limit=1000"
    rows = _http_json(url)
    return [Bar(open_ms=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5])) for r in rows]


def _binance_fapi(symbol: str) -> List[Bar]:
    spot = symbol.replace("_", "")
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={spot}&interval=4h&limit=1500"
    rows = _http_json(url)
    return [Bar(open_ms=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5])) for r in rows]


def _okx(symbol: str) -> List[Bar]:
    """OKX 单次上限 300 根，需要翻页到 ≥ MIN_BARS_REQUIRED 根，否则分位永远算不出来。"""
    base = symbol.split("_")[0]
    bars: List[Bar] = []
    after = ""
    for _ in range(3):
        url = (f"https://www.okx.com/api/v5/market/candles?instId={base}-USDT-SWAP"
               f"&bar=4H&limit=300" + (f"&after={after}" if after else ""))
        d = _http_json(url)
        if d.get("code") != "0":
            raise FeedError(f"okx: {d.get('msg')}")
        rows = d["data"]
        if not rows:
            break
        bars = [Bar(open_ms=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                    close=float(r[4]), volume=float(r[5])) for r in rows] + bars
        after = rows[-1][0]
        if len(bars) >= MIN_BARS_REQUIRED + 50:
            break
    return bars


# 实盘信号源：**只有交易场所自己的数据**（失败即 fail-closed，不换源下单）
SOURCES: List[Tuple[str, Callable[[str], List[Bar]]]] = [
    ("hl-candle", _hyperliquid),
]

# 仅诊断/展示用（allow_fallback=True 时才参与），绝不用于下单决策
DIAGNOSTIC_SOURCES: List[Tuple[str, Callable[[str], List[Bar]]]] = [
    ("mexc-contract", _mexc_contract),
    ("mexc-spot", _mexc_spot),
    ("binance-fapi", _binance_fapi),
    ("okx-swap", _okx),
]


def validate(symbol: str, bars: List[Bar], now_ms: Optional[int] = None) -> None:
    """数据完整性校验；不合格直接抛 FeedError（调用方转为「数据源异常」提醒）。"""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if len(bars) < MIN_BARS_REQUIRED + 1:
        raise FeedError(f"{symbol}: 只有 {len(bars)} 根，少于要求的 {MIN_BARS_REQUIRED + 1}")
    for i in range(1, len(bars)):
        if bars[i].open_ms <= bars[i - 1].open_ms:
            raise FeedError(f"{symbol}: 时间非单调（第 {i} 根）")
        if (bars[i].open_ms - bars[i - 1].open_ms) != INTERVAL_MS:
            raise FeedError(f"{symbol}: 第 {i} 根间隔不是 4h（缺 bar）")
    if any(b.close <= 0 or b.high < b.low for b in bars):
        raise FeedError(f"{symbol}: 存在非法价格")
    last_closed_end = bars[-2].open_ms + INTERVAL_MS
    age = now_ms - last_closed_end
    if age > STALE_TOLERANCE_MS:
        raise FeedError(f"{symbol}: 数据陈旧，最后一根收盘距今 {age / 3600000:.1f} 小时")
    if age < -INTERVAL_MS:
        raise FeedError(f"{symbol}: 时间戳超前")


def fetch(symbol: str, now_ms: Optional[int] = None, allow_fallback: bool = False) -> Feed:
    """取数。默认**只用主源（HL = 下单场所）**；主源失败抛 FeedError（fail-closed）。

    allow_fallback=True 才把诊断源接进降级链（仅供面板/排查，不用于信号）。
    """
    errors = []
    chain = SOURCES + (DIAGNOSTIC_SOURCES if allow_fallback else [])
    for name, fn in chain:
        try:
            bars = fn(symbol)
            validate(symbol, bars, now_ms=now_ms)
            return Feed(symbol=symbol, source=name, bars=bars,
                        fetched_at_ms=now_ms if now_ms is not None else int(time.time() * 1000))
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"{name}: {e}")
            continue
    raise FeedError(f"{symbol} 所有数据源均失败 → " + " | ".join(errors))
