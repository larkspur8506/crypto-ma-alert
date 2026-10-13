"""FastAPI 应用：定时检查 + 状态页 + 确认页（签名链接，无需登录）。"""
from __future__ import annotations

import datetime as dt
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import engine, market, sizing, strategy
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
    if cfg.heartbeat_push:
        ok = bool(notifier and notifier.send_message(text))
        db.log_alert(None, None, "heartbeat", text, ok)
        return
    # 静默信标：只落库不推送（每日巡检已把同样信息汇总给用户）。
    # kind='heartbeat_local' 仅供 cma_watch.py / cma_status.py 判断调度器存活。
    db.log_alert(None, None, "heartbeat_local", text, True)
    logger.info("心跳（未推送，仅落库）:\n%s", text)


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
      <p><label><input type='checkbox' name='enable_long' {'checked' if cfg.enable_long else ''}>
         允许做多</label>
         <label><input type='checkbox' name='enable_shorts' {'checked' if cfg.enable_shorts else ''}>
         允许做空</label>
         <label><input type='checkbox' name='heartbeat_push' {'checked' if cfg.heartbeat_push else ''}>
         每天推送心跳（默认关：信息已在每日巡检里）</label></p>
      <p>做空趋势过滤均线 <input name='short_regime_ma' value='{cfg.short_regime_ma}'>
         （0 = 关闭过滤，不建议）</p>
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
                     enable_long: Optional[str] = Form(None),
                     enable_shorts: Optional[str] = Form(None),
                     heartbeat_push: Optional[str] = Form(None),
                     short_regime_ma: str = Form("200"), k: str = Form("")):
    _require_admin(request, k)
    for key, val in [("equity", equity), ("risk_pct", risk_pct), ("leverage", leverage),
                     ("stop_min", stop_min), ("stop_max", stop_max)]:
        try:
            float(val)
        except ValueError:
            raise HTTPException(400, f"{key} 不是数字")
        db.set_config(key, val)
    # 复选框：勾选=1，未勾选=不传 → 0
    db.set_config("enable_long", 1 if enable_long else 0)
    db.set_config("enable_shorts", 1 if enable_shorts else 0)
    db.set_config("heartbeat_push", 1 if heartbeat_push else 0)
    try:
        int(float(short_regime_ma))
    except ValueError:
        raise HTTPException(400, "short_regime_ma 不是数字")
    db.set_config("short_regime_ma", int(float(short_regime_ma)))
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
    if a in ("close", "edit_close"):
        # 提醒消息里的 a=close 链接：该仓尚未录入真实平仓价（系统自动关的）→ 直接给表单；
        # 已经录入过（用户自己确认的）→ 给只读摘要，改正要走「修改平仓数据」按钮。
        if a == "close" and row["status"] == "closed" and not row["auto_closed"]:
            return _summary(row, pid)
        return _close_form(row)
    if a == "edit_open":
        return _entry_form(row, pid, editing=True)
    if row["status"] == "pending":
        return _entry_form(row, pid, editing=False)
    # 已确认开仓 / 已平仓 → 只读摘要（改动必须点「修改」显式进入表单，防手滑覆盖）
    return _summary(row, pid)


