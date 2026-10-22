#!/usr/bin/env python3
"""一次性迁移：修正统一账户下「浮亏被算两次」的历史快照。

用法（容器内，先 dry-run 再 apply）：

    python -m scripts.fix_equity_doublecount                # 只统计，不写库
    python -m scripts.fix_equity_doublecount --apply        # 备份后修正

安全：`--apply` 会先把库备份到 <db>.bak-YYYYmmdd-HHMMSS，再在单事务里更新；
`equity_fixed` 列保证重复执行不会二次相减。
"""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.hl.equity_fix import fix_double_count, pending_rows   # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("DB_PATH") or "/app/data/crypto_ma.db")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    a = ap.parse_args(argv)

    if not os.path.exists(a.db):
        print(f"DB 不存在：{a.db}")
        return 2
    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(hl_snapshots)")}
    if "equity_fixed" not in cols:
        print("缺 equity_fixed 列：请先让新版进程启动一次（db._migrate 会补列），再跑本脚本。")
        return 3

    todo = pending_rows(conn)
    print(f"待修正快照：{todo} 条")
    if a.apply and todo:
        bak = f"{a.db}.bak-{datetime.datetime.now(datetime.UTC).strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(a.db, bak)
        print(f"已备份：{bak}")

    stats = fix_double_count(conn, apply=a.apply)
    print(f"rows={stats['rows']} applied={stats['applied']} "
          f"delta=[{stats['delta_min']}, {stats['delta_max']}]")
    for e in stats["examples"]:
        t = datetime.datetime.fromtimestamp(e["ts"], datetime.UTC).strftime("%m-%d %H:%M")
        print(f"  {t}  {e['before']:.2f} → {e['after']:.2f}  (upnl {e['upnl']:.2f})")
    if not a.apply:
        print("（dry-run，未写库；加 --apply 执行）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
