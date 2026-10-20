"""只读 fill 对账报告：用交易所成交流水（hl_fills）重算每个已关仓位的净 PnL / R，
与本地 positions 记录逐仓比对，输出差异分布。

定位
------------------------------------------------------------------
**上线前的离线分析工具**，不是运行期对账器：
- 只读打开 sqlite（``file:...?mode=ro``），**绝不写库**；
- **不发任何网络请求**（只读本地已同步的 hl_fills）；
- exit code 恒为 0（差异是「报告内容」，不是「执行失败」）。

口径（与执行器/面板一致，见 app/posfields.py）
------------------------------------------------------------------
- 入场腿：该 coin 上 ``dir`` 含 ``Open``、``ts ∈ [opened_at-300s, closed_at+300s]``
  的 fills（下界 300s 是因为入场成交发生在 ``opened_at`` 写库之前；上界用 closed_at
  收口，否则同标的重开后的新成交会被算进这笔旧仓）。
- 出场腿：同一时间窗内 ``dir`` 含 ``Close`` 的 fills。
- ``fee_total`` = Σfee，但 ``fee_token`` 不是 USD/USDT/USDC 的行**不计入**，
  只标 ``UNRESOLVED_FEE``（无法换算就不许悄悄当成 0 参与比较）。
- ``net_fill = Σclosed_pnl − fee_total``；``R_fill = net_fill / risk_u``。
- ``R_local`` 按 positions 的 ``actual_entry`` / ``actual_stop`` / ``exit_price`` 重算
  （与 ``db.close_position`` 同一公式；**不直接信 r_multiple 列**，它可能是旧口径）。
- ``net_local = (exit_price − actual_entry) × side × 币量 − fee_local``。positions 没有
  手续费列，``fee_local`` 取本仓 fills 的 ``fee_total`` —— 于是 ``Δnet`` 只反映
  **价差/数量**差异（这正是本地记录最容易错的地方），而不是恒等于手续费。

flags
------------------------------------------------------------------
``NO_FILLS`` 该仓在 hl_fills 里查不到任何成交（MEXC 人工流本该如此）；
``NO_EXIT_FILLS`` 有入场成交无出场成交（交易所侧还没同步到 / 平仓走了别的路径）；
``UNRESOLVED_FEE`` 存在非 USD 计价手续费行（fee 未计入 fee_total）；
``NO_ACTUAL_ENTRY`` 缺 actual_entry（人工流未确认）→ 无法算 R，跳过；
``NO_RISK`` 风险额算不出来（币量/止损缺失或入场=止损）→ 无法算 R_fill；
``NO_HL`` 非 HL 执行器的仓（``hl_status`` 为空，历史 MEXC 行，仅作区分）；
``R_DIFF`` ΔR 超 ``--r-tol``；``NET_DIFF`` Δnet 超 ``--net-abs-tol``/``--net-rel-tol``。

用法
------------------------------------------------------------------
    python3 scripts/fill_recon_report.py --db data/app.db
    python3 scripts/fill_recon_report.py --db data/app.db --since 2026-10-01 --json /tmp/recon.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:                    # 为了复用 app.posfields 的双口径读法
    sys.path.insert(0, REPO_ROOT)

try:                                             # 纯 stdlib 模块，import 无副作用
    from app import posfields
except Exception:                                # noqa: BLE001 - 脚本可独立运行
    posfields = None                             # type: ignore[assignment]

FEE_TOKENS_USD = ("USD", "USDT", "USDC")
ENTRY_LOOKBACK_S = 300.0        # 入场成交早于 opened_at 写库
EXIT_LOOKAHEAD_S = 300.0        # 关账后成交落库的延迟
NO_FILLS = "NO_FILLS"
NO_EXIT_FILLS = "NO_EXIT_FILLS"
UNRESOLVED_FEE = "UNRESOLVED_FEE"
NO_ACTUAL_ENTRY = "NO_ACTUAL_ENTRY"
NO_RISK = "NO_RISK"
NO_HL = "NO_HL"
R_DIFF = "R_DIFF"
NET_DIFF = "NET_DIFF"


# ---------------------------------------------------------------- 小工具

def _num(v: Any) -> Optional[float]:
    """宽松转 float；None/空串/NaN → None（不抛）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _row_field(row: Any, name: str, default: Any = None) -> Any:
    """取列值，缺列返回 default（老库没有的列不能让整轮报告崩掉）。"""
    if posfields is not None:
        return posfields.field(row, name, default)
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def _iso(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _pct(vals: Iterable[float], q: float) -> Optional[float]:
    """百分位（线性插值，与 numpy 默认一致）。空集合返回 None。"""
    s = sorted(vals)
    if not s:
        return None
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _fee_token_quoted(token: Any) -> bool:
    """fee_token 是否美元计价。**空值按美元计价**（HL 手续费默认扣在报价币）。"""
    if token is None:
        return True
    t = str(token).strip().upper()
    return t == "" or t in FEE_TOKENS_USD


# ---------------------------------------------------------------- 读库

def open_readonly(path: str) -> sqlite3.Connection:
    """只读打开（mode=ro）：任何写入都会抛 sqlite3.OperationalError。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"库文件不存在：{path}")
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    r = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (name,)).fetchone()
    return r is not None


def load_positions(conn: sqlite3.Connection, since: Optional[float]) -> List[sqlite3.Row]:
    """已关仓位，按关闭时间过滤（--since 之前的不看）。"""
    if not _table_exists(conn, "positions"):
        return []
    sql = "SELECT * FROM positions WHERE status='closed'"
    args: List[Any] = []
    if since is not None:
        sql += " AND closed_at IS NOT NULL AND closed_at >= ?"
        args.append(float(since))
    sql += " ORDER BY COALESCE(closed_at, created_at), id"
    try:
        return list(conn.execute(sql, args))
    except sqlite3.Error:                        # 老库缺列（如 status 都没有）→ 空报告
        return []


def load_fills(conn: sqlite3.Connection, since: Optional[float]) -> List[Dict[str, Any]]:
    """读 hl_fills 为 dict 列表；表不存在/缺列时退化成空（老库要能出报告）。

    `since` 是**成交时间下界**（由调用方按仓位最小 opened_at 推出），不是关账日：
    按关账日筛会漏掉「几个月前开仓、今天关」那笔的入场成交，误报 NO_FILLS。
    """
    if not _table_exists(conn, "hl_fills"):
        return []
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(hl_fills)")}
    want = [c for c in ("tid", "symbol", "coin", "side", "sz", "px", "fee", "fee_token",
                        "dir", "closed_pnl", "ts") if c in cols]
    if not want:
        return []
    sql = f"SELECT {', '.join(want)} FROM hl_fills"
    args: List[Any] = []
    if since is not None and "ts" in cols:
        sql += " WHERE ts IS NULL OR ts >= ?"
        args.append(float(since))
    sql += " ORDER BY COALESCE(ts, 0), id" if "id" in cols else " ORDER BY 1"
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.Error:
        return []


def _coin_of(symbol: str) -> str:
    """symbol → HL 合约名（注册表优先，退化到前缀；只影响匹配范围）。"""
    try:
        from app import symbol_registry
        return symbol_registry.hl_coin(symbol)
    except Exception:                            # noqa: BLE001 - 注册表缺失/未登记
        return (symbol or "").split("_")[0]


# ---------------------------------------------------------------- 单仓对账

def _qty_local(row: Any) -> Optional[float]:
    """本地仓位的**币量**。HL 仓读 actual_contracts/hl_sz；历史 MEXC 行的
    actual_contracts 是「张数」，必须走 coin_qty（coin/张 换算后的币量）。
    """
    is_hl = bool(posfields is not None and posfields.is_hl(row))
    # 非 HL 行**不回落 contracts**：那是张数，量纲差 100~1000 倍，宁可给 None。
    names = (("actual_contracts", "hl_sz", "coin_qty") if is_hl else ("coin_qty",))
    for name in names:
        v = _num(_row_field(row, name))
        if v:
            return v
    return None


def _risk_local(row: Any, qty: Optional[float]) -> Optional[float]:
    """风险U：优先 posfields 的双口径（HL = 币量×|入场-止损|，MEXC = risk_u 列）。"""
    if posfields is not None:
        v = _num(posfields.risk_u(row))
        if v:
            return v
    entry = _num(_row_field(row, "actual_entry")) or _num(_row_field(row, "entry_price"))
    stop = _num(_row_field(row, "actual_stop")) or _num(_row_field(row, "stop_price"))
    if entry is not None and stop is not None and abs(entry - stop) > 0:
        if qty:
            return abs(entry - stop) * qty
    return _num(_row_field(row, "risk_u"))


def _match_fills(pos: Any, fills_by_coin: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """取出该仓时间窗内、同标的的成交（coin 与 symbol 两种键都认）。"""
    symbol = str(_row_field(pos, "symbol") or "")
    coin = _coin_of(symbol)
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for key in (coin, symbol):
        for f in fills_by_coin.get(key, []):
            tid = str(f.get("tid") or id(f))
            if tid in seen:
                continue
            seen.add(tid)
            out.append(f)
    return out


def reconcile_row(pos: Any, fills: List[Dict[str, Any]],
                  r_tol: float, net_abs_tol: float, net_rel_tol: float) -> Dict[str, Any]:
    """把一个仓位与它的成交比对，返回一行结果（纯函数，不抛）。"""
    flags: List[str] = []
    pid = _row_field(pos, "id")
    symbol = str(_row_field(pos, "symbol") or "")
    side = int(_num(_row_field(pos, "side")) or 1)
    opened_at = _num(_row_field(pos, "opened_at"))
    closed_at = _num(_row_field(pos, "closed_at"))
    actual_entry = _num(_row_field(pos, "actual_entry"))
    exit_px = _num(_row_field(pos, "exit_price"))
    qty = _qty_local(pos)
    risk = _risk_local(pos, qty)

    if posfields is not None and not posfields.is_hl(pos):
        flags.append(NO_HL)                       # 历史 MEXC 行：没成交属正常，先标注区分

    # ---- 时间窗：opened_at 前 300s 到 closed_at 后 300s（缺 opened_at 时退化为全窗）
    lo = (opened_at - ENTRY_LOOKBACK_S) if opened_at else None
    hi = (closed_at + EXIT_LOOKAHEAD_S) if closed_at else None

    def _in_window(f: Dict[str, Any]) -> bool:
        ts = _num(f.get("ts"))
        if ts is None:
            return True                           # 老库可能没 ts：宁可纳入也不要静默漏掉
        if lo is not None and ts < lo:
            return False
        if hi is not None and ts > hi:
            return False
        return True

    inwin = [f for f in fills if _in_window(f)]
    opens = [f for f in inwin if "open" in str(f.get("dir") or "").lower()]
    closes = [f for f in inwin if "close" in str(f.get("dir") or "").lower()]

    def _agg(rows: List[Dict[str, Any]]) -> Tuple[float, Optional[float]]:
        sz_sum, notional = 0.0, 0.0
        for f in rows:
            sz, px = _num(f.get("sz")), _num(f.get("px"))
            if not sz or px is None:
                continue
            sz_sum += sz
            notional += sz * px
        return sz_sum, (notional / sz_sum if sz_sum > 0 else None)

    entry_sz, entry_vwap = _agg(opens)
    exit_sz, exit_vwap = _agg(closes)

    fee_total, unresolved, closed_pnl_sum = 0.0, 0, 0.0
    for f in inwin:
        fee = _num(f.get("fee")) or 0.0
        if _fee_token_quoted(f.get("fee_token")):
            fee_total += fee
        else:
            unresolved += 1
        closed_pnl_sum += _num(f.get("closed_pnl")) or 0.0

    if not inwin:
        flags.append(NO_FILLS)
    elif not closes:
        flags.append(NO_EXIT_FILLS)
    if unresolved:
        flags.append(UNRESOLVED_FEE)
    if actual_entry is None:
        flags.append(NO_ACTUAL_ENTRY)

    # ---- fill 侧
    net_fill = (closed_pnl_sum - fee_total) if inwin else None
    r_fill = (net_fill / risk) if (net_fill is not None and risk and risk > 0) else None
    if inwin and (not risk or risk <= 0):
        flags.append(NO_RISK)

    # ---- 本地侧（与 db.close_position 同公式；不信 r_multiple 列）
    r_local, net_local = None, None
    if actual_entry is not None and exit_px is not None:
        local_risk = None
        stop = _num(_row_field(pos, "actual_stop")) or _num(_row_field(pos, "stop_price"))
        if stop is not None:
            local_risk = abs(actual_entry - stop)
        if local_risk and local_risk > 0:
            r_local = ((exit_px - actual_entry) * side) / local_risk
        if qty:
            gross = (exit_px - actual_entry) * side * qty
            fee_local = _num(_row_field(pos, "fee"))
            net_local = gross - (fee_local if fee_local is not None else fee_total)

    d_r = abs(r_fill - r_local) if (r_fill is not None and r_local is not None) else None
    d_net = (abs(net_fill - net_local)
             if (net_fill is not None and net_local is not None) else None)

    if d_r is not None and d_r > r_tol:
        flags.append(R_DIFF)
    if d_net is not None:
        base = abs(net_local) if net_local else 0.0
        if d_net > net_abs_tol and d_net > net_rel_tol * max(base, 1.0):
            flags.append(NET_DIFF)

    return {
        "id": pid, "symbol": symbol, "coin": _coin_of(symbol), "side": side,
        "opened_at": opened_at, "closed_at": closed_at,
        "r_local": r_local, "r_fill": r_fill, "d_r": d_r,
        "net_fill": net_fill, "net_local": net_local, "d_net": d_net,
        "entry_vwap": entry_vwap, "exit_vwap": exit_vwap,
        "entry_sz": entry_sz, "exit_sz": exit_sz,
        "qty": qty, "risk_u": risk, "fee_total": fee_total,
        "closed_pnl_sum": closed_pnl_sum, "unresolved_fee_rows": unresolved,
        "n_fills": len(inwin), "flags": flags,
    }


def build_report(conn: sqlite3.Connection, since: Optional[float], r_tol: float,
                 net_abs_tol: float, net_rel_tol: float) -> Dict[str, Any]:
    positions = load_positions(conn, since)
    # 成交下界按**最早开仓时间**（减入场回看窗）算，而不是 --since（关账日）：
    # 「三个月前开仓、上周关」的仓，入场成交远早于 --since，按关账日筛会误报 NO_FILLS。
    opens = [_num(_row_field(p, "opened_at")) for p in positions]
    known = [o for o in opens if o is not None]
    if positions and len(known) == len(opens):
        fill_since: Optional[float] = min(known) - ENTRY_LOOKBACK_S
    else:
        # 有仓缺 opened_at（老库/异常行）→ 无法定下界，宁可全量读，不漏成交
        fill_since = None if positions else since
    fills = load_fills(conn, fill_since)
    by_coin: Dict[str, List[Dict[str, Any]]] = {}
    for f in fills:
        for key in (f.get("coin"), f.get("symbol")):
            if key:
                by_coin.setdefault(str(key), []).append(f)

    rows = [reconcile_row(p, _match_fills(p, by_coin), r_tol, net_abs_tol, net_rel_tol)
            for p in positions]

    d_rs = [r["d_r"] for r in rows if r["d_r"] is not None]
    d_nets = [r["d_net"] for r in rows if r["d_net"] is not None]
    with_fills = [r for r in rows if r["n_fills"] > 0]
    summary = {
        "positions": len(rows),
        "positions_with_fills": len(with_fills),
        "positions_r_unresolved": sum(1 for r in rows if r["r_fill"] is None),
        "dr_p50": _pct(d_rs, 0.50), "dr_p90": _pct(d_rs, 0.90),
        "dr_max": max(d_rs) if d_rs else None,
        "dnet_p50": _pct(d_nets, 0.50), "dnet_p90": _pct(d_nets, 0.90),
        "dnet_max": max(d_nets) if d_nets else None,
        "r_diff_count": sum(1 for r in rows if R_DIFF in r["flags"]),
        "net_diff_count": sum(1 for r in rows if NET_DIFF in r["flags"]),
        "no_fills_count": sum(1 for r in rows if NO_FILLS in r["flags"]),
        "unresolved_fee_count": sum(1 for r in rows if UNRESOLVED_FEE in r["flags"]),
        "r_tol": r_tol, "net_abs_tol": net_abs_tol, "net_rel_tol": net_rel_tol,
    }
    return {"rows": rows, "summary": summary}


# ---------------------------------------------------------------- 输出

def _f(v: Optional[float], spec: str = "+.3f") -> str:
    return "-" if v is None else format(v, spec)


def format_text(report: Dict[str, Any], db_path: str, since: Optional[str]) -> str:
    s = report["summary"]
    lines = [
        f"# fill 对账报告  db={db_path}" + (f"  since={since}" if since else ""),
        f"# Δnet = net_fill − net_local；两侧手续费同源，所以 Δnet 反映价差/数量差异",
        "",
        (f"{'id':>5}  {'symbol':<10} {'开仓':<16} {'R_local':>8} {'R_fill':>8} {'ΔR':>7}"
         f" {'net_fill':>10} {'net_local':>10} {'Δnet':>8}  flags"),
    ]
    for r in report["rows"]:
        lines.append(
            f"{str(r['id']):>5}  {r['symbol']:<10} {_iso(r['opened_at']):<16}"
            f" {_f(r['r_local'], '+.2f'):>8} {_f(r['r_fill'], '+.2f'):>8}"
            f" {_f(r['d_r'], '.3f'):>7} {_f(r['net_fill'], '+.2f'):>10}"
            f" {_f(r['net_local'], '+.2f'):>10} {_f(r['d_net'], '.2f'):>8}"
            f"  {','.join(r['flags']) if r['flags'] else 'ok'}")
    lines += [
        "",
        f"汇总：仓位 {s['positions']}，其中 {s['positions_with_fills']} 个查到成交"
        f"（{s['no_fills_count']} 个 NO_FILLS）；R 无法计算 {s['positions_r_unresolved']} 个",
        f"ΔR 分布：p50 {_f(s['dr_p50'])}｜p90 {_f(s['dr_p90'])}｜max {_f(s['dr_max'])}"
        f"（阈值 {s['r_tol']}）→ 超阈值 {s['r_diff_count']} 个",
        f"Δnet 分布：p50 {_f(s['dnet_p50'], '+.2f')}｜p90 {_f(s['dnet_p90'], '+.2f')}"
        f"｜max {_f(s['dnet_max'], '+.2f')}（阈值 {s['net_abs_tol']} U 或 "
        f"{s['net_rel_tol'] * 100:.2f}%）→ 超阈值 {s['net_diff_count']} 个",
        f"非美元计价手续费（UNRESOLVED_FEE，未计入 fee_total）：{s['unresolved_fee_count']} 个仓位",
    ]
    return "\n".join(lines)


def _parse_since(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        d = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"--since 需要 YYYY-MM-DD，收到 {text!r}")
    return d.timestamp()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="只读 fill 对账报告：用 hl_fills 重算净 PnL/R 并与 positions 比对（不写库、不联网）")
    ap.add_argument("--db", required=True, help="sqlite 库路径（以 mode=ro 只读打开）")
    ap.add_argument("--since", default=None, help="只看该日期（UTC，YYYY-MM-DD）之后关闭的仓位")
    ap.add_argument("--r-tol", type=float, default=0.15, help="R 差异阈值（默认 0.15）")
    ap.add_argument("--net-abs-tol", type=float, default=1.0, help="净额绝对阈值，USDC（默认 1.0）")
    ap.add_argument("--net-rel-tol", type=float, default=0.0005,
                    help="净额相对阈值（默认 0.0005，即 5bp）")
    ap.add_argument("--json", default=None, metavar="PATH", help="同时把结果写成 JSON 文件")
    args = ap.parse_args(argv)

    since = _parse_since(args.since)
    try:
        conn = open_readonly(args.db)
    except (FileNotFoundError, sqlite3.Error) as e:
        # 报告工具：exit code 恒 0。库打不开也不炸，明确告知后给空报告。
        print(f"无法只读打开库：{e}（未生成任何对账行）", file=sys.stderr)
        return 0
    try:
        report = build_report(conn, since, args.r_tol, args.net_abs_tol, args.net_rel_tol)
    except Exception as e:                          # noqa: BLE001 - 报告工具不许把栈扔给用户
        print(f"对账途中异常，已降级为空报告：{type(e).__name__}: {e}", file=sys.stderr)
        report = {"rows": [], "summary": {
            "positions": 0, "positions_with_fills": 0, "positions_r_unresolved": 0,
            "dr_p50": None, "dr_p90": None, "dr_max": None,
            "dnet_p50": None, "dnet_p90": None, "dnet_max": None,
            "r_diff_count": 0, "net_diff_count": 0, "no_fills_count": 0,
            "unresolved_fee_count": 0, "r_tol": args.r_tol,
            "net_abs_tol": args.net_abs_tol, "net_rel_tol": args.net_rel_tol}}
    finally:
        conn.close()

    print(format_text(report, args.db, args.since))

    if args.json:
        payload = {
            "generated_at": time.time(),
            "generated_at_iso": _iso(time.time()),
            "db": os.path.abspath(args.db),
            "since": args.since,
            **report,
        }
        try:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            print(f"\nJSON 已写入 {args.json}")
        except OSError as e:
            print(f"JSON 写入失败（忽略）：{e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
