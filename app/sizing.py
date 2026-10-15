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
    # SOL_USDT 面值 0.1 SOL/张、价格精度 2（实测 contract.mexc.com/api/v1/contract/detail）。
    # 缺了它 engine.walk → size_order(symbol) 会 KeyError，SOL 信号整轮被 tick 的
    # per-symbol try/except 吞成「处理异常」，所以是加标的的必需项。
    "SOL_USDT": (0.1, 1, 1, 2),
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


def metrics(symbol: str, entry: float, stop: float, contracts: int,
            leverage: float = 5.0) -> dict:
    """按**真实成交**价/张数回算仓位指标（名义、币量、保证金、止损距离、风险U）。

    用于确认开仓后的风险体检：提醒文案里的名义金额是按信号价算的，
    实际成交价漂了之后同一批张数的风险就变了，必须用这个回算。
    """
    contract_size = SPECS[symbol][0]
    coin_qty = contracts * contract_size
    notional = coin_qty * entry
    risk_unit = abs(entry - stop)
    return {"coin_qty": coin_qty, "notional": notional, "margin": notional / leverage,
            "risk_unit": risk_unit, "risk_u": coin_qty * risk_unit,
            "stop_dist": (risk_unit / entry) if entry else 0.0}


def contracts_from_amount(symbol: str, price: float, amount_usdt: float) -> int:
    """把 MEXC 成交记录里的「成交金额(USDT)」+ 成交均价 反算成张数。

    MEXC 合约「单位=USDT」下成交金额就是**仓位价值(名义)** = 张数 × 面值 × 价格，
    所以 `张数 = round(成交金额 ÷ (价格 × 面值))`。

    为什么用 round 而不是 floor：MEXC 下单时自己会对张数向下取整，
    所以你看到的成交金额通常**略小于**下单时填的输入框金额；
    除法回算会落在略小于整数的位置，四舍五入才回得到真实张数
    （拿输入框金额去算是 87.8 → 88 张，拿实际成交金额算是 87.0 → 87 张）。
    """
    contract_size, step, min_vol, _ = SPECS[symbol]
    if price <= 0:
        raise ValueError("成交均价必须为正")
    if amount_usdt <= 0:
        raise ValueError("成交金额必须为正")
    n = int(math.floor(amount_usdt / (price * contract_size) / step + 0.5)) * step
    if n < min_vol:
        raise ValueError(f"按成交金额换算出的张数 {n} 小于最小可下单张数 {min_vol}")
    return n
