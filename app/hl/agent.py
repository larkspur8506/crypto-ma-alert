"""代理钱包（agent / API wallet）有效期监控 + 到期前自动清仓。

为什么需要：HL 的 API 钱包有有效期（最长 180 天，默认 90 天）。到期**那一刻**执行器
就失去下单能力 —— 开仓、平仓、撤单全部被拒，而面板上的仓位看起来一切正常（本地
有仓、交易所也有仓，但谁也关不掉）。所以在到期前把仓位清干净，别等到「想平也平不了」。

设计要点：
1. **面板零网络调用**：`check_once` 每轮把结果写进 configuration 的
   ``hl_agent_state``（JSON），面板只读这个值。否则打开面板会等一次 HL 请求。
2. **回落**：`extraAgents` 查不到（接口挂了 / 没授权 / name 被改成别的）时用
   手工兜底的 ``hl_agent_valid_until_ms``；两者都没有 → source=none，面板显示「未知」，
   不告警（宁可显示未知，也不要拿猜出来的日期去清仓）。
3. **清仓顺序不可换**：先置 kill（保证清仓期间/之后 tick 不会再开新仓）→ 撤全部挂单
   → 逐仓市价平 → 复查 clearinghouseState。任一步失败重试 2 次，仍失败就喊人。
4. **清仓的互斥由调用方负责**（`app.main._run_agent_cleanup` 持 ``_tick_lock`` 调
   `flatten_all`），本模块不持锁 —— 这样单测可以不启动调度器直接跑。
5. 本模块不 import `app.main`（避免循环依赖），交易所客户端由调用方注入。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from app import engine
from app.hl import MODE_DRY
from app.hl import sizing as hl_sizing

logger = logging.getLogger(__name__)

# agent 名字：HL 网页 More → API 里那个「同名」授权。同名 approve 会替换旧 agent，
# 所以续期必须用同一个名字，否则旧 key 仍在、新 key 又不被认。
AGENT_NAME = "Hermes"

# 配置键（四处同步：app/config.py 默认值 + OVERRIDABLE + 面板表单 + 测试）
CFG_STATE = "hl_agent_state"            # 只读展示用状态（JSON 字符串）
CFG_MANUAL_UNTIL = "hl_agent_valid_until_ms"   # 手工兜底的毫秒时间戳
CFG_CLEANUP = "agent_expiry_cleanup"    # 到期前自动清仓开关
# 执行器用的 agent 私钥（DB 优先于 env HL_PRIVATE_KEY）。
# **唯一允许出现私钥明文的地方**：执行器下单要用它，面板/日志/alert_logs 一律不许。
CFG_PRIVATE_KEY = "hl_private_key"

# alert_logs.kind（必须同步进 app.admin_ui._ALERT_KIND_TXT）
KIND_ALERT = "agent_expiry_alert"
KIND_CLEANUP = "agent_expiry_cleanup"
KIND_RENEW = "agent_renew"

DEDUP_S = 86400.0        # 同一 kind 24h 内只推一次（成功才记，失败下轮重试）

# 动作阈值（天数，自然日、北京时间口径）
THRESH_NEAR = 14         # ≤14 天开始提醒
THRESH_URGENT = 7        # ≤7 天升级文案
THRESH_CLEANUP = 3       # ≤3 天执行清仓（开关开时）

KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# level → 中文状态词（面板用；顺序即优先级）
LEVEL_TXT = {
    "unknown": "未知",
    "ok": "正常",
    "near": "临期",
    "urgent": "临界",
    "cleanup": "临界",
    "expired": "已失效",
}


# ---------------------------------------------------------------- 解析 / 换算
def pick_agent(agents: Any, name: str = AGENT_NAME) -> Optional[dict]:
    """从 extraAgents 列表里挑出 name **精确等于** `name` 的那行（找不到返回 None）。

    必须精确匹配：用户可能同时授权了多个 API 钱包（如 Hermes / gridbot），
    拿「第一个」或「最长的有效期」会读成别人的到期日。
    """
    for a in (agents or []):
        if not isinstance(a, dict):
            continue
        if str(a.get("name") or "") == str(name):
            return a
    return None


def _ms(v: Any) -> Optional[int]:
    """任意值 → 正整数毫秒时间戳；null/0/空/非数字 → None（HL 用 null 表示不过期）。"""
    if v in (None, "", 0, "0"):
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def norm_agents(raw: Any) -> List[dict]:
    """extraAgents 原始返回 → [{name, address, valid_until}]。

    `validUntil` 是毫秒；null/0 = 无过期（老账户）。非 list 的返回（错误对象/限流）
    一律当空列表 —— 调用方据此走「查不到」的回落，而不是拿脏数据算天数。
    """
    out: List[dict] = []
    for a in (raw if isinstance(raw, list) else []):
        if not isinstance(a, dict):
            continue
        out.append({"name": a.get("name"),
                    "address": str(a.get("address") or ""),
                    "valid_until": _ms(a.get("validUntil", a.get("valid_until")))})
    return out


def days_left(valid_until_ms: Any, now_ms: Optional[float] = None) -> Optional[int]:
    """剩余**自然日**（北京时间口径的「今天」到到期那天的天数差）。

    用自然日而不是「小时数 ÷ 24」：用户看的日历是「17 号到期、今天 14 号 = 还剩 3 天」，
    按小时算会得到 2.6 天 → 显示成 2 天，与日历对不上。同一时区内取日期差，跨时区不会
    因为 UTC 差 8 小时而错一天。
    """
    vu = _ms(valid_until_ms)
    if vu is None:
        return None
    try:
        now_ms = float(now_ms if now_ms is not None else time.time() * 1000.0)
        tz = engine.DISPLAY_TZ
        d_exp = dt.datetime.fromtimestamp(vu / 1000.0, tz).date()
        d_now = dt.datetime.fromtimestamp(now_ms / 1000.0, tz).date()
    except (OverflowError, OSError, ValueError):
        return None
    return (d_exp - d_now).days


def level(days: Optional[int]) -> str:
    """剩余天数 → 动作档位（顺序即动作表：ok < near < urgent < cleanup < expired）。"""
    if days is None:
        return "unknown"
    if days <= 0:
        return "expired"
    if days <= THRESH_CLEANUP:
        return "cleanup"
    if days <= THRESH_URGENT:
        return "urgent"
    if days <= THRESH_NEAR:
        return "near"
    return "ok"


def fmt_bj(ms: Any, with_time: bool = True) -> str:
    """毫秒 → 北京时间文本（面板/推送同一口径）。"""
    v = _ms(ms)
    if v is None:
        return "—"
    try:
        d = dt.datetime.fromtimestamp(v / 1000.0, engine.DISPLAY_TZ)
    except (OverflowError, OSError, ValueError):
        return "—"
    return d.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def mask_addr(a: Any) -> str:
    """地址截断展示：前 6 + 后 4。短到没法截就原样（合同上不会有，但别崩）。"""
    s = str(a or "").strip()
    if not s:
        return "—"
    if len(s) <= 12:
        return s
    return f"{s[:6]}…{s[-4:]}"


# ---------------------------------------------------------------- 私钥 → 地址
def derive_address(key: str) -> tuple[Optional[str], str]:
    """私钥 → 地址。返回 (地址, 失败原因)。

    **失败原因绝不含私钥**：异常信息只给类型与固定人话（eth_account 的报错本身也不含
    私钥，但这里不转发 str(e) —— 万一将来换了实现也不至于把私钥抄进日志/响应）。
    """
    txt = str(key or "").strip()
    if not KEY_RE.match(txt):
        return None, "私钥格式不对：应为 0x 开头 + 64 位十六进制（0-9a-f）"
    try:
        from eth_account import Account
        return Account.from_key(txt).address, ""
    except ImportError:                                          # noqa: BLE001
        return None, "服务器缺少 eth-account 依赖，暂时无法校验私钥（请让 Hermes 处理）"
    except Exception:                                            # noqa: BLE001
        return None, "私钥无法解析（不是合法的 secp256k1 私钥），请重新复制"


# ---------------------------------------------------------------- 状态读写
def read_state(db: Any) -> dict:
    """读 ``hl_agent_state``；缺失/损坏一律返回空 dict（面板必须撑住坏数据）。"""
    raw = ""
    try:
        raw = db.get_config_str(CFG_STATE, "") or ""
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[agent] 读状态失败：%s", e)
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def write_state(db: Any, state: dict) -> None:
    """写 ``hl_agent_state``（JSON 字符串）。写失败只告警 —— 不能因为面板缓存写不进去
    就中断到期检查（清仓那一步更重要）。"""
    try:
        db.set_config(CFG_STATE, json.dumps(state, ensure_ascii=False))
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[agent] 写状态失败：%s", e)


def cleanup_enabled(value: Any) -> bool:
    """自动清仓开关：**识别不了一律按「开」**。

    与推送开关的 fail-open 同一取舍：漏清仓的代价是「到期后仓位关不掉」，
    误清仓的代价是「提前平掉一笔仓」。前者不可逆，后者还能再开。
    """
    if isinstance(value, bool):
        return value
    raw = str(value if value is not None else "").strip().strip('"').strip("'").lower()
    return raw not in ("0", "false", "no", "off")


def cleanup_switch(db: Any, cfg: Any = None) -> bool:
    """当前生效的清仓开关（DB > 配置对象默认）。"""
    raw = ""
    try:
        raw = str(db.get_config_str(CFG_CLEANUP, "") or "").strip()
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[agent] 读清仓开关失败（按「开」处理）：%s", e)
        return True
    if raw:
        return cleanup_enabled(raw)
    return cleanup_enabled(getattr(cfg, CFG_CLEANUP, True))


def manual_until(db: Any, cfg: Any = None) -> Optional[int]:
    """手工兜底的有效期（毫秒）。容忍误填成秒（<1e11 视为秒）。"""
    raw = ""
    try:
        raw = str(db.get_config_str(CFG_MANUAL_UNTIL, "") or "").strip()
    except Exception:                                             # noqa: BLE001
        raw = ""
    if not raw:
        raw = str(getattr(cfg, CFG_MANUAL_UNTIL, "") or "").strip()
    if not raw:
        return None
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("[agent] 手工兜底的有效期不是数字，已忽略")
        return None
    if n <= 0:
        return None
    return n * 1000 if n < 100_000_000_000 else n      # 秒 → 毫秒（手填容错）


# ---------------------------------------------------------------- 推送
def _alert(db: Any, notifier: Any, kind: str, text: str, dedup: bool = True) -> bool:
    """24h 窗口去重 + 成功后落库（**绝不先置去重再发送**：发送失败下轮还要重试）。

    dedup=False 用于「一次性事件」（如更新 API 成功）—— 同类事件一天内可能发生多次
    （先填错再填对），不能被去重吞掉。
    """
    if dedup:
        try:
            if db.was_alerted(None, None, kind, within_s=DEDUP_S):
                logger.info("[agent] %s 24h 内已推过，跳过", kind)
                return False
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[agent] 去重查询失败（继续发送）：%s", e)
    ok = False
    try:
        ok = bool(notifier is not None and notifier.send_message(text))
    except Exception as e:                                        # noqa: BLE001
        logger.error("[agent] 推送失败：%s", e)
        ok = False
    try:
        db.log_alert(None, None, kind, text, ok)
    except Exception as e:                                        # noqa: BLE001
        logger.error("[agent] 告警落库失败（补推将不可用）：%s", e)
    return ok


def _text_near(until_ms: Any, days: int, prefix: str) -> str:
    return (f"{prefix}⚠️ API 有效期提醒：代理钱包 API 将于 {fmt_bj(until_ms)}（北京时间）到期，"
            f"还剩 {days} 天。届时执行器无法开仓、平仓、撤单。\n"
            "请在 HL 网页 More → API 用同名 Hermes 重新授权（有效期可选最长 180 天），"
            "再到面板「API 有效期」粘贴新私钥。")


def _text_urgent(until_ms: Any, days: int, prefix: str) -> str:
    return (f"{prefix}🔴 API 有效期紧急：代理钱包 API 将于 {fmt_bj(until_ms)}（北京时间）到期，"
            f"只剩 {days} 天。到期后执行器彻底失去下单能力，仓位将无法自动平掉。\n"
            "请尽快在 HL 网页 More → API 用同名 Hermes 重新授权，再到面板「API 有效期」粘贴新私钥。")


def _text_cleanup_off(until_ms: Any, days: int, prefix: str) -> str:
    return (f"{prefix}🔴 API 有效期紧急：代理钱包 API 将于 {fmt_bj(until_ms)}（北京时间）到期，"
            f"只剩 {days} 天。\n"
            "「到期前自动清仓」已关闭，系统不会替你平仓 —— 到期后想平也平不了，请自行处理。\n"
            "（需要自动保护就到面板「参数」页打开这个开关。）")


def _text_expired(until_ms: Any, prefix: str) -> str:
    return (f"{prefix}⛔ API 已失效：代理钱包 API 已于 {fmt_bj(until_ms)}（北京时间）过期，"
            "执行器不能再开仓/平仓/撤单，同时已暂停自动开新仓。\n"
            "请主钱包在 HL 网页 More → API 用同名 Hermes 授权新 API，"
            "然后到面板「API 有效期」粘贴新私钥。")


def _text_cleanup_ok(until_ms: Any, n_closed: Optional[int], prefix: str) -> str:
    did = (f"已撤掉全部挂单并平掉 {n_closed} 个标的" if n_closed
           else "已撤掉全部挂单并平掉仓位（此前已完成）")
    return (f"{prefix}✅ 已清仓（agent 到期保护）：代理钱包 API 将于 {fmt_bj(until_ms)}（北京时间）"
            f"到期，{did}，并已暂停开新仓。\n"
            "请主钱包在 HL 网页用同名 Hermes 授权新 API，然后到面板「API 有效期」粘贴新私钥。")


def _text_cleanup_failed(until_ms: Any, err: str, prefix: str) -> str:
    return (f"{prefix}⚠️ 自动清仓失败：{err}，请手动处理。\n"
            f"（代理钱包 API 将于 {fmt_bj(until_ms)}（北京时间）到期，系统已先暂停开新仓，下一轮会再试。）")


# ---------------------------------------------------------------- 清仓
def _f(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def flatten_all(db: Any, params: Any, info: Any, ex: Any, notifier: Any = None, *,
                log_event: Optional[Callable[..., None]] = None,
                retries: int = 2) -> dict:
    """到期保护清仓：撤全部挂单 → 逐仓市价平 → 复查无仓。

    返回 {"ok": bool, "error": str, "closed": [{coin, sz, px}], "attempts": int,
          "dry": bool, "cancelled": int}

    - **dry（影子模式）**：照常算「本该撤多少/平哪些」，但不碰交易所，只返回 dry=True；
      调用方据此发「本应清仓」的提醒。影子模式下不写 state.cleanup=done —— 没真平仓
      就不能显示「已清仓」。
    - kill 置位不在这里做：调用方（`check_once`）在调本函数**之前**就置好了，
      这样即使本函数第一步就抛异常，也不会留下「没人管的新仓」。
    - 失败重试 `retries` 次（默认 2 → 最多 3 轮）。每轮都重新读快照，不缓存仓位。
    """
    dry = str(getattr(params, "mode", "") or "").lower() == MODE_DRY
    address = str(getattr(params, "account_address", "") or "")

    def _ev(msg: str, **data: Any) -> None:
        if log_event is None:
            return
        try:
            log_event("order", msg, data=data or None)
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[agent] 写事件失败：%s", e)

    last_err = ""
    attempts = 0
    for _ in range(max(0, int(retries)) + 1):
        attempts += 1
        last_err = ""
        closed: List[dict] = []
        cancelled: List[int] = []

        # ---- 1) 撤掉全部挂单（先撤单：清仓期间挂着的止损单可能和新平仓单打架）
        if not dry:
            try:
                res = ex.cancel_all_orders()
            except Exception as e:                                # noqa: BLE001
                res = {"ok": False, "error": f"撤单异常：{type(e).__name__}"}
            if not res.get("ok"):
                last_err = f"撤单失败：{res.get('error') or '未知原因'}"
                _ev(f"清仓：{last_err}")
                continue
            cancelled = list((res.get("raw") or {}).get("oids") or [])
            _ev(f"清仓：已撤 {len(cancelled)} 张挂单")

        # ---- 2) 读当前仓位（**不缓存**，每轮重读；读不到不算成功）
        try:
            snap = info.snapshot(address) or {}
        except Exception as e:                                    # noqa: BLE001
            last_err = f"读取账户失败：{type(e).__name__}"
            _ev(f"清仓：{last_err}")
            continue
        positions = [p for p in (snap.get("positions") or [])
                     if _f((p or {}).get("size")) > 0]

        # ---- 3) 逐仓平（市价 = 激进 IOC 限价，reduce_only，方向 = 平仓方向）
        for pos in positions:
            coin = str(pos.get("coin") or "")
            sz = abs(_f(pos.get("size")))
            side = -1 if _f(pos.get("side"), 1) < 0 else 1
            if not coin or sz <= 0:
                continue
            if dry:
                closed.append({"coin": coin, "sz": sz, "px": _f(pos.get("mark_px")), "dry": True})
                continue
            is_buy = side != 1          # 平多 = 卖；平空 = 买（与 executor 同一口径）
            try:
                sz_dec = int((info.specs([coin]).get(coin) or {}).get("sz_decimals", 5))
            except Exception:                                     # noqa: BLE001
                sz_dec = 5
            try:
                px = hl_sizing.round_price(ex.slippage_limit(coin, is_buy, ex.mid(coin)), sz_dec)
            except Exception as e:                                # noqa: BLE001
                px = _f(pos.get("mark_px"))
                if px <= 0:
                    last_err = f"{coin} 取不到价格，无法平仓"
                    _ev(f"清仓：{last_err}")
                    continue
                _ev(f"清仓：{coin} 限价计算失败，改用标记价（{type(e).__name__}）")
            try:
                r = ex.place_order(coin, is_buy, sz, px, reduce_only=True)
            except Exception as e:                                # noqa: BLE001
                r = {"ok": False, "status": "error", "error": f"下单异常：{type(e).__name__}"}
            if not r.get("ok"):
                last_err = f"{coin} 平仓失败：{r.get('error') or '未知原因'}"
                _ev(f"清仓：{last_err}")
                continue
            if str(r.get("status") or "") != "filled":
                last_err = f"{coin} 平仓单未成交（{r.get('status')}）"
                _ev(f"清仓：{last_err}")
                continue
            closed.append({"coin": coin, "sz": sz, "px": _f(r.get("avg_px"), px)})
            _ev(f"清仓：已平 {coin} {sz:g} @ {_f(r.get('avg_px'), px):,.2f}")

        if last_err:
            continue

        # ---- 4) 复查：仓位必须真的没了（dry 模式交易所侧什么都没变，跳过复查）
        if dry:
            return {"ok": True, "error": "", "closed": closed, "attempts": attempts,
                    "dry": True, "cancelled": 0}
        try:
            snap2 = info.snapshot(address) or {}
        except Exception as e:                                    # noqa: BLE001
            last_err = f"复查账户失败：{type(e).__name__}"
            continue
        left = [p for p in (snap2.get("positions") or []) if _f((p or {}).get("size")) > 0]
        if left:
            last_err = (f"复查仍有 {len(left)} 个仓位未平（"
                        + "、".join(str((p or {}).get("coin") or "?") for p in left) + "）")
            _ev(f"清仓：{last_err}")
            continue

        return {"ok": True, "error": "", "closed": closed, "attempts": attempts,
                "dry": False, "cancelled": len(cancelled)}

    return {"ok": False, "error": last_err or "未知原因", "closed": [],
            "attempts": attempts, "dry": dry, "cancelled": 0}


# ---------------------------------------------------------------- 每轮检查
def check_once(db: Any, cfg: Any, notifier: Any, info: Any, *,
               address: str = "", name: str = AGENT_NAME,
               now_ms: Optional[float] = None,
               cleanup: Optional[Callable[..., dict]] = None,
               prefix: str = "",
               log_event: Optional[Callable[..., None]] = None) -> dict:
    """一轮到期检查：读有效期 → 算天数 → 按动作表告警/清仓 → 每轮都写 state。

    `cleanup` 是注入的清仓器（`cleanup(reason=...) -> dict`），由 app.main 提供并负责
    持 ``_tick_lock``；缺省视为「未接线」→ 记 failed 并喊人（宁可吵，也别静默）。

    返回本轮写入的 state（含 address / valid_until_ms / days_left / checked_ms /
    cleanup / source），方便调用方打日志与断言。
    """
    now_ms = float(now_ms if now_ms is not None else time.time() * 1000.0)
    prev = read_state(db)
    prev_cleanup = str(prev.get("cleanup") or "none")
    if prev_cleanup not in ("none", "done", "failed"):
        prev_cleanup = "none"

    # ---- 1) 有效期：extraAgents（name 精确匹配）→ 手工兜底 → 未知
    source = "none"
    until: Optional[int] = None
    agent_addr = ""
    if str(address or "").strip():
        try:
            agents = norm_agents(info.extra_agents(address))
            row = pick_agent(agents, name)
            if row is not None:
                until = row.get("valid_until")
                if until is not None:
                    source = "api"
                    agent_addr = str(row.get("address") or "")
                else:
                    # 行在、但 validUntil 为空 = 不限期授权（老账户）。仍算 api 来源，
                    # 但天数未知 → 面板显示「未知」，不告警、不清仓。
                    source = "api"
                    agent_addr = str(row.get("address") or "")
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[agent] extraAgents 读取失败，回落手工兜底：%s", e)
    if until is None:
        m = manual_until(db, cfg)
        if m is not None:
            until, source = m, "manual"
            agent_addr = agent_addr or str(prev.get("address") or "")

    days = days_left(until, now_ms) if until is not None else None
    lvl = level(days)
    if source == "none":
        lvl = "unknown"

    state = {"address": agent_addr, "valid_until_ms": until, "days_left": days,
             "checked_ms": int(now_ms), "cleanup": prev_cleanup, "source": source}

    # ---- 2) 动作表
    try:
        if lvl == "near":
            _alert(db, notifier, KIND_ALERT, _text_near(until, days, prefix))
        elif lvl == "urgent":
            _alert(db, notifier, KIND_ALERT, _text_urgent(until, days, prefix))
        elif lvl == "cleanup":
            if not cleanup_switch(db, cfg):
                # 开关关：照常告警，并明写「不会替你平仓」
                _alert(db, notifier, KIND_ALERT, _text_cleanup_off(until, days, prefix))
            else:
                if state["cleanup"] != "done":
                    # ★ 先 kill 置位（在任何撤单/平仓之前）：清仓中途失败/进程被杀，
                    #   下一轮 tick 也绝不会再开新仓。
                    _kill(db, "agent 到期保护：清仓前先暂停开新仓", log_event)
                    res: dict = {}
                    if cleanup is None:
                        res = {"ok": False, "error": "清仓器未接线（执行器未装配）"}
                    else:
                        try:
                            res = cleanup(reason=f"agent 剩余 {days} 天到期") or {}
                        except Exception as e:                    # noqa: BLE001
                            logger.exception("[agent] 清仓异常：%s", e)
                            res = {"ok": False, "error": f"清仓异常：{type(e).__name__}"}
                    if res.get("ok") and not res.get("dry"):
                        state["cleanup"] = "done"
                        n = len(res.get("closed") or [])
                        _alert(db, notifier, KIND_CLEANUP, _text_cleanup_ok(until, n, prefix))
                    elif res.get("ok"):
                        # 影子模式：只提醒「本应清仓」，不写 done（演出没发生）
                        _alert(db, notifier, KIND_CLEANUP,
                               f"{prefix}🧪 影子模式：API 将于 {fmt_bj(until)}（北京时间）到期，"
                               f"本应清仓 {len(res.get('closed') or [])} 个标的（已撤单但未下真单）。"
                               "切到实盘后才会自动平仓。")
                    else:
                        state["cleanup"] = "failed"
                        _alert(db, notifier, KIND_CLEANUP,
                               _text_cleanup_failed(until, str(res.get("error") or "未知原因"), prefix))
                else:
                    # 之前已清完：保证「已清仓」这条至少成功送达一次（成功即去重）
                    _alert(db, notifier, KIND_CLEANUP, _text_cleanup_ok(until, None, prefix))
        elif lvl == "expired":
            # 到期后 API 已经没权限了：置 kill（若未置）+ 每 24h 提醒一次
            _kill(db, "agent 已失效：暂停开新仓", log_event)
            _alert(db, notifier, KIND_ALERT, _text_expired(until, prefix))
        else:
            logger.info("[agent] 有效期检查：%s（source=%s，剩余 %s 天）", lvl, source, days)
    finally:
        # ---- 3) 每轮写 state（即使上面告警/清仓抛异常也要落，面板才看得见）
        state["level"] = lvl
        write_state(db, state)
    return state


def _kill(db: Any, reason: str, log_event: Optional[Callable[..., None]] = None) -> None:
    """置 kill（已置则不重复写，保留最早的触发原因）。"""
    try:
        if db.hl_kill():
            return
        db.hl_set_kill(True, reason)
        if log_event is not None:
            log_event("kill", f"agent 到期保护：{reason}")
    except Exception as e:                                        # noqa: BLE001
        logger.error("[agent] 置 kill 失败：%s", e)


def refresh_state(db: Any, cfg: Any, info: Any, *, address: str = "", name: str = AGENT_NAME,
                  now_ms: Optional[float] = None, reset_cleanup: bool = False) -> dict:
    """只重算并写 ``hl_agent_state``（不告警、不清仓）。

    用于「刚更新私钥」之后立刻刷新面板：那一刻新 key 还没被任何一轮 tick 用过，
    但要马上在面板上看到新地址/新有效期，不能等下一个整点。
    """
    now_ms = float(now_ms if now_ms is not None else time.time() * 1000.0)
    prev = read_state(db)
    source = "none"
    until: Optional[int] = None
    agent_addr = ""
    if str(address or "").strip():
        try:
            row = pick_agent(norm_agents(info.extra_agents(address)), name)
            if row is not None:
                source = "api"
                until = row.get("valid_until")
                agent_addr = str(row.get("address") or "")
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[agent] 刷新状态时 extraAgents 读取失败：%s", e)
    if until is None:
        m = manual_until(db, cfg)
        if m is not None:
            until, source = m, "manual"
    days = days_left(until, now_ms) if until is not None else None
    state = {"address": agent_addr, "valid_until_ms": until, "days_left": days,
             "checked_ms": int(now_ms),
             "cleanup": "none" if reset_cleanup else str(prev.get("cleanup") or "none"),
             "source": source, "level": level(days) if source != "none" else "unknown"}
    write_state(db, state)
    return state


def renew_key(db: Any, cfg: Any, notifier: Any, info: Any, new_key: Any, *,
              address: str = "", current_key: str = "", name: str = AGENT_NAME,
              prefix: str = "", now_ms: Optional[float] = None,
              log_event: Optional[Callable[..., None]] = None) -> tuple:
    """校验并**热切换**执行器用的 API 私钥。返回 (ok, 人话文案)。

    三关校验（任一不过就不写库）：
      1. 新私钥派生出的地址 ≠ 当前 agent 地址（防「填了旧的那把」白忙一场）
      2. extraAgents 里 name 精确等于 Hermes 的那行 address == 新地址（**必须先在
         HL 网页完成授权**，否则新 key 一写进去执行器立刻失去下单能力）
      3. 该行 validUntil > 现在（授权得是有效的）

    私钥只在内存里过一遍 `eth_account` 派生地址，随后写进 DB（执行器要用）；
    **返回值、日志、alert_logs 一律不含私钥任何片段** —— 这里只出现派生地址与日期。

    成功后立即刷新 ``hl_agent_state`` 并推送一条 `agent_renew` 提醒。
    """
    now_ms = float(now_ms if now_ms is not None else time.time() * 1000.0)
    text = str(new_key or "").strip().strip('"').strip("'")
    if not text:
        return False, ("没有收到私钥：请在下方输入框粘贴新 API 私钥"
                       "（HL 网页 More → API 生成的 0x 开头的 64 位十六进制）")
    # 首尾空白/换行是复制粘贴常态（strip 已去掉）；**中间**还有换行 = 粘了两行东西
    if "\n" in text or "\r" in text:
        return False, "私钥中间含换行 —— 请只复制那一串 0x…，一次只粘一把"
    addr, why = derive_address(text)
    if not addr:
        return False, why

    # ---- 当前 agent 地址：优先「现私钥派生」（extraAgents 里那行在续期后已经是**新**
    # 地址，拿它当「当前」会把正常续期误判成「填了同一把」）
    prev_addr = str(derive_address(str(current_key or "").strip())[0] or "") \
        if str(current_key or "").strip() else str(read_state(db).get("address") or "")
    if addr.lower() == prev_addr.lower() and prev_addr:
        return False, (f"这就是当前正在用的那把私钥（地址 {mask_addr(addr)} 未变），无需更换。"
                       "若刚在 HL 网页续期过，请确认复制的是**新生成**的私钥。")

    # ---- 新 key 必须在 HL 授权列表里（同名、同地址、未过期）
    agents: Optional[List[dict]] = [] if str(address or "").strip() else None
    api_err = ""
    if agents is not None:
        try:
            agents = norm_agents(info.extra_agents(address))
        except Exception as e:                                    # noqa: BLE001
            api_err = type(e).__name__
            agents = None
    row = pick_agent(agents, name) if agents is not None else None

    if agents is None:
        return False, (f"取不到 HL 的授权列表（{api_err or '接口异常'}），为安全起见**没保存任何东西** —— "
                       "无法确认新私钥已在链上授权时写进去，会立刻失去下单能力。请稍后重试。")
    if row is None:
        return False, (f"HL 授权列表里没有名字叫 {name} 的 API 钱包。请先在 HL 网页 "
                       f"More → API 用**同名 {name}** 完成授权（复制新私钥），再回来保存。")
    if str(row.get("address") or "").lower() != addr.lower():
        return False, (f"HL 授权列表里同名 {name} 的地址是 {mask_addr(row.get('address'))}，"
                       f"与你粘贴的这把（{mask_addr(addr)}）不是同一个。请先在 HL 网页用同名 "
                       f"{name} 完成授权，再回来保存。")
    until = _ms(row.get("valid_until"))
    if until is None or until <= now_ms:
        when = fmt_bj(until) if until else "未知"
        return False, (f"这把私钥对应的授权已经失效（有效期至 {when}，北京时间）。"
                       f"请先在 HL 网页用同名 {name} 重新授权（有效期选最长的 180 天），再回来保存。")

    # ---- 写入 + 立即刷新状态 + 推送
    try:
        db.set_config(CFG_PRIVATE_KEY, text)
    except Exception as e:                                        # noqa: BLE001
        logger.error("[agent] 写入新私钥失败：%s", type(e).__name__)
        return False, f"保存失败（{type(e).__name__}），私钥未被修改，请重试"
    state = refresh_state(db, cfg, info, address=address, name=name,
                          now_ms=now_ms, reset_cleanup=True)
    days = state.get("days_left")
    tail = f"，还剩 {days} 天" if days else ""
    msg = (f"{prefix}✅ API 已更新：新地址 {mask_addr(addr)}，有效期至 {fmt_bj(until)}"
           f"（北京时间）{tail}。下一轮起执行器就用新私钥签名。")
    _alert(db, notifier, KIND_RENEW, msg, dedup=False)
    if log_event is not None:
        try:
            log_event("decision", f"面板更新 API 私钥：新地址 {mask_addr(addr)}")
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[agent] 写事件失败：%s", e)
    return True, (f"API 已更新：新地址 {mask_addr(addr)}，有效期至 {fmt_bj(until)}（北京时间）{tail}。"
                  "下一轮起用新私钥，无需重启。")


__all__ = [
    "AGENT_NAME", "CFG_STATE", "CFG_MANUAL_UNTIL", "CFG_CLEANUP", "CFG_PRIVATE_KEY",
    "KIND_ALERT", "KIND_CLEANUP", "KIND_RENEW", "LEVEL_TXT", "DEDUP_S",
    "THRESH_NEAR", "THRESH_URGENT", "THRESH_CLEANUP",
    "pick_agent", "norm_agents", "days_left", "level", "fmt_bj", "mask_addr",
    "derive_address", "read_state", "write_state", "cleanup_enabled", "cleanup_switch",
    "manual_until", "flatten_all", "check_once", "renew_key", "refresh_state",
]
