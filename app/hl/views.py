"""Hyperliquid 执行器后台监控页（服务器渲染 HTML + JSON API）。

公开入口（签名冻结，改动需同步 tests/test_hl_views.py）：
    status_payload(deps)       组装监控 JSON。**任何字段缺失/接口失败都不抛异常**。
    render_dashboard(payload)  JSON → 自包含 HTML（内联 <style>，零外部 CDN/JS）。
    build_router(deps)         路由：/hl、/api/hl/status、/api/hl/equity、/api/hl/snapshot

装配（在 app/main.py 里挂一次即可）：
    from app.hl.views import DashboardDeps, build_router
    app.include_router(build_router(DashboardDeps(
        db=db, config_fn=current_config, info=hl_info,
        address_fn=lambda: os.environ.get("HL_ADDRESS", ""),
        mode_fn=lambda: os.environ.get("HL_EXEC_MODE", "off"))))

本页只读、不持私钥、不发起任何写请求；鉴权交给外层网关或可选的 ?k=（见 HL_DASH_REQUIRE_ADMIN）。

对账口径（system.mismatch = True 表示需人工处理）：
    1) 有停在 intent/sent 超过 3 分钟的 hl_orders（进程崩溃/超时，结果未知）
    2) DB 里 status='open' 的仓位，快照里查不到对应 coin
    3) DB 里 hl_stop_oid 非空，但快照里没有该 coin 的 reduce_only 触发单
    4) 快照里有仓位，但 DB 没有任何对应 open 记录（手工/外部仓位）
    快照读不到时不判 2/3/4（避免误报），只报第 1 条，并在 network.error 说明。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

# 公开导出（含面板合并时复用的 _render_body / DASH_CSS；其余为原有公开 API）。
__all__ = [
    "SCHEMA_VERSION", "MAINNET_DEFAULT", "DashboardDeps",
    "status_payload", "render_dashboard", "build_router",
    "DASH_CSS", "_render_body",
]

SCHEMA_VERSION = 1
_DAY = 86400.0
MAINNET_DEFAULT = "https://api.hyperliquid.xyz"

_MODE_TXT = {"off": "只提醒", "dry": "影子", "live": "实盘"}
_MODE_CLS = {"off": "muted", "dry": "warn", "live": "neg"}
_PURPOSE_TXT = {"entry": "开仓", "stop": "止损", "exit": "平仓"}
_ORDER_ST_TXT = {"intent": "待发", "planned": "计划", "sent": "已发", "resting": "挂单",
                 "filled": "成交", "cancelled": "撤单", "failed": "失败", "skipped": "跳过"}
_ST_TXT = {"pending": "待确认", "open": "持仓中", "closed": "已平仓", "ignored": "已忽略"}
_EV_TXT = {"decision": "决策", "order": "下单", "reconcile": "对账", "kill": "熔断",
           "error": "错误", "heartbeat": "心跳"}


# ---------------------------------------------------------------- 依赖注入
@dataclass
class DashboardDeps:
    """监控页依赖。全部走可替换函数，便于测试注入假实现。"""
    db: Any
    config_fn: Callable[[], Any]        # 返回 app.config.Config
    info: Any                           # app.hl.info.HlInfo 实例；可为 None
    address_fn: Callable[[], str]       # HL 钱包地址；可为空串
    mode_fn: Callable[[], str]          # off | dry | live
    base_url_fn: Callable[[], str] = lambda: MAINNET_DEFAULT
    now_fn: Callable[[], float] = time.time


# ---------------------------------------------------------------- 小工具
def _num(v: Any, default: float = 0.0) -> float:
    """安全转 float：None/垃圾值/NaN/inf → default。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _opt_num(v: Any) -> Optional[float]:
    """安全转 float，保留 None。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _fmt(v: Any, dp: int = 2, dash: str = "—") -> str:
    f = _opt_num(v)
    return dash if f is None else f"{f:,.{dp}f}"


def _utc_day_start(ts: float) -> float:
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()


def _to_seconds(v: Any) -> Optional[float]:
    """交易所时间戳单位不统一（ms/s）：>1e12 视为毫秒。"""
    f = _opt_num(v)
    if f is None:
        return None
    return f / 1000.0 if f > 1e12 else f


def _fmt_dt(v: Any, pattern: str = "%Y-%m-%d %H:%M:%S") -> str:
    f = _to_seconds(v)
    if f is None:
        return "—"
    try:
        return dt.datetime.fromtimestamp(f, dt.timezone.utc).strftime(pattern) + " UTC"
    except (OverflowError, OSError, ValueError):
        return "—"


def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def _kv(label: str, value: str, cls: str = "") -> str:
    c = f" {cls}" if cls else ""
    return f"<div class='kv'><span class='k'>{label}</span><span class='v{c}'>{value}</span></div>"


def _badge(dot_cls: str, label: str, value_html: str) -> str:
    return (f"<span class='badge'><span class='{dot_cls}'>&#9679;</span> "
            f"{label} <b>{value_html}</b></span>")


def _td(text: str, cls: str = "") -> str:
    return f"<td class='{cls}'>{text}</td>" if cls else f"<td>{text}</td>"


def _rowd(r: Any) -> Dict[str, Any]:
    """sqlite Row / dict / 任意对象 → dict；脏数据返回空 dict（渲染永不因类型炸掉）。"""
    try:
        return dict(r)
    except Exception:
        return {}


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


def _call(fn: Optional[Callable], default: Any = None) -> Any:
    """调用一个可能为 None / 会抛异常的依赖函数。"""
    if fn is None:
        return default
    try:
        r = fn()
        return default if r is None else r
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 依赖调用失败：%s", e)
        return default


def _db_call(db: Any, name: str, *args) -> Optional[Any]:
    """调用 db.<name>(...)；方法缺失或抛异常都返回 None（面板必须能撑住坏数据）。

    注意 getattr 本身也要兜住：db 代理对象的 __getattr__ 可能直接抛异常。
    """
    try:
        fn = getattr(db, name, None)
    except Exception:                                              # noqa: BLE001
        return None
    if not callable(fn):
        return None
    try:
        return fn(*args)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] db.%s 失败：%s", name, e)
        return None


def _coin_of(symbol: Any) -> str:
    s = str(symbol or "").strip()
    return s.split("_")[0] if s else ""


# 显示口径：信号层用的是 MEXC 的合约名（BTC_USDT），但真实成交发生在 Hyperliquid，
# 那里合约就叫 BTC / ETH。面板一律显示 HL 侧的名字，免得看的人以为在交易 USDT 对。
# 内部键（signal_bar_ms、幂等键、DB 里的 symbol）保持不动 —— 只换展示。
_DISP_COIN = {"BTC_USDT": "BTC", "ETH_USDT": "ETH"}


def _disp(sym: Any) -> str:
    s = str(sym or "").strip()
    if not s:
        return ""
    if s in _DISP_COIN:
        return _DISP_COIN[s]
    for suf in ("_USDT", "_USDC", "_USD"):
        if s.upper().endswith(suf):
            return s[: -len(suf)]
    return s


def _disp_text(text: Any) -> str:
    """展示用：把日志文案里的信号层合约名换成 HL 侧合约名（只替换已知映射）。

    DB 里存的原文本不动（审计保真），只有页面渲染走这里 ——
    否则交易后事件列表会出现「BTC_USDT 已开仓 0.00833 BTC」这种混着两种名字的句子。
    """
    out = str(text or "")
    for k, v in _DISP_COIN.items():
        out = out.replace(k, v)
    return out


def _addr_text(a: Any) -> str:
    s = str(a or "").strip()
    if not s:
        return "<span class='muted'>未配置</span>"
    return f"<span class='mono' title='{_esc(s)}'>{_esc(s[:6])}&hellip;{_esc(s[-4:])}</span>"


# ---------------------------------------------------------------- JSON 组装
def _config_get(deps: DashboardDeps, key: str, default: str = "") -> str:
    """读 configuration 表的一个字符串值（DB 里存的是 json.dumps 过的）。

    优先用 db.get_config_str（已解 JSON）；没有该方法时退回 get_config + json.loads。
    """
    miss = "\x00__MISS__"
    v = _db_call(deps.db, "get_config_str", key, miss)
    if isinstance(v, str) and v != miss:
        return v
    raw = _db_call(deps.db, "get_config") or {}
    try:
        raw_v = raw.get(key)
    except Exception:                                            # noqa: BLE001
        raw_v = None
    if raw_v in (None, ""):
        return default
    try:
        val = json.loads(raw_v)
    except (TypeError, ValueError):
        return str(raw_v)
    return default if val is None else str(val)


def _kill_state(deps: DashboardDeps) -> tuple:
    try:
        on = bool(deps.db.hl_kill())
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] hl_kill 失败：%s", e)
        on = False
    return on, _config_get(deps, "hl_exec_kill_reason")


def _reconcile(reachable: bool, snap_positions: List[dict], snap_orders: List[dict],
               open_by_coin: Dict[str, dict], stuck_orders: List[Any]) -> bool:
    """对账（口径见模块 docstring）。"""
    if stuck_orders:
        return True
    if not reachable:
        return False
    snap_coins = {str(p.get("coin") or "") for p in snap_positions if str(p.get("coin") or "")}
    for coin, row in open_by_coin.items():
        if coin not in snap_coins:
            return True
        if row.get("hl_stop_oid"):
            has_stop = any(str(o.get("coin") or "") == coin
                           and o.get("reduce_only") and o.get("is_trigger")
                           for o in snap_orders)
            if not has_stop:
                return True
    for coin in snap_coins:
        if coin not in open_by_coin:
            return True
    return False


def status_payload(deps: DashboardDeps) -> dict:
    """组装监控 JSON（schema_version=1）。任何字段缺失/接口失败都不抛异常。"""
    now = _num(_call(deps.now_fn), time.time())
    address = ""
    try:
        address = str(deps.address_fn() or "")
    except Exception:                                             # noqa: BLE001
        address = ""
    base_url = str(_call(deps.base_url_fn, MAINNET_DEFAULT) or MAINNET_DEFAULT)
    mode = str(_call(deps.mode_fn, "off") or "off")
    cfg = _call(deps.config_fn)

    # ---------------- 交易所快照 ----------------
    snap: Optional[dict] = None
    net_err = ""
    t0 = time.time()
    if not address:
        net_err = "未配置钱包地址"
    elif deps.info is None:
        net_err = "未配置只读客户端"
    else:
        try:
            snap = deps.info.snapshot(address)
        except Exception as e:                                    # noqa: BLE001
            net_err = f"{type(e).__name__}: {e}"
            logger.warning("[hl-views] snapshot 失败：%s", e)
    latency_ms = round((time.time() - t0) * 1000.0, 1)
    reachable = bool(snap is not None and not net_err)
    snap = snap or {}
    snap_positions: List[dict] = snap.get("positions") or []
    snap_orders: List[dict] = snap.get("open_orders") or []

    # ---------------- DB 侧 ----------------
    db_rows = _db_call(deps.db, "recent_positions", 50) or []
    open_by_coin: Dict[str, dict] = {}
    for r in db_rows:
        d = _rowd(r)
        coin = _coin_of(d.get("symbol"))
        if coin and str(d.get("status")) == "open":
            open_by_coin[coin] = d

    # ---------------- 持仓（快照为主，补 DB 的 signal/stop 信息）----------------
    positions_out: List[dict] = []
    for p in snap_positions:
        d = _rowd(p)
        coin = str(d.get("coin") or "")
        row = open_by_coin.get(coin)
        size = _num(d.get("size"))
        entry = _num(d.get("entry_px"))
        mark = _num(d.get("mark_px"))
        upnl = _num(d.get("unrealized_pnl"))

        stop_px: Optional[float] = None
        has_stop = False
        for o in snap_orders:
            od = _rowd(o)
            if str(od.get("coin") or "") != coin:
                continue
            if od.get("reduce_only") and od.get("is_trigger"):
                tp = _opt_num(od.get("trigger_px"))
                if tp and tp > 0:
                    stop_px, has_stop = tp, True
                    break

        risk = abs(entry - stop_px) * size if (stop_px and entry and size) else 0.0
        r_unrealized = round(upnl / risk, 4) if risk > 0 else 0.0
        if has_stop and stop_px and mark:
            stop_dist_pct = abs(stop_px - mark) / mark
        elif row is not None:
            stop_dist_pct = _num(row.get("stop_dist"))
        else:
            stop_dist_pct = 0.0

        try:
            side = int(d.get("side")) if d.get("side") not in (None, "") else (1 if size >= 0 else -1)
        except (TypeError, ValueError):
            side = 1 if size >= 0 else -1

        positions_out.append({
            "coin": coin,
            "side": side,
            "size": size,
            "entry_px": entry,
            "mark_px": mark,
            "position_value": _num(d.get("position_value")),
            "liq_px": _num(d.get("liq_px")),
            "leverage": int(_num(d.get("leverage"), 1)),
            "isolated": bool(d.get("isolated")),
            "margin_used": _num(d.get("margin_used")),
            "unrealized_pnl": upnl,
            "stop_px": stop_px,
            "has_stop": has_stop,
            "stop_dist_pct": stop_dist_pct,
            "r_unrealized": r_unrealized,
            "signal_bar_ms": int(row.get("signal_bar_ms")) if row is not None else None,
            "position_id": int(row.get("id")) if row is not None else None,
        })

    # ---------------- 挂单 ----------------
    orders_open: List[dict] = []
    for o in snap_orders:
        od = _rowd(o)
        orders_open.append({
            "coin": str(od.get("coin") or ""),
            "oid": od.get("oid"),
            "is_buy": bool(od.get("is_buy")),
            "sz": _num(od.get("sz")),
            "limit_px": _num(od.get("limit_px")),
            "reduce_only": bool(od.get("reduce_only")),
            "trigger_px": _opt_num(od.get("trigger_px")) or 0.0,
            "is_trigger": bool(od.get("is_trigger")),
            "timestamp": _to_seconds(od.get("timestamp")),
        })

    orders_recent: List[dict] = []
    for r in (_db_call(deps.db, "hl_recent_orders", 30) or []):
        d = _rowd(r)
        orders_recent.append({
            "symbol": d.get("symbol"),
            "coin": d.get("coin"),
            "purpose": d.get("purpose"),
            "status": d.get("status"),
            "sz": _opt_num(d.get("sz")),
            "px": _opt_num(d.get("px")),
            "oid": d.get("oid"),
            "filled_sz": _opt_num(d.get("filled_sz")),
            "avg_px": _opt_num(d.get("avg_px")),
            "error": d.get("error"),
            "updated_at": _opt_num(d.get("updated_at")),
        })

    # ---------------- 成交 / 盈亏 ----------------
    fills_rows = _db_call(deps.db, "hl_fills_since", 0.0)
    if fills_rows is None:
        fills_rows = _db_call(deps.db, "hl_recent_fills", 100000) or []
    fills_out: List[dict] = []
    today_realized = 0.0
    total_realized = 0.0
    fees_total = 0.0
    day_start = _utc_day_start(now)
    for r in (fills_rows or []):
        d = _rowd(r)
        cp = _num(d.get("closed_pnl"))
        total_realized += cp
        fees_total += _num(d.get("fee"))
        t_s = _to_seconds(d.get("ts"))
        if t_s is not None and day_start <= t_s < day_start + _DAY:
            today_realized += cp
        fills_out.append({
            "ts": _opt_num(d.get("ts")),
            "coin": d.get("coin"),
            "side": d.get("side"),
            "sz": _opt_num(d.get("sz")),
            "px": _opt_num(d.get("px")),
            "fee": _opt_num(d.get("fee")),
            "dir": d.get("dir"),
            "closed_pnl": _opt_num(d.get("closed_pnl")),
            "reduce_only": bool(d.get("reduce_only")),
        })

    funding_total = sum(_num(d.get("funding_since_open")) for d in (_rowd(p) for p in snap_positions))
    unrealized_pnl = sum(p["unrealized_pnl"] for p in positions_out)

    # ---------------- 信号 ----------------
    signals: List[dict] = []
    for r in db_rows:
        d = _rowd(r)
        entry = d.get("actual_entry")
        if entry in (None, ""):
            entry = d.get("entry_price")
        stop = d.get("actual_stop")
        if stop in (None, ""):
            stop = d.get("stop_price")
        signals.append({
            "symbol": d.get("symbol"),          # 内部键（信号层 MEXC 名），保留不变
            "coin": _disp(d.get("symbol")),     # 展示用：HL 侧合约名
            "status": d.get("status"),
            "side": int(_num(d.get("side"), 1)),
            "entry": _opt_num(entry),
            "stop": _opt_num(stop),
            "contracts": d.get("contracts"),
            "bar_ms": _opt_num(d.get("signal_bar_ms")),
        })

    # ---------------- 权益曲线 ----------------
    snaps = _db_call(deps.db, "hl_snapshots", now - 30 * _DAY, 20000) or []
    curve_30: List[List[float]] = []
    for s in snaps:
        d = _rowd(s)
        t = _opt_num(d.get("ts"))
        if t is not None:
            curve_30.append([t, _num(d.get("equity"))])
    curve_7 = [[t, e] for t, e in curve_30 if t >= now - 7 * _DAY]

    # ---------------- 事件 / 系统 ----------------
    events: List[dict] = []
    for r in (_db_call(deps.db, "hl_recent_events", 30) or []):
        d = _rowd(r)
        events.append({
            "ts": _opt_num(d.get("created_at")),
            "kind": d.get("kind"),
            "symbol": d.get("symbol"),
            "message": d.get("message"),
        })

    feed: List[dict] = []
    for r in (_db_call(deps.db, "feed_status") or []):
        d = _rowd(r)
        ok = _opt_num(d.get("last_ok_at")) is not None
        feed.append({
            "symbol": d.get("symbol"),          # 内部键，保留
            "coin": _disp(d.get("symbol")),     # 展示用
            "source": d.get("source"),
            "last_ok_at": _opt_num(d.get("last_ok_at")),
            "last_error": d.get("last_error"),
            "ok": ok,
        })

    symbols: List[str] = []
    try:
        symbols = [str(s) for s in (list(getattr(cfg, "symbols", []) or []))]
    except Exception:                                            # noqa: BLE001
        symbols = []
    last_bar: Dict[str, Optional[int]] = {}
    for s in symbols:
        v = _db_call(deps.db, "get_last_bar", s)
        last_bar[s] = int(v) if v else None

    stuck = _db_call(deps.db, "hl_stuck_orders", 180.0) or []
    executor_last_event_at = _opt_num(_db_call(deps.db, "hl_last_event_at"))

    kill, kill_reason = _kill_state(deps)

    return {
        "schema_version": SCHEMA_VERSION,
        "ts": now,
        "mode": mode,
        "kill": bool(kill),
        "kill_reason": kill_reason,
        "network": {
            "base_url": base_url,
            "testnet": "testnet" in base_url.lower(),
            "reachable": bool(reachable),
            "latency_ms": latency_ms,
            "error": net_err,
        },
        "account": {
            "address": address,
            "equity": _num(snap.get("equity_total", snap.get("equity"))),   # 总值（统一账户友好）
            "perp_equity": _num(snap.get("equity")),                        # 永续口径（原字段语义）
            "spot_usdc": _num(snap.get("spot_usdc")),
            "unified": bool(snap.get("unified", False)),
            "withdrawable": _num(snap.get("withdrawable")),
            "margin_used": _num(snap.get("margin_used")),
            "notional": _num(snap.get("notional")),
            "unrealized_pnl": unrealized_pnl,
            "today_realized": today_realized,
            "total_realized": total_realized,
            "fees_total": fees_total,
            "funding_total": funding_total,
        },
        "positions": positions_out,
        "orders_open": orders_open,
        "orders_recent": orders_recent,
        "fills": fills_out,
        "signals": signals,
        "system": {
            "feed": feed,
            "last_bar": last_bar,
            "executor_last_event_at": executor_last_event_at,
            "stuck_orders": len(stuck),
            "mismatch": _reconcile(reachable, snap_positions, snap_orders,
                                   open_by_coin, stuck),
        },
        "equity_curve": {"7d": curve_7, "30d": curve_30},
        "events": events,
    }


# ---------------------------------------------------------------- SVG 权益曲线
def _equity_svg(points: List[List[float]], width: int = 760, height: int = 160) -> str:
    """手写内联 SVG 折线（不引任何图表库）。"""
    pad_l, pad_r, pad_t, pad_b = 6, 6, 12, 16
    if len(points) < 2:
        return (f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' "
                f"aria-label='权益曲线'>"
                f"<text x='{width / 2}' y='{height / 2}' text-anchor='middle' "
                f"fill='#8b95a5' font-size='12'>暂无权益快照（等执行器落一次 hl_snapshots）</text></svg>")

    xs = [_num(p[0]) for p in points]
    ys = [_num(p[1]) for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    span_x = (x1 - x0) or 1.0
    span_y = (y1 - y0) or (abs(y0) * 0.02 or 1.0)

    def sx(x: float) -> float:
        return round(pad_l + (x - x0) / span_x * (width - pad_l - pad_r), 1)

    def sy(y: float) -> float:
        return round(height - pad_b - (y - y0) / span_y * (height - pad_t - pad_b), 1)

    pts = " ".join(f"{sx(x)},{sy(y)}" for x, y in zip(xs, ys))
    base_y = round(height - pad_b, 1)
    area = f"{sx(xs[0])},{base_y} {pts} {sx(xs[-1])},{base_y}"
    grid = "".join(
        f"<line x1='{pad_l}' y1='{round(pad_t + f * (height - pad_t - pad_b), 1)}' "
        f"x2='{width - pad_r}' y2='{round(pad_t + f * (height - pad_t - pad_b), 1)}' "
        f"stroke='#262a33' stroke-width='1'/>"
        for f in (0.25, 0.5, 0.75))
    first_y = round(sy(ys[0]), 1)
    last_x, last_y = sx(xs[-1]), sy(ys[-1])
    return (
        f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' aria-label='权益曲线'>"
        + grid
        + f"<line x1='{pad_l}' y1='{first_y}' x2='{width - pad_r}' y2='{first_y}' "
        f"stroke='#8b95a5' stroke-width='1' stroke-dasharray='4 4' opacity='0.45'/>"
        + f"<polygon points='{area}' fill='#60a5fa' opacity='0.13'/>"
        + f"<polyline points='{pts}' fill='none' stroke='#60a5fa' stroke-width='2' "
        f"stroke-linejoin='round' stroke-linecap='round'/>"
        + f"<circle cx='{last_x}' cy='{last_y}' r='3.5' fill='#60a5fa'/>"
        + "</svg>")


# ---------------------------------------------------------------- HTML 渲染
DASH_CSS = """<style>
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
     background:#0f1115;color:#e6e6e6;line-height:1.55;font-size:14px;padding:14px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:19px;margin:0 0 10px;font-weight:600}
