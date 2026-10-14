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
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.hl import (MODE_DRY, MODE_LIVE, MODE_OFF, PURPOSE_ENTRY, PURPOSE_EXIT, PURPOSE_STOP,
                    ST_FAILED, ST_FILLED, ST_INTENT, ST_PLANNED, ST_RESTING, ST_SENT, ST_SKIPPED,
                    EV_DECISION, EV_ERROR, EV_HEARTBEAT, EV_KILL, EV_ORDER, EV_RECONCILE)
from app.hl import sizing as hl_sizing
from app.hl.exchange import STATUS_DRY, STATUS_FILLED, STATUS_RESTING, make_cloid
from app.hl.info import HlInfoError

logger = logging.getLogger(__name__)

DEFAULT_COIN_MAP = {"BTC_USDT": "BTC", "ETH_USDT": "ETH"}


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
    max_slippage_pct: float = 0.003
    max_notional_mult: float = 1.5        # 单标的名义 / 权益
    max_total_notional_mult: float = 2.5  # 总名义 / 权益
    max_orders_per_day: int = 4
    max_fail_streak: int = 3
    max_daily_loss_pct: float = 0.05
    deadman_minutes: int = 30
    min_notional: float = 10.0
    snapshot_min_interval_s: float = 240.0
    stop_max_hard: float = 0.10           # 执行时止损距离超此值直接放弃
    code_rev: str = ""

    @property
    def live(self) -> bool:
        return self.mode == MODE_LIVE


