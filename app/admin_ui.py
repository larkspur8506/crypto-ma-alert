"""管理面板：单一入口 ``/admin?k=<ADMIN_TOKEN>&tab=<tab>``。

两个 Tab（链接切换，服务端渲染，**无 CDN / 无框架 / 无外部资源**，手机弱网可开；
页面**零 JS**）：

- ``overview`` 总览 —— 直接复用 :func:`app.hl.views._render_body`（不重写仓位/盈亏曲线区块）；
  末尾接「API 有效期」状态卡（地址/到期日/剩余/状态 + 到期前自动清仓开关状态）
  与「管理」卡（页尾唯一的写入口，见下）
- ``records``  记录 —— 资金流水 / hl_orders / hl_fills / signals / alert_logs / hl_events
  （「信号」2026-10-18 从总览迁入，复用 hl/views._signals_html；P4 删了页顶锚点导航
  行与六个区块容器的 id —— 手机上用不到，跳转靠滚动）

2026-10-30 面板精简（feat/panel-slim）：**只移除「可变性」，不改变任何生效行为值**。
参数页与开关页整体删除（连同它们的 POST 端点）——改参数/开关不再有任何面板写路径，
要么容器内 sqlite 直改（docs/HL_RUNBOOK.md），要么改代码走 PR。仍然是写操作的只剩
「管理」卡里的三个：拉闸/解除（kill）、更新 API（续期）、清除手工兜底有效期。
配置**读取**（Config.apply_db / configuration 表）一行不动：DB 里现有的模式、私钥、
全部参数与开关值照旧生效。

兼容（见 docs/ADMIN_MERGE_PLAN.md 第 2 节「保留」）：
- ``/hl``、``/admin/rules`` 两个旧 GET → 302 到总览；``/admin/logs`` → 记录页
  （书签/已发消息里的链接不断；被删的 POST 端点一律 404，不设兼容层）
- ``/api/hl/*`` 四个 JSON 接口完全不动；``/c/{pid}`` 人工确认页与签名链接机制已在
  2026-10-30 PR B 随人工确认/补录流一并退役（执行器自动开仓后它再无入口），
  真实成交一律在本面板「记录」页按 ``hl_fills`` 核对

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
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app import engine, posfields
from app.db import DB
from app.hl import dd as hl_dd
from app.hl import funding as hl_funding
from app.hl.pnl import QUOTE_FEE_TOKENS   # M3：费用未折算/老数据缺手续费的判据同源
from app.hl.views import (DASH_CSS, DEFAULT_ROWS, ROW_CHOICES, ROWS_COOKIE, _FILL_BS,
                        _ST_TXT, _agent_badge_html, _disp, _fee_quoted, _fmt_sz, _kv,
                        _render_body, _stuck_badge_html, normalize_span,
                        _signals_html, normalize_rows, status_payload)
from app.ui_theme import (ICON_LINKS, THEME_COOKIE, DEFAULT_THEME, H1_BLOCK, PRODUCT_NAME,
                          normalize_theme, theme_attr,
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


# 2026-10-30（feat/panel-slim）：只剩两个页签。参数/开关两页连同它们的 POST 端点整体
# 删除 —— 面板不再提供改参数、改开关、切模式、重置峰值的入口（改这些走 DB 直改）。
TABS: Tuple[Tuple[str, str], ...] = (
    ("overview", "总览"),
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
    # 阶段 B：面板读数来自调度器写的快照时，把「这份数据多旧」交给状态条。
    # 实时读数（PanelInfo / 直连 HlInfo）没有 age_s，取不到就不显示徽章。
    try:
        info = getattr(deps.hl_deps, "info", None)
        age_fn = getattr(info, "age_s", None)
        if callable(age_fn):
            age = _opt_num(age_fn())
            if age is not None and age >= 0:
                p["panel_age_s"] = age
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] 读面板快照年龄失败（不显示徽章）：%s", e)
    return p


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
            f"<div class='card'><div class='sec'>资金流水（最近 {n} 条 · 最新在前）"
            f"{_all_link('ledger', _token_of(deps))}</div>"
            f"<div class='hint'>{_esc(head)}</div>"
            + _table("<th>时间</th><th>类型</th><th class='num'>金额</th>"
                     "<th class='num'>余额</th><th class='num'>关联单</th><th>备注</th>",
                     rows_html, "暂无流水（等首次同步，或执行器未配置 HL 账户）", 6)
            + "<div class='legend'>余额 = 自账户 0 起累计（全量正序累加后再取当前页）· "
              "成交行金额 = 平仓盈亏 − 手续费 · 资金费正 = 收入 · 关联单 #N = 归集到的仓位</div>"
            "</div>")


def render_records(deps: AdminDeps, rows_n: int = DEFAULT_ROWS,
                   signals: Optional[List[Dict[str, Any]]] = None) -> str:
    """记录页六块：资金流水 → 下单记录 → 成交 → 信号 → 提醒日志 → 执行器事件。

    ``signals`` 来自 ``payload["signals"]``（hl/views.status_payload 构造）；缺省
    None 时按空表渲染「暂无信号」（直接调 render_records 的老调用点不会炸）。
    信号块与总览原来是同一份渲染代码 —— 这里直接复用 ``views._signals_html``。

    P4：P3 R6 的页顶锚点导航行与六个区块容器的 ``id='cma-…'`` 整体删除（用户拍板
    「手机上多余」）—— 页面顶部直接是「资金流水」卡，跳转靠滚动，无消费者不留死 id。
    """
    n = normalize_rows(rows_n)
    o: List[str] = []
    # ---- 资金流水（feat/funds-ledger）----
    # 位置铁律：必须在记录页**任何既有区块之前**（本函数原来第一块是 <h2>记录</h2>，
    # 资金流水的 <h2> 排在它前面，测试按 h2 顺序断言位置）。
    o.append(_funds_ledger_html(deps, n))
    o.append("<h2>记录</h2>")
    o.append(f"<div class='hint'>时间均为{engine.TZ_LABEL}；每块最多 {n} 行"
             f"（页脚「每页行数」可切 10/20/50）。</div>")

    # ---- 下单记录 ----
    o.append(f"<div class='card'><div class='sec'>下单记录（hl_orders · 最近 {n} 条）"
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
    o.append(f"<div class='card'><div class='sec'>成交（hl_fills · 最近 {n} 条）"
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
    o.append("<div class='card'>" + _signals_html(list(signals or [])[:n])
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
    o.append(f"<div class='card'><div class='sec'>{_alert_title}"
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
    o.append(f"<div class='card'><div class='sec'>{_event_title}"
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
# 2026-10-30 面板精简：随表单页删除，.field/.src/.rad/details.sw-help/details.lowfreq/
# .banner.warn 这些只服务于表单字段与折叠说明的 CSS 已一并删掉（不留死样式）。
ADMIN_CSS = """<style>
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 14px}
.tabs a{font-size:13px;padding:7px 13px;border-radius:8px;background:var(--card);
        border:1px solid var(--line);color:var(--link);text-decoration:none;white-space:nowrap}
