"""定位实盘引擎与回测引擎的信号差异：逐条件比对某个 bar。"""
import sys
sys.path.insert(0, "/opt/data/cache/scratch/crypto-ma-alert")
sys.path.insert(0, "/opt/data/cache/scratch/ma_backtest")
import pandas as pd
from app.strategy import Bar, compute_indicators, squeeze_flags, evaluate_entry
from engine import squeeze_pct_flags
from scripts.xvalidate import fetch_mexc, to_df

bars = fetch_mexc("ETH_USDT")
ind = compute_indicators(bars)
sq_live = squeeze_flags(ind["band"])
df = to_df(bars)
sq_bt = squeeze_pct_flags(df, 0.2, 250, 6)
off = len(bars) - len(df)
print("bars", len(bars), "df", len(df), "offset", off)

# 找出实盘有信号、回测没有的 bar
mismatch = []
for i in range(120, len(bars) - 1):
    s = evaluate_entry("ETH_USDT", bars, ind, sq_live, i, entry_price=bars[i + 1].open)
    if not s:
        continue
    j = i - off
    bt_has = False
    if 1 <= j < len(df) - 1:
        c = df["close"].to_numpy(); mx = df["ma_max"].to_numpy()
        if sq_bt[j - 1] and c[j] > mx[j - 1]:
            entry = df["open"].to_numpy()[j + 1]
            d = abs(entry - df["ma_min"].to_numpy()[j]) / entry
            bt_has = 0.015 <= d <= 0.10
    if not bt_has:
        mismatch.append(i)
print("实盘有信号但回测没有的 bar 数:", len(mismatch))
for i in mismatch[:4]:
    j = i - off
    print(f"\n--- bar {pd.to_datetime(bars[i].open_ms, unit='ms')} (i={i}, j={j}) ---")
    print(f"  live: sq[i-1]={sq_live[i-1]}  close={bars[i].close:.2f} ma_max[i-1]={ind['ma_max'][i-1]:.2f}"
          f"  band[i-1]={ind['band'][i-1]:.6f} ma_min[i]={ind['ma_min'][i]:.2f}"
          f"  entry={bars[i+1].open:.2f} dist={(bars[i+1].open-ind['ma_min'][i])/bars[i+1].open:.4f}")
    print(f"  bt  : sq[j-1]={bool(sq_bt[j-1])}  close={df['close'].iloc[j]:.2f} ma_max[j-1]={df['ma_max'].iloc[j-1]:.2f}"
          f"  band[j-1]={df['band'].iloc[j-1]:.6f} ma_min[j]={df['ma_min'].iloc[j]:.2f}"
          f"  entry={df['open'].iloc[j+1]:.2f} dist={(df['open'].iloc[j+1]-df['ma_min'].iloc[j])/df['open'].iloc[j+1]:.4f}")
    # 再往前找：实盘判定用的 sq[i-1] 与回测的 sq[j-1] 是否同一根
    print(f"  sq 序列对齐检查: live[i-2..i]={[bool(x) for x in sq_live[i-2:i+1]]} "
          f"bt[j-2..j]={[bool(x) for x in sq_bt[j-2:j+1]]}")
