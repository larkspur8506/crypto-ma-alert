"""成交流水（hl_fills）聚合出单个仓位的净 PnL —— **纯函数、只用 stdlib**。

为什么单独成模块
------------------------------------------------------------------
「这笔仓到底赚了多少」有两个互相独立的答案来源：
  1. 本地记录：`positions` 的 `actual_entry` / `exit_price` / `hl_sz`（下单那一刻的数）；
  2. 交易所流水：每笔真实成交的 `closedPnl` 与 `fee`（钱实际怎么动的）。
两者会分叉：部分成交、IOC 未全成、手动加减仓、平仓价记成限价……PR-2 的离线报告
（`scripts/fill_recon_report.py`）已经证明这个分叉真实存在。执行器要在**关账那一刻**
就拿流水口径算一遍、并与本地口径对比（差异大就记事件），所以聚合逻辑必须是可单测的
纯函数，不能埋在 executor 里跟 DB/网络纠缠。

口径（与 `scripts/fill_recon_report.py` 严格同源，别各写一套）
------------------------------------------------------------------
- 时间窗：`ts ∈ [opened_at − 300s, closed_at + 300s]`
  * 下界 −300s：入场成交发生在 `opened_at` 写库**之前**（先成交再落库），用
    `>= opened_at` 会把入场腿整条漏掉 → 数量对账必然超差、末段净额少算入场费；
  * 上界 +300s：平仓成交在关账之后才回库；而 `closed_at` 之后更久属于**同标的重开**
    的新仓位，算进来就是串仓（这是 PR-2 踩过的坑）。
- 腿：`dir` 含 `Open` → 入场腿，含 `Close` → 出场腿（HL 的取值是
  `Open Long` / `Close Short` 这类，大小写不敏感）。
- `side` 用来排除同一窗口内**反方向**的成交（同标的快速反手时会出现），
  这类行不计腿、不计费、计入 `ignored_cnt` —— 它们属于另一笔仓位。
- 手续费：优先 `fee_quote`（PR-1 写入时已折算成 U）；为 NULL 时回退 `fee`
  —— 但**只有** `fee_token` 属于计价币（USD/USDT/USDC，或空=HL 默认扣报价币）
  才允许回退。非计价币行（0.2 ARB 之类）不进 `fee_total`，只计
  `unresolved_fee_cnt`：宁可少算也不许把「币的数量」当「U」加进净利。
- `net = Σclosed_pnl − fee_total`（交易所已把平仓盈亏算进 closedPnl，不要再自己乘价差）。

不做的事：不读 DB、不发请求、不猜测缺失字段（缺就是缺，由调用方决定怎么标注）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "ENTRY_LOOKBACK_S", "EXIT_LOOKAHEAD_S", "QUOTE_FEE_TOKENS", "TS_MS_THRESHOLD",
    "window_bounds", "in_window", "fee_of", "leg_of", "from_exchange",
    "aggregate_fills", "net_size", "pnl_diff",
]

ENTRY_LOOKBACK_S = 300.0        # 入场成交早于 opened_at 写库
EXIT_LOOKAHEAD_S = 300.0        # 平仓成交晚于 closed_at 落库
# 只有这些币种的 fee 能 1:1 当 U 累加（与 executor.QUOTE_FEE_TOKENS 同集合）
QUOTE_FEE_TOKENS = frozenset({"USD", "USDT", "USDC"})

LEG_ENTRY = "entry"
LEG_EXIT = "exit"


# ---------------------------------------------------------------- 小工具
def _get(row: Any, key: str, default: Any = None) -> Any:
    """兼容 sqlite3.Row（无 .get，缺列抛 IndexError）与 dict（KeyError）。"""
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _num(v: Any) -> Optional[float]:
    """宽松转 float；None/垃圾/NaN/inf → None（绝不抛）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _is_quote_token(token: Any) -> bool:
    """fee_token 是否美元计价。**空值按计价币**（HL 手续费默认扣在报价币）。"""
    if token is None:
        return True
    t = str(token).strip().upper()
    return t == "" or t in QUOTE_FEE_TOKENS


# 毫秒/秒判别阈值：epoch 毫秒都在 1e12 以上（1e12 ms ≈ 2001 年），秒级最大约 1e10。
# 唯一判据，Python 侧（_ts_seconds）与 SQL 侧（v_ledger 归一）必须同源，别各写一个数。
TS_MS_THRESHOLD = 1e12