def _entry_form(row, pid: int, editing: bool) -> HTMLResponse:
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
                f"张数 {row['actual_contracts'] or row['contracts']}）。</p>")
    cur_entry = (row["actual_entry"] or row["entry_price"]) if editing else row["entry_price"]
    cur_stop = (row["actual_stop"] or row["stop_price"]) if editing else row["stop_price"]
    cur_qty = (row["actual_contracts"] or row["contracts"]) if editing else row["contracts"]
    edit_flag = "<input type='hidden' name='edit' value='1'>" if editing else ""
    title = f"修改开仓数据 #{pid}" if editing else f"确认开仓 #{pid}"
    body = f"""
    <h1>{title}</h1>
    <div class='card'>
      <b>{row['symbol']}</b> {side_txt}｜信号 bar {engine.fmt_ms(row['signal_bar_ms'])} UTC<br>
      建议入场：<b>{row['entry_price']:,.2f}</b>｜止损：<b>{row['stop_price']:,.2f}</b>{stop_hint}
      （{row['stop_dist'] * 100:.2f}%）<br>
      建议张数：<b>{row['contracts']}</b> 张｜名义 <b>{row['notional']:,.0f} USDT</b>｜
      风险 {row['risk_u']:.1f}U（目标 {cfg.equity:,.0f}U × {cfg.risk_pct * 100:.0f}% =
      {cfg.equity * cfg.risk_pct:.1f}U）
      {note}
    </div>
    <form method='post'>
      <input type='hidden' name='a' value='open'><input type='hidden' name='t' value='{tok("open")}'>{edit_flag}
      <p>实际成交均价 <input name='actual_entry' value='{cur_entry:.2f}'></p>
      <p>成交金额 USDT <input name='fill_amount' value='' placeholder='成交记录里的金额'>
        <span class='muted'>填了就按它换算张数（比下单时看到的预览张数准）</span></p>
      <p>实际张数 <input name='actual_contracts' value='{cur_qty}'>
        <span class='muted'>没填成交金额时用这个</span></p>
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
    return page(title, body)


def _status_label(row) -> str:
    return {"pending": "待确认", "open": "持仓中", "closed": "已平仓",
            "ignored": "已忽略"}.get(row["status"], row["status"])


def _summary(row, pid: int) -> HTMLResponse:
    """只读摘要页：只显示当前记录 + 显式入口（修改 / 手动平仓）。"""
    tok = lambda act: engine.sign_token(config.token_secret, pid, act)      # noqa: E731
    side_txt = "多单" if (row["side"] or 1) == 1 else "空单"
    entry = row["actual_entry"] or row["entry_price"]
    stop = row["actual_stop"] or row["stop_price"]
    qty = row["actual_contracts"] or row["contracts"]
    lines = [
        f"<b>{row['symbol']}</b> {side_txt}｜信号 bar {engine.fmt_ms(row['signal_bar_ms'])} UTC",
        f"入场 <b>{entry:,.2f}</b>｜止损 <b>{stop:,.2f}</b>｜{qty} 张",
    ]
    if row["status"] == "closed":
        lines.append(f"离场 <b>{row['exit_price'] or 0:,.2f}</b>（{row['exit_reason']}）"
                     f"｜本次 <b>{row['r_multiple'] or 0:+.2f}R</b>")
        if row["auto_closed"]:
            lines.append("<span class='muted'>离场价是系统按参考价记录的；"
                         "若与 MEXC 实际成交不同，点下面的修改按钮改正。</span>")
    btns = []
    if row["status"] in ("open", "closed"):
        btns.append(f"<a class='btn' href='/c/{pid}?a=edit_open&t={tok('edit_open')}'>修改开仓数据</a>")
    if row["status"] == "open":
        btns.append(f"<a class='btn gray' href='/c/{pid}?a=close&t={tok('close')}'>手动平仓</a>")
    if row["status"] == "closed":
        btns.append(f"<a class='btn' href='/c/{pid}?a=edit_close&t={tok('edit_close')}'>修改平仓数据</a>")
    body = (f"<h1>{_status_label(row)} #{pid}</h1>"
            f"<div class='card'>{'<br>'.join(lines)}</div>"
            f"<div class='card'>{' '.join(btns) or '<span class=muted>无可操作项</span>'}</div>")
    return page(f"{_status_label(row)} #{pid}", body)


def _close_form(row) -> HTMLResponse:
    entry = row["actual_entry"] or row["entry_price"]
    stop = row["actual_stop"] or row["stop_price"]
    side_txt = "多单" if (row["side"] or 1) == 1 else "空单"
    hint = ""
    if row["status"] == "closed":
        hint = (f"<p class='muted'>当前记录：离场价 <b>{row['exit_price'] or 0:,.2f}</b>"
                f"（{row['exit_reason']}，约 {row['r_multiple'] or 0:+.2f}R）。"
                f"保存后按新价重算 R。</p>")
    body = f"""
    <h1>录入平仓 #{row['id']}</h1>
    <div class='card'>
      <b>{row['symbol']}</b> {side_txt}｜入场 {entry:,.2f}｜止损 {stop:,.2f}
      {hint}
    </div>
    <form method='post'>
      <input type='hidden' name='a' value='close'>
      <input type='hidden' name='t' value='{engine.sign_token(config.token_secret, row["id"], "close")}'>
      {'<input type="hidden" name="edit" value="1">' if row['status'] == 'closed' else ''}
      <p>实际平仓价 <input name='exit_price' value='{row["exit_price"] or 0:.2f}'></p>
      <p>离场原因
        <select name='exit_reason' style='background:#0f1115;color:#e6e6e6;padding:8px;border-radius:6px'>
          <option value='ma20' {'selected' if row['exit_reason'] == 'ma20' else ''}>收到提醒后平仓</option>
          <option value='stop' {'selected' if row['exit_reason'] == 'stop' else ''}>止损单被触发</option>
          <option value='manual' {'selected' if row['exit_reason'] == 'manual' else ''}>手动提前平</option>
        </select></p>
      <button type='submit'>保存平仓价</button>
    </form>
    <p><a class='btn gray' href='/c/{row['id']}?a=view&t={engine.sign_token(config.token_secret, row['id'], "view")}'>返回</a></p>
    """
    return page(f"录入平仓 #{row['id']}", body)


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
        return page("已忽略", f"<h1>已忽略 #{pid}</h1><p>{row['symbol']} 这笔不计入统计。</p>")
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
        return page("已平仓", f"<h1>已记录平仓 #{pid}</h1><p>{row['symbol']} @ {px:,.2f}（{reason}）</p>")
    if a == "open":
        editing = bool(form.get("edit"))
        if row["status"] == "pending":
            pass
        elif row["status"] in ("open", "closed") and editing:
            pass          # 从「修改开仓数据」进来，允许改正
        else:
            raise HTTPException(400, f"该仓位状态是 {row['status']}；如需改正请从确认页点「修改开仓数据」")
        try:
            entry = float(str(form.get("actual_entry") or row["entry_price"]))
            stop = float(str(form.get("actual_stop") or row["stop_price"]))
        except ValueError:
            raise HTTPException(400, "数值格式错误")
        err = engine.validate_stop_side(row["side"] or 1, entry, stop)
        if err:
            raise HTTPException(400, err)
        raw_amount = str(form.get("fill_amount") or "").strip()
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
                qty = int(float(str(form.get("actual_contracts") or row["contracts"])))
            except ValueError:
                raise HTTPException(400, "张数不是数字")
        placed = bool(form.get("stop_placed"))
        if row["status"] == "pending":
            db.confirm_position(pid, entry, qty, stop, placed)
        else:
            db.update_fill(pid, entry, qty, stop, placed)   # 修改已确认的记录
        rev = engine.review_fill(current_config(), row["symbol"], row["entry_price"],
                                 row["contracts"], entry, stop, qty, source)
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
                              f"<span class='muted'>{src_txt}</span></p>{warn}{review}")
    raise HTTPException(400, f"未知操作 {a}")


@app.post("/admin/tick")
def admin_tick(request: Request, k: Optional[str] = None):
    _require_admin(request, k)
    sent = engine.tick(db, current_config(), notifier)
    return {"sent": len(sent), "messages": sent}
