"""MR（超跌反弹）腿 — HL（xyz:XYZ100）适配，挂在现有进程的 5 分钟 job 里跑。

与 lighter-ma-bot 的 `app/mr.py` 同一套规则（信号口径 `app/mr_signal.py` 逐字节
同码，P3 对抗审核时做两侧一致性断言）；差异只在与交易所的边界：

- **数据**：`HlInfo.candles(coin, '1h', …)` 直接给 ET 可聚合的小时 bar；
  szDecimals 从 `meta?dex=xyz` 现查，绝不硬编码。
- **权益口径**：统一账户下现货 USDC 余额**已含**全部持仓浮盈亏（见
  `app/hl/equity_fix.py` 的结论），所以 sizing 基数 = spot accountValue −
  xyz dex 的 marginUsed（MR 仓占用）。uPnL 不用再减——它已经在两边。
- **市价单**：HL 没有真市价，用激进 IOC 限价（mid ± slippage_bps 带宽），
  价格按官方规则取整（≤5 位有效数字、≤6−szDecimals 位小数）。
- **幂等**：cloid 由 `make_cloid(coin, signal_ms, purpose)` 确定性推导；
  台账 `signal_date` 唯一约束兜底。
- **费用**：HIP-3 taker 0.09%/边，净盈亏按 0.0009×名义×2 边估算入账
  （fee_quote 列），funding 用 `userFundingHistory` 实际值。
- **对账 fail-closed**、**kill/开关只挡入场不挡离场**、**MR 异常不拖死主腿**
  与 Lighter 侧完全一致。
- 开关：configuration `mr_enabled`（默认 '0'）+ `/admin/hl/mr` 写入并落
  `mr_admin` 事件；全局 `hl_kill()` 同样挡 MR 入场。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from app import mr_signal
from app.hl.exchange import _clean_alert_text, make_cloid

logger = logging.getLogger(__name__)

#: 与 lighter 侧同值：读不懂的全局 kill 事件不当开仓绿灯（db.hl_kill 读配置与事件）。
MR_EVENT_KIND = "mr_admin"
#: taker 费率（HIP-3 xyz 实测 0.09%/边）——净盈亏的估算项。
TAKER_FEE_RATE = 0.0009


@dataclass(frozen=True)
class HlMrConfig:
    """HL MR 腿参数。改动 = 改这里 + 走 PR（参数面刻意最小）。"""

    coin: str = "XYZ100"
    dex: str = "xyz"
    exposure: float = 0.5               # 名义 = 权益 × 0.5
    min_notional: float = 10.0
    slippage_bps: int = 15              # IOC 限价的保护带宽（单边）
    candle_hours: int = 500             # 1h×500 ≈ 21 天会话


def _round_px(px: float, sz_decimals: int) -> float:
    """HL 价格精度：≤5 位有效数字且小数位 ≤ 6−szDecimals（官方规则）。"""
    out = float(f"{px:.5g}")
    return round(out, max(6 - int(sz_decimals), 0))


def _floor_sz(sz: float, sz_decimals: int) -> float:
    step = 10 ** int(sz_decimals)
    return int(sz * step) / step


class HlMrLeg:
    """HL 侧 MR 腿。`tick()` 每 5 分钟由调度器调一次；其余一律不碰。"""

    def __init__(self, *, db: Any, info: Any, exchange: Any,
                 address_fn: Callable[[], str], config: Optional[HlMrConfig] = None,
                 notifier: Any = None, clock: Callable[[], float] = time.time):
        self.db = db
        self.info = info
        self.exchange = exchange
        self.address_fn = address_fn
        self.config = config or HlMrConfig()
        self.notifier = notifier
        self.clock = clock
        self._recon_alerted_at = 0.0

    # ------------------------------------------------------------------ 读

    def _enabled(self) -> bool:
        try:
            return self.db.get_config_str("mr_enabled", "0") in ("1", "true", "on")
        except Exception:                                     # noqa: BLE001
            return False

    def _global_kill(self) -> bool:
        try:
            return bool(self.db.hl_kill())
        except Exception:                                     # noqa: BLE001
            return True                                       # 读不懂当 kill

    def _alert(self, text: str) -> None:
        clean = _clean_alert_text("MR: " + text, limit=800)
        logger.warning("%s", clean)
        try:
            self.db.hl_log_event("mr", clean)
        except Exception:                                     # noqa: BLE001
            pass
        if self.notifier is not None:
            try:
                self.notifier.send_message(clean)
            except Exception as exc:                          # noqa: BLE001
                logger.warning("mr: alert delivery failed: %s", exc)

    # ------------------------------------------------------------------ 账户读数

    def _read_account(self, address: str) -> Dict[str, Any]:
        cfg = self.config
        spot = self.info.spot_state(address) or {}
        ms = spot.get("marginSummary") or {}
        equity_total = float(ms.get("accountValue") or 0.0)
        dex_state = self.info.user_state_dex(address, cfg.dex) or {}
        dms = dex_state.get("marginSummary") or {}
        mr_margin_used = float(dms.get("marginUsed") or 0.0)
        mr_pos = None
        for item in dex_state.get("assetPositions") or []:
            p = item.get("position") or {}
            if (p.get("coin") or "") != cfg.coin:
                continue
            szi = float(p.get("szi") or 0.0)
            if szi == 0:
                continue
            lev = p.get("leverage") or {}
            mr_pos = {
                "szi": szi, "side": 1 if szi > 0 else -1,
                "entry_px": float(p.get("entryPx") or 0.0),
                "position_value": float(p.get("positionValue") or 0.0),
                "margin_used": float(lev.get("marginUsed") or 0.0),
                "unrealized_pnl": float(p.get("unrealizedPnl") or 0.0),
            }
            break
        return {"equity_total": equity_total, "mr_margin_used": mr_margin_used,
                "mr_pos": mr_pos}

    def _sz_decimals(self) -> Optional[int]:
        cfg = self.config
        try:
            for row in self.info.perp_meta_dex(cfg.dex) or []:
                if (row.get("name") or "") == cfg.coin:
                    return int(row.get("szDecimals"))
        except Exception as exc:                              # noqa: BLE001
            logger.warning("mr: perp_meta_dex failed: %s", exc)
            return None
        return None

    def _candles(self, now_ms: int) -> List[dict]:
        cfg = self.config
        start = now_ms - cfg.candle_hours * 3600_000
        return list(self.info.candles(cfg.coin, "1h", start, now_ms) or [])

    def _mark(self, rows: List[dict], mr_pos: Optional[dict]) -> float:
        cfg = self.config
        try:
            mids = self.info.all_mids() or {}
            mark = float(mids.get(cfg.coin) or 0.0)
        except Exception:                                     # noqa: BLE001
            mark = 0.0
        if mark <= 0 and rows:
            mark = float(rows[-1].get("c") or 0.0)
        if mark <= 0 and mr_pos:
            value, szi = mr_pos["position_value"], abs(mr_pos["szi"])
            mark = value / szi if szi else 0.0
        return mark

    # ------------------------------------------------------------------ 主流程

    def tick(self, now_ms: Optional[int] = None) -> dict:
        cfg = self.config
        now = int(now_ms if now_ms is not None else int(self.clock() * 1000))
        status: Dict[str, Any] = {"enabled": False, "kill": False,
                                  "state": "disabled", "recon": "unknown",
                                  "error": "", "updated_at": self.clock()}
        try:
            return self._tick_inner(now, status)
        except Exception as exc:                              # noqa: BLE001
            logger.exception("mr: tick failed")
            status.update(state="error", error=f"{type(exc).__name__}: {exc}",
                          updated_at=self.clock())
            self._alert(f"MR tick 异常：{type(exc).__name__}: {exc}")
            return status

    def _tick_inner(self, now: int, status: Dict[str, Any]) -> dict:
        cfg = self.config
        address = (self.address_fn() or "").strip()
        if not address:
            status["error"] = "HL_ADDRESS 为空"
            status["state"] = "error"
            return status
        enabled = self._enabled()
        kill = self._global_kill()
        status.update(enabled=enabled, kill=kill)
        open_row = self.db.mr_open_trade()
        account = self._read_account(address)
        pos = account["mr_pos"]

        # ---- 对账（双向） ------------------------------------------------------
        holding = False
        recon_ok = True
        fresh = (open_row is not None and open_row["entry_ts"]
                 and now / 1000.0 - float(open_row["entry_ts"]) < 2 * 3600.0)
        if open_row is not None and pos is None and fresh:
            holding = True                                    # 刚下单，持仓行还没出现
        elif open_row is not None and pos is None:
            recon_ok = False
            self._recon_fail("台账有 OPEN 仓但交易所在 xyz dex 没有 XYZ100 持仓（人工核对）")
        elif open_row is None and pos is not None:
            recon_ok = False
            self._recon_fail("交易所有 XYZ100 持仓但台账没有 OPEN（人工核对，不自动吃）")
        elif open_row is not None and pos is not None:
            holding = True
        if recon_ok:
            status["recon"] = "ok"
            self._recon_alerted_at = 0.0
        else:
            status["recon"] = "err"
            status["state"] = "recon_err"
            return status

        # ---- 会话与信号 --------------------------------------------------------
        rows = self._candles(now)
        sessions = mr_signal.aggregate_sessions(rows)
        if len(sessions) < 6:
            status["state"] = "no_data"
            status["error"] = f"usable sessions={len(sessions)}"
            return status
        status["last_session_date"] = sessions[-1].date
        status["sessions_usable"] = len(sessions)

        if not enabled and not holding:
            status["state"] = "disabled"
            return status
        if holding:
            return self._maybe_exit(now, sessions, rows, open_row, pos, status)
        return self._maybe_enter(now, sessions, rows, account, status)

    def _recon_fail(self, message: str) -> None:
        now_s = self.clock()
        if now_s - self._recon_alerted_at >= 6 * 3600.0:
            self._recon_alerted_at = now_s
            self._alert(f"MR 对账不平：{message}")
        logger.error("mr recon: %s", message)

    # ------------------------------------------------------------------ 入场

    def _maybe_enter(self, now: int, sessions, rows, account: dict,
                     status: Dict[str, Any]) -> dict:
        cfg = self.config
        if status.get("kill"):
            status["state"] = "flat"
            return status
        decision = mr_signal.entry_decision(sessions=sessions, now_ms=now, flat=True)
        status["state"] = "flat"
        if decision is None:
            self._mark_expired_signal(sessions, now)
            return status
        signal_date = decision["signal_date"]
        if self.db.mr_trade_by_signal(signal_date) is not None:
            return status
        sz_dec = self._sz_decimals()
        if sz_dec is None:
            status["state"] = "error"
            status["error"] = "szDecimals 未知（meta?dex 查不到该币）"
            self._alert("MR 放弃：szDecimals 未知，拒单（fail-closed）")
            return status
        mark = self._mark(rows, None)
        if mark <= 0:
            status["state"] = "no_data"
            status["error"] = "无可用标记价"
            return status
        equity_total = account["equity_total"]
        base = equity_total - account["mr_margin_used"]
        if equity_total <= 0 or base <= 0:
            status["state"] = "no_equity"
            return status
        notional = base * cfg.exposure
        sz = _floor_sz(notional / mark, sz_dec)
        if notional < cfg.min_notional or sz <= 0:
            reason = (f"权益不足：equity={equity_total:.2f} notional={notional:.2f} "
                      f"sz={sz}（min ${cfg.min_notional}）")
            self.db.mr_mark_skipped(signal_date, reason)
            self._alert(f"MR 信号 {signal_date} 放弃：{reason}")
            return status
        if notional > base:
            self.db.mr_mark_skipped(signal_date, "notional > equity（保证金不足保护）")
            return status

        cloid = make_cloid(cfg.coin, _signal_ms(signal_date), "mr_entry")
        limit_px = _round_px(mark * (1 + cfg.slippage_bps / 10000.0), sz_dec)
        result = self.exchange.place_order(
            cfg.coin, True, sz, limit_px, reduce_only=False, tif="Ioc",
            cloid=cloid)
        if not result.get("ok"):
            # 下单失败：不写台账（下次 tick 同 cloid 重试），告警由 exchange 层清洗
            status["state"] = "order_rejected"
            status["error"] = str(result.get("error") or result.get("raw") or "unknown")
            self._alert(f"MR 入场被拒：{status['error'][:200]}")
            return status
        trade_id = self.db.mr_record_entry(
            signal_date=signal_date, entry_session=decision["entry_session_date"],
            qty=sz, qty_int=int(round(sz * 10 ** 4)), entry_price=mark,
            entry_order_id=str(result.get("oid") or ""),
            entry_ts=now / 1000.0, notional=sz * mark, margin=sz * mark,
            equity_at_entry=equity_total,
            raw={"ref_price": mark, "limit_px": limit_px, "cloid": cloid,
                 "result_status": str(result.get("status"))})
        status["state"] = "entered"
        status["last_fill_at"] = now / 1000.0
        self._alert(f"MR 入场：{cfg.coin} {sz} @≈{mark:.2f}"
                    f"（名义 {sz * mark:.2f}，信号 {signal_date}），台账 #{trade_id}")
        return status

    def _mark_expired_signal(self, sessions, now_ms: int) -> None:
        """信号日已整体错过（入场日已是过去时）却没有台账 → 记 SKIPPED，可审计。"""
        sigs = mr_signal.session_signals(sessions)
        k = mr_signal.completed_upto(sessions, now_ms)
        if k <= 0 or not sigs[k - 1]["entry"]:
            return
        s = sessions[k - 1]
        entry_day = mr_signal.next_business_day(s.date)
        today = (datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
                 .astimezone(mr_signal.ZoneInfo(mr_signal.SESSION_TZ))
                 .strftime("%Y-%m-%d"))
        if entry_day < today and self.db.mr_trade_by_signal(s.date) is None:
            self.db.mr_mark_skipped(s.date, "窗口错过（停机/延迟）")

    # ------------------------------------------------------------------ 离场

    def _maybe_exit(self, now: int, sessions, rows, open_row,
                    pos: Optional[dict], status: Dict[str, Any]) -> dict:
        cfg = self.config
        entry_session = str(open_row["entry_session"] or open_row["signal_date"])
        exit_date = mr_signal.exit_decision(sessions=sessions, now_ms=now,
                                            entry_session_date=entry_session)
        status["state"] = "holding"
        status["pos"] = {
            "signal_date": open_row["signal_date"],
            "entry_session": entry_session,
            "qty": open_row["qty"],
            "entry_price": open_row["entry_price"],
            "notional": open_row["notional"],
            "upnl": pos["unrealized_pnl"] if pos else None,
        }
        if exit_date is None:
            return status
        sz_dec = self._sz_decimals()
        if sz_dec is None:
            status["state"] = "error"
            status["error"] = "szDecimals 未知，无法平仓"
            self._alert("MR 离场受阻：szDecimals 未知（fail-closed，下轮重试）")
            return status
        mark = self._mark(rows, pos)
        if mark <= 0:
            status["state"] = "error"
            status["error"] = "无可用标记价"
            return status
        sz = abs(float(pos["szi"])) if pos else float(open_row["qty"])
        cloid = make_cloid(cfg.coin, _signal_ms(str(exit_date)), "mr_exit")
        limit_px = _round_px(mark * (1 - cfg.slippage_bps / 10000.0), sz_dec)
        result = self.exchange.place_order(
            cfg.coin, False, sz, limit_px, reduce_only=True, tif="Ioc",
            cloid=cloid)
        if not result.get("ok"):
            status["state"] = "holding"
            status["error"] = str(result.get("error") or result.get("raw") or "unknown")
            self._alert(f"MR 离场被拒（下轮重试）：{status['error'][:200]}")
            return status
        funding_paid = self._funding_paid(open_row, now)
        notional = float(open_row["notional"] or 0.0)
        entry_price = float(open_row["entry_price"] or 0.0)
        fee_est = TAKER_FEE_RATE * (notional + sz * mark)
        funding_pct = (funding_paid / notional * 100.0 if notional > 0 else None)
        net_pnl = ((mark - entry_price) * sz - funding_paid - fee_est)
        self.db.mr_close_trade(
            int(open_row["id"]), exit_price=mark,
            exit_order_id=str(result.get("oid") or ""),
            exit_ts=now / 1000.0, exit_reason="sma5", exit_signal_date=exit_date,
            funding_pct=funding_pct, fee_quote=fee_est, net_pnl=net_pnl,
            raw={"ref_price": mark, "limit_px": limit_px, "cloid": cloid,
                 "funding_paid_usdc": funding_paid, "fee_est": fee_est,
                 "result_status": str(result.get("status"))})
        status["state"] = "exited"
        status["last_fill_at"] = now / 1000.0
        self._alert(
            f"MR 离场：{cfg.coin} 平多 @≈{mark:.2f}（净 {net_pnl:+.2f} USDC，"
            f"funding {funding_pct if funding_pct is not None else '?'}%、费用≈{fee_est:.2f}），"
            f"信号日 {exit_date}")
        return status

    def _funding_paid(self, open_row, now_ms: int) -> float:
        """持仓期实际支付的资金费（USDC，正=已付）。取不到 → 0（宁可低估也不编数）。"""
        cfg = self.config
        try:
            start_ms = int(float(open_row["entry_ts"]) * 1000) - 3600_000
            rows = self.info.user_funding_history(self.address_fn(), start_ms, now_ms)
            total = 0.0
            for item in rows or []:
                delta = item.get("delta") or {}
                if delta.get("type") == "funding" and delta.get("coin") == cfg.coin:
                    total += float(delta.get("usdc") or 0.0)
            return -total          # HL：usdc 负 = 多头支付 → 取反变「已付为正」
        except Exception as exc:                              # noqa: BLE001
            logger.warning("mr: funding history failed: %s", exc)
            return 0.0


def _signal_ms(signal_date: str) -> int:
    """会话日期 → 确定性毫秒键（UTC 零点）。cloid 只需要稳定。"""
    dt = datetime.strptime(signal_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
