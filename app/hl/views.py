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
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import posfields
from app.ui_theme import (ICON_LINKS, SYMBOL_CSS, THEME_CSS, normalize_theme, theme_attr,
                          theme_switch_html)

logger = logging.getLogger(__name__)

# 公开导出（含面板合并时复用的 _render_body / DASH_CSS；其余为原有公开 API）。
__all__ = [
    "SCHEMA_VERSION", "MAINNET_DEFAULT", "DashboardDeps",
    "status_payload", "render_dashboard", "build_router",
    "DASH_CSS", "_render_body", "THEME_CSS",
    "ROW_CHOICES", "DEFAULT_ROWS", "ROWS_COOKIE", "normalize_rows", "_signals_html",
]

# 长表每页行数（手机优先）：默认 20，面板上可切 10/20/50，选择存 cookie `cma_rows`。
# 只作用于「记录/历史」类长表（记录页的下单/成交/信号/提醒/事件五块）；权益曲线不受影响。
ROW_CHOICES = (10, 20, 50)
DEFAULT_ROWS = 20
ROWS_COOKIE = "cma_rows"

SCHEMA_VERSION = 1
_DAY = 86400.0
MAINNET_DEFAULT = "https://api.hyperliquid.xyz"

_MODE_TXT = {"off": "只提醒", "dry": "影子", "live": "实盘"}
_MODE_CLS = {"off": "muted", "dry": "warn", "live": "neg"}
# 信号表状态 → 中文（_signals_html 用；记录页复用同一映射）
_ST_TXT = {"pending": "待确认", "open": "持仓中", "closed": "已平仓", "ignored": "已忽略"}

# hl_status（执行器侧的账实状态）→ 中文。只在「不是 open」时才显示在状态列第二行 ——
# open 就是「持仓中」，再写一遍纯噪音。未知值兜底原文（不编一个看着像结论的词）。
_HL_ST_TXT = {"closing": "平仓待核对", "mismatch": "账实不符", "aborted": "开仓中止"}
# 宽限期（hl_close_grace_s）：与 Config/Executor 同一默认与范围（面板不 import main，
# 也不 import executor —— 这里只要一个「读配置 + 兜底」的小工具）。
_GRACE_DEFAULT_S = 900.0
_GRACE_MIN_S, _GRACE_MAX_S = 300.0, 3600.0

# 成交「买卖」列的四向推导（side 是多空方向不是买卖方向 —— 平多其实是卖出）。
# 总览的成交表 2026-10-18 迁到记录页后，这里是唯一口径源，记录页 import 复用
# （别在 admin_ui 里再抄一份）。
_FILL_BS = {"Open Long": "买", "Close Short": "买",
            "Close Long": "卖", "Open Short": "卖"}


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


def _opt_int(v: Any) -> Optional[int]:
    """安全转 int，保留 None（symbol_state.dense_cnt 这类可空整数）。"""
    f = _opt_num(v)
    return None if f is None else int(f)


def _fee_quoted(d: Dict[str, Any]) -> Optional[float]:
    """一笔成交的手续费（U 口径）：优先 ``fee_quote``（PR-1 写入时折算好的），
    为 NULL 时回退原始 ``fee``（PR-1 之前的历史行 / 非计价币行）。

    与执行器 ``_fees_since`` 严格同口径（#34 的读侧语义）：面板与净利计算看到同一个
    手续费数字，否则「面板说 0.35U、净利算 0.21U」永远对不上。
    """
    fq = _opt_num(d.get("fee_quote"))
    if fq is not None:
        return fq
    return _opt_num(d.get("fee"))


def _pnl_from_fills(cfg: Any) -> bool:
    """面板净额口径开关（hl_pnl_from_fills，默认关 = 交易所 closedPnl 口径）。

    默认关的理由见 ExecParams.pnl_from_fills：流水口径依赖 hl_fills 已同步完整，
    上线初期先让差异被记录，再决定是否切换读数。
    """
    try:
        return bool(getattr(cfg, "hl_pnl_from_fills", False))
    except Exception:                                            # noqa: BLE001
        return False


def _utc_day_start(ts: float) -> float:
    """当日 0 点（北京时间，与展示层「今日」一致；名字保留以免破坏调用方）。"""
    tz = _display_tz()
    d = dt.datetime.fromtimestamp(ts, tz).date()
    return dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()


def _to_seconds(v: Any) -> Optional[float]:
    """交易所时间戳单位不统一（ms/s）：>1e12 视为毫秒。"""
    f = _opt_num(v)
    if f is None:
        return None
    return f / 1000.0 if f > 1e12 else f


def _display_tz() -> dt.timezone:
    """展示时区（与 engine.DISPLAY_TZ 同源，env DISPLAY_TZ_OFFSET，默认北京时间）。

    用 float 解析（engine 侧也是 float）：int("8.0") 会抛 ValueError 打穿面板。
    """
    try:
        off = float(os.environ.get("DISPLAY_TZ_OFFSET", "8") or 8)
    except (TypeError, ValueError):
        off = 8.0
    return dt.timezone(dt.timedelta(hours=off))


def _fmt_dt(v: Any, pattern: str = "%m-%d %H:%M:%S") -> str:
    """时间展示：北京时间（原为 UTC，与 admin 面板/推送口径不一致，已统一）。"""
    f = _to_seconds(v)
    if f is None:
        return "—"
    try:
        return dt.datetime.fromtimestamp(f, _display_tz()).strftime(pattern)
    except (OverflowError, OSError, ValueError):
        return "—"


def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def normalize_rows(v: Any) -> int:
    """cookie/查询参数 → 合法行数；认不出一律回落默认（不改用途，只影响展示）。"""
    try:
        n = int(str(v or "").strip())
    except (TypeError, ValueError):
        return DEFAULT_ROWS
    return n if n in ROW_CHOICES else DEFAULT_ROWS


def _fmt_sz(v: Any) -> str:
    """数量展示：BTC 这类小数量按精度显示（0.00095 不能显示成 0.00）。"""
    f = _opt_num(v)
    if f is None:
        return "—"
    for dp in (2, 3, 4, 5, 6):
        if f == round(f, dp):
            return f"{f:,.{dp}f}".rstrip("0").rstrip(".") if dp > 2 else f"{f:,.2f}"
    return f"{f:,.6f}"


def _kv(label: str, value: str, cls: str = "", title: str = "") -> str:
    c = f" {cls}" if cls else ""
    t = f" title='{_esc(title)}'" if title else ""
    return f"<div class='kv'{t}><span class='k'>{label}</span><span class='v{c}'>{value}</span></div>"


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
               open_by_coin: Dict[str, dict], stuck_orders: List[Any],
               orders_trusted: bool = True) -> bool:
    """对账（口径见模块 docstring）。

    `orders_trusted=False`（挂单接口回落/非 list）时**跳过止损判断**——
    与执行器同一口径：读不到 isTrigger 时把「有止损」读成「没止损」，
    据此判 mismatch 会吓用户去手动平仓。
    """
    if stuck_orders:
        return True
    if not reachable:
        return False
    snap_coins = {str(p.get("coin") or "") for p in snap_positions if str(p.get("coin") or "")}
    for coin, row in open_by_coin.items():
        if coin not in snap_coins:
            return True
        if row.get("hl_stop_oid") and orders_trusted:
            has_stop = any(str(o.get("coin") or "") == coin
                           and o.get("reduce_only") and o.get("is_trigger")
                           for o in snap_orders)
            if not has_stop:
                return True
    for coin in snap_coins:
        if coin not in open_by_coin:
            return True
    return False


