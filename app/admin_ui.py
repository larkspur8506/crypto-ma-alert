"""管理面板（合并版）：单一入口 ``/admin?k=<ADMIN_TOKEN>&tab=<tab>``。

四个 Tab（链接切换，服务端渲染，**无 CDN / 无框架 / 无外部资源**，手机弱网可开；
唯一的原生 JS 是「参数」页资金口径的单选钮显隐，无 JS 时两个字段都显示）：

- ``overview`` 总览 —— 直接复用 :func:`app.hl.views._render_body`（不重写仓位/盈亏曲线区块）；
  末尾接「API 有效期」状态卡（地址/到期日/剩余/状态 + 到期前自动清仓开关状态）
- ``params``   参数 —— 策略 / 执行与推送 / 资金 / API 有效期 四组，显示来源（DB·env·默认）与影响预览；
  末尾另有「更新 API（续期）」折叠表单（粘新私钥，POST /admin/hl/agent/renew）
- ``switch``   开关 —— 执行模式三选一（切实盘二次确认 + 校验凭据）、策略与保护（做多/做空/到期自动清仓）、
  推送开关、kill switch
- ``records``  记录 —— hl_orders / hl_fills / signals / alert_logs / hl_events 五块
  （「信号」2026-10-18 从总览迁入，复用 hl/views._signals_html）

兼容（见 docs/ADMIN_MERGE_PLAN.md 第 2 节「保留」）：
- ``/hl``、``/admin/rules``、``/admin/logs`` 三个旧 GET → 302 到新位置（书签/已发消息里的链接不断）
- ``/admin/rules`` 的 POST 仍在 ``app.main``（继续写 DB）
- ``/api/hl/*`` 四个 JSON 接口、``/c/{pid}`` 确认页完全不动

面板只读交易逻辑：不改 ``app/strategy.py``、``app/hl/executor.py``、``app/hl/exchange.py``。
所有依赖走 :class:`AdminDeps` 注入，便于单测用假实现。
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import io
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app import engine, posfields, sizing
from app.db import DB
from app.hl import dd as hl_dd
from app.hl import funding as hl_funding
from app.hl.pnl import QUOTE_FEE_TOKENS   # M3：费用未折算/老数据缺手续费的判据同源
from app.hl.views import (DASH_CSS, DEFAULT_ROWS, ROW_CHOICES, ROWS_COOKIE, _FILL_BS,
                        _ST_TXT, _agent_badge_html, _disp, _fee_quoted, _fmt_sz, _kv,
                        _render_body, _stuck_badge_html, normalize_span,
                        _signals_html, normalize_rows, status_payload)
from app.ui_theme import (ICON_LINKS, THEME_COOKIE, DEFAULT_THEME, H1_BLOCK, PRODUCT_NAME,
                          PRODUCT_SUBTITLE, THEMES, normalize_theme, theme_attr,
                          theme_switch_html)

logger = logging.getLogger(__name__)

# 提醒日志（alert_logs.kind）→ 中文。以前直接显示英文原文（heartbeat_local /
# exec_alert_fills），和「执行器事件」那张表的中文列不一致、看不懂（用户 2026-10-15 反馈）。
_ALERT_KIND_TXT = {
    "entry": "开仓提醒",
    "exit": "离场提醒",
    "data_error": "数据源异常",
    "data_error_pending": "数据源异常（观察中）",
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

# 记录页「主页」要隐藏的例行行（P1 降噪，用户要求：主页只看真实提醒/异常）。
# 心跳两类（每小时推送 + 本地静默）会淹没真实提醒；子页 /admin/list/alerts 不过滤
# （全部可看），所以这里只影响 render_records 的取数切片，不碰 DB 层。
_ALERT_NOISE_KINDS = ("heartbeat", "heartbeat_local")

# 执行器事件里同样属例行行的：心跳；「对账一致」是每小时一次的正常回执（reconcile
# kind 下其余消息都是异常/升级/观察期，必须照常显示），只按消息原文精确匹配。
_RECONCILE_OK_MSG = "对账一致"

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
            + "<span class='muted'>&nbsp;（历史类长表；盈亏曲线不受影响）</span></div>")


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
           "error": "错误", "heartbeat": "心跳", "dd": "回撤"}

# 推送开关：key -> (中文标签, 说明, 缺省值)
# report_push / fill_push 是新键（首次保存即落 DB）。
NOTIFY_KEYS: Tuple[Tuple[str, str, str, str], ...] = (
    ("heartbeat_push", "每日心跳推送", "体检报告；关掉后只落库不推送，看守脚本仍判活", "0"),
    ("report_push", "日报 / 周报推送", "周报周一发送；关掉后只落库（kind=weekly_local）", "1"),
    ("fill_push", "成交回报推送", "开 = 开仓/平仓成交都推一条；关 = 不推（止损被触发等异常提醒不受影响）", "1"),
)

# 开关页「策略与保护」：key -> (中文标签, 说明, 默认值, env_key)
# 2026-10-19 从参数页迁出（方向开关被「保存全部参数」整份写 0 的事故）。
STRATEGY_KEYS: Tuple[Tuple[str, str, str, bool, str], ...] = (
    ("enable_long", "允许做多", "关掉后不再发做多信号（已有仓位不受影响，止损照常）",
     True, "ENABLE_LONG"),
    ("enable_shorts", "允许做空", "关掉后不再发做空信号（已有仓位不受影响，止损照常）",
     True, "ENABLE_SHORTS"),
    # 空单风险减半（feat/short-risk-half）：与「允许做空」相邻 —— 它们都是「怎么对待
    # 空单腿」的开关。倍率 0.5 写死在 app/sizing.py:SHORT_HALF_SCALE，这里不给输入框
    # （可调系数会变成回测里的又一个拟合自由度）。只影响开仓规模，不动止损/离场。
    ("short_risk_half", "空单风险减半",
     "开启后做空每笔风险按 0.5 倍计算（风险U、名义、张数一起减半），多单不变；"
     "只影响开仓，不影响已持仓（止损位、离场规则照旧）。"
     "关 = 空单与多单一样按「单笔风险」全额下单",
     False, "SHORT_RISK_HALF"),
    ("agent_expiry_cleanup", "到期前自动清仓（≤3 天）",
     "开 = API 快到期（剩余 3 天以内）时自动撤单平仓，避免到期后想平也平不了；"
     "关 = 只提醒不清仓（需要自己盯）", True, "AGENT_EXPIRY_CLEANUP"),
    # PR-3：净额口径切换。默认关 —— 先让「本地 vs 流水」的差异被记录和核对，
    # 再决定要不要让面板改读流水口径（net_pnl 两态都写，随时可切回）。
    ("hl_pnl_from_fills", "记录页净额改用流水口径",
     "开 = 记录页「净额（流水）」列显示按真实成交聚合的净额；"
     "关 = 不显示该列（面板仍用交易所已实现口径）。两种状态下净额都会照常计算入库",
     False, "HL_PNL_FROM_FILLS"),
    # 2026-10-22 事故修复：交易所挂单止损成交后本地不关账，旧逻辑下一轮对账会误判
    # mismatch 并 kill。默认开；关掉 = 回到旧行为（只适合排查对账逻辑时临时用）。
    ("hl_fill_close", "交易所已平仓时自动关账",
     "开 = 对账发现「本地有仓、交易所无仓」时，先用已同步的平仓成交（reduce-only、"
     "开仓后、数量足额）证明仓位真的已平 → 按成交价关账并把止损单落终态，不暂停交易；"
     "证据不成立才按旧行为报对账异常并停手。关 = 两条路径一起回到旧行为："
     "对账不再按成交证据关账，且 mismatch 行的止损离场也照旧让出接管权"
     "（只能人工补录，别指望 4h 收盘的 on_exit 代关）",
     True, "HL_FILL_CLOSE"),
    # 2026-10-22 第二轮（对账失败自动消化）：
    # ① 宽限期 —— 「交易所刚平、本地流水还没同步」是最常见的时滞，用 kill 打断自动交易
    #    并不能让它更快对上账，只会把人叫起来看一个会自动消失的问题。
    ("hl_close_grace", "对账证据不足时先进宽限期",
     "开 = 对账发现「交易所已无该仓、但本轮拿不到成交证据」时先挂起"
     "（状态「平仓待核对」、每轮自动重试并定点回捞成交、不暂停交易），"
     "超过宽限时间（参数页可改，默认 15 分钟）才升级成对账异常并停手；"
     "宽限期内该标的禁止开新仓。关 = 第一轮没证据就按旧行为报异常并停手",
     True, "HL_CLOSE_GRACE"),
    ("hl_kill_autoclear", "对账异常可自动恢复",
     "开 = 仅当停手是「对账异常」造成的（人工触发 / 当日亏损熔断 / 数量对账 / "
     "到期保护除外），且连续 2 轮对账干净（无本地持仓、无挂单、挂单接口可信）时，"
     "自动解除停手并推一条提醒；关 = 永远等人工解除",
     True, "HL_KILL_AUTOCLEAR"),
    # 回撤熔断（DD Guard）：判据是**策略口径**峰谷回撤（剔除出入金的 TWR 链）。
    # 与日内 5% 那条线互补：日内线每天重置，抓不到「每天亏 4%」的温水煮青蛙。
    # 熔断只停开新仓、不平仓，且 origin='dd' 永不自解（人工解）。
    ("hl_dd_guard", "回撤熔断（策略口径峰谷回撤）",
     "开 = 策略回撤达告警线推一条提醒、达熔断线停止开新仓（持仓按既有规则离场，"
     "不强制平仓，需人工到本页解除）；阈值按当前「单笔风险」档位自动选"
     "（1.5% 档 25/30、2.5% 档 30/40、3% 档 40/50），"
     "要覆盖到参数页「风控阈值」组填。"
     "关 = 只保留日内 5% 那条线", True, "HL_DD_GUARD"),
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
    # 「为什么没开仓」（PR-5 /why）：可注入的行数据源，便于测试固定输出（不触网）。
    # 缺省时总览会走 app.why.build_rows（现取 4h K 线、只读）。
    why_rows_fn: Optional[Callable[[], Any]] = None

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
    """DB 里 set_config 走 json.dumps，字符串值会带引号，先剥掉再判。

    truthy 集合与 :func:`_flag` 逐字一致（含 'y'/'t'）——渲染与写入两套口径
    不一致时，DB 存 'y' 的开关会显示「关」但实际生效为开（审查抓过的偏差）。
    """
    return str(v if v is not None else "").strip().strip('"\'').lower() in (
        "1", "true", "yes", "on", "y", "t")


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
        p = p if isinstance(p, dict) else {}
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] status_payload 失败：%s", e)
        p = {}
    # API 有效期（P3 R3）：status_payload 里已按 hl_agent_state DB 缓存填了 agent；
    # AdminDeps 注入的 agent_state_fn 是同一份缓存（render_agent_card 也读它）——
    # 可注入时用它覆盖，保证状态条徽章与「API 有效期」details 永远是同一个数。
    if deps.agent_state_fn is not None:
        try:
            st = deps.agent_state_fn()
            if isinstance(st, dict):
                p["agent"] = st
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[admin] 读 agent_state 失败（徽章按 status_payload 兜底）：%s", e)
    return p


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


def _risk_pair(equity: float, rp: float, half: bool) -> Tuple[float, float]:
    """(多单风险U, 空单风险U)。开关关闭时两者相同 —— 唯一的折算入口是 sizing。"""
    return equity * rp, equity * sizing.effective_risk_pct(rp, -1, half)


def _risk_phrase(risk_u: float, risk_u_short: float, half: bool) -> str:
    """「单笔风险」的白话：开关打开时**必须**区分多空，否则面板在骗人。

    关闭时保持原来的一句话（逐字不变）；打开时两个数各写一次，不重复。
    """
    if half:
        return f"单笔风险 多单 {risk_u:,.2f}U · 空单 {risk_u_short:,.2f}U"
    return f"单笔风险 {risk_u:,.2f}U"


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
    # 空单风险减半（short_risk_half）开着时，空单的单笔风险只有一半 —— 预览行
    # 必须把两个方向分开写，否则面板会告诉用户「单笔风险 20U」，而他真去开空
    # 只有 10U（或反过来照着 20U 加仓 = 风险翻倍）。
    short_half = bool(getattr(cfg, "short_risk_half", False))
    risk_u, risk_u_short = _risk_pair(eq, rp, short_half)
    notional = risk_u / sd
    notional_short = (risk_u_short / sd) if risk_u_short > 0 else 0.0
    return {"price": price, "equity": eq, "risk_pct": rp, "leverage": lev,
            "stop_dist": sd, "risk_u": risk_u, "notional": notional,
            "margin": notional / lev, "liq": sizing.liq_price(price, lev, 1),
            # 空单口径（开关关闭时与多单完全相同）
            "short_half": short_half, "risk_u_short": risk_u_short,
            "notional_short": notional_short, "margin_short": notional_short / lev,
            # 做空参考（同参数镜像）：预览页给出两个方向的强平价，防误读
            "liq_short": sizing.liq_price(price, lev, -1)}


def _impact_line(d: Optional[dict]) -> str:
    if not d:
        return "影响预览：无法取价（等只读接口恢复后再算）"
    if d.get("short_half"):
        # 多空分开写：风险/名义/保证金各自一组，同一数字不重复
        return (f"按 BTC 现价 {d['price']:,.0f}："
                f"{_risk_phrase(d['risk_u'], d['risk_u_short'], True)} · "
                f"名义 多 {d['notional']:,.0f}U/空 {d['notional_short']:,.0f}U · "
                f"保证金 多 {d['margin']:,.0f}U/空 {d['margin_short']:,.0f}U · "
                f"强平 多{d['liq']:,.0f}/空{d['liq_short']:,.0f}")
    return (f"按 BTC 现价 {d['price']:,.0f}："
            f"{_risk_phrase(d['risk_u'], d['risk_u_short'], False)} · "
            f"名义 {d['notional']:,.0f}U · 保证金 {d['margin']:,.0f}U · "
            f"强平 多{d['liq']:,.0f}/空{d['liq_short']:,.0f}")


# ---------------------------------------------------------------- 参数 Tab
# (db_key, 分组, 标签, env_key, 默认值, 类型)
# 2026-10-19 用户拍板：方向/保护类开关（enable_long / enable_shorts /
# agent_expiry_cleanup）**撤出参数页**，迁到「开关」页的「策略与保护」卡。
# 事故原因：这些键原来混在「保存全部参数」这一张 form 里，未勾选 = 提交 0，
# 一次提交就把线上的方向开关静默关掉（2026-10-18/19 线上真出过）。参数页只留
# 「填数字/选口径」的字段，开关全部归开关页。
PARAM_FIELDS: Tuple[Tuple[str, str, str, str, Any, str], ...] = (
    ("stop_min", "策略", "止损距离下限", "STOP_MIN", 0.02, "float"),
    ("stop_max", "策略", "止损距离上限", "STOP_MAX", 0.08, "float"),
    ("short_regime_ma", "策略", "做空趋势过滤均线（0=关闭，不建议）",
     "SHORT_REGIME_MA", 200, "int"),
    ("risk_pct", "执行与推送", "单笔风险", "RISK_PCT", 0.02, "float"),
    ("leverage", "执行与推送", "杠杆（逐仓）", "LEVERAGE", 5.0, "float"),
    # PR-4：churn 熔断阈值。放在参数页而不是「开关」页 —— 它是个数值（0 = 关），
    # 和 risk_pct 一样属于「策略参数」，改完下一轮 tick 生效。
    ("hl_churn_max", "执行与推送", "撤单/重挂熔断阈值（10 分钟内次数，0=关闭）",
     "HL_CHURN_MAX", 5, "int"),
    # 宽限期（对账失败自动消化）：存**秒**（配置/装配都是秒），渲染时在旁边提示分钟，
    # 免得「15」被当成 15 秒填进去（那样等于关掉宽限）。范围 300–3600。
    ("hl_close_grace_s", "执行与推送", "平仓证据宽限时长（秒，300–3600，默认 900 = 15 分钟）",
     "HL_CLOSE_GRACE_S", 900.0, "float"),
    # 数据源告警观察期（feat/feed-alert-defer）：失败先落库观察、持续未恢复才推送。
    # 数值参数 → 归参数页（与 hl_churn_max 同类），0 = 立即推（旧行为）是合法值。
    ("feed_alert_delay_s", "执行与推送", "数据源异常提醒延迟（秒，0=立即）",
     "FEED_ALERT_DELAY_S", 1800.0, "float"),
    ("equity_mode", "资金", "资金口径", "HL_EQUITY_MODE", "follow", "mode"),
    ("equity", "资金", "账户权益基数 U（固定模式用）", "ACCOUNT_EQUITY", 1000.0, "float"),
    ("equity_min", "资金", "资金下限 U（跟随模式用）", "", 20.0, "float"),
    ("hl_agent_valid_until_ms", "API 有效期", "手工兜底的有效期（毫秒时间戳，可留空）",
     "HL_AGENT_VALID_UNTIL_MS", "", "text"),
)
_GROUPS: Tuple[Tuple[str, str], ...] = (
    ("策略", "决定什么信号会被发出来；方向开关已迁到「开关」页"),
    ("执行与推送", "决定下多大、占多少保证金（每日心跳推送在「开关」页）"),
    # 回撤阈值 2026-10-23 从「开关」页迁来（分类：数值型保护阈值归参数页，与
    # hl_churn_max / hl_close_grace_s 同类；布尔开关才归「开关」页）。
    # 放这里的原因：它的默认值**由 risk_pct 档位联动**（1.5% 档 25/30、2.5% 档
    # 30/40、3% 档 40/50），改「单笔风险」时两条线自动跟着走 —— 属于「参数」的心智。
    ("风控阈值", "回撤熔断线等保护阈值；留空 = 按「单笔风险」档位自动"),
    ("资金", "下单基数怎么算"),
    ("API 有效期", "接口查不到有效期时的手工兜底；到期前要不要自动清仓在「开关」页"),
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
    if kind == "int":
        return f"{int(_num(raw, _num(default)))}"
    if kind in ("str", "mode"):
        return str(raw if raw is not None else default)
    if kind == "text":
        return str(raw if raw is not None else default)
    return f"{_num(raw, _num(default)):g}"


def _preview_hint(key: str, cfg: Any, base: float, live: float,
                  imp: Optional[dict], imp_max: Optional[dict]) -> str:
    """每项旁的一行小字：这个字段改动能带来什么变化（按当前值实算，不靠猜）。"""
    if key == "equity":
        eq_v = _num(getattr(cfg, "equity", 0))
        return f"仅「固定基数」模式用：基数 {eq_v:,.0f}U → " + _risk_phrase(
            *_risk_pair(eq_v, _num(getattr(cfg, "risk_pct", 0)),
                        bool(getattr(cfg, "short_risk_half", False))),
            bool(getattr(cfg, "short_risk_half", False)))
    if key == "equity_mode":
        mode = str(getattr(cfg, "equity_mode", "follow") or "follow")
        if mode == "fixed":
            return "固定：按上面写的基数下单，不跟账户盈亏走"
        if live <= 0:
            return f"⚠️ 读不到实时权益（{live:,.2f}）→ 实际按固定基数 {base:,.0f}U 下单，不是跟随"
        return f"跟随：实时权益 {live:,.2f}U → 当前生效基数 {base:,.0f}U"
    if key == "equity_min":
        return f"仅「跟随实时权益」模式用：基数不低于 {_num(getattr(cfg, 'equity_min', 0)):,.0f}U"
    if key == "risk_pct":
        rp_v = _num(getattr(cfg, "risk_pct", 0))
        half = bool(getattr(cfg, "short_risk_half", False))
        return f"{rp_v * 100:.2f}% × 基数 {base:,.0f}U = " + \
            _risk_phrase(*_risk_pair(base, rp_v, half), half)
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
    if key == "short_regime_ma":
        n = int(_num(getattr(cfg, "short_regime_ma", 0)))
        return "0 = 关闭过滤（裸做空，不建议）" if n <= 0 else f"用 MA{n} 过滤空头信号"
    if key == "hl_agent_valid_until_ms":
        return ("仅当 HL 授权接口查不到同名 Hermes 时用：填毫秒时间戳"
                "（如 1830000000000）。留空 = 不改动当前值（要清空点下面的「清除兜底」）")
    if key == "hl_close_grace_s":
        try:
            sec = float(getattr(cfg, "hl_close_grace_s", 900.0) or 900.0)
        except (TypeError, ValueError):
            sec = 900.0
        return (f"{sec:.0f} 秒 ≈ {sec / 60:.0f} 分钟：交易所已平仓但拿不到成交证据时，"
                f"最多等这么久才判对账异常并停手（期间该标的禁止开新仓）。"
                f"调小 = 更快交人，调大 = 更多自动消化")
    if key == "feed_alert_delay_s":
        try:
            sec = float(getattr(cfg, "feed_alert_delay_s", 1800.0) or 0.0)
        except (TypeError, ValueError):
            sec = 1800.0
        if sec <= 0:
            return "0 = 立即推送（旧行为）：数据源一失败就发提醒"
        return (f"{sec:.0f} 秒 ≈ {sec / 60:.0f} 分钟：数据源失败先只记录不发提醒，"
                f"持续未恢复超过这么久才推送（滤掉交易所例行维护这类分钟级抖动）；"
                f"期间自己恢复就永远不打扰。填 0 恢复立即推")
    return ""


_TRUTHY = ("1", "true", "yes", "on", "y", "t")


def _flag(val: Any) -> int:
    """开关取值：'0'/'false'/'off'/空 = 关。

    不能只写 `1 if val else 0` —— 字符串 "0" 在 Python 里是真值，会把「关」写成「开」
    （fail-dangerous：脚本/表单发 0 反而打开推送）。
    """
    return 1 if str(val if val is not None else "").strip().lower() in _TRUTHY else 0


def _manual_kill(deps: Any, on: bool, reason: str = "") -> None:
    """面板按下的 kill 一律标注 origin='manual' —— 人工 kill **永不**被自动解除。

    为什么要显式传：`db.hl_set_kill` 的 origin 默认值虽然是 manual，但那是给「新增
    调用点忘了传」兜底的保守默认；面板这条路径是**明确的**人工动作，把意图写进参数里
    比依赖默认值可靠（将来默认值变了也不会把人工 kill 变成可自解的）。

    兼容注入的替身：`deps.hl_set_kill` 是老签名的两参数可调用对象时不带 origin
    （测试/脚本里的 lambda），退化成旧调用 —— 但绝不因为参数不匹配就把动作丢掉。
    """
    fn = deps.hl_set_kill
    if fn is None:
        return
    try:
        fn(on, reason, origin="manual")
    except TypeError:
        fn(on, reason)


def _kill_origin(deps: Any) -> str:
    """当前 kill 的来源（manual/reconcile/dd/daily_loss/size/expiry/auto）。读不到 → 空串。"""
    try:
        return str(deps.db.hl_kill_origin() or "").strip()
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] 读 kill 来源失败（按未知来源处理）：%s", e)
        return ""


def _reset_dd_peak(deps: Any, now: Optional[float] = None) -> tuple:
    """把回撤基线（peak_index）重置为**当前 index**；返回 (ok, 人话)。

    【v2·高2】这是两个**独立**动作，不要合并：
      - 「解除 DD 熔断」：必须顺带重置基线（否则下一轮 DD 仍是 31% → 立刻又熔断，
        「人工解」形同虚设）。触发点在 `/admin/switch/kill`（on=0 且 origin=='dd'），
        且必须在**同一个请求**里完成 —— 分两次点，中间那一轮 tick 会抢先置 kill。
      - 「重置回撤基线」按钮：只重置峰值，**不**解除 kill。自动重置等于绕过熔断，
        所以这条路必须人显式点。
    """
    try:
        st = hl_dd.load_state(deps.db)
    except Exception as e:                                        # noqa: BLE001
        return False, f"读回撤状态失败：{type(e).__name__}: {e}"
    if st is None:
        return False, "还没有回撤锚点（执行器还没算过一轮），无需重置"
    try:
        new = hl_dd.reset_peak(deps.db, now=now)
    except Exception as e:                                        # noqa: BLE001
        return False, f"重置回撤基线失败：{type(e).__name__}: {e}"
    if new is None:
        return False, "当前回撤指数不可用（权益可能已亏光），基线未重置 —— 请人工核对交易所"
    return True, (f"回撤基线已重置：当前指数 {new['index']:.2f}，"
                  f"回撤从 0% 重新计量")


def _num_field(key: str, label: str, value: str, hint: str, src: str, step: str = "",
               attrs: str = "") -> str:
    st = f" step='{step}'" if step else ""
    at = f" {attrs}" if attrs else ""
    return (f"<div class='field'{at}><div class='lbl'>{_esc(label)}"
            f"<span class='src'>&nbsp;来源 {src} · 当前 {_esc(value)}</span></div>"
            f"<input type='text' inputmode='decimal' required name='{_esc(key)}' "
            f"value='{_esc(value)}'{st}>"
            f"<div class='hint'>{hint}</div></div>")


def _mode_field(key: str, label: str, cur: str, hint: str, src: str) -> str:
    """资金口径：单选钮组（两个选项，服务端渲染 checked）。

    以前是 <select>，和下面两个数字字段混在一起，「改口径」这件事不显眼；
    改成单选钮并置顶，配合 :func:`_equity_mode_js` 按模式显隐对应字段。
    """
    opts = "".join(
        f"<label class='rad'><input type='radio' name='{_esc(key)}' value='{v}' "
        f"{'checked' if v == cur else ''}> {_esc(t)}</label>"
        for v, t in (("follow", "跟随实时权益"), ("fixed", "固定基数")))
    return (f"<div class='field'><div class='lbl'>{_esc(label)}"
            f"<span class='src'>&nbsp;来源 {src} · 当前 {_esc(cur)}</span></div>"
            f"{opts}<div class='hint'>{hint}</div></div>")


def _equity_mode_js() -> str:
    """参数页资金组：按「资金口径」单选钮显隐 equity / equity_min（原生 JS，无框架）。

    约定：``data-mode`` 标出字段**只属于**哪个模式；选中值不同就 display:none。
    同时把隐藏字段的 input 设 ``disabled``（不随表单提交 → 服务端按「缺字段 =
    保持原值」处理）并同步 ``required`` —— 只 display:none 不够：required 留在
    隐藏字段上时，浏览器无法聚焦报错，整张表单会**静默提交失败**（审查高危）。
    脚本没跑起来（禁用 JS / 老浏览器）时两个字段都可见可提交 —— 每个字段的
    hint 里已写明它属于哪个模式，无 JS 的回退是可用的，只是少一层视觉过滤。
    """
    return (
        "<script>(function(){var f=function(){"
        "var r=document.querySelector(\"input[name='equity_mode']:checked\");"
        "if(!r)return;var m=r.value;"
        "var e=document.querySelectorAll('[data-mode]');"
        "for(var i=0;i<e.length;i++){"
        "var on=(e[i].getAttribute('data-mode')===m);"
        "e[i].style.display=on?'':'none';"
        "var inp=e[i].querySelector('input');"
        "if(inp){inp.disabled=!on;inp.required=on;}}};"
        "var g=document.querySelectorAll(\"input[name='equity_mode']\");"
        "for(var j=0;j<g.length;j++){g[j].addEventListener('change',f);}f();})();</script>")


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
            if kind == "text":
                # 自由文本（可留空）：手工兜底时间戳这类，不能 required
                cur = _cur_val(cfg, key, default, kind)
                o.append(f"<div class='field'><div class='lbl'>{_esc(label)}"
                         f"<span class='src'>&nbsp;来源 {src} · 当前 {_esc(cur or '空')}</span></div>"
                         f"<input type='text' inputmode='numeric' name='{_esc(key)}' "
                         f"value='{_esc(cur)}'>"
                         f"<div class='hint'>{hint}</div></div>")
            elif kind == "mode":
                # 资金口径：单选钮（值班的人一眼看到「现在按哪个口径算钱」）
                o.append(_mode_field(key, label, _cur_val(cfg, key, default, kind), hint, src))
            else:
                # data-mode：字段只属于哪个资金口径（原生 JS 按选中值显隐；
                # 无 JS 时两个都显示，hint 里已写明各自属于哪个模式）
                attrs = {"equity": "data-mode='fixed'",
                         "equity_min": "data-mode='follow'"}.get(key, "")
                o.append(_num_field(key, label, _cur_val(cfg, key, default, kind),
                                    hint, src, step="1" if kind == "int" else "",
                                    attrs=attrs))
        # 「风控阈值」组没有 PARAM_FIELDS 条目：回撤阈值是**两个联动的可选数值**
        # （不是单个字段），输入框的语义也与普通数值项不同（留空 = 保持原值，
        # 显式 0 = 回到档位自动，而不是「不改动」），所以自己渲染。
        if group == "风控阈值":
            o.append(_dd_threshold_fields(cfg, raw))
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
    # 「更新 API（续期）」表单 2026-10-19 从总览状态卡里拆出来单独放这里：
    # 状态（到期日/剩余/状态词）是「当下」，回总览末尾；写私钥是运维设置，留参数页。
    # 区块自己兜底，渲染失败不弄挂参数页。
    try:
        o.append(render_agent_renew_block(deps))
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] 更新 API 区块渲染失败：%s", e)
    # 资金口径显隐脚本（原生 JS，无框架、无外部资源）；必须在字段之后执行。
    o.append(_equity_mode_js())
    return "".join(o)


def _parse_params(form: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """校验 + 转换。全部通过才返回可写值；有任何错就返回错误列表且**不写库**。"""
    errs: List[str] = []
    out: Dict[str, Any] = {}

    def take_float(key: str, name: str, lo: float, hi: Optional[float],
                   strict: bool = True) -> Optional[float]:
        """strict=True 要求 v > lo（权益/风险率必须为正）；False 允许 v == lo（资金下限可为 0）。

        **键缺失或空值 = 保持原值**（2026-10-19 审查高危回归）：资金组两个字段会随
        模式被 JS disabled（disabled 的 input 不提交）；且 Starlette 对 Optional[str]
        的空串同样给 None —— 「空 = 不改动」与手工兜底字段同口径，无法区分
        「用户清空」与「没提交」，所以两者都不写库、不报错。
        """
        if form.get(key) is None:
            return None
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
    # churn 熔断阈值（PR-4）：整数，0 = 关闭熔断。**缺字段/空 = 保持原值**——
    # 与同页其它字段一致（老书签/curl 不带这个键时不能把它刷成 0=关掉保护）。
    ch_raw = form.get("hl_churn_max")
    if ch_raw is not None:
        ch_txt = str(ch_raw or "").strip()
        if ch_txt:
            try:
                ch_v = int(float(ch_txt.replace(",", "")))
            except ValueError:
                errs.append("撤单/重挂熔断阈值不是数字")
                ch_v = None
            if ch_v is not None:
                lo, hi = _R["hl_churn_max"]
                if not (lo <= ch_v <= hi):
                    errs.append(f"撤单/重挂熔断阈值必须在 {lo:g}~{hi:g} 之间（0=关闭）")
                else:
                    out["hl_churn_max"] = ch_v

    # 平仓证据宽限时长（秒）：与 hl_churn_max 同一套语义 —— **缺字段/空 = 保持原值**
    # （老书签/curl 不带这个键时不能把它刷成 0 = 第一轮没证据就 kill，那正是要修的行为）。
    gs_raw = form.get("hl_close_grace_s")
    if gs_raw is not None:
        gs_txt = str(gs_raw or "").strip()
        if gs_txt:
            try:
                gs_v = float(gs_txt.replace(",", ""))
            except ValueError:
                errs.append("平仓证据宽限时长不是数字")
                gs_v = None
            if gs_v is not None:
                lo, hi = _R["hl_close_grace_s"]
                if not (lo <= gs_v <= hi):
                    errs.append(f"平仓证据宽限时长必须在 {lo:.0f}~{hi:.0f} 秒之间"
                                f"（{lo / 60:.0f}~{hi / 60:.0f} 分钟）")
                else:
                    out["hl_close_grace_s"] = gs_v

    # 数据源告警观察期（秒）：与上面同语义 —— 缺字段/空 = 保持原值（老书签/curl
    # 不带这个键时不能把它刷成别的值）；显式 0 = 立即推（旧行为），是合法值。
    fd_raw = form.get("feed_alert_delay_s")
    if fd_raw is not None:
        fd_txt = str(fd_raw or "").strip()
        if fd_txt:
            try:
                fd_v = float(fd_txt.replace(",", ""))
            except ValueError:
                errs.append("数据源异常提醒延迟不是数字")
                fd_v = None
            if fd_v is not None:
                lo, hi = _R["feed_alert_delay_s"]
                if not (lo <= fd_v <= hi):
                    errs.append(f"数据源异常提醒延迟必须在 {lo:g}~{hi:g} 秒之间（0=立即推送）")
                else:
                    out["feed_alert_delay_s"] = fd_v

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

    # 资金口径：**缺字段 = 保持原值**（2026-10-19 审查高危回归）。旧实现 Form("follow")
    # 会把老书签/curl 的缺字段请求静默刷成 follow，直接改变下单基数口径。
    mode_raw = form.get("equity_mode")
    if mode_raw is None:
        pass                                   # 缺字段：不写库
    else:
        mode = str(mode_raw).strip().lower()
        if mode not in ("follow", "fixed"):
            errs.append("资金口径只能是 follow / fixed")
        else:
            out["equity_mode"] = mode

    if sm is not None and sx is not None and sm > sx:
        errs.append(f"止损下限 {sm:g} 不能大于上限 {sx:g}")

    # 方向/保护类开关（enable_long / enable_shorts / agent_expiry_cleanup）**不在
    # 参数 POST 的处理范围**（2026-10-19 迁到 /admin/switch/strategy）。这里既不再
    # 读取也不再有「缺省 = 0」的兜底 —— 参数提交永远碰不到这三个键。
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


def _dd_threshold_fields(cfg: Any, raw: Dict[str, Any]) -> str:
    """回撤告警线/熔断线两个**可选**输入框（留空 = 按 risk_pct 档位自动）。

    输入框里只回填「覆盖值」，不回填自动推导出来的数字：把自动算出的 0.25 填进框里，
    用户下次一保存就变成「显式覆盖 0.25」，以后改 risk_pct 线就不跟着走了 —— 那正是
    §3.4 要避免的坑。当前**生效**值写在提示文字里，供对照。
    """
    risk = _num(getattr(cfg, "risk_pct", 0.02), 0.02)
    th = hl_dd.thresholds(risk, _num(getattr(cfg, "hl_dd_warn_pct", 0.0), 0.0),
                          _num(getattr(cfg, "hl_dd_kill_pct", 0.0), 0.0))

    def _val(key: str) -> str:
        v = _num(getattr(cfg, key, 0.0), 0.0)
        return "" if v <= 0 else f"{v:g}"

    hint = (f"当前生效：告警 {th['warn'] * 100:g}% / 熔断 {th['kill'] * 100:g}%"
            f"（按单笔风险 {risk * 100:g}% 档位自动）。留空 = 保持原值；"
            f"填 0 = 回到档位自动；改「单笔风险」时两条线会自动跟着走")
    # 【低5】常驻小字（不是只在出错时才出现）：用户填完提交后如果被拒收，输入框里
    # 还回显着他填的值，而 DB 里跑的是旧值 —— 不说清「你的值没生效」就等于静默失效。
    # 放在这里而不是只靠 flash 提示：flash 只出现一次，事后回看页面看不出差别。
    hint += (f"。你填的值若自相矛盾会被拒收、不生效（须满足 告警线 < 熔断线 ≤ "
             f"{hl_dd.MAX_KILL * 100:g}%）")
    if th["rejected"]:
        hint += ("。⚠️ 当前 DB 里的覆盖值自相矛盾，你填的值没生效 —— "
                 "已按单笔风险档位的默认线执行")
    return ("<div class='field'><div class='lbl'>回撤阈值覆盖（可选，小数，如 0.25 = 25%）"
            f"<span class='src'>&nbsp;来源 {_source(raw, 'hl_dd_warn_pct', 'HL_DD_WARN_PCT')}"
            f" / {_source(raw, 'hl_dd_kill_pct', 'HL_DD_KILL_PCT')}</span></div>"
            "<label class='chk'>告警线 <input type='text' inputmode='decimal' "
            f"name='hl_dd_warn_pct' value='{_esc(_val('hl_dd_warn_pct'))}' "
            "placeholder='自动'></label>"
            "<label class='chk'>熔断线 <input type='text' inputmode='decimal' "
            f"name='hl_dd_kill_pct' value='{_esc(_val('hl_dd_kill_pct'))}' "
            "placeholder='自动'></label>"
            f"<div class='hint'>{_esc(hint)}</div></div>")


def _parse_dd_thresholds(warn_raw: Any, kill_raw: Any, cfg: Any,
                         risk_pct: Any = None) -> tuple:
    """解析两个可选回撤阈值 → ({key: value}, 提示文案)。

    规则（§6）：
      - 字段缺失 / 空白 → 不写库（保持原值），**绝不**把空串写成 0；
      - 非数字 / 越界 / 与另一条线自相矛盾 → **两个都拒收**（保留原值），并给一句人话说明。
    范围（0 或 5%~95%）与「warn < kill」全部由 `app/hl/dd.py` 判（`pct_ok` /
    `thresholds`，含档位联动）—— 这里不再抄一遍区间（§6 低4）。

    ``risk_pct``：**本次提交后**的单笔风险（2026-10-23 加）。档位联动时判据依赖
    risk_pct，而装配层 `Config.apply_db` 用的是**同一次提交后**的 risk_pct ——
    两层必须判在同一个档位上，否则会出现「面板放行、装配拒收」：用户同时改
    risk_pct 和回撤阈值、且只填了其中一条线时，留空那条按档位补出来的值，
    在旧档位下成立、在新档位下不成立。不传 risk_pct 时退回 cfg 里的当前值
    （= 同一次提交没改 risk_pct 的情形，与装配层一致）。
    """
    if warn_raw is None and kill_raw is None:
        return {}, ""
    cur = {"hl_dd_warn_pct": _num(getattr(cfg, "hl_dd_warn_pct", 0.0), 0.0),
           "hl_dd_kill_pct": _num(getattr(cfg, "hl_dd_kill_pct", 0.0), 0.0)}
    out: Dict[str, float] = {}
    range_txt = f"{hl_dd.MIN_PCT * 100:g}%~{hl_dd.MAX_KILL * 100:g}%"
    for key, raw in (("hl_dd_warn_pct", warn_raw), ("hl_dd_kill_pct", kill_raw)):
        if raw is None:
            continue
        s = str(raw).strip()
        if s == "":
            continue                     # 留空 = 保持原值（不是 0）
        try:
            v = float(s)
        except (TypeError, ValueError):
            return {}, "回撤阈值被拒收（不是数字），两条都保留原值"
        if v != v:                       # NaN
            return {}, "回撤阈值被拒收（不是数字），两条都保留原值"
        if not hl_dd.pct_ok(v):
            return {}, f"回撤阈值被拒收（须在 {range_txt} 之间，或留空按档位自动），两条都保留原值"
        out[key] = v
    if not out:
        return {}, ""
    # 档位判据必须与装配层 apply_db 一致：都用**本次提交后**的 risk_pct。
    risk = _num(risk_pct, None)
    if risk is None:
        risk = _num(getattr(cfg, "risk_pct", 0.02), 0.02)
    if hl_dd.thresholds(risk, out.get("hl_dd_warn_pct", cur["hl_dd_warn_pct"]),
                        out.get("hl_dd_kill_pct", cur["hl_dd_kill_pct"]))["rejected"]:
        # 【低5】文案必须让用户知道「你填的值没生效」，不只是「被拒收」：DB 里保留的
        # 是旧值（apply_db 的拒收语义不改），面板输入框却还回显着刚填的数字 ——
        # 不说清「没生效」用户会以为自己已经改了线。
        return {}, (f"回撤阈值没生效（自相矛盾：告警线必须小于熔断线、熔断线 ≤"
                    f"{hl_dd.MAX_KILL * 100:g}%），两条都保留原值（当前生效值见输入框下方）")
    return out, ""


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

    # ---- 策略与保护（2026-10-19 从参数页迁入） ----
    # 这三个键以前在参数页的「保存全部参数」那张大表单里：未勾选 = 提交 0，
    # 一次「保存全部参数」就能把线上方向开关静默关掉（2026-10-18/19 出过事故）。
    # 现在独立成一张小卡 + 独立 POST，且用 hidden+checkbox 双字段：
    #   未勾选 → 只提交 hidden 的 0 → 关（必须能关掉）
    #   勾选   → 提交 0 再提交 1 → 后端取最后一个 = 开
    #   字段完全缺失（脚本/旧书签）→ 后端保持原值，不写库
    o.append("<div class='card'><div class='sec'>策略与保护</div>"
             "<div class='hint'>决定发不发哪个方向、API 快到期时要不要自动清仓，"
             "以及对账拿不到成交证据时先宽限还是立刻停手。"
             "改完立刻生效（下一轮 tick），不用重启。</div>"
             "<form method='post' action='/admin/switch/strategy'>"
             f"<input type='hidden' name='k' value='{_esc(_token_of(deps))}'>")
    for key, label, hint, dflt, env_key in STRATEGY_KEYS:
        cur = getattr(cfg, key, dflt)
        on = _b(cur) if not isinstance(cur, bool) else cur
        o.append(_bool_field(key, label, bool(on), hint, _source(raw, key, env_key)))
    # 回撤阈值的**两个输入框** 2026-10-23 迁到参数页「风控阈值」组（数值型保护
    # 阈值归参数页，与 hl_churn_max / hl_close_grace_s 同类；这里只留布尔开关）。
    # 迁移前它们就在这张卡里 —— 那是上一轮做 DD Guard 时「顺手放进已有卡片」的
    # 连带结果，不是分类上的选择。
    o.append("<button type='submit'>保存策略与保护</button></form></div>")

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
             "解除（恢复正常）</button></form>")
    # 【v2·高2】「重置回撤基线」独立入口：只把 peak_index 归到当前 index（回撤从此刻
    # 重新计量），**不**解除 kill。为什么要单开一个按钮：用户可能只想重新起算、或者
    # kill 已经因为别的原因停着（对账/当日亏损），这时不该被迫先解 kill 再重置。
    # 反过来「解除 DD 熔断」会顺带重置基线（§4.6），那条路径在 POST 里做。
    o.append("<form method='post' action='/admin/switch/dd_reset'>"
             f"<input type='hidden' name='k' value='{_esc(_token_of(deps))}'>"
             "<button class='gray' type='submit'>重置回撤基线</button>"
             "<div class='hint'>把峰值（peak_index）改成当前指数：回撤从 0% 重新计量，"
             "下次熔断要再跌一个熔断线。<b>不解除</b> kill；也不作用于账户权益回撤口径。</div>"
             "</form></div>")
    return "".join(o)



# ---------------------------------------------------------------- 记录 Tab
def _all_link(slug: str, token: str) -> str:
    """记录页各块的「全部 ›」入口（feat/list-subpages）：跳到该表的独立子页。

    零 JS 的普通链接；k 原样带上（子页与其余 admin 路由同法鉴权）。
    """
    q = quote(str(token or ""), safe="")
    return (f"<a class='alllink' href='/admin/list/{slug}?k={q}' "
            f"title='查看该表全部记录（可筛选、可导出 CSV）'>全部 ›</a>")


def _ledger_row_html(r: Dict[str, Any], bal: Optional[float]) -> str:
    """资金流水一行（主记录页与 /admin/list/ledger 子页共用同一份渲染，口径不二份）。"""
    amt = _opt_num(r.get("amount_usd"))
    amt_cls = "pos" if (amt or 0) > 0 else ("neg" if (amt or 0) < 0 else "")
    kind = hl_funding.kind_text(r.get("kind"))
    pid = r.get("position_id")
    link = f"#{int(pid)}" if pid is not None else "—"
    # 备注：coin/数量/hash 前 10 位；口径例外如实标注（不许静默）。
    # M3：fee_missing 按成因分两种文案 —— fee_token 非计价币（如 ARB）时
    # _fee_quote 按设计返回 None，不是「老数据」，得说明真相。
    notes: List[str] = []
    if r.get("coin"):
        notes.append(str(r["coin"]))
    if r.get("fee_missing"):
        fee_tok = str(r.get("fee_token") or "").strip().upper()
        if fee_tok and fee_tok not in QUOTE_FEE_TOKENS:
            notes.append(f"费用未折算（非计价币 {_esc(fee_tok)}）")
        else:
            notes.append("老数据缺手续费")
    if pid is None:
        notes.append("无持仓期资金费" if r.get("kind") == "funding" else "未归属")
    ref = str(r.get("ref") or "")
    tail = ref[-14:] if ref.startswith("fund:") else ref[:10]
    # 币名去重（P1 词面修复）：fund 组合键 ref 形如 "t(ms)|BTC|+0.009152"，截尾后
    # 备注会渲染成 "BTC · |BTC|+0.009152" —— 币名已在备注开头，把尾部的 "|币名"
    # 段剥掉，只留数量。hash 类 ref 不含 "|币名|"，原样保留。
    _coin_txt = str(r.get("coin") or "").strip()
    if tail and _coin_txt:
        if tail.startswith(f"|{_coin_txt}|"):
            tail = tail[len(_coin_txt) + 2:]
        elif tail.startswith(f"{_coin_txt}|"):
            tail = tail[len(_coin_txt) + 1:]
        elif f"|{_coin_txt}|" in tail:
            # 截尾落在组合键中段（带了一截时间戳）时同样剥掉前段，只留数量。
            tail = tail.split(f"|{_coin_txt}|", 1)[1]
    if tail and tail != "fund:":
        notes.append(_esc(tail))
    return (f"<tr><td class='num'>{_ts(r.get('ts'))}</td>"
            f"<td>{_esc(kind)}</td>"
            f"<td class='num {amt_cls}'>{('—' if amt is None else f'{amt:+,.2f}')}</td>"
            f"<td class='num'>{_fmt(bal)}</td>"
            f"<td class='num'>{_esc(link)}</td>"
            f"<td>{' · '.join(notes) or '—'}</td></tr>")


def _funds_ledger_html(deps: AdminDeps, n: int) -> str:
    """「资金流水」区块（feat/funds-ledger）：记录页最前面的总资金流水表。

    三源 union 自 v_ledger 视图（不复制数据）；排序**最新在前**（与记录页其它表
    一致），但「事件后余额」必须先对全量正序算 running balance 再切片 ——
    用当前页现算余额会把页首之前的历史漏掉（spec 明令禁止，见 funding.running_balances）。
    行数与底部 10/20/50 选择器联动（同一个 rows cookie，本区块不单设行数入口）。
    """
    try:
        rows = [_rowd(r) for r in (deps.db.hl_ledger_rows() or [])]
        snap = deps.db.hl_latest_snapshot()
        total = _num(deps.db.hl_ledger_sum())
    except Exception as e:                                            # noqa: BLE001
        logger.warning("[admin] 资金流水读取失败：%s", e)
        return ("<h2>资金流水</h2><div class='card'>"
                "<div class='sec'>资金流水</div>"
                f"<div class='hint bad'>读取失败：{_esc(type(e).__name__)}"
                "（v_ledger 视图异常，请查日志）</div></div>")
    # 对账行：Σ全部流水 vs 最新快照权益，残差如实显示（不凑平）。
    snap_d = dict(snap) if snap is not None else None
    equity = _num(snap_d.get("equity")) if snap_d else 0.0
    residual = equity - total
    # 全量正序 running balance（期初 0），再切「最新 n 条」显示
    balances = hl_funding.running_balances(rows)
    page = list(zip(rows[-n:] if n else [], balances[-n:] if n else []))
    page.reverse()                      # 最新在前

    rows_html = ""
    for r, bal in page:
        rows_html += _ledger_row_html(r, bal)

    equity_txt = "暂无快照" if not snap else f"{_fmt(equity)} U"
    head = (f"Σ全部流水 = {_fmt(total)} U ｜ 当前账户权益（最新快照）= {equity_txt} ｜ "
            f"残差 = 权益 − Σ流水 = {_fmt(residual)} U（未实现盈亏 + 口径零头）")
    return (f"<h2>资金流水</h2>"
            f"<div class='card' id='cma-ledger'><div class='sec'>资金流水（最近 {n} 条 · 最新在前）"
            f"{_all_link('ledger', _token_of(deps))}</div>"
            f"<div class='hint'>{_esc(head)}</div>"
            + _table("<th>时间</th><th>类型</th><th class='num'>金额</th>"
                     "<th class='num'>余额</th><th class='num'>关联单</th><th>备注</th>",
                     rows_html, "暂无流水（等首次同步，或执行器未配置 HL 账户）", 6)
            + "<div class='legend'>余额 = 自账户 0 起累计（全量正序累加后再取当前页）· "
              "成交行金额 = 平仓盈亏 − 手续费 · 资金费正 = 收入 · 关联单 #N = 归集到的仓位</div>"
            "</div>")


# 记录页锚点导航（P3 R6）：slug 即各区块容器的 id（加 cma- 前缀做命名空间）。
# 六个 slug 互不相同 —— 渲染前的「查重」就在这份表上：同一 id 出现两次会跳错块。
_RECORD_ANCHORS = (("资金流水", "cma-ledger"), ("下单", "cma-orders"), ("成交", "cma-fills"),
                   ("信号", "cma-signals"), ("提醒", "cma-alerts"), ("事件", "cma-events"))


def render_records(deps: AdminDeps, rows_n: int = DEFAULT_ROWS,
                   signals: Optional[List[Dict[str, Any]]] = None) -> str:
    """记录页六块：资金流水 → 下单记录 → 成交 → 信号 → 提醒日志 → 执行器事件。

    ``signals`` 来自 ``payload["signals"]``（hl/views.status_payload 构造）；缺省
    None 时按空表渲染「暂无信号」（直接调 render_records 的老调用点不会炸）。
    信号块与总览原来是同一份渲染代码 —— 这里直接复用 ``views._signals_html``。

    P3 R6：页顶一行零 JS 锚点导航（六个 <a href='#cma-…'>），各区块容器带对应 id；
    id 前缀 cma- 是命名空间 —— 渲染前查重（_RECORD_ANCHORS 六个 slug 互不相同），
    不与页面既有 id 冲突。
    """
    n = normalize_rows(rows_n)
    o: List[str] = []
    # ---- 锚点导航（P3 R6）：小屏自然换行，样式复用 tabs 行，零 JS ----
    o.append("<div class='tabs'>" + "".join(
        f"<a href='#{sid}'>{label}</a>" for label, sid in _RECORD_ANCHORS) + "</div>")
    # ---- 资金流水（feat/funds-ledger）----
    # 位置铁律：必须在记录页**任何既有区块之前**（本函数原来第一块是 <h2>记录</h2>，
    # 资金流水的 <h2> 排在它前面，测试按 h2 顺序断言位置）。
    o.append(_funds_ledger_html(deps, n))
    o.append("<h2>记录</h2>")
    o.append(f"<div class='hint'>时间均为{engine.TZ_LABEL}；每块最多 {n} 行"
             f"（页脚「每页行数」可切 10/20/50）。</div>")

    # ---- 下单记录 ----
    o.append(f"<div class='card' id='cma-orders'><div class='sec'>下单记录（hl_orders · 最近 {n} 条）"
             f"{_all_link('orders', _token_of(deps))}</div>")
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
    o.append(f"<div class='card' id='cma-fills'><div class='sec'>成交（hl_fills · 最近 {n} 条）"
             f"{_all_link('fills', _token_of(deps))}</div>")
    rows = ""
    for r in _rows(deps, "hl_recent_fills", n):
        cp = _opt_num(r.get("closed_pnl"))
        cp_cls = "pos" if (cp or 0) > 0 else ("neg" if (cp or 0) < 0 else "")
        d = str(r.get("dir") or "")
        bs = _FILL_BS.get(d, "—")
        # 手续费走 views._fee_quoted（fee_quote 优先、NULL 回退 fee）：与总览的
        # 「净手续费」和净利计算同口径，否则同一笔成交在两处显示不同的数字。
        fee_txt = _fmt(_fee_quoted({"fee": r.get("fee"), "fee_quote": r.get("fee_quote")}), 4)
        rows += (f"<tr><td class='num'>{_ts(r.get('ts') or r.get('created_at'))}</td>"
                 f"<td>{_esc(r.get('coin') or _coin('', r.get('symbol')))}</td>"
                 f"<td>{bs}</td>"
                 f"<td class='num'>{_fmt(r.get('sz'), 4)}</td>"
                 f"<td class='num'>{_fmt(r.get('px'))}</td>"
                 f"<td class='num'>{fee_txt}</td>"
                 f"<td class='num {cp_cls}'>{_fmt(cp)}</td>"
                 f"<td>{'是' if r.get('reduce_only') else '—'}</td></tr>")
    o.append(_table("<th>时间</th><th>币种</th><th>买卖</th><th class='num'>数量</th>"
                    "<th class='num'>价格</th><th class='num'>手续费</th>"
                    "<th class='num'>平仓盈亏</th><th>减仓</th>",   # 列名「减仓」= 这笔是不是平仓性质
                    rows, "暂无成交记录", 8))
    o.append("</div>")

    # ---- 信号 ----（2026-10-18 从总览迁入；顺序与总览原顺序一致：成交之后）
    # 用 _signals_html 渲染（自带 <h2>信号</h2> + 表格，与总览那一块逐字相同），
    # 外面只套 card 边框、不再加 sec 标题 —— 否则「信号」会出现两次。
    # 切片用 n：以前总览整表渲染，仓位历史一长页面就无限变长（顺手按每页行数收口）。
    o.append("<div class='card' id='cma-signals'>" + _signals_html(list(signals or [])[:n])
             + f"<div class='foot'><span class='muted'>信号 / 仓位历史（含已平仓）：</span>"
               f"{_all_link('positions', _token_of(deps))}</div></div>")

    # ---- 提醒日志 ----
    # P1 降噪：先过滤后取数（铁律 8——标题行数必须等于实际渲染行数）。心跳类占比高，
    # 直接取 n 条再滤会「最近 20 条渲染 3 行」。P2 把池扩到 max(n*6, 2000)：
    # 例行行 24 条/小时滚 120 条只盖约 5 小时，真实事件（decision/告警/下单）几乎
    # 永远在池外、主页恒空态；SQLite 本地读 2000 行开销可忽略。
    _alert_pool = _rows(deps, "recent_alerts", max(n * 6, 2000))
    _alert_raw_n = len(_alert_pool)
    _alert_rows = [r for r in _alert_pool
                   if str(r.get("kind") or "") not in _ALERT_NOISE_KINDS][:n]
    _shown_n = len(_alert_rows)
    _alert_title = (f"提醒日志（alert_logs · 最近 {_shown_n} 条）" if _shown_n
                    else "提醒日志（alert_logs）")
    o.append(f"<div class='card' id='cma-alerts'><div class='sec'>{_alert_title}"
             f"{_all_link('alerts', _token_of(deps))}</div>")
    # 口径落可见文字：主页滤掉了什么、去哪看全部，必须写出来而不是让人猜少了行。
    # （措辞避开「全部 ›」字面 —— 那是 _all_link 的专属词，测试按出现次数数链接。）
    o.append("<div class='hint'>主页已隐藏心跳类例行行（推送/静默）；"
             "全部记录（含心跳）见「全部」子页。</div>")
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
    # 空表文案区分「真没有」和「被滤光了」：不让人误以为系统没在跑。
    _alert_empty = ("暂无提醒日志" if _alert_raw_n == 0
                    else "该时间窗内只有心跳类例行记录")
    o.append(_table("<th>时间</th><th>类型</th><th>标的</th><th>状态</th><th>内容</th>",
                    rows, _alert_empty, 5))
    o.append("</div>")

    # ---- 执行器事件 ----
    # P1 降噪：主页隐藏「心跳」与「对账一致」两类例行行（对账异常/升级照常显示）；
    # 子页不过滤。同样是先过滤后取数（铁律 8）。P2 池扩到 max(n*6, 2000)，
    # 与提醒日志同一修法（hl_events 例行行 24 条/小时，120 只盖约 5 小时；
    # 30 天 prune 后量级约 1.7 万行，2000 取的是最近约 3.5 天）。
    _event_pool = _rows(deps, "hl_recent_events", max(n * 6, 2000))
    _event_raw_n = len(_event_pool)
    _event_rows = [r for r in _event_pool
                   if not (str(r.get("kind") or "") == "heartbeat"
                           or (str(r.get("kind") or "") == "reconcile"
                               and str(r.get("message") or "").strip() == _RECONCILE_OK_MSG))][:n]
    _shown_n = len(_event_rows)
    _event_title = (f"执行器事件（hl_events · 最近 {_shown_n} 条）" if _shown_n
                    else "执行器事件（hl_events）")
    o.append(f"<div class='card' id='cma-events'><div class='sec'>{_event_title}"
             f"{_all_link('events', _token_of(deps))}</div>")
    o.append("<div class='hint'>主页已隐藏「心跳」与「对账一致」例行行；"
             "异常 / 决策 / 下单照常显示，全部事件见「全部」子页。</div>")
    rows = ""
    for r in _event_rows:
        kind = str(r.get("kind") or "—")
        msg = str(r.get("message") or "")
        rows += (f"<tr><td class='num'>{_ts(r.get('created_at'))}</td>"
                 f"<td>{_esc(_EV_TXT.get(kind, kind))}</td>"
                 f"<td>{_esc(r.get('symbol') or '—')}</td>"
                 f"<td>{_msg_cell(msg)}</td></tr>")
    # 空表文案区分「真没有」和「被滤光了」。
    _event_empty = ("暂无执行器事件" if _event_raw_n == 0
                    else "该时间窗内只有心跳 / 对账一致例行行")
    o.append(_table("<th>时间</th><th>类型</th><th>标的</th><th>内容</th>",
                    rows, _event_empty, 4))
    o.append("</div>")
    return "".join(o)


def _coin(coin: Any, symbol: Any) -> str:
    """优先显示 HL 侧币种名（BTC），否则从 MEXC 合约名（BTC_USDT）截出来。"""
    c = str(coin or "").strip()
    if c:
        return c
    s = str(symbol or "").strip()
    return s.split("_")[0] if s else "—"


# ---------------------------------------------------------------- 列表子页
# 六个独立子页（/admin/list/<slug>）的展示配置：表名、标题、筛选字段。
# 筛选字段的白名单与时间列在 app.db.DB.LIST_SUBPAGES（SQL 只认那边）；
# 这里只管「页面上长什么样」。label_map：值 → 中文（下拉选项按 DB 去重值生成，
# 用映射出文案，映射没有的值如实显示原值 —— 不编词）；"number" = 数字输入框。
_LIST_SUBPAGE_CFG: Dict[str, Dict[str, Any]] = {
    "orders": {"title": "下单记录", "filters": (("coin", "标的", None),
                                                ("purpose", "用途", _PURPOSE_TXT))},
    "fills": {"title": "成交", "filters": (("coin", "币种", None),
                                           ("dir", "买卖", None))},
    "ledger": {"title": "资金流水", "filters": (("kind", "类型", hl_funding.KIND_TXT),
                                                ("coin", "币种", None),
                                                ("position_id", "关联单 #", "number"))},
    "events": {"title": "执行器事件", "filters": (("kind", "类型", _EV_TXT),)},
    "alerts": {"title": "提醒日志", "filters": (("kind", "类型", _ALERT_KIND_TXT),)},
    "positions": {"title": "信号 / 仓位历史", "filters": (("symbol", "标的", None),
                                                          ("status", "状态", _ST_TXT))},
}


def _csv_ts(v: Any) -> str:
    """CSV 的时间列：北京时间全格式（页面上是短格式，导出要能当日期用）。"""
    f = _opt_num(v)
    if f is None:
        return ""
    s = f / 1000.0 if f > 1e12 else f
    try:
        return dt.datetime.fromtimestamp(s, engine.DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


# CSV 列规格：slug → [(表头, 取值函数)]。值一律导出 DB 原值（枚举不翻中文）——
# 导出是给人做表格/对账用的，原值可筛选、可核对；只有时间列按 spec 翻成北京时间。
def _csv_num(v: Any) -> Any:
    return "" if v is None else v


# CSV 公式注入防护（OWASP CSV Injection 惯例）：电子表格会把以 = + - @ 开头的
# 单元格当公式执行。只对「不是数字的文本」加前缀（数值列/负数原样不动）。
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _csv_safe(v: Any) -> Any:
    s = str(v)
    if s[:1] in _CSV_FORMULA_PREFIXES:
        try:
            float(s)
        except ValueError:
            return "'" + s
    return v


_CSV_COLS: Dict[str, List[Tuple[str, Callable[[Any], Any]]]] = {
    "orders": [("id", lambda r: r.get("id")), ("时间", lambda r: _csv_ts(r.get("created_at"))),
               ("标的", lambda r: r.get("coin") or ""), ("用途", lambda r: r.get("purpose") or ""),
               ("状态", lambda r: r.get("status") or ""), ("数量", lambda r: _csv_num(r.get("sz"))),
               ("价格", lambda r: _csv_num(r.get("px"))),
               ("已成交", lambda r: _csv_num(r.get("filled_sz"))),
               ("成交均价", lambda r: _csv_num(r.get("avg_px"))),
               ("错误", lambda r: r.get("error") or "")],
    "fills": [("id", lambda r: r.get("id")), ("成交时间", lambda r: _csv_ts(r.get("ts"))),
              ("币种", lambda r: r.get("coin") or ""), ("方向", lambda r: r.get("dir") or ""),
              ("数量", lambda r: _csv_num(r.get("sz"))), ("价格", lambda r: _csv_num(r.get("px"))),
              ("手续费U", lambda r: _csv_num(_fee_quoted(r))),
              ("平仓盈亏U", lambda r: _csv_num(r.get("closed_pnl"))),
              ("减仓", lambda r: 1 if r.get("reduce_only") else 0)],
    "ledger": [("时间", lambda r: _csv_ts(r.get("ts"))), ("类型", lambda r: r.get("kind") or ""),
               ("币种", lambda r: r.get("coin") or ""), ("金额U", lambda r: _csv_num(r.get("amount_usd"))),
               ("余额U", lambda r: _csv_num(r.get("_bal"))),
               ("关联单", lambda r: _csv_num(r.get("position_id"))),
               ("来源", lambda r: r.get("src") or ""), ("引用", lambda r: r.get("ref") or "")],
    "events": [("id", lambda r: r.get("id")), ("时间", lambda r: _csv_ts(r.get("created_at"))),
               ("类型", lambda r: r.get("kind") or ""), ("标的", lambda r: r.get("symbol") or ""),
               ("内容", lambda r: r.get("message") or "")],
    "alerts": [("id", lambda r: r.get("id")), ("时间", lambda r: _csv_ts(r.get("created_at"))),
               ("类型", lambda r: r.get("kind") or ""), ("标的", lambda r: r.get("symbol") or ""),
               ("送达", lambda r: 1 if r.get("sent_successfully") else 0),
               ("内容", lambda r: r.get("message") or "")],
    "positions": [("id", lambda r: r.get("id")), ("标的", lambda r: r.get("symbol") or ""),
                  ("状态", lambda r: r.get("status") or ""),
                  ("方向", lambda r: "多" if int(_num(r.get("side"), 1)) >= 0 else "空"),
                  ("入场", lambda r: _csv_num(posfields.entry_px(r))),
                  ("止损", lambda r: _csv_num(posfields.stop_px(r))),
                  ("数量", lambda r: _csv_num(posfields.qty(r))),
                  ("信号bar", lambda r: _csv_ts(r.get("signal_bar_ms")))],
}


def _date_bounds(date_from: str, date_to: str, unit: str) -> Tuple[Optional[float], Optional[float]]:
    """「YYYY-MM-DD」→ 时间列同单位的 epoch 边界（北京时间口径，含端点）。

    from = 当天 00:00:00.000，to = 当天 23:59:59.999（含当天的最后一秒）。
    手敲的坏日期按「无该边界」处理（fail-soft）：老书签带着垃圾参数不该 500，
    也不该悄悄把整表都放进来之外的语义 —— 只是这条边界不生效。
    """
    tz = engine.DISPLAY_TZ
    out: List[Optional[float]] = []
    for raw, end in ((date_from, False), (date_to, True)):
        if not str(raw or "").strip():
            out.append(None)
            continue
        try:
            d = dt.datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
        except ValueError:
            out.append(None)
            continue
        if end:
            t = dt.datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=tz).timestamp()
        else:
            t = dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()
        out.append(t * 1000.0 if unit == "ms" else t)
    return out[0], out[1]


def _subpage_parse(deps: AdminDeps, slug: str, params: Any) -> Dict[str, Any]:
    """把查询参数解析成子页取数输入（字段白名单外的键一律忽略）。

    返回 {filters(db用), raw(回显/拼链接用), tf/tt(时间列单位边界), qs(翻页/导出链接携带的筛选串)}。
    """
    spec = DB.LIST_SUBPAGES[slug]
    cfg = _LIST_SUBPAGE_CFG[slug]
    raw: Dict[str, str] = {}
    filters: Dict[str, Any] = {}
    for key, _label, kind in cfg["filters"]:
        v = str(params.get(key, "") or "").strip()
        if not v:
            continue
        raw[key] = v
        if kind == "number":
            # 关联单 # 是整数：敲坏的非数字按「没筛」处理（与坏日期同一口径）
            try:
                filters[key] = int(v)
            except ValueError:
                raw.pop(key)
        else:
            filters[key] = v
    tf, tt = _date_bounds(params.get("df", ""), params.get("dt", ""), spec["time_unit"])
    if tf is not None:
        raw["df"] = str(params.get("df", "")).strip()
    if tt is not None:
        raw["dt"] = str(params.get("dt", "")).strip()
    qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in raw.items())
    return {"filters": filters, "raw": raw, "tf": tf, "tt": tt, "qs": qs}


def _subpage_url(slug: str, k: str, qs: str, cursor: str = "") -> str:
    """拼子页链接：k + 筛选串（翻页链接必须把筛选条件原样带上，spec 定案 3）。"""
    parts = [f"k={quote(str(k or ''), safe='')}"]
    if qs:
        parts.append(qs)
    if cursor:
        parts.append(cursor)
    return f"/admin/list/{slug}?" + "&".join(parts)


def _subpage_rows_html(slug: str, rows: List[Dict[str, Any]],
                       balances: Optional[List[float]] = None) -> str:
    """子页表体：行渲染与主记录页同口径（资金流水直接复用 _ledger_row_html）。"""
    out = ""
    if slug == "ledger":
        for r, bal in zip(rows, balances or []):
            out += _ledger_row_html(r, bal)
        return out
    if slug == "orders":
        for r in rows:
            st = str(r.get("status") or "—")
            st_cls = "neg" if st == "failed" else ("warn" if st in ("intent", "sent") else "")
            err = str(r.get("error") or "")[:60]
            out += (f"<tr><td class='num'>{_ts(r.get('updated_at') or r.get('created_at'))}</td>"
                    f"<td>{_esc(_coin(r.get('coin'), r.get('symbol')))}</td>"
                    f"<td>{_esc(_PURPOSE_TXT.get(str(r.get('purpose')), r.get('purpose') or '—'))}</td>"
                    f"<td><span class='{st_cls}'>{_esc(_ORDER_ST_TXT.get(st, st))}</span></td>"
                    f"<td class='num'>{_fmt(r['filled_sz'] if r.get('filled_sz') is not None else r.get('sz'), 4)}</td>"
                    f"<td class='num'>{_fmt(r['avg_px'] if r.get('avg_px') is not None else r.get('px'))}</td>"
                    f"<td><span class='muted'>{_esc(err or '—')}</span></td></tr>")
        return out
    if slug == "fills":
        for r in rows:
            cp = _opt_num(r.get("closed_pnl"))
            cp_cls = "pos" if (cp or 0) > 0 else ("neg" if (cp or 0) < 0 else "")
            bs = _FILL_BS.get(str(r.get("dir") or ""), "—")
            fee_txt = _fmt(_fee_quoted({"fee": r.get("fee"), "fee_quote": r.get("fee_quote")}), 4)
            out += (f"<tr><td class='num'>{_ts(r.get('ts') or r.get('created_at'))}</td>"
                    f"<td>{_esc(r.get('coin') or _coin('', r.get('symbol')))}</td>"
                    f"<td>{bs}</td>"
                    f"<td class='num'>{_fmt(r.get('sz'), 4)}</td>"
                    f"<td class='num'>{_fmt(r.get('px'))}</td>"
                    f"<td class='num'>{fee_txt}</td>"
                    f"<td class='num {cp_cls}'>{_fmt(cp)}</td>"
                    f"<td>{'是' if r.get('reduce_only') else '—'}</td></tr>")
        return out
    if slug == "alerts":
        for r in rows:
            ok = bool(r.get("sent_successfully"))
            _kind = str(r.get("kind") or "—")
            out += (f"<tr><td class='num'>{_ts(r.get('created_at'))}</td>"
                    f"<td>{_esc(_ALERT_KIND_TXT.get(_kind, _kind))}</td>"
                    f"<td>{_esc(r.get('symbol') or '—')}</td>"
                    f"<td><span class='{'pos' if ok else 'neg'}'>"
                    f"{'已送达' if ok else '失败'}</span></td>"
                    f"<td>{_msg_cell(str(r.get('message') or ''))}</td></tr>")
        return out
    if slug == "events":
        for r in rows:
            kind = str(r.get("kind") or "—")
            out += (f"<tr><td class='num'>{_ts(r.get('created_at'))}</td>"
                    f"<td>{_esc(_EV_TXT.get(kind, kind))}</td>"
                    f"<td>{_esc(r.get('symbol') or '—')}</td>"
                    f"<td>{_msg_cell(str(r.get('message') or ''))}</td></tr>")
        return out
    if slug == "positions":
        for r in rows:
            st = str(r.get("status") or "—")
            st_cls = "pos" if st == "open" else ("warn" if st == "pending" else "")
            try:
                side_i = int(r.get("side"))
            except (TypeError, ValueError):
                side_i = 1
            out += (f"<tr><td>{_esc(_disp(r.get('symbol')) or _coin('', r.get('symbol')) or '—')}</td>"
                    f"<td><span class='{st_cls}'>{_esc(_ST_TXT.get(st, st))}</span></td>"
                    f"<td>{'多' if side_i >= 0 else '空'}</td>"
                    f"<td class='num'>{_fmt(posfields.entry_px(r))}</td>"
                    f"<td class='num'>{_fmt(posfields.stop_px(r))}</td>"
                    f"<td class='num'>{_fmt_sz(posfields.qty(r))}</td>"
                    f"<td class='num'>{_ts(r.get('signal_bar_ms'))}</td></tr>")
        return out
    return ""


_SUBPAGE_HEADS: Dict[str, str] = {
    "orders": "<th>时间</th><th>标的</th><th>用途</th><th>状态</th>"
              "<th class='num'>数量</th><th class='num'>均价</th><th>错误</th>",
    "fills": "<th>时间</th><th>币种</th><th>买卖</th><th class='num'>数量</th>"
             "<th class='num'>价格</th><th class='num'>手续费</th>"
             "<th class='num'>平仓盈亏</th><th>减仓</th>",
    "ledger": "<th>时间</th><th>类型</th><th class='num'>金额</th>"
              "<th class='num'>余额</th><th class='num'>关联单</th><th>备注</th>",
    "events": "<th>时间</th><th>类型</th><th>标的</th><th>内容</th>",
    "alerts": "<th>时间</th><th>类型</th><th>标的</th><th>状态</th><th>内容</th>",
    "positions": "<th>标的</th><th>状态</th><th>方向</th><th class='num'>入场</th>"
                 "<th class='num'>止损</th><th class='num'>数量</th><th>信号 bar</th>",
}

_SUBPAGE_EMPTIES: Dict[str, str] = {
    "orders": "暂无下单记录", "fills": "暂无成交记录", "ledger": "暂无流水",
    "events": "暂无执行器事件", "alerts": "暂无提醒日志", "positions": "暂无信号",
}


def _ledger_cursor(r: Dict[str, Any]) -> str:
    """资金流水的游标：排序键 (ts, ref) 序列化成「ts|ref」。

    不用 OFFSET（会漂移）也不能用行号（新流水追加在正序尾部会把老页行号顶走）；
    (ts, ref) 是 hl_ledger_rows 的排序键本身，新流水只会出现在键更大的一侧，
    老页永远稳定。ref 不含「|」（flow:N / fill:N / fund:…）。
    """
    return f"{_opt_num(r.get('ts')) or 0.0}|{r.get('ref') or ''}"


def render_list_subpage(deps: AdminDeps, slug: str, rows_n: int, params: Any) -> str:
    """列表子页正文：游标分页 + 筛选器 + CSV 导出入口（零 JS，纯 GET 链接/表单）。"""
    cfg = _LIST_SUBPAGE_CFG[slug]
    n = normalize_rows(rows_n)
    parsed = _subpage_parse(deps, slug, params)
    f, tf, tt, qs = parsed["filters"], parsed["tf"], parsed["tt"], parsed["qs"]
    token = _token_of(deps)
    before_s = str(params.get("before", "") or "").strip()
    after_s = str(params.get("after", "") or "").strip()

    head_html = (f"<h2>{_esc(cfg['title'])}</h2>"
                 f"<div class='hint'><a href='/admin?tab=records&amp;k={quote(str(token), safe='')}'>"
                 "‹ 返回记录页</a></div>")

    # ---- 取数（fail-soft：DB 层坏掉时显示错误行，页面不 500） ----
    err_html = ""
    try:
        if slug == "ledger":
            all_rows = [_rowd(r) for r in
                        (deps.db.list_subpage_rows("ledger", filters=f, time_from=tf, time_to=tt)
                         or [])]
            # 余额列：全量正序预计算后再切片（复用 PR #53 口径，页间连续）。
            balances = hl_funding.running_balances(all_rows)
            z = len(all_rows)
        else:
            before_i = int(before_s) if before_s.isdigit() else None
            after_i = int(after_s) if after_s.isdigit() else None
            page_rows = [_rowd(r) for r in
                         (deps.db.list_subpage_rows(slug, limit=n, before=before_i,
                                                    after=after_i, filters=f,
                                                    time_from=tf, time_to=tt) or [])]
            z = int(deps.db.list_subpage_count(slug, filters=f, time_from=tf, time_to=tt))
    except Exception as e:                                            # noqa: BLE001
        logger.warning("[admin] 列表子页 %s 读取失败：%s", slug, e)
        err_html = (f"<div class='card'><div class='hint bad'>读取失败："
                    f"{_esc(type(e).__name__)}（请查日志）</div></div>")
        page_rows, all_rows, balances = [], [], []

    nav_links: List[str] = []
    # 翻页链接要带上当前每页行数（显式 n= 优先于 cookie，与主记录页同口径）：
    # 否则带着 ?n=10 进来的人点「下一页」会掉回 cookie 的默认行数。
    nav_qs = (qs + "&" if qs else "") + f"n={n}"
    x = y = 0
    if err_html:
        count_txt = ""
    elif slug == "ledger":
        keys = [(_opt_num(r.get("ts")) or 0.0, str(r.get("ref") or "")) for r in all_rows]
        if before_s or after_s:
            bound: Optional[Tuple[float, str]] = None
            try:
                b_ts, b_ref = (before_s or after_s).split("|", 1)
                bound = (float(b_ts), b_ref)
            except (TypeError, ValueError):
                bound = None
            if bound is None:                       # 坏游标 → 回到最新一页（fail-soft）
                sel = list(range(max(0, z - n), z))
            elif before_s:
                sel = [i for i, kk in enumerate(keys) if kk < bound][-n:] if n else []
            else:
                sel = [i for i, kk in enumerate(keys) if kk > bound][:n] if n else []
        else:
            sel = list(range(max(0, z - n), z))
        disp = [(all_rows[i], balances[i]) for i in sel]
        disp.reverse()                              # 最新在前（与主记录页同序）
        x = (z - sel[-1]) if sel else 0             # 最新序号 = 共 Z − 正序下标（1 基）
        y = (z - sel[0]) if sel else 0
        if sel and (z - sel[0]) > 1:                # 还有更新的 → 上一页
            cur_q = quote(_ledger_cursor(all_rows[sel[-1]]), safe="")
            nav_links.append(f"<a class='btn gray' "
                             f"href='{_subpage_url(slug, token, nav_qs, 'after=' + cur_q)}'>"
                             "‹ 上一页（更新）</a>")
        if sel and sel[0] > 0:                      # 还有更旧的 → 下一页
            cur_q = quote(_ledger_cursor(all_rows[sel[0]]), safe="")
            nav_links.append(f"<a class='btn' "
                             f"href='{_subpage_url(slug, token, nav_qs, 'before=' + cur_q)}'>"
                             "下一页（更旧）›</a>")
        page_rows = [r for r, _b in disp]
        rows_html = _subpage_rows_html(slug, [r for r, _b in disp], [b for _r, b in disp])
    else:
        rows_html = _subpage_rows_html(slug, page_rows)
        if page_rows:
            # 序号 = 同筛选下 count(id >= 该行 id)：页眉数字与行内容同源，不靠数行数。
            x = int(deps.db.list_subpage_count(slug, filters=f, time_from=tf, time_to=tt,
                                               id_gte=page_rows[0].get("id")))
            y = int(deps.db.list_subpage_count(slug, filters=f, time_from=tf, time_to=tt,
                                               id_gte=page_rows[-1].get("id")))
            if y < z:
                older_id = page_rows[-1].get("id")
                nav_links.append(f"<a class='btn' href='{_subpage_url(slug, token, nav_qs, f'before={older_id}')}'>"
                                 "下一页（更旧）›</a>")
            if x > 1:
                newer_id = page_rows[0].get("id")
                nav_links.append(f"<a class='btn gray' href='{_subpage_url(slug, token, nav_qs, f'after={newer_id}')}'>"
                                 "‹ 上一页（更新）</a>")
    if (before_s or after_s) and not err_html:
        nav_links.append(f"<a class='btn gray' href='{_subpage_url(slug, token, nav_qs)}'>回到最新</a>")
    count_txt = (f"第 {x}–{y} 条 / 共 {z} 条（每页 {n} 行）"
                 if not err_html and (z or not (before_s or after_s)) else "")

    # ---- 筛选器（GET 表单，零 JS：提交即重载） ----
    # 隐藏 n 与翻页链接口径一致：提交筛选不掉回 cookie 行数。
    form: List[str] = [f"<form method='get' action='/admin/list/{slug}' class='inline'>"
                       f"<input type='hidden' name='k' value='{_esc(token)}'>"
                       f"<input type='hidden' name='n' value='{int(n)}'>"]
    for key, label, kind in cfg["filters"]:
        cur = parsed["raw"].get(key, "")
        if kind == "number":
            form.append(f"<label class='chk'>{_esc(label)} "
                        f"<input type='text' inputmode='numeric' name='{_esc(key)}' "
                        f"value='{_esc(cur)}' placeholder='全部' style='width:88px'></label>")
            continue
        try:
            opts = list(deps.db.list_subpage_options(slug, key) or [])
        except Exception:                                             # noqa: BLE001
            opts = []
        if cur and cur not in opts:
            opts.append(cur)                    # 当前筛选值不在去重列表（如刚删的行）也要回显
        items = "".join(
            f"<option value='{_esc(v)}'{' selected' if v == cur else ''}>"
            f"{_esc(kind.get(v, v) if isinstance(kind, dict) else v)}</option>"
            for v in opts)
        form.append(f"<label class='chk'>{_esc(label)} <select name='{_esc(key)}'>"
                    f"<option value=''>全部</option>{items}</select></label>")
    form.append(f"<label class='chk'>从 <input type='date' name='df' value='{_esc(parsed['raw'].get('df', ''))}'></label>")
    form.append(f"<label class='chk'>到 <input type='date' name='dt' value='{_esc(parsed['raw'].get('dt', ''))}'></label>")
    form.append("<button type='submit'>筛选</button></form>"
                f"<a class='btn gray' href='{_subpage_url(slug, token, '')}'>重置</a>")

    # ---- 每页行数切换（同一个 cma_rows cookie；切完回本子页并带原筛选参数） ----
    back = quote(f"/admin/list/{slug}" + (f"?{qs}" if qs else ""), safe="")
    row_links = " ".join(
        f"<a class='{'btn' if m == n else 'btn gray'}' "
        f"href='/admin/list/rows?n={m}&back={back}&k={quote(str(token), safe='')}'>{m}</a>"
        for m in ROW_CHOICES)

    # ---- 导出 CSV（同筛选条件下全量流式，不分页） ----
    export_qs = ("&" + qs) if qs else ""
    export_link = (f"<a class='btn' href='/admin/export?table={slug}&k={quote(str(token), safe='')}"
                   f"{export_qs}'>导出 CSV</a>")

    # 资金流水筛选态的余额口径必须写在**可见文字**里（spec 定案 5）：筛选后的余额
    # 是「期初 0 + 筛选后全集」累计，不是账户余额，不注明会被误读。
    ledger_note = ""
    if slug == "ledger":
        if parsed["filters"] or tf is not None or tt is not None:
            ledger_note = ("<div class='hint'>⚠️ 已筛选：<b>余额按当前筛选范围累计</b>"
                           "（期初 0 起算），不是账户余额；清空筛选后恢复全量口径。</div>")

    thead = _SUBPAGE_HEADS[slug]
    body = (head_html
            + f"<div class='card'><div class='sec'>"
              f"{_esc(cfg['title'])}（{_esc(count_txt)}）{export_link}</div>"
            + ledger_note
            + "<div class='foot'>" + "".join(form) + "</div>"
            + _table(thead, rows_html, _SUBPAGE_EMPTIES[slug], thead.count("<th>"))
            + (f"<div class='foot'>{(' '.join(nav_links))}</div>" if nav_links else "")
            + f"<div class='foot'><span class='muted'>每页行数：</span>{row_links}</div>"
            + "</div>")
    if slug == "ledger":
        body += ("<div class='legend'>余额 = 自期初 0 起累计（全量正序累加后再切当前页）· "
                 "成交行金额 = 平仓盈亏 − 手续费 · 资金费正 = 收入</div>")
    return body


def _subpage_page(token: str, slug: str, body_html: str, theme: str = DEFAULT_THEME,
                  rows_n: int = DEFAULT_ROWS) -> HTMLResponse:
    """子页外壳：与主面板同一套 head/CSS（不引新依赖），记录 Tab 高亮。"""
    label = _LIST_SUBPAGE_CFG[slug]["title"]
    attr = theme_attr(theme)
    attr_html = f" data-theme='{attr}'" if attr else ""
    from app.hl.views import THEME_CSS
    return HTMLResponse(
        f"<!doctype html><html lang='zh-CN'{attr_html}><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{ICON_LINKS}"
        f"<title>{PRODUCT_NAME} · {label}</title>{THEME_CSS}{DASH_CSS}{ADMIN_CSS}</head>"
        f"<body><div class='wrap'>{H1_BLOCK}"
        f"{_tabs_html('records', token)}{body_html}"
        f"{theme_switch_html('records', theme, token)}"
        f"{rows_switch_html('records', rows_n, token)}"
        "<div class='foot'><span class='muted'>手机可用 · 无外部资源 · "
        "面板只读，不持私钥</span></div></div></body></html>")


def _csv_stream(deps: AdminDeps, slug: str, f: Dict[str, Any],
                tf: Optional[float], tt: Optional[float]):
    """CSV 生成器：同筛选条件**全量**流式写出（不分页；id 游标分批取防大表撑内存）。

    UTF-8 带 BOM（Excel 打开中文不乱码）；首行表头；时间列北京时间全格式。
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([h for h, _fn in _CSV_COLS[slug]])
    yield "\ufeff" + buf.getvalue()
    if slug == "ledger":
        rows = [_rowd(r) for r in
                (deps.db.list_subpage_rows("ledger", filters=f, time_from=tf, time_to=tt)
                 or [])]
        # 余额列口径与子页/主记录页一致：全量正序预计算（复用 PR #53 实现）
        for r, bal in zip(rows, hl_funding.running_balances(rows)):
            r["_bal"] = bal
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerow(
                [str(_csv_safe(fn(r))) for _h, fn in _CSV_COLS[slug]])
            yield buf.getvalue()
        return
    last_id: Optional[int] = None
    while True:
        rows = list(deps.db.list_subpage_rows(slug, limit=500, before=last_id,
                                              filters=f, time_from=tf, time_to=tt) or [])
        if not rows:
            return
        for r in rows:
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerow(
                [str(_csv_safe(fn(_rowd(r)))) for _h, fn in _CSV_COLS[slug]])
            yield buf.getvalue()
        last_id = _rowd(rows[-1]).get("id")        # DESC 序的最后一行 = 本批最小 id
        if len(rows) < 500:
            return


