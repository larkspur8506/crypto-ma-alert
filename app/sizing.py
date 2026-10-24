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
from typing import Optional

from app import symbol_registry as registry

# 兼容 shim：SPECS 的**权威来源已迁到 `app.symbol_registry`**，这里保留同形 dict
# （旧四元组 面值/步长/最小张数/价格精度），供现有调用点（engine.review_fill 的
# CONTRACT_SPECS、确认页换算、测试）零改动读取。新代码请直接用 registry.mexc_spec()。
# 仅收录「登记过且带 MEXC 段」的标的；缺段的标的不再进 SPECS（这正是优雅降级的前提）。
SPECS = {s: spec.as_tuple() for s in registry.known_symbols()
         if (spec := registry.mexc_spec(s)) is not None}
MAINTENANCE_MARGIN_RATE = 0.005   # 一档维持保证金率近似值（算强平价用）

# ---- 空单风险减半（feat/short-risk-half）----
# 0.5 **写死在一处**，不做可调参数：它是风险政策常数（「空单腿只承担一半单笔
# 风险」），不是策略参数。一旦做成可调系数，回测里它就会变成又一个可以往历史
# 数据上拟合的自由度（参数越多越容易把噪音拟合进来），所以它不进参数页、
# 不进 Config._RANGES、也不接受任何数值型 env/DB 覆盖。
SHORT_HALF_SCALE = 0.5


def effective_risk_pct(risk_pct: float, side: int, half: bool = False) -> float:
    """下单风险比例的**唯一生效点**：空单腿在开关打开时按 ``SHORT_HALF_SCALE`` 折算。

    为什么要有这个 helper 而不是在两个 sizing 函数里各写一遍 ``× 0.5``：
    引擎侧（`app/sizing.py::size_order`，提醒/dry 影子卡）和执行器侧
    （`app/hl/sizing.py::size_order_hl`，live 真下单）是**两条路径、两处代码**。
    多一份实现就多一个「改了一处漏了另一处」的口子 —— 那样用户看到的开空卡写着
    1%，实际下的却是 2%，而且两边都能自圆其说，很难被发现。所以两边都只调这里。

    只折算**风险比例**，不碰止损价/止损距离：名义 = 风险U ÷ 止损距离，风险减半
    名义自然减半，保证金跟着减半，强平价更远（不需要专门改杠杆）。
    开关关闭时原值返回（不做 float() 归一），保证与原行为逐字节一致。
    """
    if side == -1 and half:
        return float(risk_pct) * SHORT_HALF_SCALE
    return risk_pct


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
               risk_pct: float, leverage: float = 5.0, side: int = 1) -> Optional[Order]:
    """按「单笔风险 = 权益 × risk_pct」倒算 MEXC 合约张数（多空同一套公式）。

    场所规格缺失时（symbol 未在 registry 登记，或登记了但没有 MEXC 段）**返回 None**，
    不再抛 `KeyError` —— 旧行为下 `engine.walk → size_order` 的 KeyError 会被 tick 的
    per-symbol `try/except` 吞成一句「处理异常」，整个标的的信号一个都出不来。调用方
    （engine.format_entry）遇到 None 应照常出信号、把张数段换成「已跳过张数建议」。

    ``risk_pct`` 是**已经折算过**的生效风险比例：空单减半由调用点先过一遍
    `effective_risk_pct(risk_pct, side, half)`（本函数不自己判开关 —— 判据只允许有
    一处，见该 helper 的说明）。
    """
    spec = registry.mexc_spec(symbol)
    if spec is None:
        return None
    contract_size, step, min_vol, _ = spec.as_tuple()
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
