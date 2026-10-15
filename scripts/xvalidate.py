"""交叉验证（多空双向，多标的）：app/strategy.py（实盘用）vs 回测引擎（pandas，已验证）
在同一份行情上的信号集合。必须逐笔完全一致（bar 时间 / 方向 / 止损价），否则实盘与回测
口径不一致，回测结论全部作废。

回测引擎侧复用技能脚本 `finance/strategy-backtest/scripts/ma_trend_backtest.py`：
它 import 期会跑模块级主程序（打印仓位表 + 拉 Binance 报价），所以用 **exec 截断法**
只执行到 `import urllib.request as _u`（主程序起点）之前的**定义段**，拿到
`squeeze_pct_flags / signals` 而不触发任何网络。

用法：
    python3 scripts/xvalidate.py                                  # 默认三标的
    python3 scripts/xvalidate.py --symbols BTC_USDT,ETH_USDT,SOL_USDT

数据源：BTC/ETH 走 MEXC 合约 K 线（与既有基线一致）；SOL 走 Binance 现货 4h
（MEXC SOL 合约只有约 333 天历史，Binance 现货自 2020-08-11 上市，样本更长）。
两个引擎看的是**同一份 bars**，所以源不影响一致性结论。
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                               # noqa: E402
from app.strategy import Bar, scan, compute_indicators            # noqa: E402

STOP_MIN, STOP_MAX = 0.015, 0.10
REGIME_MA = 200
DEFAULT_SYMBOLS = "BTC_USDT,ETH_USDT,SOL_USDT"

BT_SCRIPT = os.environ.get(
    "MA_BACKTEST_SCRIPT",
    "/opt/data/skills/finance/strategy-backtest/scripts/ma_trend_backtest.py")


def load_backtest_engine():
    """exec 截断法取回测原语（见模块 docstring）。"""
    with open(BT_SCRIPT, encoding="utf-8") as f:
        src = f.read()
    cut = src.index("import urllib.request as _u")     # 主程序起点；截断掉网络调用
    ns: dict = {}
    exec(compile(src[:cut], BT_SCRIPT, "exec"), ns)    # noqa: S102 (本地技能脚本，可信)
    return ns["squeeze_pct_flags"], ns["signals"]


squeeze_pct_flags, bt_signals = load_backtest_engine()


# ---------------------------------------------------------------- 取数
def _bars_from_mexc_rows(k):
    return [Bar(open_ms=int(k["time"][i]) * 1000, open=float(k["open"][i]), high=float(k["high"][i]),
                low=float(k["low"][i]), close=float(k["close"][i]), volume=float(k["vol"][i]))
            for i in range(len(k["time"]))]


def fetch_mexc(symbol):
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?interval=Hour4"
    d = json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "xval"}), timeout=30))
    return _bars_from_mexc_rows(d["data"])


def _binance_spot_rows(spot, start_ms=0):
    """Binance 现货 4h 全历史分页（单次上限 1000 根）。用 data-api.binance.vision
    （公开数据镜像，不受美国 IP 的 451 限制）。

    注意**必须显式带 startTime**（首轮 startTime=0 才从上市首根开始返回）；
    不带时 Binance 只给最近 1000 根，循环立刻因「无更新」退出，样本会被静默截短。
    """
    out, cur = [], start_ms
    while True:
        url = (f"https://data-api.binance.vision/api/v3/klines?symbol={spot}"
               f"&interval=4h&limit=1000&startTime={cur}")
        rows = json.load(urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "xval"}), timeout=30))
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 1000:
            break
        cur = int(rows[-1][0]) + 1
    return out


def fetch_binance_spot(symbol):
    spot = symbol.replace("_", "")
    return [Bar(open_ms=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5])) for r in _binance_spot_rows(spot)]


# SOL 用 Binance 现货长历史；其余走 MEXC 合约（与既有基线一致）
OVERRIDE_SOURCE = {"SOL_USDT": fetch_binance_spot}


def fetch_bars(symbol):
    return OVERRIDE_SOURCE.get(symbol, fetch_mexc)(symbol)


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="实盘 vs 回测引擎 信号级交叉验证")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                    help=f"逗号分隔的标的清单（默认 {DEFAULT_SYMBOLS}）")
    args = ap.parse_args(argv)
    symbols = [s for s in args.symbols.split(",") if s.strip()]

    ok_all = True
    for sym in symbols:
        bars = fetch_bars(sym)
        print(f"\n=== {sym}  {len(bars)} 根  "
              f"{pd.to_datetime(bars[0].open_ms, unit='ms')} → "
              f"{pd.to_datetime(bars[-1].open_ms, unit='ms')} ===")

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
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