def _today_start_ts(now: Optional[float] = None) -> float:
    d = dt.datetime.now(dt.timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Executor:
    def __init__(self, db, params: ExecParams, info, exchange, notifier=None):
        self.db = db
        self.p = params
        self.info = info
        self.ex = exchange
        self.notifier = notifier

    # ------------------------------------------------------------ 工具
    def coin(self, symbol: str) -> str:
        return self.p.coin_map.get(symbol, symbol.split("_")[0])

    def _alert(self, text: str) -> None:
        try:
            if self.notifier is not None:
                self.notifier.send_message(text)
        except Exception as e:                                       # noqa: BLE001
            logger.error("[hl-exec] 推送失败：%s", e)

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
        problems: List[str] = []
        local_open = [r for r in self.db.recent_positions(50)
                      if r["status"] == "open" and (r["hl_status"] or "") in ("open", "mismatch")]
        seen = set()
        for r in local_open:
            coin = self.coin(r["symbol"])
            seen.add(coin)
            if coin not in ex_pos:
                problems.append(f"{r['symbol']} 本地有仓（#{r['id']}）但交易所无仓位")
                self.db.set_pos_hl(r["id"], hl_status="mismatch")
                continue
            if not self._has_stop(snap, coin):
                problems.append(f"{r['symbol']} 有仓但**没有止损挂单**")
        for coin in ex_pos:
            if coin not in seen:
                problems.append(f"交易所有 {coin} 仓位但本地无记录（人工单？）")
        return {"ok": not problems, "problems": problems, "unknown": False}

    @staticmethod
    def _has_stop(snap: dict, coin: str) -> bool:
        for o in snap.get("open_orders") or []:
            if o.get("coin") == coin and o.get("reduce_only") and o.get("is_trigger"):
                return True
        return False

    # ------------------------------------------------------------ 闸门
    def _entry_gates(self, symbol: str, plan, snap: dict) -> List[str]:
        p = self.p
        bad: List[str] = []
        if self.db.hl_kill():
            bad.append("kill switch 已触发")
        if self.p.live and not self.ex.available:
            # 只在 live 校验写通道：dry 期不下单、不需要私钥（否则「先跑影子不用给私钥」就不成立）
            bad.append(f"写通道不可用：{self.ex.init_error or '未配置凭据'}")
        if plan.below_min_notional:
            bad.append(f"名义 {plan.notional:.1f}U 低于交易所下限 {p.min_notional:g}U")
        cap = p.equity * p.max_notional_mult
        if plan.notional > cap:
            bad.append(f"名义 {plan.notional:.0f}U 超过单标的上限 {cap:.0f}U")
        total = float((snap or {}).get("notional") or 0.0)
        if total + plan.notional > p.equity * p.max_total_notional_mult:
            bad.append(f"总名义将达 {total + plan.notional:.0f}U，超过上限 "
                       f"{p.equity * p.max_total_notional_mult:.0f}U")
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
    def on_entry(self, position_id: int) -> bool:
        """接管一次开仓。返回 True = 已接管（调用方不要再走人工确认流）。"""
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
            return False

        try:
            specs = self.info.specs([coin]).get(coin) or {}
            sz_decimals = int(specs.get("sz_decimals", 0))
        except HlInfoError as e:
            self._event(EV_ERROR, f"读不到合约规格，放弃开仓：{e}", symbol=symbol)
            return False

        mid = 0.0
        try:
            mid = self.ex.mid(coin)
        except Exception as e:                                       # noqa: BLE001
            self._event(EV_ERROR, f"取不到 {coin} mid，放弃开仓：{e}", symbol=symbol)
            return False
        if mid <= 0:
            self._event(EV_ERROR, f"{coin} mid 非法（{mid}），放弃开仓", symbol=symbol)
            return False

        # 用**当前 mid** 当入场价重算数量（风险归一），止损仍用信号给的止损价
        stop = float(row["stop_price"])
        plan = hl_sizing.size_order_hl(coin, sz_decimals, mid, stop, self.p.equity,
                                      self.p.risk_pct, self.p.leverage, side=side,
                                      min_notional=self.p.min_notional)
        gates = self._entry_gates(symbol, plan, snap)
        if gates:
            self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_ENTRY, side, plan.sz,
                                     px=mid, status=ST_SKIPPED)
            self._event(EV_DECISION, "开仓被闸门拒绝：" + "；".join(gates), symbol=symbol,
                        data={"plan": plan.__dict__})
            if self.p.live:
                self._alert(f"【HL执行】{symbol} 信号被闸门拒绝（未下单）：\n- " + "\n- ".join(gates))
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
                f"【HL影子】{symbol} 若为实盘会开仓 {plan.sz} {coin} @~{mid:,.1f}",
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
            # 没有保护的仓位不允许存在 → 立刻平掉
            close = self.ex.place_order(coin, side != 1, filled_sz, limit_px, reduce_only=True)
            msg = (f"【HL执行】⚠️ {symbol} 止损单挂失败（{stop_res['error']}），已立即平仓"
                   f"（平仓结果：{close['status']}）。本次未持仓，请人工核对。")
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
        self.db.confirm_position(position_id, avg_px, int(round(filled_sz)), stop, True)
        plan2 = hl_sizing.HlOrderPlan(**{**plan.__dict__, "entry_price": avg_px,
                                         "sz": filled_sz, "notional": filled_sz * avg_px})
        drift = (avg_px - mid) / mid if mid else 0.0
        self._alert("\n".join([
            f"【HL执行】{symbol} 已开仓 {filled_sz} {coin} @ {avg_px:,.2f}",
            f"止损触发价 {stop:,.2f}（{plan2.stop_dist * 100:.2f}%）｜风险 {plan2.risk_u:.1f}U"
            f"（权益 {self.p.equity:,.0f}U 的 {plan2.risk_u / self.p.equity * 100:.2f}%）",
            f"名义 {plan2.notional:,.0f}U｜保证金 ≈{plan2.margin:,.0f}U｜强平价 {plan2.liq_price:,.0f}",
            f"滑点 {drift * 100:+.2f}%（信号价 {row['entry_price']:,.2f} → 成交 {avg_px:,.2f}）｜cloid 已固化",
        ]))
        self._event(EV_ORDER, f"{symbol} 开仓完成：{filled_sz} @ {avg_px:,.2f}，止损单已挂",
                    symbol=symbol, data={"risk_u": plan2.risk_u, "notional": plan2.notional})
        return True

    def _entry_failed(self, symbol: str, bar_ms: int, msg: str) -> bool:
        self.db.hl_update_order(symbol, bar_ms, PURPOSE_ENTRY, status=ST_FAILED,
                               error=str(msg)[:300])
        self._event(EV_ERROR, f"开仓失败：{msg}", symbol=symbol)
        if self.p.live:
            self._alert(f"【HL执行】⚠️ {symbol} 开仓失败：{msg}")
        return True

    # ------------------------------------------------------------ 离场
    def on_exit(self, position_id: int, reason: str, ref_price: float) -> bool:
        """接管一次离场。返回 True = 已接管（调用方不要再动 DB 状态）。"""
        row = self.db.position_by_id(position_id)
        if row is None or self.p.mode == MODE_OFF:
            return False
        if (row["hl_status"] or "") not in ("open",):
            return False
        symbol, side = row["symbol"], int(row["side"] or 1)
        coin = self.coin(symbol)
        bar_ms = int(row["signal_bar_ms"])
        sz = float(row["hl_sz"] or 0.0)

        snap = self._snapshot(force=True)
        ex_pos = {p["coin"]: p for p in (snap or {}).get("positions") or []}
        # 止损已由交易所触发（stop 事件）→ 用真实成交价关账，不发平仓单
        if reason == "stop" and coin not in ex_pos:
            px, fills = self._last_fill_px(coin)
            self._close_local(position_id, px or ref_price, "stop", sz)
            self._alert(f"【HL执行】{symbol} 止损已由交易所触发 @ {px or ref_price:,.2f}，本地已关账")
            return True

        if not self.p.live:
            # 真实仓位在交易所、执行器却不是 live：绝不能替引擎把本地关账（会造成本地/交易所不一致），
            # 交给人工，并且必须喊出来。
            self._event(EV_DECISION, f"dry 模式：不代平真实仓位 {symbol}（{reason}）", symbol=symbol)
            self._alert(f"【HL执行】⚠️ {symbol} 触发离场（{reason}）但执行器不是 live 模式，"
                        f"交易所仍有真实仓位 —— 请人工平仓并撤掉止损单")
            return True

        # 1) 先撤掉止损单（否则可能与新平仓单互相打架）
        so = self.db.hl_order(symbol, bar_ms, PURPOSE_STOP)
        if so is not None and so["oid"]:
            self.ex.cancel(coin, int(so["oid"]))
            self.db.hl_update_order(symbol, bar_ms, PURPOSE_STOP, status="cancelled")
        # 2) 平仓（reduce-only IOC）
        if sz <= 0:
            sz = abs(float(ex_pos.get(coin, {}).get("size") or 0.0))
        if sz <= 0:
            self._event(EV_ERROR, f"{symbol} 离场时无法确定持仓量，跳过（请人工处理）", symbol=symbol)
            self._alert(f"【HL执行】⚠️ {symbol} 离场异常：交易所查不到该仓位，已跳过，请人工核对")
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
        self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_EXIT, side, sz, px=limit_px,
                                 reduce_only=True, status=ST_SENT,
                                 cloid=make_cloid(symbol, bar_ms, PURPOSE_EXIT))
        res = self.ex.place_order(coin, side != 1, sz, limit_px, reduce_only=True,
                                  cloid=self.ex._cloid(symbol, bar_ms, PURPOSE_EXIT))
        if not res["ok"]:
            self.db.hl_update_order(symbol, bar_ms, PURPOSE_EXIT, status=ST_FAILED,
                                    error=str(res.get("error"))[:300])
            self._event(EV_ERROR, f"平仓失败：{res['error']}", symbol=symbol)
            self._alert(f"【HL执行】⚠️ {symbol} 平仓失败：{res['error']}\n"
                        f"（止损单已撤！请立即人工平仓或手动挂回止损）")
            return True
        px = float(res["avg_px"] or limit_px)
        self.db.hl_update_order(symbol, bar_ms, PURPOSE_EXIT, status=ST_FILLED,
                                oid=res.get("oid"), filled_sz=res.get("filled_sz"), avg_px=px,
                                raw=str(res.get("raw"))[:2000])
        self._close_local(position_id, px, reason, sz)
        r = self._r_multiple(row, px)
        self._alert(f"【HL执行】{symbol} 已平仓 @ {px:,.2f}（{reason}）｜本次 {r:+.2f}R")
        self._event(EV_ORDER, f"{symbol} 平仓完成 @ {px:,.2f}（{reason}）", symbol=symbol,
                    data={"r": r})
        return True

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
        """从交易所成交里取最近一笔该 coin 的平仓价（用于止损已触发的关账）。"""
        try:
            fills = self.info.user_fills(self.p.account_address) or []
        except HlInfoError:
            return None, []
        mine = [f for f in fills if f.get("coin") == coin]
        if not mine:
            return None, []
        px = None
        for f in mine[:8]:
            try:
                px = float(f.get("px"))
            except (TypeError, ValueError):
                continue
            break
        return px, mine[:8]

    # ------------------------------------------------------------ 每轮
    def run(self, force_snapshot: bool = False) -> List[str]:
        """每轮 tick 调用：快照 → 对账 → 熔断 → 死手开关 → 心跳。返回告警文案。"""
        alerts: List[str] = []
        if self.p.mode == MODE_OFF:
            return alerts
        snap = self._snapshot(force=force_snapshot)
        if snap is None:
            return alerts

        # 1) 对账
        rec = self.reconcile(snap)
        if not rec["ok"]:
            problems = rec["problems"]
            self._event(EV_RECONCILE, "对账异常：" + "；".join(problems), data=rec)
            if not rec.get("unknown"):
                self.db.hl_set_kill(True, "对账异常")
                self._event(EV_KILL, "对账异常 → 自动 kill switch", data=rec)
            alerts.append("【HL执行】⚠️ 对账异常（已暂停自动交易）：\n- " + "\n- ".join(problems))
        else:
            self.db.hl_log_event(EV_RECONCILE, "对账一致", code_rev=self.p.code_rev)

        # 2) 当日亏损熔断（基线 = 当日第一张快照；当前值用刚读到的实时权益）
        day0 = _today_start_ts()
        base = self.db.hl_snapshots(since_ts=day0, limit=1)
        if base and not self.db.hl_kill():
            start_eq = float(base[0]["equity"] or 0.0)
            now_eq = float(snap.get("equity") or 0.0)
            if start_eq > 0 and (now_eq - start_eq) / start_eq <= -self.p.max_daily_loss_pct:
                self.db.hl_set_kill(True, "当日亏损熔断")
                self._event(EV_KILL, f"当日亏损 {((now_eq - start_eq) / start_eq) * 100:.1f}% → kill",
                            data={"start_equity": start_eq, "equity": now_eq})
                alerts.append(f"【HL执行】⚠️ 当日亏损 {(now_eq - start_eq) / start_eq * 100:.1f}%"
                              f"（阈值 -{self.p.max_daily_loss_pct * 100:g}%）→ 已停止自动交易")

        # 3) 死手开关
        if self.p.live and not self.db.hl_kill():
            r = self.ex.schedule_cancel(minutes=self.p.deadman_minutes)
            if not r["ok"]:
                self._event(EV_ERROR, f"死手开关设置失败：{r['error']}")

        # 4) 心跳（看守据此判断执行器存活）
        self._event(EV_HEARTBEAT, f"executor 心跳（equity {snap.get('equity')}）",
                    data={"kill": self.db.hl_kill(), "mode": self.p.mode})
        for a in alerts:
            self._alert(a)
        return alerts
