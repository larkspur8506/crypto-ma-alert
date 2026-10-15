"""positions 行「双口径」的读侧统一入口。

背景（2026-10-15 双口径收敛）
------------------------------------------------------------------
`positions` 一行里躺着两套量口径：

- **MEXC 人工口径**（`engine` 建行时写）：`contracts`=张数、`coin_qty`、
  `notional`、`risk_u`，以及 `equity_at_entry`=配置 equity（如 1000U）。
  历史 MEXC 行与「提醒→人工下单」流只有这一套，照旧读这些字段。
- **HL 真实口径**（执行器成交后写）：`hl_status` 非空标记这行归 HL 执行器管，
  `actual_entry` / `actual_contracts`（**币量**，不是张数）/ `actual_stop` 是
  真实成交值；`equity_at_entry` 由执行器在成交确认点写回**实际生效基数**。

同一行两套数字会让面板/确认页/心跳把 0.00084 BTC 显示成 85 张、把 100U 的仓
按 1000U 基数解读。**读侧一律先判定这行是不是 HL 仓**：是 → 读 actual_*；
否（历史 MEXC 行）→ 原字段照旧，不做任何迁移。

判定只用行内既有字段 `hl_status`，**不引入任何全局模式标志**：
`hl_status` 只在 `Executor` 成交/落库路径写（open/aborted/closed/mismatch），
按定义就等价于「这行归 HL 执行器管」。不能用 `actual_entry` 判定——MEXC 人工流
的用户确认也会写 `actual_entry`。
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["field", "is_hl", "qty", "qty_label", "qty_unit", "contracts_raw",
           "entry_px", "stop_px", "notional", "risk_u", "equity_base"]


def field(row: Any, name: str, default: Any = None) -> Any:
    """安全取字段值。

    兼容三种入参：`sqlite3.Row`（**没有 `.get`**，取不到的列抛 IndexError）、
    `dict`（KeyError）、以及 `None`（返回 default）。列存在但值为 NULL 时返回
    原值 `None` —— 调用方用 `a or b` 做回退。
    """
    if row is None:
        return default
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f                                    # NaN → None


def is_hl(row: Any) -> bool:
    """这行是不是 HL 执行器管的仓（`hl_status` 非空）。"""
    return bool(field(row, "hl_status"))


def contracts_raw(row: Any) -> Optional[float]:
    """原始 `contracts` —— MEXC 人工口径的「张数」，HL 仓上无意义（仅兼容/审计用）。"""
    return _num(field(row, "contracts"))


def qty(row: Any) -> Optional[float]:
    """真实数量：HL 仓 = 币量（actual_contracts → hl_sz），MEXC 行 = 张数。"""
    if is_hl(row):
        return (_num(field(row, "actual_contracts"))
                or _num(field(row, "hl_sz"))
                or _num(field(row, "contracts")))
    return _num(field(row, "contracts"))


def qty_unit(row: Any) -> str:
    return "币量" if is_hl(row) else "张"


def qty_label(row: Any) -> str:
    """表单/文案后缀（与旧 `张数` 文案衔接）。"""
    return "数量（币量）" if is_hl(row) else "张数"


def entry_px(row: Any) -> Optional[float]:
    """真实入场价（actual 优先），两套口径都这么读（人工流确认也写 actual_entry）。"""
    return _num(field(row, "actual_entry")) or _num(field(row, "entry_price"))


def stop_px(row: Any) -> Optional[float]:
    return _num(field(row, "actual_stop")) or _num(field(row, "stop_price"))


def notional(row: Any) -> Optional[float]:
    """名义：HL 仓 = 实际币量 × 实际入场价；MEXC 行 = 建行时算好的 `notional`。"""
    if is_hl(row):
        q, e = qty(row), entry_px(row)
        if q and e:
            return q * e
    return _num(field(row, "notional"))


def risk_u(row: Any) -> Optional[float]:
    """风险U：HL 仓 = 实际币量 × |实际入场 - 实际止损|；MEXC 行 = `risk_u`。"""
    if is_hl(row):
        q, e, s = qty(row), entry_px(row), stop_px(row)
        if q and e and s:
            return q * abs(e - s)
    return _num(field(row, "risk_u"))


def equity_base(row: Any, fallback: Optional[float] = None) -> Optional[float]:
    """下单基数：HL 仓 = 执行器写回的实际生效基数；MEXC 行 = 建行时的配置 equity。

    字段 `equity_at_entry` 两种情况下都非空，所以这里不做口径判定，直接用
    （HL 仓成交后会被执行器覆盖成实际生效基数）。
    """
    return _num(field(row, "equity_at_entry")) or (None if fallback is None else _num(fallback))
