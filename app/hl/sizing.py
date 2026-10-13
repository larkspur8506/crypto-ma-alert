"""Hyperliquid 永续仓位换算与下单参数取整（纯 stdlib，零第三方依赖）。

模块边界：只有「取整规则 + 风险换算」，不发网络、不持私钥。
实际下单在 `app/hl/exchange.py`，那里会调用本模块的 `round_price` / `round_size`
把参数压成交易所能接受的形态，再用 `size_order_hl` 算出 sz。

—— 价格取整（对齐官方 `hyperliquid-python-sdk/examples/rounding.py`）——
- 永续 MAX_DECIMALS = 6：小数位不超过 `6 - szDecimals`
- 最多 5 位有效数字（`f"{px:.5g}"`）
- 价格 > 100000 时直接四舍五入到整数
- **整数价格永远合法**，不受上面两条约束（`max_dp` 被 `max(0, ...)` 兜底）

—— 数量取整 ——
- **向下**取整到步长 `10^-szDecimals`（floor，绝不用 round）
  round 可能向上进位 → 超出可下量 → 交易所拒单；floor 只会少下不会超。
- 用 `+1e-9` 抵消浮点表示误差（如 `0.00013 * 1e5` 落进 12.999999999999998）

—— 风险口径（与 `app/sizing.py` 的 MEXC 版同构）——
    单笔风险U = 权益 × risk_pct
    目标名义  = 单笔风险U ÷ 止损距离
    sz        = 目标名义 ÷ 入场价，再 floor 到 szDecimals
    名义      = sz × 入场价        风险U = sz × |入场 - 止损|
    保证金    = 名义 ÷ 杠杆

注意：`size_order_hl` 里 `entry_price` / `stop_price` 是**信号价原值**，
不做价格取整（与 MEXC 版一致）；发单前由执行器对这两个值各调一次 `round_price`。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

PERP_MAX_DECIMALS = 6          # 永续价格最大小数位
DEFAULT_MIN_NOTIONAL = 10.0    # 永续最小下单名义（USDC）
MAINTENANCE_MARGIN_RATE = 0.005   # 一档维持保证金率近似（算强平价用）

STOP_MIN = 0.015   # 止损距离建议下限（太窄是噪音），与 app/strategy.py 一致
STOP_MAX = 0.10    # 止损距离建议上限（配合逐仓 5x：强平 -19.5% 晚于止损）

_FP_EPS = 1e-9      # 抵消 floor 时的浮点表示误差（相对 1 个步长仍极小）
_REL_TOL = 1e-12    # 最小名义判定的相对容差（FP 误差约 2e-16，这里留 6 个数量级余量）


# ---------------------------------------------------------------- 取整
def round_size(sz: float, sz_decimals: int) -> float:
    """数量向下取整到 `10^-sz_decimals` 步长。

    例：`round_size(0.000129, 5) == 0.00012`（不是 0.00013）。
    """
    if sz < 0:
        raise ValueError("数量不能为负")
    if sz_decimals < 0:
        raise ValueError("sz_decimals 不能为负")
    step = 10.0 ** (-sz_decimals)
    k = math.floor(sz / step + _FP_EPS)   # 抵消 sz/step 的浮点误差（0.00013*1e5→12.999999999999998）
    # 用除法还原而不是 `k * step`：乘幂会漂 1 ulp（12*1e-5 != 0.00012，是两个相邻 double），
    # 除以整数幂才是正确舍入，结果与字面量 `0.00012` 逐位相等。
    return float(k) / (10 ** sz_decimals)


def round_price(px: float, sz_decimals: int, is_perp: bool = True) -> float:
    """价格取整：5 位有效数字 ∧ 小数位 ≤ (6 - szDecimals)，>100000 取整数。

    例：`round_price(1234.56, 5) == 1234.6`（sz_decimals=5 → 最多 1 位小数）
        `round_price(100_001.7, 5) == 100_002.0`（>100k → 整数）
        `round_price(84_000.0, 5) == 84_000.0`（整数价格永远合法）
    """
    if px <= 0:
        raise ValueError("价格必须为正")
    if not is_perp:
        raise ValueError("本模块只支持永续（is_perp=False 未实现）")
    if px > 100_000:
        return float(round(px))
    max_dp = max(0, PERP_MAX_DECIMALS - sz_decimals)   # 整数价格恒合法
    return float(round(float(f"{px:.5g}"), max_dp))


def meets_min_notional(sz: float, price: float,
                       min_notional: float = DEFAULT_MIN_NOTIONAL) -> bool:
    """名义（sz × price）是否达到交易所最小下单金额（默认 10 USDC）。"""
    if sz <= 0 or price <= 0:
        return False
    return sz * price >= min_notional * (1 - _REL_TOL)   # 浮点容差


# ---------------------------------------------------------------- 风险
def liq_price(entry: float, leverage: float, side: int = 1,
              mmr: float = MAINTENANCE_MARGIN_RATE) -> float:
    """逐仓强平价近似。多单 `entry×(1 - 1/lev + mmr)`，空单 `entry×(1 + 1/lev - mmr)`。"""
    if entry <= 0:
        raise ValueError("入场价必须为正")
    if leverage <= 0:
        raise ValueError("杠杆必须为正")
    if side == 1:
        return entry * (1 - 1.0 / leverage + mmr)
    if side == -1:
        return entry * (1 + 1.0 / leverage - mmr)
    raise ValueError("side 只能是 1（多）或 -1（空）")


def max_safe_leverage(stop_dist: float) -> float:
    """清算价必须晚于止损价 → 杠杆上限（`0.5 / stop_dist`，含 50% 安全余量）。"""
    if stop_dist <= 0:
        raise ValueError("止损距离必须为正")
    return 0.5 / stop_dist


def validate_stop_side(side: int, entry: float, stop: float) -> str | None:
    """校验止损方向 vs 入场价，返回中文错误文案；合法时返回 None。"""
    if side == 1:
        if stop >= entry:
            return "做多要求止损价低于入场价"
    elif side == -1:
        if stop <= entry:
            return "做空要求止损价高于入场价"
    else:
        return "side 只能是 1（多）或 -1（空）"
    return None


# ---------------------------------------------------------------- 仓位计划
@dataclass(frozen=True)
class HlOrderPlan:
    """一次开仓的完整参数与风险体检（发单前的唯一真相源）。"""
    coin: str
    side: int
    entry_price: float      # 信号价原值（发单前需另调 round_price）
    stop_price: float       # 信号价原值（发单前需另调 round_price）
    stop_dist: float
    sz: float               # 已 floor 到 szDecimals 的可下数量
    notional: float         # = sz × entry_price
    risk_u: float           # 触发止损的实际亏损 = sz × |entry - stop|
    margin: float           # = notional / leverage
    leverage: float
    max_safe_leverage: float
    liq_price: float
    below_min_notional: bool
    warnings: list[str]


def size_order_hl(coin: str, sz_decimals: int, entry_price: float, stop_price: float,
                  equity: float, risk_pct: float, leverage: float = 5.0, side: int = 1,
                  min_notional: float = DEFAULT_MIN_NOTIONAL) -> HlOrderPlan:
    """按「单笔风险 = 权益 × risk_pct」倒算 Hyperliquid 永续下单数量。

    非法输入 raise ValueError（中文消息）；风险/止损距离越界只进 warnings，不 raise。
    """
    if not isinstance(coin, str) or not coin:
        raise ValueError("coin 必须是非空字符串")
    if entry_price <= 0:
        raise ValueError("入场价必须为正")
    if stop_price <= 0:
        raise ValueError("止损价必须为正")
    if equity <= 0:
        raise ValueError("权益必须为正")
    if risk_pct <= 0:
        raise ValueError("单笔风险比例必须为正")
    if leverage <= 0:
        raise ValueError("杠杆必须为正")
    if sz_decimals < 0:
        raise ValueError("sz_decimals 不能为负")

    err = validate_stop_side(side, entry_price, stop_price)
    if err:
        raise ValueError(err)

    risk_unit = abs(entry_price - stop_price)
    risk_u_target = equity * risk_pct
    stop_dist = risk_unit / entry_price
    target_notional = risk_u_target / stop_dist
    sz = round_size(target_notional / entry_price, sz_decimals)
    notional = sz * entry_price
    risk_u = sz * risk_unit
    margin = notional / leverage
    msl = max_safe_leverage(stop_dist)
    liq = liq_price(entry_price, leverage, side)
    below = not meets_min_notional(sz, entry_price, min_notional)

    warnings: list[str] = []
    if not (STOP_MIN <= stop_dist <= STOP_MAX):
        warnings.append(
            f"止损距离 {stop_dist * 100:.2f}% 超出建议区间 "
            f"{STOP_MIN * 100:.1f}%~{STOP_MAX * 100:.0f}%")
    if leverage > msl:
        warnings.append(
            f"杠杆 {leverage:g}x 超过安全上限 {msl:.2f}x，强平价会早于止损价")
    if below:
        warnings.append(
            f"名义 {notional:.2f} USDC 小于交易所 {min_notional:g} USDC 下限")

    return HlOrderPlan(
        coin=coin, side=side, entry_price=entry_price, stop_price=stop_price,
        stop_dist=stop_dist, sz=sz, notional=notional, risk_u=risk_u,
        margin=margin, leverage=leverage, max_safe_leverage=msl,
        liq_price=liq, below_min_notional=below, warnings=warnings,
    )


__all__ = [
    "PERP_MAX_DECIMALS", "DEFAULT_MIN_NOTIONAL", "MAINTENANCE_MARGIN_RATE",
    "STOP_MIN", "STOP_MAX",
    "round_size", "round_price", "meets_min_notional",
    "liq_price", "max_safe_leverage", "validate_stop_side",
    "HlOrderPlan", "size_order_hl",
]
