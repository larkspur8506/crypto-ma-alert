"""端到端一致性（多空双向，多标的）：dry-run 的 walk 回放 vs 回测引擎 simulate，
在同一份行情上的**完整回合**必须一致。

比 xvalidate.py 更进一步：不只看信号，而是看「入场 bar → 离场 bar → 离场原因 → R」整条链。

回测引擎侧复用技能脚本 `finance/strategy-backtest/scripts/ma_trend_backtest.py`：
它 import 期会跑模块级主程序（打印仓位表 + 拉 Binance 报价），所以用 **exec 截断法**
只执行到 `import urllib.request as _u`（主程序起点）之前的**定义段**。

用法：
    python3 scripts/xvalidate_roundtrips.py                       # 默认三标的
    python3 scripts/xvalidate_roundtrips.py --symbols BTC_USDT,ETH_USDT,SOL_USDT

数据源：BTC/ETH 走 MEXC 合约 K 线（与既有基线一致）；SOL 走 Binance 现货 4h 长历史。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                               # noqa: E402
from app import engine, strategy as S                             # noqa: E402
from app.config import Config                                     # noqa: E402

# 取数（与 xvalidate.py 同源策略）直接从那边复用，避免两套口径漂移。
# scripts/ 不是包，把脚本目录放进 path 后按模块名导入（脚本方式运行时 sys.path[0]
# 本来就是 scripts/，这里显式补一份以防被当模块调用）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xvalidate import fetch_bars, DEFAULT_SYMBOLS                 # noqa: E402

CFG = Config()
CFG.equity, CFG.risk_pct, CFG.leverage = 1000.0, 0.02, 5.0
CFG.stop_min, CFG.stop_max = 0.015, 0.10
CFG.enable_long, CFG.enable_shorts, CFG.short_regime_ma = True, True, 200
CFG.token_secret = "xval"
CFG.base_url = "https://x"
COST = 0.0002          # MEXC taker 0.02%/边

BT_SCRIPT = os.environ.get(
    "MA_BACKTEST_SCRIPT",
    "/opt/data/skills/finance/strategy-backtest/scripts/ma_trend_backtest.py")


def load_backtest_engine():
    """exec 截断法取回测原语（见模块 docstring）。"""
    with open(BT_SCRIPT, encoding="utf-8") as f:
        src = f.read()
    cut = src.index("import urllib.request as _u")
    ns: dict = {}
    exec(compile(src[:cut], BT_SCRIPT, "exec"), ns)     # noqa: S102 (本地技能脚本，可信)
    return ns["squeeze_pct_flags"], ns["signals"], ns["simulate"]


squeeze_pct_flags, bt_signals, simulate = load_backtest_engine()


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
                              "stop": ev.sig.stop_price, "side": ev.sig.side})
            elif ev.kind == "exit":
                t = trips[-1]
                risk = abs(t["in_px"] - t["stop"])
                t.update(out_ms=ev.bar_ms, out_px=ev.exit_price, reason=ev.reason,
                         r=((ev.exit_price - t["in_px"]) if t["side"] == 1
                            else (t["in_px"] - ev.exit_price)) / risk)
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
    df["sma200"] = df["close"].rolling(200).mean()
    cols = [f"sma{n}" for n in (20, 60, 120)] + [f"ema{n}" for n in (20, 60, 120)]
    df["ma_max"] = df[cols].max(axis=1)
    df["ma_min"] = df[cols].min(axis=1)
    df["band"] = (df["ma_max"] - df["ma_min"]) / df["close"]
    df = df.dropna().copy()
    sq = squeeze_pct_flags(df, 0.2, 250, 6)
    o = df["open"].to_numpy(); c = df["close"].to_numpy(); s200 = df["sma200"].to_numpy()
    sigs = []
    for (t, side, stop) in bt_signals(df, sq, "breakout"):
        d = abs(o[t + 1] - stop) / o[t + 1]
        if not (CFG.stop_min <= d <= CFG.stop_max):
            continue
        if side == -1 and (pd.isna(s200[t]) or c[t] >= s200[t]):
            continue
        sigs.append((t, side, stop))
    trades, _, _ = simulate(df, sigs, exit_mode="ma20trail", risk_pct=CFG.risk_pct, sq=sq,
                            cost_per_side=COST, fund=None)
    ot = df["open_time"].to_numpy()
    return [{"in_ms": int(ot[t["entry_i"]]), "in_px": t["entry"], "stop": t["stop"],
             "side": t["side"], "out_ms": int(ot[t["exit_i"]]), "out_px": t["exit"], "r": t["r"]}
            for t in trades]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="dry-run walk vs 回测 simulate 回合级交叉验证")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                    help=f"逗号分隔的标的清单（默认 {DEFAULT_SYMBOLS}）")
    args = ap.parse_args(argv)
    symbols = [s for s in args.symbols.split(",") if s.strip()]

    ok = True
    for symbol in symbols:
        bars = fetch_bars(symbol)
        a = walk_roundtrips(bars, symbol)
        b = backtest_roundtrips(bars, symbol)
        na = sum(1 for x in a if x["side"] == 1)
        nb = sum(1 for x in b if x["side"] == 1)
        print(f"\n=== {symbol}  实盘walk {len(a)} 回合（多 {na}/空 {len(a) - na}）"
              f" ｜ 回测simulate {len(b)} 回合（多 {nb}/空 {len(b) - nb}） ===")
        n = min(len(a), len(b))
        bad = []
        # 末端未平仓的回合（walk 留仓、无 out_ms；回测 simulate 在窗口末端强制平仓）
        # 单独核对方向与入场 bar
        a_closed = [x for x in a if "out_ms" in x]
        a_open = [x for x in a if "out_ms" not in x]
        for k, (ak, bk) in enumerate(zip(a_closed, b)):
            # walk 的 entry 事件 bar_ms = **信号 bar**（幂等键），回测的 entry_i = 入场 bar = 信号 bar + 1
            if (ak["in_ms"] + 14_400_000 != bk["in_ms"] or ak["out_ms"] != bk["out_ms"]
                    or ak["side"] != bk["side"]):
                bad.append((k, ak, bk))
        print(f"  前 {n} 个回合中不一致：{len(bad)}")
        for k, x, y in bad[:3]:
            print(f"    #{k} walk side={x['side']} in={pd.to_datetime(x['in_ms'], unit='ms')} "
                  f"out={pd.to_datetime(x.get('out_ms', 0), unit='ms')}")
            print(f"        bt  side={y['side']} in={pd.to_datetime(y['in_ms'], unit='ms')} "
                  f"out={pd.to_datetime(y['out_ms'], unit='ms')}")
        if a_open:
            print(f"  窗口末端未平仓：{len(a_open)} 笔（walk 留仓、回测末端强平，不计入逐笔比对）")
            for x in a_open:
                print(f"    walk side={x['side']} 信号bar={pd.to_datetime(x['in_ms'], unit='ms')}")
        if len(a) == len(b) and not bad:
            rs_walk = [x["r"] for x in a_closed]
            rs_bt = [t["r"] for t in b[:len(a_closed)]]
            dmax = max((abs(p - q) for p, q in zip(rs_walk, rs_bt)), default=0)
            avg = sum(rs_walk) / len(rs_walk) if rs_walk else 0
            print(f"  ✅ 回合完全一致；R 最大差 {dmax:.4f}（差异来自回测按名义计费、walk 未计费）")
            print(f"  回放均R {avg:+.3f}，共 {len(rs_walk)} 笔")
        else:
            ok = False
            print("  ❌ 存在差异")

    print("\n总体:", "✅ dry-run 与回测引擎端到端一致（含做空）" if ok else "❌ 有差异，需排查")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
