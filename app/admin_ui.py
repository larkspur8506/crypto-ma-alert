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
from app.hl.views import DASH_CSS, _render_body, status_payload

logger = logging.getLogger(__name__)

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
    def put(self, key: str, value: Any) -> None:
        if self.set_config is None:
            return
        try:
            self.set_config(key, value)
        except Exception as e:                                    # noqa: BLE001
            logger.error("[admin] 写配置 %s 失败：%s", key, e)

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
        return []
    return [_rowd(r) for r in out]


def _badge(dot_cls: str, label: str, value: str, title: str = "") -> str:
    t = f" title='{_esc(title)}'" if title else ""
    return (f"<span class='badge'{t}><span class='{dot_cls}'>&#9679;</span> "
            f"{label} <b>{value}</b></span>")


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
            "margin": notional / lev, "liq": sizing.liq_price(price, lev, 1)}


def _impact_line(d: Optional[dict]) -> str:
    if not d:
        return "影响预览：无法取价（等只读接口恢复后再算）"
    return (f"按 BTC 现价 {d['price']:,.0f}：单笔风险 {d['risk_u']:.2f}U · "
            f"名义 {d['notional']:,.0f}U · 保证金 {d['margin']:,.0f}U · 强平 {d['liq']:,.0f}")


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
)
_GROUPS: Tuple[Tuple[str, str], ...] = (
    ("策略", "决定什么信号会被发出来"),
    ("执行与推送", "决定下多大、占多少保证金"),
    ("资金", "下单基数怎么算"),
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
                   f"强平 ≈{imp['liq']:,.0f}"
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
    return ""


_TRUTHY = ("1", "true", "yes", "on", "y", "t")


def _flag(val: Any) -> int:
    """开关取值：'0'/'false'/'off'/空 = 关。

    不能只写 `1 if val else 0` —— 字符串 "0" 在 Python 里是真值，会把「关」写成「开」
    （fail-dangerous：脚本/表单发 0 反而打开推送）。
    """
    return 1 if str(val if val is not None else "").strip().lower() in _TRUTHY else 0


def _num_field(key: str, label: str, value: str, hint: str, src: str, step: str = "") -> str:
    st = f" step='{step}'" if step else ""
    return (f"<div class='field'><div class='lbl'>{_esc(label)}"
            f"<span class='src'>&nbsp;来源 {src} · 当前 {_esc(value)}</span></div>"
            f"<input type='text' inputmode='decimal' name='{_esc(key)}' value='{_esc(value)}'{st}>"
            f"<div class='hint'>{hint}</div></div>")


def _bool_field(key: str, label: str, on: bool, hint: str, src: str) -> str:
    return (f"<div class='field'><div class='lbl'>{_esc(label)}"
            f"<span class='src'>&nbsp;来源 {src} · 当前 {'开' if on else '关'}</span></div>"
            f"<label class='chk'><input type='checkbox' name='{_esc(key)}' value='1' "
            f"{'checked' if on else ''}></label><div class='hint'>{hint}</div></div>")


def render_params(deps: AdminDeps, cfg: Any, payload: dict, price: float) -> str:
    raw = deps.get_all()
    live, base, base_txt = effective_base(deps, cfg, payload)
    imp = impact(cfg, price, base)
    imp_max = impact(cfg, price, base, stop_dist=_num(getattr(cfg, "stop_max", 0)))

    o: List[str] = []
    o.append("<h2>参数</h2>")
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

    o.append("<form method='post' action='/admin/params'>")
    o.append("<button type='submit'>保存全部参数</button></form>")
    return "".join(o)


def _parse_params(form: Dict[str, str]) -> Tuple[Dict[str, Any], List[str]]:
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

    eq = take_float("equity", "账户权益基数", 0.0, 10_000_000.0)
    rp = take_float("risk_pct", "单笔风险", 0.0, 0.5)
    lev = take_float("leverage", "杠杆", 0.999, 125.0, strict=False)
    sm = take_float("stop_min", "止损下限", 0.0, 1.0)
    sx = take_float("stop_max", "止损上限", 0.0, 1.0)
    emin = take_float("equity_min", "资金下限", 0.0, 1_000_000.0, strict=False)

    # 做空均线：必须是整数
    sraw = str(form.get("short_regime_ma") or "").strip()
    if not sraw:
        errs.append("做空均线为空")
    else:
        try:
            n = int(float(sraw))
        except ValueError:
            errs.append("做空均线不是数字")
            n = -1
        if n < 0 or n > 2000:
            errs.append("做空均线必须在 0~2000 之间（0=关闭过滤）")
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
def render_records(deps: AdminDeps) -> str:
    o: List[str] = []
    o.append("<h2>记录</h2>")
    o.append(f"<div class='hint'>时间均为{engine.TZ_LABEL}；每块最多 50 行。</div>")

    # ---- 下单记录 ----
    o.append("<div class='card'><div class='sec'>下单记录（hl_orders）</div>")
    rows = ""
    for r in _rows(deps, "hl_recent_orders", 50):
        st = str(r.get("status") or "—")
        st_cls = "neg" if st == "failed" else ("warn" if st in ("intent", "sent") else "")
        err = str(r.get("error") or "")[:60]
        rows += (f"<tr><td class='num'>{_ts(r.get('updated_at') or r.get('created_at'))}</td>"
                 f"<td>{_esc(_coin(r.get('coin'), r.get('symbol')))}</td>"
                 f"<td>{_esc(_PURPOSE_TXT.get(str(r.get('purpose')), r.get('purpose') or '—'))}</td>"
                 f"<td><span class='{st_cls}'>{_esc(_ORDER_ST_TXT.get(st, st))}</span></td>"
                 f"<td class='num'>{_fmt(r.get('filled_sz') or r.get('sz'), 4)}</td>"
                 f"<td class='num'>{_fmt(r.get('avg_px') or r.get('px'))}</td>"
                 f"<td><span class='muted'>{_esc(err or '—')}</span></td></tr>")
    o.append(_table("<th>时间</th><th>标的</th><th>用途</th><th>状态</th>"
                    "<th class='num'>数量</th><th class='num'>均价</th><th>错误</th>",
                    rows, "暂无下单记录", 7))
    o.append("</div>")

    # ---- 成交 ----
    o.append("<div class='card'><div class='sec'>成交（hl_fills）</div>")
    rows = ""
    for r in _rows(deps, "hl_recent_fills", 50):
        cp = _opt_num(r.get("closed_pnl"))
        cp_cls = "pos" if (cp or 0) > 0 else ("neg" if (cp or 0) < 0 else "")
        rows += (f"<tr><td class='num'>{_ts(r.get('ts') or r.get('created_at'))}</td>"
                 f"<td>{_esc(r.get('coin') or _coin('', r.get('symbol')))}</td>"
                 f"<td>{'买' if _num(r.get('side'), 0) >= 0 else '卖'}</td>"
                 f"<td class='num'>{_fmt(r.get('sz'), 4)}</td>"
                 f"<td class='num'>{_fmt(r.get('px'))}</td>"
                 f"<td class='num'>{_fmt(r.get('fee'), 4)}</td>"
                 f"<td class='num {cp_cls}'>{_fmt(cp)}</td>"
                 f"<td>{'减仓' if r.get('reduce_only') else '—'}</td></tr>")
    o.append(_table("<th>时间</th><th>币种</th><th>方向</th><th class='num'>数量</th>"
                    "<th class='num'>价格</th><th class='num'>手续费</th>"
                    "<th class='num'>平仓盈亏</th><th>减仓</th>",
                    rows, "暂无成交记录", 8))
    o.append("</div>")

    # ---- 提醒日志 ----
    o.append("<div class='card'><div class='sec'>提醒日志（alert_logs）</div>")
    rows = ""
    for r in _rows(deps, "recent_alerts", 50):
        ok = bool(r.get("sent_successfully"))
        msg = str(r.get("message") or "")
        shown = msg if len(msg) <= 160 else msg[:160] + "&hellip;"
        rows += (f"<tr><td class='num'>{_ts(r.get('created_at'))}</td>"
                 f"<td>{_esc(r.get('kind') or '—')}</td>"
                 f"<td>{_esc(r.get('symbol') or '—')}</td>"
                 f"<td><span class='{'pos' if ok else 'neg'}'>"
                 f"{'已送达' if ok else '失败'}</span></td>"
                 f"<td><span title='{_esc(msg)}'>{_esc(shown)}</span></td></tr>")
    o.append(_table("<th>时间</th><th>类型</th><th>标的</th><th>状态</th><th>内容</th>",
                    rows, "暂无提醒日志", 5))
    o.append("</div>")

    # ---- 执行器事件 ----
    o.append("<div class='card'><div class='sec'>执行器事件（hl_events）</div>")
    rows = ""
    for r in _rows(deps, "hl_recent_events", 50):
        kind = str(r.get("kind") or "—")
        msg = str(r.get("message") or "")
        shown = msg if len(msg) <= 160 else msg[:160] + "&hellip;"
        rows += (f"<tr><td class='num'>{_ts(r.get('created_at'))}</td>"
                 f"<td>{_esc(_EV_TXT.get(kind, kind))}</td>"
                 f"<td>{_esc(r.get('symbol') or '—')}</td>"
                 f"<td><span title='{_esc(msg)}'>{_esc(shown)}</span></td></tr>")
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
.tabs a{font-size:13px;padding:7px 13px;border-radius:8px;background:#171a21;
        border:1px solid #262a33;color:#9ad;text-decoration:none;white-space:nowrap}
.tabs a.on{background:#2563eb;border-color:#2563eb;color:#fff;font-weight:600}
table{font-size:13px}
th,td{padding:6px 8px}
.field{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;padding:9px 0;
       border-bottom:1px solid #1e222b}
.field:last-child{border-bottom:0}
.field .lbl{flex:1 1 160px;min-width:130px;font-size:13px}
.field input[type=text]{width:118px;background:#0f1115;border:1px solid #374151;color:#e6e6e6;
       border-radius:6px;padding:7px 9px;font-size:13px}
.field select{background:#0f1115;border:1px solid #374151;color:#e6e6e6;
       border-radius:6px;padding:7px 9px;font-size:13px}
.field .hint,.hint{width:100%}
.hint{font-size:12.5px;color:#8b95a5;margin:2px 0 6px;line-height:1.5}
.hint.big{font-size:13px;color:#e6e6e6}
.src{color:#8b95a5;font-size:11.5px}
.chk,.rad{display:flex;align-items:center;gap:8px;font-size:13px;margin:7px 0}
.chk.warnline{color:#fbbf24}
.chk input,.rad input{width:17px;height:17px;accent-color:#2563eb}
.banner.warn{border-color:#78350f;background:#2a1d08;color:#fcd34d}
button{display:inline-block;background:#2563eb;color:#fff;border:0;border-radius:6px;
       padding:9px 15px;font-size:13.5px;cursor:pointer;margin:6px 8px 0 0}
button.gray{background:#374151}button.red{background:#b91c1c}
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
    items = [
        _badge(_MODE_CLS[mode], "模式", _MODE_TXT[mode]),
        _badge("neg" if kill else "pos", "熔断", "已触发" if kill else "未触发",
               str(p.get("kill_reason") or "")),
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
    """
    body = _render_body(payload)
    body = re.sub(r"<h1>.*?</h1>\s*", "", body, count=1, flags=re.S)
    body = re.sub(r"<div class='bar'>.*?</div>\s*", "", body, count=1, flags=re.S)
    return body


def _page(token: str, tab: str, strip_html: str, body_html: str, msg: str = "") -> HTMLResponse:
    label = dict(TABS).get(tab, "总览")
    banner = ""
    if msg:
        banner = (f"<div class='banner' style='border-color:#14532d;background:#0f2418;"
                  f"color:#86efac'>{_esc(msg)}</div>")
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>crypto-ma-alert · {label}</title>{DASH_CSS}{ADMIN_CSS}</head>"
        f"<body><div class='wrap'><h1>crypto-ma-alert 管理面板</h1>"
        f"{strip_html}{_tabs_html(tab, token)}{banner}{body_html}"
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
        payload = get_payload(deps)
        strip = _status_strip(payload)
        if tab == "overview":
            body = _overview_body(deps, payload)
        elif tab == "params":
            body = render_params(deps, cfg, payload, btc_price(deps))
        elif tab == "switch":
            body = render_switch(deps, cfg)
        else:
            body = render_records(deps)
        resp = _page(token, tab, strip, body, msg)
        # Cookie 必须设在返回的 response 上：Starlette 在函数返回新 Response 时会丢弃
        # 注入的 response 上写的头（所以这里不能用依赖注入的 response）。
        if k:
            resp.set_cookie("cma_admin", token, httponly=True, max_age=30 * 86400)
        return resp

    # ---------------- 参数 ----------------
    @router.post("/admin/params")
    def admin_params(request: Request, equity: str = Form(""), risk_pct: str = Form(""),
                     leverage: str = Form(""), stop_min: str = Form(""),
                     stop_max: str = Form(""), short_regime_ma: str = Form(""),
                     equity_mode: str = Form("follow"), equity_min: str = Form(""),
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
        })
        if errs:
            raise HTTPException(400, "参数校验失败：" + "；".join(errs))
        for key, val in values.items():
            deps.put(key, val)
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
        deps.put("hl_exec_mode", m)
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
        for key, val in vals.items():
            deps.put(key, val)
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
        return RedirectResponse(_back(token, "switch"), status_code=303)

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
