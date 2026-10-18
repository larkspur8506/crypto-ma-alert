"""管理面板（合并版）：单一入口 ``/admin?k=<ADMIN_TOKEN>&tab=<tab>``。

四个 Tab（链接切换，服务端渲染，**零 JS / 零 CDN**，手机弱网可开）：

- ``overview`` 总览 —— 直接复用 :func:`app.hl.views._render_body`（不重写仓位/权益曲线区块）
- ``params``   参数 —— 策略 / 执行与推送 / 资金 三组，显示来源（DB·env·默认）与影响预览
- ``switch``   开关 —— 执行模式三选一（切实盘二次确认 + 校验凭据）、推送开关、kill switch
- ``records``  记录 —— hl_orders / hl_fills / alert_logs / hl_events 四张表

兼容（见 docs/ADMIN_MERGE_PLAN.md 第 2 节「保留」）：
- ``/hl``、``/admin/rules``、``/admin/logs`` 三个旧 GET → 302 到新位置（书签/已发消息里的链接不断）
- ``/admin/rules`` 的 POST 仍在 ``app.main``（继续写 DB）
- ``/api/hl/*`` 四个 JSON 接口、``/c/{pid}`` 确认页完全不动

面板只读交易逻辑：不改 ``app/strategy.py``、``app/hl/executor.py``、``app/hl/exchange.py``。
所有依赖走 :class:`AdminDeps` 注入，便于单测用假实现。
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import engine, sizing
from app.hl.views import (DASH_CSS, DEFAULT_ROWS, ROW_CHOICES, ROWS_COOKIE, _kv,
                        _render_body, normalize_rows, status_payload)
from app.ui_theme import (THEME_COOKIE, DEFAULT_THEME, THEMES, normalize_theme,
                          theme_attr, theme_switch_html)

logger = logging.getLogger(__name__)

# 提醒日志（alert_logs.kind）→ 中文。以前直接显示英文原文（heartbeat_local /
# exec_alert_fills），和「执行器事件」那张表的中文列不一致、看不懂（用户 2026-10-15 反馈）。
_ALERT_KIND_TXT = {
    "entry": "开仓提醒",
    "exit": "离场提醒",
    "data_error": "数据源异常",
    "heartbeat": "心跳（推送）",
    "heartbeat_local": "心跳（静默）",
    "weekly": "周报（推送）",
    "weekly_local": "周报（静默）",
    "exec_alert": "执行器告警",
    "exec_alert_fills": "执行器成交回报",
    "tick_error": "处理异常告警",
    "agent_expiry_near": "API 有效期提醒",
    "agent_expiry_urgent": "API 有效期紧急",
    "agent_expiry_expired": "API 已失效",
    "agent_expiry_cleanup_ok": "API 到期清仓成功",
    "agent_expiry_cleanup_fail": "API 到期清仓失败",
    "agent_expiry_cleanup_off": "API 到期未自动清仓",
    "agent_renew": "API 已更新",
    # 旧 kind（历史行）：保留映射，历史记录照样显示中文
    "agent_expiry_alert": "API 有效期提醒（旧）",
    "agent_expiry_cleanup": "API 到期自动清仓（旧）",
}

# 回填标记：2026-10-15 之前执行器推送不落 alert_logs，历史推送由 hl_events 回填，
# message 一律以此前缀开头（面板据此提示口径，也便于一键回滚）。
BACKFILL_PREFIX = "[回填] "


def rows_switch_html(tab: str, current: int, k: str = "") -> str:
    """页脚「每页行数」切换行（零 JS，纯链接；与主题切换同一套做法）。"""
    cur = normalize_rows(current)
    q = quote(str(k or ""), safe="")
    links = []
    for n in ROW_CHOICES:
        cls = "btn" if n == cur else "btn gray"
        links.append(f"<a class='{cls}' href='/admin/rows?n={n}&tab={_esc(tab)}&k={q}'>{n}</a>")
    return ("<div class='foot'><span class='muted'>每页行数：</span>" + " ".join(links)
            + "<span class='muted'>&nbsp;（历史类长表；信号与曲线不受影响）</span></div>")


TABS: Tuple[Tuple[str, str], ...] = (
    ("overview", "总览"),
    ("params", "参数"),
    ("switch", "开关"),
    ("records", "记录"),
)
_TAB_KEYS = tuple(k for k, _ in TABS)
_MODE_TXT = {"off": "只通知", "dry": "影子", "live": "实盘"}
_MODE_CLS = {"off": "muted", "dry": "warn", "live": "neg"}
_PURPOSE_TXT = {"entry": "开仓", "stop": "止损", "exit": "平仓"}
_ORDER_ST_TXT = {"intent": "待发", "planned": "计划", "sent": "已发", "resting": "挂单",
                 "filled": "成交", "cancelled": "撤单", "failed": "失败", "skipped": "跳过"}
_EV_TXT = {"decision": "决策", "order": "下单", "reconcile": "对账", "kill": "熔断",
           "error": "错误", "heartbeat": "心跳"}

# 推送开关：key -> (中文标签, 说明, 缺省值)
# report_push / fill_push 是新键（首次保存即落 DB）。
NOTIFY_KEYS: Tuple[Tuple[str, str, str, str], ...] = (
    ("heartbeat_push", "每日心跳推送", "体检报告；关掉后只落库不推送，看守脚本仍判活", "0"),
    ("report_push", "日报 / 周报推送", "周报周一发送；关掉后只落库（kind=weekly_local）", "1"),
    ("fill_push", "成交回报推送", "开 = 开仓/平仓成交都推一条；关 = 不推（止损被触发等异常提醒不受影响）", "1"),
)


# ---------------------------------------------------------------- 依赖注入
@dataclass
class AdminDeps:
    """管理面板依赖。全部走可替换函数，便于测试注入假实现。

    db            app.db.DB（或等价替身）
    config_fn     返回 app.config.Config（已 apply_db，口径 DB > env > 默认）
    hl_deps       app.hl.views.DashboardDeps（overview Tab 与状态条的数据源）
    mode_fn       当前执行模式 off / dry / live
    address_fn    HL 钱包地址（live 校验用，可为空串）
    private_key_fn HL 私钥（只判断非空，**绝不**在这里读取/回显明文）
    token_fn      返回 ADMIN_TOKEN
    mids_fn       可选：返回 {币种: 现价}（影响预览用，失败时回落 hl_deps.info.all_mids）
    price_fallback 价格全拿不到时的兜底值（0 = 预览降级成「无法取价」，不报错）
    """

    db: Any
    config_fn: Callable[[], Any]
    hl_deps: Any
    mode_fn: Callable[[], str]
    token_fn: Callable[[], str]
    address_fn: Callable[[], str] = lambda: ""
    private_key_fn: Callable[[], str] = lambda: ""
    set_config: Optional[Callable[[str, Any], None]] = None
    get_config: Optional[Callable[[], Dict[str, str]]] = None
    hl_set_kill: Optional[Callable[[bool, str], None]] = None
    hl_log_event: Optional[Callable[..., None]] = None
    mids_fn: Optional[Callable[[], Dict[str, float]]] = None
    price_fallback: float = 0.0
    # ---- API 钱包到期卡片 + 「更新 API」表单（app.hl.agent） ----
    # agent_state_fn 只读 DB 里的 hl_agent_state 缓存（面板渲染**零网络调用**）
    agent_state_fn: Optional[Callable[[], Dict[str, Any]]] = None
    # renew_fn(新私钥) -> (ok, 人话文案)；校验/写库/推送都在 app.hl.agent.renew_key 里
    renew_fn: Optional[Callable[[str], Any]] = None
    cleanup_enabled_fn: Optional[Callable[[], bool]] = None

    def __post_init__(self) -> None:
        if self.set_config is None:
            self.set_config = getattr(self.db, "set_config", None)
        if self.get_config is None:
            self.get_config = getattr(self.db, "get_config", None)
        if self.hl_set_kill is None:
            self.hl_set_kill = getattr(self.db, "hl_set_kill", None)
        if self.hl_log_event is None:
            self.hl_log_event = getattr(self.db, "hl_log_event", None)

    # ---- 便捷封装（缺方法 / 抛异常都不让面板炸掉） ----
    def put(self, key: str, value: Any) -> Optional[str]:
        """写配置；失败返回错误文案（调用方带回页面显示），成功返回 None。"""
        if self.set_config is None:
            return "DB 不可用（set_config 缺失）"
        try:
            self.set_config(key, value)
            return None
        except Exception as e:                                    # noqa: BLE001
            logger.error("[admin] 写配置 %s 失败：%s", key, e)
            return f"{key}: {type(e).__name__}: {e}"

    def get_all(self) -> Dict[str, Any]:
        if self.get_config is None:
            return {}
        try:
            return dict(self.get_config() or {})
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[admin] 读配置失败：%s", e)
            return {}

    def kill_on(self) -> bool:
        try:
            return bool(self.db.hl_kill())
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[admin] hl_kill 失败：%s", e)
            return False

    def log_event(self, kind: str, message: str) -> None:
        if self.hl_log_event is None:
            return
        try:
            self.hl_log_event(kind, message)
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[admin] 记事件失败：%s", e)


# ---------------------------------------------------------------- 小工具
def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _opt_num(v: Any) -> Optional[float]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _fmt(v: Any, dp: int = 2) -> str:
    f = _opt_num(v)
    return "—" if f is None else f"{f:,.{dp}f}"


def _b(v: Any) -> bool:
    """DB 里 set_config 走 json.dumps，字符串值会带引号，先剥掉再判。"""
    return str(v if v is not None else "").strip().strip('"\'').lower() in (
        "1", "true", "yes", "on")


def _ts(v: Any) -> str:
    """时间 → 北京时间（口径同 app.engine：秒/毫秒都认，>1e12 视为毫秒）。"""
    f = _opt_num(v)
    if f is None:
        return "—"
    s = f / 1000.0 if f > 1e12 else f
    try:
        return dt.datetime.fromtimestamp(s, engine.DISPLAY_TZ).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"


def _rowd(r: Any) -> Dict[str, Any]:
    try:
        return dict(r)
    except Exception:                                             # noqa: BLE001
        return {}


def _rows(deps: "AdminDeps", name: str, limit: int = 50) -> List[Dict[str, Any]]:
    """取一张表的最近 N 行；方法缺失 / 抛异常都返回空表（面板必须撑住坏数据）。"""
    try:
        fn = getattr(deps.db, name, None)
    except Exception:                                             # noqa: BLE001
        return []
    if not callable(fn):
        return []
    try:
        out = fn(limit) or []
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] %s 读取失败：%s", name, e)
        # 读取失败要能被看出来（以前显示成「暂无记录」，运维分不清是空还是坏）
        return [{"_error": f"读取失败：{type(e).__name__}"}]
    return [_rowd(r) for r in out]


def _badge(dot_cls: str, label: str, value: str, title: str = "") -> str:
    t = f" title='{_esc(title)}'" if title else ""
    return (f"<span class='badge'{t}><span class='{dot_cls}'>&#9679;</span> "
            f"{label} <b>{value}</b></span>")


def _msg_cell(msg: str) -> str:
    """消息单元格：≤160 字符保持 title 悬停（原样，避免每行都可点的噪音）；
    超长改用原生 <details> 折叠（零 JS）——iOS Safari 无 title 悬停，超出部分原先永久不可读。"""
    if len(msg) <= 160:
        return f"<span title='{_esc(msg)}'>{_esc(msg)}</span>"
    shown = msg[:160] + "…"
    return (f"<details class=\"msgdetails\"><summary>{_esc(shown)}</summary>"
            f"<div class=\"fullmsg\">{_esc(msg)}</div></details>")


def _table(thead: str, rows_html: str, empty: str, empty_cols: int) -> str:
    if rows_html:
        return f"<div class='tablewrap'><table><thead><tr>{thead}</tr></thead>" \
               f"<tbody>{rows_html}</tbody></table></div>"
    return (f"<div class='tablewrap'><table><thead><tr>{thead}</tr></thead>"
            f"<tbody><tr><td colspan='{empty_cols}' class='muted'>{_esc(empty)}</td></tr>"
            f"</tbody></table></div>")


def _call(fn: Optional[Callable], default: Any = None) -> Any:
    if fn is None:
        return default
    try:
        r = fn()
        return default if r is None else r
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] 依赖调用失败：%s", e)
        return default


# ---------------------------------------------------------------- 数据组装
def get_payload(deps: AdminDeps) -> dict:
    """overview Tab 与顶部状态条共用一份 payload（status_payload 本身已保证不抛异常）。"""
    try:
        p = status_payload(deps.hl_deps)
        return p if isinstance(p, dict) else {}
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] status_payload 失败：%s", e)
        return {}


def btc_price(deps: AdminDeps) -> float:
    """当前 BTC 现价（用于影响预览）。取不到就返回兜底值，**绝不抛异常**。"""
    for src in (deps.mids_fn, _info_mids(deps.hl_deps)):
        if src is None:
            continue
        try:
            v = src().get("BTC")
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[admin] allMids 失败：%s", e)
            continue
        f = _opt_num(v)
        if f and f > 0:
            return f
    return float(deps.price_fallback or 0.0)


def _info_mids(hl_deps: Any) -> Optional[Callable[[], Dict[str, Any]]]:
    info = getattr(hl_deps, "info", None)
    fn = getattr(info, "all_mids", None) if info is not None else None
    return fn if callable(fn) else None


def effective_base(deps: AdminDeps, cfg: Any, payload: dict) -> Tuple[float, float, str]:
    """返回 (实时权益, 当前生效基数, 口径说明)。口径与执行器 effective_equity 一致。"""
    acc = payload.get("account") or {}
    live = _num(acc.get("equity"))
    eq = _num(getattr(cfg, "equity", 0.0))
    mode = str(getattr(cfg, "equity_mode", "follow") or "follow").strip().lower()
    if mode == "fixed" or live <= 0:
        return live, eq, ("固定基数" if mode == "fixed" else "固定基数（读不到实时权益）")
    eff = math.floor(live / 10.0) * 10.0
    mn = _num(getattr(cfg, "equity_min", 0.0))
    if mn and eff < mn:
        eff = mn
    return live, eff, "跟随实时权益"


def impact(cfg: Any, price: float, base: Optional[float] = None,
           *, equity: Optional[float] = None, risk_pct: Optional[float] = None,
           leverage: Optional[float] = None, stop_dist: Optional[float] = None
           ) -> Optional[dict]:
    """按当前 BTC 价估算：单笔风险 U / 名义 U / 保证金 U / 强平价。

    输入不完整（价格为 0、权益为 0 等）时返回 None，调用方显示「—」而不是报错。
    """
    eq = equity if equity is not None else (base if base is not None else _num(getattr(cfg, "equity", 0.0)))
    rp = risk_pct if risk_pct is not None else _num(getattr(cfg, "risk_pct", 0.02))
    lev = leverage if leverage is not None else _num(getattr(cfg, "leverage", 5.0))
    sd = stop_dist if stop_dist is not None else _num(getattr(cfg, "stop_min", 0.0))
    if price <= 0 or eq <= 0 or rp <= 0 or sd <= 0 or lev <= 0:
        return None
    risk_u = eq * rp
    notional = risk_u / sd
    return {"price": price, "equity": eq, "risk_pct": rp, "leverage": lev,
            "stop_dist": sd, "risk_u": risk_u, "notional": notional,
            "margin": notional / lev, "liq": sizing.liq_price(price, lev, 1),
            # 做空参考（同参数镜像）：预览页给出两个方向的强平价，防误读
            "liq_short": sizing.liq_price(price, lev, -1)}


def _impact_line(d: Optional[dict]) -> str:
    if not d:
        return "影响预览：无法取价（等只读接口恢复后再算）"
    return (f"按 BTC 现价 {d['price']:,.0f}：单笔风险 {d['risk_u']:.2f}U · "
            f"名义 {d['notional']:,.0f}U · 保证金 {d['margin']:,.0f}U · "
            f"强平 多{d['liq']:,.0f}/空{d['liq_short']:,.0f}")


# ---------------------------------------------------------------- 参数 Tab
# (db_key, 分组, 标签, env_key, 默认值, 类型)
PARAM_FIELDS: Tuple[Tuple[str, str, str, str, Any, str], ...] = (
    ("stop_min", "策略", "止损距离下限", "STOP_MIN", 0.02, "float"),
    ("stop_max", "策略", "止损距离上限", "STOP_MAX", 0.08, "float"),
    ("enable_long", "策略", "允许做多", "ENABLE_LONG", True, "bool"),
    ("enable_shorts", "策略", "允许做空", "ENABLE_SHORTS", True, "bool"),
    ("short_regime_ma", "策略", "做空趋势过滤均线（0=关闭，不建议）",
     "SHORT_REGIME_MA", 200, "int"),
    ("risk_pct", "执行与推送", "单笔风险", "RISK_PCT", 0.02, "float"),
    ("leverage", "执行与推送", "杠杆（逐仓）", "LEVERAGE", 5.0, "float"),
    ("heartbeat_push", "执行与推送", "每日心跳推送", "HEARTBEAT_PUSH", False, "bool"),
    ("equity", "资金", "账户权益基数 U（固定模式用）", "ACCOUNT_EQUITY", 1000.0, "float"),
    ("equity_mode", "资金", "资金口径", "HL_EQUITY_MODE", "follow", "str"),
    ("equity_min", "资金", "资金下限 U（跟随模式用）", "", 20.0, "float"),
    ("agent_expiry_cleanup", "API 有效期", "到期前自动清仓（≤3 天）", "AGENT_EXPIRY_CLEANUP", True, "bool"),
    ("hl_agent_valid_until_ms", "API 有效期", "手工兜底的有效期（毫秒时间戳，可留空）",
     "HL_AGENT_VALID_UNTIL_MS", "", "text"),
)
_GROUPS: Tuple[Tuple[str, str], ...] = (
    ("策略", "决定什么信号会被发出来"),
    ("执行与推送", "决定下多大、占多少保证金"),
    ("资金", "下单基数怎么算"),
    ("API 有效期", "API 钱包到期前要不要自动清仓；接口查不到有效期时的手工兜底"),
)


def _source(db_raw: Dict[str, Any], key: str, env_key: str) -> str:
    v = db_raw.get(key)
    if v not in (None, ""):
        return "DB"
    if env_key and (os.environ.get(env_key) or "").strip():
        return "env"
    return "默认"


def _cur_val(cfg: Any, key: str, default: Any, kind: str) -> str:
    raw = getattr(cfg, key, default)
    if kind == "bool":
        return "开" if bool(raw) else "关"
    if kind == "int":
        return f"{int(_num(raw, _num(default)))}"
    if kind == "str":
        return str(raw if raw is not None else default)
    if kind == "text":
        return str(raw if raw is not None else default)
    return f"{_num(raw, _num(default)):g}"


def _preview_hint(key: str, cfg: Any, base: float, live: float,
                  imp: Optional[dict], imp_max: Optional[dict]) -> str:
    """每项旁的一行小字：这个字段改动能带来什么变化（按当前值实算，不靠猜）。"""
    if key == "equity":
        return f"固定基数 {_num(getattr(cfg, 'equity', 0)):,.0f}U → 单笔风险 " \
               f"{_num(getattr(cfg, 'equity', 0)) * _num(getattr(cfg, 'risk_pct', 0)):,.2f}U"
    if key == "equity_mode":
        mode = str(getattr(cfg, "equity_mode", "follow") or "follow")
        if mode == "fixed":
            return "固定：按上面写的基数下单，不跟账户盈亏走"
        if live <= 0:
            return f"⚠️ 读不到实时权益（{live:,.2f}）→ 实际按固定基数 {base:,.0f}U 下单，不是跟随"
        return f"跟随：实时权益 {live:,.2f}U → 当前生效基数 {base:,.0f}U"
    if key == "equity_min":
        return f"跟随模式下基数不低于 {_num(getattr(cfg, 'equity_min', 0)):,.0f}U"
    if key == "risk_pct":
        return f"{_num(getattr(cfg, 'risk_pct', 0)) * 100:.2f}% × 基数 {base:,.0f}U = 单笔风险 " \
               f"{base * _num(getattr(cfg, 'risk_pct', 0)):,.2f}U"
    if key == "leverage":
        lev = _num(getattr(cfg, 'leverage', 5))
        if imp:
            return f"名义 {imp['notional']:,.0f}U ÷ {lev:g}x = 保证金 {imp['margin']:,.0f}U；" \
                   f"强平 ≈多 {imp['liq']:,.0f} / 空 {imp['liq_short']:,.0f}"
        return f"{lev:g}x 逐仓（取价失败，保证金/强平不算）"
    if key == "stop_min":
        return f"最紧止损 {_num(getattr(cfg, 'stop_min', 0)) * 100:.2f}% → 名义 " \
               f"{imp['notional']:,.0f}U（止损越紧仓位越大）" if imp else "止损越紧仓位越大"
    if key == "stop_max":
        return f"最宽止损 {_num(getattr(cfg, 'stop_max', 0)) * 100:.2f}% → 名义 " \
               f"{imp_max['notional']:,.0f}U" if imp_max else "止损越宽仓位越小"
    if key == "enable_long":
        return "开 = 会发做多信号" if getattr(cfg, "enable_long", True) else "关 = 不做多"
    if key == "enable_shorts":
        return "开 = 会发做空信号" if getattr(cfg, "enable_shorts", True) else "关 = 不做空"
    if key == "short_regime_ma":
        n = int(_num(getattr(cfg, "short_regime_ma", 0)))
        return "0 = 关闭过滤（裸做空，不建议）" if n <= 0 else f"用 MA{n} 过滤空头信号"
    if key == "heartbeat_push":
        return "开 = 每天推一条心跳；关 = 只落库不推送" if getattr(cfg, "heartbeat_push", False) \
            else "关 = 只落库不推送（看守脚本靠落库判断存活）"
    if key == "agent_expiry_cleanup":
        return ("开 = API 快到期（剩余 3 天以内）时自动撤单平仓，避免到期后想平也平不了"
                if getattr(cfg, "agent_expiry_cleanup", True)
                else "关 = 只提醒不清仓（到期后执行器无法平仓，需要自己盯）")
    if key == "hl_agent_valid_until_ms":
        return ("仅当 HL 授权接口查不到同名 Hermes 时用：填毫秒时间戳"
                "（如 1830000000000）。留空 = 不改动当前值（要清空点下面的「清除兜底」）")
    return ""


_TRUTHY = ("1", "true", "yes", "on", "y", "t")


def _flag(val: Any) -> int:
    """开关取值：'0'/'false'/'off'/空 = 关。

    不能只写 `1 if val else 0` —— 字符串 "0" 在 Python 里是真值，会把「关」写成「开」
    （fail-dangerous：脚本/表单发 0 反而打开推送）。
    """
    return 1 if str(val if val is not None else "").strip().lower() in _TRUTHY else 0


def _cleanup_on(val: Any) -> int:
    """「到期前自动清仓」的取值：**识别不了按「开」**（fail-open）。

    与推送开关相反：漏清仓的代价是「到期后仓位想平也平不了」，误清仓只是提前
    平掉一笔仓（还能再开）。所以只有明确的假值才判关。
    """
    if isinstance(val, bool):
        return 1 if val else 0
    raw = str(val if val is not None else "").strip().lower()
    return 0 if raw in ("0", "false", "no", "off") else 1


def _num_field(key: str, label: str, value: str, hint: str, src: str, step: str = "") -> str:
    st = f" step='{step}'" if step else ""
    return (f"<div class='field'><div class='lbl'>{_esc(label)}"
            f"<span class='src'>&nbsp;来源 {src} · 当前 {_esc(value)}</span></div>"
            f"<input type='text' inputmode='decimal' required name='{_esc(key)}' "
            f"value='{_esc(value)}'{st}>"
            f"<div class='hint'>{hint}</div></div>")


def _bool_field(key: str, label: str, on: bool, hint: str, src: str) -> str:
    """复选框 + **hidden 兜底**（H3）。

    裸 checkbox 在「未勾选」时**什么都不提交** —— 后端拿不到字段，任何
    「缺省=开」的逻辑都会让这个开关永远关不掉（原来就是这样：取消勾选、保存、
    刷新，还是开）。

    在 checkbox 之前放一个同名的 `<input type=hidden value='0'>`：
      - 未勾选 → 只提交 hidden 的 0 → 关
      - 勾选   → 先提交 0 再提交 1 → 后端取**最后一个**值 = 1 → 开
    （浏览器按文档顺序提交；FastAPI/Starlette 的 Form 取值也是「后出现的覆盖」，
    见 tests/test_agent_expiry.py 的同名多值用例。）
    """
    return (f"<div class='field'><div class='lbl'>{_esc(label)}"
            f"<span class='src'>&nbsp;来源 {src} · 当前 {'开' if on else '关'}</span></div>"
            f"<input type='hidden' name='{_esc(key)}' value='0'>"
            f"<label class='chk'><input type='checkbox' name='{_esc(key)}' value='1' "
            f"{'checked' if on else ''}></label><div class='hint'>{hint}</div></div>")


def render_params(deps: AdminDeps, cfg: Any, payload: dict, price: float) -> str:
    raw = deps.get_all()
    live, base, base_txt = effective_base(deps, cfg, payload)
    imp = impact(cfg, price, base)
    imp_max = impact(cfg, price, base, stop_dist=_num(getattr(cfg, "stop_max", 0)))

    o: List[str] = []
    o.append("<h2>参数</h2>")
    # 表单必须包住所有输入控件：控件在 form 外的话浏览器只提交 form 内的
    # 元素 → POST 空 body → 必 400（自面板合并起就坏，用户从没在面板改过参数）。
    o.append("<form method='post' action='/admin/params'>")
    o.append("<div class='card'><div class='sec'>改动影响预览</div>"
             f"<div class='hint big'>{_impact_line(imp)}</div>"
             f"<div class='hint'>下单基数：<b>{base:,.0f} U</b>（{_esc(base_txt)}；"
             f"实时权益 {live:,.2f} U）<br>保存后立即写 DB，下一轮 tick 生效，无需重启。"
             "下面是<b>当前值</b>的影响；改完保存后这一行按新值重算。</div></div>")

    for group, desc in _GROUPS:
        o.append(f"<div class='card'><div class='sec'>{_esc(group)}</div>"
                 f"<div class='hint'>{_esc(desc)}</div>")
        for key, grp, label, env_key, default, kind in PARAM_FIELDS:
            if grp != group:
                continue
            src = _source(raw, key, env_key)
            hint = _preview_hint(key, cfg, base, live, imp, imp_max)
            if kind == "bool":
                o.append(_bool_field(key, label, bool(getattr(cfg, key, default)), hint, src))
            elif kind == "text":
                # 自由文本（可留空）：手工兜底时间戳这类，不能 required
                cur = _cur_val(cfg, key, default, kind)
                o.append(f"<div class='field'><div class='lbl'>{_esc(label)}"
                         f"<span class='src'>&nbsp;来源 {src} · 当前 {_esc(cur or '空')}</span></div>"
                         f"<input type='text' inputmode='numeric' name='{_esc(key)}' "
                         f"value='{_esc(cur)}'>"
                         f"<div class='hint'>{hint}</div></div>")
            elif kind == "str":
                cur = str(getattr(cfg, key, default))
                opts = "".join(
                    f"<option value='{v}' {'selected' if v == cur else ''}>{t}</option>"
                    for v, t in (("follow", "跟随实时权益"), ("fixed", "固定基数")))
                o.append(f"<div class='field'><div class='lbl'>{_esc(label)}"
                         f"<span class='src'>&nbsp;来源 {src} · 当前 {_esc(cur)}</span></div>"
                         f"<select name='{_esc(key)}'>{opts}</select>"
                         f"<div class='hint'>{hint}</div></div>")
            else:
                o.append(_num_field(key, label, _cur_val(cfg, key, default, kind),
                                    hint, src, step="1" if kind == "int" else ""))
        o.append("</div>")

    o.append("<button type='submit'>保存全部参数</button></form>")
    # 手工兜底现在「留空 = 不改动」（M3），所以清空必须有独立按钮（不能嵌在上面的
    # form 里 —— HTML 不允许表单嵌套）。只在填了值时才显示，避免噪音。
    cur_until = str(_cur_val(cfg, "hl_agent_valid_until_ms", "", "text") or "").strip()
    if cur_until:
        o.append("<form method='post' action='/admin/hl/agent/fallback/clear' class='inline'>"
                 f"<input type='hidden' name='k' value='{_esc(_token_of(deps))}'>"
                 f"<span class='hint'>手工兜底有效期当前为 {_esc(cur_until)}（毫秒）。"
                 "留空保存 = 保持原值。</span>"
                 "<button class='gray' type='submit'>清除兜底</button></form>")
    return "".join(o)


def _parse_params(form: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """校验 + 转换。全部通过才返回可写值；有任何错就返回错误列表且**不写库**。"""
    errs: List[str] = []
    out: Dict[str, Any] = {}

    def take_float(key: str, name: str, lo: float, hi: Optional[float],
                   strict: bool = True) -> Optional[float]:
        """strict=True 要求 v > lo（权益/风险率必须为正）；False 允许 v == lo（资金下限可为 0）。"""
        s = str(form.get(key) or "").strip().replace(",", "")
        if not s:
            errs.append(f"{name} 为空")
            return None
        try:
            v = float(s)
        except ValueError:
            errs.append(f"{name} 不是数字")
            return None
        if not math.isfinite(v):
            errs.append(f"{name} 非法")
            return None
        if (v <= lo) if strict else (v < lo):
            errs.append(f"{name} 必须{'大于' if strict else '不小于'} {lo:g}")
            return None
        if hi is not None and v > hi:
            errs.append(f"{name} 不能大于 {hi:g}")
            return None
        out[key] = v
        return v

    # 校验范围与 Config._RANGES 单一真源对齐（审计三轮 M6）：以前面板接受 0~1.0，
    # Config 却只认 0.0001~0.5 —— 面板写 0.8 入库成功、apply_db 静默丢弃，
    # 页面显示「保存成功」实际没生效。
    from app.config import Config as _Cfg
    _R = _Cfg._RANGES
    eq = take_float("equity", "账户权益基数", *_R["equity"])
    rp = take_float("risk_pct", "单笔风险", *_R["risk_pct"])
    lev = take_float("leverage", "杠杆", _R["leverage"][0], _R["leverage"][1], strict=False)
    sm = take_float("stop_min", "止损下限", *_R["stop_min"])
    sx = take_float("stop_max", "止损上限", *_R["stop_max"])
    emin = take_float("equity_min", "资金下限", *_R["equity_min"], strict=False)

    # 做空均线：必须是整数
    sraw = str(form.get("short_regime_ma") or "").strip()
    if not sraw:
        errs.append("做空均线为空")
    else:
        try:
            n = int(float(sraw))
        except ValueError:
            errs.append("做空均线不是数字")
            n = None
        if n is not None:
            if n < 0 or n > _R["short_regime_ma"][1]:
                errs.append(f"做空均线必须在 0~{_R['short_regime_ma'][1]} 之间（0=关闭过滤）")
            else:
                out["short_regime_ma"] = n

    mode = str(form.get("equity_mode") or "").strip().lower()
    if mode not in ("follow", "fixed"):
        errs.append("资金口径只能是 follow / fixed")
    else:
        out["equity_mode"] = mode

    if sm is not None and sx is not None and sm > sx:
        errs.append(f"止损下限 {sm:g} 不能大于上限 {sx:g}")

    # 复选框：勾选=1，未勾选=不传 → 0
    out["enable_long"] = _flag(form.get("enable_long"))
    out["enable_shorts"] = _flag(form.get("enable_shorts"))
    out["heartbeat_push"] = _flag(form.get("heartbeat_push"))
    # 到期前自动清仓（H3 + fail-open）：
    #   字段缺失（老书签的表单/脚本没带这个键）→ **保持原值**，不写库；
    #   有值（面板现在用 hidden+checkbox 双字段，必然是 0 或 1；脚本可能发别的）
    #   → 识别不了按「开」。漏清仓的代价（到期后仓位关不掉）远大于提前平一笔。
    _raw_cleanup = form.get("agent_expiry_cleanup")
    if isinstance(_raw_cleanup, (list, tuple)):          # 同名多值：取最后一个
        _raw_cleanup = _raw_cleanup[-1] if _raw_cleanup else None
    if _raw_cleanup is not None and str(_raw_cleanup).strip() != "":
        out["agent_expiry_cleanup"] = 1 if _cleanup_on(_raw_cleanup) else 0
    # 手工兜底的有效期（M3）：**空 = 不传 = 保持原值**，与同页其它数字字段一致。
    # 以前空串会落库把兜底值抹掉 —— 用户只想改个风险率，手工兜底就没了。
    # 要清空兜底走「API 有效期」卡片里的「清除兜底」按钮（显式操作）。
    _until_raw = form.get("hl_agent_valid_until_ms")
    if isinstance(_until_raw, (list, tuple)):
        _until_raw = _until_raw[-1] if _until_raw else None
    _until_txt = str(_until_raw if _until_raw is not None else "").strip().replace(",", "")
    if _until_txt:
        try:
            _until_v = int(float(_until_txt))
        except ValueError:
            errs.append("手工兜底的有效期不是数字（毫秒时间戳，可留空 = 不改动）")
            _until_v = 0
        if _until_v < 0:
            errs.append("手工兜底的有效期不能是负数")
        elif _until_v:
            out["hl_agent_valid_until_ms"] = str(_until_v)
    if eq is not None:
        out["equity"] = eq
    if rp is not None:
        out["risk_pct"] = rp
    if lev is not None:
        out["leverage"] = lev
    if sm is not None:
        out["stop_min"] = sm
    if sx is not None:
        out["stop_max"] = sx
    if emin is not None:
        out["equity_min"] = emin
    return out, errs


# ---------------------------------------------------------------- 开关 Tab
def render_switch(deps: AdminDeps, cfg: Any) -> str:
    mode = str(_call(deps.mode_fn, "off") or "off")
    if mode not in ("off", "dry", "live"):
        mode = "off"
    kill = deps.kill_on()
    addr = str(_call(deps.address_fn, "") or "")
    has_key = bool(str(_call(deps.private_key_fn, "") or "").strip())
    raw = deps.get_all()

    o: List[str] = []
    o.append("<h2>开关</h2>")

    # ---- 执行模式 ----
    o.append("<div class='card'><div class='sec'>执行模式</div>")
    o.append("<form method='post' action='/admin/switch/mode'>")
    for v, label in (("off", "只通知（不发单，人工照提醒操作）"),
                     ("dry", "影子（真下单前先在测试网演练）"),
                     ("live", "实盘（用真钱下单）")):
        o.append(f"<label class='rad'><input type='radio' name='mode' value='{v}' "
                 f"{'checked' if v == mode else ''}> {_esc(label)}</label>")
    if mode == "live":
        o.append("<div class='banner'>&#9888;&#65039; 当前是<b>实盘</b>：执行器正在用真钱下单。</div>")
    o.append("<div class='banner warn'><b>切到实盘会用真钱下单</b>，且会接管开仓/离场"
             "（人工操作卡自动不再发送）。</div>")
    o.append("<label class='chk warnline'><input type='checkbox' name='confirm_live' value='1'> "
             "我已确认：切实盘会真下单</label>")
    o.append("<button type='submit'>切换模式</button></form>")

    o.append(f"<div class='hint'>钱包地址："
             f"{'已配置（' + _esc(addr[:6]) + '&hellip;' + _esc(addr[-4:]) + '）' if addr else '未配置'}"
             f"｜私钥：{'已配置' if has_key else '未配置'}"
             f"｜地址/私钥来自 env，面板只读不改。</div></div>")

    # ---- 推送开关 ----
    o.append("<div class='card'><div class='sec'>推送开关</div>"
             "<form method='post' action='/admin/switch/notify'>")
    for key, label, desc, dflt in NOTIFY_KEYS:
        on = _b(raw.get(key, dflt))
        o.append(f"<label class='rad'><input type='checkbox' name='{_esc(key)}' value='1' "
                 f"{'checked' if on else ''}> <b>{_esc(label)}</b></label>")
        o.append(f"<div class='hint'>&nbsp;&nbsp;{_esc(desc)}"
                 f"<span class='src'>&nbsp;来源 {'DB' if key in raw else '默认'}</span></div>")
    o.append("<button type='submit'>保存推送开关</button></form></div>")

    # ---- kill switch ----
    o.append("<div class='card'><div class='sec'>kill switch</div>")
    if kill:
        o.append("<div class='banner'>&#9888;&#65039; <b>已触发</b>：执行器暂停开新仓"
                 "（仍允许人工平仓/撤单）。</div>")
    else:
        o.append("<div class='hint'>当前未触发。触发后执行器不再开新仓。</div>")
    o.append("<form method='post' action='/admin/switch/kill'>"
             f"<button class='{'red' if not kill else 'gray'}' type='submit' name='on' value='1'>"
             "触发（暂停开仓）</button>"
             f"<button class='{'gray' if kill else 'red'}' type='submit' name='on' value='0'>"
             "解除（恢复正常）</button></form></div>")
    return "".join(o)


# ---------------------------------------------------------------- 记录 Tab
def render_records(deps: AdminDeps, rows_n: int = DEFAULT_ROWS) -> str:
    n = normalize_rows(rows_n)
    o: List[str] = []
    o.append("<h2>记录</h2>")
    o.append(f"<div class='hint'>时间均为{engine.TZ_LABEL}；每块最多 {n} 行"
             f"（页脚「每页行数」可切 10/20/50）。</div>")

    # ---- 下单记录 ----
    o.append(f"<div class='card'><div class='sec'>下单记录（hl_orders · 最近 {n} 条）</div>")
    rows = ""
    _orders_rows = _rows(deps, "hl_recent_orders", n)
    if _orders_rows and _orders_rows[0].get("_error"):
        rows += f"<tr><td colspan='8' class='bad'>{_esc(_orders_rows[0]['_error'])}</td></tr>"
        _orders_rows = []
    for r in _orders_rows:
        st = str(r.get("status") or "—")
        st_cls = "neg" if st == "failed" else ("warn" if st in ("intent", "sent") else "")
        err = str(r.get("error") or "")[:60]
        rows += (f"<tr><td class='num'>{_ts(r.get('updated_at') or r.get('created_at'))}</td>"
                 f"<td>{_esc(_coin(r.get('coin'), r.get('symbol')))}</td>"
                 f"<td>{_esc(_PURPOSE_TXT.get(str(r.get('purpose')), r.get('purpose') or '—'))}</td>"
                 f"<td><span class='{st_cls}'>{_esc(_ORDER_ST_TXT.get(st, st))}</span></td>"
                 f"<td class='num'>{_fmt(r['filled_sz'] if r.get('filled_sz') is not None else r.get('sz'), 4)}</td>"
                 f"<td class='num'>{_fmt(r['avg_px'] if r.get('avg_px') is not None else r.get('px'))}</td>"
                 f"<td><span class='muted'>{_esc(err or '—')}</span></td></tr>")
    o.append(_table("<th>时间</th><th>标的</th><th>用途</th><th>状态</th>"
                    "<th class='num'>数量</th><th class='num'>均价</th><th>错误</th>",
                    rows, "暂无下单记录", 7))
    o.append("</div>")

    # ---- 成交 ----
    o.append(f"<div class='card'><div class='sec'>成交（hl_fills · 最近 {n} 条）</div>")
    rows = ""
    # 买卖方向由 dir 推导（2026-10-15 修）：side 是多空方向不是买卖方向，
    # 以前 side>=0→买 把「平多（卖出）」也标成买。四向口径：
    # Open Long / Close Short = 买；Close Long / Open Short = 卖。
    _BS = {"Open Long": "买", "Close Short": "买",
           "Close Long": "卖", "Open Short": "卖"}
    for r in _rows(deps, "hl_recent_fills", n):
        cp = _opt_num(r.get("closed_pnl"))
        cp_cls = "pos" if (cp or 0) > 0 else ("neg" if (cp or 0) < 0 else "")
        d = str(r.get("dir") or "")
        bs = _BS.get(d, "—")
        rows += (f"<tr><td class='num'>{_ts(r.get('ts') or r.get('created_at'))}</td>"
                 f"<td>{_esc(r.get('coin') or _coin('', r.get('symbol')))}</td>"
                 f"<td>{bs}</td>"
                 f"<td class='num'>{_fmt(r.get('sz'), 4)}</td>"
                 f"<td class='num'>{_fmt(r.get('px'))}</td>"
                 f"<td class='num'>{_fmt(r.get('fee'), 4)}</td>"
                 f"<td class='num {cp_cls}'>{_fmt(cp)}</td>"
                 f"<td>{'是' if r.get('reduce_only') else '—'}</td></tr>")
    o.append(_table("<th>时间</th><th>币种</th><th>买卖</th><th class='num'>数量</th>"
                    "<th class='num'>价格</th><th class='num'>手续费</th>"
                    "<th class='num'>平仓盈亏</th><th>减仓</th>",   # 列名「减仓」= 这笔是不是平仓性质
                    rows, "暂无成交记录", 8))
    o.append("</div>")

    # ---- 提醒日志 ----
    o.append(f"<div class='card'><div class='sec'>提醒日志（alert_logs · 最近 {n} 条）</div>")
    _alert_rows = _rows(deps, "recent_alerts", n)
    _backfilled = sum(1 for r in _alert_rows
                      if str(r.get("message") or "").startswith(BACKFILL_PREFIX))
    if _backfilled:
        o.append(f"<div class='hint'>2026-10-15 之前执行器推送不落库（当时版本限制），"
                 f"下表含 {_backfilled} 条按执行器事件（hl_events）回填的历史记录，"
                 f"标记为「[回填]」。</div>")
    rows = ""
    for r in _alert_rows:
        ok = bool(r.get("sent_successfully"))
        msg = str(r.get("message") or "")
        _kind = str(r.get("kind") or "—")
        rows += (f"<tr><td class='num'>{_ts(r.get('created_at'))}</td>"
                 f"<td>{_esc(_ALERT_KIND_TXT.get(_kind, _kind))}</td>"
                 f"<td>{_esc(r.get('symbol') or '—')}</td>"
                 f"<td><span class='{'pos' if ok else 'neg'}'>"
                 f"{'已送达' if ok else '失败'}</span></td>"
                 f"<td>{_msg_cell(msg)}</td></tr>")
    o.append(_table("<th>时间</th><th>类型</th><th>标的</th><th>状态</th><th>内容</th>",
                    rows, "暂无提醒日志", 5))
    o.append("</div>")

    # ---- 执行器事件 ----
    o.append(f"<div class='card'><div class='sec'>执行器事件（hl_events · 最近 {n} 条）</div>")
    rows = ""
    for r in _rows(deps, "hl_recent_events", n):
        kind = str(r.get("kind") or "—")
        msg = str(r.get("message") or "")
        rows += (f"<tr><td class='num'>{_ts(r.get('created_at'))}</td>"
                 f"<td>{_esc(_EV_TXT.get(kind, kind))}</td>"
                 f"<td>{_esc(r.get('symbol') or '—')}</td>"
                 f"<td>{_msg_cell(msg)}</td></tr>")
    o.append(_table("<th>时间</th><th>类型</th><th>标的</th><th>内容</th>",
                    rows, "暂无执行器事件", 4))
    o.append("</div>")
    return "".join(o)


def _coin(coin: Any, symbol: Any) -> str:
    """优先显示 HL 侧币种名（BTC），否则从 MEXC 合约名（BTC_USDT）截出来。"""
    c = str(coin or "").strip()
    if c:
        return c
    s = str(symbol or "").strip()
    return s.split("_")[0] if s else "—"


# ---------------------------------------------------------------- 页面骨架
ADMIN_CSS = """<style>
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 14px}
.tabs a{font-size:13px;padding:7px 13px;border-radius:8px;background:var(--card);
        border:1px solid var(--line);color:var(--link);text-decoration:none;white-space:nowrap}
