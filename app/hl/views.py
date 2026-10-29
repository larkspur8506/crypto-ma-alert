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
from urllib.parse import quote

from app import posfields
from app.hl import flows as hl_flows
from app.hl import dd as hl_dd
from app.ui_theme import (ICON_LINKS, SYMBOL_CSS, THEME_CSS, H1_BLOCK, PRODUCT_NAME,
                          normalize_theme, theme_attr, theme_switch_html)

logger = logging.getLogger(__name__)

# 公开导出（含面板合并时复用的 _render_body / DASH_CSS；其余为原有公开 API）。
__all__ = [
    "SCHEMA_VERSION", "MAINNET_DEFAULT", "DashboardDeps",
    "status_payload", "render_dashboard", "build_router",
    "DASH_CSS", "_render_body", "THEME_CSS",
    "ROW_CHOICES", "DEFAULT_ROWS", "ROWS_COOKIE", "normalize_rows", "_signals_html",
    "_stuck_badge_html", "_agent_badge_html",
]

# 长表每页行数（手机优先）：默认 20，面板上可切 10/20/50，选择存 cookie `cma_rows`。
# 只作用于「记录/历史」类长表（记录页的下单/成交/信号/提醒/事件五块）；盈亏曲线不受影响。
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
_HL_ST_TXT = {"closing": "平仓待核对", "mismatch": "账实不符", "aborted": "开仓中止",
              # 执行器平仓后会把 hl_status 写成 closed（executor 关账路径），不映射的话
              # 状态列会出现「已平仓 + closed」两种写法同现（原始英文值漏到界面上）。
              "closed": "已平仓"}
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


def _asub(label: str, value: str, cls: str = "", title: str = "") -> str:
    """账户卡次级行（P2）：标签+数值的小字单元，flex 换行排开，不再逐行键值。

    cls 着色在数值 <b> 上（标签恒 muted 色，由 .acct-sub 统一给）。
    """
    c = f" class='{cls}'" if cls else ""
    t = f" title='{_esc(title)}'" if title else ""
    return f"<span{t}>{label} <b{c}>{value}</b></span>"


def _badge(dot_cls: str, label: str, value_html: str) -> str:
    return (f"<span class='badge'><span class='{dot_cls}'>&#9679;</span> "
            f"{label} <b>{value_html}</b></span>")


def _strip_badge(dot_cls: str, label: str, value: str, title: str = "") -> str:
    """状态条徽章（/hl 与 /admin 两条状态条共用一种写法；admin_ui._status_strip 也用它）。"""
    t = f" title='{_esc(title)}'" if title else ""
    return (f"<span class='badge'{t}><span class='{dot_cls}'>&#9679;</span> "
            f"{label} <b>{value}</b></span>")


def _stuck_badge_html(system: Any) -> str:
    """卡单徽章（P3 R2）：只在 >0 时出现在状态条（琥珀）。

    从「系统」卡搬来 —— 口径（持续 3 分钟未推进才计入）随迁进 title（桌面增强，
    规格 R2 明确允许）；正常态状态条不出现「卡单」字样。
    """
    n = _opt_int(_as_dict(system).get("stuck_orders"))
    if not n or n <= 0:
        return ""
    return _strip_badge("warn", "卡单", f"{n} 笔",
                        "卡在 intent/sent 的下单：持续 3 分钟未推进才计入")


def _agent_badge_html(agent: Any) -> str:
    """API 有效期徽章（P3 R3）：状态条是唯一常驻出处。

    ≤14 天琥珀、≤7 天红、已触发自动清仓红；不限期绿；读不到到期灰（如实显示，
    下一个整点会自动检查）。数字来自 hl_agent_state（DB 缓存，零网络调用）。
    """
    a = _as_dict(agent)
    level = str(a.get("level") or "")
    cleanup = str(a.get("cleanup") or "none")
    days = _opt_int(a.get("days_left"))
    if cleanup == "done":
        return _strip_badge("neg", "API", "已清仓", "API 授权已到期并触发自动清仓")
    if level == "no_expiry":
        return _strip_badge("pos", "API", "不限期", "API 授权未设到期时间")
    if days is None:
        return _strip_badge("muted", "API", "—", "API 授权有效期未知（每个整点自动检查）")
    cls = "neg" if days <= 7 else ("warn" if days <= 14 else "pos")
    return _strip_badge(cls, "API", f"{days} 天",
                        "API 授权剩余有效期；≤3 天且有仓位时自动清仓（「开关」页可关）")


def _orders_normal(positions: List[dict], orders: List[dict]) -> bool:
    """挂单表按需渲染判定（P3 R1）。

    正常态 = 每个持仓标的恰好一条 reduce-only 触发单、无其它挂单、无孤儿 ——
    此时止损已由仓位表「止损 ✔️」列唯一表达，挂单表整块不渲染。
    判不清（挂单不可信 / 数据缺失）一律返回 False = 照常渲染（fail-open：
    宁可多显示，不可漏报）。纯渲染层判定，/api/hl/status 的 orders_open 口径不变。
    """
    # 挂单不可信（快照 orders_trusted=False）→ 止损列本身是「? 无法确认」，判不清。
    if any(_as_dict(p).get("stop_unknown") for p in positions):
        return False
    by_coin: Dict[str, int] = {}
    for od in (_as_dict(x) for x in orders):
        coin = str(od.get("coin") or "")
        if not (od.get("is_trigger") and od.get("reduce_only")):
            return False                      # 非止损类型挂单 = 异常态
        by_coin[coin] = by_coin.get(coin, 0) + 1
    pos_coins = {str(_as_dict(p).get("coin") or "") for p in positions}
    # 集合不等 = 有孤儿挂单 / 有仓没单；再逐标的数一遍保证「恰好一条」。
    if set(by_coin) != pos_coins:
        return False
    return all(n == 1 for n in by_coin.values())


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


# 回撤熔断（DD Guard）状态词。iOS 没有 title 悬停 → 状态必须是**可见文字**（§7）。
# 规格只给了四个词（正常/已达告警线/已熔断/暂不可用）；总开关关掉时**复用「暂不可用」**
# 而不是新造第五个词，把原因写进括号里 —— 判据确实不可用，词汇表保持封闭。
_DD_ST_TXT = {
    "normal": "正常",
    "warn": "已达告警线",
    # 【低3】「已熔断」必须带上「停手中」：这个词只允许在 kill **实态为真** 时出现。
    # 旧文案就四个字，用户无法判断「是还在停手，还是指标越线但已经恢复交易」。
    "killed": "已熔断（停手中）",
    # 【低3】DD 越了熔断线、但 kill 实态是假的（例如人工解熔断后峰值还没重置，
    # 或 monitor 还没跑到下一轮）：既不能说「已熔断」（已恢复交易，会误导人以为
    # 还在停手），也不能说「正常」（下一轮就会重新熔断，会误导人现在可以开仓）。
    "over_kill": "已越熔断线（当前未停手）",
    "unavailable": "暂不可用",
    "off": "暂不可用",
}


