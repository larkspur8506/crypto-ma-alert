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
   → 逐仓市价平 → 复查 clearinghouseState → **本地仓位同步关账**（不关账就会让
   对账每 5 分钟报一次 mismatch → 永久 kill 循环，续期后也解不开）。
4. **清仓的互斥**：调用方（`app.main._run_agent_cleanup`）持 ``_tick_lock`` 与 tick
   互斥，执行器侧再用**进程级** ``_op_lock``（见 `app.hl.executor`，所有实例共用一把）
   与 run/on_entry/on_exit 串行；本模块另提供 `cleanup_session()` 标记，让补挂止损
   这类「顺手动交易所」的路径在清仓期间主动让路。
5. 本模块不 import `app.main`（避免循环依赖），交易所客户端由调用方注入。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from app import engine
from app.hl import MODE_DRY, MODE_OFF, PURPOSE_ENTRY
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
# 四种文案**必须分 kind**：共用 kind 会被 24h 去重互相压制（升级通知被吞、清仓成功
# 被 24 小时前的失败通知压住），而这四条的性质完全不同。
KIND_NEAR = "agent_expiry_near"          # ≤14 天提醒（24h 去重）
KIND_URGENT = "agent_expiry_urgent"      # ≤7 天升级（独立 kind → 不被 near 压制）
KIND_EXPIRED = "agent_expiry_expired"    # 真过期后的续期提醒（24h 去重）
KIND_CLEANUP_OK = "agent_expiry_cleanup_ok"       # 清仓成功（**边沿触发**，只推一次）
KIND_CLEANUP_FAIL = "agent_expiry_cleanup_fail"   # 清仓失败（24h 去重）
KIND_CLEANUP_OFF = "agent_expiry_cleanup_off"     # 开关关/执行器 off：只提醒，不清仓
KIND_RENEW = "agent_renew"
# 旧 kind：保留只读兼容（历史 alert_logs 行还在，面板/记录页照样认）
KIND_ALERT_LEGACY = "agent_expiry_alert"
KIND_CLEANUP_LEGACY = "agent_expiry_cleanup"

DEDUP_S = 86400.0        # 同一 kind 24h 内只推一次（成功才记，失败下轮重试）
OFF_ALERT_DEDUP_S = 7 * 86400.0   # off 模式「执行器未启用」提醒 7 天节流（用户是故意的，别天天吵）

# 动作阈值（天数，自然日、北京时间口径；**档位判定用毫秒**，天数只用于展示）
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
    "no_expiry": "不限期",
}

# ---------------------------------------------------------------- 清仓会话标记
# 清仓要「撤全部挂单 → 平仓 → 复查」，同进程里还有 hl_monitor 的 run()（5 分钟一轮）
# 会做「补挂止损 / 市价平」。两边交错 = 刚撤的止损被挂回去、刚平的仓又被补一单。
# 执行器侧已有进程级 _op_lock 串行，这里再加一个**可读的状态标记**：让那些
# 「有事没事顺手补一下」的路径（_repair_missing_stop）在清仓期间主动让路，
# 而不是靠锁的先后顺序赌运气。
_cleanup_depth = 0
_cleanup_lock = threading.Lock()


def cleanup_in_progress() -> bool:
    """当前进程是否正在做到期清仓（供执行器决定要不要动交易所）。"""
    return _cleanup_depth > 0


@contextmanager
def cleanup_session() -> Iterator[None]:
    """清仓期间置标记（可重入、异常安全）。"""
    global _cleanup_depth
    with _cleanup_lock:
        _cleanup_depth += 1
    try:
        yield
    finally:
        with _cleanup_lock:
            _cleanup_depth = max(0, _cleanup_depth - 1)