def _symbol_state_rows(deps: DashboardDeps, db_rows: List[Any],
                       symbols: List[str]) -> List[dict]:
    """symbol_state 表 → 「三标的动态」每标的一条（缺行/缺字段一律 None，永不抛异常）。

    顺序与配置的标的顺序一致；`position_side` 只看 status='open' 的仓位行
    （pending 不算仓位，与「仓位」表同口径）。
    """
    cached: Dict[str, dict] = {}
    for r in (_db_call(deps.db, "symbol_states") or []):
        d = _rowd(r)
        sym = str(d.get("symbol") or "")
        if sym:
            cached[sym] = d
    open_side: Dict[str, int] = {}
    for r in db_rows:
        d = _rowd(r)
        if str(d.get("status")) == "open":
            sym = str(d.get("symbol") or "")
            if sym:
                open_side[sym] = int(_num(d.get("side"), 1))
    out: List[dict] = []
    for sym in symbols:
        d = cached.get(sym) or {}
        out.append({
            "symbol": sym,                       # 内部键（保留）
            "coin": _disp(sym),                  # 展示用：HL 侧合约名
            "price": _opt_num(d.get("price")),
            "sma20": _opt_num(d.get("sma20")),
            "sma200": _opt_num(d.get("sma200")),
            "long_trigger": _opt_num(d.get("long_trigger")),
            "short_trigger": _opt_num(d.get("short_trigger")),
            "dense_cnt": _opt_int(d.get("dense_cnt")),
            "updated_at": _opt_num(d.get("updated_at")),
            "position_side": open_side.get(sym),
        })
    return out


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
        trusted = bool(snap.get("orders_trusted", False))
        if trusted:
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
            side = int(d.get("side")) if d.get("side") not in (None, "") else 1
        except (TypeError, ValueError):
            side = 1
        # 注：size 是绝对值，不能用来推方向（旧兜底恒返回 1）；方向以快照 side 为准。

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
            "stop_unknown": has_stop is False and not trusted,   # 挂单不可信：显示「无法确认」而非 ❌
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
    for r in (_db_call(deps.db, "hl_recent_orders", 50) or []):
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
    # 审计三轮 M8：hl_fills_since 是 ORDER BY id ASC（最旧在前），行数超过截断数后
    # 「最近成交」会全是最老数据。改用 hl_recent_fills（id DESC）取最近 N 条；
    # 今日/累计已实现与手续费仍按这批全量算（保留窗口内语义）。
    fills_rows = _db_call(deps.db, "hl_recent_fills", 2000) or []
    fills_out: List[dict] = []
    today_realized = 0.0
    total_realized = 0.0
    fees_total = 0.0
    day_start = _utc_day_start(now)
    for r in (fills_rows or []):
        d = _rowd(r)
        cp = _num(d.get("closed_pnl"))
        total_realized += cp
        # 费用口径统一走 fee_quote（NULL 回退 fee）：非计价币行的 fee 是「币的数量」，
        # 直接当 U 累加会把 0.2 ARB 记成 0.2U（#34 修的就是这个，读侧漏了这里）。
        fees_total += _fee_quoted(d) or 0.0
        t_s = _to_seconds(d.get("ts"))
        if t_s is not None and day_start <= t_s < day_start + _DAY:
            today_realized += cp
        fills_out.append({
            "ts": _opt_num(d.get("ts")),
            "coin": d.get("coin"),
            "side": d.get("side"),
            "sz": _opt_num(d.get("sz")),
            "px": _opt_num(d.get("px")),
            # 逐笔也走 fee_quote（口径同 fees_total，逐笔加起来等于合计）
            "fee": _fee_quoted(d),
            "fee_token": d.get("fee_token"),
            "dir": d.get("dir"),
            "closed_pnl": _opt_num(d.get("closed_pnl")),
            "reduce_only": bool(d.get("reduce_only")),
        })

    # 资金费取「收付视角」：HL 的 cumFunding.sinceOpen 是**仓位成本视角**（付出为正，
    # 空头收钱时该值为负），与 userFunding.delta.usdc 的**入账视角**（正=收入）符号相反。
    # 面板统一按「正=收入」渲染，所以此处对每个 sinceOpen 取反；
    # payload 的 funding_total（及 /api/hl/* 消费者）与总览同用这一个口径。
    funding_total = sum(-_num(d.get("funding_since_open")) for d in (_rowd(p) for p in snap_positions))
    unrealized_pnl = sum(p["unrealized_pnl"] for p in positions_out)

    # ---------------- 信号 ----------------
    signals: List[dict] = []
    pnl_from_fills = _pnl_from_fills(cfg)      # 净额口径开关（默认关）
    for r in db_rows:
        d = _rowd(r)
        entry = d.get("actual_entry")
        if entry in (None, ""):
            entry = d.get("entry_price")
        stop = d.get("actual_stop")
        if stop in (None, ""):
            stop = d.get("stop_price")
        sig = {
            "symbol": d.get("symbol"),          # 内部键（信号层 MEXC 名），保留不变
            "coin": _disp(d.get("symbol")),     # 展示用：HL 侧合约名
            "status": d.get("status"),
            "side": int(_num(d.get("side"), 1)),
            "entry": _opt_num(entry),
            "stop": _opt_num(stop),
            "contracts": d.get("contracts"),    # 原始 MEXC 口径张数（兼容字段，展示不用）
            # 双口径收敛（app/posfields.py）：数量走统一读侧 —— HL 仓 = 真实币量
            # （actual_contracts/hl_sz），历史 MEXC 行 = 张数。行里的 contracts 在
            # HL 仓上是 MEXC 人工口径的「张数」，直接显示会出现「85 张 vs 0.00084 BTC」。
            "qty": _opt_num(posfields.qty(d)),
            "qty_unit": posfields.qty_unit(d),
            "bar_ms": _opt_num(d.get("signal_bar_ms")),
        }
        # hl_status（执行器侧账实状态）：只在「有必要说」的时候进 payload ——
        # 空/open 意味着「一切正常」或「不是 HL 仓」，面板不必多一行字。
        hl_st = str(d.get("hl_status") or "")
        if hl_st and hl_st != "open":
            sig["hl_status"] = hl_st
            sig["hl_status_text"] = hl_status_text(hl_st)
            if hl_st == "closing":
                # 剩余宽限：留出「超时前不留空白」——读不到起点就按「刚进入」算满额。
                total = _grace_seconds_cfg(cfg)
                first = _grace_first_seen(deps.db, d.get("id"))
                left = total if first is None else max(0.0, total - max(0.0, now - first))
                sig["grace_left_s"] = round(left, 1)
        if pnl_from_fills:
            # 流水口径净额（PR-3）：**只在开关打开时才给出这些键** —— 记录表以
            # 「有没有 net_pnl 键」作为「要不要多显示一列净额」的依据，关掉时表格结构
            # 与老版本逐字相同（不新增空列，也不改 _signals_html 的默认形态）。
            # 值为 NULL = 没算过/查不到成交（不知道），不是 0。
            sig["net_pnl"] = _opt_num(d.get("net_pnl"))
            sig["pnl_source"] = d.get("pnl_source")
        signals.append(sig)

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

    # ---------------- 三标的动态（纯读 symbol_state 缓存）----------------
    symbol_state_out = _symbol_state_rows(deps, db_rows, symbols)

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
            "spot_hold": _num(snap.get("spot_hold")),
            "spot_ok": bool(snap.get("spot_ok", True)),
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
        "fills": fills_out[:100],   # JSON 也截断（看守/巡检只看头部；全量会让响应无限膨胀）
        "signals": signals,
        "symbol_state": symbol_state_out,
        "system": {
            "feed": feed,
            "last_bar": last_bar,
            "executor_last_event_at": executor_last_event_at,
            "stuck_orders": len(stuck),
            "mismatch": _reconcile(reachable, snap_positions, snap_orders,
                                   open_by_coin, stuck,
                                   orders_trusted=bool(snap.get("orders_trusted", False))
                                   if snap else False),
        },
        "equity_curve": {"7d": curve_7, "30d": curve_30},
        "events": events,
    }