def _dd_meta(deps: DashboardDeps, cfg: Any, flows_ok: bool) -> Dict[str, Any]:
    """总览「策略回撤」那一行的数据源：**只读**持久化状态，不重算 TWR 链。

    为什么不重算：判据是执行器每轮推进的复利链（每轮一格），面板若按快照逐点重算，
    得到的是另一条曲线（采样密度不同）→ 用户看到的数字与真正触发熔断的数字不一致，
    那比不显示更糟。这里只做「读状态 + 算 DD + 选状态词」。
    """
    risk = _num(getattr(cfg, "risk_pct", 0.02) if cfg is not None else 0.02, 0.02)
    th = hl_dd.thresholds(
        risk,
        _num(getattr(cfg, "hl_dd_warn_pct", 0.0) if cfg is not None else 0.0),
        _num(getattr(cfg, "hl_dd_kill_pct", 0.0) if cfg is not None else 0.0))
    out: Dict[str, Any] = {
        "dd_ok": False, "dd_status": "unavailable",
        "dd_status_text": _DD_ST_TXT["unavailable"],
        "dd_pct": None, "dd_index": None, "dd_peak_index": None, "dd_asof": None,
        "dd_warn_pct": round(th["warn"], 6), "dd_kill_pct": round(th["kill"], 6),
        "dd_auto": bool(th["auto"]), "dd_note": "",
    }
    if not bool(getattr(cfg, "hl_dd_guard", True) if cfg is not None else True):
        out.update(dd_status="off", dd_status_text=_DD_ST_TXT["off"],
                   dd_note="回撤熔断已关闭")
        return out
    if not flows_ok:
        # 流水读不到 → 指数的历史段不可信（少一笔入金就会被当成亏损）→ 不显示数字
        out["dd_note"] = "出入金流水读取失败"
        return out
    st = hl_dd.load_state(deps.db)
    if not st:
        out["dd_note"] = "尚未计算（等执行器跑一轮）"
        return out
    pct = hl_dd.drawdown(st.get("index"), st.get("peak_index"))
    if pct is None:
        out["dd_note"] = "状态数据不完整"
        return out
    try:
        killed = bool(deps.db.hl_kill())
    except Exception as e:                                       # noqa: BLE001
        logger.warning("[hl-views] hl_kill 失败（按未熔断显示）：%s", e)
        killed = False
    # 「已熔断」既看当前回撤，也看 kill 的来源：指数回升后 DD 可能掉回线下，
    # 但停手还在（dd 永不自解）—— 那种时候显示「正常」会让人以为可以开仓了。
    origin = str(_config_get(deps, "hl_exec_kill_origin", "") or "")
    over = pct >= th["kill"]
    # 【低3】「已熔断」只允许在 kill **实态为真** 时出现。旧判据
    # `pct >= kill or (killed and origin == "dd")` 把「DD 越线但已恢复交易」
    # 也显示成「已熔断」—— 用户以为还在停手，实际下一轮就要重新熔断前仓位已开得出去。
    # 四态：killed（真停手）/ over_kill（越线未停手）/ warn / normal。
    if killed and (origin == "dd" or over):
        status = "killed"
    elif over:
        status = "over_kill"
    elif pct >= th["warn"]:
        status = "warn"
    else:
        status = "normal"
    out.update(dd_ok=True, dd_status=status, dd_status_text=_DD_ST_TXT[status],
               dd_pct=round(pct, 6), dd_index=round(float(st["index"]), 4),
               dd_peak_index=round(float(st["peak_index"]), 4),
               dd_asof=float(st.get("updated_at") or 0.0) or None)
    # 【v2·高1】执行器「本轮没判」的说明（只有流水接口失败/无地址时才写，闸门内跳过
    # 属正常节奏、不写）。这里只读、不自己推断 —— 判据有没有真的跑过只有执行器知道。
    wait = str(_config_get(deps, hl_dd.WAIT_KEY, "") or "").strip()
    if wait:
        out["dd_wait_note"] = wait
    return out



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