# ---------------------------------------------------------------- 解析 / 换算
def pick_agent(agents: Any, name: str = AGENT_NAME,
               now_ms: Optional[float] = None) -> Optional[dict]:
    """从 extraAgents 里挑出 name **精确等于** `name` 的那行。

    必须精确匹配：用户可能同时授权了多个 API 钱包（如 Hermes / gridbot），
    拿「第一个」或「最长的有效期」会读成别人的到期日。

    同名多行时（HL 允许同名 approve，历史行可能残留）**不能取第一条**：
      - 有效行（valid_until 非空且 > now）里取 valid_until **最大**的；
      - 全都过期（或有效期未知）→ 取 valid_until 最大的那条（交给调用方判 expired）。
    """
    cands = [a for a in (agents or [])
             if isinstance(a, dict) and str(a.get("name") or "") == str(name)]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    ref = float(now_ms if now_ms is not None else time.time() * 1000.0)

    def _until(a: dict) -> int:
        return _ms(a.get("valid_until", a.get("validUntil"))) or 0

    live = [a for a in cands if _until(a) > ref]
    pool = live or cands
    return max(pool, key=_until)


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
    """剩余**自然日**（北京时间口径）。**只用于展示**，不再驱动动作档位。

    档位判定改用毫秒（见 `level_ms`）：到期当天 23:00 到期、中午 12:00 检查时
    自然日差是 0，但实际还剩 11 小时现货可平仓 —— 那天必须照样清仓/重试。
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


def days_left_display(valid_until_ms: Any, now_ms: Optional[float] = None) -> Optional[int]:
    """展示用剩余天数：向上取整（还剩 11 小时 = 「1 天」，不显示成 0 天）。"""
    vu = _ms(valid_until_ms)
    if vu is None:
        return None
    try:
        now_ms = float(now_ms if now_ms is not None else time.time() * 1000.0)
    except (TypeError, ValueError):
        return None
    remain = vu - now_ms
    if remain <= 0:
        return 0
    return int(-(-remain // 86_400_000.0))


def level_ms(valid_until_ms: Any, now_ms: Optional[float] = None) -> str:
    """按**毫秒**判动作档位（唯一真源）。

    - `now_ms >= until` → expired（真过期：执行器已经签不动单了）
    - 0 < 剩余 ≤ 3 天 → cleanup（清仓/重试窗口，含到期当天）
    - ≤7 天 urgent / ≤14 天 near / 其余 ok
    """
    vu = _ms(valid_until_ms)
    if vu is None:
        return "unknown"
    if now_ms is None:
        now_ms = time.time() * 1000.0
    try:
        ref = float(now_ms)
    except (TypeError, ValueError):
        return "unknown"
    remain = float(vu) - ref
    if remain <= 0:
        return "expired"
    if remain <= THRESH_CLEANUP * 86_400_000:
        return "cleanup"
    if remain <= THRESH_URGENT * 86_400_000:
        return "urgent"
    if remain <= THRESH_NEAR * 86_400_000:
        return "near"
    return "ok"


def level(days: Optional[int]) -> str:
    """剩余**自然日** → 档位（**仅展示/兼容用**，动作判定请用 `level_ms`）。

    保留这个函数是为了面板在只有 state.days_left 时能渲染；它现在把 0 天当成
    「今天到期、还在清理窗口」，不再判 expired —— 那正是 H1 的坑。
    """
    if days is None:
        return "unknown"
    if days < 0:
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
def _alert(db: Any, notifier: Any, kind: str, text: str, dedup: bool = True,
           window_s: float = DEDUP_S) -> bool:
    """窗口去重 + 成功后落库（**绝不先置去重再发送**：发送失败下轮还要重试）。

    dedup=False 用于「一次性事件 / 边沿事件」（如清仓成功、更新 API 成功）——
    这类事件本身只该在状态跳变时发生一次，再叠一层时间去重反而会把
    「失败 → 成功」的告警吞掉。
    """
    if dedup:
        try:
            if db.was_alerted(None, None, kind, within_s=window_s):
                logger.info("[agent] %s 窗口内已推过，跳过", kind)
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


def _days_txt(days: Optional[int]) -> str:
    """展示口径的剩余天数（向上取整；0 天 = 今天到期）。"""
    return "不到 1 天" if days == 0 else f"{days} 天"


def _text_near(until_ms: Any, days: Optional[int], prefix: str) -> str:
    return (f"{prefix}⚠️ API 有效期提醒：代理钱包 API 将于 {fmt_bj(until_ms)}（北京时间）到期，"
            f"还剩 {_days_txt(days)}。届时执行器无法开仓、平仓、撤单。\n"
            "请在 HL 网页 More → API 用同名 Hermes 重新授权（有效期可选最长 180 天），"
            "再到面板「API 有效期」粘贴新私钥。")


def _text_urgent(until_ms: Any, days: Optional[int], prefix: str) -> str:
    return (f"{prefix}🔴 API 有效期紧急：代理钱包 API 将于 {fmt_bj(until_ms)}（北京时间）到期，"
            f"只剩 {_days_txt(days)}。到期后执行器彻底失去下单能力，仓位将无法自动平掉。\n"
            "请尽快在 HL 网页 More → API 用同名 Hermes 重新授权，再到面板「API 有效期」粘贴新私钥。")


def _text_cleanup_off(until_ms: Any, days: Optional[int], prefix: str) -> str:
    return (f"{prefix}🔴 API 有效期紧急：代理钱包 API 将于 {fmt_bj(until_ms)}（北京时间）到期，"
            f"只剩 {_days_txt(days)}。\n"
            "「到期前自动清仓」已关闭，系统不会替你平仓 —— 到期后想平也平不了，请自行处理。\n"
            "（需要自动保护就到面板「参数」页打开这个开关。）")


def _text_cleanup_offline(until_ms: Any, days: Optional[int], prefix: str) -> str:
    """执行器没启用（off）—— 用户是故意的，不喊「清仓失败」。"""
    return (f"{prefix}⚠️ API 到期临近且执行器未启用（off）：代理钱包 API 将于 "
            f"{fmt_bj(until_ms)}（北京时间）到期，只剩 {_days_txt(days)}。\n"
            "当前不会自动撤单/平仓 —— 请自行决定是否手动处理（到期后想平也平不了）。")


def _text_expired(until_ms: Any, prefix: str) -> str:
    """只在**真过期**（now >= validUntil）时使用，文案与事实一致。"""
    return (f"{prefix}⛔ API 已失效：代理钱包 API 的有效期已于 {fmt_bj(until_ms)}（北京时间）截止，"
            "执行器现在开仓/平仓/撤单都会被拒，同时已暂停自动开新仓。\n"
            "请主钱包在 HL 网页 More → API 用同名 Hermes 授权新 API，"
            "然后到面板「API 有效期」粘贴新私钥。")


def _text_expired_soon(until_ms: Any, prefix: str) -> str:
    """过期前最后一次提醒（还剩不到 1 天，但**还没**过期）。"""
    return (f"{prefix}🔴 API 将在 {fmt_bj(until_ms)}（北京时间）失效（不到 1 天）。"
            "到期后执行器无法开仓、平仓、撤单，系统会在此之前清仓并暂停开新仓。\n"
            "请尽快在 HL 网页 More → API 用同名 Hermes 重新授权，再到面板「API 有效期」粘贴新私钥。")


def _text_cleanup_ok(until_ms: Any, n_closed: Optional[int], prefix: str) -> str:
    did = (f"已撤掉全部挂单并平掉 {n_closed} 个标的" if n_closed
           else "已撤掉全部挂单（当时没有持仓）")
    return (f"{prefix}✅ 已清仓（agent 到期保护）：代理钱包 API 将于 {fmt_bj(until_ms)}（北京时间）"
            f"到期，{did}，并已暂停开新仓。\n"
            "请主钱包在 HL 网页用同名 Hermes 授权新 API，然后到面板「API 有效期」粘贴新私钥。"
            "续期后到「开关」页解除 kill 即可恢复自动交易（本地仓位已同步关账，无需手工改库）。")


def _text_cleanup_dry(until_ms: Any, n_closed: Optional[int], prefix: str) -> str:
    """影子模式：一张单都没发，文案必须如实说「未发送任何指令」。"""
    return (f"{prefix}🧪 影子模式：API 将于 {fmt_bj(until_ms)}（北京时间）到期，"
            f"正式模式下此时会清掉 {n_closed or 0} 个标的。\n"
            "影子模式未向交易所发送任何指令（撤单/平仓都没发）；正式生效需切到 live 模式。")


def _text_cleanup_failed(until_ms: Any, err: str, prefix: str,
                         cancel_failed: Optional[List[dict]] = None) -> str:
    detail = ""
    if cancel_failed:
        items = []
        for f in cancel_failed[:5]:
            items.append(f"{f.get('coin') or '?'}#{f.get('oid')}（{f.get('error') or '未知原因'}）")
        detail = "\n未撤掉的挂单：" + "、".join(items)
        if len(cancel_failed) > 5:
            detail += f" 等 {len(cancel_failed)} 张"
    return (f"{prefix}⚠️ 自动清仓失败：{err}，请手动处理。{detail}\n"
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
    cancel_failed: List[dict] = []
    touched: Dict[str, float] = {}      # coin → 本次清仓里实际平掉的成交均价（跨重试累计）
    for _ in range(max(0, int(retries)) + 1):
        attempts += 1
        last_err = ""
        closed: List[dict] = []
        cancelled: List[int] = []
        cancel_failed = []

        # ---- 1) 撤掉全部挂单（先撤单：清仓期间挂着的止损单可能和新平仓单打架）
        #     读不到/不可信 → cancel_all_orders 返回 ok=False（M1）：宁可整轮重试，
        #     也不要在「挂单状态未知」时贸然平仓留一堆孤儿 reduce-only 单。
        if not dry:
            try:
                res = ex.cancel_all_orders()
            except Exception as e:                                # noqa: BLE001
                res = {"ok": False, "error": f"撤单异常：{type(e).__name__}"}
            if not res.get("ok"):
                cancel_failed = list((res.get("raw") or {}).get("failed") or [])
                last_err = f"撤单失败：{res.get('error') or '未知原因'}"
                _ev(f"清仓：{last_err}")
                continue
            cancelled = list((res.get("raw") or {}).get("oids") or [])
            cancel_failed = list((res.get("raw") or {}).get("failed") or [])
            _ev(f"清仓：已撤 {len(cancelled)} 张挂单"
                + (f"（{len(cancel_failed)} 张没撤掉）" if cancel_failed else ""))

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
            touched[coin] = _f(r.get("avg_px"), px)
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

        # ---- 5) 交易所侧确认清空 → **本地仓位同步关账**（H2）
        # 不关账的后果：交易所空仓、本地仍 status='open' → reconcile 每 5 分钟报
        # 「本地有仓但交易所无仓位」→ 重置 kill（对账异常）→ 续期并解 kill 后
        # 下一轮又被拉回去，形成永久 kill 循环。
        # 口径：交易所快照里**已不存在**的标的（不是「本次刚平掉的那些」）——
        # 上一轮清仓平了仓但关账失败/进程被杀时，这轮交易所已经空仓、closed 是空的，
        # 只按 closed 关账就会漏掉，永久错位。
        still = {str((p or {}).get("coin") or "") for p in (snap2.get("positions") or [])}
        prices = dict(touched)
        for p in (snap.get("positions") or []):
            c = str((p or {}).get("coin") or "")
            prices.setdefault(c, _f((p or {}).get("mark_px")))
        booked = _close_local_positions(db, params, still, prices, _ev)
        return {"ok": True, "error": "", "closed": closed, "attempts": attempts,
                "dry": False, "cancelled": len(cancelled),
                "closed_local": booked, "cancel_failed": cancel_failed}

    return {"ok": False, "error": last_err or "未知原因", "closed": [],
            "attempts": attempts, "dry": dry, "cancelled": 0,
            "closed_local": [], "cancel_failed": cancel_failed}


# 清仓关账用的 exit_reason：面板/统计据此区分「到期保护平仓」与策略离场
EXIT_REASON_EXPIRY = "agent_expiry_cleanup"


def _coin_of(db: Any, params: Any, symbol: str) -> Optional[str]:
    """symbol → HL 合约名（executor coin_map → 注册表；解析不出返回 None）。"""
    cmap = getattr(params, "coin_map", None) or {}
    if symbol in cmap:
        return cmap[symbol]
    try:
        from app import symbol_registry as registry
        return registry.hl_coin(symbol)
    except Exception:                                             # noqa: BLE001
        return None


def _entry_issued(db: Any, row: Any) -> bool:
    """这行是不是**真的发过** HL 入场单（与 executor.reconcile 同一口径）。"""
    try:
        o = db.hl_order(str(row["symbol"]), int(row["signal_bar_ms"] or 0), PURPOSE_ENTRY)
    except Exception:                                             # noqa: BLE001
        return True
    if o is None:
        return False
    return (o["status"] or "") in ("sent", "resting", "filled")


def _close_local_positions(db: Any, params: Any, still_on_exchange: Any,
                          prices: Dict[str, float],
                          ev: Optional[Callable[..., None]] = None) -> int:
    """把交易所已经没有仓的本地仓位关账（幂等；单行失败只记日志，不否定清仓结论）。

    - status='closed'、exit_reason='agent_expiry_cleanup'、auto_closed=1
      （沿用「引擎先关账、用户可在确认页覆盖真实平仓价」的既有语义）
    - exit_price 用**实际成交均价**（place_order 回报的 avg_px），没有回报才用
      标记价 / 开仓价兜底
    - 只处理 HL 管的行（`hl_status` 为 open/mismatch，或 hl_orders 里有真实入场单）；
      MEXC 人工行不碰（那是另一个场所的仓位）
    - 对应标的的挂单状态（hl_orders 里还 resting 的止损/离场单）同步标 cancelled
    - bar_state 不动：清仓不是「处理完这根 bar」，动它会让引擎跳过这根 bar 的信号
    """
    still = {str(c) for c in (still_on_exchange or set())}
    booked = 0
    try:
        rows = [r for r in db.recent_positions(200) if (r["status"] or "") in ("open", "pending")]
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[agent] 读本地仓位失败（无法关账）：%s", e)
        return 0
    for r in rows:
        coin = _coin_of(db, params, str(r["symbol"]))
        if not coin or coin in still:
            continue
        hl_managed = (r["hl_status"] or "") in ("open", "mismatch")
        if not hl_managed:
            # hl_status 还没写（成交→落库之间崩溃）时，靠「真的发过入场单」判定
            if (r["hl_status"] or "") != "" or not _entry_issued(db, r):
                continue
        px = _f(prices.get(coin)) or _f(r["actual_entry"]) or _f(r["entry_price"])
        try:
            # auto=True：exit_price 只是参考（真实价在 hl_fills），用户可在确认页覆盖
            db.close_position(int(r["id"]), px, EXIT_REASON_EXPIRY, auto=True)
            db.set_pos_hl(int(r["id"]), hl_status="closed")
            booked += 1
            if ev is not None:
                ev(f"清仓：本地仓位 #{r['id']} {r['symbol']} 已关账 @ {px:,.2f}")
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[agent] 本地仓位 #%s 关账失败：%s", r["id"], e)
        # 该标的还挂着的单（止损/离场）在交易所已被撤 —— 本地状态同步，别留 resting 幽灵单
        try:
            for o in db.hl_recent_orders(200):
                if (str(o["symbol"]) == str(r["symbol"])
                        and (o["status"] or "") in ("intent", "sent", "resting")):
                    db.hl_update_order(str(o["symbol"]), int(o["bar_ms"]),
                                       str(o["purpose"]), status="cancelled",
                                       error="agent 到期清仓：已撤销")
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[agent] 同步本地挂单状态失败：%s", e)
    return booked


# ---------------------------------------------------------------- 每轮检查
def resolve_until(db: Any, cfg: Any, info: Any, *, address: str = "", name: str = AGENT_NAME,
                  now_ms: Optional[float] = None) -> Tuple[str, Optional[int], str, bool]:
    """解析「当前生效的有效期」。返回 (source, until_ms, agent_addr, row_found)。

    **H4 的硬口径**：extraAgents 里 name 精确命中的那一行就是权威来源 ——
    它的 `validUntil` 为 null/0 表示**不限期授权**，此时：
      - source = "api"、until = None（面板显示「不限期」）
      - **绝不**回落到手工兜底（兜底值往往是几个月前填的陈旧数字，拿它算
        「≤3 天」会直接把仓位平掉 —— 这是 H4 的真身）
      - 不告警、不清仓
    只有「一行都没命中」（name 不匹配 / 接口失败 / 接口挂了）才回落手工兜底。
    """
    source = "none"
    until: Optional[int] = None
    agent_addr = ""
    row_found = False
    if str(address or "").strip():
        try:
            row = pick_agent(norm_agents(info.extra_agents(address)), name, now_ms)
            if row is not None:
                row_found = True
                source = "api"
                agent_addr = str(row.get("address") or "")
                until = row.get("valid_until")
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[agent] extraAgents 读取失败，回落手工兜底：%s", e)
    if not row_found:
        m = manual_until(db, cfg)
        if m is not None:
            until, source = m, "manual"
    return source, until, agent_addr, row_found


def check_once(db: Any, cfg: Any, notifier: Any, info: Any, *,
               address: str = "", name: str = AGENT_NAME,
               now_ms: Optional[float] = None,
               cleanup: Optional[Callable[..., dict]] = None,
               prefix: str = "",
               log_event: Optional[Callable[..., None]] = None,
               mode: str = "") -> dict:
    """一轮到期检查：读有效期 → 判档位 → 按动作表告警/清仓 → 每轮都写 state。

    `cleanup` 是注入的清仓器（`cleanup(reason=...) -> dict`），由 app.main 提供并负责
    持 ``_tick_lock``；缺省视为「未接线」→ 记 failed 并喊人（宁可吵，也别静默）。

    `mode`（off/dry/live）只影响文案与 off 的行为：off 是用户主动选的「只提醒」，
    不能每小时喊「自动清仓失败」—— 那既不是失败，也不该进重试循环。

    返回本轮写入的 state（含 address / valid_until_ms / days_left / checked_ms /
    cleanup / source / level）。
    """
    now_ms = float(now_ms if now_ms is not None else time.time() * 1000.0)
    prev = read_state(db)
    prev_cleanup = str(prev.get("cleanup") or "none")
    if prev_cleanup not in ("none", "done", "failed", "skipped"):
        prev_cleanup = "none"

    # ---- 1) 有效期：extraAgents（name 精确匹配）→ 手工兜底 → 未知
    source, until, agent_addr, row_found = resolve_until(
        db, cfg, info, address=address, name=name, now_ms=now_ms)
    if source == "manual":
        agent_addr = agent_addr or str(prev.get("address") or "")

    # ---- 2) 档位：**毫秒精确**（H1）。days 只用于展示。
    days = days_left_display(until, now_ms) if until is not None else None
    if source == "none":
        lvl = "unknown"
    elif until is None:
        # 命中授权行但 validUntil 为空 = 不限期（source=api）
        lvl = "no_expiry"
    else:
        lvl = level_ms(until, now_ms)

    state = {"address": agent_addr, "valid_until_ms": until, "days_left": days,
             "checked_ms": int(now_ms), "cleanup": prev_cleanup, "source": source,
             "row_found": row_found}

    # ---- 3) 动作表
    try:
        if lvl == "no_expiry":
            # 不限期授权：什么都不做（不清仓、不告警、不读兜底）
            logger.info("[agent] 授权行 validUntil 为空 → 不限期，无需处理")
            if state["cleanup"] != "done":
                state["cleanup"] = "none"
        elif lvl == "near":
            _alert(db, notifier, KIND_NEAR, _text_near(until, days, prefix))
        elif lvl == "urgent":
            # 独立 kind：升级通知不会被 near 的 24h 去重压住（M4）
            _alert(db, notifier, KIND_URGENT, _text_urgent(until, days, prefix))
        elif lvl == "cleanup":
            _do_cleanup(db, cfg, notifier, cleanup, state, until, days, prefix,
                        log_event, mode)
        elif lvl == "expired":
            # 到期后 API 已经没权限了：置 kill（若未置）+ 每 24h 提醒一次
            _kill(db, "agent 已失效：暂停开新仓", log_event)
            _alert(db, notifier, KIND_EXPIRED, _text_expired(until, prefix))
        else:
            logger.info("[agent] 有效期检查：%s（source=%s，剩余 %s 天）", lvl, source, days)
            # 有效期还远：上一轮的 done/failed 自然过期，别让面板永远显示「已清仓」
            if state["cleanup"] in ("failed", "skipped"):
                state["cleanup"] = "none"
    finally:
        # ---- 4) 每轮写 state（即使上面告警/清仓抛异常也要落，面板才看得见）
        state["level"] = lvl
        write_state(db, state)
    return state


def _do_cleanup(db: Any, cfg: Any, notifier: Any, cleanup: Optional[Callable[..., dict]],
                state: dict, until: Any, days: Optional[int], prefix: str,
                log_event: Optional[Callable[..., None]], mode: str) -> None:
    """cleanup 档位的动作：开关关 → 提醒；off → 提醒(7 天节流)；否则真清仓。"""
    if not cleanup_switch(db, cfg):
        # 开关关：照常告警，并明写「不会替你平仓」
        _alert(db, notifier, KIND_CLEANUP_OFF, _text_cleanup_off(until, days, prefix))
        if state["cleanup"] != "done":       # 已经清过仓的事实不能被开关状态抹掉
            state["cleanup"] = "none"
        return
    if str(mode or "").lower() == MODE_OFF:
        # 执行器 off 是用户主动选的「只提醒」，不是清仓失败：
        # 标 skipped + 一条「请自行决定」的提醒，7 天节流，**不进失败重试循环**。
        if state["cleanup"] != "skipped":
            state["cleanup"] = "skipped"
        _alert(db, notifier, KIND_CLEANUP_OFF, _text_cleanup_offline(until, days, prefix),
               window_s=OFF_ALERT_DEDUP_S)
        return
    if state["cleanup"] == "done":
        # 之前已清完：**不再每 24h 重推**（M4 去掉这条刷屏）。
        # 只在「清仓成功后 state 写库失败」这类极端情况下补一次：
        # 由 _alert 的 24h 窗口兜住，不会反复刷。
        return

    # ★ 先 kill 置位（在任何撤单/平仓之前）：清仓中途失败/进程被杀，
    #   下一轮 tick 也绝不会再开新仓。
    _kill(db, "agent 到期保护：清仓前先暂停开新仓", log_event)
    res: dict = {}
    if cleanup is None:
        res = {"ok": False, "error": "清仓器未接线（执行器未装配）"}
    else:
        try:
            res = cleanup(reason=f"agent 剩余 {days} 天到期") or {}
        except Exception as e:                                    # noqa: BLE001
            logger.exception("[agent] 清仓异常：%s", e)
            res = {"ok": False, "error": f"清仓异常：{type(e).__name__}"}
    n = len(res.get("closed") or [])
    if res.get("ok") and not res.get("dry"):
        state["cleanup"] = "done"
        # 边沿触发：none/failed → done 才推（dedup=False，绝不被 24h 窗口吞掉）
        _alert(db, notifier, KIND_CLEANUP_OK, _text_cleanup_ok(until, n, prefix),
               dedup=False)
    elif res.get("ok"):
        # 影子模式：一张单都没发，文案如实说「未发送任何指令」，不写 done
        _alert(db, notifier, KIND_CLEANUP_FAIL,
               _text_cleanup_dry(until, n, prefix))
    else:
        state["cleanup"] = "failed"
        _alert(db, notifier, KIND_CLEANUP_FAIL,
               _text_cleanup_failed(until, str(res.get("error") or "未知原因"), prefix,
                                    res.get("cancel_failed") or []))


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
    # 与 check_once 同一口径（H4：命中行 validUntil 为空 = 不限期，不回落地兜底）
    source, until, agent_addr, _row = resolve_until(
        db, cfg, info, address=address, name=name, now_ms=now_ms)
    days = days_left_display(until, now_ms) if until is not None else None
    if source == "none":
        lvl = "unknown"
    elif until is None:
        lvl = "no_expiry"
    else:
        lvl = level_ms(until, now_ms)
    state = {"address": agent_addr, "valid_until_ms": until, "days_left": days,
             "checked_ms": int(now_ms),
             "cleanup": "none" if reset_cleanup else str(prev.get("cleanup") or "none"),
             "source": source, "level": lvl}
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

    # ---- 当前在用的地址：优先「现私钥派生」，其次 DB 里那把的派生地址。
    # **不能**拿 state.address 当「当前」—— 那只是 HL 授权列表里的地址：
    # 网页上用同名新 key 授权后，它立刻就是**新**地址，拿它比对会把正常续期
    # 误判成「填了同一把」。current_key/DB 都为空 = 首次配置 → 没有「当前」可比，
    # 跳过这一关（否则第一次录入会被自己挡住）。
    cur_key = str(current_key or "").strip()
    if not cur_key:
        try:
            cur_key = str(db.get_config_str(CFG_PRIVATE_KEY, "") or "").strip()
        except Exception:                                         # noqa: BLE001
            cur_key = ""
    first_time = not cur_key
    prev_addr = str(derive_address(cur_key)[0] or "") if cur_key else ""
    if first_time:
        logger.info("[agent] 首次配置 API 私钥（库里没有现成私钥），跳过「同地址」检查")
    elif addr.lower() == prev_addr.lower() and prev_addr:
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
    rows = [a for a in (agents or []) if str(a.get("name") or "") == str(name)]
    # 同名多行（M6）：优先挑**地址就是这把新 key** 的那行 —— 用户刚在网页上
    # 用同名授权成功，行里可能还残留同名的旧行；取第一条会把续期挡下来。
    mine = [a for a in rows if str(a.get("address") or "").lower() == addr.lower()]
    row = (max(mine, key=lambda a: _ms(a.get("valid_until")) or 0) if mine
           else pick_agent(agents, name, now_ms)) if agents is not None else None

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
    "KIND_NEAR", "KIND_URGENT", "KIND_EXPIRED", "KIND_CLEANUP_OK", "KIND_CLEANUP_FAIL",
    "KIND_RENEW", "KIND_ALERT_LEGACY", "KIND_CLEANUP_LEGACY",
    "LEVEL_TXT", "DEDUP_S", "OFF_ALERT_DEDUP_S", "EXIT_REASON_EXPIRY",
    "THRESH_NEAR", "THRESH_URGENT", "THRESH_CLEANUP",
    "pick_agent", "norm_agents", "days_left", "days_left_display", "level", "level_ms",
    "fmt_bj", "mask_addr", "derive_address", "read_state", "write_state",
    "cleanup_enabled", "cleanup_switch", "manual_until", "flatten_all", "check_once",
    "renew_key", "refresh_state", "resolve_until", "cleanup_in_progress", "cleanup_session",
]