def _ts_seconds(v: Any) -> Optional[float]:
    """成交时刻统一成秒。库里由 `_sync_fills` 写成秒（ms//1000）；但历史导入/手工
    数据里出现过毫秒（>1e12）—— 不归一的话整行会被判成「窗口外」而静默漏掉。"""
    t = _num(v)
    if t is None:
        return None
    return t / 1000.0 if t > TS_MS_THRESHOLD else t


def window_bounds(opened_at: Any, closed_at: Any) -> Tuple[Optional[float], Optional[float]]:
    """时间窗上下界。缺上界（仓位还在开）时只卡下界 —— 数量对账要用「至今全部成交」。"""
    lo = _num(opened_at)
    hi = _num(closed_at)
    return ((lo - ENTRY_LOOKBACK_S) if lo is not None else None,
            (hi + EXIT_LOOKAHEAD_S) if hi is not None else None)


def in_window(ts: Any, lo: Optional[float], hi: Optional[float]) -> bool:
    """成交时刻是否在窗内。**ts 缺失按不在窗内**（无法定位归属，宁可少算也不串仓）。"""
    t = _ts_seconds(ts)
    if t is None:
        return False
    if lo is not None and t < lo:
        return False
    if hi is not None and t > hi:
        return False
    return True


def leg_of(direction: Any) -> Optional[str]:
    """`dir` → entry / exit / None。"""
    d = str(direction or "").strip().lower()
    if "open" in d:
        return LEG_ENTRY
    if "close" in d:
        return LEG_EXIT
    return None


def _side_ok(direction: Any, side: Any) -> bool:
    """方向词与仓位方向是否一致。无方向词（老库/异常行）→ 不排除。"""
    d = str(direction or "").strip().lower()
    s = _num(side)
    s = 1.0 if s is None else s
    if "long" in d:
        return s >= 0
    if "short" in d:
        return s < 0
    return True


def fee_of(fill: Any) -> Tuple[Optional[float], bool]:
    """返回 (计价币金额 or None, 是否已解决)。

    - `fee_quote` 非 NULL → 用它（PR-1 之后写入的行都走这条）；
    - 否则 fee_token 是计价币（含空）→ 回退 `fee`（PR-1 之前的历史行）；
    - 否则 → (None, False)：换算不了就不许当 0 参与净利累加。
    """
    fq = _num(_get(fill, "fee_quote"))
    if fq is not None:
        return fq, True
    if _is_quote_token(_get(fill, "fee_token")):
        return (_num(_get(fill, "fee")) or 0.0), True
    return None, False


# ---------------------------------------------------------------- 交易所原始成交
def from_exchange(f: Any) -> Dict[str, Any]:
    """把交易所 `userFills*` 的一条原始成交归一成聚合函数认识的形状。

    交易所字段是驼峰（`feeToken` / `closedPnl`）且 `time` 是毫秒；库里是下划线 +
    秒。执行器「全量回捞」时拿到的是前者，直接喂 `aggregate_fills` 会**静默漏掉
    手续费与时间窗判断**（键名不对 → 取到 None → 该行按非计价币处理），所以必须
    先过这一层。缺省值一律 None/0，不猜。
    """
    fee = _num(_get(f, "fee"))
    token = _get(f, "feeToken", _get(f, "fee_token"))
    # 与写库路径同口径：计价币 → fee_quote=fee；其余 → None（由 fee_of 回退判断）
    fee_quote = fee if _is_quote_token(token) else None
    t = _num(_get(f, "time", _get(f, "ts")))
    return {
        "tid": str(_get(f, "tid") or _get(f, "hash") or ""),
        "coin": _get(f, "coin"),
        "dir": _get(f, "dir"),
        "sz": _num(_get(f, "sz")),
        "px": _num(_get(f, "px")),
        "fee": fee,
        "fee_token": token,
        "fee_quote": fee_quote,
        "closed_pnl": _num(_get(f, "closedPnl", _get(f, "closed_pnl"))),
        "ts": (t / 1000.0) if (t is not None and t > 1e12) else t,
    }