h2{font-size:12px;margin:20px 0 8px;color:#9ad;letter-spacing:1.5px}
.card{background:#171a21;border:1px solid #262a33;border-radius:10px;padding:12px 14px;margin-bottom:12px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.badge{display:inline-flex;align-items:center;gap:6px;background:#171a21;border:1px solid #262a33;
       border-radius:999px;padding:6px 12px;font-size:12px;white-space:nowrap}
.badge b{font-size:13px;font-weight:600}
.sec{font-size:11px;color:#8b95a5;letter-spacing:1.5px;margin-bottom:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:2px 20px}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:13px;padding:4px 0;
    border-bottom:1px solid #1e222b}
.kv:last-child{border-bottom:0}
.kv .k{color:#8b95a5;white-space:nowrap}
.kv .v{font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
.pos{color:#4ade80}.neg{color:#f87171}.warn{color:#fbbf24}.muted{color:#8b95a5;font-size:12px}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;
            border:1px solid #262a33;border-radius:10px;background:#171a21}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border-bottom:1px solid #262a33;padding:6px 9px;text-align:left;white-space:nowrap}
thead th{color:#8b95a5;font-weight:500;font-size:11.5px;background:#14171e}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.banner{border:1px solid #7f1d1d;background:#2a1215;color:#fca5a5;border-radius:10px;
        padding:10px 14px;margin-bottom:12px;font-size:13px}
.banner .muted{color:#c99}
.chart-wrap{background:#171a21;border:1px solid #262a33;border-radius:10px;padding:10px 12px 6px}
.chart-head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;
            font-size:12px;color:#8b95a5;margin-bottom:2px}
.chart-head b{color:#e6e6e6}
svg.chart{display:block;width:100%;height:160px}
.btn{display:inline-block;background:#2563eb;color:#fff;border-radius:6px;padding:5px 11px;
     font-size:12px;text-decoration:none;white-space:nowrap}
.btn.gray{background:#374151}
.foot{margin:18px 0 6px;color:#8b95a5;font-size:12px;display:flex;flex-wrap:wrap;
      gap:8px;align-items:center}
.foot .spacer{flex:1}
a{color:#60a5fa}
@media (max-width:640px){body{padding:9px;font-size:13px}h1{font-size:17px}
    .badge{padding:5px 10px;font-size:11px}.grid{grid-template-columns:1fr}}
</style>"""


def _sign(v: Any, unit: str = " U") -> tuple:
    """返回 (带符号文本, 颜色 class)。"""
    f = _opt_num(v)
    if f is None:
        return "—", ""
    cls = "pos" if f > 0 else ("neg" if f < 0 else "")
    return f"{f:+,.2f}{unit}", cls


def _render_body(p: dict) -> str:
    mode = str(p.get("mode") or "off")
    net = _as_dict(p.get("network"))
    acc = _as_dict(p.get("account"))
    system = _as_dict(p.get("system"))
    positions = _as_list(p.get("positions"))
    orders_open = _as_list(p.get("orders_open"))
    orders_recent = _as_list(p.get("orders_recent"))
    fills = _as_list(p.get("fills"))
    signals = _as_list(p.get("signals"))
    events = _as_list(p.get("events"))
    curve = _as_dict(p.get("equity_curve"))
    kill = bool(p.get("kill"))
    kill_reason = p.get("kill_reason") or ""
    reachable = bool(net.get("reachable"))
    latency = _opt_num(net.get("latency_ms"))
    error = str(net.get("error") or "")
    mismatch = bool(system.get("mismatch"))
    stuck = _opt_num(system.get("stuck_orders"))
    curve7 = _as_list(curve.get("7d"))
    curve30 = _as_list(curve.get("30d"))
    shown = [x for x in (curve7 if len(curve7) >= 2 else curve30)
             if isinstance(x, (list, tuple)) and len(x) >= 2]

    o: List[str] = []
    o.append("<h1>Hyperliquid 执行器监控</h1>")

    # ---------------- 顶部状态条 ----------------
    o.append("<div class='bar'>")
    o.append(_badge(_MODE_CLS.get(mode, "muted"), "模式", _esc(_MODE_TXT.get(mode, mode))))
    if kill:
        extra = f" <span class='muted'>{_esc(kill_reason)}</span>" if kill_reason else ""
        o.append(f"<span class='badge'><span class='neg'>&#9679;</span> 熔断 <b>已触发</b>{extra}</span>")
    else:
        o.append(_badge("pos", "熔断", "未触发"))
    o.append("<span class='badge'>" + (
        f"<span class='pos'>&#9679;</span> 对账 <b>一致</b>" if not mismatch
        else f"<span class='neg'>&#9679;</span> 对账 <b>不一致</b>"
    ) + "</span>")
    lat_txt = f"{latency:.0f} ms" if latency is not None else "—"
    # 这是「服务器 → Hyperliquid」的耗时（容器每次拉账户快照的开销），不是你到面板的网络延迟。
    o.append(f"<span class='badge' title='oracle1 上的容器请求 Hyperliquid /info 的耗时（含失败重试）；"
             f"与你的浏览器到本站的延迟无关'>"
             f"<span class='pos'>&#9679;</span> HL 延迟 <b>{lat_txt}</b></span>")
    o.append(f"<span class='badge'><span class='muted'>&#9679;</span> 更新 "
             f"<b>{_fmt_dt(p.get('ts'))}</b></span>")
    o.append("</div>")

    if not reachable:
        o.append(f"<div class='banner'>&#9888;&#65039; 未配置 / 读取失败：{_esc(error or '未知原因')}<br>"
                 f"<span class='muted'>交易所侧数据暂不可用；下方仍显示数据库里的信号、下单与成交记录。</span></div>")

    # ---------------- 账户卡片 ----------------
    o.append("<div class='card'><div class='sec'>账户</div><div class='grid'>")
    o.append(_kv("钱包地址", _addr_text(acc.get("address"))))
    unreal_txt, unreal_cls = _sign(acc.get("unrealized_pnl"))
    today_txt, today_cls = _sign(acc.get("today_realized"))
    total_txt, total_cls = _sign(acc.get("total_realized"))
    fund_txt, fund_cls = _sign(acc.get("funding_total"))
    fees = _num(acc.get("fees_total"))
    o.append(_kv("账户总值", f"{_fmt(acc.get('equity'))} U"))
    if acc.get("unified"):
        # 统一账户：钱在现货状态里，永续口径恒为 0 —— 说清楚，免得以为「没资金」
        o.append(_kv("　现货 USDC（抵押品）", f"{_fmt(acc.get('spot_usdc'))} U", "muted"))
        o.append(_kv("　永续账户口径", f"{_fmt(acc.get('perp_equity'))} U", "muted"))
    wd = _num(acc.get("withdrawable"))
    if acc.get("unified") and wd <= 0:
        # 统一账户下永续口径的 withdrawable 恒为 0（钱在现货状态里）→ 直接显示 0 会误导成「没钱」。
        # 真实可提 = 现货 USDC；HL 提现固定收 1 USDC 手续费（与金额无关）。
        o.append(_kv("可提余额（现货口径，提现固定扣 1U）",
                     f"{_fmt(_num(acc.get('spot_usdc')))} U"))
    else:
        o.append(_kv("可提余额", f"{_fmt(wd)} U"))
    o.append(_kv("已用保证金", f"{_fmt(acc.get('margin_used'))} U"))
    o.append(_kv("持仓名义", f"{_fmt(acc.get('notional'))} U"))
    o.append(_kv("浮盈浮亏", unreal_txt, unreal_cls))
    o.append(_kv("今日已实现", today_txt, today_cls))
    o.append(_kv("累计已实现", total_txt, total_cls))
    o.append(_kv("资金费率累计", fund_txt, fund_cls))
    o.append(_kv("累计手续费", f"{-fees:+,.2f} U", "neg" if fees > 0 else ""))
    o.append("</div></div>")

    # ---------------- 权益曲线 ----------------
    o.append("<h2>权益曲线</h2>")
    o.append("<div class='chart-wrap'>")
    if len(shown) >= 2:
        first_e = _opt_num(shown[0][1])
        last_e = _opt_num(shown[-1][1])
        chg = ""
        if first_e:
            chg = f"　{((last_e - first_e) / abs(first_e) * 100.0):+.2f}%"
        o.append(f"<div class='chart-head'><span>{'近 7 天' if len(curve7) >= 2 else '近 30 天'}"
                 f"（{len(shown)} 个快照）</span>"
                 f"<span>起点 {_fmt(first_e)} U &rarr; 最新 <b>{_fmt(last_e)} U</b>{chg}</span></div>")
    else:
        o.append("<div class='chart-head'><span class='muted'>暂无数据</span></div>")
    o.append(_equity_svg(shown))
    o.append("</div>")

    # ---------------- 仓位 ----------------
    o.append("<h2>仓位</h2>")
    o.append("<div class='tablewrap'><table><thead><tr>"
             "<th>币种</th><th>方向</th><th class='num'>数量</th><th class='num'>入场</th>"
             "<th class='num'>标记</th><th class='num'>浮盈浮亏</th><th class='num'>R</th>"
             "<th class='num'>止损</th><th class='num'>止损距离</th><th class='num'>杠杆</th>"
             "<th class='num'>保证金</th><th class='num'>强平价</th>"
             "</tr></thead><tbody>")
    for pos in (_as_dict(x) for x in positions):
        upnl_txt, upnl_cls = _sign(pos.get("unrealized_pnl"))
        side_txt = "多" if _num(pos.get("side"), 1) >= 0 else "空"
        dist = _opt_num(pos.get("stop_dist_pct"))
        r_txt, r_cls = _sign(pos.get("r_unrealized"), "R")
        if pos.get("has_stop"):
            stop_cell = f"<span class='pos'>&#10004;&#65039; {_fmt(pos.get('stop_px'))}</span>"
        else:
            stop_cell = "<span class='neg'>&#10060; 无</span>"
        o.append("<tr>"
                 + _td(f"<b>{_esc(pos.get('coin') or '—')}</b>")
                 + _td(f"<b>{side_txt}</b>")
                 + _td(_fmt(pos.get("size")), "num")
                 + _td(_fmt(pos.get("entry_px")), "num")
                 + _td(_fmt(pos.get("mark_px")), "num")
                 + _td(upnl_txt, f"num {upnl_cls}".strip())
                 + _td(r_txt, f"num {r_cls}".strip())
                 + _td(stop_cell, "num")
                 + _td(f"{dist * 100:.2f}%" if dist is not None else "—", "num")
                 + _td(f"{_num(pos.get('leverage'), 1):.0f}x"
                       + (" <span class='muted'>逐</span>" if pos.get("isolated") else ""), "num")
                 + _td(_fmt(pos.get("margin_used")), "num")
                 + _td(_fmt(pos.get("liq_px")) if _opt_num(pos.get("liq_px")) else "—", "num")
                 + "</tr>")
    if not positions:
        o.append(f"<tr><td colspan='12' class='muted'>"
                 f"{'交易所无持仓' if reachable else '交易所数据暂不可读'}"
                 f"（信号见下方「信号」）</td></tr>")
    o.append("</tbody></table></div>")

    # ---------------- 挂单 ----------------
    o.append("<h2>交易所挂单</h2>")
    o.append("<div class='tablewrap'><table><thead><tr>"
             "<th>币种</th><th>类型</th><th>方向</th><th class='num'>数量</th>"
             "<th class='num'>限价</th><th class='num'>触发价</th><th class='num'>触发时间</th>"
             "</tr></thead><tbody>")
    for od in (_as_dict(x) for x in orders_open):
        kind = "止损触发" if od.get("is_trigger") else ("市价/限价" if od.get("limit_px") else "下单")
        if od.get("reduce_only"):
            kind = (kind + " <span class='muted'>(减仓)</span>")
        o.append("<tr>"
                 + _td(_esc(od.get("coin") or "—"))
                 + _td(kind)
                 + _td("买" if od.get("is_buy") else "卖")
                 + _td(_fmt(od.get("sz")), "num")
                 + _td(_fmt(od.get("limit_px")), "num")
                 + _td(_fmt(od.get("trigger_px")), "num")
                 + _td(_fmt_dt(od.get("timestamp")), "num")
                 + "</tr>")
    if not orders_open:
        o.append("<tr><td colspan='7' class='muted'>交易所无挂单</td></tr>")
    o.append("</tbody></table></div>")

    # ---------------- 下单记录 ----------------
    o.append("<h2>下单记录</h2>")
    o.append("<div class='tablewrap'><table><thead><tr>"
             "<th>时间</th><th>标的</th><th>用途</th><th>状态</th><th class='num'>数量</th>"
             "<th class='num'>下单价</th><th class='num'>成交量</th><th class='num'>均价</th>"
             "<th>错误</th></tr></thead><tbody>")
    for od in (_as_dict(x) for x in orders_recent):
        st = str(od.get("status") or "—")
        st_cls = "neg" if st == "failed" else ("warn" if st in ("intent", "sent") else "")
        err = str(od.get("error") or "")
        o.append("<tr>"
                 + _td(_fmt_dt(od.get("updated_at")), "num")
                 + _td(_esc(od.get("symbol") or "—"))
                 + _td(_esc(_PURPOSE_TXT.get(str(od.get("purpose")), od.get("purpose") or "—")))
                 + _td(f"<span class='{st_cls}'>{_esc(_ORDER_ST_TXT.get(st, st))}</span>")
                 + _td(_fmt(od.get("sz")), "num")
                 + _td(_fmt(od.get("px")), "num")
                 + _td(_fmt(od.get("filled_sz")), "num")
                 + _td(_fmt(od.get("avg_px")), "num")
                 + _td(f"<span class='muted'>{_esc(err[:60] or '—')}</span>")
                 + "</tr>")
    if not orders_recent:
        o.append("<tr><td colspan='9' class='muted'>暂无下单记录</td></tr>")
    o.append("</tbody></table></div>")

    # ---------------- 最近成交 ----------------
    o.append("<h2>最近成交</h2>")
    o.append("<div class='tablewrap'><table><thead><tr>"
             "<th>时间</th><th>币种</th><th>方向</th><th class='num'>数量</th>"
             "<th class='num'>价格</th><th class='num'>手续费</th><th>对手</th>"
             "<th class='num'>平仓盈亏</th><th>减仓</th></tr></thead><tbody>")
    for f in (_as_dict(x) for x in fills):
        cp_txt, cp_cls = _sign(f.get("closed_pnl"))
        d = str(f.get("dir") or "")
        o.append("<tr>"
                 + _td(_fmt_dt(f.get("ts")), "num")
                 + _td(_esc(f.get("coin") or "—"))
                 + _td("买" if _num(f.get("side"), 0) >= 0 else "卖")
                 + _td(_fmt(f.get("sz")), "num")
                 + _td(_fmt(f.get("px")), "num")
                 + _td(_fmt(f.get("fee"), 4), "num")
                 + _td({"a": "对手盘卖", "b": "对手盘买"}.get(d, _esc(d or "—")))
                 + _td(cp_txt, f"num {cp_cls}".strip())
                 + _td("是" if f.get("reduce_only") else "—")
                 + "</tr>")
    if not fills:
        o.append("<tr><td colspan='9' class='muted'>暂无成交记录</td></tr>")
    o.append("</tbody></table></div>")

    # ---------------- 信号 ----------------
    o.append("<h2>信号</h2>")
    o.append("<div class='tablewrap'><table><thead><tr>"
             "<th>标的</th><th>状态</th><th>方向</th><th class='num'>入场</th>"
             "<th class='num'>止损</th><th class='num'>张数</th><th>信号 bar</th>"
             "</tr></thead><tbody>")
    for sg in (_as_dict(x) for x in signals):
        st = str(sg.get("status") or "—")
        st_cls = {"open": "pos", "pending": "warn"}.get(st, "")
        o.append("<tr>"
                 + _td(_esc(sg.get("coin") or _disp(sg.get("symbol")) or "—"))
                 + _td(f"<span class='{st_cls}'>{_esc(_ST_TXT.get(st, st))}</span>")
                 + _td("多" if _num(sg.get("side"), 1) >= 0 else "空")
                 + _td(_fmt(sg.get("entry")), "num")
                 + _td(_fmt(sg.get("stop")), "num")
                 + _td(_fmt(sg.get("contracts"), 0), "num")
                 + _td(_fmt_dt(sg.get("bar_ms")), "num")
                 + "</tr>")
    if not signals:
        o.append("<tr><td colspan='7' class='muted'>暂无信号</td></tr>")
    o.append("</tbody></table></div>")

    # ---------------- 执行器事件 ----------------
    o.append("<h2>执行器事件</h2>")
    o.append("<div class='tablewrap'><table><thead><tr>"
             "<th>时间</th><th>类型</th><th>标的</th><th>内容</th></tr></thead><tbody>")
    for ev in (_as_dict(x) for x in events):
        msg = _disp_text(ev.get("message") or "")
        shown_msg = msg if len(msg) <= 180 else msg[:180] + "&hellip;"
        o.append("<tr>"
                 + _td(_fmt_dt(ev.get("ts")), "num")
                 + _td(_esc(_EV_TXT.get(str(ev.get("kind")), ev.get("kind") or "—")))
                 + _td(_esc(_disp(ev.get("symbol")) or "—"))
                 + _td(f"<span title='{_esc(msg)}'>{_esc(shown_msg)}</span>")
                 + "</tr>")
    if not events:
        o.append("<tr><td colspan='4' class='muted'>暂无执行器事件</td></tr>")
    o.append("</tbody></table></div>")

    # ---------------- 系统卡片 ----------------
    o.append("<h2>系统</h2>")
    o.append("<div class='card'><div class='grid'>")
    o.append(_kv("执行器最后事件", _fmt_dt(system.get("executor_last_event_at"))))
    o.append(_kv("卡在 intent/sent 的下单", f"{int(stuck or 0)} 笔",
                 "neg" if stuck else "muted"))
    o.append(_kv("对账", "一致" if not mismatch else "不一致", "pos" if not mismatch else "neg"))
    o.append(_kv("交易所地址", _esc(net.get("base_url") or "—")))
    o.append(_kv("环境", "测试网" if net.get("testnet") else "主网"))
    o.append(_kv("熔断", "已触发" if kill else "未触发", "neg" if kill else "pos"))
    o.append("</div>")
    feed = _as_list(system.get("feed"))
    if feed:
        o.append("<div style='margin-top:10px'><div class='sec'>数据源健康</div>")
        o.append("<div class='tablewrap'><table><thead><tr><th>标的</th><th>来源</th>"
                 "<th>最后成功</th><th>错误</th></tr></thead><tbody>")
        for fd in (_as_dict(x) for x in feed):
            ok = bool(fd.get("ok"))
            o.append("<tr>"
                     + _td(_esc(fd.get("coin") or _disp(fd.get("symbol")) or "—"))
                     + _td(f"<span class='{ 'pos' if ok else 'neg'}'>"
                           f"{'正常' if ok else '失败'}</span>")
                     + _td(_fmt_dt(fd.get("last_ok_at")), "num")
                     + _td(f"<span class='muted'>{_esc(str(fd.get('last_error') or '—')[:80])}</span>")
                     + "</tr>")
        o.append("</tbody></table></div></div>")
    last_bar = _as_dict(system.get("last_bar"))
    if last_bar:
        o.append("<div style='margin-top:10px'><div class='sec'>已处理到</div>")
        o.append("<div class='tablewrap'><table><thead><tr><th>标的</th><th>最后处理 bar</th>"
                 "</tr></thead><tbody>")
        for sym, bar_ms in last_bar.items():
            o.append("<tr>" + _td(_esc(_disp(sym))) + _td(_fmt_dt(bar_ms), "num") + "</tr>")
        o.append("</tbody></table></div></div>")
    o.append("</div>")

    return "".join(o)


def _render_footer(cur: int) -> str:
    links = []
    for n, label in ((0, "手动刷新"), (10, "每 10 秒"), (30, "每 30 秒"), (60, "每 60 秒")):
        href = "/hl" if n == 0 else f"/hl?refresh={n}"
        cls = "btn" if n == cur else "btn gray"
        links.append(f"<a class='{cls}' href='{href}'>{label}</a>")
    return ("<div class='foot'><span class='muted'>自动刷新：</span>"
            + " ".join(links)
            + "<span class='spacer'></span>"
            + f"<span class='muted'>本页只读、不持私钥 &middot; schema v{SCHEMA_VERSION}</span></div>")


def render_dashboard(payload: dict) -> str:
    """dict → 完整自包含 HTML 字符串（内联 <style>，零外部 CDN/JS）。永不抛异常。"""
    p = payload if isinstance(payload, dict) else {}
    try:
        body = _render_body(p)
    except Exception as e:                                        # noqa: BLE001
        logger.exception("[hl-views] render_dashboard 渲染失败：%s", e)
        body = (f"<h1>Hyperliquid 执行器监控</h1>"
                f"<div class='banner'>渲染失败：{_esc(e)}</div>")
    refresh = int(_num(p.get("refresh")))
    if refresh not in (10, 30, 60):
        refresh = 0
    meta = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh else ""
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{meta}<title>Hyperliquid 执行器监控</title>{DASH_CSS}</head>"
        f"<body><div class='wrap'>{body}{_render_footer(refresh)}</div></body></html>")


# ---------------------------------------------------------------- 路由
def _admin_required(deps: DashboardDeps, k: str) -> bool:
    """可选鉴权：仅 HL_DASH_REQUIRE_ADMIN=1 时生效（默认关闭，便于内网直连）。

    返回 True = 应拒绝（401）。未开启或未配 ADMIN_TOKEN 一律放行。
    """
    if os.environ.get("HL_DASH_REQUIRE_ADMIN", "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    token = ""
    cfg = _call(deps.config_fn)
    try:
        token = str(getattr(cfg, "admin_token", "") or "")
    except Exception:                                            # noqa: BLE001
        return False
    if not token:
        return False
    return str(k or "") != token


def build_router(deps: DashboardDeps) -> Any:
    """构建监控路由：/hl（HTML）、/api/hl/status、/api/hl/equity、/api/hl/snapshot。"""
    router = APIRouter(tags=["hl"])

    @router.get("/hl", response_class=HTMLResponse)
    def hl_page(request: Request, refresh: int = 0, k: str = ""):
        if _admin_required(deps, k):
            raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
        payload = dict(status_payload(deps))
        payload["refresh"] = refresh if refresh in (10, 30, 60) else 0
        return HTMLResponse(render_dashboard(payload))

    @router.get("/api/hl/status")
    def hl_status(request: Request, k: str = ""):
        if _admin_required(deps, k):
            raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
        return status_payload(deps)

    @router.get("/api/hl/equity")
    def hl_equity(request: Request, k: str = ""):
        if _admin_required(deps, k):
            raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
        p = status_payload(deps)
        a = p.get("account") or {}
        return {
            "ts": p.get("ts"),
            "mode": p.get("mode"),
            "kill": p.get("kill"),
            "reachable": bool((p.get("network") or {}).get("reachable")),
            "equity": a.get("equity"),
            "unrealized_pnl": a.get("unrealized_pnl"),
            "today_realized": a.get("today_realized"),
            "total_realized": a.get("total_realized"),
            "fees_total": a.get("fees_total"),
            "funding_total": a.get("funding_total"),
            "positions": p.get("positions") or [],
            "curve": p.get("equity_curve") or {},
        }

    @router.get("/api/hl/snapshot")
    def hl_snapshot(request: Request, k: str = ""):
        if _admin_required(deps, k):
            raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
        p = status_payload(deps)
        return {
            "ts": p.get("ts"),
            "network": p.get("network") or {},
            "account": p.get("account") or {},
            "positions": p.get("positions") or [],
            "orders_open": p.get("orders_open") or [],
        }

    return router