# ---------------------------------------------------------------- SVG 权益曲线
def _equity_svg(points: List[List[float]], width: int = 760, height: int = 160) -> str:
    """手写内联 SVG 折线（不引任何图表库）。"""
    # 审计三轮 L：按像素列降采样 —— 30 天 5 分钟一张快照 ≈ 8600 点，
    # 保留窗口内可能上万点，全量内联 polyline 会让页面体积和渲染开销线性膨胀。
    if len(points) > (width * 2):
        step = len(points) / (width * 2)
        points = [points[min(len(points) - 1, int(i * step))] for i in range(width * 2)]
    pad_l, pad_r, pad_t, pad_b = 6, 6, 12, 16
    if len(points) < 2:
        return (f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' "
                f"aria-label='权益曲线'>"
                f"<text x='{width / 2}' y='{height / 2}' text-anchor='middle' "
                f"class='gtext' font-size='12'>暂无权益快照（等执行器落一次 hl_snapshots）</text></svg>")

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
        f"class='gline' stroke-width='1'/>"
        for f in (0.25, 0.5, 0.75))
    first_y = round(sy(ys[0]), 1)
    last_x, last_y = sx(xs[-1]), sy(ys[-1])
    return (
        f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' aria-label='权益曲线'>"
        + grid
        + f"<line x1='{pad_l}' y1='{first_y}' x2='{width - pad_r}' y2='{first_y}' "
        f"class='gbase' stroke-width='1' stroke-dasharray='4 4' opacity='0.45'/>"
        + f"<polygon points='{area}' class='garea' opacity='0.13'/>"
        + f"<polyline points='{pts}' fill='none' class='gline2' stroke-width='2' "
        f"stroke-linejoin='round' stroke-linecap='round'/>"
        + f"<circle cx='{last_x}' cy='{last_y}' r='3.5' class='gdot'/>"
        + "</svg>")


# ---------------------------------------------------------------- HTML 渲染
DASH_CSS = """<style>
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
     background:var(--bg);color:var(--fg);line-height:1.55;font-size:14px;padding:14px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:19px;margin:0 0 10px;font-weight:600}
h2{font-size:12px;margin:20px 0 8px;color:var(--h2);letter-spacing:1.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.badge{display:inline-flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line);
       border-radius:999px;padding:6px 12px;font-size:12px;white-space:nowrap}
.badge b{font-size:13px;font-weight:600}
.sec{font-size:11px;color:var(--muted);letter-spacing:1.5px;margin-bottom:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:2px 20px}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:13px;padding:4px 0;
    border-bottom:1px solid var(--line2)}
.kv:last-child{border-bottom:0}
.kv .k{color:var(--muted);white-space:nowrap}
.kv .v{font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
/* 卡内的句子行（原因 / 闸门异常 / 待确认提示）：与 .kv 的「键→数字」两列区分开，
   整句折行、不抢现价的视觉重量。 */
.whyline{margin-top:4px;font-size:12px;line-height:1.5;overflow-wrap:anywhere}
.pos{color:var(--pos)}.neg{color:var(--neg)}.warn{color:var(--warn)}.muted{color:var(--muted);font-size:12px}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;
            border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border-bottom:1px solid var(--line);padding:6px 9px;text-align:left;white-space:nowrap}
thead th{color:var(--muted);font-weight:500;font-size:11.5px;background:var(--card2)}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.banner{border:1px solid var(--banner-line);background:var(--banner-bg);color:var(--banner-fg);border-radius:10px;
        padding:10px 14px;margin-bottom:12px;font-size:13px}
.banner .muted{color:var(--banner-muted)}
.chart-wrap{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px 6px}
.chart-head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;
            font-size:12px;color:var(--muted);margin-bottom:2px}
.chart-head b{color:var(--fg)}
svg.chart{display:block;width:100%;height:160px}
svg.chart .gtext{fill:var(--muted)}
svg.chart .gline{stroke:var(--line)}
svg.chart .gbase{stroke:var(--muted)}
svg.chart .garea{fill:var(--link)}
svg.chart .gline2{stroke:var(--link)}
svg.chart .gdot{fill:var(--link)}
.btn{display:inline-block;background:var(--btn);color:#fff;border-radius:6px;padding:5px 11px;
     font-size:12px;text-decoration:none;white-space:nowrap}
.btn.gray{background:var(--btn-gray)}
.foot{margin:18px 0 6px;color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;
      gap:8px;align-items:center}
.foot .spacer{flex:1}
a{color:var(--link)}
@media (max-width:640px){body{padding:9px;font-size:13px}h1{font-size:17px}
    .badge{padding:5px 10px;font-size:11px}.grid{grid-template-columns:1fr}}
""" + SYMBOL_CSS + "</style>"


# ---------------------------------------------------------------- 三标的动态区块
# 位置：总览页最顶部（tabs 之后第一个区块、在「账户」卡片之前）。每标的一张卡，自上而下：
#   名称 · 状态词 · 现价 → 距离行 → 原因行 → 闸门异常行 → （区块底部一句可见脚注）
#
# 为什么把「为什么没开仓」并进来：两块回答的是同一个问题（现在能不能动）。分开渲染时同一屏
# 会出现两个现价（缓存 vs 现取，数字还不一样）、两套状态词（「均线未密集」/「均线不密集」）、
# 两处密集根数；而闸门正常时整列都是同一条「放行」，纯噪音。合并后**现价只有一个来源、
# 状态词只有一套、闸门只在异常时出现**，同时保留两块各自不可替代的信息：距离行的线价
# （可操作）与原因行的六步拦截数字（带带宽/门槛）。
#
# 数据：why 行（app.why.build_rows，现算）为主源；why 缺失/报错时退回 symbol_state
# （引擎 tick 落的缓存）并标注读数时间。面板依旧不重算任何判定，只挑字段渲染。
_SQUEEZE_CAP = 6          # 密集根数展示封顶：满 6 根即已满足入场前提
_NEAR_TITLE = "接近触发（按收盘判定）"
# 图例行（区块底部，手机看不见 title 悬停，口径必须落在可见文字上）：
# ① 离场只看收盘破线 → 琥珀（接近）就有意义；②③ 破线还要满足前置条件，琥珀会误导。
# 「信号 = 六步规则 / 闸门 = 动作前的附加保护」这段原属「为什么没开仓」的脚注 ——
# 两块合一后只剩一条脚注，否则同一屏又要读两遍口径。
_LEGEND_HTML = ("<div class=\"sym-legend\">① 离场按 4h 收盘判定 · "
                "② 破线+密集满 6 根开多 · ③ 破线+MA200 下方才开空 · "
                "信号 = 六步规则（根数 → 密集 → 破线 → MA200 → 止损距离）· "
                "闸门 = 执行器动作前的附加保护（成交回报后 15s 静置、10 分钟内 5 次撤挂熔断），"
                "只影响「什么时候动手」，不改变信号本身；闸门正常时不显示</div>")
_EXIT_TITLE = {1: "按 4h 收盘判定，非盘中触发：收盘跌破该线即自动平仓",
               -1: "按 4h 收盘判定，非盘中触发：收盘站上该线即自动平仓"}
_LONG_TITLE = "4h 收盘突破该线即入场（需均线密集满 6 根）"
_SHORT_TITLE = "收盘跌破该线且在 MA200 下方才开空"


def _need_pct(kind: str, price: Any, line: Any) -> Optional[float]:
    """到达触发线所需变动幅度（%，自带方向符号）。四方向：

    exit_short  持仓空：需要**涨**到 SMA20（收盘站上才自动平仓）→ (线 − 现价)/现价
    exit_long   持仓多：需要**跌**到 SMA20（收盘跌破）        → (现价 − 线)/现价
    break_up    无仓上破开多：需要涨到 6均线上轨               → (线 − 现价)/现价
    break_down  无仓下破开空：需要跌到 6均线下轨               → (现价 − 线)/现价

    现价/触发线缺失或非法（<=0）→ None（该子行整体不渲染，fail-soft）。
    """
    px, v = _opt_num(price), _opt_num(line)
    if px is None or px <= 0 or v is None:
        return None
    if kind in ("exit_short", "break_up"):
        return (v - px) / px * 100.0
    if kind in ("exit_long", "break_down"):
        return (px - v) / px * 100.0
    return None