# ---------------------------------------------------------------- 聚合
def aggregate_fills(fills: Iterable[Any], opened_at: Any, closed_at: Any,
                    coin: str, side: Any = 1) -> Dict[str, Any]:
    """把该仓时间窗内的成交聚成一份 PnL 摘要（纯函数，永不抛）。

    返回键
    ------
    entry_sz / entry_vwap : 入场腿数量与成交量加权均价（无量 → 0.0 / None）
    exit_sz  / exit_vwap  : 出场腿同上
    fee_total             : 已折算成 U 的手续费合计
    closed_pnl_sum        : Σ closed_pnl（交易所口径，未扣费）
    net                   : closed_pnl_sum − fee_total
    unresolved_fee_cnt    : 无法折算成 U 的手续费**行数**（>0 表示 net 偏乐观）
    n_fills               : 窗内成交笔数（0 = 没有可依据的流水）
    ignored_cnt           : 窗内但方向与仓位相反（属于另一笔仓）而排除的笔数
    """
    lo, hi = window_bounds(opened_at, closed_at)
    entry_sz = exit_sz = 0.0
    entry_ntl = exit_ntl = 0.0
    fee_total = 0.0
    closed_pnl_sum = 0.0
    unresolved = 0
    n_fills = 0
    ignored = 0

    for f in fills or []:
        if coin and str(_get(f, "coin") or "") != str(coin):
            continue
        ts = _get(f, "ts")
        if not in_window(ts, lo, hi):
            continue
        direction = _get(f, "dir")
        if not _side_ok(direction, side):
            # 同标的反手（先平多再开空）：窗内会出现另一笔仓的成交，
            # 连它的手续费一起算进来会把净额弄脏，整行排除。
            ignored += 1
            continue
        n_fills += 1

        sz = _num(_get(f, "sz"))
        px = _num(_get(f, "px"))
        leg = leg_of(direction)
        if leg is not None and sz is not None and sz > 0 and px is not None:
            if leg == LEG_ENTRY:
                entry_sz += sz
                entry_ntl += sz * px
            else:
                exit_sz += sz
                exit_ntl += sz * px

        fee_val, resolved = fee_of(f)
        if resolved:
            fee_total += fee_val or 0.0
        else:
            unresolved += 1
        closed_pnl_sum += _num(_get(f, "closed_pnl")) or 0.0

    return {
        "entry_sz": entry_sz,
        "entry_vwap": (entry_ntl / entry_sz) if entry_sz > 0 else None,
        "exit_sz": exit_sz,
        "exit_vwap": (exit_ntl / exit_sz) if exit_sz > 0 else None,
        "fee_total": fee_total,
        "closed_pnl_sum": closed_pnl_sum,
        "net": closed_pnl_sum - fee_total,
        "unresolved_fee_cnt": unresolved,
        "n_fills": n_fills,
        "ignored_cnt": ignored,
    }


def net_size(agg: Dict[str, Any]) -> float:
    """成交口径的**净持仓量** = Σ入场 − Σ出场（用于与交易所仓位数对账）。"""
    try:
        return float(agg.get("entry_sz") or 0.0) - float(agg.get("exit_sz") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def pnl_diff(r_local: Optional[float], r_fill: Optional[float],
             net_local: Optional[float], net_fill: Optional[float],
             notional: Optional[float],
             r_tol: float = 0.15, r_rel_tol: float = 0.10,
             net_abs_tol: float = 1.0, net_rel_tol: float = 0.0005) -> Dict[str, Any]:
    """本地口径 vs 流水口径的差异判定（阈值与 PR-2 报告脚本一致，便于对数）。

    触发条件（任一）：
      |ΔR|   > max(r_tol, r_rel_tol × |R_local|)
      |Δnet| > max(net_abs_tol, net_rel_tol × |notional|)
    相对项防止「大仓位的正常舍入差」被当成异常；绝对项兜住小仓位的噪声。
    """
    d_r = (abs(r_fill - r_local)
           if (r_fill is not None and r_local is not None) else None)
    d_net = (abs(net_fill - net_local)
             if (net_fill is not None and net_local is not None) else None)
    r_thr = max(float(r_tol), float(r_rel_tol) * abs(float(r_local or 0.0)))
    net_thr = max(float(net_abs_tol), float(net_rel_tol) * abs(float(notional or 0.0)))
    return {
        "d_r": d_r,
        "d_net": d_net,
        "r_threshold": r_thr,
        "net_threshold": net_thr,
        "r_diff": bool(d_r is not None and d_r > r_thr),
        "net_diff": bool(d_net is not None and d_net > net_thr),
    }
