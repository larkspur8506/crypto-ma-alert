"""运行时配置：env 优先，其次 SQLite configuration 表（用户可改的参数）。

DB 列 > env > 代码默认 —— 改代码默认值不会影响线上已有 DB 行。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from app import strategy


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_f(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_i(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    # 策略（可被 DB 覆盖的放 db_overridable）
    symbols: List[str] = field(default_factory=lambda: [s for s in _env(
        "SYMBOLS", "BTC_USDT,ETH_USDT").split(",") if s])
    risk_pct: float = _env_f("RISK_PCT", 0.02)
    equity: float = _env_f("ACCOUNT_EQUITY", 1000.0)
    leverage: float = _env_f("LEVERAGE", 5.0)
    stop_min: float = _env_f("STOP_MIN", strategy.STOP_MIN)
    stop_max: float = _env_f("STOP_MAX", strategy.STOP_MAX)
    band_window: int = _env_i("BAND_WINDOW", strategy.BAND_WINDOW)
    band_quantile: float = _env_f("BAND_QUANTILE", strategy.BAND_QUANTILE)
    min_squeeze_bars: int = _env_i("MIN_SQUEEZE_BARS", strategy.MIN_SQUEEZE_BARS)

    # 服务
    base_url: str = _env("BASE_URL", "https://ma.222677.xyz")
    admin_token: str = _env("ADMIN_TOKEN")
    token_secret: str = _env("TOKEN_SECRET")
    dry_run: bool = _env("DRY_RUN", "false").lower() in ("1", "true", "yes")
    tz: str = _env("TZ", "UTC")

    # 推送
    wechat_webhook: str = _env("WECHAT_WEBHOOK_URL")
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = _env("TELEGRAM_CHAT_ID")

    # 调度
    tick_minutes: List[int] = field(default_factory=lambda: [3, 63, 123, 183, 243, 303])
    heartbeat_hour: int = _env_i("HEARTBEAT_HOUR", 8)
    weekly_dow: int = _env_i("WEEKLY_DOW", 1)      # 周一
    alert_prefix: str = _env("ALERT_PREFIX", "【双均线】")

    def __post_init__(self) -> None:
        if not self.token_secret:
            self.token_secret = self.admin_token or "dev-secret-change-me"

    # ---- DB 可覆盖项 ----
    OVERRIDABLE = ("equity", "risk_pct", "leverage", "stop_min", "stop_max")

    def apply_db(self, values: dict) -> "Config":
        for k in self.OVERRIDABLE:
            if k in values and values[k] not in (None, ""):
                try:
                    setattr(self, k, float(values[k]))
                except (TypeError, ValueError):
                    continue
        return self