def _perf_stats(rows: List[Any]) -> Dict[str, Any]:
    """「策略绩效（上线以来）」的只读聚合：positions 表 status='closed' 行 → 统计量。

    过滤口径（血泪防坑，见规格 P1）：
    - 库里混有 2026-10-14 彩排/演练的**合成行**：其 signal_bar_ms 不在 4h 网格上
      （4h = 14_400_000 ms，UTC 对齐），不对齐的一律不计入 —— 宁可少算一笔真实交易，
      也不让排练数据污染均R/胜率。
    - ``r_multiple`` 为 NULL 的行不计入笔数（没算过 R ≠ R 为 0）。
    - side 约定 1=多 / -1=空（与 positions 表 DDL 及既有展示层一致，历史上映射写反过，
      这里只按 DDL 口径分组，不做任何「聪明」的纠偏）。
    """
    bar_ms = 4 * 3600 * 1000                     # 4h 信号网格（UTC 对齐）
    n = long = short = wins = 0
    r_sum = 0.0
    for r in rows or []:
        d = _rowd(r)
        bar = _opt_num(d.get("signal_bar_ms"))
        if bar is None or bar <= 0 or int(bar) % bar_ms != 0:
            continue                             # 合成行/脏行：不计入
        r = _opt_num(d.get("r_multiple"))
        if r is None:
            continue                             # 没算过 R：不计入笔数
        n += 1
        if int(_num(d.get("side"), 1)) < 0:
            short += 1
        else:
            long += 1
        r_sum += r
        if r > 0:
            wins += 1
    if n <= 0:
        return {"n": 0, "long": 0, "short": 0, "wins": 0, "avg_r": None,
                "win_rate": None, "total_r": None}
    return {"n": n, "long": long, "short": short, "wins": wins,
            "avg_r": round(r_sum / n, 4),
            "win_rate": round(wins / n, 4),
            "total_r": round(r_sum, 4)}


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
    # 读数来自调度器快照时（阶段 B），上面那个 latency_ms 只是「读一次 SQLite」的耗时（毫秒级），
    # 不是网络延迟。快照里带了调度器写入时实测的那一轮耗时（`panel_net`）就用它，并标记来源 +
    # 采样距今多久 —— 面板据此说明「这是 N 分钟前那一轮的耗时」，而不是假装刚刚测过。
    latency_from_snapshot = False
    latency_age_s: Optional[float] = None
    panel_net = snap.get("panel_net")
    stored_ms = _opt_num(panel_net.get("latency_ms")) if isinstance(panel_net, dict) else None
    if stored_ms is not None and stored_ms > 0:      # ≤0 视为没测出来
        latency_ms = stored_ms
        latency_from_snapshot = True
        sampled = _opt_num(panel_net.get("ts"))
        if sampled:
            latency_age_s = round(max(0.0, now - sampled), 1)
    elif snap.get("panel_snapshot_age_s") is not None:
        # 读数确实来自快照、但快照里没有采样值（升级前写的老行）⇒ 报「没测」（None），
        # 不能把上面那次 SQLite 读取的 0~1 ms 当成网络延迟（这正是不显示「1 ms」的原因）。
        latency_ms = None
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
            # 单粒度资金费（feat/funds-ledger）：快照的 cumFunding.sinceOpen 是**成本
            # 视角**（付出为正），与面板「正=收入」口径相反，此处取反 —— 与账户级
            # funding_total 同一套换算（见下方 funding_total 注释）。
            "funding_income": -_num(d.get("funding_since_open")),
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
    # 单粒度资金费（feat/funds-ledger）：{position_id: Σ amount_usd}，一次分组查询
    # 拿全量，循环里只查字典。给「已平仓行」的资金费小字用（持仓中的在仓位卡列）。
    fund_by_pos = _db_call(deps.db, "hl_funding_by_position") or {}
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
        # 单粒度资金费（feat/funds-ledger）：**只挂 HL 仓行**（非 HL 忽略），且该仓
        # 在 hl_funding 里有归集行；没有资金费记录的仓不带键，渲染层不加小字。
        if posfields.is_hl(d) and d.get("id") is not None:
            fs = fund_by_pos.get(int(d["id"]))
            if fs is not None:
                sig["funding_sum"] = round(fs, 6)
        signals.append(sig)

    # ---------------- 策略绩效（上线以来，只读聚合，零行情调用）----------------
    # 纯读库（positions closed 行）；口径与过滤见 _perf_stats。放在 payload 里而非
    # 渲染层现查：/api/hl/status 的调用方（看守/巡检）也能拿到同一份数字。
    perf = {"n": 0, "long": 0, "short": 0, "wins": 0, "avg_r": None,
            "win_rate": None, "total_r": None}
    try:
        perf = _perf_stats(_db_call(deps.db, "closed_positions", 0.0) or [])
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 策略绩效聚合失败（按空数据处理）：%s", e)

    # ---------------- 盈亏曲线（已剔除出入金）+ TWR 指数 ----------------
    # 旧版这里直接画 hl_snapshots.equity —— 出入金会把曲线顶出一个台阶（本账户
    # 2026-10-14 +100.79、10-21 +901.80 两笔转入），看着像暴赚。现在主曲线是
    # 「剔除出入金的累计盈亏」，另叠一条时间加权收益指数（TWR，起点 100）：
    # 前者回答「交易赚了多少 U」，后者回答「收益率多少」（入金的时点不影响它）。
    # 计算全在 app/hl/flows.py（纯本地，不打接口）；equity 仍用于熔断基线与顶部数字。
    snaps = _db_call(deps.db, "hl_snapshots", now - 30 * _DAY, 20000) or []
    flows = _db_call(deps.db, "hl_flows_since", 0.0) or []
    # 「流水可信吗」：有过一次**成功**同步（hl_flows_synced_at）或本地已有流水。
    # 两者都没有 = 从没拉成功过 → 不画曲线（不退回权益曲线：那正是要删掉的东西，
    # 而且会给出误导性走势），只显示一句提示。
    synced_at = _opt_num(_config_get(deps, "hl_flows_synced_at", "0")) or 0.0
    flows_ok = bool(flows) or synced_at > 0
    curve = hl_flows.curve(
        snaps, flows, now=now, window_s=30 * _DAY, flows_ok=flows_ok,
        flows_note="" if flows_ok else "出入金流水读取失败，盈亏曲线暂不可用（系统会自动重试）")
    # 回撤熔断（DD Guard）字段并进 curve.meta：面板与 /api/hl/status 共用同一份数字
    # （§10.5 要求 curve meta 里出现 DD 字段）。**只读**执行器持久化的状态。
    if isinstance(curve.get("meta"), dict):
        curve["meta"].update(_dd_meta(deps, cfg, flows_ok))
        # 各时间窗的净入金（span 切换时摘要行要跟着窗口走，见 _render_body）。
        # 「全部」就是 net_deposit 本身，不重复存（同一事实只存一份）。
        try:
            dep_7 = dep_30 = 0.0
            for f in flows:
                d = _rowd(f)
                ts, amt = _num(d.get("ts")), _num(d.get("amount"))
                if ts >= now - 7 * _DAY:
                    dep_7 += amt
                if ts >= now - 30 * _DAY:
                    dep_30 += amt
            curve["meta"]["net_deposit_span"] = {
                "7": round(dep_7, 6), "30": round(dep_30, 6)}
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[hl-views] 分窗净入金计算失败（渲染层回退全期口径）：%s", e)

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
        # 故障判定与引擎心跳同一口径（engine.heartbeat_text）：「最近一次失败是否
        # 晚于最近一次成功」——只看 last_ok_at 的话，出过一次错的源永远显示正常
        #（mark_feed 失败路径不清 last_ok_at）。
        ok_at = _opt_num(d.get("last_ok_at")) or 0.0
        err_at = _opt_num(d.get("error_at")) or 0.0
        ok = ok_at > 0 and ok_at >= err_at
        feed.append({
            "symbol": d.get("symbol"),          # 内部键，保留
            "coin": _disp(d.get("symbol")),     # 展示用
            "source": d.get("source"),
            "last_ok_at": _opt_num(d.get("last_ok_at")),
            "last_error": d.get("last_error"),
            # 最后一次失败的时间（P2 合并后「三标的动态」的失败小字行要用它显示
            # 「最后错误 HH:MM」；老 payload 没带，读不到就如实显示 —）
            "error_at": _opt_num(d.get("error_at")),
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

    # API 有效期（P3 R3）：状态条徽章的数据源。只读 hl_agent_state DB 缓存
    #（执行器每个整点刷新），面板渲染零网络调用；缺失/损坏一律空 dict → 徽章显「—」。
    agent_out: dict = {}
    try:
        from app.hl import agent as _agent_mod
        st = _agent_mod.read_state(deps.db) if deps.db is not None else {}
        st = st if isinstance(st, dict) else {}
        lvl = str(st.get("level") or "")
        if not lvl:
            # 老 state 没有 level 字段时的兜底（有 until 就按毫秒重算），口径同 admin_ui.render_agent_card
            lvl = (_agent_mod.level_ms(st.get("valid_until_ms"), st.get("checked_ms"))
                   if st.get("valid_until_ms") else "unknown")
        agent_out = {"days_left": _opt_int(st.get("days_left")), "level": lvl,
                     "cleanup": str(st.get("cleanup") or "none")}
    except Exception as e:                                            # noqa: BLE001
        logger.warning("[hl-views] 读 API 有效期状态失败（徽章显 —）：%s", e)

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
            "latency_from_snapshot": latency_from_snapshot,
            "latency_age_s": latency_age_s,
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
        # 盈亏曲线（已剔除出入金）+ TWR 指数。旧键 equity_curve（原始权益曲线）已按
        # 用户要求**整个删掉**，不是开关 —— 全仓只有这一个图表源。
        "curve": curve,
        # 策略绩效（上线以来）：总览卡片数据源（只读聚合，见 _perf_stats）。
        "perf": perf,
        # API 有效期（P3 R3）：状态条徽章唯一常驻出处；/admin 的 get_payload 会用
        # deps.agent_state_fn 的同一份缓存覆盖（注入测试路径）。
        "agent": agent_out,
        "events": events,
    }


# ---------------------------------------------------------------- SVG 盈亏曲线
_CURVE_EMPTY = "暂无数据（等执行器落快照一次 hl_snapshots）"


def _curve_points(v: Any) -> List[List[float]]:
    """payload 的 [[ts, val], ...] → 干净的 float 点列（脏数据直接丢）。"""
    out: List[List[float]] = []
    for x in _as_list(v):
        if isinstance(x, (list, tuple)) and len(x) >= 2:
            t, y = _opt_num(x[0]), _opt_num(x[1])
            if t is not None and y is not None:
                out.append([t, y])
    return out


SPAN_CHOICES = (("7", "近 7 天"), ("30", "近 30 天"), ("all", "全部"))
# 盈亏曲线时间窗（P1）：GET span ∈ 7/30/all，缺参/非法值回落 7。「全部」受快照
# 滚动清理（hl_snapshots 约 30 天）限制，图例里必须写明，不能让人以为有历史全量。
_SNAPSHOT_RETENTION_NOTE = "快照保留约 30 天，更早数据已清理"


def normalize_span(v: Any) -> str:
    """span 参数白名单：只认 7/30/all，其余（含缺参）一律回落 7。"""
    s = str(v or "").strip().lower()
    return s if s in ("7", "30", "all") else "7"


def _curve_window(pnl: List[List[float]], twr: List[List[float]],
                  span: str = "7") -> tuple:
    """按所选时间窗切展示点列，返回 (pnl, twr, 标题, 实际天数)。

    - 标题按**实际首尾**换算（快照不足所选窗口时如实标注，不编「近 7 天」）；
      「全部」固定标「全部」。
    - 窗口内不足 2 个点画不成线 → 回落用实际可得的全序列（旧「近 7 天不足回 30 天」
      行为的推广：宁多画不空屏，但标题按真实跨度写）。
    """
    span = normalize_span(span)
    if not pnl:
        return [], [], "近 7 天", 0.0
    if span == "all":
        sel_p, sel_t = pnl, twr
    else:
        days = int(span)
        cut = pnl[-1][0] - days * _DAY
        sel_p = [p for p in pnl if p[0] >= cut]
        sel_t = [q for q in twr if q[0] >= cut]
        if len(sel_p) < 2:
            sel_p, sel_t = pnl, twr
    title = "全部" if span == "all" else f"近 {max(1, int((sel_p[-1][0] - sel_p[0][0]) / _DAY + 0.5))} 天"
    return sel_p, sel_t, title, (sel_p[-1][0] - sel_p[0][0]) / _DAY if len(sel_p) >= 2 else 0.0


def _downsample(n: int, cap: int) -> List[int]:
    """按像素列降采样（抽样下标，审计三轮 L）：30 天 5 分钟一张快照 ≈ 8600 点，全量内联
    polyline 会让页面体积和渲染开销线性膨胀。返回要保留的下标列表。"""
    if n <= cap:
        return list(range(n))
    step = n / float(cap)
    idx = [min(n - 1, int(i * step)) for i in range(cap)]
    if idx[-1] != n - 1:
        idx[-1] = n - 1
    return idx


def _pnl_svg(pnl: List[List[float]], twr: List[List[float]],
             width: int = 760, height: int = 160) -> str:
    """手写内联 SVG（零外部资源）：

    - 主曲线 = 累计盈亏（USDC，左轴）：0 基准线灰色虚线；正区间绿、负区间红
      —— 用**分段折线**实现（穿过 0 时插值出交点，两段各自着色），不是渐变、不靠 JS。
    - 叠加曲线 = 收益指数 TWR（独立缩放，右轴）：虚线；右侧标出该轴的 min/max。
    """
    if len(pnl) < 2:
        return (f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' "
                f"aria-label='盈亏曲线'>"
                f"<text x='{width / 2}' y='{height / 2}' text-anchor='middle' "
                f"class='gtext' font-size='12'>{_CURVE_EMPTY}</text></svg>")

    pad_l, pad_r, pad_t, pad_b = 6, 46, 12, 16
    idx = _downsample(len(pnl), width * 2)
    pts_p = [pnl[i] for i in idx]
    # TWR 与 pnl 同一时间轴（同一份快照序列）。长度不一致时按时间对齐，不强凑。
    if len(twr) == len(pnl):
        pts_t = [twr[i] for i in idx]
    else:
        by_ts = {round(p[0], 3): p[1] for p in twr}
        pts_t = [[p[0], by_ts[round(p[0], 3)]] for p in pts_p if round(p[0], 3) in by_ts]

    xs = [p[0] for p in pts_p]
    ys = [p[1] for p in pts_p]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(min(ys), 0.0), max(max(ys), 0.0)      # 0 一定在可视范围内
    span_x = (x1 - x0) or 1.0
    span_y = (y1 - y0) or 1.0

    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def sx(x: float) -> float:
        return round(pad_l + (x - x0) / span_x * plot_w, 1)

    def sy(y: float) -> float:
        return round(height - pad_b - (y - y0) / span_y * plot_h, 1)

    # ---- 主曲线：按 0 轴切段（正绿负红），穿过 0 时插值出交点
    segs: List[tuple] = []
    cur_cls: Optional[str] = None
    cur: List[tuple] = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        cls = "gpos" if y >= 0 else "gneg"
        px, py = sx(x), sy(y)
        if cur_cls is None:
            cur_cls, cur = cls, [(px, py)]
            continue
        if cls == cur_cls:
            cur.append((px, py))
            continue
        prev_x, prev_y = xs[i - 1], ys[i - 1]
        denom = (y - prev_y) or (1e-12 if y >= 0 else -1e-12)
        x_cross = prev_x + (-prev_y) / denom * (x - prev_x)
        cross = (sx(x_cross), sy(0.0))
        cur.append(cross)
        segs.append((cur_cls, cur))
        cur_cls, cur = cls, [cross, (px, py)]
    if cur:
        segs.append((cur_cls, cur))

    paths = []
    for cls, pts in segs:
        if len(pts) < 2:
            pts = [pts[0], (pts[0][0] + 0.1, pts[0][1])]   # 单点段也要可见
        coords = " ".join(f"{px},{py}" for px, py in pts)
        paths.append(f"<polyline points='{coords}' fill='none' class='{cls}' "
                     f"stroke-width='2' stroke-linejoin='round' stroke-linecap='round'/>")

    # ---- 叠加曲线：TWR 独立缩放（右轴）
    right = ""
    if len(pts_t) >= 2:
        ts_ = [p[0] for p in pts_t]
        vs = [p[1] for p in pts_t]
        t0, t1 = min(vs), max(vs)
        pad = (t1 - t0) * 0.08 or max(abs(t1) * 0.01, 1.0)
        t0, t1 = t0 - pad, t1 + pad
        span_t = (t1 - t0) or 1.0

        def ty(v: float) -> float:
            return round(height - pad_b - (v - t0) / span_t * plot_h, 1)

        coords = " ".join(f"{sx(x)},{ty(v)}" for x, v in zip(ts_, vs))
        right = (f"<polyline points='{coords}' fill='none' class='gtwr' stroke-width='1.6' "
                 f"stroke-linejoin='round' stroke-linecap='round'/>"
                 # 右轴 min/max 两个刻度（可见文字，不靠悬停）
                 + f"<text x='{width - pad_r + 3}' y='{ty(max(vs)) + 4}' class='gtext' "
                   f"font-size='10'>{max(vs):,.1f}</text>"
                 + f"<text x='{width - pad_r + 3}' y='{ty(min(vs)) + 4}' class='gtext' "
                   f"font-size='10'>{min(vs):,.1f}</text>")

    grid = "".join(
        f"<line x1='{pad_l}' y1='{round(pad_t + f * plot_h, 1)}' "
        f"x2='{width - pad_r}' y2='{round(pad_t + f * plot_h, 1)}' "
        f"class='gline' stroke-width='1'/>"
        for f in (0.25, 0.5, 0.75))
    zero_y = sy(0.0)
    last_x, last_y = sx(xs[-1]), sy(ys[-1])

    return (
        f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' aria-label='盈亏曲线'>"
        + grid
        # 0 基准线：盈亏曲线读的是「赚还是亏」，这条线必须在
        + f"<line x1='{pad_l}' y1='{zero_y}' x2='{width - pad_r}' y2='{zero_y}' "
        f"class='gbase' stroke-width='1' stroke-dasharray='4 4' opacity='0.55'/>"
        + right
        + "".join(paths)
        + f"<circle cx='{last_x}' cy='{last_y}' r='3' class='gdot'/>"
        + "</svg>")
# ---------------------------------------------------------------- HTML 渲染
DASH_CSS = """<style>
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
     background:var(--bg);color:var(--fg);line-height:1.55;font-size:14px;padding:14px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:19px;margin:0 0 2px;font-weight:600}
/* 头部副题（P3 命名重设计）：h1 下一行 muted 小字，一句话说清标的/周期/交易所。 */
.h1sub{margin:0 0 10px;font-size:12px;color:var(--muted)}
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
svg.chart .gpos{stroke:var(--pos)}
svg.chart .gneg{stroke:var(--neg)}
svg.chart .gtwr{stroke:var(--link);stroke-dasharray:5 4;opacity:.9}
svg.chart .gdot{fill:var(--link)}
.btn{display:inline-block;background:var(--btn);color:#fff;border-radius:6px;padding:5px 11px;
     font-size:12px;text-decoration:none;white-space:nowrap}
.btn.gray{background:var(--btn-gray)}
.foot{margin:18px 0 6px;color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;
      gap:8px;align-items:center}
.foot .spacer{flex:1}
a{color:var(--link)}
/* 账户卡（P2 分层）：顶层 3 关键数大字一行；次级项 muted 小字 flex 换行排开
   （不再逐行键值，同一卡内少一半行高）。 */
.acct-top{display:flex;flex-wrap:wrap;gap:6px 24px;padding:2px 0 8px;
          border-bottom:1px solid var(--line2)}
.acct-top b{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}
.acct-top .muted{font-size:11px}
.acct-sub{display:flex;flex-wrap:wrap;gap:2px 16px;padding:8px 0 2px;
          font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.acct-sub b{font-weight:600;font-variant-numeric:tabular-nums}
/* 账户卡次级折叠（P3 R4）与 API 有效期折叠（P3 R3）：原生 <details>，零 JS。
   summary 摸起来要像可点的：指针光标 + muted 小字。 */
details.acct-details>summary,details.card>summary{cursor:pointer;color:var(--muted);
   font-size:12.5px;letter-spacing:1px;padding:2px 0}
details.card>summary{font-size:12px;color:var(--h2)}
/* 仓位表窄屏适配（P2）：小字行 tr.subm 默认隐藏（宽屏 13 列齐全）；≤640px 时
   隐藏 .hidem 列、显示小字行 —— 显隐只靠 CSS media query，零 JS。 */
tr.subm{display:none}
tr.subm td{white-space:normal;font-size:11.5px;color:var(--muted)}
@media (max-width:640px){body{padding:9px;font-size:13px}h1{font-size:17px}
    .badge{padding:5px 10px;font-size:11px}.grid{grid-template-columns:1fr}
    .hidem{display:none}tr.subm{display:table-row}}
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
        # 精简（2026-10-29 用户要求）：只报当下的带宽与门槛两个数，密集根数等细节
        # 不再重复（状态词里已有 N/6）。字段缺失（旧缓存/降级路径）回落 strategy 原文。
        band_cur, thr_cur = _opt_num(r.get("band_cur")), _opt_num(r.get("thr_cur"))
        if band_cur is not None and thr_cur is not None:
            txt = f"带宽 {band_cur * 100:.2f}% · 门槛 {thr_cur * 100:.2f}%"
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


def _sym_card(r: Dict[str, Any], feed_map: Dict[str, Dict[str, Any]] = None,
              last_bar: Dict[str, Any] = None) -> str:
    """一个标的的卡片：现价 + 状态词 + 距离行（持仓只给离场线；无仓给两条入场线）
    + 原因行 + 闸门异常行 + 数据源/已处理到小字行。现价全页只在这里出现一次
    （两块合并的核心约束）。
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
    # 数据源 / 已处理到小字行（P2 合并）：放在该标的块末尾、原因/闸门行之后。
    o.append(_feed_line(r, feed_map or {}, last_bar or {}))
    o.append("</div>")
    return "".join(o)


def _feed_line(r: Dict[str, Any], feed_map: Dict[str, Dict[str, Any]],
               last_bar: Dict[str, Any]) -> str:
    """标的卡末尾的数据源小字行（2026-10-29 精简：正常态不渲染，异常才出现）。

    正常：不产生 DOM（正常态静默；信号源固定 hl-candle、fail-closed，正常没话可说）。
    失败：`数据源 hl-candle 失败（最后错误 HH:MM） · 已处理到 …`，用 neg 样式醒目
    （源失败即 fail-closed 跳过本轮，这是最该被看见的异常）。字段缺失时如实写
    「无记录 / —」，绝不编时间。feed 里没这个标的 → 「数据源 无记录」。
    """
    sym = str(r.get("symbol") or "")
    fd = feed_map.get(sym)
    bar_txt = _fmt_dt((last_bar or {}).get(sym), "%m-%d %H:%M")
    if fd is None:
        return _whyline(f"数据源 无记录 · 已处理到 {bar_txt}", "muted")
    # 失败行的 source 可能为空（引擎 mark_feed 失败路径不写源名）；本系统信号源
    # 固定 hl-candle（fail-closed、不换源），空缺时兜底写实名，不算编造。
    src = str(fd.get("source") or "hl-candle")
    if fd.get("ok"):
        return ""
    err_at = _fmt_dt(fd.get("error_at"), "%H:%M") if fd.get("error_at") else "—"
    return _whyline(f"数据源 {src} 失败（最后错误 {err_at}） · 已处理到 {bar_txt}", "neg")


def _symbol_dynamics(p: dict) -> str:
    """「三标的动态」区块（2026-10-29 起与「仓位」表对换：仓位在最顶，本块在其后）。

    合并后这里同时回答「离触发线多远」（距离行）和「卡在哪一步 / 被闸门挡了吗」
    （原因行 / 闸门异常行）—— 原「为什么没开仓」区块已删除。P2 再吸收「数据源健康」
    与「已处理到」两张表：每标的名下加一行数据源小字（见 _feed_line），
    三张块合成一张卡。

    fail-soft：某标的两侧数据都缺失 → 价格显示「—」、子行整体不渲染，其余标的不受影响；
    整块任何异常都不能弄挂总览页（调用点在 try 里）。
    区块头时间 = 三个标的里最新的 updated_at（北京时间，与面板其它时间同口径）。
    """
    rows = _merged_cards(p)
    if not rows:
        return ""
    system = _as_dict(p.get("system"))
    feed_map: Dict[str, Dict[str, Any]] = {}
    for fd in (_as_dict(x) for x in _as_list(system.get("feed"))):
        sym = str(fd.get("symbol") or "")
        if sym:
            feed_map[sym] = fd
    last_bar = _as_dict(system.get("last_bar"))
    stamps = [t for t in (_opt_num(r.get("updated_at")) for r in rows) if t]
    when = _fmt_dt(max(stamps), "%H:%M") if stamps else "—"
    o = [f"<h2>三标的动态<span class=\"muted\">行情 {when} · 每 30 分钟随信号 tick 更新</span></h2>",
         "<div class=\"card\"><div class=\"grid\">"]
    for r in rows:
        o.append(_sym_card(r, feed_map, last_bar))
    # 图例行已按用户要求整段删除（2026-10-29）；口径细节留在 title 悬停里。
    o.append("</div>")
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
        hl_txt = hl_status_text(hl_st) if hl_st else ""
        # hl_status 与主状态同词时不显示第二行（曾出现「已平仓」+ 原始值 closed 两种
        # 写法同现；现在 closed 已映射成中文，但同词再显示一遍仍是噪音）。
        if hl_txt and hl_txt != _ST_TXT.get(st, st):
            # 第二行：执行器侧的账实状态（closing/mismatch/...）。宽限期额外给剩余时间，
            # 「超时前不留空白」—— 剩 0 分钟也要写出来（那正是「马上要升级成异常」）。
            extra = ""
            if hl_st == "closing":
                left = _opt_num(sg.get("grace_left_s"))
                if left is not None:
                    mins = max(0, int(math.ceil(max(0.0, left) / 60.0)))
                    extra = f" · 剩 {mins} 分钟"
            hl_note = (f"<div class='muted'>{_esc(hl_txt)}"
                       f"{_esc(extra)}</div>")
        # 单粒度资金费小字（feat/funds-ledger）：HL 持仓/已平仓行在状态下方多一行
        # 「本单资金费合计 ±X U」（正=收入，与面板全局口径一致）。非 HL 行 / 没有资金费
        # 记录的仓不带 funding_sum 键 → 不加行，表格结构不变。
        if sg.get("funding_sum") is not None:
            fund_txt, _cls = _sign(sg.get("funding_sum"))
            hl_note += f"<div class='muted'>本单资金费合计 {_esc(fund_txt)}</div>"
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


def _pct_txt(v: Any, dash: str = "—") -> str:
    """阈值 → 人话百分比（0.25 → 25%）。面板上只讲「25%」，不暴露内部小数。"""
    f = _opt_num(v)
    return dash if f is None else f"{f * 100:g}%"


def _span_links(cur: str, k: str = "", refresh: int = 0) -> str:
    """盈亏曲线时间窗切换链接（P1 功能二，零 JS 纯 <a>）。

    当前项标 ✅ 并用高亮 btn；k 与 refresh 原样带回 —— PR #54 的教训：深链路由
    丢 k 会 401，这里所有链接都必须带全参数。链接统一指向 /admin?tab=overview：
    面板已合并为单一入口，在 /hl 旧页点上也会回到面板，不再各自为政。
    """
    cur = normalize_span(cur)
    q = quote(str(k or ""), safe="")
    parts: List[str] = []
    for val, label in SPAN_CHOICES:
        href = f"/admin?tab=overview&span={val}"
        if refresh:
            href += f"&refresh={int(refresh)}"
        if q:
            href += f"&k={q}"
        if val == cur:
            parts.append(f"<a class='btn' href='{href}'>✅ {label}</a>")
        else:
            parts.append(f"<a class='btn gray' href='{href}'>{label}</a>")
    return " ".join(parts)


def _perf_line_html(p: dict) -> str:
    """「上线以来」一行（2026-10-29 用户要求：原策略绩效整卡精简成这一行，并入盈亏曲线）。

    纯渲染：数字全部来自 payload.perf（_perf_stats 只读聚合）+ account.total_realized
    （U 对照，全页唯一出处仍是这行）。无已平仓记录 → 只显「暂无」+ U 对照。
    """
    perf = _as_dict(p.get("perf"))
    n = int(_num(perf.get("n")))
    avg_r = _opt_num(perf.get("avg_r"))
    acc = _as_dict(p.get("account"))
    u_txt, u_cls = _sign(acc.get("total_realized"))
    u_seg = (f" · 累计已实现 <b class='{u_cls}'>{u_txt}</b>"
             if u_txt != "—" else "")
    if n <= 0 or avg_r is None:
        # N=0 不除零、不编数；U 对照仍要出现（「累计已实现」全页唯一出处在这行）。
        return f"<div class=\"legend\">上线以来：暂无已平仓记录{u_seg}</div>"
    total_r = _num(perf.get("total_r"))
    wins = int(_num(perf.get("wins")))
    win_rate = _num(perf.get("win_rate"))
    return ("<div class=\"legend\">上线以来：已平仓 <b>"
            f"{n}</b> 笔（多 {int(_num(perf.get('long')))} · 空 {int(_num(perf.get('short')))}） · "
            f"均R <b class=\"{'pos' if avg_r >= 0 else 'neg'}\">{avg_r:+.2f}</b> · "
            f"胜率 {win_rate * 100:.0f}%（赢 {wins} 笔） · "
            f"累计 R <b class=\"{'pos' if total_r >= 0 else 'neg'}\">{total_r:+.1f}</b>"
            f"{u_seg}</div>")


def _dd_line_html(meta: Dict[str, Any]) -> str:
    """「策略回撤 …」那一行（§7）：可见文字，不靠 title 悬停。

    必须写清两个「回撤」的区别 —— 同一区块里「最大回撤 19.54U」是 **U 金额**口径
    （pnl 序列峰谷），本行是 **百分比**口径（TWR 峰谷、已剔除出入金）；不写清用户会
    以为两个数字在打架。
    """
    st = str(meta.get("dd_status") or "unavailable")
    txt = str(meta.get("dd_status_text") or _DD_ST_TXT.get(st, "暂不可用"))
    note = str(meta.get("dd_note") or "")
    # 不可用/已关闭都要把**原因**写在括号里：只写「暂不可用」等于让人去猜是流水坏了
    # 还是自己把开关关了。
    if st in ("unavailable", "off") and note:
        txt = f"{txt}（{note}）"
    pct = _opt_num(meta.get("dd_pct"))
    pct_txt = "—" if pct is None else f"{pct * 100:.2f}%"
    # 【低3】over_kill 用 warn 色（不是 neg/红）：它是「需要注意」而不是「已停手」——
    # 红色会和 killed 抢视觉优先级，用户分不清哪个才真的要停手。
    cls = ("neg" if st == "killed"
           else "warn" if st == "over_kill"
           else ("muted" if st != "warn" else ""))
    span = f"<span class='{cls}'>{_esc(txt)}</span>" if cls else _esc(txt)
    # 「剔除出入金，峰值以来；按流水同步节奏更新」括注已按用户要求删除（2026-10-29）。
    wait = str(meta.get("dd_wait_note") or "").strip()
    tail = f" · <span class='neg'>{_esc(wait)}</span>" if wait else ""
    return (f"<div class=\"legend\">策略回撤 {pct_txt} · "
            f"告警线 {_pct_txt(meta.get('dd_warn_pct'))} · "
            f"熔断线 {_pct_txt(meta.get('dd_kill_pct'))} · 当前状态：{span}{tail}</div>")



def _render_body(p: dict) -> str:
    mode = str(p.get("mode") or "off")
    net = _as_dict(p.get("network"))
    acc = _as_dict(p.get("account"))
    system = _as_dict(p.get("system"))
    positions = _as_list(p.get("positions"))
    orders_open = _as_list(p.get("orders_open"))
    curve = _as_dict(p.get("curve"))
    curve_meta = _as_dict(curve.get("meta"))
    kill = bool(p.get("kill"))
    kill_reason = p.get("kill_reason") or ""
    reachable = bool(net.get("reachable"))
    latency = _opt_num(net.get("latency_ms"))
    error = str(net.get("error") or "")
    mismatch = bool(system.get("mismatch"))
    pnl_all = _curve_points(curve.get("pnl"))
    twr_all = _curve_points(curve.get("twr"))
    # 时间窗（P1）：GET span ∈ 7/30/all，缺参/非法值回落 7（normalize_span 白名单）。
    span = normalize_span(p.get("curve_span"))
    shown_pnl, shown_twr, curve_span, _actual_days = _curve_window(pnl_all, twr_all, span)

    o: List[str] = []
    o.append(H1_BLOCK)

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
    # 卡单徽章（P3 R2）：只在 >0 时出现，正常态状态条不出现「卡单」字样。
    o.append(_stuck_badge_html(system))
    lat_txt = f"{latency:.0f} ms" if latency is not None else "—"
    # 这是「服务器 → Hyperliquid」的耗时（容器每次拉账户快照的开销），不是你到面板的网络延迟。
    # 快照模式（阶段 B）下本次渲染没访交易所，数字来自调度器上一轮的实测，标题必须说清。
    if bool(net.get("latency_from_snapshot")):
        lat_title = "调度器上一轮读 Hyperliquid 的耗时（本次渲染没有访问交易所）；与你的浏览器到本站的延迟无关"
    else:
        lat_title = ("oracle1 上的容器请求 Hyperliquid /info 的耗时（含失败重试）；"
                     "与你的浏览器到本站的延迟无关")
    o.append(f"<span class='badge' title='{_esc(lat_title)}'>"
             f"<span class='{('pos' if latency is not None else 'muted')}'>&#9679;</span>"
             f" HL 延迟 <b>{lat_txt}</b></span>")
    # API 有效期徽章（P3 R3）：唯一常驻出处（原「API 有效期」卡降级为总览末尾的 details）。
    o.append(_agent_badge_html(p.get("agent")))
    o.append(f"<span class='badge'><span class='muted'>&#9679;</span> 更新 "
             f"<b>{_fmt_dt(p.get('ts'))}</b></span>")
    o.append("</div>")

    if not reachable:
        o.append(f"<div class='banner'>&#9888;&#65039; 未配置 / 读取失败：{_esc(error or '未知原因')}<br>"
                 f"<span class='muted'>交易所侧数据暂不可用；信号 / 下单 / 成交记录见「记录」页。</span></div>")

    # ---------------- 仓位 ----------------
    # 窄屏（≤640px）只留 8 个关键列（.hidem 列 display:none，不横向滚动）；被隐藏列
    # 的数值在每仓下方 tr.subm 小字行**原样补足**（宽屏 CSS 隐藏小字行、窄屏隐藏列，
    # 同一时刻同一事实只显示一份 —— 纯 CSS 显隐，零 JS）。表格仍是单一实现，
    # 不复制第二套「手机版表」。
    o.append("<h2>仓位</h2>")
    o.append("<div class='tablewrap'><table><thead><tr>"
             "<th>币种</th><th>方向</th><th class='num hidem'>数量</th><th class='num'>入场</th>"
             "<th class='num'>标记</th><th class='num'>浮盈浮亏</th><th class='num'>R</th>"
             "<th class='num'>止损</th><th class='num'>止损距离</th><th class='num hidem'>杠杆</th>"
             "<th class='num hidem'>保证金</th><th class='num hidem'>强平价</th>"
             "<th class='num hidem'>本单资金费</th>"
             "</tr></thead><tbody>")
    for pos in (_as_dict(x) for x in positions):
        upnl_txt, upnl_cls = _sign(pos.get("unrealized_pnl"))
        side_txt = "多" if _num(pos.get("side"), 1) >= 0 else "空"
        dist = _opt_num(pos.get("stop_dist_pct"))
        r_txt, r_cls = _sign(pos.get("r_unrealized"), "R")
        # 本单资金费（正=收入）：交易所 cumFunding.sinceOpen 已在 status_payload
        # 换算成收入视角；交易所没给（老快照）就如实显示「—」，不猜 0。
        fund_txt, fund_cls = _sign(pos.get("funding_income"))
        # ROE%（对齐 HL 网页习惯）＝ 浮盈 ÷ 本仓保证金。保证金 0/取不到时只显 U
        # 不显 % —— 除数都没有，百分比是编出来的。
        margin_v = _opt_num(pos.get("margin_used"))
        upnl_v = _opt_num(pos.get("unrealized_pnl"))
        roe_txt = (f" · {upnl_v / margin_v * 100.0:+.2f}%"
                   if margin_v and upnl_v is not None else "")
        if pos.get("has_stop"):
            stop_cell = f"<span class='pos'>&#10004;&#65039; {_fmt(pos.get('stop_px'))}</span>"
        elif pos.get("stop_unknown"):
            stop_cell = "<span class='muted'>? 无法确认</span>"
        else:
            stop_cell = "<span class='neg'>&#10060; 无</span>"
        o.append("<tr>"
                 + _td(f"<b>{_esc(pos.get('coin') or '—')}</b>")
                 + _td(f"<b>{side_txt}</b>")
                 + _td(_fmt_sz(pos.get("size")), "num hidem")
                 + _td(_fmt(pos.get("entry_px")), "num")
                 + _td(_fmt(pos.get("mark_px")), "num")
                 + _td(upnl_txt + roe_txt, f"num {upnl_cls}".strip())
                 + _td(r_txt, f"num {r_cls}".strip())
                 + _td(stop_cell, "num")
                 + _td(f"{dist * 100:.2f}%" if dist is not None else "—", "num")
                 + _td(f"{_num(pos.get('leverage'), 1):.0f}x"
                       + (" <span class='muted'>逐</span>" if pos.get("isolated") else ""),
                       "num hidem")
                 + _td(_fmt(pos.get("margin_used")), "num hidem")
                 + _td(_fmt(pos.get("liq_px")) if _opt_num(pos.get("liq_px")) else "—",
                       "num hidem")
                 + _td(fund_txt, f"num hidem {fund_cls}".strip())
                 + "</tr>")
        # 小字行：与表格同一份 payload 数值、同一套格式化函数（同一事实一种写法），
        # 只是载体不同 —— 窄屏隐藏列时由它补足，宽屏整个 <tr> 被 CSS 藏掉。
        o.append("<tr class='subm'><td colspan='13'>"
                 f"数量 {_fmt_sz(pos.get('size'))} · "
                 f"杠杆 {_num(pos.get('leverage'), 1):.0f}x"
                 f"{' 逐' if pos.get('isolated') else ''} · "
                 f"保证金 {_fmt(pos.get('margin_used'))} U · "
                 f"强平 {_fmt(pos.get('liq_px')) if _opt_num(pos.get('liq_px')) else '—'} · "
                 f"本单资金费 {fund_txt}</td></tr>")
    if not positions:
        o.append(f"<tr><td colspan='13' class='muted'>"
                 f"{'交易所无持仓' if reachable else '交易所数据暂不可读'}"
                 f"（信号 / 下单 / 成交见「记录」页）</td></tr>")
    o.append("</tbody></table></div>")
    # 图例（可见文字，iOS 无悬停）：ROE 口径收口 + 窄屏小字行的存在说明。
    o.append('<div class="legend">浮盈 % = 浮盈 ÷ 保证金（ROE）· '
             '窄屏隐藏列的数值见各仓下方小字行</div>')

    # ---------------- 账户卡片（P2：分层 + 消重复；P3 R4/R5：折叠 + 主从）----------------
    # 分两层：顶层关键数（大字，手机一眼读）＋ 次级项收进 <details>「资金明细」
    #（默认收起，零 JS）。P3 R5：顶层删「累计已实现」—— 它已并入策略绩效卡
    #（U 对照行，全页唯一出处），顶层只剩 账户总值 · 可提余额。
    # 「浮盈浮亏」「资金费（持仓期内）」两行删除：同一数字在仓位表每仓各有一份
    # （浮盈还带 ROE%，见下），账户级聚合行等于同屏第二遍；空仓时浮盈本来就没有
    # 可看的，无信息损失。次级项只是搬位置，一项不少、数值口径不变。
    o.append("<div class='card'><div class='sec'>账户</div>")
    today_txt, today_cls = _sign(acc.get("today_realized"))
    fees = _num(acc.get("fees_total"))
    # 可提口径与旧版一致：统一账户且交易所 withdrawable<=0 时按 现货−占用 现算
    # （手续费说明已按用户要求删除，2026-10-29；口径细节保留在 title 悬停）。
    wd = _num(acc.get("withdrawable"))
    if acc.get("unified") and wd <= 0:
        avail = max(_num(acc.get("spot_usdc")) - _num(acc.get("spot_hold")), 0.0)
        wd_html = f"{_fmt(avail)} U"
        wd_title = "统一账户下的实际可提：现货余额扣除仓位/挂单占用的保证金"
    else:
        wd_html = f"{_fmt(wd)} U"
        wd_title = ""
    wd_t = f" title='{_esc(wd_title)}'" if wd_title else ""
    o.append("<div class='acct-top'>"
             f"<span>账户总值 <b>{_fmt(acc.get('equity'))} U</b></span>"
             f"<span{wd_t}>可提余额 <b>{wd_html}</b></span></div>")
    o.append("<details class='acct-details'><summary>资金明细</summary>")
    o.append("<div class='acct-sub'>")
    o.append(_asub("钱包地址", _addr_text(acc.get("address"))))
    if acc.get("unified") and not acc.get("spot_ok", True):
        # 现货状态读取失败 → equity_total 只剩浮盈，数字完全不可信，必须喊出来
        o.append("<span class='neg'>⚠️ 现货余额读取失败：总值/可提不可信（只剩浮盈），不是没钱</span>")
    if acc.get("unified"):
        # 统一账户：资金分布在「现货」和「合约」两侧 —— 用人话一行说完。
        # 合约侧占用的就是已用保证金（同一个数），不再单列重复一列。
        o.append(_asub("资金分布（统一账户）",
                       f"现货 {_fmt(acc.get('spot_usdc'))} U · 合约占用 {_fmt(acc.get('margin_used'))} U",
                       title="统一账户下资金分在现货与合约两侧；合约侧占用 = 已用保证金"))
    if not acc.get("unified"):
        o.append(_asub("已用保证金", f"{_fmt(acc.get('margin_used'))} U"))
    o.append(_asub("持仓名义", f"{_fmt(acc.get('notional'))} U"))
    o.append(_asub("今日已实现", today_txt, today_cls,
                   title="交易所 closedPnl 口径，未扣手续费与资金费"))
    o.append(_asub("净手续费（付出）", f"{-fees:+,.2f} U", "neg" if fees > 0 else ""))
    o.append("</div>")
    o.append("</details>")
    # 注脚（可见文字，iOS Safari 无 title 悬停）：账户相关数字的口径收口。
    # 「已实现」两行的 title 保留作桌面增强，这里给出手机唯一能看到的口径。
    o.append('<div class="legend">已实现为交易所口径，未扣手续费与资金费 · '
             '可提 = 现货 − 仓位/挂单占用</div>')
    o.append("</div>")

    # ---------------- 盈亏曲线（已剔除出入金）+ 收益指数 ----------------
    # 页面只有这一张图（旧「权益曲线」已删掉，没有开关、没有视图切换）：
    # 主曲线 = 累计盈亏（U），虚线 = 收益指数 TWR（起点 100，右轴）。
    # 流水读不到时**不画任何曲线**，也不退回权益 —— 那条线正是要删掉的误导源。
    o.append("<h2>盈亏曲线（已剔除出入金）</h2>")
    o.append("<div class='chart-wrap'>")
    if curve_meta.get("flows_ok") is False:
        o.append("<div class='chart-head'><span class='muted'>暂无数据</span></div>")
        o.append(_pnl_svg([], []))
        note = str(curve_meta.get("flows_note")
                   or "出入金流水读取失败，盈亏曲线暂不可用（系统会自动重试）")
        o.append(f"<div class=\"legend\">{_esc(note)}</div>")
    elif len(shown_pnl) >= 2:
        # 摘要行四个数字**按所选窗口重算**（P1 功能二）：都在同一份曲线序列上切窗，
        # 不另起炉灶 —— span=all 时与 curve.meta 的全期数字逐字相同（首点 pnl=0、
        # 指数=100），两种口径不会打架。
        w_pnl = shown_pnl[-1][1] - shown_pnl[0][1]
        # 峰谷回撤复用 flows 的同一份实现（同一事实一种写法，不复制第二份逻辑）。
        w_dd = hl_flows._max_drawdown([v for _, v in shown_pnl])
        dep_span = _as_dict(curve_meta.get("net_deposit_span"))
        w_dep = (_opt_num(dep_span.get(span)) if span in ("7", "30") else None)
        if w_dep is None:
            w_dep = _opt_num(curve_meta.get("net_deposit")) or 0.0
        if len(shown_twr) >= 2 and shown_twr[0][1] > 0:
            # 区间链式收益：指数末点 ÷ 首点 − 1（窗口内任意一点都是链式乘积，直接相除）。
            w_twr = (shown_twr[-1][1] / shown_twr[0][1] - 1.0) * 100.0
            twr_label = "收益指数（区间）"
        else:
            w_twr = _opt_num(curve_meta.get("twr_total_pct")) or 0.0
            twr_label = "收益指数"
        # 小字一行（iOS 没有悬停，口径必须落在可见文字上）：四个数字一次说完。
        o.append(f"<div class='chart-head'><span>{curve_span}（{len(shown_pnl)} 个快照）</span>"
                 f"<span>累计盈亏 <b class='{'pos' if w_pnl >= 0 else 'neg'}'>"
                 f"{w_pnl:+,.2f} U</b></span>"
                 f"<span>{_span_links(span, str(p.get('admin_k') or ''), int(_num(p.get('refresh'))))}</span></div>")
        o.append(f"<div class=\"legend\">{curve_span}口径：累计盈亏 {w_pnl:+,.2f}U · "
                 f"净入金 {w_dep:,.2f}U · 最大回撤 {w_dd:,.2f}U · "
                 f"{twr_label} {w_twr:+.2f}%</div>")
        o.append(_pnl_svg(shown_pnl, shown_twr))
        tail = f" · {_SNAPSHOT_RETENTION_NOTE}" if span == "all" else ""
        o.append('<div class="legend">实线=累计盈亏（U，左轴）· '
                 f'虚线=收益指数（起点 100，右轴）{tail}</div>')
    else:
        o.append("<div class='chart-head'><span class='muted'>暂无数据（等执行器落快照）</span></div>")
        o.append(_pnl_svg([], []))
    # 策略回撤（DD Guard）：**任何分支都要有这一行** —— 曲线画不出来（流水坏）时更需要
    # 告诉人「回撤判据现在可不可信」，而「暂不可用」本身就是一句结论。
    o.append(_dd_line_html(curve_meta))
    # 「上线以来」一行（原策略绩效整卡，2026-10-29 用户要求精简并入本区块）。
    try:
        perf_line = _perf_line_html(p)
        if perf_line:
            o.append(perf_line)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 上线以来统计行渲染失败：%s", e)
    o.append("</div>")

    # ---------------- 三标的动态（含 /why 原因行与闸门） ----------------
    # 2026-10-29 用户要求与「仓位」表对换：仓位移到最顶，本块跟在盈亏曲线之后。
    # 距离行 + 原因行 + 闸门异常行都在这一块里 —— 原「为什么没开仓」表格已合并进来。
    # 数据由调用方注入 payload（why 由 admin_ui 现算，symbol_state 来自 DB 缓存），
    # 本函数不做 I/O，逐块兜底：缺哪份数据就只少那几行，绝不弄挂总览页。
    try:
        o.append(_symbol_dynamics(p))
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 三标的动态区块渲染失败：%s", e)


    # ---------------- 挂单（按需渲染，P3 R1）----------------
    # 正常态（每仓恰好一条 reduce-only 触发单、无孤儿、无其它挂单）整块不渲染：
    # 止损已由仓位表「止损 ✔️」列唯一表达（顺带消掉止损价在挂单表的第二次出现）。
    # 异常态 / 判不清（fail-open）才照常渲染完整挂单表。子页 /admin/list/orders 不变。
    if not _orders_normal(positions, orders_open):
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

    # ---------------- 执行器（P3 R2 瘦身）----------------
    # 原「系统」卡收敛：对账/熔断两行删除（顶部状态条徽章是唯一出处）、
    # 卡单移状态条徽章（>0 才出现）—— 卡内不再重复任何一处状态条已有的事实。
    # 只留「执行器最后事件」（判活读数）+ 交易所地址/环境一行 muted。
    # 注：「下单记录」「最近成交」两张表已从总览移除（2026-10-18 用户要求：与「记录」页
    # 的 hl_orders / hl_fills 完全重复 → 去 /admin?tab=records 看）。
    # 更早的「执行器事件」表同理（2026-10-15 移除）。「信号」表也在本次迁到记录页，
    # 渲染函数保留为 _signals_html（记录页复用它，两边长得一样）。
    base_txt = str(net.get("base_url") or "—").replace("https://", "").replace("http://", "")
    o.append("<h2>执行器</h2>")
    o.append("<div class='card'><div class='grid'>")
    o.append(_kv("执行器最后事件", _fmt_dt(system.get("executor_last_event_at"))))
    o.append("</div>")
    o.append(f"<div class='muted'>交易所 {_esc(base_txt)} · "
             f"{'测试网' if net.get('testnet') else '主网'}</div>")
    # P2：原「数据源健康」「已处理到」两张表已整块删除（不是折叠）—— 每标的数据源
    # 状态由「三标的动态」卡在该标的名下的小字行承载（见 _feed_line，2026-10-29 起
    # 正常态不渲染、仅源失败时出现）。
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
        body = (f"{H1_BLOCK}"
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
        f"{meta}<title>{PRODUCT_NAME} · 总览</title>{THEME_CSS}{DASH_CSS}</head>"
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
    def hl_page(request: Request, refresh: int = 0, k: str = "", span: str = "7"):
        if _admin_required(deps, k, request):
            raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
        payload = dict(status_payload(deps))
        payload["refresh"] = refresh if refresh in (10, 30, 60) else 0
        # 盈亏曲线时间窗（P1）：白名单外（含缺参）回落 7，见 normalize_span。
        payload["curve_span"] = normalize_span(span)
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
            # 盈亏曲线（已剔除出入金）+ TWR 指数：与面板同一份 payload，
            # 老的原始权益曲线已删除（不是开关），调用方要画图只读这个。
            "curve": p.get("curve") or {},
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
