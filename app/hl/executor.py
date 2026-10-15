"""HL 执行器：把信号变成订单，把订单变成可审计的记录。

设计原则（可靠性优先）：
- **fail-closed**：任何不确定（读不到账户、对账不一致、止损挂不上、SDK 不可用）→ 不交易，
  必要时立刻平掉已开仓位。宁可丢机会，不可留无保护仓位。
- **幂等**：每根 bar 的每个动作有唯一 cloid + 唯一 DB 键；重放/重试都不会双开。
- **可审计**：先落 intent 再发请求；每步都写 hl_events；面板/看护都读这些表。
- **可回滚**：模式 off/dry/live 三态，任何时刻可以退回「只提醒」。

本模块不 import 官方 SDK（那在 exchange.py 里）。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import math
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.hl import (MODE_DRY, MODE_LIVE, MODE_OFF, PURPOSE_ENTRY, PURPOSE_EXIT, PURPOSE_EXIT_REHANG,
                    PURPOSE_STOP,
                    ST_FAILED, ST_FILLED, ST_INTENT, ST_PLANNED, ST_RESTING, ST_SENT, ST_SKIPPED,
                    EV_DECISION, EV_ERROR, EV_HEARTBEAT, EV_KILL, EV_ORDER, EV_RECONCILE)
from app.hl import sizing as hl_sizing
from app.hl.exchange import STATUS_DRY, STATUS_FILLED, STATUS_RESTING, make_cloid
from app.engine import DISPLAY_TZ, TZ_LABEL
from app.hl.info import HlInfoError

logger = logging.getLogger(__name__)

DEFAULT_COIN_MAP = {"BTC_USDT": "BTC", "ETH_USDT": "ETH"}


# ---------------------------------------------------------------- 推送文案（人类可读）

def _fmt_u(x, nd: int = 2) -> str:
    return f"{x:,.{nd}f}"


def _side_word(side: int) -> str:
    return "做多" if int(side or 1) >= 0 else "做空"


def fmt_entry_alert(coin: str, side: int, sz: float, entry_px: float, stop: float,
                    stop_dist: float, notional: float, margin: float, liq: float,
                    risk_u: float, equity: float, signal_px: float, oid, stop_oid,
                    when: "dt.datetime", tz_label: str,
                    leverage: float = 5.0) -> str:
    """开仓回报：价格 / 数量 / 保证金 / 强平 / 风险 分行排，别糊成一坨。

    equity 传**生效基数**（effective_equity），不是配置 equity —— follow 模式下
    两者默认不同，传错会让「权益的 X%」差一个数量级。
    """
    drift = ((entry_px - signal_px) / signal_px * 100) if signal_px else 0.0
    stop_pct = ((stop - entry_px) / entry_px * 100) if entry_px else 0.0
    pct = (risk_u / equity * 100) if equity else 0.0
    return "\n".join([
        f"【HL执行】✅ 已开仓 · {coin} {_side_word(side)} {leverage:g}x 逐仓",
        "",
        f"成交价    {_fmt_u(entry_px)}",
        f"数量      {sz:g} {coin}（名义 {_fmt_u(notional, 1)} U）",
        f"止损价    {_fmt_u(stop)}（{stop_pct:+.2f}%）",
        f"保证金    ≈{_fmt_u(margin, 1)} U",
        f"强平价    {_fmt_u(liq)}",
        f"风险      {_fmt_u(risk_u)} U（权益的 {pct:.1f}%）",
        f"滑点      {drift:+.2f}%（信号价 {_fmt_u(signal_px)}）",
        f"时间      {when.strftime('%m-%d %H:%M')} {tz_label}",
        f"单号      开仓 {oid} · 止损 {stop_oid}",
    ])


def fmt_exit_alert(coin: str, side: int, entry_px: float, exit_px: float, sz: float,
                   reason: str, r_multiple: float, gross_pnl: float, fees: float,
                   opened_at, when: "dt.datetime", tz_label: str) -> str:
    """平仓回报：说清**为什么平**（止损/策略离场）+ 价格 + 盈亏（含手续费）。"""
    net = gross_pnl - fees
    if reason == "stop":
        head, why = "🛑 止损离场", "止损单被触发"
    elif reason == "ma20":
        head = "📈 策略止盈" if net > 0 else ("📉 策略离场" if net < 0 else "➖ 策略离场")
        why = "收盘跌破 SMA20" if int(side or 1) >= 0 else "收盘站上 SMA20"
    else:
        head, why = "已平仓", str(reason)
    hold = ""
    try:
        mins = max(0, int((when.timestamp() - float(opened_at)) // 60))
        if mins >= 1440:
            d, h = mins // 1440, mins % 1440 // 60
            hold = f"{d} 天" + (f" {h} 小时" if h else "")
        elif mins >= 60:
            h, m = mins // 60, mins % 60
            hold = f"{h} 小时" + (f" {m} 分钟" if m else "")
        else:
            hold = f"{mins} 分钟"
    except (TypeError, ValueError):
        pass
    lines = [
        f"【HL执行】{head} · {coin} {_side_word(side)}",
        "",
        f"离场原因  {why}",
        f"平仓价    {_fmt_u(exit_px)}（开仓 {_fmt_u(entry_px)}）",
        f"本次盈亏  {net:+,.2f} U（{r_multiple:+.2f}R）",
        f"盈亏构成  价差 {gross_pnl:+,.2f} U · 手续费 {_fmt_u(fees)} U",
    ]
    if hold:
        lines.append(f"持仓时长  {hold}")
    lines.append(f"时间      {when.strftime('%m-%d %H:%M')} {tz_label}")
    return "\n".join(lines)


@dataclass
class ExecParams:
    mode: str = MODE_OFF
    account_address: str = ""
    private_key: str = ""
    base_url: str = "https://api.hyperliquid.xyz"
    coin_map: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COIN_MAP))
    leverage: float = 5.0
    risk_pct: float = 0.02
    equity: float = 1000.0
    equity_mode: str = "follow"          # follow = 跟随实时权益（带下限）；fixed = 用上面的固定值
    equity_min: float = 20.0
    notify_fills: bool = True            # 成交回报推送（面板开关 fill_push）；异常提醒不受此开关影响
    max_slippage_pct: float = 0.003
    # 名义上限（单标的 / 合计，单位=权益倍数）。**默认 0 = 不设限**（用户拍板 2026-10-14）：
    # 仓位已由「风险 ÷ 止损距离」定死（单笔风险恒 risk_pct），上限只会误伤合法信号
    # —— 实测 1000U 账户下两个标的都用紧止损（1.5%）时合计 2.67x > 2.5x，第二个仓会被卡掉。
    # 留成可选（>0 生效）而不是删掉：万一将来想加暴露度护栏，改配置即可。
    max_notional_mult: float = 0.0
    max_total_notional_mult: float = 0.0
    max_orders_per_day: int = 4
    max_fail_streak: int = 3
    max_daily_loss_pct: float = 0.05
    deadman_minutes: int = 30
    # 死手开关（HL 原生 scheduleCancel）：默认**关闭**。理由见 docs/HL_RUNBOOK.md —— 它的语义是
    # 「机器人失联就撤掉所有挂单」，会连持仓的止损单一起撤（对持仓型策略是反向保险），
    # 且低成交量账户本身就调不通（每轮报错）。要启用必须显式开 hl_use_schedule_cancel。
    use_schedule_cancel: bool = False
    min_notional: float = 10.0
    snapshot_min_interval_s: float = 240.0
    stop_max_hard: float = 0.10           # 执行时止损距离超此值直接放弃
    code_rev: str = ""

    @property
    def follow(self) -> bool:
        return str(self.equity_mode or "follow").lower() != "fixed"

    @property
    def live(self) -> bool:
        return self.mode == MODE_LIVE


def _today_start_ts(now: Optional[float] = None) -> float:
    """当日 0 点（**北京时间**，与展示层一致；旧版 UTC 日界会让「今日下单数/
    今日已实现」比用户直觉早/晚 8 小时）。"""
    tz = dt.timezone(dt.timedelta(hours=int(os.environ.get("DISPLAY_TZ_OFFSET", "8"))))
    d = dt.datetime.fromtimestamp(now if now is not None else dt.datetime.now(
        dt.timezone.utc).timestamp(), tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Executor:
    def __init__(self, db, params: ExecParams, info, exchange, notifier=None):
        self.db = db
        self.p = params
        self.info = info
        self.ex = exchange
        self.notifier = notifier
        # 进程内互斥：run()/on_entry/on_exit 会做「撤单→下单→落库」多步序列，
        # 两个线程并发跑同一段（_job_hl_monitor 与 tick 的 run、或两轮 tick）会
        # 重放同一确定性 cloid → 被交易所拒单 → 误判失败 → 走「兜底平仓」误平仓。
        # APScheduler 的 max_instances 只约束各自的 job，管不了跨 job 并发。
        self._op_lock = threading.Lock()

    # ------------------------------------------------------------ 工具
    def coin(self, symbol: str) -> str:
        return self.p.coin_map.get(symbol, symbol.split("_")[0])

    def _alert_fill(self, text: str) -> None:
        """成交回报（开仓/平仓成功）。受面板「成交回报推送」开关控制；异常类提醒一律走 _alert。

        开关关闭时也落库（kind=exec_alert_fills，不进补推白名单——开关是用户意图），
        这样 alert_logs 里有完整审计轨迹，且开关重新打开后可见历史。
        """
        if not getattr(self.p, "notify_fills", True):
            try:
                self.db.log_alert(None, None, "exec_alert_fills", text, True)
            except Exception as e:                                   # noqa: BLE001
                logger.error("[hl-exec] 成交回报落库失败：%s", e)
            return
        self._alert(text, kind="exec_alert_fills")

    def _alert(self, text: str, symbol: Optional[str] = None,
               kind: str = "exec_alert") -> None:
        """执行器告警：直发 + 落 alert_logs。

        「平仓失败（止损已撤）」「止损单丢失」这类告警一推失败就是永久丢
        （引擎侧走 _emit 有 24h 补推，执行器侧以前完全没有）—— 现在也写进
        alert_logs（bar_ms=NULL + 时间窗去重，同 data_error 的约定），
        main._retry_failed_alerts 会照常补推。
        """
        ok = False
        try:
            ok = bool(self.notifier is not None and self.notifier.send_message(text))
        except Exception as e:                                       # noqa: BLE001
            logger.error("[hl-exec] 推送失败：%s", e)
            ok = False
        try:
            self.db.log_alert(symbol, None, kind, text, ok)
        except Exception as e:                                       # noqa: BLE001
            logger.error("[hl-exec] 告警落库失败（补推将不可用）：%s", e)
        return ok

    def _event(self, kind: str, message: str, symbol: Optional[str] = None,
               data: Any = None) -> None:
        try:
            self.db.hl_log_event(kind, message, symbol=symbol, data=data, code_rev=self.p.code_rev)
        except Exception as e:                                       # noqa: BLE001
            logger.error("[hl-exec] 写事件失败：%s", e)

    def _snapshot(self, force: bool = False):
        """读账户快照；>=snapshot_min_interval_s 才落库一次（面板曲线用）。失败返回 None。"""
        try:
            snap = self.info.snapshot(self.p.account_address)
        except HlInfoError as e:
            self._event(EV_ERROR, f"读取账户失败：{e}")
            return None
        if not self.p.account_address:
            return snap
        last = self.db.hl_latest_snapshot()
        if force or last is None or (snap["ts"] - float(last["ts"])) >= self.p.snapshot_min_interval_s:
            self.db.hl_add_snapshot(snap)
        return snap

    # ------------------------------------------------------------ 对账
    def reconcile(self, snap: Optional[dict] = None) -> dict:
        """以交易所为权威核对本地状态。返回 {"ok":bool,"problems":[str,...]}。"""
        snap = snap if snap is not None else self._snapshot()
        if snap is None:
            return {"ok": False, "problems": ["读不到账户状态（info 失败）"], "unknown": True}
        ex_pos = {p["coin"]: p for p in snap.get("positions") or []}
        # 挂单列表可信才允许判断「止损有无」：回落老接口时读不到 isTrigger，会把有止损读成没止损，
        # 据此自动补挂/平仓 = 误平仓。缺字段按**不可信**处理 —— 与面板（views）和看护
        # （watchdog）同口径（fail-closed）：唯一生产来源 info.snapshot 总会显式写这个键，
        # 缺键只可能出现在桩/半成品数据里，那种情况宁可只告警、不自动动仓。
        trusted = bool(snap.get("orders_trusted", False))
        problems: List[str] = []
        missing_stop: List[dict] = []
        unconfirmed: List[str] = []
        local_open = [r for r in self.db.recent_positions(50)
                      if r["status"] in ("open", "pending")
                      # hl_status 为 NULL 也算：on_entry 在「入场成交→落库」之间崩溃时
                      # 仓位行还没来得及写 hl_status='open'，这样的仓同样需要止损保护/对账，
                      # 否则只能走到 kill 分支当「人工单」处理。
                      # 审计三轮 M4：status='pending' 也纳入 —— on_entry 在「交易所成交
                      # → confirm_position」之间崩溃时行还是 pending，真实裸仓以前只会被
                      # 打成「人工单」kill+告警，永不补挂止损。
                      # 审计四轮 P1：pending 行**必须真的发过入场单**才算真实仓位。
                      # on_entry 在读数据阶段就退出（快照/规格/mid 失败 → entry 行记成
                      # skipped）时交易所什么都没有，无条件纳入会让下一轮对账报
                      # 「本地有仓但交易所无仓位」→ 误置 kill + 误标 mismatch。
                      and (r["status"] == "open" or self._entry_issued(r))
                      and (r["hl_status"] or "") in ("open", "mismatch", "")]
        seen = set()
        for r in local_open:
            coin = self.coin(r["symbol"])
            seen.add(coin)
            if coin not in ex_pos:
                problems.append(f"{r['symbol']} 本地有仓（#{r['id']}）但交易所无仓位")
                self.db.set_pos_hl(r["id"], hl_status="mismatch")
                continue
            if not self._has_stop(snap, coin):
                if trusted:
                    problems.append(f"{r['symbol']} 有仓但**没有止损挂单**")
                    missing_stop.append(dict(r))
                else:
                    unconfirmed.append(f"{r['symbol']} 有仓但无法确认止损"
                                       f"（挂单接口回落，读不到触发单）")
        for coin in ex_pos:
            if coin not in seen:
                problems.append(f"交易所有 {coin} 仓位但本地无记录（人工单？）")
        return {"ok": not problems, "problems": problems, "unknown": False,
                "missing_stop": missing_stop, "unconfirmed": unconfirmed}

    def _repair_missing_stop(self, row: dict, snap: dict) -> None:
        """**确认**有仓却没有止损挂单 → 先自动补挂原止损价；补不上就市价平掉。

        与开仓路径同一口径：不允许无保护持仓存在。用独立 cloid（stop:fix）避免与原单撞号。
        补挂成功也不解锁 kill —— 异常已经发生，由人工核对后决定是否恢复自动交易。
        """
        symbol = str(row.get("symbol"))
        coin = self.coin(symbol)
        bar_ms = int(row.get("signal_bar_ms") or 0)
        side = int(row.get("side") or 1)
        # 非 live 模式绝不动交易所：dry 的 place_order 返回 STATUS_DRY（非
        # RESTING/FILLED），按失败处理会误走「补挂失败→市价平仓」分支，
        # 推出吓人且失实的告警（实际什么都没发）。dry 下只提示人工。
        if not self.p.live:
            self._event(EV_ERROR, f"dry 模式：{symbol} 止损缺失，不动交易所，仅提示",
                        symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 交易所仓位缺止损单，但执行器不是 live 模式，"
                        f"未自动处理 —— 请人工查看并处理。")
            return
        stop = float(row.get("stop_price") or 0.0)
        sz = float(row.get("hl_sz") or 0.0)
        if sz <= 0:                                   # 本地没记数量 → 以交易所为准
            ex_pos = {p["coin"]: p for p in (snap.get("positions") or [])}
            sz = abs(float((ex_pos.get(coin) or {}).get("size") or 0.0))
        if stop <= 0 or sz <= 0:
            self._event(EV_ERROR, f"{symbol} 止损丢失且参数不全（stop={stop} sz={sz}），未动作",
                        symbol=symbol)
            self._alert(f"【HL执行】🛑 {coin} 止损单丢失，但本地缺少止损价/数量，**未能自动处理**，"
                        f"请立刻人工查看交易所持仓！")
            return
        try:
            sz_dec = int((self.info.specs([coin]).get(coin) or {}).get("sz_decimals", 5))
        except HlInfoError:
            sz_dec = 5
        px = hl_sizing.round_price(stop, sz_dec)
        try:
            res = self.ex.place_order(coin, side != 1, sz, px, reduce_only=True,
                                      trigger_px=px,
                                      cloid=self._attempt_cloid(symbol, bar_ms, PURPOSE_STOP + ":fix"))
        except Exception as ex:                                     # noqa: BLE001
            res = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        if res.get("ok") and res.get("status") in (STATUS_RESTING, STATUS_FILLED):
            self.db.hl_update_order(symbol, bar_ms, PURPOSE_STOP, status=res["status"],
                                    oid=res.get("oid"), error=None)
            self.db.set_pos_hl(int(row["id"]), hl_stop_oid=res.get("oid"))
            self._event(EV_ORDER, f"{symbol} 止损丢失 → 已自动补挂 @ {px:,.2f}", symbol=symbol)
            self._alert(f"【HL执行】🔧 {coin} 止损单丢失，已自动补挂（原价 {px:,.2f}，"
                        f"新单号 {res.get('oid')}，{sz} {coin}）。自动交易已暂停（kill），"
                        f"核对后可恢复。")
            return
        # 补挂失败 → 无保护持仓不允许存在 → 市价平掉
        self._event(EV_ERROR, f"{symbol} 止损补挂失败（{res.get('error')}）→ 立即平仓",
                    symbol=symbol, data={"place": res})
        try:
            limit_px = hl_sizing.round_price(self.ex.slippage_limit(coin, side != 1), sz_dec)
        except Exception as ex:                                     # noqa: BLE001
            limit_px = float((snap.get("positions") or [{}])[0].get("mark_px") or px)
            logger.warning("[hl-exec] 平仓限价计算失败（用标价）：%s", ex)
        try:
            close = self.ex.place_order(coin, side != 1, sz, limit_px, reduce_only=True)
        except Exception as ex:                                     # noqa: BLE001
            close = {"ok": False, "status": "error", "error": f"{type(ex).__name__}: {ex}"}
        ok_close = bool(close.get("ok")) and close.get("status") == STATUS_FILLED
        if ok_close:
            self._close_local(int(row["id"]), float(close.get("avg_px") or limit_px),
                              "stop_missing", sz)
        self._alert(f"【HL执行】🛑 {coin} 止损单丢失且补挂失败（{res.get('error')}），"
                    f"已{'市价平仓' if ok_close else '尝试平仓但结果异常'}（{close.get('status')}）。"
                    f"**请立刻人工核对交易所**！")

    @staticmethod
    def _has_stop(snap: dict, coin: str) -> bool:
        for o in snap.get("open_orders") or []:
            if o.get("coin") == coin and o.get("reduce_only") and o.get("is_trigger"):
                return True
        return False

    # ------------------------------------------------------------ 闸门
    def _entry_gates(self, symbol: str, plan, snap: dict, equity: Optional[float] = None) -> List[str]:
        p = self.p
        bad: List[str] = []
        if self.db.hl_kill():
            bad.append("kill switch 已触发")
        if self.p.live and not self.ex.available:
            # 只在 live 校验写通道：dry 期不下单、不需要私钥（否则「先跑影子不用给私钥」就不成立）
            bad.append(f"写通道不可用：{self.ex.init_error or '未配置凭据'}")
        if plan.below_min_notional:
            bad.append(f"名义 {plan.notional:.1f}U 低于交易所下限 {p.min_notional:g}U")
        eq = float(equity if equity is not None else p.equity)
        total = float((snap or {}).get("notional") or 0.0)
        if p.max_notional_mult and p.max_notional_mult > 0:          # 0 = 不设限（默认）
            cap = eq * p.max_notional_mult
            if plan.notional > cap:
                bad.append(f"名义 {plan.notional:.0f}U 超过单标的上限 {cap:.0f}U")
        if p.max_total_notional_mult and p.max_total_notional_mult > 0:
            if total + plan.notional > eq * p.max_total_notional_mult:
                bad.append(f"总名义将达 {total + plan.notional:.0f}U，超过上限 "
                           f"{eq * p.max_total_notional_mult:.0f}U")
        if plan.stop_dist > p.stop_max_hard:
            bad.append(f"执行时止损距离 {plan.stop_dist * 100:.2f}% 超过硬上限 {p.stop_max_hard * 100:g}%")
        if plan.leverage > plan.max_safe_leverage:
            bad.append(f"杠杆 {plan.leverage:g}x 超过安全上限 {plan.max_safe_leverage:.1f}x")
        today = _today_start_ts()
        entries_today = len([r for r in self.db.hl_recent_orders(100)
                             if r["purpose"] == PURPOSE_ENTRY and float(r["created_at"]) >= today
                             and r["status"] in (ST_SENT, ST_RESTING, ST_FILLED)])
        if entries_today >= p.max_orders_per_day:
            bad.append(f"当日下单已达 {entries_today} 次（上限 {p.max_orders_per_day}）")
        fails = len([r for r in self.db.hl_recent_orders(50)
                     if r["status"] == ST_FAILED and time.time() - float(r["created_at"]) < 3600])
        if fails >= p.max_fail_streak:
            bad.append(f"近一小时失败 {fails} 次（上限 {p.max_fail_streak}）")
        for coin_pos in (snap or {}).get("positions") or []:
            if coin_pos["coin"] == self.coin(symbol):
                bad.append(f"交易所已有 {self.coin(symbol)} 仓位，拒绝重复开仓")
        return bad

    # ------------------------------------------------------------ 开仓
    def _current_closed_bar_ms(self, symbol: str) -> int:
        """当前最后一根已收盘 4h bar 的开盘时刻（平仓 cloid 用）。

        取 bar_state 里引擎已处理的进度；读不到时按当前时间对齐 4h 网格推导。
        返回 0 表示完全无法确定（调用方回落入场 bar）。
        """
        try:
            last = self.db.get_last_bar(symbol)
            if last:
                return int(last)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            now_ms = int(time.time() * 1000)
            return now_ms - (now_ms % (4 * 3600 * 1000))
        except Exception:                                        # noqa: BLE001
            return 0

    def _attempt_cloid(self, symbol: str, bar_ms: int, purpose: str) -> str:
        """重试类下单的 cloid：在 (symbol, bar, purpose) 之外再掺一个**尝试时刻**。

        HL 的 cloid 账户级唯一（重复 cloid 直接被拒，官方文档亦然）。只按
        (symbol, bar, purpose) 推导时，同一个 4h 内第二次尝试会拿到与第一次**完全
        相同**的 cloid：平仓重试被拒 → 而重试的第一步已经撤掉了止损 = 裸仓
        （审计四轮 P2，复现见 tests/test_audit4_p1.py）。这里掺进当前秒数：
        - 两次真实尝试至少隔一个引擎 tick（30 分钟），秒数必不同 → 永不撞号；
        - 同一秒内的「同一次尝试重放」（崩溃后立刻重跑）会拿到同一个值，
          保留幂等语义（且每次尝试含多次网络往返 + _op_lock 串行，撞秒实际不可达）。
        DB 里记的 cloid 与发给交易所的严格同源（都用这个函数）。
        """
        return make_cloid(symbol, bar_ms, f"{purpose}@{int(time.time())}")

    def _entry_issued(self, row) -> bool:
        """该 pending 行是否**真的向交易所发过**入场单。

        只有「已发出/在场/已成交」三种状态才算真实仓位（成交后崩溃的那种裸仓
        正是 sent/resting/filled）；没发过（intent/skipped/无记录）的 pending 行
        是「信号到了但本轮没能下单」，交易所侧什么都没有，不该当仓位对账。
        读不到记录时按真实仓位处理（fail-closed：宁可多对一次账，不漏保护）。
        """
        try:
            o = self.db.hl_order(str(row["symbol"]), int(row["signal_bar_ms"] or 0),
                                 PURPOSE_ENTRY)
        except Exception:                                        # noqa: BLE001
            return True
        if o is None:
            return False
        return (o["status"] or "") in (ST_SENT, ST_RESTING, ST_FILLED)

    def on_entry(self, position_id: int) -> bool:
        """接管一次开仓。返回 True = 已接管（调用方不要再走人工确认流）。"""
        with self._op_lock:
            return self._on_entry_locked(position_id)

    def _on_entry_locked(self, position_id: int) -> bool:
        row = self.db.position_by_id(position_id)
        if row is None or self.p.mode == MODE_OFF:
            return False
        symbol, side = row["symbol"], int(row["side"] or 1)
        coin = self.coin(symbol)
        bar_ms = int(row["signal_bar_ms"])

        # 已有意图/结果 → 不重复处理（幂等）
        existing = self.db.hl_order(symbol, bar_ms, PURPOSE_ENTRY)
        if existing is not None and existing["status"] not in (ST_SKIPPED,):
            self._event(EV_DECISION, f"{symbol} 本 bar 已有 entry 记录（{existing['status']}），跳过",
                        symbol=symbol, data={"order_id": existing["id"]})
            return True

        snap = self._snapshot(force=True)
        if snap is None:
            self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_ENTRY, side, 0.0,
                                     status=ST_SKIPPED)
            self._event(EV_ERROR, "读不到账户，放弃本次开仓", symbol=symbol)
            if self.p.live:
                # live 下不能回落人工卡：人工卡按配置 equity（1000U）算金额，与
                # 真实账户差一个数量级，照着下单就是风险放大（审计三轮 P1-H5）。
                # 已接管 → 引擎只补推这条告警（有 alert_logs 补推兜底）。
                self._alert(f"【HL执行】⚠️ {coin} 信号到达但读不到账户快照，本轮**未下单**。"
                            f"这笔信号不会自动重试（占用的仓位槽约 8 小时后自动释放）。")
                return True
            return False

        try:
            specs = self.info.specs([coin]).get(coin) or {}
            sz_decimals = int(specs.get("sz_decimals", 0))
        except HlInfoError as e:
            self._event(EV_ERROR, f"读不到合约规格，放弃开仓：{e}", symbol=symbol)
            if self.p.live:
                self._alert(f"【HL执行】⚠️ {coin} 信号到达但读不到合约规格，本轮**未下单**。"
                            f"这笔信号不会自动重试（占用的仓位槽约 8 小时后自动释放）。")
                return True
            return False

        mid = 0.0
        try:
            mid = self.ex.mid(coin)
        except Exception as e:                                       # noqa: BLE001
            self._event(EV_ERROR, f"取不到 {coin} mid，放弃开仓：{e}", symbol=symbol)
            if self.p.live:
                self._alert(f"【HL执行】⚠️ {coin} 信号到达但取不到行情，本轮**未下单**。"
                            f"这笔信号不会自动重试（占用的仓位槽约 8 小时后自动释放）。")
                return True
            return False
        if mid <= 0:
            self._event(EV_ERROR, f"{coin} mid 非法（{mid}），放弃开仓", symbol=symbol)
            if self.p.live:
                self._alert(f"【HL执行】⚠️ {coin} 信号到达但行情非法，本轮**未下单**。"
                            f"这笔信号不会自动重试（占用的仓位槽约 8 小时后自动释放）。")
                return True
            return False

        # 用**当前 mid** 当入场价重算数量（风险归一），止损仍用信号给的止损价
        stop = float(row["stop_price"])
        eff_equity = self.effective_equity(snap)          # 跟随实时权益（带下限）
        plan = hl_sizing.size_order_hl(coin, sz_decimals, mid, stop, eff_equity,
                                      self.p.risk_pct, self.p.leverage, side=side,
                                      min_notional=self.p.min_notional)
        gates = self._entry_gates(symbol, plan, snap, equity=eff_equity)
        if gates:
            self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_ENTRY, side, plan.sz,
                                     px=mid, status=ST_SKIPPED)
            self._event(EV_DECISION, "开仓被闸门拒绝：" + "；".join(gates), symbol=symbol,
                        data={"plan": plan.__dict__})
            if self.p.live:
                self._alert(f"【HL执行】{coin} 信号被闸门拒绝（未下单）：\n- " + "\n- ".join(gates))
            return True

        status = ST_PLANNED if not self.p.live else ST_INTENT
        self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_ENTRY, side, plan.sz,
                                 px=mid, cloid=make_cloid(symbol, bar_ms, PURPOSE_ENTRY),
                                 status=status)
        self.db.hl_log_event(EV_ORDER, f"{symbol} entry 意图：{plan.sz} {coin} @~{mid:,.1f}"
                                       f"（止损 {stop:,.1f}，风险 {plan.risk_u:.1f}U）",
                             symbol=symbol, data={"plan": plan.__dict__, "live": self.p.live},
                             code_rev=self.p.code_rev)
        if not self.p.live:
            self._event(EV_DECISION, "dry 模式：只记录计划不下单（人工提醒照常发出）", symbol=symbol,
                        data={"plan": plan.__dict__})
            # 关键：dry 必须返回 False，让引擎照常发人工操作卡 ——
            # 否则信号被静默吞掉，仓位停在 pending 占住该标的的槽，影子期反而没有可比对的人工动作。
            self._alert("\n".join([
                f"【HL影子】{coin} 若为实盘会开仓 {plan.sz} {coin} @~{mid:,.1f}",
                f"止损 {stop:,.1f}（{plan.stop_dist * 100:.2f}%）｜风险 {plan.risk_u:.1f}U｜"
                f"名义 {plan.notional:,.0f}U｜杠杆 {plan.leverage:g}x",
                "（dry 模式：未下任何单，下面的人工提醒照常）",
            ]))
            return False

        # 1) 逐仓杠杆
        lev = self.ex.update_leverage(coin, int(round(self.p.leverage)), cross=False)
        if not lev["ok"]:
            return self._entry_failed(symbol, bar_ms, f"设置杠杆失败：{lev['error']}")

        # 2) 入场 IOC 限价
        try:
            limit_px = hl_sizing.round_price(self.ex.slippage_limit(coin, side == 1, mid),
                                             sz_decimals)
        except Exception as e:                                       # noqa: BLE001
            return self._entry_failed(symbol, bar_ms, f"计算限价失败：{e}")
        res = self.ex.place_order(coin, side == 1, plan.sz, limit_px,
                                  cloid=self.ex._cloid(symbol, bar_ms, PURPOSE_ENTRY))
        if not res["ok"] or res["status"] not in (STATUS_FILLED, STATUS_RESTING):
            return self._entry_failed(symbol, bar_ms, f"入场失败：{res['error']}")
        if res["status"] == STATUS_RESTING:
            # IOC 不该挂着；挂上了说明限价不够激进 → 撤掉并放弃（不追价）
            self.ex.cancel(coin, int(res["oid"] or 0))
            return self._entry_failed(symbol, bar_ms, "IOC 未成交（限价不够激进），已撤单放弃")
        filled_sz = float(res["filled_sz"] or plan.sz)
        avg_px = float(res["avg_px"] or mid)
        self.db.hl_update_order(symbol, bar_ms, PURPOSE_ENTRY, status=ST_FILLED,
                                oid=res.get("oid"), filled_sz=filled_sz, avg_px=avg_px,
                                raw=str(res.get("raw"))[:2000])

        # 3) 止损单（硬要求）
        stop_res = self.ex.place_order(coin, side != 1, filled_sz,
                                       hl_sizing.round_price(stop, sz_decimals),
                                       reduce_only=True, trigger_px=hl_sizing.round_price(stop, sz_decimals),
                                       cloid=self.ex._cloid(symbol, bar_ms, PURPOSE_STOP))
        self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_STOP, side, filled_sz,
                                 px=stop, reduce_only=True, status=ST_SENT,
                                 cloid=make_cloid(symbol, bar_ms, PURPOSE_STOP))
        if not stop_res["ok"] or stop_res["status"] not in (STATUS_RESTING, STATUS_FILLED):
            self.db.hl_update_order(symbol, bar_ms, PURPOSE_STOP, status=ST_FAILED,
                                    error=str(stop_res.get("error"))[:300])
            # 没有保护的仓位不允许存在 → 立刻平掉。
            # 平仓限价必须按**平仓方向**算滑点（side != 1），不能用上面开仓方向的 limit_px
            try:
                close_px = hl_sizing.round_price(
                    self.ex.slippage_limit(coin, side != 1, mid), sz_decimals)
            except Exception:                                        # noqa: BLE001
                close_px = limit_px
            close = self.ex.place_order(coin, side != 1, filled_sz, close_px, reduce_only=True)
            closed_ok = bool(close.get("ok")) and close.get("status") == STATUS_FILLED
            msg = (f"【HL执行】⚠️ {coin} 止损单挂失败（{stop_res['error']}），"
                   + ("已立即平仓" if closed_ok else f"平仓未确认（{close.get('status')}）——仓位可能仍在交易所")
                   + "。本次未持仓，请人工核对。")
            self._event(EV_ERROR, f"止损挂单失败 → 立即平仓：{stop_res['error']}",
                        symbol=symbol, data={"close": close})
            self.db.set_pos_hl(position_id, hl_status="aborted")
            self._alert(msg)
            return True
        self.db.hl_update_order(symbol, bar_ms, PURPOSE_STOP, status=ST_RESTING,
                                oid=stop_res.get("oid"), raw=str(stop_res.get("raw"))[:2000])

        # 4) 落仓位
        self.db.set_pos_hl(position_id, hl_status="open", hl_entry_oid=res.get("oid"),
                           hl_stop_oid=stop_res.get("oid"), hl_sz=filled_sz)
        self.db.confirm_position(position_id, avg_px, float(filled_sz), stop, True)
        plan2 = hl_sizing.HlOrderPlan(**{**plan.__dict__, "entry_price": avg_px,
                                         "sz": filled_sz, "notional": filled_sz * avg_px})
        self._alert_fill(fmt_entry_alert(
            coin=coin, side=side, sz=filled_sz, entry_px=avg_px, stop=stop,
            stop_dist=plan2.stop_dist, notional=plan2.notional, margin=plan2.margin,
            liq=plan2.liq_price, risk_u=plan2.risk_u, equity=eff_equity,
            signal_px=float(row["entry_price"] or avg_px), oid=res.get("oid"),
            stop_oid=stop_res.get("oid"), when=dt.datetime.now(DISPLAY_TZ), tz_label=TZ_LABEL,
            leverage=float(self.p.leverage or 5.0)))
        self._event(EV_ORDER, f"{symbol} 开仓完成：{filled_sz} @ {avg_px:,.2f}，止损单已挂",
                    symbol=symbol, data={"risk_u": plan2.risk_u, "notional": plan2.notional})
        return True

    def _entry_failed(self, symbol: str, bar_ms: int, msg: str) -> bool:
        coin = self.coin(symbol)                     # 文案用 HL 合约名，DB 键仍用 symbol
        self.db.hl_update_order(symbol, bar_ms, PURPOSE_ENTRY, status=ST_FAILED,
                               error=str(msg)[:300])
        self._event(EV_ERROR, f"开仓失败：{msg}", symbol=symbol)
        if self.p.live:
            self._alert(f"【HL执行】⚠️ {coin} 开仓失败：{msg}")
        return True

    # ------------------------------------------------------------ 离场
    def on_exit(self, position_id: int, reason: str, ref_price: float) -> bool:
        """接管一次离场。返回 True = 已接管（调用方不要再动 DB 状态）。"""
        with self._op_lock:
            return self._on_exit_locked(position_id, reason, ref_price)

    def _on_exit_locked(self, position_id: int, reason: str, ref_price: float) -> bool:
        row = self.db.position_by_id(position_id)
        if row is None or self.p.mode == MODE_OFF:
            return False
        if (row["hl_status"] or "") not in ("open",):
            return False
        symbol, side = row["symbol"], int(row["side"] or 1)
        coin = self.coin(symbol)
        bar_ms = int(row["signal_bar_ms"])
        # 平仓/重挂止损的 cloid 用「当前离场 bar」而不是入场 bar：
        # ① 同一仓位多次触发离场（首次失败后下个 4h 重试）时 cloid 必须变化，
        #    否则被交易所按重复 cloid 拒单，自动离场永久报废；
        # ② 重挂止损与开仓止损（PURPOSE_STOP@入场bar）天然不同源，不再撞号。
        exit_bar_ms = self._current_closed_bar_ms(symbol) or bar_ms
        sz = float(row["hl_sz"] or 0.0)

        snap = self._snapshot(force=True)
        ex_pos = {p["coin"]: p for p in (snap or {}).get("positions") or []}
        # 止损已由交易所触发（stop 事件）→ 用真实成交价关账，不发平仓单
        if reason == "stop" and snap is not None and coin not in ex_pos:
            px, fills = self._last_fill_px(coin)
            self._close_local(position_id, px or ref_price, "stop", sz)
            r = self._r_multiple(row, px or ref_price)
            self._alert(f"【HL执行】🛑 止损离场 · {coin} {'做多' if side == 1 else '做空'}\n"
                        f"止损单已由交易所触发 @ {px or ref_price:,.2f}，本地已关账（R {r:+.2f}）")
            return True
        if reason == "stop" and snap is None:
            # 快照读不到 ≠ 仓位没了。不能按参考价关账 —— 万一仓位还在（且止损单也丢了），
            # 本地关账后就没人管它了。只提醒，留给下一轮/人工。
            self._event(EV_ERROR, f"{symbol} 止损事件但快照不可读，未关账（下一轮再核对）",
                        symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 判定止损离场，但账户快照读取失败，"
                        f"本地暂未关账 —— 请人工核对交易所。")
            return True

        if not self.p.live:
            # 真实仓位在交易所、执行器却不是 live：绝不能替引擎把本地关账（会造成本地/交易所不一致），
            # 交给人工，并且必须喊出来。
            self._event(EV_DECISION, f"dry 模式：不代平真实仓位 {symbol}（{reason}）", symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 触发离场（{reason}）但执行器不是 live 模式，"
                        f"交易所仍有真实仓位 —— 请人工平仓并撤掉止损单")
            return True

        # 1) 先撤掉止损单（否则可能与新平仓单互相打架）。
        #    审计三轮 M1：首次平仓失败后重挂的止损记在 exit:rehang（当前离场 bar）、
        #    对账补挂在 stop:fix —— 只撤 PURPOSE_STOP 会漏掉这两类，留下幽灵触发单。
        #    撤单动作撤的是「交易所侧当前挂着的止损」，所以把 DB 里三种 purpose 的
        #    未撤单记录全查一遍，按 oid 去重逐个撤。
        cancelled_purposes = []
        for purpose in (PURPOSE_STOP, PURPOSE_STOP + ":fix", PURPOSE_EXIT_REHANG):
            so = self.db.hl_order(symbol, bar_ms, purpose)
            if purpose == PURPOSE_EXIT_REHANG:
                # exit:rehang 键在离场 bar 上，同一仓可能有多条（多次离场尝试），
                # hl_order 只按三元键取一条 → 用 hl_recent_orders 扫全部
                for r in self.db.hl_recent_orders(100):
                    if (r["symbol"] == symbol and r["purpose"] == purpose
                            and r["status"] in (ST_RESTING, ST_SENT) and r["oid"]):
                        self.ex.cancel(coin, int(r["oid"]))
                        self.db.hl_update_order(symbol, int(r["bar_ms"]), purpose,
                                                status="cancelled")
                        cancelled_purposes.append(purpose)
                continue
            if so is not None and so["oid"] and so["status"] in (ST_RESTING, ST_SENT, ST_FILLED):
                self.ex.cancel(coin, int(so["oid"]))
                self.db.hl_update_order(symbol, bar_ms, purpose, status="cancelled")
                cancelled_purposes.append(purpose)
        # 2) 平仓（reduce-only IOC）。限价必须按**平仓方向**算滑点
        #    （买入平空 = mid×(1+s)，卖出平多 = mid×(1−s)；用开仓方向的限价会让
        #     卖单拿到比 mid 高的最低可接受价 → 滑点保护完全失效）。
        if sz <= 0:
            sz = abs(float(ex_pos.get(coin, {}).get("size") or 0.0))
        if sz <= 0:
            self._event(EV_ERROR, f"{symbol} 离场时无法确定持仓量，跳过（请人工处理）", symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 离场异常：交易所查不到该仓位，已跳过，请人工核对")
            return True
        try:
            sz_dec = int((self.info.specs([coin]).get(coin) or {}).get("sz_decimals", 5))
        except HlInfoError:
            sz_dec = 5
        try:
            limit_px = hl_sizing.round_price(self.ex.slippage_limit(coin, side != 1), sz_dec)
        except Exception as e:                                       # noqa: BLE001
            limit_px = ref_price
            self._event(EV_ERROR, f"平仓限价计算失败（用参考价）：{e}", symbol=symbol)
        # cloid 按「尝试时刻」区分：同一 4h 内第二次离场尝试不能再复用同一个值
        # （HL 拒重复 cloid，而重试第一步已撤掉止损 → 撞号就是裸仓，见 _attempt_cloid）
        exit_cloid = self._attempt_cloid(symbol, exit_bar_ms, PURPOSE_EXIT)
        self.db.hl_record_intent(symbol, coin, exit_bar_ms, PURPOSE_EXIT, side, sz, px=limit_px,
                                 reduce_only=True, status=ST_SENT,
                                 cloid=exit_cloid)
        res = self.ex.place_order(coin, side != 1, sz, limit_px, reduce_only=True,
                                  cloid=exit_cloid)
        if not res["ok"]:
            self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FAILED,
                                    error=str(res.get("error"))[:300])
            self._event(EV_ERROR, f"平仓失败：{res['error']}", symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 平仓失败：{res['error']}\n"
                        f"（止损单已撤！请立即人工平仓或手动挂回止损）")
            return True
        if res["status"] != STATUS_FILLED:
            # IOC 挂出未成（resting）≠ 已平仓。绝不能按限价关账 —— 交易所仓位还在，
            # 而止损单已被撤掉。处理：撤掉这张挂单、**立即挂回原止损**（保护仓位）、
            # 拉人工提醒；不关本地账，让下一轮 on_exit / 对账继续跟进。
            self.ex.cancel(coin, int(res.get("oid") or 0))
            self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FAILED,
                                    error="平仓 IOC 未成交（已撤单并挂回止损）")
            # 重挂止损用独立 purpose（exit:rehang）：与开仓止损（stop@入场bar）、
            # 事后补挂（stop:fix）都不同源 —— HL 的 cloid 账户级永久唯一，
            # 复用任何一个都会被拒单，重挂失败=仓位裸奔。
            re_stop_px = hl_sizing.round_price(float(row["actual_stop"] or row["stop_price"]), sz_dec)
            rehang_cloid = self._attempt_cloid(symbol, exit_bar_ms, PURPOSE_EXIT_REHANG)
            re_stop = self.ex.place_order(
                coin, side != 1, sz, re_stop_px,
                reduce_only=True, trigger_px=re_stop_px,
                cloid=rehang_cloid)
            re_ok = bool(re_stop.get("ok")) and re_stop.get("status") in (STATUS_RESTING, STATUS_FILLED)
            self.db.hl_record_intent(symbol, coin, exit_bar_ms, PURPOSE_EXIT_REHANG, side, sz,
                                     px=re_stop_px, reduce_only=True,
                                     status=(re_stop.get("status") if re_ok else ST_FAILED),
                                     cloid=rehang_cloid,
                                     error=None if re_ok else str(re_stop.get("error"))[:300])
            if re_ok:
                self._event(EV_ERROR, f"{symbol} 平仓未成交（resting），已撤单并重挂止损"
                                      f"（{re_stop.get('status')} @ {re_stop_px:,.2f}）", symbol=symbol)
                self._alert(f"【HL执行】⚠️ {coin} 平仓单未成交（已撤）。"
                            f"止损单已重新挂回 @ {re_stop_px:,.2f}。请人工核对并处理。")
                return True
            # 重挂失败 → 不允许无保护持仓存在 → 立即市价平掉（与开仓挂止损失败同一口径）
            self._event(EV_ERROR, f"{symbol} 平仓未成交且止损重挂失败"
                                  f"（{re_stop.get('error')}）→ 立即平仓", symbol=symbol)
            try:
                close_px = hl_sizing.round_price(self.ex.slippage_limit(coin, side != 1), sz_dec)
            except Exception:                                        # noqa: BLE001
                close_px = limit_px
            close = self.ex.place_order(coin, side != 1, sz, close_px, reduce_only=True)
            closed_ok = bool(close.get("ok")) and close.get("status") == STATUS_FILLED
            if closed_ok:
                px = float(close.get("avg_px") or close_px)
                self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FILLED,
                                        oid=close.get("oid"), filled_sz=close.get("filled_sz"),
                                        avg_px=px, raw=str(close.get("raw"))[:2000])
                self._close_local(position_id, px, reason, sz)
                r = self._r_multiple(row, px)
                self._alert(f"【HL执行】⚠️ {coin} 平仓未成交、止损重挂失败，已市价平仓"
                            f" @ {px:,.2f}（R {r:+.2f}）。请人工核对。")
            else:
                self._alert(f"【HL执行】🛑 {coin} 平仓未成交且止损重挂失败、市价平仓未确认"
                            f"（{close.get('status')}）——仓位可能仍在交易所且无止损，"
                            f"请立即人工处理！")
            return True
        if res.get("filled_sz") is not None and float(res["filled_sz"] or 0) + 1e-12 < sz:
            # 部分成交（审计三轮 P1-H3）：这轮的第一步已把原止损撤掉了，
            # 剩余量没有保护 —— 必须按剩余量把止损挂回去（exit:rehang 口径，
            # cloid 用当前离场 bar，与全量重挂同一来源），且本地**不**关账，
            # 留 open 让下一轮 on_exit / 对账继续跟进。
            filled = float(res["filled_sz"] or 0)
            remain = sz - filled
            # 审计四轮 P2：这张 IOC 已经结束了（成交一半），必须把 exit 行改成 filled。
            # 旧代码不更新它 → hl_orders 里永久停在 sent，看护把它当「卡住的意图单」
            # 每轮告警、面板对账判 mismatch（复现见 tests/test_audit4_p1.py）。
            # 本地仓**不**关账（剩余量还在交易所，由下面重挂止损 + 下一轮继续离场）。
            try:
                self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FILLED,
                                        oid=res.get("oid"), filled_sz=filled,
                                        avg_px=res.get("avg_px"),
                                        raw=str(res.get("raw"))[:2000])
            except Exception as ex:                                  # noqa: BLE001
                logger.warning("[hl-exec] 部分成交更新 exit 行失败（忽略）：%s", ex)
            self._event(EV_ORDER, f"{symbol} 平仓部分成交 {filled}/{sz}，剩余 {remain} 重挂止损",
                        symbol=symbol, data={"filled": filled, "remain": remain})
            try:
                sz_dec = int((self.info.specs([coin]).get(coin) or {}).get("sz_decimals", 5))
            except HlInfoError:
                sz_dec = 5
            re_px = hl_sizing.round_price(float(row["actual_stop"] or row["stop_price"]), sz_dec)
            rehang_cloid = self._attempt_cloid(symbol, exit_bar_ms, PURPOSE_EXIT_REHANG)
            try:
                re_stop = self.ex.place_order(
                    coin, side != 1, remain, re_px, reduce_only=True, trigger_px=re_px,
                    cloid=rehang_cloid)
            except Exception as ex:                                   # noqa: BLE001
                re_stop = {"ok": False, "error": f"{type(ex).__name__}: {ex}", "status": "error"}
            re_ok = bool(re_stop.get("ok")) and re_stop.get("status") in (STATUS_RESTING, STATUS_FILLED)
            try:
                self.db.hl_record_intent(symbol, coin, exit_bar_ms, PURPOSE_EXIT_REHANG, side,
                                         remain, px=re_px, reduce_only=True,
                                         status=(re_stop.get("status") if re_ok else ST_FAILED),
                                         cloid=rehang_cloid,
                                         error=None if re_ok else str(re_stop.get("error"))[:300])
            except Exception as ex:                                   # noqa: BLE001
                logger.warning("[hl-exec] 部分成交重挂记录失败（忽略）：%s", ex)
            if re_ok:
                self._event(EV_ERROR, f"{symbol} 部分成交后止损已按剩余量 {remain} 重挂",
                            symbol=symbol)
                self._alert(f"【HL执行】⚠️ {coin} 平仓部分成交（{filled}/{sz}），剩余 {remain} "
                            f"{coin} 已重挂止损 @ {re_px:,.2f}，本地保持持仓，下一轮继续离场。")
            else:
                self._event(EV_ERROR, f"{symbol} 部分成交后止损重挂失败（{re_stop.get('error')}）"
                                      f" → 尝试市价平掉剩余量", symbol=symbol)
                try:
                    close_px2 = hl_sizing.round_price(self.ex.slippage_limit(coin, side != 1), sz_dec)
                except Exception:                                    # noqa: BLE001
                    close_px2 = limit_px
                try:
                    close2 = self.ex.place_order(coin, side != 1, remain, close_px2,
                                                 reduce_only=True)
                except Exception as ex:                              # noqa: BLE001
                    close2 = {"ok": False, "status": "error",
                              "error": f"{type(ex).__name__}: {ex}"}
                c2_ok = bool(close2.get("ok")) and close2.get("status") == STATUS_FILLED
                self._alert(f"【HL执行】🛑 {coin} 平仓部分成交且剩余量止损重挂失败，"
                            f"已尝试市价平剩余 {remain}（{'成功' if c2_ok else '结果异常，请立即人工核对'}）。")
                if c2_ok and float(close2.get("filled_sz") or 0) + 1e-12 >= remain:
                    px2 = float(close2.get("avg_px") or close_px2)
                    self._close_local(position_id, px2, reason, sz)
                    r = self._r_multiple(row, px2)
                    self._event(EV_ORDER, f"{symbol} 剩余量已平 @ {px2:,.2f}（{reason}）",
                                symbol=symbol, data={"r": r})
                    return True
            return True    # 本地不关账（或已按剩余平完关账），本轮结束
        px = float(res["avg_px"] or limit_px)
        self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FILLED,
                                oid=res.get("oid"), filled_sz=res.get("filled_sz"), avg_px=px,
                                raw=str(res.get("raw"))[:2000])
        self._close_local(position_id, px, reason, sz)
        r = self._r_multiple(row, px)
        gross = (px - float(row["actual_entry"] or row["entry_price"])) * sz * (1 if side == 1 else -1)
        # 平仓手续费在下一轮 _sync_fills 才会进库 —— 先同步一次再算，否则净利少算平仓费
        try:
            self._sync_fills()
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 平仓前同步成交失败（手续费可能少算）：%s", e)
        fees = self._fees_since(coin, row["opened_at"])
        self._alert_fill(fmt_exit_alert(
            coin=coin, side=side, entry_px=float(row["actual_entry"] or row["entry_price"]),
            exit_px=px, sz=sz, reason=reason, r_multiple=r, gross_pnl=gross, fees=fees,
            opened_at=row["opened_at"], when=dt.datetime.now(DISPLAY_TZ), tz_label=TZ_LABEL))
        self._event(EV_ORDER, f"{symbol} 平仓完成 @ {px:,.2f}（{reason}）", symbol=symbol,
                    data={"r": r})
        return True

    @staticmethod
    def _num(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def effective_equity(self, snap: Optional[dict]) -> float:
        """下单基数。follow：按交易所实时权益（floor 到 10U、带下限）；读不到则回落到配置值。

        为什么要跟随：写在配置里的基数不会随账户盈亏变化 —— 涨了不复利、跌了相对风险悄悄变大。
        为什么要取整：几毛钱的浮亏浮盈不该让仓位跳来跳去（也避免每次下单都换一个数）。
        只设下限（用户拍板）：入金到 1000U 就按 1000 算，不加上限。
        """
        base = float(self.p.equity or 0.0)
        if not self.p.follow or not snap:
            return base
        # 统一账户现货读取失败 → equity_total = 0 + 浮盈，不可信（P1-H1，审计三轮）。
        # 熔断分支早就显式检查 spot_ok（run 里「数据不可信，宁可跳过」），定仓路径
        # 却没用它 → 无仓时 equity_total≤0 回落到配置 equity=1000U，真钱 100U 的账户
        # 会按 20U 风险定仓（10 倍）。这里必须同样 fail-closed：回落 equity_min，
        # 绝不放大风险。
        if not bool(snap.get("spot_ok", True)):
            logger.warning("[hl-exec] 现货读取失败（spot_ok=False），权益不可信，"
                           "回落 equity_min=%s", self.p.equity_min)
            return float(self.p.equity_min or 20.0)
        live = float(snap.get("equity_total", snap.get("equity")) or 0.0)
        if live <= 0:
            logger.warning("[hl-exec] 实时权益读到 %.4f，回落 equity_min=%.2f（不再用配置基数，"
                           "防风险放大）", live, self.p.equity_min)
            return float(self.p.equity_min or 20.0)
        eff = math.floor(live / 10.0) * 10.0
        if self.p.equity_min and eff < self.p.equity_min:
            eff = float(self.p.equity_min)
            if eff > live:
                # equity_min 配置超过真实权益 → 单笔风险被放大（live/eff 倍）。只记事件提醒，
                # 不阻断（用户拍板过「不加上限」，但配错下限值得被看见）。
                self._event(EV_DECISION,
                            f"equity_min={eff:g}U 高于实时权益 {live:.2f}U —— 生效基数被抬高，"
                            f"实际单笔风险约为目标的 {eff / live:.2f} 倍，请核对配置")
        self._note_equity_change(base, eff)
        return eff

    def _note_equity_change(self, prev: float, now: float) -> None:
        """基数发生显著变化（>20%）时留一条可追溯的事件 —— 入金/提现/大额浮亏都看得见。"""
        try:
            last = float(self.db.get_config_str("hl_last_equity_base") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        if last and abs(now - last) / max(last, 1e-9) < 0.2:
            return
        self.db.set_config("hl_last_equity_base", now)
        self._event(EV_DECISION, f"下单基数 {last:g} → {now:g} U（跟随实时权益）",
                    data={"prev": last, "now": now, "config_base": prev})

    def _fees_since(self, coin: str, since_ts) -> float:
        """该仓位开仓以来累计手续费（读本地 hl_fills；读不到就返回 0，不影响主流程）。

        下界用「opened_at − 120s」：入场成交发生在 opened_at 写库**之前**，
        用 >= opened_at 会把入场那笔手续费排除掉（净利被高估）。
        """
        try:
            since = float(since_ts or 0) - 120.0
        except (TypeError, ValueError):
            return 0.0
        if since < 0:
            since = 0.0
        total = 0.0
        for r in self.db.hl_recent_fills(500):
            try:
                if str(r["coin"]) == coin and float(r["ts"] or 0) >= since:
                    total += float(r["fee"] or 0.0)
            except (TypeError, ValueError, KeyError):
                continue
        return total

    def _sync_fills(self, limit: int = 100) -> int:
        """把交易所的成交同步进 hl_fills（按 tid 去重，幂等）。返回新增条数。"""
        if not self.p.account_address:
            return 0
        fills = self.info.user_fills(self.p.account_address) or []
        known = {str(r["tid"]) for r in self.db.hl_recent_fills(500)}
        added = 0
        reverse = {v: k for k, v in (self.p.coin_map or {}).items()}
        for f in fills[:limit]:
            tid = str(f.get("tid") or f.get("hash") or "")
            if not tid or tid in known:
                continue
            coin = str(f.get("coin") or "")
            direction = str(f.get("dir") or "")
            self.db.hl_upsert_fill(
                tid=tid, symbol=reverse.get(coin, f"{coin}_USDT"), coin=coin,
                side=1 if "Long" in direction else -1, sz=self._num(f.get("sz")),
                px=self._num(f.get("px")), fee=self._num(f.get("fee")),
                fee_token=f.get("feeToken"), dir=direction,
                closed_pnl=self._num(f.get("closedPnl")), hash=f.get("hash"),
                reduce_only=("Close" in direction),
                ts=int(f["time"]) // 1000 if f.get("time") else None)
            added += 1
        if added:
            self._event(EV_ORDER, f"同步 {added} 笔交易所成交到本地", data={"added": added})
        return added

    def _r_multiple(self, row, exit_px: float) -> float:
        entry = float(row["actual_entry"] or row["entry_price"])
        stop = float(row["actual_stop"] or row["stop_price"])
        side = int(row["side"] or 1)
        risk = abs(entry - stop)
        if risk <= 0:
            return 0.0
        move = (exit_px - entry) if side == 1 else (entry - exit_px)
        return move / risk

    def _close_local(self, position_id: int, px: float, reason: str, sz: float) -> None:
        self.db.close_position(position_id, px, reason, auto=False)
        self.db.set_pos_hl(position_id, hl_status="closed")

    def _last_fill_px(self, coin: str) -> Tuple[Optional[float], List[dict]]:
        """从交易所成交里取最近一笔该 coin 的**平仓**成交价（用于止损已触发的关账）。

        必须过滤 dir 含 "Close" —— 否则同标的重开后会取到开仓价当平仓价，R 值全错。
        """
        try:
            fills = self.info.user_fills(self.p.account_address) or []
        except HlInfoError:
            return None, []
        mine = [f for f in fills if f.get("coin") == coin]
        if not mine:
            return None, []
        closing = [f for f in mine if "Close" in str(f.get("dir") or "")]
        source = closing or mine          # 没有平仓成交时退回全量（供调用方诊断）
        px = None
        for f in source[:8]:
            try:
                px = float(f.get("px"))
            except (TypeError, ValueError):
                continue
            break
        return px, mine[:8]

    # ------------------------------------------------------------ 每轮
    def run(self, force_snapshot: bool = False) -> List[str]:
        """每轮 tick 调用：快照 → 对账 → 熔断 → 死手开关 → 心跳。返回告警文案。"""
        with self._op_lock:
            return self._run_locked(force_snapshot)

    def _run_locked(self, force_snapshot: bool = False) -> List[str]:
        alerts: List[str] = []
        if self.p.mode == MODE_OFF:
            return alerts
        snap = self._snapshot(force=force_snapshot)
        if snap is None:
            # 读不到快照也要写心跳（带 degraded 标记）：
            # 看守判活只看 heartbeat 事件的时间 —— 整轮 return 的话，
            # 「活着但读不到账户」会被误报成「执行器死了」。
            self._event(EV_HEARTBEAT, "executor 心跳（读不到账户快照，已降级）",
                        data={"kill": self.db.hl_kill(), "mode": self.p.mode, "degraded": True})
            alerts.append("【HL执行】⚠️ 读不到 HL 账户快照（已连续重试），本轮跳过对账/熔断/成交同步。"
                          "若持续出现请检查网络或 HL 状态。")
            return alerts

        # 1) 对账
        rec = self.reconcile(snap)
        if not rec["ok"]:
            problems = rec["problems"]
            self._event(EV_RECONCILE, "对账异常：" + "；".join(problems), data=rec)
            if not rec.get("unknown"):
                self.db.hl_set_kill(True, "对账异常")
                self._event(EV_KILL, "对账异常 → 自动 kill switch", data=rec)
            # 告警去重：kill 态下每轮 run() 都会走到这里（5 分钟一轮），
            # 同一状态只推送一次 + 每 6 小时提醒一次仍在等人工；恢复由下方 else 分支报告。
            key = "hl_recon_alert_at"
            last_alert = float(self.db.get_config_str(key) or 0)
            due = (time.time() - last_alert) >= 6 * 3600
            if due:
                # 审计三轮 M2：节流键必须在**推送成功后**再写 —— 以前先写后发，
                # 中间任一步异常（补挂/死手/同步抛错）会把该轮告警吞掉且 6h 静默。
                # 现在把文案先放进 alerts（末尾统一 _alert，成功才落库记账），
                # 节流键改为在 _alert 成功后由 pending_throttle 回写。
                alerts.append(("hl_recon_alert_at",
                               "【HL执行】⚠️ 对账异常（已暂停自动交易）：\n- " + "\n- ".join(problems)))
            # 确认「有仓但没止损」→ 不允许无保护持仓存在：先补挂，补不上就市价平掉
            for row in rec.get("missing_stop") or []:
                self._repair_missing_stop(row, snap)
        elif rec.get("unconfirmed"):
            # 读不到触发单：只提示、绝不动作（否则会误平仓）；同一状态只喊一次
            items = rec["unconfirmed"]
            self.db.hl_log_event(EV_RECONCILE, "无法确认止损（挂单接口回落）：" + "；".join(items),
                                 code_rev=self.p.code_rev)
            if str(self.db.get_config_str("hl_stop_unconfirmed") or "") != "1":
                self.db.set_config("hl_stop_unconfirmed", "1")
                alerts.append("【HL执行】⚠️ 无法确认止损（挂单接口临时回落）：\n- "
                              + "\n- ".join(items)
                              + "\n未做任何动作。请看一眼面板/交易所；恢复后我会再报一次。")
        else:
            if str(self.db.get_config_str("hl_stop_unconfirmed") or "") == "1":
                self.db.set_config("hl_stop_unconfirmed", "0")
                alerts.append("【HL执行】✅ 挂单接口已恢复，止损状态可正常确认")
            # 对账恢复正常：清掉告警节流标记（下次异常立即推送），并告知一次
            if float(self.db.get_config_str("hl_recon_alert_at") or 0) > 0:
                self.db.set_config("hl_recon_alert_at", "0")
                if not rec.get("unconfirmed"):
                    alerts.append("【HL执行】✅ 对账已恢复正常")
            self.db.hl_log_event(EV_RECONCILE, "对账一致", code_rev=self.p.code_rev)

        # 2) 当日亏损熔断（基线 = 当日第一张快照；当前值用刚读到的实时权益）
        day0 = _today_start_ts()
        base = self.db.hl_snapshots(since_ts=day0, limit=1)
        spot_ok = bool(snap.get("spot_ok", True)) if snap else False
        if base and not self.db.hl_kill():
            # 统一账户下 snap["equity"]（永续口径）恒为 0 → 必须用账户总值，否则熔断永不触发
            start_eq = float(base[0]["equity"] or 0.0)
            now_eq = float(snap.get("equity_total", snap.get("equity")) or 0.0)
            # 现货读取失败时 equity_total 只剩浮盈 —— 数据不可信，宁可跳过也不误熔断
            if not spot_ok:
                logger.warning("[hl-exec] 现货余额读取失败，本轮跳过亏损熔断判断（数据不可信）")
            elif start_eq > 0 and (now_eq - start_eq) / start_eq <= -self.p.max_daily_loss_pct:
                self.db.hl_set_kill(True, "当日亏损熔断")
                self._event(EV_KILL, f"当日亏损 {((now_eq - start_eq) / start_eq) * 100:.1f}% → kill",
                            data={"start_equity": start_eq, "equity": now_eq})
                alerts.append(f"【HL执行】⚠️ 当日亏损 {(now_eq - start_eq) / start_eq * 100:.1f}%"
                              f"（阈值 -{self.p.max_daily_loss_pct * 100:g}%）→ 已停止自动交易")

        # 3) 死手开关（HL 原生 scheduleCancel）：**默认关闭**，见 ExecParams.use_schedule_cancel。
        #    关闭时这一步完全不碰交易所；即使在开启状态下调用失败/抛异常，也绝不外泄 ——
        #    后面的成交同步、心跳、看守判活等主流程一步都不能被它拖累。
        if self.p.live and not self.db.hl_kill() and getattr(self.p, "use_schedule_cancel", False):
            try:
                r = self.ex.schedule_cancel(minutes=self.p.deadman_minutes)
            except Exception as ex:                                   # noqa: BLE001
                r = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
            flagged = str(self.db.get_config_str("hl_deadman_err") or "") == "1"
            try:
                if r["ok"]:
                    if flagged:
                        self.db.set_config("hl_deadman_err", "0")
                        self._event(EV_ERROR, "死手开关恢复可用")
                elif not flagged:
                    self.db.set_config("hl_deadman_err", "1")
                    self._event(EV_ERROR,
                                f"死手开关设置失败（仅记一次，恢复时再报）：{r['error']}",
                                data={"error": str(r["error"])[:300]})
            except Exception as ex:                                   # noqa: BLE001
                logger.warning("[hl-exec] 死手开关记账失败（忽略）：%s", ex)

        # 4) 同步成交到本地（面板的「最近成交/手续费累计/今日与累计已实现」全靠 hl_fills；
        #    在此之前**没有任何生产代码写过这张表** → 真交易后面板这三项会一直是 0）
        try:
            self._sync_fills()
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 同步成交失败（不影响交易）：%s", e)

        # 5) 心跳（看守据此判断执行器存活）
        self._event(EV_HEARTBEAT,
                    f"executor 心跳（equity {snap.get('equity_total', snap.get('equity'))}）",
                    data={"kill": self.db.hl_kill(), "mode": self.p.mode})
        for a in alerts:
            if isinstance(a, tuple):
                key, text = a
                if self._alert(text):
                    # 推送成功才写节流键（失败则下轮再推，见 M2）
                    try:
                        self.db.set_config(key, str(int(time.time())))
                    except Exception as ex:                           # noqa: BLE001
                        logger.warning("[hl-exec] 节流键写入失败（忽略）：%s", ex)
            else:
                self._alert(a)
        return [a[1] if isinstance(a, tuple) else a for a in alerts]
