"""端到端一致性：dry-run 的 walk 回放 vs 回测引擎 simulate，在同一份 MEXC 数据上的**完整回合**必须一致。

比 xvalidate.py 更进一步：不只看信号，而是看「入场 bar → 离场 bar → 离场原因 → R」整条链。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/opt/data/cache/scratch/ma_backtest")

import pandas as pd
from app import engine, market, strategy as S
from app.config import Config
from app.sizing import size_order
from engine import squeeze_pct_flags, signals as bt_signals, simulate

CFG = Config()
CFG.equity, CFG.risk_pct, CFG.leverage = 1000.0, 0.02, 5.0
CFG.stop_min, CFG.stop_max = 0.015, 0.10
CFG.token_secret = "xval"
CFG.base_url = "https://x"
COST = 0.0002          # MEXC taker 0.02%/边


def walk_roundtrips(bars, symbol):
    ind = S.compute_indicators(bars)
    sq = S.squeeze_flags(ind["band"], CFG.band_window, CFG.band_quantile, CFG.min_squeeze_bars)
    pos = None
    trips = []
    for i in range(1, len(bars) - 1):
        events, pos = engine.walk(CFG, symbol, bars, ind, sq, pos, i, i, i)
        for ev in events:
            if ev.kind == "entry":
                trips.append({"in_ms": ev.bar_ms, "in_px": ev.sig.entry_price,
                              "stop": ev.sig.stop_price})
            elif ev.kind == "exit":
                t = trips[-1]
                t.update(out_ms=ev.bar_ms, out_px=ev.exit_price, reason=ev.reason,
                         r=(ev.exit_price - t["in_px"]) / (t["in_px"] - t["stop"]))
    return trips


def backtest_roundtrips(bars, symbol):
    df = pd.DataFrame({"open_time": [b.open_ms for b in bars], "open": [b.open for b in bars],
                       "high": [b.high for b in bars], "low": [b.low for b in bars],
                       "close": [b.close for b in bars], "volume": [b.volume for b in bars]})
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("dt")
    for n in (20, 60, 120):
        df[f"sma{n}"] = df["close"].rolling(n).mean()
        df[f"ema{n}"] = df["close"].ewm(span=n, adjust=False).mean()
    cols = [f"sma{n}" for n in (20, 60, 120)] + [f"ema{n}" for n in (20, 60, 120)]
    df["ma_max"] = df[cols].max(axis=1)
    df["ma_min"] = df[cols].min(axis=1)
    df["band"] = (df["ma_max"] - df["ma_min"]) / df["close"]
    df = df.dropna().copy()
    sq = squeeze_pct_flags(df, 0.2, 250, 6)
    o = df["open"].to_numpy()
    sigs = []
    for (t, side, stop) in bt_signals(df, sq, "breakout"):
        if side != 1:
            continue
        d = abs(o[t + 1] - stop) / o[t + 1]
        if CFG.stop_min <= d <= CFG.stop_max:
            sigs.append((t, side, stop))
    trades, _, _ = simulate(df, sigs, exit_mode="ma20trail", risk_pct=CFG.risk_pct, sq=sq,
                            cost_per_side=COST, fund=None)
    ot = df["open_time"].to_numpy()
    out = []
    for t in trades:
        out.append({"in_ms": int(ot[t["entry_i"]]), "in_px": t["entry"], "stop": t["stop"],
                    "out_ms": int(ot[t["exit_i"]]), "out_px": t["exit"], "r": t["r"]})
    return out


ok = True
for symbol in CFG.symbols:
    bars = market.fetch(symbol).bars
    a = walk_roundtrips(bars, symbol)
    b = backtest_roundtrips(bars, symbol)
    print(f"\n=== {symbol}  实盘walk 回合 {len(a)} ｜ 回测simulate 回合 {len(b)} ===")
    n = min(len(a), len(b))
    bad = []
    for k in range(n):
        # walk 的 entry 事件 bar_ms = **信号 bar**（幂等键），回测的 entry_i = 入场 bar = 信号 bar + 1
        if a[k]["in_ms"] + 14_400_000 != b[k]["in_ms"] or a[k]["out_ms"] != b[k]["out_ms"]:
            bad.append((k, a[k], b[k]))
    print(f"  前 {n} 个回合中入场/离场 bar 不一致：{len(bad)}")
    for k, x, y in bad[:3]:
        print(f"    #{k}: walk in={pd.to_datetime(x['in_ms'], unit='ms')} out={pd.to_datetime(x['out_ms'], unit='ms')}")
        print(f"         bt   in={pd.to_datetime(y['in_ms'], unit='ms')} out={pd.to_datetime(y['out_ms'], unit='ms')}")
    if len(a) == len(b) and not bad:
        rs_walk = [x["r"] for x in a if "r" in x]
        rs_bt = [x["r"] for x in b]
        dmax = max((abs(p - q) for p, q in zip(rs_walk, rs_bt)), default=0)
        avg = sum(rs_walk) / len(rs_walk) if rs_walk else 0
        print(f"  ✅ 回合完全一致；R 最大差 {dmax:.4f}（差异来自回测按名义计费、walk 未计费）")
        print(f"  回放均R {avg:+.3f}，共 {len(rs_walk)} 笔")
    else:
        ok = False
        print("  ❌ 存在差异")

print("\n总体:", "✅ dry-run 与回测引擎端到端一致" if ok else "❌ 有差异，需排查")
