"""FastAPI 应用：定时检查 + 合并管理面板 + 确认页（签名链接，无需登录）。

面板入口（合并版，见 docs/ADMIN_MERGE_PLAN.md 第 2 节）：
    GET /admin?k=<ADMIN_TOKEN>&tab=overview|params|switch|records
    POST /admin/params、/admin/switch/mode、/admin/switch/notify、/admin/switch/kill
旧地址兼容：/hl、/admin/rules、/admin/logs 的 GET 全部 302 到对应 Tab；
           /admin/rules 的 POST 保留（继续写 DB，老书签的表单还能提交）。
保留不动：/c/{pid} 确认页、/api/hl/* 四个 JSON 接口、/admin/hl/mode 与 /admin/hl/kill
          （runbook 里的 curl 还在用）、/admin/tick。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from dataclasses import replace
from urllib.parse import quote as _urlquote
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.middleware.gzip import GZipMiddleware

from app import engine, market, posfields, sizing, strategy
from app.config import Config
from app.db import DB
from app.hl import pnl as hl_pnl
from app.hl import funding as hl_funding
from app.notify import get_notifier
from app.ui_theme import (THEME_CSS, normalize_theme, page_shell, PRODUCT_NAME)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("cma")

DB_PATH = os.environ.get("DB_PATH", "/app/data/crypto_ma.db")
db = DB(DB_PATH)
config = Config()
notifier = get_notifier(config)
scheduler = None

# gzip 的起压门槛（见 add_middleware 处的理由）。之所以不叫 min_size：Starlette >=0.20
# 把这个参数从 min_size 改名成 minimum_size（fastapi>=0.110 依赖的版本都是新名字）。
GZIP_MIN_SIZE = 1024

PAGE_CSS = """
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
     margin:0;padding:16px;background:var(--bg);color:var(--fg);line-height:1.5}
h1{font-size:18px;margin:0 0 12px}h2{font-size:15px;margin:18px 0 8px;color:var(--h2)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left}
th{color:var(--muted);font-weight:500}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:12px}
.ok{color:var(--pos)}.bad{color:var(--neg)}.muted{color:var(--muted);font-size:12px}
a.btn,button{display:inline-block;background:var(--btn);color:#fff;border:0;border-radius:6px;
     padding:10px 14px;font-size:14px;text-decoration:none;margin:4px 6px 4px 0;cursor:pointer}