# ---------------------------------------------------------------- 页面骨架
ADMIN_CSS = """<style>
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 14px}
.tabs a{font-size:13px;padding:7px 13px;border-radius:8px;background:var(--card);
        border:1px solid var(--line);color:var(--link);text-decoration:none;white-space:nowrap}
.tabs a.on{background:var(--btn);border-color:var(--btn);color:#fff;font-weight:600}
table{font-size:13px}
th,td{padding:6px 8px}
.alllink{float:right;font-size:12px;font-weight:400;color:var(--link)}
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
    """顶部常显状态条：执行模式 / 熔断 / 对账 / 卡单(>0) / HL 延迟 / API 有效期 / 更新时间。

    P3 R2/R3：卡单与 API 有效期两个徽章从这里唯一常驻（原「系统」卡的卡单行、
    原「API 有效期」卡的常驻整卡均已删）—— 徽章渲染与 /hl 状态条共用 views 的
    同一个函数（同一事实一种写法）。
    """
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
        # 卡单徽章（P3 R2）：>0 才出现（琥珀），正常态状态条不出现「卡单」字样。
        _stuck_badge_html(system),
        _badge("pos" if lat is not None else "muted", "HL 延迟",
               f"{lat:.0f} ms" if lat is not None else "—",
               "服务器请求 Hyperliquid 的耗时，与你到面板的延迟无关"),
        # API 有效期徽章（P3 R3）：唯一常驻出处，≤14 天琥珀、≤7 天红、已清仓红。
        _agent_badge_html(p.get("agent")),
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
    """总览：完全复用 hl/views._render_body（仓位表/盈亏曲线/挂单/事件一块都不重写）。

    _render_body 自带 <h1> 和它自己的状态条，而本面板顶部已有标题 + 状态条，
    所以把这两块剥掉，避免同一屏出现两个 <h1>、两条状态条。

    「API 有效期」状态卡接在整个总览的**最后**（在「系统」卡之后）：它是低频查看的
    运维信息，用户日常看的是三标的动态/账户/仓位；同时它不插进「三标的动态」与账户卡
    之间（那个位置由需求钉死给三标的动态）。卡片自己兜底，渲染失败不弄挂总览。

    「更新 API（续期）」表单**不在这里** —— 它是写操作入口，留在「参数」页
    （:func:`render_agent_renew_block`）。2026-10-19 只搬走表单，状态卡回原位。

    「为什么没开仓」（/why）已经并进 views._render_body 的「三标的动态」板块（原因行 /
    闸门异常行），数据源是这里现算并注入 payload["why"] —— 见 PR-5：面板不重算判定，
    只喂数据；`why_rows_fn` 可注入（测试用固定行，绝不触网）。
    """
    if "why" not in payload:
        payload = dict(payload)
        payload["why"] = _why_rows_for(deps)
    body = _render_body(payload)
    # 剥掉 _render_body 自带的 <h1> + 副题（.h1sub）和它自己的状态条：
    # 本面板顶部已有「双均线交易台」标题 + 状态条，避免同一屏出现两个 h1、两条状态条。
    body = re.sub(r"<h1>.*?</h1>\s*(?:<div class='h1sub'>.*?</div>\s*)?", "", body,
                  count=1, flags=re.S)
    body = re.sub(r"<div class='bar'>.*?</div>\s*", "", body, count=1, flags=re.S)
    try:
        body += render_agent_card(deps)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] API 有效期卡片渲染失败：%s", e)
    return body


def _why_rows_for(deps: AdminDeps) -> List[Dict[str, Any]]:
    """总览的 /why 行：优先用注入的函数，否则走 app.why.build_rows（只读）。

    单独一层的原因：运维只想看面板时不该因为「取行情失败」看到 500 —— 任何异常都
    退回空列表（区块整体不渲染），与三标的动态的 fail-soft 同口径。
    """
    fn = getattr(deps, "why_rows_fn", None)
    if callable(fn):
        try:
            return list(fn() or [])
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[admin] 注入的 /why 数据源失败（区块略去）：%s", e)
            return []
    try:
        from app.hl.views import _why_rows
        return _why_rows(deps.hl_deps)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] /why 诊断失败（区块略去）：%s", e)
        return []


# ---------------------------------------------------------------- API 有效期卡片
_LEVEL_CLS = {"ok": "pos", "near": "warn", "urgent": "neg", "cleanup": "neg",
              "expired": "neg", "unknown": "muted", "no_expiry": "pos"}


def _agent_source_txt(src: str) -> str:
    return {"api": "HL 授权接口", "manual": "手工兜底", "none": ""}.get(str(src or ""), "")


def render_agent_card(deps: AdminDeps) -> str:
    """「API 有效期」状态卡（总览末尾）：地址（截断）/到期日/剩余天数/状态词/自动清仓开关。

    数据只来自 ``hl_agent_state``（DB 缓存）—— 渲染**零网络调用**，面板在 HL 接口
    全挂时也能打开。state 缺失/损坏时显示「未知」并给出「下一个整点会刷新」的说明。

    **只渲染状态，不渲染写操作**：「更新 API（续期）」表单在「参数」页，见
    :func:`render_agent_renew_block`。
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
    # P3 R3：整卡从常驻 h2+card 降级为总览末尾的 <details>（默认收起，零 JS）；
    # 剩余 ≤14 天（THRESH_NEAR，与提醒同一阈值）或已触发清仓/已过期时服务端渲染
    # open —— 零 JS 的「自动展开」。徽章（状态条）是唯一常驻出处，这里是低频详情。
    open_attr = ""
    if ((days_i is not None and days_i <= agent_mod.THRESH_NEAR)
            or level in ("cleanup", "expired")):
        open_attr = " open"
    o.append(f"<details class='card agent-card'{open_attr}><summary>API 有效期</summary>")
    o.append("<div class='grid'>")
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
             f"（≤{agent_mod.THRESH_CLEANUP} 天且有仓位时自动清仓，可在「开关」页的「策略与保护」里关闭）。</div>")
    o.append("</details>")
    return "".join(o)


