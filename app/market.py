"""行情取数：MEXC 永续 K 线（主）+ 降级链（纯 stdlib，无第三方依赖）。

- 主源：`https://contract.mexc.com/api/v1/contract/kline/{SYMBOL}?interval=Hour4`
  返回最近 2000 根，**列式** time/open/high/low/close/vol，**最后一根是未收盘的当前 bar**。
- 降级链：MEXC 合约 → MEXC 现货 → Binance fapi → OKX；降级时在提醒里标注来源。
- 校验：根数 ≥ MIN_BARS_REQUIRED、时间单调、最后一根已收盘且新鲜。
"""
from __future__ import annotations

import json
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


SOURCES: List[Tuple[str, Callable[[str], List[Bar]]]] = [
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


def fetch(symbol: str, now_ms: Optional[int] = None) -> Feed:
    """按降级链取数；全部失败抛 FeedError。"""
    errors = []
    for name, fn in SOURCES:
        try:
            bars = fn(symbol)
            validate(symbol, bars, now_ms=now_ms)
            return Feed(symbol=symbol, source=name, bars=bars,
                        fetched_at_ms=now_ms if now_ms is not None else int(time.time() * 1000))
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"{name}: {e}")
            continue
    raise FeedError(f"{symbol} 所有数据源均失败 → " + " | ".join(errors))