def _sym_state_word(side: Any, dense: Any) -> tuple:
    """状态词 (文本, 颜色 class, 悬停 title)。

    - 有 status='open' 仓位 →「持仓中（多/空）」muted（持仓标的 walk 不评估入场，不谈密集）
    - 无仓：密集根数 NULL/0 →「均线未密集」muted；1~5 →「密集酝酿 n/6」hot；
      ≥6 → 封顶显示「密集酝酿 6/6」hot，title 里给真实根数
    """
    sd = _opt_int(side)
    if sd in (1, -1):
        return f"持仓中（{'多' if sd == 1 else '空'}）", "muted", ""
    n = _opt_int(dense) or 0
    if n <= 0:
        return "均线未密集", "muted", ""
    return (f"密集酝酿 {min(n, _SQUEEZE_CAP)}/{_SQUEEZE_CAP}", "hot",
            f"6 均线已连续 {n} 根密集，满 {_SQUEEZE_CAP} 根后收盘突破即入场")


def _sym_row(label: str, mark: str, title: str, need: Optional[float], line: Any,
             allow_warn: bool = True) -> str:
    """距离行：`还差 +X.X%`（0<need<1% 且 allow_warn 时标 warn + 悬停「接近触发」）/
    `待收盘判定`，百分比之后跟触发线价格（价格格式复用面板 _fmt 口径）。

    allow_warn 的语义 =「距离近 = 今晚可能动作」是否成立：
      ① 离场行（持仓）只看 4h 收盘破线，距离近确实意味着可能动作 → 恒 True；
      ② 上破开多行只需密集满 6 根（做多无 MA200 过滤），由调用方传
        `dense_cnt >= _SQUEEZE_CAP`（缺 dense_cnt → False）；
      ③ 下破开空行是三重的：密集满 6 根 **且** sma200 非 None **且** 现价 < sma200；
        任一不成立（含 sma200 缺失 = 无法核实过滤条件）都不亮琥珀 → fail-closed。
    """
    line_txt = f" <span class=\"muted\">线 {_fmt(line)}</span>"
    if need is None or need <= 0:
        val = f"<span class=\"muted\">待收盘判定</span>{line_txt}"
    else:
        cls = (f" class=\"warn\" title=\"{_NEAR_TITLE}\"" if allow_warn and need < 1.0
               else "")
        val = f"还差 <b{cls}>{need:+.1f}%</b>{line_txt}"
    return (f"<div class=\"kv\"><span class=\"k\">{label} "
            f"<span class=\"fn\" title=\"{_esc(title)}\">{mark}</span></span>"
            f"<span class=\"v\">{val}</span></div>")


def _pos_of(r: Dict[str, Any]) -> tuple:
    """卡片用的仓位口径：(status, side)。

    两个来源不同：why 行给 why.position（status='open'/'pending' 才有意义）；只有
    symbol_state 的降级路径没有 status，position_side 非空即代表「有 open 仓」
    （_symbol_state_rows 只把 status='open' 的仓写成 position_side）。
    返回 ("", None) = 无仓 → 走 ②③ 入场距离行。
    """
    pos = _as_dict(r.get("position"))
    st = str(pos.get("status") or "")
    if st in ("open", "pending"):
        return st, (_opt_int(pos.get("side")) or 1)
    sd = _opt_int(r.get("position_side"))
    if sd in (1, -1):
        return "open", sd
    return "", None


def _sym_state(r: Dict[str, Any]) -> tuple:
    """状态词（唯一一套，按优先级，命中即止）：(文本, 颜色 class, 悬停 title)。

    1. 行情读取失败 / 没有现价 → neg（数字都没有，谈密集/信号都是空话）
    2. 有 open 仓 → muted（walk 不评估入场，密集根数对它没意义）
    3. 有 pending 仓 → warn（占着槽位）
    4. why.fired → pos（信号成立，等执行）
    5~6. 密集酝酿 n/6（hot，封顶 6）→ 均线未密集（muted）

    旧代码在两块里各写一套（「均线未密集」vs「均线不密集」），统一取前者。
    """
    px = _opt_num(r.get("price"))
    if r.get("error") or px is None or px <= 0:
        return "行情读取失败", "neg", ""
    status, side = _pos_of(r)
    if status == "open":
        return f"持仓中（{'多' if side == 1 else '空'}）", "muted", ""
    if status == "pending":
        return "待确认（占槽）", "warn", "同一标的至多 1 仓，确认或忽略后才会评估新信号"
    why = _as_dict(r.get("why"))
    if why.get("fired"):
        return (f"信号成立（{'多' if int(_num(why.get('side'))) > 0 else '空'}）", "pos",
                str(why.get("reason") or ""))
    return _sym_state_word(None, r.get("dense_cnt"))


def _whyline(text: str, cls: str = "muted") -> str:
    """卡内的一句人话（原因行 / 闸门异常行 / 待确认提示）。

    与距离行（.kv）分开的类名：.kv 是「键 → 数字」两列，这里是一整句，样式与语义都不同。
    """
    return f"<div class=\"whyline {cls}\">{_esc(text)}</div>" if cls else \
        f"<div class=\"whyline\">{_esc(text)}</div>"


def _reason_line(r: Dict[str, Any]) -> str:
    """原因行：吸收原「信号态 / 拦截原因」单元格，一句人话、带数字。

    顺序刻意与 _sym_state 一致（仓位在行情之上）：有仓时真正该做的是处理这一仓，
    而不是去读「假如无仓」的行情读数。降级路径必须把**读数时间**写出来，
    否则用户会把缓存里的旧价当成现价（这正是合并前两个现价打架的根源）。
    """
    status, _ = _pos_of(r)
    if status == "open":
        return _whyline("持仓中，本轮不评估新入场", "muted")
    if status == "pending":
        return _whyline("待确认占用仓位槽，确认或忽略后才会评估新信号", "warn")
    err = str(r.get("error") or "")
    when = _fmt_dt(r.get("updated_at"), "%H:%M")
    if err:
        tail = f"（显示 {when} 的读数）" if _opt_num(r.get("price")) is not None else ""
        return _whyline(f"行情读取失败：{err}{tail}", "neg")
    why = _as_dict(r.get("why"))
    if not why:
        return _whyline(f"诊断数据暂不可用（显示 {when} 的读数）", "muted")
    if why.get("fired"):
        sig = _as_dict(why.get("signal"))
        ep, sp = _opt_num(sig.get("entry_price")), _opt_num(sig.get("stop_price"))
        sd = _opt_num(sig.get("stop_dist"))
        if ep is not None and sp is not None:
            pct = f"（{sd * 100:.2f}%）" if sd is not None else ""
            return _whyline(f"信号成立：入场 {_fmt(ep)}、止损 {_fmt(sp)}{pct}", "pos")
        return _whyline(str(why.get("reason") or "信号成立"), "pos")
    # reason 由 strategy.explain_entry 给（带带宽/门槛数字的人话），面板只做三件展示层的事：
    # ① 词面统一：strategy 的文案是「均线不密集」，而本卡状态词与图例统一用「均线未密集」——
    #    同一张卡上下两种说法等于又回到合并前「两套状态词」的老毛病。只替换展示文本，
    #    不动 strategy 的文案与判定（信号口径禁止改），/api/hl/why 仍返回 strategy 原文。
    # ② 密集根数口径统一：strategy 报的是「本轮判定所用那根」（squeeze[i-1]）的连续根数，
    #    而状态词与距离行取的是最后一根已收盘 bar（ind[i]）——两者**恒差一根**，同一张卡
    #    上下两个数打架（用户实测：状态词 2/6、原因行「现 1 根」）。同样只改展示文本，
    #    把「（现 N 根）」换成卡片自己的 dense_cnt；/api/hl/why 仍是 strategy 原文。
    # ③ 按真拦截点组句：strategy 的密集拦截模板把「带宽没压进」与「根数不够」用「或」
    #    并列、从不区分——已压进门槛时「未压到门槛以内」半句是假话（2026-10-21 用户实测：
    #    带宽 1.62% < 门槛 1.73% 却显示「未压到」）。装配层（app/why.py）给了**当下 bar**
    #    的带宽/门槛（band_cur/thr_cur），拦截点是密集时按真话组句；字段缺失（旧缓存/
    #    降级路径）回落 strategy 原文 + ①②的替换。
    if str(why.get("blocked_by") or "") == "squeeze":
        band_cur, thr_cur = _opt_num(r.get("band_cur")), _opt_num(r.get("thr_cur"))
        dense = _opt_int(r.get("dense_cnt"))
        need = _opt_int(r.get("min_squeeze_bars")) or _SQUEEZE_CAP
        if band_cur is not None and thr_cur is not None and dense is not None:
            if band_cur <= thr_cur:
                txt = (f"均线未密集：带宽 {band_cur * 100:.2f}% 已压到门槛 "
                       f"{thr_cur * 100:.2f}% 以内，连续密集 {dense} 根、"
                       f"满 {need} 根才具备开仓条件")
            else:
                txt = (f"均线未密集：带宽 {band_cur * 100:.2f}% 未压到门槛 "
                       f"{thr_cur * 100:.2f}% 以内（连续密集 {dense} 根）")
            if why.get("drift"):
                txt += " ⚠️口径漂移"
            return _whyline(txt, "muted")
    txt = str(why.get("reason") or "—").replace("均线不密集", "均线未密集")
    dense = _opt_int(r.get("dense_cnt"))
    if dense is not None:
        txt = re.sub(r"（现 \d+ 根）", f"（现 {dense} 根）", txt)
    if why.get("drift"):
        txt += " ⚠️口径漂移"
    return _whyline(txt, "muted")


