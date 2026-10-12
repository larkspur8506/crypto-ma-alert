"""引擎：状态机 + 提醒文案 + 定时任务。

`walk()` 是**纯函数**（不碰 DB / 网络），线上 tick 与离线 dry-run 共用它，
保证回放结果与线上行为一致。

状态机（每标的独立）：
    IDLE ──信号──▶ PENDING（已推入场票，待确认）──确认──▶ OPEN ──离场/止损──▶ CLOSED
                       └──忽略──▶ IGNORED（离场判定仍对该虚拟仓位执行，避免漏掉已开的仓）

只有**最近一根已收盘 bar** 的信号才是可交易的入场票；更早的信号发「错过」提示，
不发可下单卡片（入场价已变，风险几何已失效）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app import market, strategy
from app.sizing import size_order

logger = logging.getLogger(__name__)
INTERVAL_MS = market.INTERVAL_MS


def fmt_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%m-%d %H:%M")


def fmt_money(x: float) -> str:
    return f"{x:,.0f}"


# ---------------------------------------------------------------- 数据结构

@dataclass
class PositionState:
    symbol: str
    entry_price: float
    stop_price: float
    contracts: int
    signal_bar_ms: int
    position_id: int = 0
    status: str = "pending"
    actual_entry: Optional[float] = None
    actual_stop: Optional[float] = None
    side: int = 1

    @property
    def eff_entry(self) -> float:
        return float(self.actual_entry or self.entry_price)

    @property
    def eff_stop(self) -> float:
        return float(self.actual_stop or self.stop_price)

    @property
    def action_label(self) -> str:
        return "多单" if self.side == 1 else "空单"

    @property
    def close_side(self) -> str:
        return "卖出平仓" if self.side == 1 else "买入平仓"


@dataclass
class Event:
    kind: str                       # entry | entry_missed | exit
    symbol: str
    bar_ms: int
    is_latest: bool
    sig: Optional[strategy.Signal] = None
    order: Optional[object] = None
    exit_price: float = 0.0
    reason: str = ""
    bars_held: int = 0


@dataclass
class Alert:
    kind: str
    symbol: Optional[str]
    bar_ms: Optional[int]
    text: str


# ---------------------------------------------------------------- 签名链接

def sign_token(secret: str, position_id: int, action: str) -> str:
    msg = f"{position_id}:{action}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verify_token(secret: str, position_id: int, action: str, token: str) -> bool:
    return hmac.compare_digest(sign_token(secret, position_id, action), token or "")


def confirm_url(cfg, position_id: int, action: str = "view") -> str:
    base = (cfg.base_url or "").rstrip("/")
    return f"{base}/c/{position_id}?a={action}&t={sign_token(cfg.token_secret, position_id, action)}"


# ---------------------------------------------------------------- 文案

def format_entry(cfg, sig: strategy.Signal, order, position_id: int, stale: bool = False) -> str:
    if stale:
        return format_missed(cfg, sig)
    long = sig.is_long
    stop_note = f"距离 {sig.stop_dist * 100:.2f}%" + ("" if long else "，在价格上方")
    return "\n".join([
        f"{cfg.alert_prefix}{sig.symbol} {sig.action}信号（4h 收盘 {fmt_ms(sig.bar_open_ms)} UTC）",
        f"入场：市价（当前 {fmt_money(sig.entry_price)}）",
        f"止损触发价：{fmt_money(sig.stop_price)}（{stop_note}）",
        f"下单：逐仓 {cfg.leverage:.0f}x / 市价 / 单位选 USDT / 输入 {fmt_money(order.notional)} USDT"
        f" → 点【{sig.open_button}】",
        f"      （核对持仓价值 ≈ {fmt_money(order.notional)}U，约 {order.contracts} 张）",
        f"止损单：计划委托，触发价 {fmt_money(sig.stop_price)}，{sig.close_side}，触发参考=最新成交价",
        f"止盈：不设",
        f"风险：{order.risk_u:.1f}U（权益 {fmt_money(cfg.equity)}U × {cfg.risk_pct * 100:.0f}%），"
        f"保证金({cfg.leverage:.0f}x) {fmt_money(order.margin)}U",
        f"离场：收到本系统提醒后 4 小时内市价平仓",
        f"确认开仓：{confirm_url(cfg, position_id)}",
    ])


def format_missed(cfg, sig: strategy.Signal) -> str:
    return "\n".join([
        f"{cfg.alert_prefix}⚠️ 错过的信号（补报）",
        f"{sig.symbol} 在 {fmt_ms(sig.bar_open_ms)} UTC 的 4h 收盘触发{sig.action}"
        f"（当时入场 {fmt_money(sig.entry_price)}，止损 {fmt_money(sig.stop_price)}）",
        f"该信号已超过一根 4h，入场价已变、风险几何失效，不建议追。仅作记录。",
    ])


def format_exit(cfg, pos: PositionState, exit_price: float, reason: str, bar_ms: int,
                position_id: int, stale: bool = False, bars_held: int = 0) -> str:
    entry, stop = pos.eff_entry, pos.eff_stop
    risk = abs(entry - stop)
    r = (exit_price - entry) / risk if pos.side == 1 else (entry - exit_price) / risk
    r = r if risk > 0 else 0.0
    ma_note = "4h 收盘跌破 SMA20" if pos.side == 1 else "4h 收盘站上 SMA20"
    why = {"stop": "触发止损单", "ma20": ma_note, "manual": "手动平仓"}.get(reason, reason)
    head = "⚠️ 补报·若还没平仓请立即市价平仓" if stale else "离场提醒"
    return "\n".join([
        f"{cfg.alert_prefix}{pos.symbol} {pos.action_label} {head}（{fmt_ms(bar_ms)} UTC 收盘）",
        f"原因：{why}",
        f"参考离场价：{fmt_money(exit_price)}（本次约 {r:+.2f}R，持仓约 {bars_held * 4} 小时）",
        f"建议：4 小时内市价平仓（{pos.close_side}）；平仓后确认委托列表已空（撤掉残留止损单）",
        f"确认平仓：{confirm_url(cfg, position_id, 'close')}",
    ])


def format_data_error(cfg, symbol: str, err: str) -> str:
    return "\n".join([
        f"{cfg.alert_prefix}⚠️ 数据源异常",
        f"{symbol}: {err}",
        f"本次检查未产生任何信号。请检查网络或接口是否变更。",
    ])


# ---------------------------------------------------------------- 纯状态机

def walk(cfg, symbol: str, bars: Sequence[strategy.Bar],
         ind: Dict[str, List[Optional[float]]], squeeze: Sequence[bool],
         position: Optional[PositionState], from_i: int, to_i: int, latest_closed_i: int
         ) -> Tuple[List[Event], Optional[PositionState]]:
    """走 from_i..to_i（含），返回 (事件列表, 结束时的仓位)。不做任何 I/O。"""
    events: List[Event] = []
    for i in range(max(from_i, 1), min(to_i, len(bars) - 2) + 1):
        is_latest = (i == latest_closed_i)
        if position is not None:
            bar = bars[i]
            hit_stop = (bar.low <= position.eff_stop) if position.side == 1 \
                else (bar.high >= position.eff_stop)
            # 入场当根（信号 bar 的下一根）只允许止损离场，MA20 离场从再下一根起
            # —— 与回测引擎的 `u > ei` 守卫一致
            is_entry_bar = (bar.open_ms - position.signal_bar_ms) == INTERVAL_MS
            hit_ma20 = (not is_entry_bar) and strategy.exit_triggered(bars, ind, i, position.side)
            if hit_stop or hit_ma20:
                events.append(Event(kind="exit", symbol=symbol, bar_ms=bar.open_ms, is_latest=is_latest,
                                    exit_price=(position.eff_stop if hit_stop else bar.close),
                                    reason=("stop" if hit_stop else "ma20"),
                                    bars_held=int((bar.open_ms - position.signal_bar_ms) / INTERVAL_MS)))
                position = None
        if position is None:
            sig = strategy.evaluate_entry(symbol, bars, ind, squeeze, i,
                                          entry_price=bars[i + 1].open,
                                          stop_min=cfg.stop_min, stop_max=cfg.stop_max,
                                          allow_long=cfg.enable_long,
                                          allow_short=cfg.enable_shorts,
                                          short_regime_ma=cfg.short_regime_ma)
            if sig is not None:
                order = size_order(symbol, sig.entry_price, sig.stop_price, cfg.equity,
                                   cfg.risk_pct, cfg.leverage, side=sig.side)
                events.append(Event(kind=("entry" if is_latest else "entry_missed"), symbol=symbol,
                                    bar_ms=sig.bar_open_ms, is_latest=is_latest, sig=sig, order=order))
                if is_latest:
                    position = PositionState(symbol=symbol, entry_price=sig.entry_price,
                                             stop_price=sig.stop_price, contracts=order.contracts,
                                             signal_bar_ms=sig.bar_open_ms, side=sig.side)
    return events, position


# ---------------------------------------------------------------- tick（线上）

def _row_to_state(row) -> PositionState:
    return PositionState(symbol=row["symbol"], entry_price=row["entry_price"],
                         stop_price=row["stop_price"], contracts=row["contracts"],
                         signal_bar_ms=row["signal_bar_ms"], position_id=row["id"],
                         status=row["status"], actual_entry=row["actual_entry"],
                         actual_stop=row["actual_stop"], side=row["side"] or 1)


def _index_of_bar(bars: Sequence[strategy.Bar], bar_ms: int) -> Optional[int]:
    for k in range(len(bars) - 1, -1, -1):
        if bars[k].open_ms == bar_ms:
            return k
        if bars[k].open_ms < bar_ms:
            return None
    return None


def tick(db, cfg, notifier, now_ms: Optional[int] = None) -> List[str]:
    """一次检查：取数 → 校验 → 判离场/入场 → 推送 → 落库。返回已发送的文案。"""
    sent: List[str] = []
    for symbol in cfg.symbols:
        try:
            feed = market.fetch(symbol, now_ms)
        except market.FeedError as e:
            db.mark_feed(symbol, "", False, str(e))
            _emit(db, notifier, cfg, Alert("data_error", symbol, None,
                                           format_data_error(cfg, symbol, str(e))), sent)
            continue
        db.mark_feed(symbol, feed.source, True)
        bars = feed.bars
        ind = strategy.compute_indicators(bars)
        sq = strategy.squeeze_flags(ind["band"], cfg.band_window, cfg.band_quantile,
                                    cfg.min_squeeze_bars)
        latest_closed_i = len(bars) - 2

        last_bar = db.get_last_bar(symbol)
        if last_bar is None:
            from_i = latest_closed_i
        else:
            idx = _index_of_bar(bars, last_bar)
            from_i = (idx + 1) if idx is not None else latest_closed_i
        if from_i > latest_closed_i:
            db.set_last_bar(symbol, bars[latest_closed_i].open_ms)
            continue

        row = db.open_position(symbol)
        position = _row_to_state(row) if row else None
        events, new_pos = walk(cfg, symbol, bars, ind, sq, position, from_i, latest_closed_i,
                               latest_closed_i)

        for ev in events:
            if ev.kind == "exit":
                if row is not None:
                    db.close_position(row["id"], ev.exit_price, ev.reason)
                    text = format_exit(cfg, position or _row_to_state(row), ev.exit_price, ev.reason,
                                       ev.bar_ms, row["id"], stale=not ev.is_latest,
                                       bars_held=ev.bars_held)
                else:
                    text = f"{cfg.alert_prefix}{ev.symbol} 离场信号（无对应持仓记录）"
                _emit(db, notifier, cfg, Alert("exit", symbol, ev.bar_ms, text), sent)
                row = None
                position = None
            elif ev.kind == "entry":
                pid = db.create_position(symbol=symbol, signal_bar_ms=ev.bar_ms, side=ev.sig.side,
                                         entry_price=ev.sig.entry_price, stop_price=ev.sig.stop_price,
                                         stop_dist=ev.sig.stop_dist, contracts=ev.order.contracts,
                                         coin_qty=ev.order.coin_qty, notional=ev.order.notional,
                                         risk_u=ev.order.risk_u, equity_at_entry=cfg.equity)
                text = format_entry(cfg, ev.sig, ev.order, pid)
                _emit(db, notifier, cfg, Alert("entry", symbol, ev.bar_ms, text), sent)
                position = new_pos
            else:                                     # entry_missed
                _emit(db, notifier, cfg, Alert("entry_missed", symbol, ev.bar_ms,
                                               format_missed(cfg, ev.sig)), sent)
        db.set_last_bar(symbol, bars[latest_closed_i].open_ms)
    return sent


def _emit(db, notifier, cfg, alert: Alert, sent: List[str]) -> None:
    """幂等 + 成功后标记；dry_run 只打印不发送。"""
    if db.was_alerted(alert.symbol, alert.bar_ms, alert.kind):
        logger.info("[dedup] 已推送过，跳过 %s %s %s", alert.symbol, alert.bar_ms, alert.kind)
        return
    if getattr(cfg, "dry_run", False):
        print(f"\n----- DRY-RUN {alert.kind} {alert.symbol} {fmt_ms(alert.bar_ms) if alert.bar_ms else ''} -----")
        print(alert.text)
        sent.append(alert.text)
        return
    ok = bool(notifier and notifier.send_message(alert.text))
    db.log_alert(alert.symbol, alert.bar_ms, alert.kind, alert.text, ok)
    if ok:
        sent.append(alert.text)
    else:
        logger.error("[alert] 推送失败，未标记成功（下次重试）: %s %s", alert.kind, alert.symbol)


# ---------------------------------------------------------------- 心跳 / 周报

def heartbeat_text(db, cfg) -> str:
    lines = [f"{cfg.alert_prefix}心跳 {dt.datetime.now(dt.timezone.utc).strftime('%m-%d %H:%M')} UTC"]
    for r in db.feed_status():
        ok = f"ok（{r['source']}）" if r["last_ok_at"] else "❌ 失败"
        extra = f" 最近错误: {r['last_error']}" if r["last_error"] and not r["last_ok_at"] else ""
        lines.append(f"数据源 {r['symbol']}: {ok}{extra}")
    for symbol in cfg.symbols:
        row = db.open_position(symbol)
        if row:
            st = "待确认" if row["status"] == "pending" else "持仓中"
            lines.append(f"{symbol}: {st} {row['contracts']} 张 @ "
                         f"{fmt_money(row['actual_entry'] or row['entry_price'])}，"
                         f"止损 {fmt_money(row['actual_stop'] or row['stop_price'])}")
    if not any(db.open_position(s) for s in cfg.symbols):
        lines.append("当前无持仓")
    recent = db.recent_alerts(limit=200)
    day = [a for a in recent if a["created_at"] > __import__("time").time() - 86400]
    failed = [a for a in day if not a["sent_successfully"]]
    lines.append(f"近 24h 提醒 {len(day)} 条，其中失败 {len(failed)} 条")
    return "\n".join(lines)


def weekly_text(db, cfg) -> str:
    import time as _t
    since = _t.time() - 7 * 86400
    rows = db.closed_positions(since)
    lines = [f"{cfg.alert_prefix}周报 {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}"]
    if rows:
        rs = [r["r_multiple"] or 0.0 for r in rows]
        wins = [x for x in rs if x > 0]
        lines.append(f"本周平仓 {len(rs)} 笔：均R {sum(rs) / len(rs):+.2f}，"
                     f"胜率 {len(wins) / len(rs) * 100:.0f}%，总R {sum(rs):+.1f}")
    else:
        lines.append("本周无平仓")
    allrows = db.closed_positions(0.0)
    if allrows:
        rs = [r["r_multiple"] or 0.0 for r in allrows]
        lines.append(f"累计 {len(rs)} 笔：均R {sum(rs) / len(rs):+.2f}，总R {sum(rs):+.1f}，"
                     f"权益 {fmt_money(cfg.equity)}U × (1+{cfg.risk_pct:.2f}R 复利) ≈ "
                     f"{fmt_money(cfg.equity * (1 + cfg.risk_pct * sum(rs) / max(len(rs), 1)) ** len(rs) if rs else cfg.equity)}U")
    pending = [r for r in db.recent_positions(200) if r["status"] == "pending"]
    lines.append(f"未确认（按假设值统计）{len(pending)} 笔")
    return "\n".join(lines)
