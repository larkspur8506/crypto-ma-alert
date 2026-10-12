"""以损定仓 + MEXC 合约换算（纯 stdlib）。

MEXC 下单界面「单位选 USDT」时，输入框填的是**仓位价值（名义）**，不是保证金
（官方文档：`USDT—Your position value is calculated in USDT value`；
`Maximum openable USDT = Cont × Size × Price`）。所以：

    输入金额(USDT) = 单笔风险U ÷ 止损距离%

杠杆只影响保证金占用与强平价，不影响仓位大小和风险。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# symbol -> (合约面值, 张数步长, 最小张数, 价格精度)
SPECS = {
    "BTC_USDT": (0.0001, 1, 1, 1),
    "ETH_USDT": (0.01, 1, 1, 2),
}
MAINTENANCE_MARGIN_RATE = 0.005   # 一档维持保证金率近似值（算强平价用）


@dataclass(frozen=True)
class Order:
    symbol: str
    entry_price: float
    stop_price: float
    stop_dist: float
    contracts: int
    coin_qty: float
    notional: float          # = MEXC 输入框该填的 USDT 金额
    margin: float            # 逐仓 5x 下占用保证金
    risk_u: float            # 触发止损时的实际亏损
    max_safe_leverage: float
    below_minimum: bool = False


def liq_price(entry: float, leverage: float, side: int = 1,
              mmr: float = MAINTENANCE_MARGIN_RATE) -> float:
    """逐仓强平价近似（多单）。"""
    if side == 1:
        return entry * (1 - 1.0 / leverage + mmr)
    return entry * (1 + 1.0 / leverage - mmr)


def max_safe_leverage(stop_dist: float, mmr: float = MAINTENANCE_MARGIN_RATE) -> float:
    """清算价必须晚于止损价 → 杠杆上限。留 50% 安全余量。"""
    return 0.5 * 1.0 / max(stop_dist, 1e-9)


def size_order(symbol: str, entry_price: float, stop_price: float, equity: float,
               risk_pct: float, leverage: float = 5.0, side: int = 1) -> Order:
    """按「单笔风险 = 权益 × risk_pct」倒算 MEXC 合约张数（多空同一套公式）。"""
    contract_size, step, min_vol, _ = SPECS[symbol]
    if entry_price <= 0 or equity <= 0 or risk_pct <= 0:
        raise ValueError("entry_price / equity / risk_pct 必须为正")
    risk_unit = (entry_price - stop_price) if side == 1 else (stop_price - entry_price)
    if risk_unit <= 0:
        raise ValueError("做多要求止损低于入场价，做空要求止损高于入场价")

    risk_u = equity * risk_pct
    stop_dist = risk_unit / entry_price
    target_notional = risk_u / stop_dist          # 名义金额 = 风险U ÷ 止损距离

    contracts = int(math.floor((target_notional / entry_price) / contract_size))
    below_min = contracts < min_vol
    if below_min:
        contracts = min_vol                        # 退化为最小可下单张数
    contracts = int(math.floor(contracts / step) * step)

    coin_qty = contracts * contract_size
    notional = coin_qty * entry_price
    return Order(
        symbol=symbol, entry_price=entry_price, stop_price=stop_price, stop_dist=stop_dist,
        contracts=contracts, coin_qty=coin_qty, notional=notional,
        margin=notional / leverage, risk_u=coin_qty * risk_unit,
        max_safe_leverage=max_safe_leverage(stop_dist), below_minimum=below_min,
    )
