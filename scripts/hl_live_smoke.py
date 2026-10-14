"""HL 写路径真单冒烟（**会下真单**，动的是真钱 —— 只在切 live 前/改动写路径后手工跑）。

跑法（在 oracle1 上，脚本从 stdin 进容器）：
    cat scripts/hl_live_smoke.py | ssh oracle1 'sudo -n docker exec -i crypto-ma-alert python -'

顺序建议：先把模式切 off（避免 5 分钟一轮的看守把这张「本地无记录」的仓判成人工单 → 拉 kill），
跑完再切回 dry/live。

规模：约 12 USDC 名义（交易所下限 10U），开仓 → 挂止损 → 核对快照 → 撤止损 → 平仓，全程约 5 秒，
成本 ≈ 0.01 USDC（taker 0.045% × 2）。任何异常都会走 finally 平仓 + 撤残留挂单。

2026-10-14 首跑即刻抓到两个真 bug（单测用假 SDK 全绿也测不出来）：
  1. cloid 传裸字符串 → SDK 内部 'str' object has no attribute 'to_raw'，下单直接失败
  2. openOrders 对触发单不返回 isTrigger/triggerPx → 止损单被判成裸奔（会拉 kill switch）
"""
"""HL 写路径冒烟：开 ~12 USDC 名义 → 设杠杆 → 挂止损 → 核对快照 → 撤止损 → 平仓 → 核对归零。

在容器内跑（私钥从 env 读，绝不打印）。任何一句异常都会走 finally 把仓位平掉 + 撤残留挂单。
"""
import json
import os
import time

from app.hl import sizing as hl_sizing
from app.hl.exchange import HlExchange, make_cloid
from app.hl.info import HlInfo

COIN, SYM, TARGET_NTL = "BTC", "BTC_USDT", 12.0
ADDR = os.environ["HL_ACCOUNT_ADDRESS"]


def log(*a):
    print(" ".join(str(x) for x in a), flush=True)


info = HlInfo()
ex = HlExchange(account_address=ADDR, private_key=os.environ.get("HL_PRIVATE_KEY", ""),
                base_url="https://api.hyperliquid.xyz", dry_run=False,
                max_slippage_pct=0.003, info=info)
log("① 写通道:", "可用" if ex.available else f"不可用（{ex.init_error}）")
if not ex.available:
    raise SystemExit(1)

specs = info.specs([COIN])[COIN]
sz_dec = int(specs["sz_decimals"])
mid = ex.mid(COIN)
sz = hl_sizing.round_size(TARGET_NTL / mid, sz_dec)
ntl = sz * mid
log(f"② 规格 szDecimals={sz_dec} 最大杠杆={specs['max_leverage']}x | mid={mid:,.2f} | "
    f"计划 {sz} {COIN} = {ntl:.2f}U")
if ntl < 10:
    raise SystemExit("名义低于交易所下限 10U，放弃")

snap0 = info.snapshot(ADDR, mids={COIN: mid})
log(f"③ 开仓前：总值 {snap0['equity_total']:.2f}U | 仓位 {len(snap0['positions'])} | "
    f"挂单 {len(snap0['open_orders'])}")

stop_oid = None
bar = int(time.time() * 1000)
try:
    r = ex.update_leverage(COIN, 5, cross=False)
    log("④ 设逐仓 5x:", r["ok"], r.get("error") or "")

    px = hl_sizing.round_price(ex.slippage_limit(COIN, True, mid), sz_dec)
    r = ex.place_order(COIN, True, sz, px, cloid=make_cloid(SYM, bar, "entry"))
    log("⑤ 入场 IOC:", json.dumps({k: r[k] for k in ("ok", "status", "oid", "avg_px",
                                                     "filled_sz", "error")}, ensure_ascii=False))
    if not r["ok"]:
        raise SystemExit("入场失败，中止")
    filled = float(r["filled_sz"] or 0) or sz

    stop_px = hl_sizing.round_price(mid * 0.98, sz_dec)
    r2 = ex.place_order(COIN, False, filled, stop_px, reduce_only=True, trigger_px=stop_px,
                        cloid=make_cloid(SYM, bar, "stop"))
    log(f"⑥ 挂止损（reduce_only，触发 {stop_px:,.2f}）:",
        json.dumps({k: r2[k] for k in ("ok", "status", "oid", "error")}, ensure_ascii=False))
    stop_oid = r2.get("oid")

    snap1 = info.snapshot(ADDR, mids={COIN: ex.mid(COIN)})
    pos = snap1["positions"][0] if snap1["positions"] else {}
    log(f"⑦ 持仓中：总值 {snap1['equity_total']:.2f}U | 仓位 {len(snap1['positions'])} 个 | "
        f"{pos.get('coin')} side={pos.get('side')} sz={pos.get('size')} entry={pos.get('entry_px')} "
        f"uPnL={pos.get('unrealized_pnl')} liq={pos.get('liq_px')} lev={pos.get('leverage')}")
    has_stop = any(o["coin"] == COIN and o["reduce_only"] and o["is_trigger"]
                   for o in snap1["open_orders"])
    log(f"⑧ 挂单 {len(snap1['open_orders'])} 个 | 止损单在场判定 has_stop={has_stop}")

    if not stop_oid:
        for o in snap1["open_orders"]:
            if o["coin"] == COIN and o["reduce_only"]:
                stop_oid = o["oid"]
    if stop_oid:
        rc = ex.cancel(COIN, int(stop_oid))
        log("⑨ 撤止损:", rc["ok"], rc.get("error") or "")
finally:
    time.sleep(1)
    snap = info.snapshot(ADDR, mids={COIN: ex.mid(COIN)})
    for p in snap["positions"]:
        if p["coin"] == COIN and p["size"]:
            m2 = ex.mid(COIN)
            px2 = hl_sizing.round_price(ex.slippage_limit(COIN, False, m2), sz_dec)
            rc = ex.place_order(COIN, False, p["size"], px2, reduce_only=True,
                                cloid=make_cloid(SYM, int(time.time() * 1000), "exit"))
            log("⑩ 平仓:", json.dumps({k: rc[k] for k in ("ok", "status", "avg_px",
                                                         "filled_sz", "error")}, ensure_ascii=False))
    time.sleep(2)
    snap2 = info.snapshot(ADDR, mids={COIN: ex.mid(COIN)})
    for o in snap2["open_orders"]:
        if o["coin"] == COIN:
            rr = ex.cancel(COIN, int(o["oid"]))
            log("⑪ 清理残留挂单", o["oid"], rr["ok"])

time.sleep(2)
snap3 = info.snapshot(ADDR, mids={COIN: ex.mid(COIN)})
log(f"⑫ 收尾：总值 {snap3['equity_total']:.2f}U | 仓位 {len(snap3['positions'])} | "
    f"挂单 {len(snap3['open_orders'])}")
for f in (info.user_fills(ADDR) or [])[:6]:
    log("    成交:", f.get("coin"), f.get("dir"), f.get("sz"), "@", f.get("px"),
        "fee", f.get("fee"), "closedPnl", f.get("closedPnl"))
