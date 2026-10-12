"""dry-run：用**线上同一份 walk 代码**回放最近 N 天，打印会推什么（不发送、不写库）。

用法：
    python3 scripts/dry_run.py            # 默认 90 天
    python3 scripts/dry_run.py --days 30
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market, strategy as S          # noqa: E402
from app.config import Config                          # noqa: E402


def replay(symbol: str, bars, cfg: Config, days: int):
    """逐根回放：把每根已收盘 bar 都当成「当时刚收盘」，与线上 tick 的行为一致。"""
    ind = S.compute_indicators(bars)
    sq = S.squeeze_flags(ind["band"], cfg.band_window, cfg.band_quantile, cfg.min_squeeze_bars)
    cutoff_ms = bars[-1].open_ms - days * 86400 * 1000
    start_i = next((i for i, b in enumerate(bars) if b.open_ms >= cutoff_ms), len(bars) - 2)
    pos = None
    out = []
    for i in range(start_i, len(bars) - 1):
        events, pos = engine.walk(cfg, symbol, bars, ind, sq, pos, i, i, i)
        out.extend(events)
    return out, pos, start_i


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    cfg = Config()
    cfg.base_url = cfg.base_url or "https://ma.222677.xyz"
    print(f"账户权益 {cfg.equity:.0f}U ｜ 单笔风险 {cfg.risk_pct * 100:.1f}% ｜ 杠杆 {cfg.leverage:.0f}x ｜ "
          f"止损过滤 {cfg.stop_min * 100:.1f}%~{cfg.stop_max * 100:.1f}%")
    print(f"回放窗口：最近 {args.days} 天\n")

    total = {"entry": 0, "exit": 0, "entry_missed": 0}
    for symbol in cfg.symbols:
        try:
            feed = market.fetch(symbol)
        except market.FeedError as e:
            print(f"❌ {symbol} 取数失败：{e}")
            continue
        bars = feed.bars
        events, pos, start_i = replay(symbol, bars, cfg, args.days)
        span0 = time.strftime("%m-%d %H:%M", time.gmtime(bars[start_i].open_ms / 1000))
        span1 = time.strftime("%m-%d %H:%M", time.gmtime(bars[-2].open_ms / 1000))
        print("=" * 100)
        print(f"{symbol}  数据源={feed.source}  {len(bars)} 根  回放 {span0} → {span1} UTC")
        print("=" * 100)
        for ev in events:
            total[ev.kind] = total.get(ev.kind, 0) + 1
            stamp = time.strftime("%m-%d %H:%M", time.gmtime(ev.bar_ms / 1000))
            tag = {"entry": "🟢 入场", "exit": "🔴 离场", "entry_missed": "⚪️ 错过"}[ev.kind]
            print(f"\n[{tag}] {stamp} UTC  ({'最新 bar → 可交易' if ev.is_latest else '历史 bar'})")
            if ev.kind == "entry":
                print(engine.format_entry(cfg, ev.sig, ev.order, 0))
            elif ev.kind == "exit":
                pos_txt = (f"入场 {ev.exit_price:,.2f} 离场，持仓 {ev.bars_held * 4}h，"
                           f"原因 {ev.reason}")
                print(f"  离场参考价 {ev.exit_price:,.2f}（{ev.reason}），持仓约 {ev.bars_held * 4} 小时")
            else:
                print(engine.format_missed(cfg, ev.sig))
        if pos is not None:
            print(f"\n⚠️ 回放结束时仍有未平仓持仓：入场 {pos.entry_price:,.2f} 止损 {pos.stop_price:,.2f}")
        print(f"\n小计：入场 {sum(1 for e in events if e.kind == 'entry')} ｜ "
              f"离场 {sum(1 for e in events if e.kind == 'exit')} ｜ "
              f"错过 {sum(1 for e in events if e.kind == 'entry_missed')}\n")

    print("=" * 100)
    print(f"合计：入场 {total.get('entry', 0)} ｜ 离场 {total.get('exit', 0)} ｜ 错过 {total.get('entry_missed', 0)}")
    print("=" * 100)


if __name__ == "__main__":
    main()
