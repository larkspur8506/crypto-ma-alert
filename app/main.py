"""FastAPI 应用：定时检查 + 状态页 + 确认页（签名链接，无需登录）。"""
from __future__ import annotations

import datetime as dt
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import engine, market, strategy
from app.config import Config
from app.db import DB
from app.notify import get_notifier

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
     margin:0;padding:16px;background:#0f1115;color:#e6e6e6;line-height:1.5}
h1{font-size:18px;margin:0 0 12px}h2{font-size:15px;margin:18px 0 8px;color:#9ad}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid #262a33;padding:6px 8px;text-align:left}
th{color:#8b95a5;font-weight:500}
.card{background:#171a21;border:1px solid #262a33;border-radius:8px;padding:12px;margin-bottom:12px}
.ok{color:#4ade80}.bad{color:#f87171}.muted{color:#8b95a5;font-size:12px}
a.btn,button{display:inline-block;background:#2563eb;color:#fff;border:0;border-radius:6px;
     padding:10px 14px;font-size:14px;text-decoration:none;margin:4px 6px 4px 0;cursor:pointer}
button.gray{background:#374151}button.red{background:#b91c1c}
input{background:#0f1115;border:1px solid #374151;color:#e6e6e6;border-radius:6px;padding:8px;width:130px}
pre{background:#0f1115;border:1px solid #262a33;border-radius:6px;padding:10px;overflow-x:auto;font-size:12px}
</style>
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html><head><meta charset='utf-8'>"
                        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                        f"<title>{title}</title>{PAGE_CSS}</head><body>{body}</body></html>")


def current_config() -> Config:
    return config.apply_db(db.get_config())


# ---------------------------------------------------------------- 调度

def _job_tick() -> None:
    try:
        sent = engine.tick(db, current_config(), notifier)
        if sent:
            logger.info("tick 发送 %d 条提醒", len(sent))
    except Exception as e:                                    # noqa: BLE001
        logger.exception("tick 异常: %s", e)


def _job_heartbeat() -> None:
    cfg = current_config()
    text = engine.heartbeat_text(db, cfg)
    ok = bool(notifier and notifier.send_message(text))
    db.log_alert(None, None, "heartbeat", text, ok)


def _job_weekly() -> None:
    cfg = current_config()
    text = engine.weekly_text(db, cfg)
    ok = bool(notifier and notifier.send_message(text))
    db.log_alert(None, None, "weekly", text, ok)


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
        scheduler.start()
        logger.info("调度器已启动：tick 每小时 :03/:33；心跳 %02d:00 UTC；周报周一", config.heartbeat_hour)
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


# ---------------------------------------------------------------- 路由

@app.get("/health")
def health():
    return {"status": "ok", "symbols": config.symbols,
            "scheduler": bool(scheduler), "dry_run": config.dry_run}


@app.get("/")
def root():
    return RedirectResponse("/admin")


@app.get("/admin", response_class=HTMLResponse)
def admin_status(request: Request, k: Optional[str] = None):
    _require_admin(request, k)
    cfg = current_config()
    rows = "".join(
        f"<tr><td>{r['symbol']}</td><td>{'多' if (r['side'] or 1) == 1 else '空'}</td>"
        f"<td>{r['status']}</td><td>{r['contracts']}</td>"
        f"<td>{r['entry_price']:,.0f}</td><td>{r['stop_price']:,.0f}</td>"
        f"<td>{r['stop_dist'] * 100:.1f}%</td>"
        f"<td>{engine.fmt_ms(r['signal_bar_ms'])}</td>"
        f"<td><a href='/c/{r['id']}?a=view&t={engine.sign_token(cfg.token_secret, r['id'], 'view')}'>查看</a></td></tr>"
        for r in db.recent_positions(20))
    feeds = "".join(
        f"<tr><td>{r['symbol']}</td><td class='{'ok' if r['last_ok_at'] else 'bad'}'>"
        f"{r['source'] if r['last_ok_at'] else '失败'}</td>"
        f"<td class='muted'>{r['last_error'] or ''}</td></tr>" for r in db.feed_status())
    last_bars = "".join(
        f"<tr><td>{s}</td><td>{engine.fmt_ms(db.get_last_bar(s)) if db.get_last_bar(s) else '—'}</td></tr>"
        for s in cfg.symbols)
    body = f"""
    <h1>{cfg.alert_prefix}crypto-ma-alert</h1>
    <div class='card'>
      <b>配置</b><br>标的：{', '.join(cfg.symbols)}｜权益 {cfg.equity:,.0f}U｜单笔风险 {cfg.risk_pct * 100:.1f}%｜
      杠杆 {cfg.leverage:.0f}x｜止损过滤 {cfg.stop_min * 100:.1f}%~{cfg.stop_max * 100:.1f}%<br>
      <span class='muted'>通道：{'企业微信 ' if cfg.wechat_webhook else ''}{'Telegram' if cfg.telegram_token and cfg.telegram_chat_id else ''}
      ｜dry_run={cfg.dry_run}｜<a href='/admin/rules?k={config.admin_token}'>改参数</a>｜
      <a href='/admin/logs?k={config.admin_token}'>提醒日志</a></span>
    </div>
    <h2>数据源健康</h2><table><tr><th>标的</th><th>状态</th><th>错误</th></tr>{feeds}</table>
    <h2>已处理到</h2><table><tr><th>标的</th><th>最后处理 bar</th></tr>{last_bars}</table>
    <h2>最近仓位/信号</h2>
    <table><tr><th>标的</th><th>方向</th><th>状态</th><th>张数</th><th>入场</th><th>止损</th><th>距离</th><th>信号 bar</th><th></th></tr>
    {rows or "<tr><td colspan=9 class='muted'>暂无</td></tr>"}</table>
    <div class='card muted'>待确认（pending）的仓位按假设值计入统计；点「查看」可确认或忽略。</div>
    """
    resp = page("crypto-ma-alert", body)
    if k:
        resp.set_cookie("cma_admin", k, httponly=True, max_age=30 * 86400)
    return resp


@app.get("/admin/rules", response_class=HTMLResponse)
def admin_rules(request: Request, k: Optional[str] = None):
    _require_admin(request, k)
    cfg = current_config()
    saved = db.get_config()
    body = f"""
    <h1>参数</h1>
    <div class='card'>
    <form method='post'>
      <input type='hidden' name='k' value='{k or ""}'>
      <p>账户权益(U) <input name='equity' value='{cfg.equity}'></p>
      <p>单笔风险(小数，0.02=2%) <input name='risk_pct' value='{cfg.risk_pct}'></p>
      <p>杠杆(逐仓) <input name='leverage' value='{cfg.leverage}'></p>
      <p>止损距离下限 <input name='stop_min' value='{cfg.stop_min}'></p>
      <p>止损距离上限 <input name='stop_max' value='{cfg.stop_max}'></p>
      <button type='submit'>保存</button>
    </form>
    <div class='muted'>DB 值优先于环境变量。当前 DB 覆盖：{saved or '无'}</div>
    </div>
    <p><a class='btn gray' href='/admin?k={k or ""}'>返回</a></p>
    """
    return page("参数", body)


@app.post("/admin/rules")
def admin_rules_post(request: Request, equity: str = Form(...), risk_pct: str = Form(...),
                     leverage: str = Form(...), stop_min: str = Form(...), stop_max: str = Form(...),
                     k: str = Form("")):
    _require_admin(request, k)
    for key, val in [("equity", equity), ("risk_pct", risk_pct), ("leverage", leverage),
                     ("stop_min", stop_min), ("stop_max", stop_max)]:
        try:
            float(val)
        except ValueError:
            raise HTTPException(400, f"{key} 不是数字")
        db.set_config(key, val)
    return RedirectResponse(f"/admin/rules?k={k}", status_code=303)


@app.get("/admin/logs", response_class=HTMLResponse)
def admin_logs(request: Request, k: Optional[str] = None):
    _require_admin(request, k)
    rows = "".join(
        f"<tr><td>{engine.fmt_ms(int(r['created_at'] * 1000))}</td><td>{r['kind']}</td>"
        f"<td>{r['symbol'] or '—'}</td>"
        f"<td class='{'ok' if r['sent_successfully'] else 'bad'}'>"
        f"{'已送达' if r['sent_successfully'] else '失败'}</td>"
        f"<td><details><summary>内容</summary><pre>{r['message']}</pre></details></td></tr>"
        for r in db.recent_alerts(50))
    return page("提醒日志", f"<h1>提醒日志</h1><table><tr><th>时间</th><th>类型</th><th>标的</th>"
                            f"<th>状态</th><th>内容</th></tr>{rows}</table>"
                            f"<p><a class='btn gray' href='/admin?k={k or ''}'>返回</a></p>")


# ---------------------------------------------------------------- 确认页

@app.get("/c/{pid}", response_class=HTMLResponse)
def confirm_view(pid: int, request: Request, a: str = "view", t: str = ""):
    if not engine.verify_token(config.token_secret, pid, a, t):
        raise HTTPException(401, "链接无效或已过期")
    row = db.position_by_id(pid)
    if not row:
        raise HTTPException(404, "记录不存在")
    if a == "close":
        return _close_page(row)
    tok = lambda act: engine.sign_token(config.token_secret, pid, act)      # noqa: E731
    side_txt = "做多" if (row["side"] or 1) == 1 else "做空"
    stop_hint = "（价格下方）" if (row["side"] or 1) == 1 else "（价格上方）"
    body = f"""
    <h1>确认开仓 #{pid}</h1>
    <div class='card'>
      <b>{row['symbol']}</b> {side_txt}｜信号 bar {engine.fmt_ms(row['signal_bar_ms'])} UTC<br>
      建议入场：<b>{row['entry_price']:,.2f}</b>｜止损：<b>{row['stop_price']:,.2f}</b>{stop_hint}
      （{row['stop_dist'] * 100:.2f}%）<br>
      建议张数：<b>{row['contracts']}</b> 张｜名义 <b>{row['notional']:,.0f} USDT</b>｜
      风险 {row['risk_u']:.1f}U
    </div>
    <form method='post'>
      <input type='hidden' name='a' value='open'><input type='hidden' name='t' value='{tok("open")}'>
      <p>实际成交价 <input name='actual_entry' value='{row["entry_price"]:.2f}'></p>
      <p>实际张数 <input name='actual_contracts' value='{row["contracts"]}'></p>
      <p>实际止损触发价 <input name='actual_stop' value='{row["stop_price"]:.2f}'></p>
      <p><label><input type='checkbox' name='stop_placed' value='1' checked style='width:auto'> 已挂止损单</label></p>
      <button type='submit'>按以上数值确认开仓</button>
    </form>
    <form method='post' style='margin-top:8px'>
      <input type='hidden' name='a' value='ignore'><input type='hidden' name='t' value='{tok("ignore")}'>
      <button class='red' type='submit'>未开仓 / 忽略</button>
    </form>
    <div class='card muted'>状态：{row['status']}</div>
    """
    return page(f"确认开仓 #{pid}", body)


def _close_page(row) -> HTMLResponse:
    entry = row["actual_entry"] or row["entry_price"]
    stop = row["actual_stop"] or row["stop_price"]
    side_txt = "多单" if (row["side"] or 1) == 1 else "空单"
    body = f"""
    <h1>确认平仓 #{row['id']}</h1>
    <div class='card'>
      <b>{row['symbol']}</b> {side_txt}｜入场 {entry:,.2f}｜止损 {stop:,.2f}
    </div>
    <form method='post'>
      <input type='hidden' name='a' value='close'>
      <input type='hidden' name='t' value='{engine.sign_token(config.token_secret, row["id"], "close")}'>
      <p>实际平仓价 <input name='exit_price' value='{row["exit_price"] or 0:.2f}'></p>
      <p>离场原因
        <select name='exit_reason' style='background:#0f1115;color:#e6e6e6;padding:8px;border-radius:6px'>
          <option value='ma20'>收到提醒后平仓</option>
          <option value='stop'>止损单被触发</option>
          <option value='manual'>手动提前平</option>
        </select></p>
      <button type='submit'>确认平仓</button>
    </form>
    """
    return page(f"确认平仓 #{row['id']}", body)


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
        db.ignore_position(pid)
        return page("已忽略", f"<h1>已忽略 #{pid}</h1><p>{row['symbol']} 这笔不计入统计。</p>")
    if a == "close":
        try:
            px = float(str(form.get("exit_price") or row["entry_price"]))
        except ValueError:
            raise HTTPException(400, "平仓价不是数字")
        reason = str(form.get("exit_reason") or "ma20")
        db.close_position(pid, px, reason)
        return page("已平仓", f"<h1>已记录平仓 #{pid}</h1><p>{row['symbol']} @ {px:,.2f}（{reason}）</p>")
    if a == "open":
        try:
            entry = float(str(form.get("actual_entry") or row["entry_price"]))
            qty = int(float(str(form.get("actual_contracts") or row["contracts"])))
            stop = float(str(form.get("actual_stop") or row["stop_price"]))
        except ValueError:
            raise HTTPException(400, "数值格式错误")
        placed = bool(form.get("stop_placed"))
        db.confirm_position(pid, entry, qty, stop, placed)
        warn = "" if placed else "<p class='bad'>⚠️ 未挂止损单，这笔的风险不受保护</p>"
        return page("已确认", f"<h1>已确认开仓 #{pid}</h1>"
                              f"<p>{row['symbol']} @ {entry:,.2f}，{qty} 张，止损 {stop:,.2f}</p>{warn}")
    raise HTTPException(400, f"未知操作 {a}")


@app.post("/admin/tick")
def admin_tick(request: Request, k: Optional[str] = None):
    _require_admin(request, k)
    sent = engine.tick(db, current_config(), notifier)
    return {"sent": len(sent), "messages": sent}
