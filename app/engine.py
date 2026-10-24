"""引擎：状态机 + 提醒文案 + 定时任务。

`walk()` 是**纯函数**（不碰 DB / 网络），线上 tick 与离线 dry-run 共用它，
保证回放结果与线上行为一致。

状态机（每标的独立）：
    IDLE ──信号──▶ PENDING（已推入场票，待确认）──确认──▶ OPEN ──离场/止损──▶ CLOSED
                       └──忽略──▶ IGNORED（立即释放仓位槽；此后系统完全不再跟踪该仓位，
                                    连离场提醒也不发——若实际开了仓却点了忽略，需自行管理）

只有**最近一根已收盘 bar** 的信号才是可交易的入场票；更早的信号发「错过」提示，
不发可下单卡片（入场价已变，风险几何已失效）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app import market, posfields, strategy
from app.sizing import SPECS as CONTRACT_SPECS
from app.sizing import effective_risk_pct
from app.sizing import metrics as sizing_metrics
from app.sizing import size_order

logger = logging.getLogger(__name__)
INTERVAL_MS = market.INTERVAL_MS
# pending（待确认）仓位超过这个数量的 4h 根就自动作废释放槽位：
# 用户漏看一次提醒不该永久堵死该标的的后续信号（历史 bug：pending 永久占槽）。
PENDING_BARS_MAX = 3

# 提醒里的时间一律按**北京时间**显示（用户在国内看）：bar 用 UTC 开盘时间做键，
# 但给人看的时间要换成 +08，否则对不上自己的下单/平仓时刻。
# 需要换回 UTC 显示：env DISPLAY_TZ_OFFSET=0（导入时读取，改完要重启容器）。
DISPLAY_TZ_OFFSET_HOURS = float(os.environ.get("DISPLAY_TZ_OFFSET", "8"))
DISPLAY_TZ = dt.timezone(dt.timedelta(hours=DISPLAY_TZ_OFFSET_HOURS))
TZ_LABEL = "北京时间" if DISPLAY_TZ_OFFSET_HOURS == 8 else f"UTC{DISPLAY_TZ_OFFSET_HOURS:+g}"

# 周报窗口：固定「过去 7 天」（滚动窗口，不是自然周——周一跑也能覆盖完整 7 天）。
WEEKLY_WINDOW_S = 7 * 86400
# 系统上线日（周报「上线以来」段的起点，写死；不要拿最早一条成交推——早期手工
# 试单会让起点漂移，读者对不上「共 N 笔」的口径）。
WEEKLY_LAUNCH_DATE = dt.date(2026, 10, 12)


def fmt_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, DISPLAY_TZ).strftime("%m-%d %H:%M")


def fmt_bar_close(ms: int) -> str:
    """bar 的**收盘时刻**（开盘时间 + 4h），北京时间。

    提醒里报「收盘时间」而不是 bar 的开盘时间：bar 用开盘时间标识（20:00 那根 = 20:00~24:00），
    但用户看到「20:00 收盘」会以为信号晚了 4 小时，与自己的下单时间对不上。
    """
    return fmt_ms(ms + INTERVAL_MS)


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

def format_entry(cfg, sig: strategy.Signal, order, position_id: int, stale: bool = False,
                 executor_managed: bool = False) -> str:
    """开仓提醒文案。

    `executor_managed`（执行模式）：True = 执行器已接管（mode=live/dry，调用方
    `_tick_symbol` 传 `executor is not None`）。此时提醒里**不能出现 MEXC 人工下单指引**
    （逐仓 5x / 单位选 USDT / 输入 X USDT / N 张）—— 那些数字是按配置 equity 算的
    人工口径，与 HL 实际成交（跟随实时权益、coin 数量）不是一个口径，照着做就是误导。
    风险行同理不再写具体 U 数，只报目标风险比例并注明以 HL 开仓回报为准。
    默认 False（纯提醒模式）保持原有 MEXC 文案逐字符不变。
    """
    if stale:
        return format_missed(cfg, sig, executor_managed=executor_managed)
    long = sig.is_long
    stop_note = f"距离 {sig.stop_dist * 100:.2f}%" + ("" if long else "，在价格上方")
    # 卡片上写的「风险 X%」必须是**生效后**的比例：空单风险减半开着时，开空卡显
    # 示 risk_pct×0.5，多单不变。开关关着时 effective_risk_pct 原值返回 → 文案
    # 与改动前逐字符一致。
    risk_pct = effective_risk_pct(cfg.risk_pct, sig.side, getattr(cfg, "short_risk_half", False))
    lines = [
        f"{cfg.alert_prefix}{sig.symbol} {sig.action}信号（4h 收盘 {fmt_bar_close(sig.bar_open_ms)} {TZ_LABEL}）",
        f"入场：市价（当前 {fmt_money(sig.entry_price)}）",
        f"止损触发价：{fmt_money(sig.stop_price)}（{stop_note}）",
    ]
    if executor_managed:
        # 托管模式：MEXC 人工下单段（含张数建议）整段退役，换成一句执行说明。
        lines.append("▶ 系统将自动在 Hyperliquid 执行，成交后推送回报")
    elif order is None:
        # 场所规格缺失（registry 里没有该标的的 MEXC 段）：**信号与提醒照常产出**，
        # 只把张数段换成一句显式说明。旧版这里会 KeyError，被 tick 的 per-symbol
        # except 吞成「处理异常」，整个标的的信号一个都出不来（静默坏）。
        lines.append("      （该场所规格未配置，已跳过张数建议）")
    else:
        lines += [
            f"下单：逐仓 {cfg.leverage:.0f}x / 市价 / 单位选 USDT / 输入 {fmt_money(order.notional)} USDT"
            f" → 点【{sig.open_button}】",
            f"      （核对持仓价值 ≈ {fmt_money(order.notional)}U，约 {order.contracts} 张）",
        ]
    lines += [
        f"止损单：计划委托，触发价 {fmt_money(sig.stop_price)}，{sig.close_side}，触发参考=最新成交价",
        f"止盈：不设",
    ]
    if executor_managed:
        # 权益口径不同（配置 equity vs 实时生效权益）→ 不写具体 U 数。
        lines.append(f"风险：单笔 {risk_pct * 100:.2f}% 权益（实际以 HL 开仓回报为准）")
    elif order is not None:
        lines.append(
            f"风险：{order.risk_u:.1f}U"
            + (f"（⚠️ 已退化到最小张数，超过目标 权益 {fmt_money(cfg.equity)}U × {risk_pct * 100:.0f}% = "
               f"{cfg.equity * risk_pct:.1f}U）" if getattr(order, "below_minimum", False)
               else f"（权益 {fmt_money(cfg.equity)}U × {risk_pct * 100:.0f}%）") + "，"
            f"保证金({cfg.leverage:.0f}x) {fmt_money(order.margin)}U")
    if not executor_managed:
        # 离场是 executor.on_exit 的活（自动挂止损/自动平仓 + 成交回报），
        # 人工模式的「4 小时内自己市价平仓」在这个模式下是误导，整行不出现。
        lines.append(f"离场：收到本系统提醒后 4 小时内市价平仓")
    lines += [
        f"确认开仓：{confirm_url(cfg, position_id)}",
    ]
    return "\n".join(lines)


def validate_stop_side(side: int, entry: float, stop: float) -> Optional[str]:
    """校验止损价在正确一侧：做多必须低于入场、做空必须高于入场。返回错误文案或 None。

    确认流里用户手填的止损价必须过这一关——做空填成下方止损会静默污染 R 统计，
    而且实盘上那个止损单方向是反的（等于没有保护）。
    """
    if side == 1 and stop >= entry:
        return f"做多的止损价必须低于入场价（你填的是 入场 {entry:,.2f} / 止损 {stop:,.2f}）"
    if side == -1 and stop <= entry:
        return f"做空的止损价必须高于入场价（你填的是 入场 {entry:,.2f} / 止损 {stop:,.2f}）"
    return None


def review_fill(cfg, symbol: str, suggested_entry: float, suggested_contracts: int,
                entry: float, stop: float, contracts: int, source: str = "contracts",
                equity: Optional[float] = None, side: Optional[int] = None) -> dict:
    """确认开仓后按**真实成交**做一次风险体检（纯计算，不落库）。

    为什么需要：提醒文案里的「输入 N USDT」是按**信号价**算的名义金额
    （N = 风险U ÷ 止损距离）。用户晚入场时成交价会漂，同一批张数的
    止损距离与实际风险就都变了：

    - 做空晚入场（价格已涨）→ 止损更近、风险变小、更容易被噪音扫掉；
    - 做多晚入场（价格已跌）→ 止损更远、**实际风险超过目标**。

    所以必须在成交后回算一次并把偏差讲清楚，而不是让用户以为
    「填了提醒里的金额 = 风险就是 2%」。

    `equity`：这行的**下单基数**（positions.equity_at_entry；HL 仓由执行器写回
    实际生效基数，MEXC 行是建行时的配置值）。不传则退回 `cfg.equity`（行为不变）。

    `side`：这行的方向（+1/-1）。传了才会按「空单风险减半」折算目标风险 ——
    否则体检会把一笔**本来就该只有一半风险**的空单判成「偏离目标 -50%」，还会
    建议你「要正好 20U 就该开双倍张数」，那正是这个开关要防的事。
    不传（老脚本/测试）按多单口径 ⇒ 不折算 ⇒ 与改动前完全一致。
    """
    contract_size = CONTRACT_SPECS[symbol][0]
    m = sizing_metrics(symbol, entry, stop, contracts, cfg.leverage)
    base = float(equity) if equity else cfg.equity
    target = base * effective_risk_pct(cfg.risk_pct, 1 if side is None else side,
                                       getattr(cfg, "short_risk_half", False))
    drift = (entry - suggested_entry) / suggested_entry if suggested_entry else 0.0
    notes: List[str] = []

    if abs(drift) >= 0.005:
        notes.append(
            f"成交价 {entry:,.2f} 比信号价 {suggested_entry:,.2f} "
            f"{'高' if drift > 0 else '低'} {abs(drift) * 100:.2f}%"
            f"（信号价是提醒那一刻的参考价，偏差来自延迟入场）")

    if not (cfg.stop_min - 1e-9 <= m["stop_dist"] <= cfg.stop_max + 1e-9):
        notes.append(
            f"止损距离 {m['stop_dist'] * 100:.2f}% 已超出 {cfg.stop_min * 100:g}%~"
            f"{cfg.stop_max * 100:g}% 的验证区间 —— 这笔的风险几何与回测口径不一致")

    dev = (m["risk_u"] - target) / target if target else 0.0
    if abs(dev) >= 0.2:
        notes.append(
            f"实际风险 {m['risk_u']:.1f}U（基数 {base:,.0f}U 的 "
            f"{m['risk_u'] / base * 100:.1f}%）偏离目标 {target:.0f}U {dev * 100:+.0f}%")
        if m["risk_unit"] > 0:
            want = int(target / (m["risk_unit"] * contract_size))
            cap = int((target / cfg.stop_min) / entry / contract_size) if entry > 0 else 0
            extra = (f"；止损距离低于 {cfg.stop_min * 100:g}% 下限，"
                     f"系统名义上限（风险U/下限）只能到 {cap} 张，超了就是隐性加杠杆"
                     if want > cap else "")
            notes.append(f"若要正好 {target:.0f}U 风险应开 {want} 张（本次 {contracts} 张）{extra}")

    if source == "amount" and suggested_contracts and contracts < suggested_contracts * 0.6:
        notes.append(
            f"按金额换算出的 {contracts} 张明显少于建议 {suggested_contracts} 张 —— 核对一下"
            f"填的是不是成交记录里的『成交金额（仓位价值）』；保证金 × {cfg.leverage:.0f} 才是名义")

    return {"level": "warn" if notes else "ok", "notes": notes, "contracts": contracts,
            "source": source, "entry": entry, "stop": stop, "drift": drift,
            "target_risk_u": target, "equity": base, **m}


def regime_note(side: int, price: float, ma200: float) -> dict:
    """确认页的 MA200 提示（短文案）。

    做空有趋势过滤（信号 bar 收盘 < MA200）：若成交前价格已回到 MA200 上方，
    过滤条件事实上已失效 —— 历史统计这类做空均R −0.26（n=44）、胜率 25%。
    做多没有这条过滤，只在价格跌破 MA200 时提示「质量偏弱」（历史仍为正期望）。
    """
    if ma200 <= 0:
        return {"level": "muted", "text": ""}
    dev = (price / ma200 - 1) * 100
    if side == -1:
        if price > ma200:
            return {"level": "warn",
                    "text": f"⚠️ 当前价 {price:,.0f} 在 MA200 上方 {dev:+.2f}% —— 做空趋势过滤已失效"}
        return {"level": "ok", "text": f"当前价 {price:,.0f} 在 MA200 下方 {dev:+.2f}%（过滤成立）"}
    if price < ma200:
        return {"level": "warn",
                "text": f"⚠️ 当前价 {price:,.0f} 在 MA200 下方 {dev:+.2f}% —— 做多趋势质量偏弱"}
    return {"level": "ok", "text": f"当前价 {price:,.0f} 在 MA200 上方 {dev:+.2f}%（趋势配合）"}


def format_missed(cfg, sig: strategy.Signal, executor_managed: bool = False) -> str:
    """错过的信号补报。

    托管模式下**不能写「不建议追」这种人工处置建议**：系统本来就不会追补
    （entry_missed 事件不交给执行器），用户能做的只有什么都不做 —— 文案据实说。
    人工模式逐字符保持原样。注意本函数不含 MEXC 张数/名义段（不引用 order）。
    """
    tail = ("该信号已超过一根 4h，入场价已变、风险几何失效，系统不会自动追补。仅作记录。"
            if executor_managed
            else "该信号已超过一根 4h，入场价已变、风险几何失效，不建议追。仅作记录。")
    return "\n".join([
        f"{cfg.alert_prefix}⚠️ 错过的信号（补报）",
        f"{sig.symbol} 在 {fmt_bar_close(sig.bar_open_ms)} {TZ_LABEL}的 4h 收盘触发{sig.action}"
        f"（当时入场 {fmt_money(sig.entry_price)}，止损 {fmt_money(sig.stop_price)}）",
        tail,
    ])


def format_exit(cfg, pos: PositionState, exit_price: float, reason: str, bar_ms: int,
                position_id: int, stale: bool = False, bars_held: int = 0) -> str:
    entry, stop = pos.eff_entry, pos.eff_stop
    risk = abs(entry - stop)
    r = (exit_price - entry) / risk if pos.side == 1 else (entry - exit_price) / risk
    r = r if risk > 0 else 0.0
    ma_note = "4h 收盘跌破 SMA20" if pos.side == 1 else "4h 收盘站上 SMA20"
    why = {"stop": "触发止损单", "ma20": ma_note, "manual": "手动平仓",
           "exchange": "交易所侧已平仓（人工/外部）"}.get(reason, reason)
    # 止损是**交易所已成交**的事件，不能再说「4 小时内市价平仓」——照做等于反手开新仓。
    if reason == "stop":
        head = "止损补报" if stale else "止损提醒"
        advice = (f"止损单应已在交易所触发（{pos.close_side}）：到 MEXC 核对持仓已平、无残留委托；"
                  f"实际成交价与止损价不同就在确认页填真实平仓价")
    else:
        head = "⚠️ 补报·若还没平仓请立即市价平仓" if stale else "离场提醒"
        advice = f"建议：4 小时内市价平仓（{pos.close_side}）；平仓后确认委托列表已空（撤掉残留止损单）"
    return "\n".join([
        f"{cfg.alert_prefix}{pos.symbol} {pos.action_label} {head}（{fmt_bar_close(bar_ms)} {TZ_LABEL}收盘）",
        f"原因：{why}",
        f"参考离场价：{fmt_money(exit_price)}（本次约 {r:+.2f}R，持仓约 {bars_held * 4} 小时）",
        advice,
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
                # 空单风险减半的**引擎侧生效点**（提醒 / dry 影子卡 / 待确认行都用这里算出来的
                # 张数）：只折算风险比例，止损价与止损距离原样传下去。
                # getattr 兜底 False：没有该属性的 cfg 替身（老测试/脚本）退回改动前行为。
                order = size_order(symbol, sig.entry_price, sig.stop_price, cfg.equity,
                                   effective_risk_pct(cfg.risk_pct, sig.side,
                                                      getattr(cfg, "short_risk_half", False)),
                                   cfg.leverage, side=sig.side)
                # order 为 None = 场所规格缺失（该标的未登记 / 无 MEXC 段）：
                # **照常产出信号事件**（提醒由 format_entry 降级出文案），只是没有张数建议。
                events.append(Event(kind=("entry" if is_latest else "entry_missed"), symbol=symbol,
                                    bar_ms=sig.bar_open_ms, is_latest=is_latest, sig=sig, order=order))
                if is_latest:
                    position = PositionState(symbol=symbol, entry_price=sig.entry_price,
                                             stop_price=sig.stop_price,
                                             contracts=(order.contracts if order is not None else 0),
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


# ------------------------------------------------- 面板缓存（symbol_state）
# 「三标的动态」区块的数据源：每根 tick 把每标的的现价/触发线/密集根数 upsert 进
# symbol_state，面板只读库。**纯展示层，不参与任何信号或执行决策。**

def _dense_run(band: Sequence[Optional[float]], cfg, i: int) -> int:
    """以第 i 根结尾的「连续密集」根数（薄封装：判定在 strategy.dense_run，同一份口径）。

    面板要的是「还差几根」，signal 要的是「连续 ≥ cfg.min_squeeze_bars 根」；两边都从
    strategy 的 `squeeze_flags(min_bars=1)` 派生，不复制窗口/分位常数。
    """
    return strategy.dense_run(band, i, cfg.band_window, cfg.band_quantile)


def symbol_snapshot(cfg, bars: Sequence[strategy.Bar], ind: Dict[str, List[Optional[float]]],
                    latest_closed_i: int, has_position: bool = False) -> Dict[str, Optional[float]]:
    """面板区块要的一行快照（纯计算、无 I/O）。

    口径（全部复用 strategy 现成的序列，不重新实现一套常数）：
    - price = 最新一根**未收盘** bar 的 close（与信号同一份 hl-candle，市场层已给）
    - sma20 = **最后一根已收盘 bar** 的 SMA20 —— 离场判定看的就是这条线
    - sma200 = 同一根已收盘 bar 的 SMA200 —— 做空（③）的趋势过滤线，面板琥珀要用
      （持仓标的也照存，无副作用）
    - long_trigger / short_trigger = 最后一根已收盘 bar 的 6均线 max/min（上轨/下轨）；
      下一次入场判定（evaluate_entry 在第 i 根用的正是 ind[i-1]）比的就是这两个值
    - dense_cnt = 以最后一根已收盘 bar 结尾的连续密集根数（库里存真实值，展示封顶 6/6）
    `has_position=True`（有 status='open' 仓位）：walk 不评估入场，触发线与密集根数
    没有意义 → 留 NULL，面板遇到 NULL 会略去对应子行。
    """
    price: Optional[float] = float(bars[-1].close) if bars else None
    sma20: Optional[float] = None
    sma_seq = ind.get("sma20") or []
    cur = sma_seq[latest_closed_i] if 0 <= latest_closed_i < len(sma_seq) else None
    if cur is not None:
        sma20 = float(cur)
    # SMA200 与 SMA20 同源同索引（同一根已收盘 bar）；序列缺失/该位为 None → 留 NULL。
    # 持仓标的也照存（面板 ③ 行不渲染，无副作用；离场/开仓都不读它）。
    sma200: Optional[float] = None
    sma200_seq = ind.get("sma200") or []
    cur200 = sma200_seq[latest_closed_i] if 0 <= latest_closed_i < len(sma200_seq) else None
    if cur200 is not None:
        sma200 = float(cur200)
    snap: Dict[str, Optional[float]] = {"price": price, "sma20": sma20, "sma200": sma200,
                                       "long_trigger": None, "short_trigger": None,
                                       "dense_cnt": None}
    if has_position:
        return snap
    for key, seq in (("long_trigger", ind.get("ma_max") or []),
                     ("short_trigger", ind.get("ma_min") or [])):
        cur = seq[latest_closed_i] if 0 <= latest_closed_i < len(seq) else None
        if cur is not None:
            snap[key] = float(cur)
    snap["dense_cnt"] = _dense_run(ind.get("band") or [], cfg, latest_closed_i)
    return snap


def _record_symbol_state(db, cfg, symbol: str, bars: Sequence[strategy.Bar],
                         ind: Dict[str, List[Optional[float]]], latest_closed_i: int,
                         has_position: bool) -> None:
    """把本标的的快照 upsert 进 symbol_state（面板「三标的动态」读它）。

    只在 walk 走通（或被 pending 挡下，压根没走到 walk）之后调用 —— walk 抛异常时
    不写。调用点排在 set_last_bar 之后：即使这里失败也**不影响 bar 进度与信号推送**，
    异常照旧由 tick 的 per-symbol try/except 收成一条 tick_error 告警。
    """
    db.set_symbol_state(symbol, **symbol_snapshot(cfg, bars, ind, latest_closed_i,
                                                 has_position=has_position))


def tick(db, cfg, notifier, now_ms: Optional[int] = None, executor=None) -> List[str]:
    """一次检查：取数 → 校验 → 判离场/入场 → 推送 → 落库。返回已发送的文案。

    `executor`（app.hl.executor.Executor 或 None）：给了就由它接管开仓/离场
    （真实下单），引擎不再发人工操作卡；没给就是原来的纯提醒模式。
    """
    sent: List[str] = []
    if executor is not None:
        try:
            executor.run()
        except Exception as e:                                   # noqa: BLE001
            logger.exception("执行器 run() 异常: %s", e)
    for symbol in cfg.symbols:
        try:
            _tick_symbol(db, cfg, notifier, symbol, now_ms, executor, sent)
        except market.FeedError as e:
            db.mark_feed(symbol, "", False, str(e))
            _emit(db, notifier, cfg, Alert("data_error", symbol, None,
                                           format_data_error(cfg, symbol, str(e))), sent)
        except Exception as e:                                   # noqa: BLE001
            # 单个标的的任何异常（未知 symbol、非法配置、DB 故障…）都不能
            # 打断其余标的 —— 旧版一个 KeyError 就让所有标的的 tick 静默停摆。
            # 审计见：这里旧代码把异常压成一句「处理异常」data_error 提醒就过去了，
            # 规格漏配这类**配置错误**因此长期不可见；现改为显性告警（带异常类型、
            # 落 alert_logs、按 (symbol, 异常类型) 2h 窗口去重，避免每 30 分钟刷屏）。
            logger.exception("[tick] %s 处理异常: %s", symbol, e)
            _emit_symbol_error(db, notifier, cfg, symbol, e, sent)
    return sent


# (symbol, 异常类型) → 上次成功推送告警的时间戳；2h 窗口去重，防刷屏。
TICK_ERROR_WINDOW_S = 7200.0
_TICK_ERR_SENT_AT: Dict[Tuple[str, str], float] = {}


def _brief(msg, limit: int = 120) -> str:
    """异常摘要截断：压掉换行/多余空白，超长截断加省略号。"""
    s = " ".join(str(msg).split())
    return s if len(s) <= limit else s[:limit] + "…"


def _emit_symbol_error(db, notifier, cfg, symbol: str, exc: BaseException,
                       sent: List[str]) -> None:
    """per-symbol 异常 → 显性告警（推送 + 落 alert_logs），按 (symbol, 异常类型) 去重。

    与 data_error 同一套「窗口去重」思路，但窗口键是 (symbol, 异常类型)：同一标的换成
    另一类异常会照常告警，同类异常 2h 内只报一次。dry_run 只打印、不推送不落库（与
    _emit 一致）。
    """
    etype = type(exc).__name__
    key = (symbol, etype)
    now = time.time()
    if now - _TICK_ERR_SENT_AT.get(key, 0.0) < TICK_ERROR_WINDOW_S:
        logger.info("[tick] %s %s 告警窗口内去重，跳过推送", symbol, etype)
        return
    text = f"{cfg.alert_prefix}⚠️ {symbol} 处理失败：{etype}: {_brief(exc)}"
    if getattr(cfg, "dry_run", False):
        print(f"\n----- DRY-RUN tick_error {symbol} -----")
        print(text)
        sent.append(text)
        return
    try:
        ok = bool(notifier and notifier.send_message(text))
    except Exception:                                            # noqa: BLE001
        logger.exception("[tick] 异常告警发送失败: %s", symbol)
        return
    db.log_alert(symbol, None, "tick_error", text, ok)
    if ok:
        _TICK_ERR_SENT_AT[key] = now
        sent.append(text)
    else:
        logger.error("[tick] %s 处理失败告警推送失败（下次重试）", symbol)


def _tick_symbol(db, cfg, notifier, symbol: str, now_ms, executor, sent: List[str]) -> None:
        feed = market.fetch(symbol, now_ms)
        # 成功路径也写 feed_health：心跳/面板的「数据源健康」靠 last_ok_at 判断，
        # 只记失败的话 last_ok_at 永远停在首次成功之前（P1 回归，已修）。
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
        # 仓位行提前读（只读）：下面两条「本轮没有新 bar 要处理」的早退路径也要写面板
        # 状态 —— 已收盘 bar 每 4h 才换一根，只在 walk 之后写会让面板时间戳 4h 才动一次，
        # 区块头写的却是「每 30 分钟随信号 tick 更新」。
        row = db.open_position(symbol)
        has_open = row is not None and row["status"] == "open"
        if from_i > latest_closed_i:
            db.set_last_bar(symbol, bars[latest_closed_i].open_ms)
            _record_symbol_state(db, cfg, symbol, bars, ind, latest_closed_i, has_open)
            return

        # pending（用户还没确认）不算真仓：不对它判离场（否则从未开过的仓会被
        # 自动关账、写 r_multiple 污染统计）；但超过 PENDING_BARS_MAX 根 4h 未处理
        # 就作废释放槽位，否则一次漏看提醒会永久堵死该标的的新信号。
        if row is not None and row["status"] == "pending":
            ref_now = int(now_ms if now_ms is not None else time.time() * 1000)
            age_bars = (ref_now - int(row["signal_bar_ms"])) // INTERVAL_MS
            if age_bars >= PENDING_BARS_MAX:
                db.ignore_position(int(row["id"]))
                # 注意 age_bars 从信号 bar 开盘起算：提醒发出时已是 1（bar 收盘后），
                # 所以用户体感的确认窗口 ≈ (PENDING_BARS_MAX-1) 根 4h，文案如实写。
                _emit(db, notifier, cfg, Alert(
                    "exit", symbol, int(row["signal_bar_ms"]),
                    (f"{cfg.alert_prefix}{symbol} 待确认仓位已作废：信号发出约 "
                     f"{(PENDING_BARS_MAX - 1) * 4} 小时未确认，槽位已释放。"
                     f"若你实际开了仓，请自行管理离场。")),
                    sent)
                row = None
            else:
                # pending 仍在确认窗口内 → 它占住该标的唯一的仓位槽：
                # 不评估新入场（否则连续突破会叠出第二个 pending、旧行成幽灵）。
                # 审计三轮 M12：不再推进 bar 进度 —— 以前期间每次 tick 都 set_last_bar
                # 往前走，用户晚确认后 from_i 已越过入场根与中间根，这些根的止损
                # 判定被永久跳过。留在信号 bar，确认后从入场根起重扫（幂等键
                # was_alerted 保证已推过的离场提醒不会重复推）。
                # 面板状态照常刷新（pending 不是仓位 → 触发线/密集根数照常算）。
                _record_symbol_state(db, cfg, symbol, bars, ind, latest_closed_i, False)
                return
        position = _row_to_state(row) if row is not None and row["status"] != "pending" else None
        events, new_pos = walk(cfg, symbol, bars, ind, sq, position, from_i, latest_closed_i,
                               latest_closed_i)

        for ev in events:
            if ev.kind == "exit":
                handled = False
                exec_exc = False
                if executor is not None and row is not None:
                    try:
                        handled = executor.on_exit(row["id"], ev.reason, ev.exit_price)
                    except Exception as e:                       # noqa: BLE001
                        logger.exception("执行器 on_exit 异常: %s", e)
                        # 审计三轮 M13：on_exit 可能已在交易所平掉仓位后才抛异常。
                        # 此时再按参考价把本地关成 auto_closed + 推人工离场卡，会与
                        # 真实状态冲突（用户可能照卡片再去平一次=反手开新仓）。
                        # 异常路径只推告警，不动本地账，留给下一轮 tick/对账收敛。
                        exec_exc = True
                        _emit(db, notifier, cfg, Alert(
                            "data_error", symbol, None,
                            f"{cfg.alert_prefix}{symbol} 执行器离场时异常（{type(e).__name__}），"
                            f"本轮未关账未发操作卡 —— 下一轮会重新评估，请留意后续提醒。"), sent)
                if handled or exec_exc:
                    # 执行器已经平仓并推送了成交回报 —— 不再发人工操作卡。
                    # exec_exc 同样跳过本地关账（见上），避免重复关账/误导卡。
                    row = None
                    position = None
                    continue
                if row is not None:
                    db.close_position(row["id"], ev.exit_price, ev.reason, auto=True)
                    text = format_exit(cfg, position or _row_to_state(row), ev.exit_price, ev.reason,
                                       ev.bar_ms, row["id"], stale=not ev.is_latest,
                                       bars_held=ev.bars_held)
                else:
                    text = f"{cfg.alert_prefix}{ev.symbol} 离场信号（无对应持仓记录）"
                _emit(db, notifier, cfg, Alert("exit", symbol, ev.bar_ms, text), sent)
                row = None
                position = None
            elif ev.kind == "entry":
                # ev.order 为 None = 场所规格缺失：仓位行仍建（信号照常记录/提醒），
                # 只是量字段留 0；张数建议由 format_entry 降级为「已跳过张数建议」。
                o = ev.order
                pid = db.create_position(
                    symbol=symbol, signal_bar_ms=ev.bar_ms, side=ev.sig.side,
                    entry_price=ev.sig.entry_price, stop_price=ev.sig.stop_price,
                    stop_dist=ev.sig.stop_dist,
                    contracts=(o.contracts if o is not None else 0),
                    coin_qty=(o.coin_qty if o is not None else 0.0),
                    notional=(o.notional if o is not None else 0.0),
                    risk_u=(o.risk_u if o is not None else 0.0),
                    equity_at_entry=cfg.equity)
                handled = False
                if executor is not None:
                    try:
                        handled = executor.on_entry(pid)
                    except Exception as e:                       # noqa: BLE001
                        logger.exception("执行器 on_entry 异常: %s", e)
                if handled:
                    position = new_pos
                    continue
                text = format_entry(cfg, ev.sig, ev.order, pid,
                                    executor_managed=(executor is not None))
                _emit(db, notifier, cfg, Alert("entry", symbol, ev.bar_ms, text), sent)
                position = new_pos
            else:                                     # entry_missed
                _emit(db, notifier, cfg, Alert("entry_missed", symbol, ev.bar_ms,
                                               format_missed(cfg, ev.sig,
                                                             executor_managed=(executor is not None))), sent)
        db.set_last_bar(symbol, bars[latest_closed_i].open_ms)
        # 面板状态（排在 set_last_bar 之后：写失败不影响 bar 进度与信号推送）。
        # has_position 取 walk 走完之后的真实状态：本轮刚离场 → False（照常给两条入场线）。
        _record_symbol_state(db, cfg, symbol, bars, ind, latest_closed_i,
                             has_position=(new_pos is not None))


def _emit(db, notifier, cfg, alert: Alert, sent: List[str]) -> None:
    """幂等 + 成功后标记；dry_run 只打印不发送。

    bar_ms 为空的提醒（数据源故障等）用 2 小时时间窗去重 —— 没有时间窗的话
    NULL 键会匹配任意历史成功行，同一故障第二次起永远静默。
    """
    within = 7200.0 if alert.bar_ms is None else None
    if db.was_alerted(alert.symbol, alert.bar_ms, alert.kind, within_s=within):
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
    lines = [f"{cfg.alert_prefix}心跳 {dt.datetime.now(DISPLAY_TZ).strftime('%m-%d %H:%M')} {TZ_LABEL}"]
    for r in db.feed_status():
        # 故障判定看「最近一次失败是否晚于最近一次成功」——只看 last_ok_at 的话，
        # 出过一次错的源永远显示 ok（mark_feed 失败路径不清 last_ok_at）。
        ok_at = float(r["last_ok_at"] or 0)
        err_at = float(r["error_at"] or 0)
        healthy = ok_at > 0 and ok_at >= err_at
        ok = f"ok（{r['source']}）" if healthy else "❌ 失败"
        extra = f" 最近错误: {r['last_error']}" if r["last_error"] and not healthy else ""
        lines.append(f"数据源 {r['symbol']}: {ok}{extra}")
    for symbol in cfg.symbols:
        row = db.open_position(symbol)
        if row:
            st = "待确认" if row["status"] == "pending" else "持仓中"
            # 双口径：HL 仓的 contracts 是 MEXC 人工口径张数，真实数量是 actual_*（币量）
            qty = posfields.qty(row)
            lines.append(f"{symbol}: {st} {qty:g} {posfields.qty_unit(row)} @ "
                         f"{fmt_money(posfields.entry_px(row) or 0.0)}，"
                         f"止损 {fmt_money(posfields.stop_px(row) or 0.0)}")
    if not any(db.open_position(s) for s in cfg.symbols):
        lines.append("当前无持仓")
    recent = db.recent_alerts(limit=200)
    day = [a for a in recent if a["created_at"] > __import__("time").time() - 86400]
    failed = [a for a in day if not a["sent_successfully"]]
    lines.append(f"近 24h 提醒 {len(day)} 条，其中失败 {len(failed)} 条")
    return "\n".join(lines)


def _disp_symbol(sym) -> str:
    """BTC_USDT → BTC（周报只说币名，交易所在哪不影响读者理解）。"""
    s = str(sym or "").strip()
    for suf in ("_USDT", "_USDC", "_USD"):
        if s.endswith(suf):
            return s[: -len(suf)]
    return s


def _side_word(side) -> str:
    """方向白话：+1 做多 / -1 做空。"""
    try:
        return "做多" if int(side or 1) > 0 else "做空"
    except (TypeError, ValueError):
        return "做多"


def _sgn(x: float, nd: int = 2, comma: bool = False) -> str:
    """带符号数字：+1.23 / −1.23（负数用排版减号 U+2212，微信里比 ASCII 横杠清楚）。"""
    body = f"{abs(float(x)):,.{nd}f}" if comma else f"{abs(float(x)):.{nd}f}"
    return ("+" if float(x) >= 0 else "−") + body


def _circle(i: int) -> str:
    """①…⑳；超过 20 笔退化成 21. 22. —— 圈码只到 20。"""
    return chr(0x2460 + i - 1) if 1 <= i <= 20 else f"{i}."


def _exit_reason_plain(reason, side) -> str:
    """离场原因 → 一句人话。未知原因原样保留（绝不翻译成看着像结论的词）。"""
    r = str(reason or "").strip()
    if r == "ma20":
        # 多头是收盘跌破均线离场、空头是收盘站上均线离场：方向不同说法必须不同，
        # 否则空单写「收下均线」等于把离场理由说反。
        return "价格收上均线，正常离场" if _side_word(side) == "做空" else "价格收下均线，正常离场"
    if r == "stop":
        return "触发止损"
    if r == "exchange":
        # 交易所侧已被平掉（人工/外部端下单），不是策略离场也不是止损 —— 说清楚，
        # 免得周报/面板把它读成「正常离场」。
        return "交易所侧已平仓（人工/外部）"
    if r in ("agent_expiry_cleanup", "expiry_cleanup"):
        return "API 到期保护清仓"
    return r or "原因未记录"


def _exec_mode(db) -> str:
    """执行器模式：DB（面板写入）> env > 未知。读不到就写「未知」，不猜成 off。"""
    raw = ""
    try:
        raw = str(db.get_config_str("hl_exec_mode", "") or "").strip().strip('"')
    except Exception:                                               # noqa: BLE001
        raw = ""
    if not raw:
        raw = (os.environ.get("HL_EXEC_MODE") or "").strip()
    # 只认三种合法模式；其它（含空）统统写「未知」——把脏值原样印出去等于替
    # 用户断言一个不存在的执行状态。
    return raw if raw in ("off", "dry", "live") else "未知"


def _feed_state(db) -> str:
    """信号源健康度：与心跳同口径 —— 最近一次失败晚于最近一次成功即异常。

    没有任何健康行时写「未知」（不能默认正常：一次都没成功的源也不等于正常）。
    """
    try:
        rows = list(db.feed_status() or [])
    except Exception:                                               # noqa: BLE001
        return "未知"
    if not rows:
        return "未知"
    bad = []
    for r in rows:
        ok_at = float(r["last_ok_at"] or 0)
        err_at = float(r["error_at"] or 0)
        if not (ok_at > 0 and ok_at >= err_at):
            bad.append(_disp_symbol(r["symbol"]))
    if not bad:
        return "正常"
    return "异常（" + "、".join(bad) + "）"


def _equity_window(db) -> Optional[Tuple[float, float]]:
    """窗口首条 / 最新一条 hl_snapshots 的账户净值（equity 列）。任一端取不到返回 None。

    「最新一条」就是库里最后那行 —— 它的 equity 是 0/NULL 时按取不到处理，
    不回退到更早的快照冒充本周净值（那等于拿旧数当新数）。
    """
    import time as _t
    first = None
    first_id = None
    try:
        snaps = list(db.hl_snapshots(_t.time() - WEEKLY_WINDOW_S) or [])
    except Exception:                                               # noqa: BLE001
        snaps = []
    for r in snaps:
        try:
            v = float(r["equity"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if v > 0:
            first = v
            first_id = posfields.field(r, "id")
            break
    last = None
    last_id = None
    try:
        row = db.hl_latest_snapshot()
        if row is not None:
            v = float(row["equity"])
            if v > 0:
                last, last_id = v, posfields.field(row, "id")
    except Exception:                                               # noqa: BLE001
        last = None
    if first is None or last is None:
        return None
    # 首尾是同一行 = 窗口里只有一条快照：算不出「本周变化」，按取不到处理
    # （否则会把 0 变化写成「本周 ±0.0U」，读者会误以为账户这周纹丝不动）。
    if first_id is not None and first_id == last_id:
        return None
    return first, last


def weekly_text(db, cfg) -> str:
    """周报：过去 7 天（北京时间）的平仓、账户净值、逐笔与当前状态。

    读者是拿着真金白银跟单的人，所以三条硬约束：
    1. 数字只从库里取 —— 净值取不到就写「本轮取不到净值，下轮补」，绝不拿配置
       本金冒充真实账户；
    2. 一个数字只出现一次（净值差只在概览），逐笔段不重复合计；
    3. 只写人话：不复利推演、不写「永续口径/抵押品」这类黑话。
    """
    import time as _t
    now_ts = _t.time()
    since = now_ts - WEEKLY_WINDOW_S
    start_d = dt.datetime.fromtimestamp(since, DISPLAY_TZ).date()
    end_d = dt.datetime.fromtimestamp(now_ts, DISPLAY_TZ).date()

    week_rows = list(db.closed_positions(since) or [])
    all_rows = list(db.closed_positions(0.0) or [])
    try:
        recent = list(db.recent_positions(200) or [])
    except Exception:                                               # noqa: BLE001
        recent = []
    pending_rows = [r for r in recent if r["status"] == "pending"]
    # 当前持仓 = 真开出来的仓（open）。pending 单列在那行 ⚠️ 提醒里 —— 塞进持仓
    # 数里会让「持仓 2」里混着一笔还没下单的信号，与下面「待确认」行重复计数。
    open_rows = [r for r in recent if r["status"] == "open"]
    # 新开 = 窗口内**真开出来的仓**（open/closed）。pending（待确认）与 ignored
    # （用户点了忽略/超时作废）都没真进场，两处都算会让概览比实际多开一倍。
    new_cnt = 0
    for r in recent:
        if r["status"] not in ("open", "closed"):
            continue
        try:
            if float(r["created_at"] or 0) >= since:
                new_cnt += 1
        except (KeyError, IndexError, TypeError, ValueError):
            continue

    lines = [f"{cfg.alert_prefix}周报 {start_d.strftime('%m-%d')}～{end_d.strftime('%m-%d')}"
             f"（窗口=过去 7 天，{TZ_LABEL}）"]

    # ---------------- 概览
    lines.append("▍本周概览")
    if week_rows:
        by_kd: Dict[Tuple[str, str], int] = {}
        for r in week_rows:
            key = (_disp_symbol(r["symbol"]), _side_word(r["side"]))
            by_kd[key] = by_kd.get(key, 0) + 1
        kd_txt = "、".join(f"{k[0]} {k[1]}×{v}" if v > 1 else f"{k[0]} {k[1]}"
                           for k, v in sorted(by_kd.items()))
        by_why: Dict[str, int] = {}
        for r in week_rows:
            why = _exit_reason_plain(r["exit_reason"], r["side"])
            by_why[why] = by_why.get(why, 0) + 1
        why_txt = "、".join(f"{k} {v} 笔" for k, v in by_why.items())
        lines.append(f"平仓 {len(week_rows)} 笔（{kd_txt}；离场原因：{why_txt}）"
                     f"· 新开 {new_cnt} 笔 · 当前持仓 {len(open_rows)}")
    else:
        lines.append(f"本周无平仓 · 当前持仓 {len(open_rows)}")

    if pending_rows:
        lines.append(f"⚠️ 有 {len(pending_rows)} 笔信号待确认，本周统计暂按假设值")

    eq = _equity_window(db)
    if eq is None:
        lines.append("账户净值：（本轮取不到净值，下轮补）")
    else:
        a, b = eq
        delta = b - a
        pct = (delta / a * 100.0) if a else 0.0
        lines.append(f"账户净值：{a:,.1f} → {b:,.1f} U"
                     f"（本周 {_sgn(delta, 1, comma=True)} U，约 {_sgn(pct, 1)}%）")

    # ---------------- 逐笔
    if week_rows:
        risk_txt = f"{float(cfg.risk_pct) * 100:.2f}%"
        lines.append("")
        lines.append(f"▍本周逐笔（1R = 这笔交易计划的亏损额，即开仓时净值的 {risk_txt}）")
        for i, r in enumerate(week_rows, 1):
            entry = posfields.entry_px(r)
            if entry is None:
                entry = r["entry_price"]
            exit_px = posfields.field(r, "exit_price")
            closed_at = float(r["closed_at"] or 0)
            day = dt.datetime.fromtimestamp(closed_at, DISPLAY_TZ).strftime("%m-%d")
            rmult = float(r["r_multiple"] or 0.0)
            lines.append(
                f"{_circle(i)} {day} {_disp_symbol(r['symbol'])} {_side_word(r['side'])} "
                f"{fmt_money(entry)} → {fmt_money(exit_px or 0)}　{_sgn(rmult, 2)}R　"
                f"{_exit_reason_plain(r['exit_reason'], r['side'])}")

    # ---------------- 上线以来
    lines.append("")
    lines.append(f"▍上线以来（{WEEKLY_LAUNCH_DATE.strftime('%m-%d')} 起）")
    if all_rows:
        all_r = [float(r["r_multiple"] or 0.0) for r in all_rows]
        wins = sum(1 for x in all_r if x > 0)
        losses = sum(1 for x in all_r if x < 0)
        lines.append(f"共 {len(all_r)} 笔：胜 {wins} · 负 {losses}，"
                     f"合计 {_sgn(sum(all_r), 2)}R")
    else:
        lines.append("共 0 笔：还没有已平仓的交易")

    week_r = [float(r["r_multiple"] or 0.0) for r in week_rows]
    if len(week_r) >= 2 and all(x < 0 for x in week_r):
        n = len(week_r)
        probs = "、".join(f"{0.65 ** k * 100:.1f}%（{k} 笔）" for k in (n, n + 1, n + 2))
        lines.append(f"本策略历史胜率约 35%，靠少数大赢单覆盖多数小亏单；"
                     f"起步连亏的概率约 {probs}，属正常波动，不是策略失效。")
    lines.append("按既定规则：实盘满 20 笔或满 12 个月才做第一次评估，系统会自动报告。")

    # ---------------- 当前状态
    lines.append("")
    lines.append("▍当前状态")
    if open_rows:
        hold = []
        for r in sorted(open_rows, key=lambda x: float(
                posfields.field(x, "opened_at") or x["created_at"] or 0)):
            opened = float(posfields.field(r, "opened_at") or r["created_at"] or 0)
            day = dt.datetime.fromtimestamp(opened, DISPLAY_TZ).strftime("%m-%d") if opened else "日期不详"
            hold.append(f"{_disp_symbol(r['symbol'])} {_side_word(r['side'])}（{day} 开仓）")
        hold_txt = "、".join(hold)
    else:
        hold_txt = "无"
    lines.append(f"持仓：{hold_txt} ｜ 执行器：{_exec_mode(db)} ｜ "
                 f"信号源：{_feed_state(db)}")
    return "\n".join(lines)