.tabs a.on{background:var(--btn);border-color:var(--btn);color:#fff;font-weight:600}
table{font-size:13px}
th,td{padding:6px 8px}
.alllink{float:right;font-size:12px;font-weight:400;color:var(--link)}
.hint{width:100%;font-size:12.5px;color:var(--muted);margin:2px 0 6px;line-height:1.5}
.hint.big{font-size:13px;color:var(--fg)}
.chk{display:flex;align-items:center;gap:8px;font-size:13px;margin:7px 0}
.chk input{width:17px;height:17px;accent-color:var(--btn)}
.banner.ok{border-color:var(--okb-line);background:var(--okb-bg);color:var(--okb-fg)}
.card.danger{border:2px solid #b91c1c}
button{display:inline-block;background:var(--btn);color:#fff;border:0;border-radius:6px;
       padding:9px 15px;font-size:13.5px;cursor:pointer;margin:6px 8px 0 0}
button.gray{background:var(--btn-gray)}button.red{background:#b91c1c}
@media (max-width:640px){body{padding:9px;font-size:13px}h1{font-size:17px}
   .badge{padding:5px 9px;font-size:11.5px}h2{font-size:13px}
   .grid{grid-template-columns:1fr}table{font-size:13px}th,td{padding:5px 7px}}
</style>"""


def _age_txt(f: float) -> str:
    """「X 秒/分钟/小时前」的唯一格式化入口（数据徽章与延迟徽章的标题共用，避免两种写法）。"""
    if f < 60:
        return f"{int(f)} 秒前"
    if f < 3600:
        return f"{int(f // 60)} 分钟前"
    return f"{f / 3600:.1f} 小时前"


def _panel_age_badge(age: Any) -> str:
    """数据新鲜度徽章（阶段 B）：读数是快照时标出「多旧」；实时读取时返回空串。

    只有「读数来自调度器快照」才会传 age 进来 —— 实时读取（PanelInfo/直连）没有 age_s。
    超过 10 分钟转琥珀：monitor 每 5 分钟一轮，两轮没刷新就说明调度器出问题了。
    """
    f = _opt_num(age)
    if f is None or f < 0:
        return ""
    return _badge("muted" if f < 600 else "warn", "数据", _age_txt(f),
                  "面板读数是调度器最近一轮的读数（每 5 分钟刷新一次），不是此刻的实时值；"
                  "下单/对账/熔断走的始终是实时读数")


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
    # 读数来自调度器快照时，本次渲染**没有访问交易所** ⇒ 渲染时测到的耗时（毫秒级的 SQLite 读）
    # 绝不能当成网络延迟显示（曾显示成「1 ms」）。快照里带了调度器那一轮实测的耗时就用它，
    # 老快照（没有 `panel_net`）则显「—」。
    from_snapshot = _opt_num(p.get("panel_age_s")) is not None
    lat_from_snapshot = bool(net.get("latency_from_snapshot"))
    lat = _opt_num(net.get("latency_ms")) if (not from_snapshot or lat_from_snapshot) else None
    if lat is not None and lat_from_snapshot:
        age_txt = _opt_num(net.get("latency_age_s"))
        lat_title = ("调度器上一轮" + (f"（{_age_txt(age_txt)}）" if age_txt is not None else "")
                     + "读 Hyperliquid 的耗时；本次渲染没有访问交易所，也不是你到面板的延迟")
    elif from_snapshot:
        lat_title = "读数是调度器快照，本次渲染没有访问交易所 —— 所以这里不显示网络延迟"
    else:
        lat_title = "服务器请求 Hyperliquid 的耗时，与你到面板的延迟无关"
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
               f"{lat:.0f} ms" if lat is not None else "—", lat_title),
        # API 有效期徽章（P3 R3）：唯一常驻出处，≤14 天琥珀、≤7 天红、已清仓红。
        _agent_badge_html(p.get("agent")),
        # 数据新鲜度（阶段 B）：读数来自调度器快照时标出「几秒/几分钟前」，
        # 实时读取时不渲染（避免正常态多一个恒为「0 秒」的噪音徽章）。
        _panel_age_badge(p.get("panel_age_s")),
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


def _overview_body(deps: AdminDeps, payload: dict, cfg: Any = None) -> str:
    """总览：完全复用 hl/views._render_body（仓位表/盈亏曲线/挂单/事件一块都不重写）。

    _render_body 自带 <h1> 和它自己的状态条，而本面板顶部已有标题 + 状态条，
    所以把这两块剥掉，避免同一屏出现两个 <h1>、两条状态条。

    「API 有效期」状态卡接在整个总览的**最后**（在「系统」卡之后）：它是低频查看的
    运维信息，用户日常看的是三标的动态/账户/仓位；同时它不插进「三标的动态」与账户卡
    之间（那个位置由需求钉死给三标的动态）。卡片自己兜底，渲染失败不弄挂总览。

    「管理」卡（2026-10-30）接在「API 有效期」之后：面板仅存的三个写操作（拉闸 /
    更新 API / 清除兜底）全在这里，页面最底部是为了不让人日常滑屏顺手碰到；同样
    fail-soft（任何异常都不弄挂总览）。

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
    if cfg is None:
        cfg = _call(deps.config_fn)
    try:
        body += render_admin_card(deps, cfg)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[admin] 管理卡渲染失败：%s", e)
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
        o.append("<div class='hint'>还没读到授信信息：系统每个整点检查一次。"
                 "手工兜底有效期已不再提供页面入口（要设置走容器内 sqlite，见 docs/HL_RUNBOOK.md）。</div>")
    elif src == "manual":
        o.append("<div class='hint'>有效期来自 DB 里的手工兜底值（HL 授权接口没查到同名 Hermes）；"
                 "可在总览「管理」卡里清除。</div>")
    o.append("<div class='legend'>到期后执行器不能再开仓、平仓、撤单；到期前系统会按剩余天数提醒"
             f"（≤{agent_mod.THRESH_CLEANUP} 天且有仓位时自动清仓；agent_expiry_cleanup 沿用 DB 里的 "
             "值，面板不再提供切换入口）。</div>")
    o.append("</details>")
    return "".join(o)


def render_agent_renew_block(deps: AdminDeps) -> str:
    """「更新 API（续期）」折叠表单（总览「管理」卡里的一块，2026-10-30 迁入）。

    2026-10-19 从总览状态卡里**原样搬出**、2026-10-30 随参数页删除搬回总览的
    「管理」卡：动作、表单 action、字段、步骤文案一字未改，只是外层 ``card`` +
    ``sec`` 由 :func:`render_admin_card` 提供（不再自己套一层卡，避免卡中卡）。

    渲染**零网络调用**（只需当前 token），失败由调用方 try/except 兜底。
    """
    from app.hl import agent as agent_mod
    o: List[str] = []
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
    return "".join(o)


def render_admin_card(deps: AdminDeps, cfg: Any = None) -> str:
    """总览页尾「管理」卡：面板**仅存**的三个写动作 + 只读状态（2026-10-30 精简后）。

    精简原则是「只移除可变性」：参数页/开关页删掉以后，页面里还能改东西的只剩这三条，
    而且每一条都是「钱/权限遇到事」时才用的应急动作，所以集中在页尾一张红框卡里，
    日常滑屏不会顺手碰到：

      - **拉闸 / 解除**（POST /admin/switch/kill，字段 on=1/0）：提交语义、按钮文案、
        颜色切换与原来开关页的「应急操作」卡逐字一致；解除 DD 熔断时重置 peak 的
        现有逻辑在 POST 里，本轮不动（规格 A2-6：不加新确认机制）。
      - **更新 API（续期）**（:func:`render_agent_renew_block`）：原样搬来的私钥表单。
      - **清除手工兜底有效期**（POST /admin/hl/agent/fallback/clear）：仅当 DB 里
        真有兜底值时渲染 —— 没值就没什么可清，渲染出来只是噪音。**设置**入口已随
        参数表单删除（防陈旧兜底值误平仓），要设置走容器内 sqlite（runbook）。

    执行模式、钱包地址与私钥配置状态一律**只读**：面板不再提供切模式/改凭据的控件，
    值照旧来自 DB/env（Config.apply_db / hl_params），显示在这里只是为了让值班的人
    一眼看到「现在是实盘还是影子、凭据在不在」。

    ``cfg`` 缺省时自己取（保持与 _overview_body 一样的可调用性）；整卡 fail-soft，
    任何字段读不到都退化成一个提示，绝不抛异常。
    """
    if cfg is None:
        cfg = _call(deps.config_fn)
    mode = str(_call(deps.mode_fn, "off") or "off")
    if mode not in _MODE_TXT:
        mode = "off"
    kill = deps.kill_on()
    addr = str(_call(deps.address_fn, "") or "").strip()
    has_key = bool(str(_call(deps.private_key_fn, "") or "").strip())
    cur_until = str(getattr(cfg, "hl_agent_valid_until_ms", "") or "").strip()

    o: List[str] = []
    o.append("<div class='card danger'><div class='sec'>管理</div>")
    # ---- 只读：执行模式 + 地址/私钥（原开关页的说明段搬到这里）----
    o.append("<div class='hint'>执行模式："
             + _badge(_MODE_CLS[mode], "模式", _MODE_TXT[mode],
                      "面板只显示当前生效值；改模式走容器内 sqlite（见 docs/HL_RUNBOOK.md）")
             + (f"&nbsp;钱包地址：已配置（{_esc(addr[:6])}&hellip;{_esc(addr[-4:])}）"
                if addr else "&nbsp;钱包地址：未配置")
             + f"&nbsp;｜ 私钥：{'已配置' if has_key else '未配置'}"
             + "&nbsp;｜ 地址/私钥来自 env/DB，面板只读不改。</div>")

    # ---- 拉闸（kill switch）：交互与开关页「应急操作」卡逐字一致 ----
    o.append("<div class='sec'>拉闸（kill switch）</div>")
    if kill:
        o.append("<div class='banner'>&#9888;&#65039; <b>已触发</b>：执行器暂停开新仓"
                 "（仍允许人工平仓/撤单）。</div>")
    else:
        o.append("<div class='hint'>当前未触发。触发后执行器不再开新仓。</div>")
    o.append("<form method='post' action='/admin/switch/kill'>"
             # hidden k：拉闸是应急动作，不允许因 cookie 被禁/30 天过期而 401
             # （同卡的 renew/clear 都带 k，这里补齐同一种风格——审查 m1）。
             f"<input type='hidden' name='k' value='{_esc(_token_of(deps))}'>"
             f"<button class='{'red' if not kill else 'gray'}' type='submit' name='on' value='1'>"
             "触发（暂停开仓）</button>"
             f"<button class='{'gray' if kill else 'red'}' type='submit' name='on' value='0'>"
             "解除（恢复正常）</button></form>")
    # 解除 DD 熔断会顺带重置回撤基线 —— 这件事必须写在按钮旁（否则「人工解」被误解成
    # 「回撤清零」，下次误以为熔断线没生效）。
    o.append("<div class='hint'>origin=dd（回撤熔断）时「解除」会在同一请求里重置回撤基线，"
             "否则下一轮又立刻熔断；其它来源的停手只解锁、不动基线。</div>")

    # ---- 更新 API（续期）----
    o.append("<div class='sec'>更新 API（续期）</div>")
    o.append(render_agent_renew_block(deps))

    # ---- 清除手工兜底有效期（只有真有值时才渲染）----
    if cur_until:
        o.append("<div class='sec'>手工兜底有效期</div>")
        o.append(f"<div class='hint'>DB 里当前的手工兜底有效期：{_esc(cur_until)}（毫秒时间戳）。"
                 "HL 授权接口查不到同名 Hermes 时才会用到它；值过期/填错会让执行器按错误的"
                 "到期日行动，所以这里只提供「清除」。</div>")
        o.append("<form method='post' action='/admin/hl/agent/fallback/clear' class='inline'>"
                 f"<input type='hidden' name='k' value='{_esc(_token_of(deps))}'>"
                 "<button class='gray' type='submit'>清除兜底</button></form>")
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
        # 只剩两个页签：总览（含「管理」卡）/ 记录。旧书签里的 tab=params|switch 不认，
        # 上面已回落成 overview —— 不 404（书签来自已发消息，落在总览最有用）。
        if tab == "overview":
            body = _overview_body(deps, payload, cfg)
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

    # ---------------- kill switch（页尾「管理」卡，2026-10-30 从开关页迁来） ----------------
    # 迁移**只动位置与 redirect 目标**（tab=switch → tab=overview）：字段（on=1/0）、
    # 路由、事件文案、DD 熔断解除时重置基线的那段逻辑全部原样保留。
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
                _back(token, "overview", (f"已解除 kill switch{note}" if not want
                                          else "已触发 kill switch")),
                status_code=303)
        # 没有真实动作就不记「已触发」事件 —— 事件与事实必须一致
        deps.log_event("kill", f"面板请求{'触发' if want else '解除'} kill switch，"
                               "但执行器未接线（无动作）")
        return RedirectResponse(_back(token, "overview"), status_code=303)

    # ---------------- 清除手工兜底有效期（M3 的显式入口） ----------------
    @router.post("/admin/hl/agent/fallback/clear")
    def admin_agent_fallback_clear(request: Request, k: str = Form("")):
        """清掉手工兜底有效期（把空串写进 DB）。

        2026-10-30 起「设置兜底值」的输入框随参数表单一起删除了（防陈旧兜底值
        误平仓：资金路径类教训 #4）——「清除」这条**保留**，因为它只做减法，
        且是唯一能删掉 DB 里已有脏值的入口。要**设置**兜底值走容器内 sqlite 直改
        （docs/HL_RUNBOOK.md）。
        """
        token = _auth(deps, request, k or None)
        err = deps.put("hl_agent_valid_until_ms", "")
        if err:
            raise HTTPException(400, f"清除失败：{err}")
        deps.log_event("decision", "面板清除手工兜底有效期（hl_agent_valid_until_ms）")
        return RedirectResponse(_back(token, "overview", "已清除手工兜底有效期"),
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
        # 旧参数页书签：参数页已删（2026-10-30），落到总览。
        _auth(deps, request, k or None)
        return RedirectResponse(f"/admin?tab=overview&k={quote(str(k or ''))}",
                                status_code=302)

    @router.get("/admin/logs", response_class=HTMLResponse)
    def logs_redirect(request: Request, k: Optional[str] = None):
        _auth(deps, request, k or None)
        return RedirectResponse(f"/admin?tab=records&k={quote(str(k or ''))}",
                                status_code=302)

    return router
