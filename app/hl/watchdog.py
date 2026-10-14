"""HL 执行器看守检查（供 Hermes cron 每 5 分钟调用，异常才输出）。

与 cma_watch.py 分工：那个看「提醒服务」，这个看「执行器 + 交易所侧状态」。
本模块只读：不写库（除 kill switch 的提示不做任何状态变更）、不下单。

用法：
    python -m app.hl.watchdog --dry-run     # 打印所有检查项（含正常的）
    python -m app.hl.watchdog               # 只在有问题时输出（cron 静默模式）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from email.utils import parsedate_to_datetime
from typing import List, Optional

from app.hl import MODE_OFF, EV_HEARTBEAT
from app.hl.info import HlInfoError

STUCK_ORDER_S = 180.0
HEARTBEAT_MAX_S = 15 * 60
CLOCK_SKEW_MAX_S = 2.0
AGENT_WARN_DAYS = 3.0        # agent 授权剩余天数低于此值就告警（到期=无法下单）


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def check_clock_skew(base_url: str = "https://api.hyperliquid.xyz",
                     timeout: float = 10.0) -> Optional[float]:
    """用响应头 Date 估服务器时间偏差（秒）。取不到返回 None。"""
    try:
        req = urllib.request.Request(base_url + "/info",
                                     data=json.dumps({"type": "allMids"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            date_hdr = resp.headers.get("Date")
            resp.read()
        if not date_hdr:
            return None
        server = parsedate_to_datetime(date_hdr).timestamp()
        return time.time() - server
    except Exception:                                                # noqa: BLE001
        return None


def run_checks(db, cfg=None, info=None, mode: str = MODE_OFF, force: bool = False) -> List[str]:
    """返回问题列表（空 = 一切正常）。`force` 时连正常项也以「· 」前缀给出。"""
    notes: List[str] = []

    def add(msg: str, ok: bool = False) -> None:
        if ok and not force:
            return
        notes.append(("· " if ok else "⚠️ ") + msg)

    if mode == MODE_OFF:
        add("执行器处于 off 模式（只提醒）", ok=True)
        return notes

    # 1) 心跳
    last = db.hl_last_event_at(EV_HEARTBEAT)
    if last is None:
        add("执行器从未写过心跳（检查调度器/execute 钩子）")
    else:
        age = time.time() - last
        if age <= HEARTBEAT_MAX_S:
            add(f"执行器心跳 {age / 60:.1f} 分钟前", ok=True)
        else:
            add(f"执行器心跳超期 {age / 60:.1f} 分钟（应 ≤15 分钟）")

    # 2) kill switch
    if db.hl_kill():
        reason = db.get_config_str("hl_exec_kill_reason")
        add(f"kill switch 处于触发状态（{reason}）—— 自动交易已暂停，仅允许人工恢复")
    else:
        add("kill switch 未触发", ok=True)

    # 3) 卡住的意图单（可能已发出但结果未知）
    stuck = db.hl_stuck_orders(STUCK_ORDER_S)
    if stuck:
        detail = "；".join(f"#{r['id']} {r['symbol']}/{r['purpose']}({r['status']})" for r in stuck[:5])
        add(f"有 {len(stuck)} 条卡住的意图单（>3 分钟未回结果）：{detail}")

    # 4) 交易所侧：仓位与止损。
    #    审计三轮 L：地址缺失只跳过交易所侧（4/7/8），不提前 return ——
    #    后面的 24h 失败单、时钟偏差检查与交易所无关，必须照常执行。
    address = _env("HL_ACCOUNT_ADDRESS")
    if info is None or not address:
        add("未配置 HL_ACCOUNT_ADDRESS，跳过交易所侧检查")
    snap = None
    if address and info is not None:
        try:
            snap = info.snapshot(address)
        except HlInfoError as e:
            add(f"读不到 HL 账户：{e}")
    if snap is not None:
        ex_pos = {p["coin"]: p for p in snap.get("positions") or []}
        orders_trusted = bool(snap.get("orders_trusted", False))
        for p in ex_pos.values():
            if not orders_trusted:
                # 挂单接口回落（读不到 isTrigger）：与执行器同口径 —— 只提示、不判裸奔
                add(f"{p['coin']} 仓位 {p['size']} @ {p['entry_px']:,.2f}，止损状态无法确认（挂单接口不可信）")
                continue
            has_stop = any(o.get("coin") == p["coin"] and o.get("reduce_only") and o.get("is_trigger")
                           for o in snap.get("open_orders") or [])
            if not has_stop:
                add(f"{p['coin']} 有仓位但没有止损挂单（裸奔仓位，请立刻处理）")
            else:
                add(f"{p['coin']} 仓位 {p['size']} @ {p['entry_px']:,.2f}，止损单在场", ok=True)
        for r in db.recent_positions(20):
            if (r["hl_status"] or "") == "mismatch":
                add(f"仓位 #{r['id']} {r['symbol']} 处于 mismatch（本地与交易所不一致）")
        local_open = [r for r in db.recent_positions(20)
                      if r["status"] == "open" and (r["hl_status"] or "") == "open"]
        for r in local_open:
            coin = r["symbol"].split("_")[0]
            if coin not in ex_pos:
                add(f"本地显示 {r['symbol']} 持仓中（#{r['id']}），但交易所查不到该仓位")

    # 5) 近 24h 失败单
    since = time.time() - 86400
    failed = [r for r in db.hl_recent_orders(100)
              if r["status"] == "failed" and float(r["created_at"]) >= since]
    if failed:
        add(f"近 24h 有 {len(failed)} 条下单失败（最近：{failed[0]['error'] or '无错误信息'}）")
    else:
        add("近 24h 无失败下单", ok=True)

    # 6) 时钟偏差（nonce 依赖）
    skew = check_clock_skew()
    if skew is None:
        add("时钟偏差检查失败（拿不到服务器时间）")
    elif abs(skew) > CLOCK_SKEW_MAX_S:
        add(f"本机与 HL 服务器时间偏差 {skew:+.1f} 秒（>2s 会导致 nonce 被拒）")
    else:
        add(f"时钟偏差 {skew:+.2f} 秒", ok=True)

    # 7) 权益（统一账户下余额在现货状态里）—— snap 可能因地址缺失/读取失败为 None
    if snap is not None and snap.get("unified"):
        add(f"账户总值 {snap.get('equity_total', 0):,.2f} USDC"
            f"（统一账户：抵押品 {snap.get('spot_usdc', 0):,.2f} 在现货 + 浮动盈亏）", ok=True)
    elif snap is not None:
        add(f"账户权益 {snap.get('equity', 0):,.2f} USDC（可用 {snap.get('withdrawable', 0):,.2f}）",
            ok=True)

    # 8) agent（API 钱包）授权到期 —— 到期后所有下单会被拒，必须提前发现
    if not address or info is None:
        return notes
    try:
        ag = info.agent_status(address)
        agents = ag.get("agents") or []
        until = ag.get("max_valid_until")
        if not agents:
            add("账户没有任何 agent（API 钱包）授权 —— 下单会被拒，"
                "请到 HL 设置里生成 API 钱包并绑定主账户")
        elif until:
            days_left = (float(until) / 1000.0 - time.time()) / 86400.0
            names = "、".join(str(a.get("name") or str(a.get("address"))[:10]) for a in agents[:3])
            if days_left <= AGENT_WARN_DAYS:
                add(f"agent 授权只剩 {days_left:.1f} 天（{names}）"
                    f"—— 到期后自动交易会全部失败，请到 HL 设置里重新生成/延长 API 钱包")
            else:
                add(f"agent 授权 {len(agents)} 个（{names}），最长还有 {days_left:.0f} 天", ok=True)
        else:
            add(f"agent 授权 {len(agents)} 个，均无到期时间", ok=True)
    except Exception as e:                                           # noqa: BLE001
        add(f"读不到 agent 授权状态：{e}")
    return notes


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="打印全部检查项")
    ap.add_argument("--mode", default=None,
                    help="执行模式（默认从 DB 读，与面板/执行器同一口径；env HL_EXEC_MODE 只是兜底）")
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "/app/data/crypto_ma.db"))
    args = ap.parse_args(argv)

    from app.db import DB
    from app.hl.info import MAINNET, TESTNET, HlInfo

    base = TESTNET if _env("HL_TESTNET", "0") in ("1", "true") else MAINNET
    db = DB(args.db)
    # 模式判定必须与 main.hl_mode() 同口径：DB > env > off。
    # 只读 env 的话，面板切到 live 而 env 还是 dry/off 时，看守会在错误的模式下自检
    # （off 直接跳过全部检查 = 实盘无看守）。
    mode = args.mode
    if mode is None:
        raw = str(db.get_config().get("hl_exec_mode", "") or _env("HL_EXEC_MODE", MODE_OFF))
        mode = raw.strip().strip('"')
        if mode not in (MODE_OFF, "dry", "live"):
            mode = MODE_OFF
    info = HlInfo(base_url=base)
    problems = run_checks(db, info=info, mode=mode, force=args.dry_run)
    bad = [p for p in problems if p.startswith("⚠️")]
    if args.dry_run:
        print(f"[dry-run] {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC} 模式={mode}")
        for p in problems:
            print(" ", p)
        return 0
    if bad:
        print("⚠️ HL 执行器看守告警")
        for p in bad:
            body = p[len("⚠️ "):] if p.startswith("⚠️ ") else p
            print("•", body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
