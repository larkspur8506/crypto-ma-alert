"""出入金流水（hl_flows）与盈亏曲线的纯本地计算。

**为什么需要这一层**：`hl_snapshots.equity` 是「账户总值」，它把「交易赚的」和
「转进来/转出去的」混成一条线。本账户实测两笔转入（2026-10-14 100.79U、
2026-10-21 901.80U）在旧权益曲线上就是两个直上台阶，看着像暴赚，实际当期还略亏。
所以面板改成两条曲线：

- 累计盈亏 `pnl(t) = equity(t) − Σflows(≤t)`：把出入金剔掉，只看交易结果（USDC）。
- 收益指数 TWR（起点 100）：逐点链式收益率，**剔除出入金的时点影响**
  （入金前 +10% 再入金再 +10% 是 121，不是 120 —— 简单用盈亏÷本金会被入金摊薄）。

本模块只做三件事：① 把交易所账本行分类成带符号的流水；② 幂等落库 + 未知类型告警；
③ 读侧算曲线。**不触网**（采集在 `HlInfo.ledger_updates`，落库在 `DB.hl_add_flow`）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 分类表
# delta.type → 处理方式。写死成表（而不是散落的 if）：交易所新增类型时，
# 「没被显式列出来的」一律走最后的兜底分支，行为可预测。
#
#   deposit                 交易所入金，金额 usdc           → 流入 deposit
#   withdraw                交易所出金，金额 usdc           → 流出 withdraw
#   send                    账户之间转账（本账户那两笔入金就是它：从另一个 HL 账户转进）：
#                             destination == 本账户 → 流入 send_in
#                             user        == 本账户 → 流出 send_out
#                             金额优先 usdcValue，缺失回落 amount
#   accountClassTransfer    现货 ↔ 合约内部划转：钱没离开本账户，**忽略**
#   internalTransfer        账户内部划转 → 忽略
#   spotTransfer            现货内部转账 → 忽略
#   subAccountTransfer      子账户之间 → 忽略
#   vaultDeposit            存进金库：钱离开交易账户，权益同步下降 → 流出 vault_out
#   vaultWithdraw           从金库取回：钱回到交易账户 → 流入 vault_in
#   其它未知类型             按**外部出入金**计入（符号沿用交易所给的数，缺符号按流入），
#                            并在 hl_events 记一次 flow_unknown（同类型 24h 去重）——
#                            宁可把一笔看不懂的流水当成出入金剔掉，也不让曲线凭空跳一个台阶。
#
# 注：withdraw 的交易所提现手续费（固定 1 USDC）**不计入流水** —— 它不是为了投资而
# 转入/转出的钱，而是真金白银的成本，留在盈亏里才诚实。
IGNORED_TYPES = frozenset({"accountClassTransfer", "internalTransfer",
                           "spotTransfer", "subAccountTransfer"})

SIMPLE_TYPES = {
    "deposit": ("deposit", 1),          # kind, 方向（+1 流入 / -1 流出）
    "withdraw": ("withdraw", -1),
    "vaultDeposit": ("vault_out", -1),
    "vaultWithdraw": ("vault_in", 1),
}

_AMOUNT_KEYS = ("usdc", "usdcValue", "amount", "netAmount")
_SEND_AMOUNT_KEYS = ("usdcValue", "usdc", "amount", "netAmount")

EV_FLOW_UNKNOWN = "flow_unknown"
_FLOW_UNKNOWN_DEDUP_S = 24 * 3600.0


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _get(o: Any, key: str, default: Any = None) -> Any:
    """dict / sqlite3.Row 统一取值。"""
    if isinstance(o, dict):
        return o.get(key, default)
    try:
        return o[key]
    except (IndexError, KeyError, TypeError):
        return default


def _amount(delta: Dict[str, Any], keys=_AMOUNT_KEYS) -> Optional[float]:
    """按优先级取金额；一个都没有返回 None（不知道 = 不猜）。"""
    for k in keys:
        v = delta.get(k)
        if v not in (None, ""):
            return _num(v)
    return None


def classify(row: Any, address: str) -> Optional[Dict[str, Any]]:
    """一条账本行 → 一条流水；返回 None = 这条不计入（内部划转 / 与本账户无关）。

    返回 `{ts, kind, amount, hash, counterparty, dex, type}`：
    - `ts` 秒（交易所 `time` 是毫秒）
    - `amount` **带符号**：正 = 流入账户，负 = 流出
    - `hash` 去重键之一（与 ts 组成唯一键）
    """
    if not isinstance(row, dict):
        return None
    ts_ms = _num(row.get("time"), 0.0)
    if ts_ms <= 0:
        return None
    delta = row.get("delta")
    if not isinstance(delta, dict):
        return None
    typ = str(delta.get("type") or "")
    if not typ or typ in IGNORED_TYPES:
        return None

    who = str(address or "").strip().lower()
    user = str(delta.get("user") or "").strip().lower()
    dest = str(delta.get("destination") or "").strip().lower()
    h = row.get("hash")
    # hash 缺失时用 (type, ms, amount) 合成一个：去重键必须是确定的字符串，
    # SQLite 的 UNIQUE 对 NULL 不生效（多个 NULL 互不冲突），留 NULL 就等于没有去重。
    hash_ = str(h) if h else f"{typ}:{int(ts_ms)}:{_amount(delta) or 0.0}"
    dex = str(delta.get("sourceDex") or delta.get("destinationDex") or "")
    cp = ""

    if typ in SIMPLE_TYPES:
        amt = _amount(delta)
        if amt is None:
            return None
        kind, sign = SIMPLE_TYPES[typ]
        return {"ts": ts_ms / 1000.0, "kind": kind, "amount": sign * abs(amt),
                "hash": hash_, "counterparty": cp, "dex": dex, "type": typ}

    if typ == "send":
        amt = _amount(delta, _SEND_AMOUNT_KEYS)
        if amt is None:
            return None
        if who and user == who and dest == who:
            return None                     # 自己转自己：钱没动
        if who and dest == who:
            return {"ts": ts_ms / 1000.0, "kind": "send_in", "amount": abs(amt),
                    "hash": hash_, "counterparty": user, "dex": dex, "type": typ}
        if who and user == who:
            return {"ts": ts_ms / 1000.0, "kind": "send_out", "amount": -abs(amt),
                    "hash": hash_, "counterparty": dest, "dex": dex, "type": typ}
        # 两边都不是本账户（防御：账本本该只返回本账户的流水）→ 与本账户权益无关，不计
        logger.debug("[hl-flows] 跳过与本账户无关的 send：user=%s dest=%s", user, dest)
        return None

    # 未知类型：按外部出入金计入，符号沿用交易所给的数（缺符号则按流入）
    amt = _amount(delta)
    if amt is None:
        logger.warning("[hl-flows] 未知流水类型 %s 没有金额字段，忽略：%s", typ, str(row)[:200])
        return None
    return {"ts": ts_ms / 1000.0, "kind": "other", "amount": amt,
            "hash": hash_, "counterparty": user or dest, "dex": dex, "type": typ}


def _flow_unknown_logged(db: Any, typ: str, now: float) -> bool:
    """同类型 24h 内是否已经记过 flow_unknown。"""
    try:
        rows = db.hl_recent_events(200, kind=EV_FLOW_UNKNOWN) or []
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-flows] 读 flow_unknown 事件失败（按未记录处理）：%s", e)
        return False
    for r in rows:
        created = _num(_get(r, "created_at"), 0.0)
        if now - created > _FLOW_UNKNOWN_DEDUP_S:
            break                      # 按 id DESC 返回，后面的只会更老
        try:
            data = json.loads(_get(r, "data") or "{}")
        except (TypeError, ValueError):
            data = {}
        if str((data or {}).get("type") or "") == typ:
            return True
    return False


def record_flows(db: Any, rows: Iterable[Any], address: str,
                 now: Optional[float] = None) -> Dict[str, Any]:
    """分类 + 幂等落库；未知类型记一次 flow_unknown（同类型 24h 去重）。

    返回 `{"added": 新写入条数, "seen": 拉到条数, "unknown": [类型...]}`。
    """
    now = time.time() if now is None else float(now)
    added = seen = 0
    unknown: List[str] = []
    for row in rows or []:
        seen += 1
        f = classify(row, address)
        if f is None:
            continue
        try:
            if db.hl_add_flow(f):
                added += 1
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-flows] 流水落库失败（忽略该条）：%s", e)
            continue
        if f["kind"] == "other" and f["type"] not in unknown:
            unknown.append(f["type"])
    for typ in unknown:
        if _flow_unknown_logged(db, typ, now):
            continue
        try:
            db.hl_log_event(
                EV_FLOW_UNKNOWN,
                f"账本出现未知流水类型 {typ}，已按外部出入金计入盈亏曲线（请人工核对）",
                data={"type": typ})
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-flows] 写 flow_unknown 事件失败：%s", e)
    return {"added": added, "seen": seen, "unknown": unknown}


# ---------------------------------------------------------------- 读侧曲线
def _max_drawdown(values: List[float]) -> float:
    """峰谷回撤（正数=回撤幅度）。曲线走到哪都以「历史最高」为基准。"""
    peak = None
    worst = 0.0
    for v in values:
        if peak is None or v > peak:
            peak = v
        elif peak is not None:
            dd = peak - v
            if dd > worst:
                worst = dd
    return worst


def curve(snaps: Iterable[Any], flows: Iterable[Any], *, now: float,
          window_s: float = 30 * 86400.0, flows_ok: bool = True,
          flows_note: str = "") -> Dict[str, Any]:
    """由快照 + 流水算出 `{"pnl": [...], "twr": [...], "meta": {...}}`。

    - `snaps`：`hl_snapshots` 行（`ts` 秒 / `equity` 账户总值），只取 `window_s` 窗口内。
    - `flows`：`hl_flows` 行（`ts` / 带符号 `amount`），**不限窗口** —— 60 天前的入金
      同样构成今天的本金，漏掉它 pnl 会凭空多出一大块。
    - `pnl_k = equity_k − equity_base − Σflows(base < ts ≤ t_k)`：**期初权益是基线**。
      交易所入金到账前会有一批 `equity=0` 的快照（执行器先跑起来、钱还没到），它们不是
      「亏了 100%」而是「还没有本金」；期初之前已存入的钱也不该算盈亏（否则第一笔入金
      到账那一刻会凭空画出一段与入金等额的「回撤」，实测 100.79）。
    - TWR：`r_k = (equity_k − flow_k)/equity_{k−1} − 1`，`flow_k` 是落在 `(t_{k−1}, t_k]`
      的净流水；`index_k = index_{k−1}·(1+r_k)`，起点 100。`equity_{k−1} <= 0` 时该步
      不猜（r_k = 0，指数不变）并计入 `twr_skipped`。
    """
    since = float(now) - float(window_s)
    pts: List[List[float]] = []
    for s in snaps or []:
        t = _num(_get(s, "ts"), 0.0)
        if t < since:
            continue
        pts.append([t, _num(_get(s, "equity"), 0.0)])
    pts.sort(key=lambda x: x[0])
    # 丢掉「还没本金」的期初零权益快照（见 docstring）。
    while pts and pts[0][1] <= 0:
        pts.pop(0)

    fs: List[List[float]] = []
    for f in flows or []:
        fs.append([_num(_get(f, "ts"), 0.0), _num(_get(f, "amount"), 0.0)])
    fs.sort(key=lambda x: x[0])

    net_deposit = sum(a for _, a in fs)
    pnl: List[List[float]] = []
    twr: List[List[float]] = []
    if not pts:
        return {"pnl": [], "twr": [], "meta": {
            "net_deposit": round(net_deposit, 6), "pnl_total": 0.0, "pnl_7d": 0.0,
            "max_dd_pnl": 0.0, "twr_total_pct": 0.0, "flows_ok": bool(flows_ok),
            "flows_note": str(flows_note or ""), "points": 0, "flows": len(fs),
            "equity_base": None, "twr_skipped": 0}}
    t0, base_eq = pts[0][0], pts[0][1]
    # 基线之前（含同一时刻）的流水已体现在期初权益里，不重复计入 pnl。
    fi = 0
    while fi < len(fs) and fs[fi][0] <= t0:
        fi += 1
    cum = cum_prev = 0.0
    prev_eq: Optional[float] = base_eq
    index = 100.0
    skipped = 0
    for k, (t, eq) in enumerate(pts):
        while fi < len(fs) and fs[fi][0] <= t:
            cum += fs[fi][1]
            fi += 1
        pnl.append([t, round(eq - base_eq - cum, 6)])
        if k == 0:
            index = 100.0
        else:
            flow_k = cum - cum_prev            # 落在 (t_prev, t] 的净流水
            if prev_eq > 0:
                index = index * ((eq - flow_k) / prev_eq)
            else:
                skipped += 1                   # 上一点权益 <= 0：不猜，指数原地不动
        twr.append([t, round(index, 6)])
        prev_eq, cum_prev = eq, cum

    pnl_total = pnl[-1][1] if pnl else 0.0
    pnl_7d = 0.0
    if pnl:
        base = next((v for t, v in pnl if t >= float(now) - 7 * 86400.0), pnl[0][1])
        pnl_7d = pnl_total - base
    meta = {
        "net_deposit": round(net_deposit, 6),
        "pnl_total": round(pnl_total, 6),
        "pnl_7d": round(pnl_7d, 6),
        "max_dd_pnl": round(_max_drawdown([v for _, v in pnl]), 6),
        "twr_total_pct": round((twr[-1][1] - 100.0) if twr else 0.0, 6),
        "flows_ok": bool(flows_ok),
        "flows_note": str(flows_note or ""),
        "points": len(pnl),
        "flows": len(fs),
        "twr_skipped": skipped,
        # 期初权益（曲线基线）：pnl 是相对它的增量，入金到账前的 0 权益期已被剔除。
        "equity_base": round(base_eq, 6),
    }
    return {"pnl": pnl, "twr": twr, "meta": meta}