def _gate_lines(r: Dict[str, Any]) -> List[str]:
    """闸门异常行：**只在不正常时产生 DOM**（正常放行是噪音，一个字都不渲染）。

    关注侧沿用现有语义：持仓方向 > 信号方向 > 双侧都给（why.gate_side 已算好）。
    churn_limit<=0 = 熔断被人为关闭，此时「正在静置」只报静置 —— 报 churn 次数没有意义
    （永远不会熔断），只会让人以为它随时会停手。
    """
    gates = _as_dict(r.get("gates"))
    gate = _as_dict(r.get("gate"))
    items: List[tuple] = []
    if gate:
        items.append(gate)
    else:
        for key in ("long", "short"):
            g = _as_dict(gates.get(key))
            if g:
                items.append(g)
    out: List[str] = []
    for g in items:
        verdict = str(g.get("verdict") or "ok")
        if verdict == "ok":
            continue
        if verdict == "settle":
            out.append(_whyline(f"⚠️ 静置剩余 {int(_num(g.get('settle_remaining_s')))}s", "warn"))
        elif verdict == "churn":
            limit = int(_num(g.get("churn_limit")))
            if limit <= 0:
                continue
            out.append(_whyline(f"⚠️ churn {int(_num(g.get('churn_count')))}/{limit}"
                                f"（10 分钟内 {limit} 次撤挂即熔断）", "warn"))
        else:
            # 未知 verdict（执行器改了枚举而面板没跟上）：如实显示，别假装没事
            out.append(_whyline(f"⚠️ 闸门状态未知：{verdict}", "warn"))
    return out


def _merge_one(w: Dict[str, Any], st: Dict[str, Any]) -> Dict[str, Any]:
    """why 行（主源）+ symbol_state 行（降级源）→ 一张卡的数据。纯挑字段，不算判定。

    索引口径统一取「最后一根已收盘 bar」(ind[i])：sma20 / ②③ 的线价 / 密集根数 / SMA200
    全部同索引 —— 因为下一次入场判定比的就是这一根。why 行里的 ma_max_prev / ma_min_prev /
    values.dense_cnt 是 ind[i-1]（**上一根**，本轮判定用的那根），拿它算「到线距离」会让
    线价与距离差一根 bar、和 ① 的 SMA20 对不上；所以只在 symbol_state 缺失时兜底。
    """
    why = _as_dict(w.get("why"))
    vals = _as_dict(why.get("values"))
    err = str(w.get("error") or "")
    px_w = _opt_num(w.get("price"))
    # 降级判定：why 行缺失 / 取行情报错 / why 没有现价 —— 三者都只能用缓存读数
    degraded = bool(not w or err or px_w is None)
    if degraded:
        price, sma20 = _opt_num(st.get("price")), _opt_num(st.get("sma20"))
        long_trig, short_trig = _opt_num(st.get("long_trigger")), _opt_num(st.get("short_trigger"))
        dense, ma200 = _opt_int(st.get("dense_cnt")), _opt_num(st.get("sma200"))
    else:
        price = px_w
        sma20 = _opt_num(w.get("sma20"))
        if sma20 is None:
            sma20 = _opt_num(st.get("sma20"))
        long_trig = _opt_num(st.get("long_trigger"))
        if long_trig is None:
            long_trig = _opt_num(vals.get("ma_max_prev"))
        short_trig = _opt_num(st.get("short_trigger"))
        if short_trig is None:
            short_trig = _opt_num(vals.get("ma_min_prev"))
        dense = _opt_int(st.get("dense_cnt"))
        if dense is None:
            dense = _opt_int(vals.get("dense_cnt"))
        ma200 = _opt_num(st.get("sma200"))
        if ma200 is None and int(_num(vals.get("regime_ma"))) == 200:
            # ③ 琥珀要「现价 < MA200」；regime_val 只在看空方向时算过，且窗口可配
            ma200 = _opt_num(vals.get("regime_val"))
    sym = str(w.get("symbol") or st.get("symbol") or "")
    return {
        "symbol": sym,
        "coin": str(w.get("coin") or st.get("coin") or "") or _disp(sym),
        "price": price, "sma20": sma20, "sma200": ma200,
        "long_trigger": long_trig, "short_trigger": short_trig,
        "dense_cnt": dense,
        "band_cur": _opt_num(w.get("band_cur")),
        "thr_cur": _opt_num(w.get("thr_cur")),
        "min_squeeze_bars": _opt_int(w.get("min_squeeze_bars")),
        "position": _as_dict(w.get("position")),
        "position_side": st.get("position_side"),
        "why": why, "error": err or None,
        "gates": _as_dict(w.get("gates")), "gate": _as_dict(w.get("gate")),
        "gate_side": _opt_int(w.get("gate_side")) or 0,
        "updated_at": _opt_num(st.get("updated_at")),
    }


def _merged_cards(p: dict) -> List[Dict[str, Any]]:
    """payload → 卡片数据（why 顺序为主，why 缺的标的按 symbol_state 顺序补在后面）。

    两个方向都要兜：why 少一个标的不能让这张卡消失（fail-soft，只是没有原因行）；
    symbol_state 整块缺失时 why 照样能画（价格/线价取 why 行）。
    """
    why_rows = [_as_dict(x) for x in _as_list(p.get("why"))]
    st_rows: List[Dict[str, Any]] = []
    st_by_sym: Dict[str, Dict[str, Any]] = {}
    for x in _as_list(p.get("symbol_state")):
        d = _as_dict(x)
        sym = str(d.get("symbol") or "")
        if sym:
            st_by_sym[sym] = d
            st_rows.append(d)
    cards: List[Dict[str, Any]] = []
    seen = set()
    for w in why_rows:
        sym = str(w.get("symbol") or "")
        cards.append(_merge_one(w, st_by_sym.get(sym) or {}))
        seen.add(sym)
    for d in st_rows:
        sym = str(d.get("symbol") or "")
        if sym in seen:
            continue
        seen.add(sym)
        cards.append(_merge_one({}, d))
    # 既没 symbol 也没 coin 的脏行无法定位标的（也没有可展示的名字）→ 直接略去
    return [c for c in cards if c.get("symbol") or c.get("coin")]