.tabs a.on{background:var(--btn);border-color:var(--btn);color:#fff;font-weight:600}
table{font-size:13px}
th,td{padding:6px 8px}
.field{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;padding:9px 0;
       border-bottom:1px solid var(--line2)}
.field:last-child{border-bottom:0}
.field .lbl{flex:1 1 160px;min-width:130px;font-size:13px}
.field input[type=text]{width:118px;background:var(--input);border:1px solid var(--line);color:var(--fg);
       border-radius:6px;padding:7px 9px;font-size:13px}
.field select{background:var(--input);border:1px solid var(--line);color:var(--fg);
       border-radius:6px;padding:7px 9px;font-size:13px}
.field .hint,.hint{width:100%}
.hint{font-size:12.5px;color:var(--muted);margin:2px 0 6px;line-height:1.5}
.hint.big{font-size:13px;color:var(--fg)}
.src{color:var(--muted);font-size:11.5px}
.chk,.rad{display:flex;align-items:center;gap:8px;font-size:13px;margin:7px 0}
.chk.warnline{color:var(--warn)}
.chk input,.rad input{width:17px;height:17px;accent-color:var(--btn)}
.banner.warn{border-color:var(--wb-line);background:var(--wb-bg);color:var(--wb-fg)}
.banner.ok{border-color:var(--okb-line);background:var(--okb-bg);color:var(--okb-fg)}
button{display:inline-block;background:var(--btn);color:#fff;border:0;border-radius:6px;
       padding:9px 15px;font-size:13.5px;cursor:pointer;margin:6px 8px 0 0}
button.gray{background:var(--btn-gray)}button.red{background:#b91c1c}
@media (max-width:640px){body{padding:9px;font-size:13px}h1{font-size:17px}
   .badge{padding:5px 9px;font-size:11.5px}h2{font-size:13px}
   .grid{grid-template-columns:1fr}table{font-size:13px}th,td{padding:5px 7px}
   .field input[type=text]{width:100px}}
</style>"""


def _status_strip(p: dict) -> str:
    """顶部常显状态条：执行模式 / 熔断 / 对账 / HL 延迟 / 更新时间。"""
    mode = str(p.get("mode") or "off")
    if mode not in _MODE_TXT:
        mode = "off"
    kill = bool(p.get("kill"))
    system = p.get("system") or {}
    net = p.get("network") or {}
    mismatch = bool(system.get("mismatch"))
    lat = _opt_num(net.get("latency_ms"))
    kill_reason = str(p.get("kill_reason") or "").strip()
    # 熔断触发且原因非空 → 值里带上原因（可见文字）；iOS Safari 无 title 悬停，
    # 原先原因只在 title 里、手机上读不到。未触发时值不变，title 保留。
    kill_val = "未触发"
    if kill:
        kill_val = f"已触发 · {_esc(kill_reason)}" if kill_reason else "已触发"
    items = [
        _badge(_MODE_CLS[mode], "模式", _MODE_TXT[mode]),
        _badge("neg" if kill else "pos", "熔断", kill_val, kill_reason),
        _badge("pos" if not mismatch else "neg", "对账", "一致" if not mismatch else "不一致"),
        _badge("pos" if lat is not None else "muted", "HL 延迟",
               f"{lat:.0f} ms" if lat is not None else "—",
               "服务器请求 Hyperliquid 的耗时，与你到面板的延迟无关"),
        _badge("muted", "更新", _ts(p.get("ts"))),
    ]
    return "<div class='bar'>" + "".join(items) + "</div>"


def _tabs_html(tab: str, k: str) -> str:
    q = quote(str(k or ""))
    out = ["<div class='tabs'>"]
    for key, label in TABS:
        cls = "on" if key == tab else ""
        out.append(f"<a class='{cls}' href='/admin?tab={key}&k={q}'>{label}</a>")
    out.append("</div>")
    return "".join(out)


def _overview_body(deps: AdminDeps, payload: dict) -> str:
    """总览：完全复用 hl/views._render_body（仓位表/权益曲线/挂单/事件一块都不重写）。

    _render_body 自带 <h1> 和它自己的状态条，而本面板顶部已有标题 + 状态条，
    所以把这两块剥掉，避免同一屏出现两个 <h1>、两条状态条。

    「API 有效期」卡片接在整个总览的**最后**（在「系统」卡之后）：它是低频查看的
    运维信息，用户日常看的是三标的动态/账户/仓位；同时它不插进「三标的动态」与账户卡
    之间（那个位置由需求钉死给三标的动态）。卡片自己兜底，渲染失败不弄挂总览。
    """
    body = _render_body(payload)
    body = re.sub(r"<h1>.*?</h1>\s*", "", body, count=1, flags=re.S)
    body = re.sub(r"<div class='bar'>.*?</div>\s*", "", body, count=1, flags=re.S)
    try:
        body += render_agent_card(deps)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] API 有效期卡片渲染失败：%s", e)
    return body


# ---------------------------------------------------------------- API 有效期卡片
_LEVEL_CLS = {"ok": "pos", "near": "warn", "urgent": "neg", "cleanup": "neg",
              "expired": "neg", "unknown": "muted", "no_expiry": "pos"}


def _agent_source_txt(src: str) -> str:
    return {"api": "HL 授权接口", "manual": "手工兜底", "none": ""}.get(str(src or ""), "")


def render_agent_card(deps: AdminDeps) -> str:
    """「API 有效期」卡片：地址（截断）/到期日/剩余天数/状态词 + 「更新 API」折叠表单。

    数据只来自 ``hl_agent_state``（DB 缓存）—— 渲染**零网络调用**，面板在 HL 接口
    全挂时也能打开。state 缺失/损坏时显示「未知」并给出「下一个整点会刷新」的说明。
    """
    from app.hl import agent as agent_mod
    state = {}
    if deps.agent_state_fn is not None:
        try:
            state = deps.agent_state_fn() or {}
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[admin] 读 API 有效期状态失败：%s", e)
            state = {}
    if not isinstance(state, dict):
        state = {}

    on = True
    if deps.cleanup_enabled_fn is not None:
        try:
            on = bool(deps.cleanup_enabled_fn())
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[admin] 读自动清仓开关失败（按开显示）：%s", e)
            on = True

    until = state.get("valid_until_ms")
    days = state.get("days_left")
    try:
        days_i = int(days) if days not in (None, "") else None
    except (TypeError, ValueError):
        days_i = None
    level = str(state.get("level") or "")
    if not level:
        # 老 state / 手写 state 没有 level 字段时的兜底（有 until 就按毫秒重算）
        level = agent_mod.level_ms(until, state.get("checked_ms")) if until else "unknown"
    if level not in agent_mod.LEVEL_TXT:
        level = "unknown"
    cleanup_state = str(state.get("cleanup") or "none")
    state_word = agent_mod.LEVEL_TXT.get(level, "未知")
    if cleanup_state == "done" and level != "expired":
        state_word = "已清仓"
    elif cleanup_state == "failed":
        state_word = "清仓失败（待重试）"
    elif cleanup_state == "skipped":
        state_word = "未自动清仓（执行器 off）"
    elif level == "no_expiry":
        state_word = "不限期"
    elif level == "cleanup":
        # 「临界」这个词手机上读不出多重：明确写清楚 ≤3 天会做什么
        state_word = "临界（3 天内自动清仓）" if on else "临界（不会自动清仓）"
    cls = _LEVEL_CLS.get(level, "muted")

    addr = agent_mod.mask_addr(state.get("address"))
    src = _agent_source_txt(str(state.get("source") or ""))
    if level == "no_expiry":
        until_txt = "不限期（HL 授权没设到期）"
        left_txt = "不会到期"
    elif until:
        until_txt = agent_mod.fmt_bj(until)
        # 白话、不重复数字：剩余天数只在「还剩」一句出现一次
        if level == "expired":
            left_txt = "已过期"
        elif days_i is not None and days_i <= 0:
            left_txt = "不到 1 天"
        elif days_i is not None:
            left_txt = f"还剩 {days_i} 天"
        else:
            left_txt = "—"
    else:
        until_txt = "未知（还没查到授权有效期）"
        left_txt = "下一个整点自动检查"

    o: List[str] = []
    o.append("<h2>API 有效期</h2>")
    o.append("<div class='card'><div class='grid'>")
    o.append(_kv("代理钱包地址", f"<span class='mono'>{_esc(addr)}</span>",
                 title="执行器用来签名的 API 钱包（不是主钱包地址）"))
    o.append(_kv("到期日", _esc(until_txt)))
    o.append(_kv("剩余", _esc(left_txt)))
    o.append(_kv("状态", _esc(state_word), cls))
    o.append(_kv("到期前自动清仓", "已开启" if on else "已关闭",
                 "pos" if on else "neg",
                 title="剩余 3 天以内自动撤单并平掉全部仓位，避免到期后想平也平不了"))
    o.append("</div>")
    if not src:
        o.append("<div class='hint'>还没读到授信信息：系统每个整点检查一次；"
                 "也可以到「参数」页填手工兜底的有效期。</div>")
    elif src == "manual":
        o.append("<div class='hint'>有效期来自「参数」页的手工兜底值（HL 授权接口没查到同名 Hermes）。</div>")
    o.append("<div class='legend'>到期后执行器不能再开仓、平仓、撤单；到期前系统会按剩余天数提醒"
             f"（≤{agent_mod.THRESH_CLEANUP} 天且有仓位时自动清仓，可在「参数」页关闭）。</div>")

    # ---- 「更新 API」折叠表单（原生 <details>，零 JS）
    o.append("<details class=\"msgdetails\"><summary>更新 API（在 HL 网页续期后粘贴新私钥）</summary>"
             "<div class=\"fullmsg\">"
             "<div class='hint'>步骤：① 电脑/手机浏览器打开 HL 网页 → <b>More → API</b>；"
             f"② 用<b>同名 {agent_mod.AGENT_NAME}</b> 生成新 API 钱包（有效期选最长的 180 天）；"
             "③ 复制那串新私钥，粘贴到下面保存。</div>"
             "<form method='post' action='/admin/hl/agent/renew' autocomplete='off'>"
             f"<input type='hidden' name='k' value='{_esc(_token_of(deps))}'>"
             "<input type='password' name='privkey' autocomplete='off' spellcheck='false' "
             "placeholder='0x…（64 位十六进制）' required>"
             "<button type='submit'>校验并保存</button></form>"
             "<div class='hint'>保存前会先核对：新私钥对应的地址必须已在 HL 授权给同名 "
             f"{agent_mod.AGENT_NAME}、且有效期还没过。私钥只写进服务器数据库（执行器签名用），"
             "不回显、不进日志。主钱包私钥不需要填。</div>"
             "</div></details>")
    o.append("</div>")
    return "".join(o)


def _token_of(deps: AdminDeps) -> str:
    """当前面板 token（给卡片里的表单当隐藏字段）。取不到就用空串（Cookie 也能鉴权）。"""
    try:
        return str(_call(deps.token_fn, "") or "")
    except Exception:                                             # noqa: BLE001
        return ""


def _page(token: str, tab: str, strip_html: str, body_html: str, msg: str = "",
          theme: str = DEFAULT_THEME, rows_n: int = DEFAULT_ROWS) -> HTMLResponse:
    label = dict(TABS).get(tab, "总览")
    banner = ""
    if msg:
        banner = f"<div class='banner ok'>{_esc(msg)}</div>"
    attr = theme_attr(theme)
    attr_html = f" data-theme='{attr}'" if attr else ""
    from app.hl.views import THEME_CSS
    return HTMLResponse(
        f"<!doctype html><html lang='zh-CN'{attr_html}><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>crypto-ma-alert · {label}</title>{THEME_CSS}{DASH_CSS}{ADMIN_CSS}</head>"
        f"<body><div class='wrap'><h1>crypto-ma-alert 管理面板</h1>"
        f"{strip_html}{_tabs_html(tab, token)}{banner}{body_html}"
        f"{theme_switch_html(tab, theme, token)}"
        f"{rows_switch_html(tab, rows_n, token)}"
        "<div class='foot'><span class='muted'>手机可用 · 零 JS · "
        "面板只读，不持私钥</span></div></div></body></html>")


def _back(token: str, tab: str, msg: str = "已保存") -> str:
    q = [f"tab={tab}", f"k={quote(str(token or ''))}"]
    if msg:
        q.append(quote(msg, safe=""))
        q[-1] = "msg=" + q[-1]
    return "/admin?" + "&".join(q)


def _auth(deps: AdminDeps, request: Request, k: Optional[str]) -> str:
    token = str(_call(deps.token_fn, "") or "")
    if not token:
        raise HTTPException(500, "ADMIN_TOKEN 未配置")
    supplied = k or request.query_params.get("k") or request.cookies.get("cma_admin")
    if supplied != token:
        raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
    return token


# ---------------------------------------------------------------- 路由
def build_admin_router(deps: AdminDeps) -> APIRouter:
    """合并后的管理面板路由（GET /admin 四 Tab + 三个 POST + 三条旧地址重定向）。"""
    router = APIRouter(tags=["admin"])

    @router.get("/admin", response_class=HTMLResponse)
    def admin_panel(request: Request, tab: str = "overview",
                    k: Optional[str] = None, msg: str = ""):
        token = _auth(deps, request, k or None)
        if tab not in _TAB_KEYS:
            tab = "overview"
        cfg = _call(deps.config_fn)
        # 每页行数：?n= 优先（页脚链接会用），否则读 cookie，认不出回落默认 20
        rows_n = normalize_rows(request.query_params.get("n")
                                or request.cookies.get(ROWS_COOKIE))
        payload = get_payload(deps)
        payload["rows"] = rows_n          # 总览的两张长表按这个切片（views._render_body）
        strip = _status_strip(payload)
        if tab == "overview":
            body = _overview_body(deps, payload)
        elif tab == "params":
            body = render_params(deps, cfg, payload, btc_price(deps))
        elif tab == "switch":
            body = render_switch(deps, cfg)
        else:
            body = render_records(deps, rows_n)
        resp = _page(token, tab, strip, body, msg,
                     theme=normalize_theme(request.cookies.get(THEME_COOKIE)),
                     rows_n=rows_n)
        # Cookie 必须设在返回的 response 上：Starlette 在函数返回新 Response 时会丢弃
        # 注入的 response 上写的头（所以这里不能用依赖注入的 response）。
        if k:
            https_ = request.url.scheme == "https" or \
                str(request.headers.get("x-forwarded-proto", "")).lower() == "https"
            resp.set_cookie("cma_admin", token, httponly=True, max_age=30 * 86400,
                            secure=https_, samesite="lax")
        return resp

    # ---------------- 参数 ----------------
    @router.post("/admin/params")
    def admin_params(request: Request, equity: str = Form(""), risk_pct: str = Form(""),
                     leverage: str = Form(""), stop_min: str = Form(""),
                     stop_max: str = Form(""), short_regime_ma: str = Form(""),
                     equity_mode: str = Form("follow"), equity_min: str = Form(""),
                     agent_expiry_cleanup: Optional[str] = Form(None),
                     hl_agent_valid_until_ms: str = Form(""),
                     enable_long: Optional[str] = Form(None),
                     enable_shorts: Optional[str] = Form(None),
                     heartbeat_push: Optional[str] = Form(None),
                     k: str = Form("")):
        token = _auth(deps, request, k or None)
        values, errs = _parse_params({
            "equity": equity, "risk_pct": risk_pct, "leverage": leverage,
            "stop_min": stop_min, "stop_max": stop_max,
            "short_regime_ma": short_regime_ma, "equity_mode": equity_mode,
            "equity_min": equity_min, "enable_long": enable_long,
            "enable_shorts": enable_shorts, "heartbeat_push": heartbeat_push,
            "agent_expiry_cleanup": agent_expiry_cleanup,
            "hl_agent_valid_until_ms": hl_agent_valid_until_ms,
        })
        if errs:
            raise HTTPException(400, "参数校验失败：" + "；".join(errs))
        put_errs = [err for err in (deps.put(key, val) for key, val in values.items()) if err]
        if put_errs:
            raise HTTPException(400, "保存失败（其余字段可能已写入）：" + "；".join(put_errs))
        deps.log_event("decision",
                       f"面板修改参数（{', '.join(f'{k}={v:g}' if isinstance(v, float) else f'{k}={v}'
                                                  for k, v in sorted(values.items()))}）")
        return RedirectResponse(_back(token, "params"), status_code=303)

    # ---------------- 执行模式 ----------------
    @router.post("/admin/switch/mode")
    def admin_switch_mode(request: Request, mode: str = Form("off"),
                          confirm_live: Optional[str] = Form(None), k: str = Form("")):
        token = _auth(deps, request, k or None)
        m = str(mode or "").strip()
        if m not in ("off", "dry", "live"):
            raise HTTPException(400, "mode 只能是 off/dry/live")
        if m == "live":
            if str(confirm_live or "") != "1":
                raise HTTPException(400, "切实盘需要勾选二次确认（切到实盘会真下单）")
            missing = []
            if not str(_call(deps.address_fn, "") or "").strip():
                missing.append("HL_ACCOUNT_ADDRESS")
            if not str(_call(deps.private_key_fn, "") or "").strip():
                missing.append("HL_PRIVATE_KEY")
            if missing:
                raise HTTPException(400, "未配置 " + " / ".join(missing) + "，不能进 live")
        err = deps.put("hl_exec_mode", m)
        if err:
            raise HTTPException(400, f"模式切换保存失败：{err}（面板显示的仍是旧模式）")
        deps.log_event("decision", f"面板切换执行器模式 → {m}")
        if m != "live":
            if deps.hl_set_kill is not None:
                try:
                    deps.hl_set_kill(False, "")
                except Exception as e:                                   # noqa: BLE001
                    logger.warning("[admin] 清 kill 失败：%s", e)
        return RedirectResponse(_back(token, "switch"), status_code=303)

    # ---------------- 推送开关 ----------------
    @router.post("/admin/switch/notify")
    def admin_switch_notify(request: Request, heartbeat_push: Optional[str] = Form(None),
                            report_push: Optional[str] = Form(None),
                            fill_push: Optional[str] = Form(None), k: str = Form("")):
        token = _auth(deps, request, k or None)
        raw_vals = {"heartbeat_push": heartbeat_push, "report_push": report_push,
                    "fill_push": fill_push}
        vals = {key: _flag(val) for key, val in raw_vals.items()}
        put_errs = [e for e in (deps.put(key, val) for key, val in vals.items()) if e]
        if put_errs:
            raise HTTPException(400, "开关保存失败：" + "；".join(put_errs))
        deps.log_event("decision",
                       "面板修改推送开关：" + "，".join(
                           f"{key}={'开' if val else '关'}" for key, val in vals.items()))
        return RedirectResponse(_back(token, "switch"), status_code=303)

    # ---------------- kill switch ----------------
    @router.post("/admin/switch/kill")
    def admin_switch_kill(request: Request, on: str = Form("0"), k: str = Form("")):
        token = _auth(deps, request, k or None)
        want = str(on or "") in ("1", "true", "on")
        if deps.hl_set_kill is not None:
            deps.hl_set_kill(want, "人工" if want else "")
            deps.log_event("kill", f"面板{'触发' if want else '解除'} kill switch")
        else:
            # 没有真实动作就不记「已触发」事件 —— 事件与事实必须一致
            deps.log_event("kill", f"面板请求{'触发' if want else '解除'} kill switch，"
                                   "但执行器未接线（无动作）")
        return RedirectResponse(_back(token, "switch"), status_code=303)

    # ---------------- 清除手工兜底有效期（M3 的显式入口） ----------------
    @router.post("/admin/hl/agent/fallback/clear")
    def admin_agent_fallback_clear(request: Request, k: str = Form("")):
        """清掉「参数」页的手工兜底有效期（把空串写进 DB）。

        「参数」页的空输入现在是「不改动」（与同页其它字段一致），所以清空必须
        有独立入口 —— 否则用户永远删不掉填错的兜底值。
        """
        token = _auth(deps, request, k or None)
        err = deps.put("hl_agent_valid_until_ms", "")
        if err:
            raise HTTPException(400, f"清除失败：{err}")
        deps.log_event("decision", "面板清除手工兜底有效期（hl_agent_valid_until_ms）")
        return RedirectResponse(_back(token, "params", "已清除手工兜底有效期"),
                                status_code=303)

    # ---------------- 更新 API（到期续期后热切换私钥） ----------------
    @router.post("/admin/hl/agent/renew")
    def admin_agent_renew(request: Request, privkey: str = Form(""), k: str = Form("")):
        """校验新私钥并热切换（form 字段 `privkey`，不是 JSON body）。

        失败一律 400 + 人话指引；**响应与日志都不含私钥任何片段**（见 app.hl.agent.renew_key：
        那里只回派生地址与日期，异常路径也只回异常类型）。
        """
        token = _auth(deps, request, k or None)
        if deps.renew_fn is None:
            raise HTTPException(400, "更新 API 未接线（执行器模块未装配），本次没有保存任何东西")
        try:
            res = deps.renew_fn(privkey)
        except Exception as e:                                    # noqa: BLE001
            # 只记异常类型：异常 repr 里理论上可能带上入参（例如 SDK 的校验错误）
            logger.error("[admin] 更新 API 异常：%s", type(e).__name__)
            raise HTTPException(400, "更新失败（内部错误），私钥未保存，请稍后重试")
        ok, msg = (res if isinstance(res, (tuple, list)) and len(res) >= 2 else (False, str(res)))
        if not ok:
            raise HTTPException(400, str(msg))
        return RedirectResponse(_back(token, "overview", str(msg)), status_code=303)

    # ---------------- 主题切换（零 JS：设 cookie 后跳回） ----------------
    @router.get("/admin/theme")
    def admin_theme(request: Request, t: str = DEFAULT_THEME, tab: str = "overview",
                    k: Optional[str] = None):
        theme = normalize_theme(t)
        tab = tab if tab in _TAB_KEYS else "overview"
        # 未带 k 且 cookie 已登录 → 用 cookie 鉴权（页脚链接只有 admin 页里有，已登录场景）
        token = _auth(deps, request, k or None)
        https_ = request.url.scheme == "https" or \
            str(request.headers.get("x-forwarded-proto", "")).lower() == "https"
        resp = RedirectResponse(f"/admin?tab={tab}&k={quote(str(token))}", status_code=302)
        # max_age=0 删除；auto 也写死成 cookie（显式「跟随系统」选择）
        resp.set_cookie(THEME_COOKIE, theme, max_age=365 * 86400,
                        secure=https_, samesite="lax")
        return resp

    # ---------------- 每页行数切换（零 JS：设 cookie 后跳回） ----------------
    @router.get("/admin/rows")
    def admin_rows(request: Request, n: str = str(DEFAULT_ROWS), tab: str = "records",
                   k: Optional[str] = None):
        rows_n = normalize_rows(n)
        tab = tab if tab in _TAB_KEYS else "overview"
        token = _auth(deps, request, k or None)
        https_ = request.url.scheme == "https" or \
            str(request.headers.get("x-forwarded-proto", "")).lower() == "https"
        resp = RedirectResponse(f"/admin?tab={tab}&k={quote(str(token))}", status_code=302)
        resp.set_cookie(ROWS_COOKIE, str(rows_n), max_age=365 * 86400,
                        secure=https_, samesite="lax")
        return resp

    # ---------------- 旧地址兼容（302 到新位置，保持 ?k= 透传） ----------------
    @router.get("/hl", response_class=HTMLResponse)
    def hl_redirect(request: Request, k: Optional[str] = None, refresh: int = 0):
        return RedirectResponse(f"/admin?tab=overview&k={quote(str(k or ''))}",
                                status_code=302)

    @router.get("/admin/rules", response_class=HTMLResponse)
    def rules_redirect(request: Request, k: Optional[str] = None):
        _auth(deps, request, k or None)
        return RedirectResponse(f"/admin?tab=params&k={quote(str(k or ''))}",
                                status_code=302)

    @router.get("/admin/logs", response_class=HTMLResponse)
    def logs_redirect(request: Request, k: Optional[str] = None):
        _auth(deps, request, k or None)
        return RedirectResponse(f"/admin?tab=records&k={quote(str(k or ''))}",
                                status_code=302)

    return router
