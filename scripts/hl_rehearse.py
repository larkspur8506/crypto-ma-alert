#!/usr/bin/env python3
"""HL 执行器彩排：合成信号走全链路，验证「事件会被产生、文案长什么样」。

背景（2026-10-14 审计三轮）：那天的彩排是临时脚本直接拼 `engine.tick(真 notifier)`，
两条合成 ETH 信号**真实推到了生产通道**（用户收到假「开多信号」+ 假「补报平仓」，
链接还是死链）。本脚本把彩排固化下来，杜绝再犯：

- 提醒一律走 stdout（`StdoutNotifier`；**绝不 import app.main 的 notifier**，不发网络推送）
- 落库用独立临时 DB（**不碰生产 crypto_ma.db**）
- 行情用合成 K 线（monkeypatch `app.market.fetch`，**零网络**；脚本先自检合成序列能产生信号）
- `executor=None` —— **不下单、不碰交易所**（只验证信号 → 事件 → 提醒这条链路）
- 不验证推送通道（通道测试属于上线检查，用 /admin 手动 tick）

跑法（容器内）：
    cat scripts/hl_rehearse.py | sudo -n docker exec -i crypto-ma-alert python -
本地（仓库根目录、装好依赖）：
    python scripts/hl_rehearse.py

退出码：0 = 开仓/离场两条提醒都渲染成功；2 = 合成序列没能产生信号（脚本要修）；
3 = 链路跑通但提醒不完整。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import engine, market, strategy                    # noqa: E402
from app.config import Config                               # noqa: E402
from app.db import DB                                       # noqa: E402

INTERVAL_MS = market.INTERVAL_MS
SYMBOL = "BTC_USDT"
BASE = 100.0
QUIET_BARS = 12            # 收窄段长度（squeeze 至少连续 6 根）
WIDE_BARS = 408            # 宽幅段：把 band 宽度分位阈值抬上去


class StdoutNotifier:
    """彩排专用：只打印，绝不推送。"""

    def __init__(self):
        self.messages: list = []

    def send_message(self, text):
        self.messages.append(text)
        print("----- [彩排·仅打印，未发网络] -----", flush=True)
        print(text, flush=True)
        return True


def synthetic_bars(kind: str = "entry"):
    """合成 K 线：宽幅震荡 → 极窄收敛（squeeze）→ 突破（做多信号）。

    kind="entry"：信号 bar 就是**最后一根已收盘 bar**（其后跟着进行中的 bar），
                  等价于「突破那根刚收盘、引擎此刻 tick」。
    kind="exit" ：在信号 bar 之后再补两根已收盘 bar —— 入场 bar（平）与**大跌 bar**
                  （收盘跌破 SMA20），用于第二轮 tick 触发离场提醒。
    末尾永远带一根**进行中的 bar**（引擎按 `len(bars) - 2` 认已收盘 bar）。
    """
    t0 = 1_760_000_000_000 - (WIDE_BARS + QUIET_BARS + 6) * INTERVAL_MS
    seq = [BASE * (1 + (0.02 if i % 2 == 0 else -0.02)) for i in range(WIDE_BARS)]
    seq += [BASE * (1 + 0.0002 * (j % 2)) for j in range(QUIET_BARS)]
    bars = [strategy.Bar(open_ms=t0 + i * INTERVAL_MS, open=c, high=c * 1.012,
                         low=c * 0.988, close=c, volume=1.0)
            for i, c in enumerate(seq)]
    # 信号 bar：突破前高
    last = bars[-1].close
    brk = last * 1.03
    bars.append(strategy.Bar(open_ms=bars[-1].open_ms + INTERVAL_MS, open=last,
                             high=brk * 1.002, low=last * 0.999, close=brk, volume=2.0))
    if kind == "exit":
        # 入场 bar（次根开盘 = brk 成交，收盘基本平）—— 入场当根只允许止损离场
        flat = brk * 1.001
        bars.append(strategy.Bar(open_ms=bars[-1].open_ms + INTERVAL_MS, open=brk,
                                 high=flat * 1.002, low=brk * 0.998, close=flat, volume=2.0))
        # 大跌 bar：收盘跌破 SMA20（≈100）→ 触发 ma20 离场（远高于止损 100.01 的做多止损位，
        # 所以走的是均线离场而不是止损）
        drop = brk * 0.95
        bars.append(strategy.Bar(open_ms=bars[-1].open_ms + INTERVAL_MS, open=flat,
                                 high=flat * 1.001, low=drop * 0.998, close=drop, volume=2.0))
    # 进行中的 bar（引擎会丢掉）
    tail = bars[-1].close
    bars.append(strategy.Bar(open_ms=bars[-1].open_ms + INTERVAL_MS, open=tail,
                             high=tail * 1.001, low=tail * 0.999, close=tail, volume=0.4))
    return bars


def self_check(bars) -> bool:
    """合成序列必须真的能算出信号 —— 否则这个彩排脚本本身要修。"""
    ind = strategy.compute_indicators(bars)
    sq = strategy.squeeze_flags(ind["band"], 250, 0.2, 6)
    i = len(bars) - 2                                  # 最后一根已收盘 bar = 信号 bar
    sig = strategy.evaluate_entry(SYMBOL, bars, ind, sq, i, allow_long=True,
                                  allow_short=True, short_regime_ma=200)
    if sig is None:
        print("✗ 彩排自检失败：合成 K 线没有产生信号（脚本需要修，不是系统问题）")
        return False
    print(f"· 自检：合成序列在第 {i} 根产生{'做多' if sig.side == 1 else '做空'}信号"
          f"（入场 {sig.entry_price:.2f} / 止损 {sig.stop_price:.2f}）")
    return True


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="cma-rehearse-")
    db_path = os.path.join(tmp, "rehearse.db")
    print(f"= 彩排开始（独立临时库 {db_path}；行情合成；不下单、不推送）=")

    entry_bars = synthetic_bars()
    if not self_check(entry_bars):
        return 2

    db = DB(db_path)
    cfg = Config()
    cfg.symbols = [SYMBOL]
    cfg.dry_run = False                  # 走 notifier 分支（StdoutNotifier，仍不联网）
    notifier = StdoutNotifier()

    # ---- 第 1 轮：突破 bar 收盘 → 应产生开仓提醒 ----
    feed_entry = market.Feed(symbol=SYMBOL, source="rehearse", bars=entry_bars,
                             fetched_at_ms=entry_bars[-1].open_ms)
    market.fetch = lambda symbol, now_ms=None, allow_fallback=False: feed_entry   # noqa: ARG005
    engine.tick(db, cfg, notifier, now_ms=entry_bars[-1].open_ms + 3600_000, executor=None)

    row = db.open_position(SYMBOL)
    if row is None:
        print("✗ 第 1 轮没有产生仓位记录 —— 信号链路没走通")
        return 3
    print(f"· 第 1 轮：产生仓位 #{row['id']}（status={row['status']}，待确认）")

    # 模拟确认（live 下由执行器调 db.confirm_position 自动完成；本脚本直接落库
    # 等价复现同一条写路径 —— 人工确认页已于 2026-10-30 PR B 移除）
    db.confirm_position(int(row["id"]), float(row["entry_price"]), int(row["contracts"]),
                        float(row["stop_price"]), True)
    print("· 模拟用户确认开仓（confirm_position）")

    # ---- 第 2 轮：追加跌破 SMA20 的 bar → 应产生离场提醒 ----
    exit_bars = synthetic_bars("exit")
    feed_exit = market.Feed(symbol=SYMBOL, source="rehearse", bars=exit_bars,
                            fetched_at_ms=exit_bars[-1].open_ms)
    market.fetch = lambda symbol, now_ms=None, allow_fallback=False: feed_exit   # noqa: ARG005
    engine.tick(db, cfg, notifier, now_ms=exit_bars[-1].open_ms + 3600_000, executor=None)

    msgs = notifier.messages
    entry_ok = bool(msgs) and ("开仓" in msgs[0] or "信号" in msgs[0])
    exit_ok = any("离场" in m for m in msgs)
    print()
    print(f"= 彩排结果：渲染提醒 {len(msgs)} 条"
          f"（开仓={'✓' if entry_ok else '✗'} 离场={'✓' if exit_ok else '✗'}）=")
    print("= 未发送任何网络推送；未触碰生产库与交易所 =")
    if entry_ok and exit_ok:
        return 0
    print("✗ 提醒不完整：链路或文案有问题，请人工核对")
    return 3


if __name__ == "__main__":
    sys.exit(main())