def render_agent_renew_block(deps: AdminDeps) -> str:
    """「更新 API（续期）」折叠表单（参数页末尾的独立小区块）。

    2026-10-19 从总览状态卡里**原样搬出**：状态（到期日/剩余/状态词）留在总览，
    写私钥这个运维动作跟着参数页走。表单 action / 字段 / 步骤文案与原卡片一致，
    外层套 ``card`` + ``sec`` 与参数页其它组同构。

    渲染**零网络调用**（只需当前 token），失败由调用方 try/except 兜底。
    """
    from app.hl import agent as agent_mod
    o: List[str] = []
    o.append("<div class='card'><div class='sec'>更新 API（续期）</div>")
    # 原生 <details>，零 JS：无 JS 的手机也能展开粘贴
    o.append("<details class=\"msgdetails\"><summary>在 HL 网页续期后粘贴新私钥</summary>"
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
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{ICON_LINKS}"
        f"<title>{PRODUCT_NAME} · {label}</title>{THEME_CSS}{DASH_CSS}{ADMIN_CSS}</head>"
        f"<body><div class='wrap'>{H1_BLOCK}"
        f"{strip_html}{_tabs_html(tab, token)}{banner}{body_html}"
        f"{theme_switch_html(tab, theme, token)}"
        f"{rows_switch_html(tab, rows_n, token)}"
        "<div class='foot'><span class='muted'>手机可用 · 无外部资源 · "
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
        # 盈亏曲线时间窗（P1）：总览复用 views._render_body，它从 payload 读
        # curve_span / admin_k —— 不在这里注入的话 span 参数被静默忽略（恒 7 天）、
        # 切换链接缺 k（无 cookie 深链 401，PR #54 同款）。
        payload["curve_span"] = normalize_span(request.query_params.get("span"))
        payload["admin_k"] = str(k or "")
        # 注：payload["rows"] 已删除（2026-10-18）—— 总览不再有长表切片，唯一消费者
        # views._render_body 的两张表搬到了记录页，记录页直接用 rows_n 走 DB 取数。
        strip = _status_strip(payload)
        if tab == "overview":
            body = _overview_body(deps, payload)
        elif tab == "params":
            body = render_params(deps, cfg, payload, btc_price(deps))
        elif tab == "switch":
            body = render_switch(deps, cfg)
        else:
            body = render_records(deps, rows_n, payload.get("signals"))
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
    def admin_params(request: Request, equity: Optional[str] = Form(None),
                     risk_pct: str = Form(""),
                     leverage: str = Form(""), stop_min: str = Form(""),
                     stop_max: str = Form(""), short_regime_ma: str = Form(""),
                     equity_mode: Optional[str] = Form(None),
                     equity_min: Optional[str] = Form(None),
                     hl_churn_max: Optional[str] = Form(None),
                     hl_close_grace_s: Optional[str] = Form(None),
                     feed_alert_delay_s: Optional[str] = Form(None),
                     hl_agent_valid_until_ms: str = Form(""),
                     hl_dd_warn_pct: Optional[str] = Form(None),
                     hl_dd_kill_pct: Optional[str] = Form(None),
                     k: str = Form("")):
        token = _auth(deps, request, k or None)
        values, errs = _parse_params({
            "equity": equity, "risk_pct": risk_pct, "leverage": leverage,
            "stop_min": stop_min, "stop_max": stop_max,
            "short_regime_ma": short_regime_ma, "equity_mode": equity_mode,
            "equity_min": equity_min,
            # PR-4：缺字段（None）= 保持原值，与 equity_mode 同口径
            "hl_churn_max": hl_churn_max,
            # 宽限期（对账失败自动消化）：同样缺字段 = 保持原值
            "hl_close_grace_s": hl_close_grace_s,
            # 数据源告警观察期（秒）：缺字段 = 保持原值，显式 0 = 立即推
            "feed_alert_delay_s": feed_alert_delay_s,
            "hl_agent_valid_until_ms": hl_agent_valid_until_ms,
        })
        if errs:
            raise HTTPException(400, "参数校验失败：" + "；".join(errs))
        # 回撤阈值（2026-10-23 从「开关」页迁到参数页「风控阈值」组）。
        #
        # **为什么单独处理、不走 _parse_params**：这两个键的「0」不是「关闭」而是
        # 「回到档位自动」，而 _parse_params 里 0 对 hl_churn_max 是「关闭」——
        # 语义正好相反，共用一个解析器必然错一个。校验/拒收规则全部委托给
        # _parse_dd_thresholds（唯一真相在 app/hl/dd.py:thresholds）。
        #
        # **拒收降级为提示、不 400**：本路由是「全有或全无」（任一字段非法就
        # raise 400 且一个字段都不写库）。若把 DD 的拒收也做成 400，用户为了改
        # 风险率而顺手填错的阈值，会把同一次提交里的其它参数一起丢掉 —— 那是
        # 静默丢改动，比「阈值没生效」严重得多。所以：DD 拒收 → 不写这两个键、
        # 其余照存、页面上给一句人话说明。
        dd_vals, dd_msg = _parse_dd_thresholds(hl_dd_warn_pct, hl_dd_kill_pct,
                                               deps.config_fn(),
                                               risk_pct=values.get("risk_pct"))
        values.update(dd_vals)
        put_errs = [err for err in (deps.put(key, val) for key, val in values.items()) if err]
        if put_errs:
            raise HTTPException(400, "保存失败（其余字段可能已写入）：" + "；".join(put_errs))
        if values:
            deps.log_event("decision",
                           f"面板修改参数（{', '.join(f'{k}={v:g}' if isinstance(v, float) else f'{k}={v}'
                                                      for k, v in sorted(values.items()))}）")
        # 提示语：DD 拒收优先显示（那才是「你填的值没生效」），其次是保存成功。
        if dd_msg:
            return RedirectResponse(_back(token, "params", dd_msg), status_code=303)
        return RedirectResponse(_back(token, "params", "已保存" if values else "无改动"),
                                status_code=303)

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
            try:
                _manual_kill(deps, False, "")
            except Exception as e:                                   # noqa: BLE001
                logger.warning("[admin] 清 kill 失败：%s", e)
        return RedirectResponse(_back(token, "switch"), status_code=303)

    # ---------------- 策略与保护（方向开关 + 到期自动清仓） ----------------
    @router.post("/admin/switch/strategy")
    def admin_switch_strategy(request: Request,
                              enable_long: Optional[str] = Form(None),
                              enable_shorts: Optional[str] = Form(None),
                              short_risk_half: Optional[str] = Form(None),
                              agent_expiry_cleanup: Optional[str] = Form(None),
                              hl_pnl_from_fills: Optional[str] = Form(None),
                              hl_fill_close: Optional[str] = Form(None),
                              hl_close_grace: Optional[str] = Form(None),
                              hl_kill_autoclear: Optional[str] = Form(None),
                              hl_dd_guard: Optional[str] = Form(None),
                              hl_dd_warn_pct: Optional[str] = Form(None),
                              hl_dd_kill_pct: Optional[str] = Form(None),
                              k: str = Form("")):
        """保存「策略与保护」的开关。

        语义铁律（2026-10-19 事故回归）：
          - **字段完全缺失（None）= 保持原值，不写库** —— 脚本/旧书签只带一个键时
            不会把其余方向开关顺手关掉；
          - 有值（面板是 hidden 的 0 / 勾选后 0+1）= 按 :func:`_flag` 解析后写库，
            所以「未勾选 → 0」必须能真的关掉。
        """
        token = _auth(deps, request, k or None)
        raw_vals = {"enable_long": enable_long, "enable_shorts": enable_shorts,
                    "short_risk_half": short_risk_half,
                    "agent_expiry_cleanup": agent_expiry_cleanup,
                    "hl_pnl_from_fills": hl_pnl_from_fills,
                    "hl_fill_close": hl_fill_close,
                    "hl_close_grace": hl_close_grace,
                    "hl_kill_autoclear": hl_kill_autoclear,
                    "hl_dd_guard": hl_dd_guard}
        # 空单风险减半的**旧值**（写事件时要「旧→新」，不能只记新值：事后复盘要能
        # 回答「这笔空单是减半前后哪一档开的」，只有新值答不了）。
        old_half = bool(getattr(deps.config_fn(), "short_risk_half", False))
        # 到期自动清仓沿用旧 _cleanup_on 的 **fail-open**（识别不了的值按「开」）：
        # 漏清仓的代价是到期后仓位想平也平不了，误开只是可能提前平一笔。方向开关
        # 没有这种不对称，走普通 _flag（严格 1/true/yes/on/y/t 才是开）。
        vals = {}
        for key, val in raw_vals.items():
            if val is None:
                continue
            if key == "agent_expiry_cleanup":
                vals[key] = 0 if str(val).strip().lower() in ("0", "false", "no", "off") else 1
            else:
                vals[key] = _flag(val)
        # 回撤阈值（§6）：留空 = 保持原值（**不写 0**）；越界/自相矛盾 → 两条都拒收。
        # 与开关分开处理：开关的语义是「缺字段=不变、显式 0=关」，数值项的 0 是
        # 「回到档位自动」，两者不能共用一个解析器。
        dd_vals, dd_msg = _parse_dd_thresholds(hl_dd_warn_pct, hl_dd_kill_pct, deps.config_fn())
        vals.update(dd_vals)
        put_errs = [e for e in (deps.put(key, val) for key, val in vals.items()) if e]
        if put_errs:
            raise HTTPException(400, "策略与保护保存失败：" + "；".join(put_errs))
        if not vals:
            # 什么都没带：不写库、不记事件，也不报「已保存」（审查中危：假横幅）
            return RedirectResponse(
                _back(token, "switch", dd_msg or "无改动（未提交任何开关）"), status_code=303)
        word = {1: "开", 0: "关"}
        deps.log_event("decision",
                       "面板修改策略与保护（做多=%s · 做空=%s · 到期自动清仓=%s · 净额流水口径=%s"
                       " · 交易所已平仓自动关账=%s · 证据不足先进宽限=%s · 对账异常可自动恢复=%s"
                       " · 回撤熔断=%s%s%s）"
                       % (word[vals["enable_long"]] if "enable_long" in vals else "不变",
                          word[vals["enable_shorts"]] if "enable_shorts" in vals else "不变",
                          word[vals["agent_expiry_cleanup"]]
                          if "agent_expiry_cleanup" in vals else "不变",
                          word[vals["hl_pnl_from_fills"]]
                          if "hl_pnl_from_fills" in vals else "不变",
                          word[vals["hl_fill_close"]]
                          if "hl_fill_close" in vals else "不变",
                          word[vals["hl_close_grace"]]
                          if "hl_close_grace" in vals else "不变",
                          word[vals["hl_kill_autoclear"]]
                          if "hl_kill_autoclear" in vals else "不变",
                          word[vals["hl_dd_guard"]] if "hl_dd_guard" in vals else "不变",
                          (f" · 空单风险减半={word[1] if old_half else word[0]}→"
                           f"{word[vals['short_risk_half']]}")
                          if "short_risk_half" in vals else "",
                          (" · 阈值 " + " / ".join(
                              f"{k.replace('hl_dd_', '').replace('_pct', '')}={vals[k]:g}"
                              for k in ("hl_dd_warn_pct", "hl_dd_kill_pct") if k in vals))
                          if any(k in vals for k in ("hl_dd_warn_pct", "hl_dd_kill_pct")) else ""))
        return RedirectResponse(_back(token, "switch", dd_msg or "已保存"), status_code=303)

    # ---------------- 推送开关 ----------------
    @router.post("/admin/switch/notify")
    def admin_switch_notify(request: Request, heartbeat_push: Optional[str] = Form(None),
                            report_push: Optional[str] = Form(None),
                            fill_push: Optional[str] = Form(None), k: str = Form("")):
        token = _auth(deps, request, k or None)
        raw_vals = {"heartbeat_push": heartbeat_push, "report_push": report_push,
                    "fill_push": fill_push}
        # 缺字段 = 保持原值（2026-10-19 审查中危回归：旧实现缺键 → _flag(None)=0，
        # 半截表单/curl 会把没带的推送静默关掉）。显式 0 仍照常生效。
        vals = {key: _flag(val) for key, val in raw_vals.items() if val is not None}
        if not vals:
            return RedirectResponse(_back(token, "switch", "无改动（未提交任何开关）"),
                                    status_code=303)
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
            # 【v2·高2】解除**回撤熔断**时必须同时把回撤基线重置掉：
            # dd 熔断永不自解，但 peak 不重置的话，下一轮 DD 仍是 31% → 立刻又置 kill
            # （实测解除后 60s kill 就回来了）⇒「人工解」形同虚设。
            # 必须在本请求内先重置、再解锁：分两步做的话，中间那一轮 tick 会抢先置 kill。
            # 其它 origin（manual/reconcile/daily_loss/size/expiry）**不**重置基线：
            # 那些停手与回撤无关，顺手把基线抹掉等于替用户绕过回撤熔断。
            note = ""
            if not want and _kill_origin(deps) == "dd":
                ok, msg = _reset_dd_peak(deps)
                note = (("；" + msg) if ok else
                        ("；⚠️ 回撤基线未能重置（" + msg + "）"))
                deps.log_event("kill", f"解除回撤熔断（origin=dd）→ {'重置基线成功' if ok else '重置基线失败'}：{msg}")
            _manual_kill(deps, want, "人工" if want else "")
            deps.log_event("kill", f"面板{'触发' if want else '解除'} kill switch")
            return RedirectResponse(
                _back(token, "switch", (f"已解除 kill switch{note}" if not want
                                        else "已触发 kill switch")),
                status_code=303)
        # 没有真实动作就不记「已触发」事件 —— 事件与事实必须一致
        deps.log_event("kill", f"面板请求{'触发' if want else '解除'} kill switch，"
                               "但执行器未接线（无动作）")
        return RedirectResponse(_back(token, "switch"), status_code=303)

    # ---------------- 重置回撤基线（§6 高2 的第二个入口） ----------------
    @router.post("/admin/switch/dd_reset")
    def admin_switch_dd_reset(request: Request, k: str = Form("")):
        """只把 peak_index 重置为当前 index；**不**动 kill（两条动作互相独立）。

        自动重置 = 绕过熔断，所以必须人显式点这个按钮；每次动作都记一条 kill 事件，
        事后能追「谁在什么时候把基线挪了」。
        """
        token = _auth(deps, request, k or None)
        ok, msg = _reset_dd_peak(deps)
        deps.log_event("kill", f"面板{'重置回撤基线' if ok else '请求重置回撤基线（未生效）'}：{msg}")
        return RedirectResponse(_back(token, "switch", msg), status_code=303)


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

    # ---------------- 列表子页：行数切换（切完回本子页带原筛选参数） ----------------
    # 必须声明在 /admin/list/{slug} **之前**：否则 "rows" 会被当成 slug 吃掉。
    @router.get("/admin/list/rows")
    def admin_list_rows(request: Request, n: str = str(DEFAULT_ROWS), back: str = "",
                        k: Optional[str] = None):
        token = _auth(deps, request, k or None)
        rows_n = normalize_rows(n)
        # back 只接受本面板的子页路径（防开放跳转）；没有/不像 → 回记录页
        target = back if back.startswith("/admin/list/") else \
            f"/admin?tab=records&k={quote(str(token))}"
        # 深链（无 cookie 只带 k）也要能活过这条 302：请求带 k 而 back 里没有时补上，
        # 否则跳回去就 401（子页路由不像 /admin/rows 那样顺手写 cma_admin cookie）。
        if k and "k=" not in target:
            target += ("&" if "?" in target else "?") + f"k={quote(str(token))}"
        https_ = request.url.scheme == "https" or \
            str(request.headers.get("x-forwarded-proto", "")).lower() == "https"
        resp = RedirectResponse(target, status_code=302)
        resp.set_cookie(ROWS_COOKIE, str(rows_n), max_age=365 * 86400,
                        secure=https_, samesite="lax")
        return resp

    # ---------------- 列表子页（记录页每块「全部 ›」的落点） ----------------
    @router.get("/admin/list/{slug}", response_class=HTMLResponse)
    def admin_list_subpage(request: Request, slug: str, k: Optional[str] = None):
        token = _auth(deps, request, k or None)
        if slug not in _LIST_SUBPAGE_CFG:
            raise HTTPException(404, "未知列表子页")
        # ?n= 优先（行数切换链接会用），否则读 cookie，与主记录页同一口径
        rows_n = normalize_rows(request.query_params.get("n")
                                or request.cookies.get(ROWS_COOKIE))
        body = render_list_subpage(deps, slug, rows_n, request.query_params)
        return _subpage_page(token, slug, body,
                             theme=normalize_theme(request.cookies.get(THEME_COOKIE)),
                             rows_n=rows_n)

    # ---------------- 列表子页：CSV 导出（同筛选全量流式） ----------------
    @router.get("/admin/export")
    def admin_export(request: Request, table: str = "", k: Optional[str] = None):
        token = _auth(deps, request, k or None)
        if table not in _LIST_SUBPAGE_CFG:
            raise HTTPException(404, "未知导出表")
        parsed = _subpage_parse(deps, table, request.query_params)
        # 先探一次 DB（方法缺失/视图坏了在这里给 500 人话，别等流式响应中途断掉）
        try:
            deps.db.list_subpage_count(table, filters=parsed["filters"],
                                       time_from=parsed["tf"], time_to=parsed["tt"])
        except Exception as e:                                        # noqa: BLE001
            logger.warning("[admin] CSV 导出 %s 失败：%s", table, e)
            raise HTTPException(500, "导出失败（数据库读取异常），请稍后重试或查日志")
        stamp = dt.datetime.now(engine.DISPLAY_TZ).strftime("%Y%m%d-%H%M%S")
        headers = {"Content-Disposition": f"attachment; filename=cma-{table}-{stamp}.csv"}
        return StreamingResponse(_csv_stream(deps, table, parsed["filters"],
                                             parsed["tf"], parsed["tt"]),
                                 media_type="text/csv; charset=utf-8", headers=headers)

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
