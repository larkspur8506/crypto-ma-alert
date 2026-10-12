"""双均线（6MA）趋势系统核心逻辑 — 纯 stdlib，无第三方依赖。

规则（冻结版 v1，2026-10-12 与用户确认）：

- 指标：SMA20/60/120 + EMA20/60/120（收盘价）
- 均线密集：band = (6均线max − 6均线min) / 收盘，要求 band ≤ 过去 250 根的最窄 20% 分位，
  且连续 ≥ 6 根成立
- 入场（只做多）：本根收盘 > 上一根的 6均线max（band 上沿）
  → 次根开盘市价入场；止损 = 上一根的 6均线min（band 下沿）
  → 止损距离必须落在 [1.5%, 10%]，否则放弃这笔
- 离场：任一根收盘 < SMA20（止损优先于离场）

本模块是**唯一**的信号口径来源：回测脚本、dry-run 回放、线上定时任务全部 import 它，
避免「实盘代码与回测口径不一致」。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

SMA_WINDOWS = (20, 60, 120)
EMA_WINDOWS = (20, 60, 120)
MA_KEYS = [f"sma{n}" for n in SMA_WINDOWS] + [f"ema{n}" for n in EMA_WINDOWS]

BAND_WINDOW = 250          # 分位回看窗口（根）
BAND_QUANTILE = 0.20       # 最窄的 20%
MIN_SQUEEZE_BARS = 6       # 密集需连续成立的根数
STOP_MIN = 0.015           # 止损距离下限（太窄是噪音）
STOP_MAX = 0.10            # 止损距离上限（配合逐仓 5x：强平 -19.5% 永远晚于止损）

WARMUP_BARS = 120          # SMA120 需要的最小根数
# 算一次有效分位需要：250 根窗口 + 120 根预热（band 在前 119 根为 None）
MIN_BARS_REQUIRED = BAND_WINDOW + WARMUP_BARS + 2


@dataclass(frozen=True)
class Bar:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Signal:
    symbol: str
    bar_index: int            # 突破 bar 的下标
    bar_open_ms: int          # 突破 bar 的开盘时间（幂等键的一部分）
    entry_price: float        # 次根开盘价（= 提醒发出时的市价）
    stop_price: float         # band 下沿
    stop_dist: float          # (entry - stop) / entry
    ma_max: float
    ma_min: float
    band: float

    @property
    def side(self) -> int:
        return 1              # v1 只做多


# ---------------------------------------------------------------- 指标

def sma_series(values: Sequence[float], n: int) -> List[Optional[float]]:
    """简单均线；前 n-1 根为 None。

    逐根重新求和（而非滚动加减），避免长序列上的浮点漂移导致
    「收盘恰好等于均线」被误判为跌破/突破。
    """
    out: List[Optional[float]] = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = sum(values[i - n + 1:i + 1]) / n
    return out


def ema_series(values: Sequence[float], n: int) -> List[Optional[float]]:
    """指数均线，首值播种（与 pandas ewm(span=n, adjust=False) 口径一致），从第 0 根就有值。"""
    out: List[Optional[float]] = [None] * len(values)
    if not values:
        return out
    k = 2.0 / (n + 1)
    prev = float(values[0])
    out[0] = prev
    for i in range(1, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def quantile(values: Sequence[float], q: float) -> Optional[float]:
    """线性插值分位（与 pandas Series.quantile 默认口径一致）。"""
    if not values:
        return None
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def compute_indicators(bars: Sequence[Bar]) -> Dict[str, List[Optional[float]]]:
    """返回 sma20/60/120、ema20/60/120、ma_max、ma_min、band（逐根对齐）。"""
    closes = [b.close for b in bars]
    out: Dict[str, List[Optional[float]]] = {}
    for n in SMA_WINDOWS:
        out[f"sma{n}"] = sma_series(closes, n)
    for n in EMA_WINDOWS:
        out[f"ema{n}"] = ema_series(closes, n)

    n_bars = len(bars)
    ma_max: List[Optional[float]] = [None] * n_bars
    ma_min: List[Optional[float]] = [None] * n_bars
    band: List[Optional[float]] = [None] * n_bars
    for i in range(n_bars):
        vals = [out[k][i] for k in MA_KEYS]
        if any(v is None for v in vals):
            continue
        hi, lo = max(vals), min(vals)          # type: ignore[arg-type]
        ma_max[i], ma_min[i] = hi, lo
        if closes[i] > 0:
            band[i] = (hi - lo) / closes[i]
    out["ma_max"], out["ma_min"], out["band"] = ma_max, ma_min, band
    return out


# ---------------------------------------------------------------- 密集 / 信号

def squeeze_flags(band: Sequence[Optional[float]], window: int = BAND_WINDOW,
                  q: float = BAND_QUANTILE, min_bars: int = MIN_SQUEEZE_BARS) -> List[bool]:
    """连续 min_bars 根满足「band ≤ 过去 window 根的 q 分位」即为 True。

    分位窗口含当前根（与回测口径一致）。历史不足 window 根的位置一律 False。
    """
    n = len(band)
    raw = [False] * n
    for i in range(window - 1, n):
        cur = band[i]
        if cur is None:
            continue
        hist = [band[j] for j in range(i - window + 1, i + 1) if band[j] is not None]
        if len(hist) < window:
            continue
        thr = quantile(hist, q)
        if thr is not None and cur <= thr:
            raw[i] = True

    out = [False] * n
    run = 0
    for i in range(n):
        run = run + 1 if raw[i] else 0
        if run >= min_bars:
            out[i] = True
    return out


def evaluate_entry(symbol: str, bars: Sequence[Bar], ind: Dict[str, List[Optional[float]]],
                   squeeze: Sequence[bool], i: int, entry_price: Optional[float] = None,
                   stop_min: float = STOP_MIN, stop_max: float = STOP_MAX) -> Optional[Signal]:
    """判断第 i 根是否为入场信号（突破 bar）。

    entry_price 传次根开盘价；不传则用第 i 根收盘价（仅用于回放对齐）。
    """
    if i < 1 or i >= len(bars):
        return None
    if not squeeze[i - 1]:
        return None
    ma_max, ma_min, band = ind["ma_max"], ind["ma_min"], ind["band"]
    if ma_max[i - 1] is None or ma_min[i - 1] is None or band[i - 1] is None:
        return None
    if bars[i].close <= ma_max[i - 1]:
        return None

    px = float(entry_price if entry_price is not None else bars[i].close)
    stop = float(ma_min[i])          # 止损 = 突破 bar 自身的 band 下沿（与回测引擎口径一致）
    if px <= 0 or stop >= px:
        return None
    dist = (px - stop) / px
    if not (stop_min <= dist <= stop_max):
        return None
    return Signal(symbol=symbol, bar_index=i, bar_open_ms=bars[i].open_ms, entry_price=px,
                  stop_price=stop, stop_dist=dist, ma_max=float(ma_max[i - 1]),
                  ma_min=float(ma_min[i]), band=float(band[i - 1]))


def exit_triggered(bars: Sequence[Bar], ind: Dict[str, List[Optional[float]]], i: int) -> bool:
    """第 i 根收盘是否触发离场（收盘 < SMA20）。"""
    sma20 = ind["sma20"][i]
    if sma20 is None:
        return False
    return bars[i].close < sma20


def scan(bars: Sequence[Bar], symbol: str = "", start: int = 0) -> List[Signal]:
    """对整段历史扫一遍信号（回放 / 回测用）。"""
    ind = compute_indicators(bars)
    sq = squeeze_flags(ind["band"])
    out: List[Signal] = []
    for i in range(max(start, 1), len(bars) - 1):
        nxt = bars[i + 1].open if i + 1 < len(bars) else None
        s = evaluate_entry(symbol, bars, ind, sq, i, entry_price=nxt)
        if s:
            out.append(s)
    return out
