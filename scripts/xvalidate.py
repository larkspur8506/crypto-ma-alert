"""交叉验证（多空双向）：app/strategy.py（实盘用）vs 回测引擎（pandas，已验证）在同一份 MEXC 数据上的信号集合。

必须逐笔完全一致（bar 时间 / 方向 / 止损价），否则实盘与回测口径不一致，回测结论全部作废。
做空口径：跌破 band 下沿 + 收盘 < SMA200（趋势过滤）。
"""
import sys, json, urllib.request
sys.path.insert(0, "/opt/data/cache/scratch/crypto-ma-alert")
sys.path.insert(0, "/opt/data/cache/scratch/ma_backtest")

import pandas as pd
from app.strategy import Bar, scan, compute_indicators
from engine import squeeze_pct_flags, signals as bt_signals

STOP_MIN, STOP_MAX = 0.015, 0.10
REGIME_MA = 200


def fetch_mexc(symbol):
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?interval=Hour4"
    d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "xval"}), timeout=20))
    k = d["data"]
    return [Bar(open_ms=int(k["time"][i]) * 1000, open=float(k["open"][i]), high=float(k["high"][i]),
                low=float(k["low"][i]), close=float(k["close"][i]), volume=float(k["vol"][i]))
            for i in range(len(k["time"]))]


def to_df(bars):
    df = pd.DataFrame({
        "open_time": [b.open_ms for b in bars], "open": [b.open for b in bars],
        "high": [b.high for b in bars], "low": [b.low for b in bars],
        "close": [b.close for b in bars], "volume": [b.volume for b in bars]})
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("dt")
    for n in (20, 60, 120):
        df[f"sma{n}"] = df["close"].rolling(n).mean()
        df[f"ema{n}"] = df["close"].ewm(span=n, adjust=False).mean()
    df["sma200"] = df["close"].rolling(REGIME_MA).mean()
    cols = [f"sma{n}" for n in (20, 60, 120)] + [f"ema{n}" for n in (20, 60, 120)]
    df["ma_max"] = df[cols].max(axis=1)
    df["ma_min"] = df[cols].min(axis=1)
    df["band"] = (df["ma_max"] - df["ma_min"]) / df["close"]
    return df.dropna().copy()


ok_all = True
for sym in ["BTC_USDT", "ETH_USDT"]:
    bars = fetch_mexc(sym)
    print(f"\n=== {sym}  {len(bars)} 根  "
          f"{pd.to_datetime(bars[0].open_ms, unit='ms')} → {pd.to_datetime(bars[-1].open_ms, unit='ms')} ===")

    # A) 实盘引擎（多空双向）
    live = scan(bars, symbol=sym, sides=(1, -1))
    live_map = {(s.bar_open_ms, s.side): (s.entry_price, s.stop_price) for s in live}
    nl = sum(1 for s in live if s.side == 1)
    print(f"  实盘引擎：多 {nl} 笔 / 空 {len(live) - nl} 笔")

    # B) 回测引擎（同一份数据）
    df = to_df(bars)
    sq = squeeze_pct_flags(df, 0.2, 250, 6)
    o = df["open"].to_numpy(); c = df["close"].to_numpy()
    s200 = df["sma200"].to_numpy(); idx = df["open_time"].to_numpy()
    bt_map = {}
    for (t, side, stop) in bt_signals(df, sq, "breakout"):
        entry = o[t + 1]
        d = abs(entry - stop) / entry
        if not (STOP_MIN <= d <= STOP_MAX):
            continue
        if side == -1 and (pd.isna(s200[t]) or c[t] >= s200[t]):
            continue
        bt_map[(int(idx[t]), side)] = (float(entry), float(stop))
    nl = sum(1 for k in bt_map if k[1] == 1)
    print(f"  回测引擎：多 {nl} 笔 / 空 {len(bt_map) - nl} 笔")

    only_live = set(live_map) - set(bt_map)
    only_bt = set(bt_map) - set(live_map)
    print(f"  仅实盘有: {len(only_live)}  仅回测有: {len(only_bt)}")
    for k in sorted(only_live)[:3]:
        print("    LIVE-ONLY", pd.to_datetime(k[0], unit="ms"), "side", k[1])
    for k in sorted(only_bt)[:3]:
        print("    BT-ONLY  ", pd.to_datetime(k[0], unit="ms"), "side", k[1])
    common = set(live_map) & set(bt_map)
    de = max((abs(live_map[k][0] - bt_map[k][0]) for k in common), default=0.0)
    ds = max((abs(live_map[k][1] - bt_map[k][1]) for k in common), default=0.0)
    print(f"  共同信号 {len(common)} 笔；入场价最大差 {de:.2e}，止损价最大差 {ds:.2e}")
    same = not only_live and not only_bt and de < 1e-6 and ds < 1e-6
    print(f"  → {'✅ 完全一致' if same else '❌ 不一致'}")
    ok_all &= same

    ind = compute_indicators(bars)
    diffs = []
    for i in range(200, len(bars)):
        if ind["band"][i] is None or ind["sma200"][i] is None:
            continue
        j = df.index.get_loc(pd.to_datetime(bars[i].open_ms, unit="ms"))
        diffs.append(max(abs(ind["band"][i] - float(df["band"].iloc[j])),
                         abs(ind["sma200"][i] - float(df["sma200"].iloc[j]))))
    print(f"  band/sma200 最大绝对差 = {max(diffs):.3e}")

print("\n总体:", "✅ 实盘引擎与回测引擎口径一致（含做空）" if ok_all else "❌ 存在差异，必须先修")
