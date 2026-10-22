"""一次性修正：统一账户下历史快照把浮动盈亏算了两遍（equity = 现货 + upnl）。

背景（2026-10-22 对账）：HL 统一账户的现货 USDC 余额**已经包含**持仓浮动盈亏，
交易所 `portfolio.accountValueHistory` 与 `spot_usdc` 一致（两次样本：
06:55 spot 988.11 / upnl −12.86 / 交易所 ≈988.5；12:20 spot 986.46 / upnl −0.50 / 交易所 986.28）。
旧代码 `equity_total = spot_usdc + upnl` 于是把浮亏算了两次，存量快照里的
`hl_snapshots.equity` 全部多了一份 `unrealized_pnl`。

修正 = `equity - unrealized_pnl`；`unrealized_pnl` 列原样保留（历史事实不改写）。
用 `equity_fixed` 列做幂等标记：改过的行置 1，重跑不会二次相减。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

_WHERE = ("equity_fixed = 0 AND unrealized_pnl IS NOT NULL AND ABS(unrealized_pnl) > 1e-12")


def pending_rows(conn) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM hl_snapshots WHERE {_WHERE}").fetchone()[0])


def fix_double_count(conn, *, apply: bool = False) -> Dict[str, Any]:
    """把 `equity` 里多算的那份 upnl 扣回去。

    返回 {rows, applied, delta_min, delta_max, examples}；`apply=False` 时只统计不写库。
    `delta` = 修正量（= −upnl），用来核对口径方向（浮亏行的 delta 为正）。
    """
    rows = conn.execute(
        f"SELECT id, ts, equity, unrealized_pnl FROM hl_snapshots WHERE {_WHERE} ORDER BY ts"
    ).fetchall()
    if not rows:
        return {"rows": 0, "applied": False, "delta_min": 0.0, "delta_max": 0.0, "examples": []}
    deltas = [float(-r["unrealized_pnl"]) for r in rows]
    examples = [
        {"ts": float(r["ts"]), "before": round(float(r["equity"]), 6),
         "after": round(float(r["equity"]) - float(r["unrealized_pnl"]), 6),
         "upnl": round(float(r["unrealized_pnl"]), 6)}
        for r in rows[:3]
    ]
    stats = {
        "rows": len(rows),
        "applied": False,
        "delta_min": round(min(deltas), 6),
        "delta_max": round(max(deltas), 6),
        "examples": examples,
    }
    if not apply:
        return stats
    conn.execute(
        "UPDATE hl_snapshots SET equity = ROUND(equity - unrealized_pnl, 6), equity_fixed = 1 "
        f"WHERE {_WHERE}"
    )
    conn.commit()
    stats["applied"] = True
    logger.info("[equity-fix] 已修正 %d 条快照（equity -= unrealized_pnl）", len(rows))
    return stats