button.gray{background:var(--btn-gray)}button.red{background:#b91c1c}
input{background:var(--input);border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:8px;width:130px}
pre{background:var(--input);border:1px solid var(--line);border-radius:6px;padding:10px;overflow-x:auto;font-size:12px}
</style>
"""


def _req_theme(request: Request) -> str:
    """从请求 cookie 读主题（签名确认页无登录态，主题是纯展示偏好，跟 cookie 走）。"""
    try:
        return normalize_theme(request.cookies.get("cma_theme"))
    except Exception:                                            # noqa: BLE001
        return "auto"


def page(title: str, body: str, request: "Request | None" = None,
         theme: "str | None" = None) -> HTMLResponse:
    t = theme if theme is not None else (_req_theme(request) if request is not None else "auto")
    return HTMLResponse(page_shell(t, title, body, head_extra=PAGE_CSS))


def current_config() -> Config:
    return config.apply_db(db.get_config())


# ---------------------------------------------------------------- HL 执行器装配

def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def hl_mode() -> str:
    """off / dry / live。DB 值（面板/脚本可写）优先于 env。默认 off。"""
    raw = str(db.get_config().get("hl_exec_mode", "") or _env("HL_EXEC_MODE", "off")).strip().strip('"')
    return raw if raw in ("off", "dry", "live") else "off"


def hl_private_key() -> str:
    """执行器用的 agent 私钥：**DB（面板「更新 API」写入）优先，其次 env**。

    这是全仓唯一读私钥明文的入口 —— 调用方只许把它交给 HlExchange 的构造函数，
    绝不许写进日志、事件、alert_logs 或 HTTP 响应。
    """
    try:
        db_key = str(db.get_config_str("hl_private_key", "") or "").strip()
    except Exception as e:                                           # noqa: BLE001
        logger.warning("读 DB 私钥失败（回落 env）：%s", type(e).__name__)
        db_key = ""
    return db_key or _env("HL_PRIVATE_KEY")


def hl_params():
    """从 env + DB 组装 ExecParams。私钥 DB > env（面板可热切换），其余读 env/配置。"""
    from app.hl.executor import DEFAULT_COIN_MAP, ExecParams
    try:
        coin_map = json.loads(_env("HL_COIN_MAP", "")) or dict(DEFAULT_COIN_MAP)
    except ValueError:
        coin_map = dict(DEFAULT_COIN_MAP)

    def f(key: str, default: float) -> float:
        try:
            return float(_env(key, str(default)))
        except ValueError:
            return default

    cfg = current_config()
    base = hl_info_base()
    return ExecParams(
        mode=hl_mode(),
        account_address=_env("HL_ACCOUNT_ADDRESS"),
        private_key=hl_private_key(),
        base_url=base,
        coin_map=coin_map,
        leverage=cfg.leverage,
        risk_pct=cfg.risk_pct,
        # 空单风险减半：DB > env > 默认关（Config 已解析好布尔位，缺键/脏值都退回
        # 「关」= 现状）。与 pnl_from_fills / fill_close 同一种「从面板配置读布尔」
        # 的装配模式，只是默认方向相反：这是新增的风险政策，默认必须维持原行为。
        short_risk_half=bool(getattr(cfg, "short_risk_half", False)),
        equity=cfg.equity,
        # 口径 DB > env > 代码默认：Config 已解析好，不要再单独读 env（否则 DB 值会被 env 压掉）
        equity_mode=str(getattr(cfg, "equity_mode", "follow") or "follow"),
        equity_min=float(getattr(cfg, "equity_min", 20.0) or 0.0),
        notify_fills=push_on("fill_push"),
        # 净 PnL 口径（PR-3）：默认关。DB > env（Config.apply_db 已解析好），
        # 与 fill_push 同一种「从面板配置读布尔」的模式。
        pnl_from_fills=bool(getattr(cfg, "hl_pnl_from_fills", False)),
        # 交易所侧已平仓 → 对账按成交价关账（2026-10-22 事故修复）：DB > env > 默认开。
        # 缺键/脏值一律按「开」（这正是修复本身；关掉它等于回到误 kill 的旧行为），
        # 与 pnl_from_fills 同一种「从面板配置读布尔」的装配模式。
        fill_close=bool(getattr(cfg, "hl_fill_close", True)),
        # 对账失败自动消化（宽限期 + 窄口径自解 kill）：与 fill_close 同一种装配模式。
        # 脏值/缺键 → 按开（这是修复本身；关掉等于回到「没证据就 kill」的旧行为）。
        close_grace=bool(getattr(cfg, "hl_close_grace", True)),
        close_grace_s=_grace_seconds(cfg),
        kill_autoclear=bool(getattr(cfg, "hl_kill_autoclear", True)),
        # churn 熔断阈值（PR-4）：DB > env > 默认（Config 已解析好，不再单独读 env）。
        # 与 fill_push / pnl_from_fills 同一种装配模式；脏值/缺键退回 5（不静默关保护）。
        churn_max=_churn_max(cfg),
        # 出入金流水同步间隔（秒）：DB > env > 默认 1800。盈亏曲线要剔除出入金，
        # 数据源是交易所账本；**只有参数、没有面板开关**（用户要求）。
        # 脏值/缺键退回 1800（不退回 300：调接口不必比账本事件还勤）。
        flow_sync_s=_flow_sync_s(cfg),
        # 回撤熔断（DD Guard）：DB > env > 默认（Config 已解析好）。缺键按「开」，
        # 阈值 0 = 按 risk_pct 档位自动（1.5%→25/30、2.5%→30/40、3%→40/50）。
        # 与 fill_close/kill_autoclear 同一种「从面板配置读布尔」的装配模式。
        dd_guard=bool(getattr(cfg, "hl_dd_guard", True)),
        dd_warn_pct=_dd_pct(cfg, "hl_dd_warn_pct"),
        dd_kill_pct=_dd_pct(cfg, "hl_dd_kill_pct"),
        # 死手开关默认关闭：识别不了的值也按「关」处理（安全侧默认，与推送开关的 fail-open 相反）
        use_schedule_cancel=str(db.get_config_str(
            "hl_use_schedule_cancel", _env("HL_USE_SCHEDULE_CANCEL", "0")) or "0"
        ).strip().lower() in ("1", "true", "yes", "on"),
        max_slippage_pct=f("HL_MAX_SLIPPAGE_PCT", 0.003),
        # 名义上限默认 0 = 不设限（用户拍板）；要给暴露度加护栏就把 env 设回正数
        max_notional_mult=f("HL_MAX_NOTIONAL_MULT", 0.0),
        max_total_notional_mult=f("HL_MAX_TOTAL_NOTIONAL_MULT", 0.0),
        max_orders_per_day=int(f("HL_MAX_ORDERS_PER_DAY", 4)),
        max_fail_streak=int(f("HL_MAX_FAIL_STREAK", 3)),
        max_daily_loss_pct=f("HL_MAX_DAILY_LOSS_PCT", 0.05),
        deadman_minutes=int(f("HL_DEADMAN_MINUTES", 30)),
        min_notional=f("HL_MIN_NOTIONAL", 10.0),
        code_rev=_env("HL_CODE_REV", os.environ.get("CODE_REV", "")),
    )


_hl_info = None


def _churn_max(cfg) -> int:
    """churn 熔断阈值（PR-4）。脏值/缺键一律退回 5（默认保护），**不**退回 0 ——
    0 的语义是「关闭熔断」，一个读坏的字符串不该悄悄把保护关掉（fail-closed）。"""
    raw = getattr(cfg, "hl_churn_max", None)
    try:
        n = int(float(str(raw).strip().strip('"').strip("'")))
    except (TypeError, ValueError):
        return 5
    return n if n >= 0 else 5


GRACE_DEFAULT_S = 900.0


def _grace_seconds(cfg) -> float:
    """平仓宽限秒数。脏值/缺键/越界一律退回 900（15 分钟）。

    与 `_churn_max` 同一个理由：读坏的字符串不该把宽限关成 0 秒（= 第一轮没证据
    就 kill，回到要修的那个行为），也不该放大成一天（期间该标的禁止开新仓）。
    """
    raw = getattr(cfg, "hl_close_grace_s", None)
    try:
        v = float(str(raw).strip().strip('"').strip("'"))
    except (TypeError, ValueError):
        return GRACE_DEFAULT_S
    lo, hi = 300.0, 3600.0
    return v if lo <= v <= hi else GRACE_DEFAULT_S


def _flow_sync_s(cfg) -> int:
    """出入金流水同步间隔（秒）。脏值/缺键退回 1800；出界值收敛到 300~86400
    （与 Config._RANGES 同一区间：读侧不该绕过配置校验自己放行一个越界值）。"""
    raw = getattr(cfg, "hl_flow_sync_s", None)
    try:
        n = int(float(str(raw).strip().strip('"').strip("'")))
    except (TypeError, ValueError):
        return 1800
    return min(86400, max(300, n))


def _dd_pct(cfg, key: str) -> float:
    """回撤阈值覆盖（0 = 按 risk_pct 档位自动）。脏值/缺键/越界一律退回 0（自动）。

    规则**不在这里**：范围和「不符合就退回自动」的唯一真相是
    `app/hl/dd.py:norm_override`（规格 §6 低4）。这里只是装配时的一个读入口。
    为什么不退回一个具体数字：档位是「跟着 risk_pct 走」的，抄一个 0.30 进这里
    等于把 3% 档的账户钉在 2.5% 档的线上（那档 P(DD>30%)=96%，必然误触发）。
    越界值退回 0 而不是钳到边界：钳值会造出一个用户没写过的阈值。
    """
    from app.hl import dd as hl_dd
    return hl_dd.norm_override(getattr(cfg, key, None))



def hl_info_base() -> str:
    """只读客户端的 base_url。独立成函数：hl_params() 装配执行器时也要用它，
    但私钥/模式的变动不该触发只读客户端重建。"""
    if _env("HL_TESTNET", "0") in ("1", "true"):
        return "https://api.hyperliquid-testnet.xyz"
    return _env("HL_BASE_URL", "https://api.hyperliquid.xyz")


def get_hl_info():
    """只读客户端（面板/看护/执行器共用；无凭据也能用）。

    **不带缓存**：执行器、tick、对账、看护消费的必须是实时读数（fail-closed 的前提）。
    面板要省请求请用 `panel_hl_info()` —— 它是本函数返回值的一层 TTL 包装，只存在于面板依赖里。
    """
    global _hl_info
    if _hl_info is None:
        from app.hl.info import HlInfo
        _hl_info = HlInfo(base_url=hl_info_base())
    return _hl_info


_panel_info = None


def panel_hl_info():
    """面板专用的只读客户端包装（`snapshot`/`all_mids` 带 10s TTL 缓存）。

    只被 `_admin_deps()` 使用，`/api/hl/*` 与执行器仍走 `get_hl_info()` 的实时值。
    真身被换掉（测试 reload / 地址切换重建）时自动重建包装，避免拿旧对象的缓存回答新问题。
    """
    global _panel_info
    from app.panel_cache import PanelInfo
    raw = get_hl_info()
    if _panel_info is None or getattr(_panel_info, "raw", None) is not raw:
        _panel_info = PanelInfo(raw)
    return _panel_info


def invalidate_panel_cache() -> None:
    """面板写操作（参数/模式/kill）成功后调用：下一次渲染重新取真值。"""
    global _panel_info, _panel_invalidated_at
    _panel_invalidated_at = time.time()
    if _panel_info is not None:
        _panel_info.invalidate()
    # 阶段 B：快照里的六步解释/读数也可能被这次写操作改变（阈值、模式、kill），
    # 所以同时让「下一位访问者」绕过快照走一次实时，别让人刚改完参数就看到旧解释。
    if _snapshot_info is not None:
        _snapshot_info.invalidate()


# 最近一次面板写操作的时刻：/why 在写后 5 秒内强制现算（见 _why_rows）。
_panel_invalidated_at = 0.0


# ---------------------------------------------------------------- 面板快照（阶段 B）
# 调度器每轮把面板要显示的读数写进 `panel_snapshots`，面板只读库、零网络零重算。
# 细节与取舍见 app/panel_store.py 的模块头。

_snapshot_store = None
_snapshot_info = None

_PANEL_SNAPSHOT_OFF = ("0", "false", "no", "off")


def panel_snapshot_on() -> bool:
    """快照开关，默认**开**（PANEL_SNAPSHOT=0 时退回 A1 的 10s 进程内缓存）。"""
    return (os.environ.get("PANEL_SNAPSHOT", "1") or "1").strip().lower() not in _PANEL_SNAPSHOT_OFF


def panel_snapshot_store():
    """快照表门面（按 db 实例缓存；测试换库时自动重建）。"""
    global _snapshot_store
    from app.panel_store import PanelSnapshotStore
    if _snapshot_store is None or getattr(_snapshot_store, "db", None) is not db:
        _snapshot_store = PanelSnapshotStore(db)
    return _snapshot_store


def panel_snapshot_info():
    """面板专用的快照读取器：先读库，过期/缺失回落 `panel_hl_info()`（实时 + 10s）。"""
    global _snapshot_info
    from app.panel_store import SnapshotInfo
    raw = get_hl_info()
    fallback = panel_hl_info()
    if (_snapshot_info is None or getattr(_snapshot_info, "fallback", None) is not fallback
            or getattr(_snapshot_info, "store", None) is not panel_snapshot_store()):
        _snapshot_info = SnapshotInfo(panel_snapshot_store(), fallback)
    return _snapshot_info


def capture_panel_snapshot(source: str = "", with_why: bool = False) -> bool:
    """把「面板这一轮要显示的数据」写进快照表（**只读交易所，绝不下单**）。

    调用点只有两个：tick 之后（带六步结论）与 hl_monitor 之后（纯读数刷新）。
    `with_why=False` 时沿用上一条 why（六步结论只在 4h bar 收盘后才会变，每 5 分钟重算
    3 个标的 × 5000 根 K 线纯属浪费）。

    任何异常都只记日志并返回 False —— 面板会在快照缺失时回落实时读取，交易不受影响。
    """
    try:
        address = hl_params().account_address
        if not address:
            logger.info("[panel] 快照跳过：未配置钱包地址（面板会回落实时读取）")
            return False
        info = get_hl_info()
        mids = info.all_mids()
        snapshot = info.snapshot(address, mids=mids)
        why_rows = None
        if with_why:
            from app import why as why_mod
            try:
                why_rows = why_mod.build_rows(db, current_config())
            except Exception as e:                                # noqa: BLE001
                # 六步算不出来不该让整条快照作废：读数（权益/挂单/现价）仍然有效
                logger.warning("[panel] 快照的六步结论生成失败（沿用上一条）：%s", e)
                why_rows = None
        if why_rows is None:
            try:
                why_rows = panel_snapshot_store().latest_why()
            except Exception:                                     # noqa: BLE001
                why_rows = None
        store = panel_snapshot_store()
        store.write(snapshot=snapshot, mids=mids, why=why_rows, source=source or "scheduler")
        store.prune()
        # 统一账户下 `equity`（永续口径）恒为 0，账户总值在 `equity_total` —— 日志里取后者，
        # 否则运维看到「equity=0.0」会以为读坏了（面板本身两个字段都拿到，不受影响）。
        shown_equity = snapshot.get("equity_total") or snapshot.get("equity")
        logger.info("[panel] 快照已写入（%s）：equity=%s 持仓=%d 挂单=%d 现价=%d why=%s",
                    source or "scheduler", shown_equity,
                    len(snapshot.get("positions") or []), len(snapshot.get("open_orders") or []),
                    len(mids or {}), "有" if why_rows else "沿用")
        return True
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[panel] 快照写入失败（面板会回落实时读取）：%s", e)
        return False


def build_executor():
    """mode=off 时返回 None（纯提醒模式）。任何装配失败都退回 None 并记日志。"""
    p = hl_params()
    if p.mode == "off":
        return None
    try:
        from app.hl.exchange import HlExchange
        from app.hl.executor import Executor
        info = get_hl_info()
        ex = HlExchange(account_address=p.account_address, private_key=p.private_key,
                        base_url=p.base_url, dry_run=(p.mode == "dry"),
                        max_slippage_pct=p.max_slippage_pct, info=info)
        return Executor(db, p, info, ex, notifier=notifier)
    except Exception as e:                                           # noqa: BLE001
        logger.exception("装配 HL 执行器失败，退回纯提醒模式: %s", e)
        return None


# ---------------------------------------------------------------- 推送开关
# report_push / fill_push 是新键（首次从面板保存才落库）；缺省值决定「老库」的行为，
# 所以缺省必须是 1（开），否则升级后周报会突然静默。
_PUSH_ON = ("1", "true", "yes", "on")
_PUSH_OFF = ("0", "false", "no", "off")


def push_on(key: str, default: str = "1") -> bool:
    """读推送开关：DB 值优先，缺省为 default；识别不了的值按「开」处理（宁可多推一条）。"""
    try:
        raw = str(db.get_config_str(key, default) or default).strip().lower()
    except Exception as e:                                           # noqa: BLE001
        logger.warning("读推送开关 %s 失败：%s", key, e)
        return True
    return raw not in _PUSH_OFF


# ---------------------------------------------------------------- 调度

def _job_tick() -> None:
    try:
        sent = _tick_locked()
        if sent:
            logger.info("tick 发送 %d 条提醒", len(sent))
        _retry_failed_alerts()
        # 阶段 B：tick 刚算过六步、也刚读过行情 —— 顺手把面板快照写掉（含 why）。
        # 放在锁外：写快照是纯读操作 + 一次 INSERT，不该延长交易路径的持锁时间。
        capture_panel_snapshot("tick", with_why=True)
    except Exception as e:                                    # noqa: BLE001
        logger.exception("tick 异常: %s", e)


# 进程内 tick 互斥：/admin/tick 与调度器并发跑同一轮会重复推送/竞争 set_last_bar
# （APScheduler 的 max_instances 只约束它自己起的 job，管不了 HTTP 线程）。
import threading as _threading

_tick_lock = _threading.Lock()


def _tick_locked() -> list:
    with _tick_lock:
        return engine.tick(db, current_config(), notifier, executor=build_executor())


def _retry_failed_alerts(max_age_h: float = 24.0, limit: int = 5) -> None:
    """补推近 24h 内发送失败的提醒（最多 limit 条/轮）。

    背景：_emit 失败不标记幂等，但 set_last_bar 已推进 —— 旧行为下那条 entry/exit
    提醒永久丢失（离场提醒丢 = 闷亏）。这里按 alert_logs 里的失败记录补推，成功后
    更新 sent_successfully（下次不再推）。
    """
    try:
        import time as _time
        rows = db.failed_alerts_since(_time.time() - max_age_h * 3600)
        retry = [r for r in rows if (r["kind"] or "") not in ("heartbeat", "heartbeat_local",
                                                              "weekly", "weekly_local",
                                                              "exec_alert_fills",
                                                              # 观察中的数据源告警：故意不发，
                                                              # 不是发送失败，不得补推
                                                              "data_error_pending")]
        # bar_ms=NULL 的行不受 UNIQUE 约束（NULL 互不相等），同一故障可能累积多条
        # 失败行 —— 按 (symbol, kind) 只补推最新一条，避免同一告警被连发 N 次。
        _seen: set = set()
        _dedup = []
        for r in retry:
            key = (r["symbol"], r["kind"], r["bar_ms"])
            if key in _seen:
                continue
            _seen.add(key)
            _dedup.append(r)
        retry = _dedup
        for r in retry[:limit]:
            text = r["message"] or ""
            if not text:
                continue
            ok = bool(notifier and notifier.send_message(text))
            if ok:
                db.mark_alert_sent(int(r["id"]))
                logger.info("补推失败提醒成功：id=%s kind=%s", r["id"], r["kind"])
    except Exception as e:                                    # noqa: BLE001
        logger.exception("补推失败提醒异常: %s", e)


def _run_agent_cleanup(reason: str = "") -> dict:
    """到期保护清仓（**持 _tick_lock**，与 tick 互斥）。

    互斥设计（M2 修正后的事实）：
    1. 清仓全程持进程内 ``_tick_lock`` —— 与 tick / /admin/tick / hl_monitor（同锁）
       串行，进程里不可能有第二个写路径在跑；
    2. 执行器内部再用**进程级** ``_op_lock``（class 属性，所有 Executor 实例共用）
       兜底 run/on_entry/on_exit；
    3. 清仓期间置 `agent.cleanup_session()` 标记，让「有仓没止损 → 自动补挂」
       这类顺手动交易所的路径主动让路（它同时也不再在 kill 态下动手）。
    """
    with _tick_lock:
        ex = build_executor()
        if ex is None:
            # 纯提醒模式（off）或装配失败：没有执行器能平仓。返 ok=False 让上层喊人，
            # 绝不假装「已清仓」。
            return {"ok": False, "error": "执行器未启用或装配失败（模式=off？），无法自动清仓",
                    "closed": [], "dry": False}
        return ex.flatten_for_expiry(reason or "agent 到期保护")


def _agent_check(now_ms=None) -> dict:
    """跑一轮到期检查（拆出来便于 /admin 触发与单测直接调）。"""
    from app.hl import agent as agent_mod
    cfg = current_config()
    state = agent_mod.check_once(
        db, cfg, notifier, get_hl_info(),
        address=_env("HL_ACCOUNT_ADDRESS"),
        now_ms=now_ms,
        cleanup=_run_agent_cleanup,
        prefix=cfg.alert_prefix,
        log_event=lambda kind, msg, **kw: db.hl_log_event(kind, msg),
        mode=hl_mode(),
    )
    logger.info("[agent] 到期检查：level=%s source=%s 剩余 %s 天",
                state.get("level"), state.get("source"), state.get("days_left"))
    return state


def _job_agent_expiry() -> None:
    """每小时第 11 分：检查 API 钱包有效期（≤3 天且开关开 → 清仓）。"""
    try:
        _agent_check()
        _retry_failed_alerts()
    except Exception as e:                                    # noqa: BLE001
        logger.exception("agent 到期检查异常: %s", e)


def _job_hl_monitor() -> None:
    """每 5 分钟：快照 + 对账 + 死手开关 + 心跳（与信号 tick 解耦，便于看守判活）。

    **持 _tick_lock**：与 tick / /admin/tick / 到期清仓串行。以前这里不持锁，清仓
    （持锁）与 monitor（不持锁、自己新建 Executor、自己的实例锁）能同时跑，
    「清仓撤完单 → monitor 又补挂回去」来回打架（M2）。
    """
    with _tick_lock:
        ex = build_executor()
        if ex is not None:
            try:
                ex.run(force_snapshot=True)
            except Exception as e:                                    # noqa: BLE001
                logger.exception("HL 执行器监控轮异常: %s", e)
    # 阶段 B：读数刷新（5 分钟一轮）。放在锁外，且**无论执行器是否启用**都要刷新 ——
    # 面板在 mode=off/无凭据时也该能看到账户读数（只读，不碰下单路径）。
    capture_panel_snapshot("monitor", with_why=False)


def _job_heartbeat() -> None:
    cfg = current_config()
    text = engine.heartbeat_text(db, cfg)
    if cfg.heartbeat_push:
        ok = bool(notifier and notifier.send_message(text))
        db.log_alert(None, None, "heartbeat", text, ok)
    else:
        # 静默信标：只落库不推送（每日巡检已把同样信息汇总给用户）。
        # kind='heartbeat_local' 仅供 cma_watch.py / cma_status.py 判断调度器存活。
        db.log_alert(None, None, "heartbeat_local", text, True)
        logger.info("心跳（未推送，仅落库）:\n%s", text)
    _prune_alert_logs()


def _prune_alert_logs(keep_days: float = 60.0, keep_rows: int = 5000) -> None:
    """alert_logs 里 bar_ms=NULL 的行（心跳/周报/数据错误）不受 UNIQUE 幂等约束，
    会无限增长 —— 每天清一次超期的，保留最近 keep_rows 行兜底。失败只记日志。

    走 DB 的 @_locked 方法（不裸用共享连接）：这里是全仓唯一一处绕过 RLock 的
    DB 访问，跨线程 commit 会打乱其它线程的事务边界。
    """
    try:
        db.prune_alert_logs(keep_days=keep_days, keep_rows=keep_rows)
        db.prune_hl_telemetry()
    except Exception as e:                                        # noqa: BLE001
        logger.warning("alert_logs 清理失败（忽略）：%s", e)


def _job_weekly() -> None:
    text = engine.weekly_text(db, current_config())
    if push_on("report_push"):
        ok = bool(notifier and notifier.send_message(text))
        db.log_alert(None, None, "weekly", text, ok)
        return
    # 开关关了：只落库（kind=weekly_local，与心跳的静默信标同一套约定）
    db.log_alert(None, None, "weekly_local", text, True)
    logger.info("周报（未推送，仅落库）:\n%s", text)


# ---------------------------------------------------------------- K 线预热（阶段 A2）
# /why 的 K 线缓存（app/why.py::FEED_TTL_S=900）过期后，第一个访问者要自己等 10–12s
# 现取三标的行情。启动后先在后台把这个缓存填好，首个访问者直接命中。
# 与 ENABLE_SCHEDULER 分开开关：预热只影响首屏快慢，跟「要不要跑定时任务」是两件事。

_WHY_PREHEAT_OFF = ("0", "false", "no", "off")


def _why_preheat_on() -> bool:
    """预热开关，默认**开**（只有离线测试/无网络环境才该关）。"""
    return (os.environ.get("WHY_PREHEAT", "1") or "1").strip().lower() not in _WHY_PREHEAT_OFF


def _job_why_preheat() -> None:
    """后台线程里跑：只填 K 线缓存，失败只记日志（服务照常跑，首屏退回现取）。"""
    try:
        from app import why as why_mod
        bad = why_mod.preheat(current_config())
        if bad:
            logger.warning("K 线预热部分失败（首屏会现取，不影响交易与提醒）：%s", "；".join(bad))
        else:
            logger.info("K 线预热完成：三标的已进 /why 缓存，首个访问者不再等现取")
    except Exception as e:                                          # noqa: BLE001
        logger.warning("K 线预热失败（不影响服务）：%s", e)


def _start_why_preheat() -> Optional[threading.Thread]:
    """起守护线程预热，**绝不阻塞 startup**：首屏慢几秒可以等，服务起不来不行。"""
    if not _why_preheat_on():
        logger.info("K 线预热已关闭（WHY_PREHEAT=0）")
        return None
    th = threading.Thread(target=_job_why_preheat, name="why-preheat", daemon=True)
    th.start()
    return th


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    if os.environ.get("ENABLE_SCHEDULER", "true").lower() in ("1", "true", "yes"):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(_job_tick, CronTrigger(minute="3,33"), id="tick",
                          max_instances=1, coalesce=True)
        scheduler.add_job(_job_heartbeat, CronTrigger(hour=config.heartbeat_hour, minute=0), id="heartbeat")
        scheduler.add_job(_job_weekly, CronTrigger(day_of_week="mon", hour=config.heartbeat_hour, minute=5), id="weekly")
        scheduler.add_job(_job_hl_monitor, CronTrigger(minute="*/5"), id="hl_monitor",
                          max_instances=1, coalesce=True)
        # API 钱包到期检查：每小时第 11 分（错开 tick 的 :03/:33 与 hl_monitor 的 5 分整）
        scheduler.add_job(_job_agent_expiry, CronTrigger(minute=11), id="agent_expiry",
                          max_instances=1, coalesce=True)
        scheduler.start()
        logger.info("调度器已启动：tick 每小时 :03/:33（UTC 时刻）；心跳 %02d:00 UTC；周报周一；"
                    "HL 监控每 5 分钟（模式=%s）；API 有效期检查每小时 :11", config.heartbeat_hour, hl_mode())
    # 预热放在 scheduler 判断之外：只提醒模式（ENABLE_SCHEDULER=false）也需要它，
    # 而且它是后台线程，起到这一步不会拖慢 startup。
    _start_why_preheat()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="crypto-ma-alert", lifespan=lifespan)

# ---------------------------------------------------------------- gzip（阶段 A3）
# 面板 HTML 实测 ~60KB 未压缩，手机流量/首屏都要多背 ~59KB 的重复 CSS 与表格标记。
# 1024 字节起压：小 JSON（/health、/api/hl/*）压了省不了几字节，反而多一次 CPU 与
# 一层 content-encoding；面板 HTML/CSV 导出都远超这个门槛。
# 流式端点（CSV 导出）由 middleware 自己按 chunk 增量压缩；text/event-stream 在 Starlette
# 1.x 的默认排除表里，本服务也没有 SSE 端点，不会破坏长连接。
app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_SIZE)



# ---------------------------------------------------------------- 鉴权

def _require_admin(request: Request, k: Optional[str] = None) -> None:
    token = config.admin_token
    if not token:
        raise HTTPException(500, "ADMIN_TOKEN 未配置")
    supplied = k or request.query_params.get("k") or request.cookies.get("cma_admin")
    if supplied != token:
        raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")


@app.get("/health")
def health(k: Optional[str] = None):
    # 基本字段公开（看守/巡检脚本只读 status+scheduler）；带 ?k= 才给 hl_mode/hl_kill
    # （站点是公网的，无 token 不该暴露执行模式与熔断状态）。
    payload: Dict[str, Any] = {"status": "ok", "symbols": config.symbols,
                               "scheduler": bool(scheduler), "dry_run": config.dry_run}
    if k and config.admin_token and k == config.admin_token:
        payload["hl_mode"] = hl_mode()
        payload["hl_kill"] = db.hl_kill()
    return payload


@app.get("/")
def root():
    return RedirectResponse("/admin")


# ---------------------------------------------------------------- 主屏图标 / favicon
# 手机「添加到主屏」用的静态资源。路径从模块位置推导（app/static/），
# 不写死部署目录 —— CI 与容器里 app 包位置不同，写死会在测试/构建时炸。
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 图标一周内不会变；带 immutable 的 ETag 交给浏览器缓存，反代不参与。
_ICON_CACHE = "public, max-age=604800"


def _static_response(name: str, media_type: str) -> FileResponse:
    """返回 app/static/ 下的文件。存在性在**请求时**才看（import 时不碰磁盘）。"""
    path = os.path.join(STATIC_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, f"静态资源缺失：{name}")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": _ICON_CACHE})


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return _static_response("apple-touch-icon.png", "image/png")


@app.get("/icon-192.png")
def icon_192():
    return _static_response("icon-192.png", "image/png")


@app.get("/icon-512.png")
def icon_512():
    return _static_response("icon-512.png", "image/png")


@app.get("/favicon.ico")
def favicon():
    return _static_response("favicon.ico", "image/x-icon")


@app.get("/site.webmanifest")
def site_webmanifest():
    return _static_response("site.webmanifest", "application/manifest+json")


# ---------------------------------------------------------------- HL 面板与控制

def hl_guard(request: Request, response: Response, k: Optional[str] = None) -> None:
    """面板与 JSON API 的鉴权：?k=<ADMIN_TOKEN> 或 cma_admin Cookie；带 k 时写 Cookie。"""
    token = config.admin_token
    if not token:
        raise HTTPException(500, "ADMIN_TOKEN 未配置")
    supplied = k or request.query_params.get("k") or request.cookies.get("cma_admin")
    if supplied != token:
        raise HTTPException(401, "需要 ?k=<ADMIN_TOKEN>")
    if k:
        # Secure 只在 https（含反代 X-Forwarded-Proto）时加：本地 http 调试不受影响
        https_ = request.url.scheme == "https" or \
            str(request.headers.get("x-forwarded-proto", "")).lower() == "https"
        response.set_cookie("cma_admin", k, httponly=True, max_age=30 * 86400,
                            secure=https_, samesite="lax")


def _hl_deps():
    from app.hl.views import DashboardDeps
    return DashboardDeps(
        db=db,
        config_fn=current_config,
        info=get_hl_info(),
        address_fn=lambda: hl_params().account_address,
        mode_fn=hl_mode,
        base_url_fn=lambda: hl_params().base_url,
    )


def _mount_hl_views() -> None:
    try:
        from app.hl.views import build_router
        app.include_router(build_router(_hl_deps()), dependencies=[Depends(hl_guard)])
        logger.info("已挂载 /hl 监控面板（模式=%s）", hl_mode())
    except Exception as e:                                           # noqa: BLE001
        logger.warning("挂载 /hl 面板失败（不影响提醒与执行器）：%s", e)


def _mount_hl_keyform() -> None:
    """一次性私钥录入页：给「手机连不上 oracle1:22」时用的 HTTPS 通道。

    注意鉴权方式不同：它用自己的一次性令牌（15 分钟、用过即废），**不是** ADMIN_TOKEN，
    所以**不能**挂 hl_guard（否则还得带上 admin token）。
    """
    try:
        from app.hl.keyform import KeyFormDeps, build_router as build_key_router
        app.include_router(build_key_router(KeyFormDeps.from_env(db)))
        logger.info("已挂载 /admin/hl/key 私钥录入页（一次性令牌）")
    except Exception as e:                                           # noqa: BLE001
        logger.warning("挂载私钥录入页失败（不影响其他功能）：%s", e)


# ---------------------------------------------------------------- 合并管理面板
def _panel_write(fn):
    """把面板写操作包一层：**写成功之后**立刻让面板读数缓存失效。

    为什么要失效：面板读数（账户快照/现价）本身不依赖配置，但写操作会改变页面上其它区块
    （模式、kill 状态、参数生效值），如果刚写完的那次跳转还渲染着 10s 前的快照，用户会怀疑
    「点了没生效」。代价只是写操作后多取一次快照，一次点击一次，可以接受。
    """
    if fn is None:
        return None

    def wrapped(*args, **kwargs):
        out = fn(*args, **kwargs)
        invalidate_panel_cache()
        return out

    return wrapped


def _admin_deps():
    """装配合并面板的依赖（全部走可替换函数，测试里可换成假实现）。"""
    from app.admin_ui import AdminDeps
    hl = _hl_deps()
    # 面板侧的只读客户端（阶段 A1/A2 + B）：
    #   默认 = 读快照表（零网络零重算），快照缺失/过期时回落 PanelInfo（实时 + 10s TTL）；
    #   PANEL_SNAPSHOT=0 时退回 A1 的纯 TTL 包装。
    # 只换**这一份** AdminDeps 的 info（dataclasses.replace 产生新对象），
    # `_hl_deps()` 自身、`/api/hl/*`、执行器、看护仍拿 get_hl_info() 的实时客户端。
    pinfo = panel_snapshot_info() if panel_snapshot_on() else panel_hl_info()
    hl = replace(hl, info=pinfo)
    return AdminDeps(
        db=db,
        config_fn=current_config,
        hl_deps=hl,
        mode_fn=hl_mode,
        token_fn=lambda: config.admin_token,
        address_fn=lambda: hl_params().account_address,
        private_key_fn=lambda: hl_params().private_key,
        mids_fn=pinfo.all_mids,                        # 与 snapshot 里的现价共用同一份快照
        # 写路径（参数/模式/开关/kill）成功后失效面板缓存
        set_config=_panel_write(getattr(db, "set_config", None)),
        hl_set_kill=_panel_write(getattr(db, "hl_set_kill", None)),
        # 到期检查 + 面板「更新 API」：都走 app.hl.agent（面板卡片只读 state，零网络）
        agent_state_fn=lambda: _agent_state(),
        renew_fn=lambda key: _renew_agent_key(key),
        cleanup_enabled_fn=lambda: _cleanup_on(),
        # /why 诊断（只读）：阶段 B 起优先读快照里的六步结论，缺失才现取
        why_rows_fn=lambda: _why_rows(),
        # 取不到现价时预览降级成「无法取价」，不硬编一个假价格
        price_fallback=0.0,
    )


def _why_rows(live: bool = False) -> list:
    """面板「为什么没开仓」的数据源（只读；异常一律退回空列表，别弄挂总览）。

    阶段 B：默认读快照里的六步结论（tick 后写入，最多滞后一个 tick 周期）；
    快照过期/没带 why 时才现取 K 线重算（那一下约 1.2s，且只在快照断档时发生）。
    """
    if not live and panel_snapshot_on():
        # 面板写操作（参数/模式/kill）后 5 秒内的渲染一律现算：改完阈值立刻要看到新解释，
        # 不能等下一轮 tick 才刷新（这一点与 A1 的 10s 缓存同一取舍，只是从 10s 收到 5s）。
        if (time.time() - _panel_invalidated_at) < 5.0:
            live = True
    if not live and panel_snapshot_on():
        try:
            info = panel_snapshot_info()
            row = info.stored_row()                   # 同一套「过期就不用」的判据
            if row is not None and row.get("why"):
                return copy.deepcopy(row["why"])
        except Exception as e:                                  # noqa: BLE001
            logger.warning("/why 快照读取失败（改现取）：%s", e)
    try:
        from app import why as why_mod
        return why_mod.build_rows(db, current_config())
    except Exception as e:                                          # noqa: BLE001
        logger.warning("/why 诊断失败（面板区块略去）：%s", e)
        return []


def _agent_state() -> dict:
    """面板用：读 ``hl_agent_state`` 缓存（**不**现调 HL API）。"""
    from app.hl import agent as agent_mod
    return agent_mod.read_state(db)


def _cleanup_on() -> bool:
    from app.hl import agent as agent_mod
    return agent_mod.cleanup_switch(db, current_config())


def _renew_agent_key(new_key: str) -> tuple:
    """面板「更新 API」：校验 + 写 DB + 刷新 state + 推送。返回 (ok, 文案)。"""
    from app.hl import agent as agent_mod
    cfg = current_config()
    return agent_mod.renew_key(
        db, cfg, notifier, get_hl_info(), new_key,
        address=_env("HL_ACCOUNT_ADDRESS"),
        current_key=hl_private_key(),
        prefix=cfg.alert_prefix,
        log_event=lambda kind, msg, **kw: db.hl_log_event(kind, msg),
    )


def _mount_admin_ui() -> None:
    """先挂合并面板，再挂 /hl：让 /hl 的 302 覆盖 views 里的原路由。"""
    from app.admin_ui import build_admin_router
    try:
        app.include_router(build_admin_router(_admin_deps()))
        logger.info("已挂载合并管理面板 /admin（四 Tab）")
    except Exception as e:                                           # noqa: BLE001
        logger.warning("挂载合并管理面板失败：%s", e)


# 顺序有讲究：/admin 系列先注册，/hl 的旧路由被 302 覆盖，/api/hl/* 原样保留。
_mount_admin_ui()
_mount_hl_views()
_mount_hl_keyform()


# ---------------------------------------------------------------- 旧参数表单兼容
# 新表单走 POST /admin/params；这条留给老书签/老代码（比如还没删的 curl），
# 口径不变：仍然只写 DB 的五个数字字段。

@app.post("/admin/rules")
def admin_rules_post(request: Request, equity: str = Form(...), risk_pct: str = Form(...),
                     leverage: str = Form(...), stop_min: str = Form(...), stop_max: str = Form(...),
                     enable_long: Optional[str] = Form(None),
                     enable_shorts: Optional[str] = Form(None),
                     heartbeat_push: Optional[str] = Form(None),
                     agent_expiry_cleanup: Optional[str] = Form(None),
                     short_regime_ma: Optional[str] = Form(None), k: str = Form("")):
    _require_admin(request, k or None)
    # 2026-10-19 审查中危回归：**先全部校验、后统一写库**。旧实现边校验边写，
    # short_regime_ma 非法时 400 返回，但前面的数字和方向开关已经落库（半截写）。
    nums: dict = {}
    for key, val in [("equity", equity), ("risk_pct", risk_pct), ("leverage", leverage),
                     ("stop_min", stop_min), ("stop_max", stop_max)]:
        try:
            nums[key] = float(val)
        except ValueError:
            raise HTTPException(400, f"{key} 不是数字")
    ma = None
    if short_regime_ma is not None:            # 缺字段 = 保持原值（与下方开关同口径）
        try:
            ma = int(float(short_regime_ma))
        except ValueError:
            raise HTTPException(400, "short_regime_ma 不是数字")
    # ---- 校验全部通过，开始写库 ----
    for key, num in nums.items():
        # 存成 JSON 数字（不是字符串），避免 json.dumps 加引号让 apply_db 解析失败
        db.set_config(key, num)
    if ma is not None:
        db.set_config("short_regime_ma", ma)
    # 复选框（2026-10-19 事故回归）：**缺字段 = 保持原值**，显式传 0 才关。
    # 旧语义「不传 = 0」就是那次把线上方向开关静默关掉的向量 —— 老书签/curl
    # 只想改数字时，方向开关绝不能被顺手清掉。显式值与 app.admin_ui._TRUTHY
    # 同口径解析：字符串 "0" 是真值，绝不能写 `1 if v else 0`（把「关」写成「开」）。
    from app.admin_ui import _TRUTHY

    def _flag_like(v: Optional[str]) -> int:
        return 1 if str(v if v is not None else "").strip().lower() in _TRUTHY else 0

    if enable_long is not None:
        db.set_config("enable_long", _flag_like(enable_long))
    if enable_shorts is not None:
        db.set_config("enable_shorts", _flag_like(enable_shorts))
    if heartbeat_push is not None:
        db.set_config("heartbeat_push", _flag_like(heartbeat_push))
    # 老书签的表单里没有这个复选框：**不传 = 保持原值**（不能按「未勾选」清成 0，
    # 否则老页面一提交就把到期保护静默关掉）。显式传 0 才关。
    if agent_expiry_cleanup is not None:
        from app.hl.agent import cleanup_enabled
        db.set_config("agent_expiry_cleanup", 1 if cleanup_enabled(agent_expiry_cleanup) else 0)
    return RedirectResponse(f"/admin?tab=params&k={_urlquote(str(k))}", status_code=303)


# ---------------------------------------------------------------- 确认页

@app.get("/c/{pid}", response_class=HTMLResponse)
def confirm_view(pid: int, request: Request, a: str = "view", t: str = ""):
    if not engine.verify_token(config.token_secret, pid, a, t):
        raise HTTPException(401, "链接无效或已过期")
    row = db.position_by_id(pid)
    if not row:
        raise HTTPException(404, "记录不存在")
    if a in ("close", "edit_close"):
        # 提醒消息里的 a=close 链接：该仓尚未录入真实平仓价（系统自动关的）→ 直接给表单；
        # 已经录入过（用户自己确认的）→ 给只读摘要，改正要走「修改平仓数据」按钮。
        if a == "close" and row["status"] == "closed" and not row["auto_closed"]:
            return _summary(row, pid, request)
        return _close_form(row, request)
    if a == "edit_open":
        # 双口径收敛：HL 仓的真实开仓数据由执行器写（actual_* / hl_fills），
        # 本表单是 MEXC 面值口径 → 连表单都不给（按钮在摘要页已隐藏，这里挡住直达 URL）。
        if posfields.is_hl(row):
            raise HTTPException(400, "本仓由 HL 执行器管理，不支持在这里修改开仓记录"
                                     "（真实成交见面板「记录」页的 hl_fills / hl_orders）")
        return _entry_form(row, pid, editing=True, request=request)
    if row["status"] == "pending":
        return _entry_form(row, pid, editing=False, request=request)
    # 已确认开仓 / 已平仓 → 只读摘要（改动必须点「修改」显式进入表单，防手滑覆盖）
    return _summary(row, pid, request)


def _entry_form(row, pid: int, editing: bool, request: Request = None) -> HTMLResponse:
    tok = lambda act: engine.sign_token(config.token_secret, pid, act)      # noqa: E731
    side_txt = "做多" if (row["side"] or 1) == 1 else "做空"
    stop_hint = "（价格下方）" if (row["side"] or 1) == 1 else "（价格上方）"
    cfg = current_config()
    note = ""
    if not editing:
        try:
            feed = market.fetch(row["symbol"])
            ma200 = strategy.compute_indicators(feed.closed)["sma200"][-1] or 0.0
            rn = engine.regime_note(row["side"] or 1, feed.bars[-1].close, ma200)
            cls = {"warn": "bad", "ok": "muted"}.get(rn["level"], "muted")
            if rn["text"]:
                note = f"<p class='{cls}'>{rn['text']}</p>"
        except Exception as e:                                              # noqa: BLE001
            note = f"<p class='muted'>MA200 检查失败：{e}</p>"
    else:
        note = ("<p class='muted'>这是修改页：数值保存后会覆盖原来的记录"
                f"（当前入场 {row['actual_entry'] or row['entry_price']:,.2f} / "
                f"止损 {row['actual_stop'] or row['stop_price']:,.2f} / "
                f"张数 {(row['actual_contracts'] if row['actual_contracts'] is not None else row['contracts']):g}）。</p>")
    cur_entry = posfields.entry_px(row) if editing else row["entry_price"]
    cur_stop = posfields.stop_px(row) if editing else row["stop_price"]
    # 双口径判定（app/posfields.py）：**一行一判**，下面五处（张数回退、金额换算
    # 禁用、币量标签、提示语）都依赖它。缺了这行 = 任何 pending 行/修改页直接
    # NameError 500（2026-10-24 审查 B1 实锤：这行在 c950bdc 收敛时被顺手删了）。
    _is_hl = posfields.is_hl(row)
    cur_qty = posfields.qty(row) if editing else (posfields.contracts_raw(row)
                                                  if not _is_hl else posfields.qty(row))
    edit_flag = "<input type='hidden' name='edit' value='1'>" if editing else ""
    title = f"修改开仓数据 #{pid}" if editing else f"确认开仓 #{pid}"
    # 双口径（app/posfields.py）：HL 执行器管的仓 —— 行里的 contracts/notional/risk_u
    # 是 MEXC 人工口径（张数 + 按配置 equity 1000U 算的名义），**不能拿来展示**；
    # 真实值在 actual_*。历史 MEXC 行照旧读原字段。qty_label 也按口径切换。
    qty_label = posfields.qty_label(row)
    qty_hint = "" if not _is_hl else "（HL 仓按币量填写，忽略成交金额）"
    if _is_hl:
        # HL 仓：只展示真实（实际成交）口径；信号建议值单列一行并标明未含滑点。
        _sz, _ntl, _rk = posfields.qty(row), posfields.notional(row), posfields.risk_u(row)
        _base = posfields.equity_base(row)
        real_line = (f"实际成交：入场 <b>{posfields.entry_px(row):,.2f}</b>｜"
                     f"止损 <b>{posfields.stop_px(row):,.2f}</b>｜"
                     f"数量 <b>{_sz:g} {posfields.qty_unit(row)}</b>｜"
                     f"名义 <b>{_ntl:,.2f} U</b>｜风险 <b>{_rk:.2f}U</b>"
                     + (f"（基数 {_base:,.0f}U）" if _base else "") + "<br>"
                     if (_sz and posfields.entry_px(row)) else
                     "<span class='muted'>本行尚无真实成交数据（未成交/已放弃）。</span><br>")
        suggest_line = (f"信号建议（未含滑点）：入场 {row['entry_price']:,.2f}｜"
                        f"止损 {row['stop_price']:,.2f}{stop_hint}"
                        f"（{row['stop_dist'] * 100:.2f}%）\n")
    else:
        real_line = ""
        # 目标风险的百分比必须是**生效后**的比例（空单风险减半开着时 = risk_pct/2），
        # 否则确认页会拿 2% 去对一个本该 1% 的空单，用户照着加仓就是风险翻倍。
        _rp = sizing.effective_risk_pct(cfg.risk_pct, row["side"] or 1,
                                        getattr(cfg, "short_risk_half", False))
        suggest_line = (
            f"建议入场：<b>{row['entry_price']:,.2f}</b>｜止损：<b>{row['stop_price']:,.2f}</b>"
            f"{stop_hint}（{row['stop_dist'] * 100:.2f}%）<br>\n"
            f"      建议张数：<b>{row['contracts']}</b> 张｜名义 <b>{row['notional']:,.0f} USDT</b>｜\n"
            f"      风险 {row['risk_u']:.1f}U（目标 {cfg.equity:,.0f}U × {_rp * 100:.0f}% =\n"
            f"      {cfg.equity * _rp:.1f}U）\n")
    body = f"""
    <h1>{title}</h1>
    <div class='card'>
      <b>{row['symbol']}</b> {side_txt}｜信号 bar {engine.fmt_ms(row['signal_bar_ms'])} {engine.TZ_LABEL}<br>
      {real_line}{suggest_line}
      {note}
    </div>
    <form method='post'>
      <input type='hidden' name='a' value='open'><input type='hidden' name='t' value='{tok("open")}'>{edit_flag}
      <p>实际成交均价 <input name='actual_entry' value='{cur_entry:.2f}'></p>
      <p>成交金额 USDT <input name='fill_amount' value='' placeholder='成交记录里的金额'{' disabled' if _is_hl else ''}>
        <span class='muted'>{'HL 仓不适用金额换算' if _is_hl else '填了就按它换算张数（比下单时看到的预览张数准）'}</span></p>
      <p>实际{qty_label} <input name='actual_contracts' value='{cur_qty}'>
        <span class='muted'>没填成交金额时用这个{qty_hint}</span></p>
      <p>实际止损触发价 <input name='actual_stop' value='{cur_stop:.2f}'></p>
      <p><label><input type='checkbox' name='stop_placed' value='1' {'checked' if row['stop_placed'] or not editing else ''} style='width:auto'> 已挂止损单</label></p>
      <button type='submit'>{'保存修改' if editing else '按以上数值确认开仓'}</button>
    </form>
    {"" if editing else f'''
    <form method='post' style='margin-top:8px'>
      <input type='hidden' name='a' value='ignore'><input type='hidden' name='t' value='{tok("ignore")}'>
      <button class='red' type='submit'>未开仓 / 忽略</button>
    </form>'''}
    <p><a class='btn gray' href='/c/{pid}?a=view&t={tok("view")}'>返回</a></p>
    """
    return page(title, body, request=request)


def _status_label(row) -> str:
    return {"pending": "待确认", "open": "持仓中", "closed": "已平仓",
            "ignored": "已忽略"}.get(row["status"], row["status"])


def _exit_label(row) -> str:
    """离场原因 → 中文（与 engine 面板/周报同一映射，单一来源在 engine）。

    M8：这里以前直接印 `row['exit_reason']`，`exchange` 这种新枚举值就以英文原样
    出现在摘要页/确认页上；与 `stop`/`ma20` 同风格翻译，未知值由 engine 兜底原文。
    """
    return engine._exit_reason_plain(row["exit_reason"], row["side"])


def _hl_funding_lines(row) -> list:
    """确认页（/c 摘要）HL 仓的单粒度资金费两行（feat/funds-ledger，P3）。

    - 「本单资金费合计 ±X U」：hl_funding 按 position_id 归集的合计（正=收入）；
      没有资金费记录显示「暂无资金费记录（或尚未同步）」，不猜 0。
    - 「本单净盈亏（含手续费，不含资金费）」= positions.net_pnl **原口径原值**（本
      轮拍板一律不动）；「本单总成本（含资金费）」与之并列 = 手续费合计 + 资金费
      支出合计（收入不抵扣成本，收入已在上一行如实展示）。手续费合计走引擎同款
      聚合（hl_pnl.aggregate_fills + hl_fills_window），不另写一套口径。
    """
    lines: list = []
    pid = row["id"]
    fund_sum = db.hl_funding_by_position().get(int(pid))
    coin = registry_like_coin(row["symbol"])
    if fund_sum is None:
        lines.append("<span class='muted'>本单资金费合计：暂无资金费记录（或尚未同步）</span>")
        fund_sum = 0.0
    else:
        cls = "pos" if fund_sum > 0 else ("neg" if fund_sum < 0 else "")
        lines.append(f"本单资金费合计 <b class='{cls}'>{fund_sum:+,.2f} U</b>（正=收入）")
    # 手续费合计：引擎同款窗口聚合（±300s，见 app/hl/pnl.py docstring）
    fee_total = 0.0
    try:
        lo, hi = hl_pnl.window_bounds(row["opened_at"], row["closed_at"])
        fills = db.hl_fills_window(coin, lo, hi) if coin else []
        agg = hl_pnl.aggregate_fills(fills, row["opened_at"], row["closed_at"], coin,
                                     row["side"] or 1)
        fee_total = float(agg.get("fee_total") or 0.0)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[confirm] 本单手续费聚合失败（成本行按 0 展示）：%s", e)
    funding_paid = max(0.0, -fund_sum)      # 支出 = 负的合计取绝对值；收入不算成本
    net = row["net_pnl"]
    net_txt = "—（无成交数据）" if net is None else f"{float(net):+,.2f} U"
    cost = fee_total + funding_paid
    lines.append(f"<span class='muted'>本单净盈亏（含手续费，不含资金费）"
                 f"<b>{net_txt}</b>｜本单总成本（含资金费）<b>{cost:,.2f} U</b></span>")
    return lines


def registry_like_coin(symbol: str) -> str:
    """symbol → HL coin：注册表优先，未登记回落下划线前缀（与执行器默认映射一致）。"""
    try:
        from app import symbol_registry as _reg
        return _reg.hl_coin(symbol)
    except Exception:                                             # noqa: BLE001
        return hl_funding.coin_of_symbol(symbol)


def _summary(row, pid: int, request: Request = None) -> HTMLResponse:
    """只读摘要页：只显示当前记录 + 显式入口（修改 / 手动平仓）。"""
    tok = lambda act: engine.sign_token(config.token_secret, pid, act)      # noqa: E731
    side_txt = "多单" if (row["side"] or 1) == 1 else "空单"
    entry = posfields.entry_px(row)
    stop = posfields.stop_px(row)
    qty = posfields.qty(row)
    is_hl = posfields.is_hl(row)
    qty_unit = posfields.qty_unit(row)
    lines = [
        f"<b>{row['symbol']}</b> {side_txt}｜信号 bar {engine.fmt_ms(row['signal_bar_ms'])} {engine.TZ_LABEL}",
        f"入场 <b>{entry:,.2f}</b>｜止损 <b>{stop:,.2f}</b>｜{qty:g} {qty_unit}",
    ]
    if row["status"] == "closed":
        lines.append(f"离场 <b>{row['exit_price'] or 0:,.2f}</b>（{_exit_label(row)}）"
                     f"｜本次 <b>{row['r_multiple'] or 0:+.2f}R</b>")
        if row["auto_closed"] and not is_hl:
            lines.append("<span class='muted'>离场价是系统按参考价记录的；"
                         "若与 MEXC 实际成交不同，点下面的修改按钮改正。</span>")
    btns = []
    if row["status"] in ("open", "closed") and not is_hl:
        btns.append(f"<a class='btn' href='/c/{pid}?a=edit_open&t={tok('edit_open')}'>修改开仓记录</a>")
    if row["status"] == "open":
        btns.append(f"<a class='btn gray' href='/c/{pid}?a=close&t={tok('close')}'>记录平仓成交价</a>")
    if row["status"] == "closed":
        btns.append(f"<a class='btn gray' href='/c/{pid}?a=edit_close&t={tok('edit_close')}'>修改平仓数据</a>")
    # 审计三轮 M10：这些入口只改本地记账，不会在交易所平仓/改止损 —— HL 仓上
    # 「手动平仓」旧文案会让用户以为点了就真平了。明示语义。
    if is_hl:
        btns.append("<br><span class='muted'>注：本页操作只修改记录，不会在交易所下单；"
                    "交易所平仓请到 HL 面板/交易所操作。</span>")
        # 双口径收敛：HL 仓的真实成交在交易所侧且已写进 actual_*/hl_fills，本地
        # 「修改开仓记录」是 MEXC 面值口径的表单，改了就与交易所脱节 → 入口直接隐藏。
        btns.append("<br><span class='muted'>本仓由 HL 执行器管理，真实成交见面板"
                    "「记录」页的成交（hl_fills）与下单记录（hl_orders），"
                    "不支持在此修改开仓数据。</span>")
        # 单粒度资金费收口（feat/funds-ledger，P3）：HL 持仓/已平仓摘要页新增可见两行；
        # 非 HL 仓不加（is_hl 已过滤）。net_pnl 原口径原值，只在文案里注明「不含资金费」。
        lines.extend(_hl_funding_lines(row))
    body = (f"<h1>{_status_label(row)} #{pid}</h1>"
            f"<div class='card'>{'<br>'.join(lines)}</div>"
            f"<div class='card'>{' '.join(btns) or '<span class=muted>无可操作项</span>'}</div>")
    # P3 命名同步：确认页 <title> 也用「双均线交易台」（正文 h1 保持「状态 #id」不动）。
    return page(f"{PRODUCT_NAME} · {_status_label(row)} #{pid}", body, request=request)


def _close_form(row, request: Request = None) -> HTMLResponse:
    entry = posfields.entry_px(row)
    stop = posfields.stop_px(row)
    side_txt = "多单" if (row["side"] or 1) == 1 else "空单"
    hint = ""
    if row["status"] == "closed":
        hint = (f"<p class='muted'>当前记录：离场价 <b>{row['exit_price'] or 0:,.2f}</b>"
                f"（{_exit_label(row)}，约 {row['r_multiple'] or 0:+.2f}R）。"
                "保存后按新价重算 R。</p>")
    body = f"""
    <h1>录入平仓 #{row['id']}</h1>
    <div class='card'>
      <b>{row['symbol']}</b> {side_txt}｜入场 {entry:,.2f}｜止损 {stop:,.2f}
      {hint}
    </div>
    <form method='post'>
      <input type='hidden' name='a' value='close'>
      <input type='hidden' name='t' value='{engine.sign_token(config.token_secret, row["id"], "close")}'>
      {'<input type="hidden" name="edit" value="1">' if row["status"] == "closed" else ''}
      <p>实际平仓价 <input name='exit_price' value='{row["exit_price"] or 0:.2f}'></p>
      <p>离场原因
        <select name='exit_reason' style='padding:8px;border-radius:6px'>
          <option value='ma20' {'selected' if row['exit_reason'] == 'ma20' else ''}>收到提醒后平仓</option>
          <option value='stop' {'selected' if row['exit_reason'] == 'stop' else ''}>止损单被触发</option>
          <option value='manual' {'selected' if row['exit_reason'] == 'manual' else ''}>手动提前平</option>
          <option value='exchange' {'selected' if row['exit_reason'] == 'exchange' else ''}>交易所侧已平仓（人工/外部）</option>
        </select></p>
      <button type='submit'>保存平仓价</button>
    </form>
    <p><a class='btn gray' href='/c/{row['id']}?a=view&t={engine.sign_token(config.token_secret, row['id'], "view")}'>返回</a></p>
    """
    return page(f"录入平仓 #{row['id']}", body, request=request)


@app.post("/c/{pid}", response_class=HTMLResponse)
async def confirm_post(pid: int, request: Request):
    form = await request.form()
    a = str(form.get("a") or "")
    t = str(form.get("t") or "")
    if not engine.verify_token(config.token_secret, pid, a, t):
        raise HTTPException(401, "链接无效")
    row = db.position_by_id(pid)
    if not row:
        raise HTTPException(404, "记录不存在")

    if a == "ignore":
        if row["status"] not in ("pending", "open"):
            raise HTTPException(400, f"该仓位已是 {row['status']}，无需忽略")
        db.ignore_position(pid)
        return page("已忽略", f"<h1>已忽略 #{pid}</h1><p>{row['symbol']} 这笔不计入统计。</p>", request=request)
    if a == "close":
        editing = bool(form.get("edit"))
        # pending/open → 正常平仓；closed + (系统自动关 或 从「修改平仓数据」进来) → 允许改正。
        # 其余（已确认过的平仓 + 旧链接直接提交）仍拒绝，防止手滑覆盖。
        if row["status"] in ("pending", "open"):
            pass
        elif row["status"] == "closed" and (row["auto_closed"] or editing):
            pass
        else:
            raise HTTPException(400, "该仓位已平仓且成交价已确认；如需改正请从确认页点「修改平仓数据」")
        try:
            px = float(str(form.get("exit_price") or row["entry_price"]))
        except ValueError:
            raise HTTPException(400, "平仓价不是数字")
        reason = str(form.get("exit_reason") or "ma20")
        db.close_position(pid, px, reason)
        return page("已平仓", f"<h1>已记录平仓 #{pid}</h1><p>{row['symbol']} @ {px:,.2f}（{reason}）</p>", request=request)
    if a == "open":
        editing = bool(form.get("edit"))
        if row["status"] == "pending":
            pass
        elif row["status"] in ("open", "closed") and editing:
            pass          # 从「修改开仓数据」进来，允许改正
        else:
            raise HTTPException(400, f"该仓位状态是 {row['status']}；如需改正请从确认页点「修改开仓数据」")
        # 双口径收敛：HL 仓的开仓数据归执行器（actual_* / hl_fills），本地改正会把
        # MEXC 面值口径的数字写进 HL 行 → 一律 400（与 GET a=edit_open、db.update_fill
        # 同一守卫，三处一致：路由 400 + 按钮隐藏 + DB 抛错）。
        if posfields.is_hl(row) and row["status"] != "pending":
            raise HTTPException(400, "本仓由 HL 执行器管理，不支持修改开仓记录；"
                                     "真实成交请到面板「记录」页按 hl_fills 核对")
        try:
            entry = float(str(form.get("actual_entry") or row["entry_price"]))
            stop = float(str(form.get("actual_stop") or row["stop_price"]))
        except ValueError:
            raise HTTPException(400, "数值格式错误")
        err = engine.validate_stop_side(row["side"] or 1, entry, stop)
        if err:
            raise HTTPException(400, err)
        raw_amount = str(form.get("fill_amount") or "").strip()
        if raw_amount and row["hl_status"]:
            # HL 仓的成交金额换算不适用（面值/单位都是 MEXC 口径，会写脏 HL 仓）
            raise HTTPException(400, "HL 执行器管理的仓位不支持成交金额换算，请直接填写币量数量")
        source = "contracts"
        if raw_amount:
            try:
                amount = float(raw_amount)
            except ValueError:
                raise HTTPException(400, "成交金额不是数字")
            try:
                qty = sizing.contracts_from_amount(row["symbol"], entry, amount)
            except ValueError as e:
                raise HTTPException(400, str(e))
            source = "amount"
        else:
            try:
                qty = float(str(form.get("actual_contracts") or row["contracts"]))
            except ValueError:
                raise HTTPException(400, "张数不是数字")
        placed = bool(form.get("stop_placed"))
        if row["status"] == "pending":
            db.confirm_position(pid, entry, qty, stop, placed)
        else:
            try:
                db.update_fill(pid, entry, qty, stop, placed)   # 修改已确认的记录
            except ValueError as e:
                # DB 层守卫（HL 仓不可本地改开仓数据）→ 双保险，转成 400 而非 500
                raise HTTPException(400, str(e))
        # 「权益的 X%」用**这行的下单基数**（HL 仓由执行器写回实际生效基数；
        # MEXC 行是建行时的配置 equity），不再拿当前配置值硬套 —— 否则 HL 仓
        # 真实基数 100U 会被按配置 1000U 读成 0.2%。
        _eq_base = posfields.equity_base(row, current_config().equity)
        # 目标风险用**这行建行时**的计划风险（row["risk_u"]），不用当前 cfg × 当前开关
        # 现算：开关在建行与确认之间可能被切换，现算会把一笔按旧政策下的单判成
        # 「偏离 +102% / -50%」并反向建议（开双倍 / 砍一半）—— 见 review_fill 文档。
        rev = engine.review_fill(current_config(), row["symbol"], row["entry_price"],
                                 row["contracts"], entry, stop, qty, source,
                                 equity=_eq_base, side=row["side"],
                                 target_risk_u=row["risk_u"])
        src_txt = (f"按成交金额 {float(raw_amount):,.2f}U ÷ ({entry:,.2f} × 面值) 换算"
                   if source == "amount" else "按你填的张数")
        warn = "" if placed else "<p class='bad'>⚠️ 未挂止损单，这笔的风险不受保护</p>"
        notes = "".join(f"<li>{n}</li>" for n in rev["notes"])
        review = (f"<div class='card {'bad' if rev['level'] == 'warn' else 'muted'}'>"
                  f"风险体检：实际风险 <b>{rev['risk_u']:.1f}U</b>"
                  f"（权益的 {rev['risk_u'] / rev['equity'] * 100:.1f}%）｜"
                  f"止损距离 {rev['stop_dist'] * 100:.2f}%｜名义 {rev['notional']:,.0f}U｜"
                  f"保证金 {rev['margin']:,.0f}U"
                  + (f"<ul>{notes}</ul>" if notes else "　✅ 与目标一致")
                  + "</div>")
        return page("已确认", f"<h1>已确认开仓 #{pid}</h1>"
                              f"<p>{row['symbol']} @ {entry:,.2f}，{qty} 张，止损 {stop:,.2f}<br>"
                              f"<span class='muted'>{src_txt}</span></p>{warn}{review}",
                    request=request)
    raise HTTPException(400, f"未知操作 {a}")


@app.post("/admin/tick")
def admin_tick(request: Request, k: Optional[str] = None):
    _require_admin(request, k)
    sent = _tick_locked()
    return {"sent": len(sent), "messages": sent}


# ---------------------------------------------------------------- HL 模式 / kill（runbook 的 curl 在用）
@app.post("/admin/hl/mode")
def admin_hl_mode(request: Request, mode: str = Form("off"), k: str = Form("")):
    """切换执行器模式（off/dry/live）。live 要求已配置地址与私钥。"""
    _require_admin(request, k or None)
    if mode not in ("off", "dry", "live"):
        raise HTTPException(400, "mode 只能是 off/dry/live")
    if mode == "live":
        p = hl_params()
        if not p.account_address or not p.private_key:
            raise HTTPException(400, "未配置 HL_ACCOUNT_ADDRESS / HL_PRIVATE_KEY，不能进 live")
    db.set_config("hl_exec_mode", mode)
    db.hl_log_event("decision", f"人工切换执行器模式 → {mode}")
    if mode != "live":
        # 人工路径（含「切出 live」这种收尾动作）→ origin='manual'：永不被自动解除。
        db.hl_set_kill(False, "", origin="manual")
    return RedirectResponse(f"/admin?tab=switch&k={_urlquote(str(k))}", status_code=303)


@app.post("/admin/hl/kill")
def admin_hl_kill(request: Request, on: str = Form("0"), k: str = Form("")):
    """kill switch：off=恢复正常，on=暂停自动交易（仍允许人工平仓/撤单）。

    origin 显式写 'manual'：这是**人**按的按钮，绝不能被「连续 N 轮对账干净」自动解除
    —— 人按下去通常正因为它看到了机器看不出的东西（见 Executor._maintain_kill_autoclear）。

    【v2·高2】解除**回撤熔断**（origin=='dd'）时，必须在本请求内把回撤基线（peak_index）
    重置为当前 index：否则下一轮 DD 仍是 31% → 立刻又置 kill，「人工解」形同虚设。
    这条 curl 入口与面板 `/admin/switch/kill` 同一口径（runbook 用的是这条）。
    """
    _require_admin(request, k or None)
    want = on in ("1", "true", "on")
    if not want and db.hl_kill_origin() == "dd":
        from app.hl import dd as hl_dd
        try:
            new = hl_dd.reset_peak(db)
            db.hl_log_event("kill", "解除回撤熔断 → 回撤基线已重置"
                            + (f"（当前指数 {new['index']:.2f}）" if new else "（失败：状态不可用）"))
        except Exception as e:                                       # noqa: BLE001
            # 重置失败不能挡住解锁：宁可由下一轮重新熔断（用户会看到提示），也不要
            # 让一次 DB 写失败把 kill 卡死。
            db.hl_log_event("kill", f"解除回撤熔断时重置基线失败：{type(e).__name__}: {e}")
    db.hl_set_kill(want, "人工" if want else "", origin="manual")
    db.hl_log_event("kill", f"人工{'触发' if want else '解除'} kill switch")
    return RedirectResponse(f"/admin?tab=switch&k={_urlquote(str(k))}", status_code=303)