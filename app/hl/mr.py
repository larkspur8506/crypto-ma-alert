"""MR（超跌反弹）腿 — HL（xyz:XYZ100）适配，挂在现有进程的 5 分钟 job 里跑。

与 lighter-ma-bot 的 `app/mr.py` 同一套规则（信号口径 `app/mr_signal.py` 逐字节
同码，P3 对抗审核时做两侧一致性断言）；差异只在与交易所的边界：

- **数据**：`HlInfo.candles(coin, '1h', …)` 直接给 ET 可聚合的小时 bar；
  szDecimals 从 `meta?dex=xyz` 现查，绝不硬编码。
- **权益口径**：统一账户下现货 USDC 余额**已含**全部持仓浮盈亏（见
  `app/hl/equity_fix.py` 的结论），所以 sizing 基数 = `info.account_equity()`
  （现货 USDC 总额）− MR 仓占用（`marginUsed + uPnL`，与 Lighter 侧同口径）。
  注意 `spotClearinghouseState` **没有 marginSummary**，必须读 `balances`。
- **市价单**：HL 没有真市价，用激进 IOC 限价（mid ± slippage_bps 带宽），
  价格按官方规则取整（≤5 位有效数字、≤6−szDecimals 位小数）。
- **成交只认持仓**：回执 `status=filled` 之外还要看到 xyz dex 的仓真的出现/消失；
  `resting`/`dry`/零成交一律不记账（离场未清干净保持 OPEN 重试）。下单前写一条
  `mr_entry_intent` / `mr_exit_intent` 事件：确认因读错误/重启丢失时，下一轮凭意图
  把交易所真实仓位补记进台账（只认同一 signal_date），避免 recon_err 冻结。
- **杠杆**：入场前 `update_leverage(coin, 5x, cross)`（幂等）——否则该 dex 若是
  30x 逐仓，−3.3% 就强平，而 MR 本身不挂止损。
- **幂等**：台账 `signal_date` 唯一约束 + 「只在空仓时入场」是护栏；cloid 按 tick 分钟
  派生（同一轮重放同键，跨轮重试换键 —— 已用过的 cloid 重试会被拒）。
- **费用**：HIP-3 taker 0.09%/边，净盈亏按 0.0009×名义×2 边估算入账
  （fee_quote 列），funding 用 `userFundingHistory` 实际值。
- **对账 fail-closed**、**kill/开关只挡入场不挡离场**、**MR 异常不拖死主腿**
  与 Lighter 侧完全一致。
- **基金级闸门走 `hl_kill`**（2026-11-02 第五轮核对）：HL 的回撤熔断会
  `hl_set_kill(origin="dd")`、当日亏损同理，MR 读 `db.hl_kill()` ⇒ 两类熔断都挡得住。
  （Lighter 侧没有这条链路，已在那边显式接 `RiskState`。）
- **占用口径 = 价格敞口（名义 = 数量 × 标记价）**，不是保证金：5x 下保证金只有名义的
  1/5，按保证金扣会让 crypto 腿在 MR 持仓期间被放大 1.8 倍（Lighter 侧是 1x，扣满额）。
- 开关：configuration `mr_enabled`（默认 '0'）+ `/admin/hl/mr` 写入并落
  `mr_admin` 事件；全局 `hl_kill()` 同样挡 MR 入场。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from app import mr_signal
from app.hl.exchange import _clean_alert_text, make_cloid

logger = logging.getLogger(__name__)

#: 与 lighter 侧同值：读不懂的全局 kill 事件不当开仓绿灯（db.hl_kill 读配置与事件）。
MR_EVENT_KIND = "mr_admin"
#: 下单意图事件（成交确认丢失时的认领凭据）：写在下单**之前**，每轮重试各一条。
_ENTRY_INTENT_KIND = "mr_entry_intent"
_EXIT_INTENT_KIND = "mr_exit_intent"
#: taker 费率（HIP-3 xyz 实测 0.09%/边）——净盈亏的估算项。
TAKER_FEE_RATE = 0.0009


@dataclass(frozen=True)
class HlMrConfig:
    """HL MR 腿参数。改动 = 改这里 + 走 PR（参数面刻意最小）。"""

    #: HIP-3 的 coin 全程用 **dex 限定名**：meta?dex=xyz 返回 `xyz:XYZ100`、
    #  candleSnapshot/ordering 同名（裸名 candleSnapshot 直接 500，2026-11-01 部署
    #  冒烟实测）。allMids 无此键 → _mark 自动走 K 线收盘回退，无需处理。
    coin: str = "xyz:XYZ100"
    dex: str = "xyz"
    exposure: float = 0.5               # 名义 = 权益 × 0.5
    min_notional: float = 10.0
    slippage_bps: int = 15              # IOC 限价的保护带宽（单边）
    candle_hours: int = 500             # 1h×500 ≈ 21 天会话
    #: funding 基线（%名义/小时）。实测 2026-01-22~11-01 中位 0.0004%/h（3.5%/年）。
    funding_baseline_pct_per_h: float = 0.0004
    #: 自动停用门槛：实际 funding 超基线 `funding_disable_multiple` 倍，**且**已付金额
    #: 超过 `funding_disable_min_pct`（%名义）。2026-11-02 第四轮复核：旧值 2 倍落在
    #: 正常分布中间（Lighter 9 个月 12 笔回合有 7 笔触发）；10 倍 + 0.10% 只拦尖峰。
    funding_disable_multiple: float = 10.0
    funding_disable_min_pct: float = 0.10
    #: 下单意图的认领窗口（秒）：成交确认因读错误/进程重启丢失时，下一轮凭意图
    #: 把交易所真实持仓补记进台账，避免「有仓无台账 → recon_err 要人工改库」。
    adopt_window_s: float = 6 * 3600.0
    #: 即时止损触发器：成交滑点（|avg_px−ref|/ref）超过该 bp → 自动停用 MR 腿。
    #: 注意：IOC 限价 = ref ± `slippage_bps`(15bp)，成交价不可能越过限价 ⇒ >20bp
    #: 这一支在正常路径上不可达（保留作防御性断言）。真正有信息量的是
    #: 「吃掉 ≥`slippage_warn_bps` 带宽」——那条会告警（不自动停用）。
    max_slippage_bps: float = 20.0
    slippage_warn_bps: float = 10.0
    #: 显式杠杆/保证金模式：Lighter 侧有 `ensure_isolated(1x)`，HL 侧原来完全没设 ——
    #: 若该 dex 落成 30x 逐仓，−3.3% 就强平（MR 无止损、历史最差 −17.8%）。
    #: 入场前按这里设一次（幂等）；**设置失败即拒绝入场**（fail-closed）。
    leverage: int = 5
    cross_margin: bool = True
    #: 成交确认：IOC 回执 `filled` 之外，还要看到 xyz dex 的持仓真的出现/消失才记账
    #: （`resting`/`dry`/部分成交都不算成交；2026-11-02 复核 P1）。
    fill_confirm_s: float = 12.0
    fill_poll_s: float = 1.0


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
        #: 订单类告警去重（key → 上次发送时刻）；入场窗口内每 5 分钟重试不刷屏。
        self._order_alerted_at: Dict[str, float] = {}

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
            except Exception:                                 # noqa: BLE001
                logger.exception("MR 告警通知失败")

    def _alert_throttled(self, key: str, text: str,
                         interval_s: float = 30 * 60.0) -> None:
        """同一类订单问题在 `interval_s` 内只发一次（入场窗口每轮重试不刷屏）。"""
        now_s = self.clock()
        last = float(self._order_alerted_at.get(key, 0.0) or 0.0)
        if last and now_s - last < interval_s:
            logger.warning("mr: %s（同类告警已去重）", text)
            return
        self._order_alerted_at[key] = now_s
        self._alert(text)

    def _await_position(self, address: str, *, want: bool,
                        confirm_s: Optional[float] = None
                        ) -> Tuple[Optional[dict], bool]:
        """轮询 xyz dex 持仓，直到「出现」（want=True）或「消失」（want=False）。

        返回 (最后一次读到的持仓, read_ok)。`read_ok=False` = 整轮都没读到真相 ——
        离场方必须按「未知」处理，绝不当成「仓已消失」写出假 CLOSED。
        成交只认持仓，不认回执 —— HL 的 `status=filled` 也可能只成交一部分。
        """
        cfg = self.config
        budget = float(cfg.fill_confirm_s if confirm_s is None else confirm_s)
        poll = max(float(cfg.fill_poll_s), 0.0)
        attempts = 1 if poll <= 0 or budget <= 0 else int(budget / poll) + 1
        pos = None
        read_ok = False
        started = time.monotonic()
        for i in range(max(attempts, 1)):
            try:
                pos = (self._read_account(address) or {}).get("mr_pos")
            except Exception as exc:                              # noqa: BLE001
                # 单次读失败（503/限流）不打断确认：否则已成交的仓没有台账 → recon_err。
                logger.warning("mr: 成交确认读账户失败（继续轮询）：%s: %s",
                               type(exc).__name__, exc)
            else:
                read_ok = True
                if (pos is not None) is bool(want):
                    return pos, True
            # 墙钟上限（2026-11-02 第五轮）：HL 一次失败读含 3 次重试 + 1/3/9s 退避，
            # 实测 ≈26s；固定 13 次会变成 5 分钟以上的阻塞，而 MR tick 持 `_tick_lock`，
            # 会把 :03/:33 主 tick（对账/离场/补挂止损）一起顶住。
            elapsed = time.monotonic() - started
            if elapsed >= budget:
                logger.warning("mr: 成交确认超时（%.1fs ≥ %.1fs，%d 次读），按当前真相返回",
                               elapsed, budget, i + 1)
                break
            if i + 1 < attempts:
                time.sleep(max(min(poll, budget - elapsed), 0.0))
        return pos, read_ok

    def _auto_disable(self, reason: str) -> None:
        """即时止损触发器：写 mr_enabled=0（只挡新入场）+ 审计事件 + 告警。"""
        try:
            self.db.set_config("mr_enabled", "0")
            self.db.hl_log_event(MR_EVENT_KIND, f"MR 腿自动停用：{reason}",
                                 data={"enabled": False, "by": "auto", "reason": reason})
        except Exception:                                     # noqa: BLE001
            logger.exception("MR 自动停用写库失败")
        self._alert(f"MR 腿自动停用：{reason}")

    def _slippage_guard(self, result: dict, ref: float, *, side: str) -> None:
        """成交价 vs 参考价的滑点：超 `max_slippage_bps` 自动停用；吃掉大半带宽则告警。"""
        try:
            avg = result.get("avg_px")
            if not avg or not ref or ref <= 0:
                return
            slip_bps = abs(float(avg) - ref) / ref * 10000.0
            if slip_bps > self.config.max_slippage_bps:
                self._auto_disable(f"{side}滑点 {slip_bps:.1f}bp > "
                                   f"{self.config.max_slippage_bps:.0f}bp"
                                   f"（ref={ref:.2f} avg={float(avg):.2f}）")
            elif slip_bps >= self.config.slippage_warn_bps:
                self._alert_throttled(
                    f"fill_quality_{side}",
                    f"MR {side}成交吃掉 {slip_bps:.1f}bp / "
                    f"{self.config.slippage_bps:.0f}bp 滑点带宽"
                    f"（ref={ref:.2f} avg={float(avg):.2f}）——盘口偏薄，观察后续成交")
        except Exception:                                     # noqa: BLE001
            logger.exception("MR 滑点检查失败")

    def _funding_guard(self, funding_pct: Optional[float], entry_ts: float,
                       now_s: float) -> None:
        """实际付出 funding 超基线 N 倍且绝对额 >0.10% → 自动停用（触发器）。"""
        if funding_pct is None or funding_pct <= 0:
            return
        hours = max(now_s - entry_ts, 0.0) / 3600.0
        expected = self.config.funding_baseline_pct_per_h * hours
        if (expected > 0
                and funding_pct > self.config.funding_disable_multiple * expected
                and funding_pct > self.config.funding_disable_min_pct):
            self._auto_disable(
                f"funding {funding_pct:.3f}% 超基线 "
                f"{self.config.funding_disable_multiple:g} 倍"
                f"（预期 {expected:.3f}%，持仓 {hours:.1f}h）")

    def _ensure_leverage(self, status: Dict[str, Any]) -> bool:
        """入场前把该 coin 的杠杆/保证金模式设成配置值（幂等）。

        失败 → **拒绝本轮入场**（fail-closed）：MR 不挂止损，若该 dex 还是 30x 逐仓，
        −3.3% 就强平；「按现有设置继续」正是这条保护要消灭的尾部风险。
        """
        cfg = self.config
        try:
            res = self.exchange.update_leverage(
                cfg.coin, int(cfg.leverage), cross=bool(cfg.cross_margin))
            if not res.get("ok"):
                raise RuntimeError(str(res.get("error") or res.get("status") or "unknown"))
            status["leverage"] = f"{cfg.leverage}x/{'cross' if cfg.cross_margin else 'isolated'}"
            return True
        except Exception as exc:                              # noqa: BLE001
            status["leverage_error"] = f"{type(exc).__name__}: {exc}"
            self._alert_throttled(
                "leverage", f"MR 杠杆设置失败，本轮拒绝入场：{type(exc).__name__}: {exc}")
            return False

    # ------------------------------------------------------------------ 入场

    # ------------------------------------------------------------------ 账户读数

    def _read_account(self, address: str) -> Dict[str, Any]:
        cfg = self.config
        # 2026-11-02 第四轮复核：`spotClearinghouseState` **没有** marginSummary，
        # 原来读 `["marginSummary"]["accountValue"]` 恒 0（no_equity 静默不进场）。
        # 统一账户的正确口径 = 现货 USDC 总额（已含各 dex 浮盈亏），由 info 统一实现。
        equity_total = float(self.info.account_equity(address) or 0.0)
        dex_state = self.info.user_state_dex(address, cfg.dex) or {}
        dms = dex_state.get("marginSummary") or {}
        mr_margin_used = float(dms.get("marginUsed") or 0.0)
        mr_pos = None
        bare = cfg.coin.split(":")[-1]
        for item in dex_state.get("assetPositions") or []:
            p = item.get("position") or {}
            # HIP-3 的 coin 命名两可（`xyz:XYZ100` 或裸 `XYZ100`）：meta 与 allMids 实测带前缀，
            # 但持仓行的命名没有仓时无法实测 —— 两种都认，避免「有仓却看不见」（第五轮）。
            if (p.get("coin") or "") not in (cfg.coin, bare):
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
        # 占用口径 = **价格敞口（数量 × 标记价）**，不是保证金（2026-11-02 第五轮）。
        # 按保证金扣时，同一个 MR 敞口在 5x 下只扣 1/5 —— 扣多少取决于杠杆设置，
        # 而 Lighter 侧是 1x（扣满额）⇒ 同样 exposure，HL 的 crypto 腿会被放大 1.8 倍。
        # 保证金值单独保留在 `mr_margin_reserved` 里做展示，不参与定仓。
        occupied = mr_margin_used
        if mr_pos is not None:
            value = mr_pos["position_value"]
            if not value:
                value = abs(mr_pos["szi"]) * mr_pos["entry_px"]
            occupied = max(abs(value), 0.0)
        return {"equity_total": equity_total, "mr_margin_used": occupied,
                "mr_margin_reserved": mr_margin_used,
                "mr_margin_raw": mr_margin_used, "mr_pos": mr_pos}

    # ------------------------------------------------------------------ 认领

    def _recent_intent(self, kind: str, now_s: float):
        """最近一条「下单意图」事件 → (ts, payload)；超出认领窗口返回 None。"""
        try:
            rows = self.db.hl_recent_events(5, kind=kind)
        except Exception:                                         # noqa: BLE001
            return None
        for row in rows:
            try:
                ts = float(row["created_at"] or 0.0)
                raw = row["data"]
            except Exception:                                     # noqa: BLE001
                continue
            if ts <= 0 or abs(now_s - ts) > float(self.config.adopt_window_s):
                continue
            payload = _event_data(raw)
            if payload.get("signal_date"):
                return ts, payload
        return None

    def _adopt_entry(self, pos: dict, now: int, status: Dict[str, Any]) -> bool:
        """有意图、无台账、xyz dex 有仓 → 按交易所口径补记 OPEN（无需人工改库）。"""
        found = self._recent_intent(_ENTRY_INTENT_KIND, now / 1000.0)
        if found is None:
            return False
        ts, payload = found
        signal_date = str(payload.get("signal_date") or "")
        if not signal_date or self.db.mr_trade_by_signal(signal_date) is not None:
            return False
        filled_sz = abs(float(pos.get("szi") or 0.0))
        if float(pos.get("szi") or 0.0) <= 0:
            # MR 只做多：空头仓不按 MR 入场认领（手工/别的系统开的）。
            logger.warning("mr: xyz dex 的 XYZ100 是空头（szi=%s），跳过入场认领",
                           pos.get("szi"))
            return False
        entry_px = float(pos.get("entry_px") or 0.0) or float(payload.get("ref_price") or 0.0)
        if filled_sz <= 0 or entry_px <= 0:
            return False
        trade_id = self.db.mr_record_entry(
            signal_date=signal_date,
            entry_session=str(payload.get("entry_session") or signal_date),
            qty=filled_sz, qty_int=int(round(filled_sz * 10 ** 4)),
            entry_price=entry_px, entry_order_id="", entry_ts=ts,
            notional=filled_sz * entry_px, margin=filled_sz * entry_px,
            equity_at_entry=payload.get("equity"),
            raw={"adopted": True, "adopt_reason": "fill_confirmation_lost",
                 "intent_ts": ts, "fill_source": "xyz_dex_position"})
        status["state"] = "entered"
        status["adopted"] = "entry"
        self._alert(f"MR 入场确认丢失，已按 xyz dex 持仓补记台账 #{trade_id}："
                    f"{self.config.coin} {filled_sz} @≈{entry_px:.2f}（信号 {signal_date}）")
        return True

    def _adopt_exit(self, open_row, now: int, status: Dict[str, Any]) -> bool:
        """有离场意图、台账 OPEN、xyz dex 已无仓 → 按当前 mid 补记 CLOSED（价格为估算）。"""
        found = self._recent_intent(_EXIT_INTENT_KIND, now / 1000.0)
        if found is None:
            return False
        ts, payload = found
        if str(payload.get("signal_date")) != str(open_row["signal_date"]):
            return False
        mark = self._mark([], None)
        if mark <= 0:
            return False
        qty = float(open_row["qty"] or 0.0)
        entry_price = float(open_row["entry_price"] or 0.0)
        notional = float(open_row["notional"] or 0.0)
        fee_est = TAKER_FEE_RATE * (notional + qty * mark)
        net_pnl = (mark - entry_price) * qty - fee_est
        self.db.mr_close_trade(
            int(open_row["id"]), exit_price=mark, exit_order_id="",
            exit_ts=now / 1000.0, exit_reason="sma5_adopted",
            exit_signal_date=(str(payload.get("exit_signal_date") or "") or None),
            funding_pct=None, fee_quote=fee_est, net_pnl=net_pnl,
            raw={"adopted": True, "price_source": "all_mids", "intent_ts": ts})
        status["state"] = "exited"
        status["adopted"] = "exit"
        self._alert(f"MR 离场确认丢失，xyz dex 已空仓 → 按当前 mid {mark:.2f} 补记 CLOSED"
                    f"（台账 #{open_row['id']}，净 {net_pnl:+.2f} USDC，价格为估算）")
        return True

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
            mids = self.info.all_mids(dex=cfg.dex) or {}
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
            # 去重：维护窗口里每 5 分钟一条会把告警淹掉（第五轮）。
            self._alert_throttled("tick_error",
                                  f"MR tick 异常：{type(exc).__name__}: {exc}")
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
        # 给 crypto 腿定仓用的「MR 占用」：必须在 recon 分支之前写 —— recon_err 时
        # MR 仍可能占着保证金，crypto 腿不该按全权益放大。
        status["margin_used"] = float(account.get("mr_margin_used") or 0.0)
        status["margin_reserved"] = float(account.get("mr_margin_reserved") or 0.0)

        # ---- 认领：确认窗口丢失的成交（读错误 / 进程重启）-------------------------
        fresh = bool(open_row is not None and open_row["entry_ts"]
                     and now / 1000.0 - float(open_row["entry_ts"]) < 2 * 3600.0)
        if open_row is None and pos is not None:
            if self._adopt_entry(pos, now, status):
                open_row = self.db.mr_open_trade()
                fresh = True
        if open_row is not None and pos is None and not fresh:
            if self._adopt_exit(open_row, now, status):
                open_row = None

        # ---- 对账（双向） ------------------------------------------------------
        holding = False
        recon_ok = True
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
        if status.get("adopted") == "exit":
            return status            # 本轮刚补记完离场，不再评估新入场

        # 停用且空仓：连 K 线都不拉（每 5 分钟一轮，停用态不该有取数成本）
        if not enabled and not holding:
            status["state"] = "disabled"
            return status

        # ---- 会话与信号 --------------------------------------------------------
        rows = self._candles(now)
        sessions, dropped = mr_signal.aggregate_sessions_ex(rows)
        self._note_dropped_sessions(dropped, now, status)
        if len(sessions) < 6:
            status["state"] = "no_data"
            status["error"] = f"usable sessions={len(sessions)}"
            return status
        status["last_session_date"] = sessions[-1].date
        status["sessions_usable"] = len(sessions)

        if holding:
            return self._maybe_exit(now, sessions, rows, open_row, pos, status)
        return self._maybe_enter(now, sessions, rows, account, status)

    def _recon_fail(self, message: str) -> None:
        now_s = self.clock()
        if now_s - self._recon_alerted_at >= 6 * 3600.0:
            self._recon_alerted_at = now_s
            self._alert(f"MR 对账不平：{message}")
        logger.error("mr recon: %s", message)

    def _note_dropped_sessions(self, dropped, now_ms: int, status: Dict[str, Any]) -> None:
        """「整个会话因数据不足被弃用」→ 落一条审计事件（同一会话只报一次）。

        2026-11-02 第五轮：末根闸把「用错收盘价」换成了「整天静默丢弃」——
        信号日整天消失、连事件都没有。这里只报**已收盘**的会话，也不推送。
        """
        if not dropped:
            return
        tz = ZoneInfo(mr_signal.SESSION_TZ)
        reported = []
        for item in dropped[-5:]:
            try:
                close_ms = int(datetime.strptime(str(item["date"]), "%Y-%m-%d")
                               .replace(hour=16, minute=0, tzinfo=tz).timestamp() * 1000)
            except (TypeError, ValueError):
                continue
            if close_ms + 3600_000 > now_ms:                  # 还没收盘：正常
                continue
            key = "session_dropped:" + str(item["date"])
            if self._order_alerted_at.get(key):
                continue
            self._order_alerted_at[key] = now_ms / 1000.0
            reported.append(item)
            try:
                self.db.hl_log_event(
                    "mr_session_dropped",
                    f"MR 会话 {item['date']} 数据不足已弃用（bars={item['bars']}、"
                    f"末根 {item['last_bar_et']} ET、{item['reason']}）",
                    data=item)
            except Exception:                                 # noqa: BLE001
                logger.warning("mr: 写 mr_session_dropped 失败", exc_info=True)
        if reported:
            status["dropped_sessions"] = [str(r["date"]) for r in reported]

    def _enabled_since(self) -> float:
        """最近一次「启用」事件的时刻（0 = 从未启用）。给 `_mark_expired_signal` 去噪。"""
        try:
            rows = self.db.hl_recent_events(5, kind=MR_EVENT_KIND)
        except Exception:                                     # noqa: BLE001
            return 0.0
        for row in rows:
            if _event_data(row.get("data") if isinstance(row, Mapping) else None).get("enabled"):
                try:
                    return float(row["created_at"] or 0.0)
                except (KeyError, TypeError, ValueError):
                    continue
        return 0.0

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

        if not self._ensure_leverage(status):
            status["state"] = "margin_mode_failed"
            return status
        cloid = make_cloid(cfg.coin, _attempt_ms(now), "mr_entry")
        limit_px = _round_px(mark * (1 + cfg.slippage_bps / 10000.0), sz_dec)
        self.db.hl_log_event(
            _ENTRY_INTENT_KIND,
            f"MR 入场下单意图：{cfg.coin} {sz} @≈{mark:.2f}（信号 {signal_date}）",
            data={"signal_date": signal_date,
                  "entry_session": decision["entry_session_date"],
                  "planned_sz": sz, "ref_price": mark, "equity": equity_total,
                  "cloid": cloid}, ts=now / 1000.0)
        result = self.exchange.place_order(
            cfg.coin, True, sz, limit_px, reduce_only=False, tif="Ioc",
            cloid=cloid)
        if not result.get("ok"):
            # 下单失败：不写台账（下次 tick 换 cloid 重试），告警由 exchange 层清洗
            status["state"] = "order_rejected"
            status["error"] = str(result.get("error") or result.get("raw") or "unknown")
            self._alert_throttled("entry_rejected", f"MR 入场被拒：{status['error'][:200]}")
            return status
        # 「受理」不等于「成交」：IOC 只认 filled；再用 xyz dex 持仓核一遍实际数量
        if str(result.get("status") or "") != "filled":
            status["state"] = "entry_not_filled"
            status["error"] = f"回执 status={result.get('status')!r}"
            self._alert_throttled(
                "entry_not_filled",
                "MR 入场单未成交（回执不是 filled）——不写台账，下轮重试")
            return status
        filled, _seen = self._await_position(self.address_fn() or "", want=True)
        if filled is None:
            status["state"] = "entry_not_filled"
            status["error"] = "确认窗口内 xyz dex 没有持仓"
            self._alert_throttled(
                "entry_not_filled",
                "MR 入场单回执 filled 但 xyz dex 没有出现持仓——不写台账，下轮重试")
            return status
        filled_sz = abs(float(filled.get("szi") or 0.0))
        if filled_sz <= 0:
            status["state"] = "entry_not_filled"
            return status
        entry_px = float(filled.get("entry_px") or 0.0)
        if entry_px <= 0:
            entry_px = float(result.get("avg_px") or mark)
        trade_id = self.db.mr_record_entry(
            signal_date=signal_date, entry_session=decision["entry_session_date"],
            qty=filled_sz, qty_int=int(round(filled_sz * 10 ** 4)),
            entry_price=entry_px, entry_order_id=str(result.get("oid") or ""),
            entry_ts=now / 1000.0, notional=filled_sz * entry_px,
            margin=filled_sz * entry_px, equity_at_entry=equity_total,
            raw={"ref_price": mark, "limit_px": limit_px, "cloid": cloid,
                 "planned_sz": sz, "filled_sz": filled_sz,
                 "fill_source": "xyz_dex_position",
                 "result_status": str(result.get("status"))})
        if abs(filled_sz - sz) > 1e-9:
            try:
                self.db.hl_log_event("mr_entry_partial_fill",
                                     f"MR 入场部分成交：计划 {sz} / 实际 {filled_sz}")
            except Exception:                             # noqa: BLE001
                pass
        self._slippage_guard(result, mark, side="入场")
        status["state"] = "entered"
        status["last_fill_at"] = now / 1000.0
        self._alert(f"MR 入场：{cfg.coin} {filled_sz} @≈{entry_px:.2f}"
                    f"（名义 {filled_sz * entry_px:.2f}，信号 {signal_date}），台账 #{trade_id}")
        return status

    def _mark_expired_signal(self, sessions, now_ms: int) -> None:
        """信号日已整体错过（含「当天窗口已过」）却没有台账 → 记 SKIPPED，可审计。

        旧实现要求 `entry_day < today`，当天 12:30 之后才恢复运行的信号永远没有审计行。
        现在按**入场截止时刻**判定，并回看最近 5 个会话（覆盖多日停机）。
        """
        sigs = mr_signal.session_signals(sessions)
        k = mr_signal.completed_upto(sessions, now_ms)
        tz = mr_signal.ZoneInfo(mr_signal.SESSION_TZ)
        enabled_since = self._enabled_since()
        for i in range(max(k - 5, 0), k):
            if not sigs[i]["entry"]:
                continue
            s = sessions[i]
            entry_day = mr_signal.next_business_day(s.date)
            open_ms = int(datetime.strptime(entry_day, "%Y-%m-%d")
                          .replace(hour=9, minute=30, tzinfo=tz).timestamp() * 1000)
            deadline_ms = open_ms + int(mr_signal.ENTRY_WINDOW_H * 3600_000)
            if now_ms <= deadline_ms:
                continue
            if now_ms - deadline_ms > 14 * 86400_000:
                continue
            if enabled_since and deadline_ms / 1000.0 < enabled_since:
                # 停用期间过期的信号不补 SKIPPED（第五轮去噪）。
                continue
            if self.db.mr_trade_by_signal(s.date) is None:
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
        if pos is None:
            # 台账有 OPEN 但 xyz dex 还没看到仓（刚下单的宽限窗口）——没有仓可平
            status["state"] = "holding"
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
        cloid = make_cloid(cfg.coin, _attempt_ms(now), "mr_exit")
        limit_px = _round_px(mark * (1 - cfg.slippage_bps / 10000.0), sz_dec)
        self.db.hl_log_event(
            _EXIT_INTENT_KIND,
            f"MR 离场下单意图：{cfg.coin} {sz}（信号日 {open_row['signal_date']}）",
            data={"signal_date": open_row["signal_date"],
                  "exit_signal_date": exit_date, "sz": sz,
                  "ref_price": mark, "cloid": cloid}, ts=now / 1000.0)
        result = self.exchange.place_order(
            cfg.coin, False, sz, limit_px, reduce_only=True, tif="Ioc",
            cloid=cloid)
        if not result.get("ok"):
            status["state"] = "holding"
            status["error"] = str(result.get("error") or result.get("raw") or "unknown")
            self._alert_throttled("exit_rejected",
                                  f"MR 离场被拒（下轮重试）：{status['error'][:200]}")
            return status
        # 受理 ≠ 成交：必须看到 xyz dex 的仓真的没了才关账（否则台账 CLOSED + 裸仓）
        left, left_seen = self._await_position(self.address_fn() or "", want=False)
        if (left is not None or not left_seen
                or str(result.get("status") or "") != "filled"):
            if left is not None and left_seen:
                # 部分成交：先把已平掉的片段入账，否则最终决算只按剩余数量算盈亏。
                prev_sz = abs(float(pos["szi"])) if pos else float(open_row["qty"] or 0.0)
                left_sz = abs(float(left.get("szi") or 0.0))
                filled_sz = prev_sz - left_sz
                if filled_sz > 1e-12:
                    try:
                        self.db.mr_trade_append_partial(
                            int(open_row["id"]), qty=filled_sz, price=mark,
                            ts=now / 1000.0)
                        self.db.hl_log_event(
                            "mr_exit_partial",
                            f"MR 离场部分成交 {filled_sz:g}（剩余 {left_sz:g}）—— "
                            f"该片段按参考价 {mark:.2f} 记账")
                    except Exception:                         # noqa: BLE001
                        logger.warning("mr: 记录部分成交片段失败", exc_info=True)
            remain = (left or {}).get("szi", "?") if isinstance(left, dict) else "?"
            status["state"] = "exit_not_filled"
            status["error"] = f"回执 status={result.get('status')!r}，剩余 {remain}"
            self._alert_throttled(
                "exit_not_filled",
                "MR 离场单已受理但 xyz dex 仍有持仓或读不到持仓（零成交/部分成交/"
                "确认读失败）——台账保持 OPEN，下轮重试")
            return status
        funding_paid = self._funding_paid(open_row, now)
        notional = float(open_row["notional"] or 0.0)
        entry_price = float(open_row["entry_price"] or 0.0)
        # 离场成交价用回执 avg_px（filled 时可用）；取不到才退回 mid。
        exit_px = float(result.get("avg_px") or 0.0) or mark
        partials = _partial_exits(open_row)
        partial_notional = sum(abs(float(p.get("qty") or 0.0))
                               * float(p.get("price") or 0.0) for p in partials)
        partial_pnl = sum((float(p.get("price") or 0.0) - entry_price)
                          * float(p.get("qty") or 0.0) for p in partials)
        fee_est = TAKER_FEE_RATE * (notional + sz * exit_px + partial_notional)
        funding_pct = (funding_paid / notional * 100.0 if notional > 0 else None)
        net_pnl = ((exit_px - entry_price) * sz - funding_paid - fee_est + partial_pnl)
        self.db.mr_close_trade(
            int(open_row["id"]), exit_price=exit_px,
            exit_order_id=str(result.get("oid") or ""),
            exit_ts=now / 1000.0, exit_reason="sma5", exit_signal_date=exit_date,
            funding_pct=funding_pct, fee_quote=fee_est, net_pnl=net_pnl,
            raw={"ref_price": mark, "exit_price_used": exit_px,
                 "price_source": "avg_px" if result.get("avg_px") else "mid",
                 "limit_px": limit_px, "cloid": cloid,
                 "funding_paid_usdc": funding_paid, "fee_est": fee_est,
                 "partial_exits": partials, "partial_pnl": partial_pnl,
                 "result_status": str(result.get("status"))})
        self._slippage_guard(result, mark, side="离场")
        self._funding_guard(funding_pct, float(open_row["entry_ts"] or 0.0),
                            now / 1000.0)
        status["state"] = "exited"
        status["last_fill_at"] = now / 1000.0
        self._alert(
            f"MR 离场：{cfg.coin} 平多 @≈{exit_px:.2f}（净 {net_pnl:+.2f} USDC，"
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
            bare = cfg.coin.split(":")[-1]
            for item in rows or []:
                delta = item.get("delta") or {}
                # HIP-3 的 coin 命名两可（`xyz:XYZ100` 或裸 `XYZ100`），两种都认；
                # 实测主 dex 记录给的是裸名，HIP-3 无历史可证 ⇒ 不做单边假设。
                if (delta.get("type") == "funding"
                        and delta.get("coin") in (cfg.coin, bare)):
                    total += float(delta.get("usdc") or 0.0)
            return -total          # HL：usdc 负 = 多头支付 → 取反变「已付为正」
        except Exception as exc:                              # noqa: BLE001
            logger.warning("mr: funding history failed: %s", exc)
            return 0.0


def _partial_exits(row: Any) -> List[dict]:
    """台账 `raw.partial_exits`（部分成交片段）→ list；解析失败返回 []。

    注意行可能是 `sqlite3.Row`（不是 Mapping，没有 `.get`）—— 统一走下标。
    """
    if isinstance(row, Mapping):
        raw = row.get("raw")
    else:
        try:
            raw = row["raw"]
        except (KeyError, IndexError, TypeError):
            raw = None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, Mapping):
        return []
    parts = raw.get("partial_exits")
    if not isinstance(parts, list):
        return []
    return [dict(p) for p in parts if isinstance(p, Mapping)]


def _event_data(raw: Any) -> Dict[str, Any]:
    """事件 `data` 列（JSON 字符串或 dict）→ dict；解析失败返回 {}。"""
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _attempt_ms(now_ms: int) -> int:
    """按「当前 tick 分钟」派生 cloid 键：同一轮重放同键，跨轮重试换键。

    交易所会拒**已用过**的 cloid；固定用信号日期会让零成交后的重试永远被拒。
    真正的幂等护栏是台账 `mr_trades.signal_date` 唯一约束 + 只在空仓时入场。
    """
    return max(int(now_ms) // 60_000, 1)
