"""标的注册表：**单一元数据来源**（纯 stdlib、import 无副作用）。

背景（血泪）：加一个新标的要同步改四处元数据 —— `market.HL_COIN_NAMES`、
`config` 默认 `SYMBOLS`、`sizing.SPECS`、HL 动态规格。漏任何一处都**静默坏**：
`SPECS` 漏了会让 `engine.walk → size_order(symbol)` 抛 `KeyError`，被 `tick` 的
per-symbol `try/except` 吞成一句「处理异常」，整个标的的信号一个都出不来，
而单测不会红。这里把「系统认识哪些标的、各场所的规格长什么样」收进一张表，
供信号链路（以及后续的行情层 / 执行器）消费，避免再靠人肉同步。

设计约束（下一批任务按此契约消费）：

- **纯 stdlib、无副作用**：import 不读网络 / 文件 / env，可被测试与 CI 安全导入。
- `env SYMBOLS` 仍是**启用清单**（`config.symbols` 不动）；本表只管**元数据**。
  「启用但未注册」属于配置错误，由调用方 fail-loud（本表自身不做启用判定）。
- 未注册（元数据缺失）：`get()` / `hl_coin()` 抛 `SymbolNotConfigured`；
  `mexc_spec()` 返回 `None`（供 `size_order` 优雅降级，不抛）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


class SymbolNotConfigured(Exception):
    """symbol 未在本注册表中登记（元数据缺失，属配置错误，调用方应 fail-loud）。"""


@dataclass(frozen=True)
class MexcSpec:
    """MEXC 合约规格。

    字段语义与旧 `app.sizing.SPECS` 的四元组 `(合约面值 coin/张, 张数步长, 最小张数, 价格精度)`
    一一对齐，便于 `sizing` 无痛迁移（`as_tuple()` 即旧四元组）。
    """

    contract_size: float
    step: int
    min_vol: int
    price_precision: int

    def as_tuple(self) -> Tuple[float, int, int, int]:
        return (self.contract_size, self.step, self.min_vol, self.price_precision)


@dataclass(frozen=True)
class SymbolEntry:
    """一个标的的全部场所元数据（HL 合约名 + MEXC 规格；MEXC 段可为空）。"""

    symbol: str
    hl_coin: str
    mexc: Optional[MexcSpec] = None


# ---------------------------------------------------------------- 注册表本体
# 值从旧 `app.sizing.SPECS` / `market.HL_COIN_NAMES` **原样迁移**，不改任何数值：
#   BTC_USDT → BTC + MEXC(面值 0.0001, 步长 1, 最小 1 张, 价格精度 1)
#   ETH_USDT → ETH + MEXC(面值 0.01,   步长 1, 最小 1 张, 价格精度 2)
#   SOL_USDT → SOL + MEXC(面值 0.1,    步长 1, 最小 1 张, 价格精度 2)
_REGISTRY: Dict[str, SymbolEntry] = {
    "BTC_USDT": SymbolEntry("BTC_USDT", "BTC", MexcSpec(0.0001, 1, 1, 1)),
    "ETH_USDT": SymbolEntry("ETH_USDT", "ETH", MexcSpec(0.01, 1, 1, 2)),
    "SOL_USDT": SymbolEntry("SOL_USDT", "SOL", MexcSpec(0.1, 1, 1, 2)),
}


def known_symbols() -> List[str]:
    """已登记的全部 symbol（顺序稳定，与定义一致）。"""
    return list(_REGISTRY.keys())


def get(symbol: str) -> SymbolEntry:
    """取一条标的元数据；未注册抛 `SymbolNotConfigured`。"""
    entry = _REGISTRY.get(symbol)
    if entry is None:
        raise SymbolNotConfigured(
            f"{symbol}: 未在 symbol_registry 中登记（加标的必须同时登记元数据）")
    return entry


def hl_coin(symbol: str) -> str:
    """symbol → HL 合约名（如 BTC_USDT → BTC）；未注册抛 `SymbolNotConfigured`。"""
    return get(symbol).hl_coin


def mexc_spec(symbol: str) -> Optional[MexcSpec]:
    """取 MEXC 规格。

    - 已注册且有 MEXC 段 → 返回 `MexcSpec`；
    - 已注册但无 MEXC 段 → 返回 `None`；
    - 未注册 → 返回 `None`（不抛）。

    返回 `None` 的语义是「该场所规格缺失，跳过该场所的量建议」，调用方
    （`sizing.size_order`）据此**优雅降级**而不是抛异常 —— 这也是本次改造的
    核心：规格缺失不再把整个标的的信号吞成「处理异常」。
    需要 fail-loud 判断「是否登记过这个标的」请用 `get()` / `hl_coin()`。
    """
    entry = _REGISTRY.get(symbol)
    return entry.mexc if entry is not None else None
