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

import json
import logging
import os
import time
from urllib.parse import quote as _urlquote
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import engine, market, posfields, sizing, strategy
from app.config import Config
from app.db import DB
from app.notify import get_notifier
from app.ui_theme import THEME_CSS, normalize_theme, page_shell

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("cma")

DB_PATH = os.environ.get("DB_PATH", "/app/data/crypto_ma.db")
db = DB(DB_PATH)
config = Config()
notifier = get_notifier(config)
scheduler = None

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


def hl_params():
    """从 env + DB 组装 ExecParams。私钥只从 env 读，绝不落库。"""
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
    base = "https://api.hyperliquid-testnet.xyz" if _env("HL_TESTNET", "0") in ("1", "true") \
        else _env("HL_BASE_URL", "https://api.hyperliquid.xyz")
    return ExecParams(
        mode=hl_mode(),
        account_address=_env("HL_ACCOUNT_ADDRESS"),
        private_key=_env("HL_PRIVATE_KEY"),
        base_url=base,
        coin_map=coin_map,
        leverage=cfg.leverage,
        risk_pct=cfg.risk_pct,
        equity=cfg.equity,
        # 口径 DB > env > 代码默认：Config 已解析好，不要再单独读 env（否则 DB 值会被 env 压掉）
        equity_mode=str(getattr(cfg, "equity_mode", "follow") or "follow"),
        equity_min=float(getattr(cfg, "equity_min", 20.0) or 0.0),
        notify_fills=push_on("fill_push"),
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


def get_hl_info():
    """只读客户端（面板/看护/执行器共用；无凭据也能用）。"""
    global _hl_info
    if _hl_info is None:
        from app.hl.info import HlInfo
        _hl_info = HlInfo(base_url=hl_params().base_url)
    return _hl_info


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
                                                              "exec_alert_fills")]
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


def _job_hl_monitor() -> None:
    """每 5 分钟：快照 + 对账 + 死手开关 + 心跳（与信号 tick 解耦，便于看守判活）。"""
    ex = build_executor()
    if ex is None:
        return
    try:
        ex.run(force_snapshot=True)
    except Exception as e:                                    # noqa: BLE001
        logger.exception("HL 执行器监控轮异常: %s", e)


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
        scheduler.start()
        logger.info("调度器已启动：tick 每小时 :03/:33（UTC 时刻）；心跳 %02d:00 UTC；周报周一；"
                    "HL 监控每 5 分钟（模式=%s）", config.heartbeat_hour, hl_mode())
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="crypto-ma-alert", lifespan=lifespan)


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
def _admin_deps():
    """装配合并面板的依赖（全部走可替换函数，测试里可换成假实现）。"""
    from app.admin_ui import AdminDeps
    hl = _hl_deps()
    return AdminDeps(
        db=db,
        config_fn=current_config,
        hl_deps=hl,
        mode_fn=hl_mode,
        token_fn=lambda: config.admin_token,
        address_fn=lambda: hl_params().account_address,
        private_key_fn=lambda: hl_params().private_key,
        mids_fn=lambda: get_hl_info().all_mids(),
        # 取不到现价时预览降级成「无法取价」，不硬编一个假价格
        price_fallback=0.0,
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
                     short_regime_ma: str = Form("200"), k: str = Form("")):
    _require_admin(request, k or None)
    for key, val in [("equity", equity), ("risk_pct", risk_pct), ("leverage", leverage),
                     ("stop_min", stop_min), ("stop_max", stop_max)]:
        try:
            num = float(val)
        except ValueError:
            raise HTTPException(400, f"{key} 不是数字")
        # 存成 JSON 数字（不是字符串），避免 json.dumps 加引号让 apply_db 解析失败
        db.set_config(key, num)
    # 复选框：勾选=1，未勾选=不传 → 0
    db.set_config("enable_long", 1 if enable_long else 0)
    db.set_config("enable_shorts", 1 if enable_shorts else 0)
    db.set_config("heartbeat_push", 1 if heartbeat_push else 0)
    try:
        int(float(short_regime_ma))
    except ValueError:
        raise HTTPException(400, "short_regime_ma 不是数字")
    db.set_config("short_regime_ma", int(float(short_regime_ma)))
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
        suggest_line = (
            f"建议入场：<b>{row['entry_price']:,.2f}</b>｜止损：<b>{row['stop_price']:,.2f}</b>"
            f"{stop_hint}（{row['stop_dist'] * 100:.2f}%）<br>\n"
            f"      建议张数：<b>{row['contracts']}</b> 张｜名义 <b>{row['notional']:,.0f} USDT</b>｜\n"
            f"      风险 {row['risk_u']:.1f}U（目标 {cfg.equity:,.0f}U × {cfg.risk_pct * 100:.0f}% =\n"
            f"      {cfg.equity * cfg.risk_pct:.1f}U）\n")
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
        lines.append(f"离场 <b>{row['exit_price'] or 0:,.2f}</b>（{row['exit_reason']}）"
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
    body = (f"<h1>{_status_label(row)} #{pid}</h1>"
            f"<div class='card'>{'<br>'.join(lines)}</div>"
            f"<div class='card'>{' '.join(btns) or '<span class=muted>无可操作项</span>'}</div>")
    return page(f"{_status_label(row)} #{pid}", body, request=request)


def _close_form(row, request: Request = None) -> HTMLResponse:
    entry = posfields.entry_px(row)
    stop = posfields.stop_px(row)
    side_txt = "多单" if (row["side"] or 1) == 1 else "空单"
    hint = ""
    if row["status"] == "closed":
        hint = (f"<p class='muted'>当前记录：离场价 <b>{row['exit_price'] or 0:,.2f}</b>"
                f"（{row['exit_reason']}，约 {row['r_multiple'] or 0:+.2f}R）。"
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
        rev = engine.review_fill(current_config(), row["symbol"], row["entry_price"],
                                 row["contracts"], entry, stop, qty, source,
                                 equity=_eq_base)
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
        db.hl_set_kill(False, "")
    return RedirectResponse(f"/admin?tab=switch&k={_urlquote(str(k))}", status_code=303)


@app.post("/admin/hl/kill")
def admin_hl_kill(request: Request, on: str = Form("0"), k: str = Form("")):
    """kill switch：off=恢复正常，on=暂停自动交易（仍允许人工平仓/撤单）。"""
    _require_admin(request, k or None)
    want = on in ("1", "true", "on")
    db.hl_set_kill(want, "人工" if want else "")
    db.hl_log_event("kill", f"人工{'触发' if want else '解除'} kill switch")
    return RedirectResponse(f"/admin?tab=switch&k={_urlquote(str(k))}", status_code=303)