def _sym_card(r: Dict[str, Any]) -> str:
    """一个标的的卡片：现价 + 状态词 + 距离行（持仓只给离场线；无仓给两条入场线）
    + 原因行 + 闸门异常行。现价全页只在这里出现一次（两块合并的核心约束）。
    """
    price = _opt_num(r.get("price"))
    status, side = _pos_of(r)
    st_txt, st_cls, st_title = _sym_state(r)
    title_attr = f" title=\"{_esc(st_title)}\"" if st_title else ""
    name = _esc(r.get("coin") or _disp(r.get("symbol")) or "—")
    o = [("<div class=\"sym\"><div class=\"sym-h\">"
          f"<span class=\"nm\">{name}</span>"
          f"<span class=\"st {st_cls}\"{title_attr}>{st_txt}</span>"
          f"<span class=\"px\">{_fmt(price)}</span></div>")]
    if price is not None and price > 0:
        if status == "pending":
            # pending 占着仓位槽：确认前根本不会评估新入场，距离行是假信息
            o.append(_whyline("确认前不评估新入场", "muted"))
        elif status == "open":
            need = _need_pct("exit_short" if side < 0 else "exit_long", price, r.get("sma20"))
            if need is not None:
                o.append(_sym_row("距自动离场线", "①", _EXIT_TITLE[-1 if side < 0 else 1],
                                  need, r.get("sma20")))
        else:
            # 入场行（②③）的琥珀门槛：
            #   ② 上破开多：密集满 6 根（做多无 MA200 过滤）。
            #   ③ 下破开空：密集满 6 根 且 现价 < sma200（严格小于，与
            #      strategy.evaluate_entry 做空过滤同向）；sma200 缺失 = 无法核实
            #      过滤条件 → fail-closed 不亮琥珀。只有三重都成立，「距离近」才代表
            #      「今晚可能开空」。
            dense_ok = (_opt_int(r.get("dense_cnt")) or 0) >= _SQUEEZE_CAP
            ma200 = _opt_num(r.get("sma200"))
            short_ok = dense_ok and ma200 is not None and price < ma200
            need = _need_pct("break_up", price, r.get("long_trigger"))
            if need is not None:
                o.append(_sym_row("上破开多", "②", _LONG_TITLE, need, r.get("long_trigger"),
                                  allow_warn=dense_ok))
            need = _need_pct("break_down", price, r.get("short_trigger"))
            if need is not None:
                o.append(_sym_row("下破开空", "③", _SHORT_TITLE, need, r.get("short_trigger"),
                                  allow_warn=short_ok))
    o.append(_reason_line(r))
    o.extend(_gate_lines(r))
    o.append("</div>")
    return "".join(o)


def _symbol_dynamics(p: dict) -> str:
    """「三标的动态」区块（总览页最顶部，tabs 之后第一个区块，见 _render_body）。

    合并后这里同时回答「离触发线多远」（距离行）和「卡在哪一步 / 被闸门挡了吗」
    （原因行 / 闸门异常行）—— 原「为什么没开仓」区块已删除。

    fail-soft：某标的两侧数据都缺失 → 价格显示「—」、子行整体不渲染，其余标的不受影响；
    整块任何异常都不能弄挂总览页（调用点在 try 里）。
    区块头时间 = 三个标的里最新的 updated_at（北京时间，与面板其它时间同口径）。
    """
    rows = _merged_cards(p)
    if not rows:
        return ""
    stamps = [t for t in (_opt_num(r.get("updated_at")) for r in rows) if t]
    when = _fmt_dt(max(stamps), "%H:%M") if stamps else "—"
    o = [f"<h2>三标的动态<span class=\"muted\">行情 {when} · 每 30 分钟随信号 tick 更新</span></h2>",
         "<div class=\"card\"><div class=\"grid\">"]
    for r in rows:
        o.append(_sym_card(r))
    # 图例放在 grid 之后、card 收尾之前：手机（iOS Safari 无 title 悬停）唯一能看到
    # ①②③ 口径的地方；无数据时整块不渲染（上方 rows 为空已 return）。
    o.append("</div>")
    o.append(_LEGEND_HTML)
    o.append("</div>")
    return "".join(o)


# ---------------------------------------------------------------- /why 数据源
# 面板的「原因行 / 闸门异常行」数据来自这里：`app.why.build_rows`（纯只读：现取 4h K 线 →
# strategy.explain_entry 给六步 → Executor.gate_state_for 读闸门）。**面板不重算任何判定**，
# 只做渲染；`GET /api/hl/why` 与容器内 `python -m app.why` 走的也是这一条链路。
def _why_rows(deps: "DashboardDeps") -> List[Dict[str, Any]]:
    """现算 /why 行（只读）。任何异常都退回空列表 —— 诊断区块绝不弄挂总览页。"""
    try:
        from app import why as why_mod
        cfg = _call(deps.config_fn)
        if cfg is None:
            return []
        return why_mod.build_rows(deps.db, cfg)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] /why 诊断计算失败（区块略去）：%s", e)
        return []


def _sign(v: Any, unit: str = " U") -> tuple:
    """返回 (带符号文本, 颜色 class)。"""
    f = _opt_num(v)
    if f is None:
        return "—", ""
    cls = "pos" if f > 0 else ("neg" if f < 0 else "")
    return f"{f:+,.2f}{unit}", cls


def _grace_seconds_cfg(cfg: Any) -> float:
    """面板侧读宽限秒数：脏值/越界一律按 900（与 main._grace_seconds 同口径）。

    这里**不 import** app.main / app.hl.executor：面板是纯渲染层，为了一个数字把
    建库、SDK 依赖拖进来不值得（历史教训：views 一旦 import 重模块，面板测试就会
    连带建库）。两个常量各自一份，但都钉在同样的默认与范围上。
    """
    try:
        v = float(getattr(cfg, "hl_close_grace_s", _GRACE_DEFAULT_S))
    except (TypeError, ValueError):
        return _GRACE_DEFAULT_S
    return v if _GRACE_MIN_S <= v <= _GRACE_MAX_S else _GRACE_DEFAULT_S


def _grace_first_seen(db: Any, pid: Any) -> Optional[float]:
    """读该仓宽限期的起点（configuration 的 `hl_close_grace:<pid>`，格式 "<ts>:<rounds>"）。

    读不到/读不懂 → None（调用方按「刚进入宽限」算剩余，绝不因为读不到就显示空白）。
    """
    try:
        raw = str(_db_call(db, "get_config_str", f"hl_close_grace:{int(pid)}", "") or "").strip()
    except Exception:                                            # noqa: BLE001
        return None
    head = raw.split(":", 1)[0]
    try:
        ts = float(head)
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def hl_status_text(status: Any) -> str:
    """hl_status → 中文（未知值兜底原文；空串 → 空串）。"""
    s = str(status or "")
    return _HL_ST_TXT.get(s, s)


def _signals_html(signals: list) -> str:
    """「信号」表（仓位历史）的 HTML 片段：标题 + 表格，仅此一块。

    2026-10-18 从总览迁到 /admin?tab=records（用户要求：总览只看当下，历史表格归记录页）。
    渲染逻辑原样保留：状态文案走 _ST_TXT，open=pos / pending=warn 着色，
    数量用 _fmt_sz（HL 仓是真实币量 0.00084，不能显示成 0.00）。

    PR-3：payload 里带 ``net_pnl`` 键时（只在 hl_pnl_from_fills 打开时才有）多显示
    一列「净额（流水）」。**按行探测而不是按全局开关**：这样本函数保持纯渲染、不依赖
    config，关掉开关时表格结构与老版本完全一致。
    """
    o: List[str] = []
    rows = _as_list(signals)
    dicts = [_as_dict(x) for x in rows]
    with_pnl = any("net_pnl" in d for d in dicts)
    o.append("<h2>信号</h2>")
    o.append("<div class='tablewrap'><table><thead><tr>"
             "<th>标的</th><th>状态</th><th>方向</th><th class='num'>入场</th>"
             "<th class='num'>止损</th><th class='num'>数量</th>"
             + ("<th class='num'>净额（流水）</th>" if with_pnl else "")
             + "<th>信号 bar</th>"
             "</tr></thead><tbody>")
    for sg in dicts:
        st = str(sg.get("status") or "—")
        st_cls = {"open": "pos", "pending": "warn"}.get(st, "")
        hl_st = str(sg.get("hl_status") or "")
        hl_note = ""
        if hl_st:
            # 第二行：执行器侧的账实状态（closing/mismatch/...）。宽限期额外给剩余时间，
            # 「超时前不留空白」—— 剩 0 分钟也要写出来（那正是「马上要升级成异常」）。
            extra = ""
            if hl_st == "closing":
                left = _opt_num(sg.get("grace_left_s"))
                if left is not None:
                    mins = max(0, int(math.ceil(max(0.0, left) / 60.0)))
                    extra = f" · 剩 {mins} 分钟"
            hl_note = (f"<div class='muted'>{_esc(hl_status_text(hl_st))}"
                       f"{_esc(extra)}</div>")
        pnl_cell = ""
        if with_pnl:
            # net_pnl 为 NULL 时显示「—」：没算过/查不到成交 ≠ 0 收益
            pnl_cell = _td(_fmt(sg.get("net_pnl")), "num")
        o.append("<tr>"
                 + _td(_esc(sg.get("coin") or _disp(sg.get("symbol")) or "—"))
                 + _td(f"<span class='{st_cls}'>{_esc(_ST_TXT.get(st, st))}</span>" + hl_note)
                 + _td("多" if _num(sg.get("side"), 1) >= 0 else "空")
                 + _td(_fmt(sg.get("entry")), "num")
                 + _td(_fmt(sg.get("stop")), "num")
                 + _td(_fmt_sz(sg.get("qty")), "num")
                 + pnl_cell
                 + _td(_fmt_dt(sg.get("bar_ms")), "num")
                 + "</tr>")
    if not rows:
        span = 8 if with_pnl else 7
        o.append(f"<tr><td colspan='{span}' class='muted'>暂无信号</td></tr>")
    o.append("</tbody></table></div>")
    return "".join(o)


def _render_body(p: dict) -> str:
    mode = str(p.get("mode") or "off")
    net = _as_dict(p.get("network"))
    acc = _as_dict(p.get("account"))
    system = _as_dict(p.get("system"))
    positions = _as_list(p.get("positions"))
    orders_open = _as_list(p.get("orders_open"))
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
                 f"<span class='muted'>交易所侧数据暂不可用；信号 / 下单 / 成交记录见「记录」页。</span></div>")

    # ---------------- 三标的动态（含 /why 原因行与闸门） ----------------
    # 位置由需求钉死：总览页最顶部、tabs 之后第一个区块（在「账户」卡片之前），
    # 手机打开面板先看到三个标的的状态。距离行 + 原因行 + 闸门异常行都在这一块里 ——
    # 原来紧跟在后面的「为什么没开仓」表格已合并进来（同一屏两个现价/两套状态词是硬伤）。
    # 数据由调用方注入 payload（why 由 admin_ui 现算，symbol_state 来自 DB 缓存），
    # 本函数不做 I/O，逐块兜底：缺哪份数据就只少那几行，绝不弄挂总览页。
    try:
        o.append(_symbol_dynamics(p))
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 三标的动态区块渲染失败：%s", e)


    # ---------------- 账户卡片 ----------------
    o.append("<div class='card'><div class='sec'>账户</div><div class='grid'>")
    o.append(_kv("钱包地址", _addr_text(acc.get("address"))))
    unreal_txt, unreal_cls = _sign(acc.get("unrealized_pnl"))
    today_txt, today_cls = _sign(acc.get("today_realized"))
    total_txt, total_cls = _sign(acc.get("total_realized"))
    fund_txt, fund_cls = _sign(acc.get("funding_total"))
    fees = _num(acc.get("fees_total"))
    o.append(_kv("账户总值", f"{_fmt(acc.get('equity'))} U"))
    if acc.get("unified") and not acc.get("spot_ok", True):
        # 现货状态读取失败 → equity_total 只剩浮盈，数字完全不可信，必须喊出来
        o.append(_kv("⚠️ 现货余额读取失败", "上方的总值/可提不可信（只剩浮盈），不是没钱", "neg"))
    if acc.get("unified"):
        # 统一账户：资金分布在「现货」和「合约」两侧 —— 用人话一行说完。
        # 合约侧占用的就是下面原来单列的「已用保证金」（同一个数，实测该仓保证金
        # 13.7~14U 与 perp 口径一致），所以合并成一行；「永续口径」这种口径名
        # 对用户是黑话、且与保证金重复，去掉。
        o.append(_kv("资金分布（统一账户）",
                     f"现货 {_fmt(acc.get('spot_usdc'))} U · 合约占用 {_fmt(acc.get('margin_used'))} U",
                     "muted", title="统一账户下资金分在现货与合约两侧；合约侧占用 = 已用保证金"))
    wd = _num(acc.get("withdrawable"))
    if acc.get("unified") and wd <= 0:
        # 统一账户：真实可提 = 现货 USDC − 占用（仓位/挂单锁定）；HL 提现固定收 1 USDC。
        # 文案短说重点，口径细节放 title 悬停（手机长按也能看）。
        avail = max(_num(acc.get("spot_usdc")) - _num(acc.get("spot_hold")), 0.0)
        o.append(_kv("可提余额", f"{_fmt(avail)} U（提现收 1U 手续费）",
                     title="统一账户下的实际可提：现货余额扣除仓位/挂单占用的保证金"))
    else:
        o.append(_kv("可提余额", f"{_fmt(wd)} U"))
    if not acc.get("unified"):
        # 统一账户已在「资金分布」那行给出合约占用，这里不再重复一列
        o.append(_kv("已用保证金", f"{_fmt(acc.get('margin_used'))} U"))
    o.append(_kv("持仓名义", f"{_fmt(acc.get('notional'))} U"))
    o.append(_kv("浮盈浮亏", unreal_txt, unreal_cls))
    o.append(_kv("今日已实现", today_txt, today_cls,
                 title="交易所 closedPnl 口径，未扣手续费与资金费"))
    o.append(_kv("累计已实现", total_txt, total_cls,
                 title="交易所 closedPnl 口径，未扣手续费与资金费"))
    o.append(_kv("资金费（持仓期内，正=收入）", fund_txt, fund_cls))
    o.append(_kv("净手续费（付出）", f"{-fees:+,.2f} U", "neg" if fees > 0 else ""))
    o.append("</div>")
    # 注脚（可见文字，iOS Safari 无 title 悬停）：账户相关数字的口径收口。
    # 「已实现」两行的 title 保留作桌面增强，这里给出手机唯一能看到的口径。
    o.append('<div class="legend">已实现为交易所口径，未扣手续费与资金费 · '
             '可提 = 现货 − 仓位/挂单占用</div>')
    o.append("</div>")

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
        elif pos.get("stop_unknown"):
            stop_cell = "<span class='muted'>? 无法确认</span>"
        else:
            stop_cell = "<span class='neg'>&#10060; 无</span>"
        o.append("<tr>"
                 + _td(f"<b>{_esc(pos.get('coin') or '—')}</b>")
                 + _td(f"<b>{side_txt}</b>")
                 + _td(_fmt_sz(pos.get("size")), "num")
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
                 f"（信号 / 下单 / 成交见「记录」页）</td></tr>")
    o.append("</tbody></table></div>")

    # ---------------- 挂单 ----------------
    o.append("<h2>交易所挂单</h2>")
    o.append("<div class='tablewrap'><table><thead><tr>"
             "<th>币种</th><th>类型</th><th>方向</th><th class='num'>数量</th>"
             "<th class='num'>限价</th><th class='num'>触发价</th><th class='num'>时间</th>"
             "</tr></thead><tbody>")
    for od in (_as_dict(x) for x in orders_open):
        kind = "止损触发" if od.get("is_trigger") else ("市价/限价" if od.get("limit_px") else "下单")
        if od.get("reduce_only"):
            kind = (kind + " <span class='muted'>(减仓)</span>")
        o.append("<tr>"
                 + _td(_esc(od.get("coin") or "—"))
                 + _td(kind)
                 + _td("买" if od.get("is_buy") else "卖")
                 + _td(_fmt_sz(od.get("sz")), "num")
                 + _td(_fmt(od.get("limit_px")), "num")
                 + _td(_fmt(od.get("trigger_px")), "num")
                 + _td(_fmt_dt(od.get("timestamp")), "num")
                 + "</tr>")
    if not orders_open:
        o.append("<tr><td colspan='7' class='muted'>交易所无挂单</td></tr>")
    o.append("</tbody></table></div>")

    # ---------------- 系统卡片 ----------------
    # 注：「下单记录」「最近成交」两张表已从总览移除（2026-10-18 用户要求：与「记录」页
    # 的 hl_orders / hl_fills 完全重复 → 去 /admin?tab=records 看）。
    # 更早的「执行器事件」表同理（2026-10-15 移除）。「信号」表也在本次迁到记录页，
    # 渲染函数保留为 _signals_html（记录页复用它，两边长得一样）。
    o.append("<h2>系统</h2>")
    o.append("<div class='card'><div class='grid'>")
    o.append(_kv("执行器最后事件", _fmt_dt(system.get("executor_last_event_at"))))
    o.append(_kv("卡在 intent/sent 的下单", f"{int(stuck or 0)} 笔",
                 "neg" if stuck else "muted",
                 title="持续 3 分钟未推进才计入"))
    o.append(_kv("对账", "一致" if not mismatch else "不一致", "pos" if not mismatch else "neg"))
    o.append(_kv("交易所地址", _esc(net.get("base_url") or "—")))
    o.append(_kv("环境", "测试网" if net.get("testnet") else "主网"))
    o.append(_kv("熔断", "已触发" if kill else "未触发", "neg" if kill else "pos"))
    o.append("</div>")
    # 注脚（可见文字）：卡单与 HL 延迟两条口径原先只在两处 title 里，手机上读不到。
    o.append('<div class="legend">卡单 = 持续 3 分钟未推进才计入 · '
             'HL 延迟 = 容器请求 /info 耗时（含失败重试），与本机网速无关</div>')
    feed = _as_list(system.get("feed"))
    if feed:
        o.append("<div style='margin-top:10px'><div class='sec'>数据源健康</div>")
        # 表头「来源」原本渲染的是 正常/失败（源名 payload 里有 source，但行内没渲染）
        # → 改「状态」并在表头挂口径 title；源名见 payload.feed[].source。
        o.append("<div class='tablewrap'><table><thead><tr><th>标的</th>"
                 "<th title='信号源固定 hl-candle（fail-closed）'>状态</th>"
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
        o.append("</tbody></table></div>")
        # 注脚（可见文字）：表头 title 只作桌面增强，手机看不到 → 口径落成可见行。
        o.append('<div class="legend">信号源固定 hl-candle'
                 '（fail-closed：源失败即跳过本轮，不换源）</div></div>')
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


def _render_footer(cur: int, theme: str = "auto", k: str = "") -> str:
    links = []
    for n, label in ((0, "手动刷新"), (10, "每 10 秒"), (30, "每 30 秒"), (60, "每 60 秒")):
        href = "/hl" if n == 0 else f"/hl?refresh={n}"
        cls = "btn" if n == cur else "btn gray"
        links.append(f"<a class='{cls}' href='{href}'>{label}</a>")
    # 主题切换（黑/白/跟随系统）：与 /admin 面板同一套 cookie 与路由
    theme_row = theme_switch_html("overview", theme, k)
    return ("<div class='foot'><span class='muted'>自动刷新：</span>"
            + " ".join(links)
            + "<span class='spacer'></span>"
            + f"<span class='muted'>本页只读、不持私钥 &middot; schema v{SCHEMA_VERSION}</span></div>"
            + theme_row)


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
    theme = normalize_theme(p.get("theme"))
    attr = theme_attr(theme)
    attr_html = f" data-theme='{attr}'" if attr else ""
    return (
        f"<!doctype html><html lang='zh-CN'{attr_html}><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{ICON_LINKS}"
        f"{meta}<title>Hyperliquid 执行器监控</title>{THEME_CSS}{DASH_CSS}</head>"
        f"<body><div class='wrap'>{body}{_render_footer(refresh, theme, str(p.get('admin_k') or ''))}</div></body></html>")


# ---------------------------------------------------------------- 路由
def _admin_required(deps: DashboardDeps, k: str, request: "Request" = None) -> bool:
    """可选鉴权：仅 HL_DASH_REQUIRE_ADMIN=1 时生效（默认关闭，便于内网直连）。

    返回 True = 应拒绝（401）。未开启或未配 ADMIN_TOKEN 一律放行。
    与 main.hl_guard 同口径：?k= 命中或 cma_admin cookie 命中任一即放行
    （审计三轮 L：以前只认 k，开 REQUIRE_ADMIN 后带 cookie 的浏览器会话也被 401）。
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
    if str(k or "") == token:
        return False
    if request is not None:
        try:
            if str(request.cookies.get("cma_admin") or "") == token:
                return False
        except Exception:                                        # noqa: BLE001
            pass
    return True


def build_router(deps: DashboardDeps) -> Any:
    """构建监控路由：/hl（HTML）、/api/hl/status、/api/hl/equity、/api/hl/snapshot。"""
    router = APIRouter(tags=["hl"])

    @router.get("/hl", response_class=HTMLResponse)
    def hl_page(request: Request, refresh: int = 0, k: str = ""):
        if _admin_required(deps, k, request):
            raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
        payload = dict(status_payload(deps))
        payload["refresh"] = refresh if refresh in (10, 30, 60) else 0
        payload["theme"] = normalize_theme(request.cookies.get("cma_theme"))
        payload["admin_k"] = str(k or "")
        return HTMLResponse(render_dashboard(payload))

    @router.get("/api/hl/status")
    def hl_status(request: Request, k: str = ""):
        if _admin_required(deps, k, request):
            raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
        return status_payload(deps)

    @router.get("/api/hl/equity")
    def hl_equity(request: Request, k: str = ""):
        if _admin_required(deps, k, request):
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

    @router.get("/api/hl/why")
    def hl_why(request: Request, k: str = ""):
        """`/why` 决策解释（JSON，只读）：每标的一行六步 + 闸门状态。

        与面板同一份数据（`_why_rows` → `app.why.build_rows`），方便运维用 curl 看
        「现在为什么不开仓」，而不必去翻 HTML。只读，无任何副作用。
        """
        if _admin_required(deps, k, request):
            raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
        rows = _why_rows(deps)
        return {"ts": _num(_call(deps.now_fn), time.time()), "symbols": rows}

    @router.get("/api/hl/snapshot")
    def hl_snapshot(request: Request, k: str = ""):
        if _admin_required(deps, k, request):
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
