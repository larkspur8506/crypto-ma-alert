"""运行时配置：DB configuration 表优先，其次 env，最后代码默认。

DB 列 > env > 代码默认 —— 改代码默认值不会影响线上已有 DB 行；
改 /admin/rules 落 DB 即生效，无需重启。
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

    # 方向：做空必须带趋势过滤（回测：裸做空年化仅 +8%/回撤 36%；加 MA200 过滤后组合年化 +49.6%）
    enable_long: bool = _env("ENABLE_LONG", "true").lower() in ("1", "true", "yes")
    enable_shorts: bool = _env("ENABLE_SHORTS", "true").lower() in ("1", "true", "yes")
    short_regime_ma: int = _env_i("SHORT_REGIME_MA", strategy.REGIME_MA)

    # 资金口径（feat 资金跟随）：follow = 按交易所实时权益算下单基数（带下限、取整到 10U）；
    # fixed = 用固定 equity。加进 OVERRIDABLE 后可从 /admin?tab=params 热改。
    equity_mode: str = _env("HL_EQUITY_MODE", "follow").lower() or "follow"
    equity_min: float = _env_f("HL_EQUITY_MIN", 20.0)

    # 服务
    base_url: str = _env("BASE_URL", "https://ma.222677.xyz")
    admin_token: str = _env("ADMIN_TOKEN")
    token_secret: str = _env("TOKEN_SECRET")
    dry_run: bool = _env("DRY_RUN", "false").lower() in ("1", "true", "yes")
    tz: str = _env("TZ", "UTC")
    # 心跳默认**只落库不推送**（静默信标）：每日巡检已覆盖同样信息，避免重复；
    # 看守/巡检脚本靠 alert_logs 里的心跳行判断调度器存活。设 true 恢复每天推送。
    heartbeat_push: bool = _env("HEARTBEAT_PUSH", "false").lower() in ("1", "true", "yes")

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
    OVERRIDABLE = ("equity", "risk_pct", "leverage", "stop_min", "stop_max",
                   "enable_long", "enable_shorts", "short_regime_ma", "heartbeat_push",
                   "equity_mode", "equity_min")

    # 数值配置的合法范围：apply_db 收敛到这里，超出范围/自相矛盾的组合直接拒收
    # （保留原值）——否则 stop_min>stop_max 会让全部信号静默消失、leverage=0 会让
    # 每轮 tick 抛 ZeroDivisionError，而面板数值看着完全「正常」。
    _RANGES = {
        "risk_pct": (0.0001, 0.5),
        "leverage": (0.5, 125.0),
        "stop_min": (0.0001, 0.5),
        "stop_max": (0.0001, 0.5),
        "equity": (1.0, 100_000_000.0),
        "equity_min": (1.0, 100_000_000.0),
        "short_regime_ma": (0, 1000),
    }

    def apply_db(self, values: dict) -> "Config":
        # 先解析成 dict，统一做范围/一致性校验后再 setattr
        parsed: dict = {}
        for k in self.OVERRIDABLE:
            if k not in values or values[k] in (None, ""):
                continue
            # 去掉 JSON 引号：set_config 走 json.dumps，字符串值会存成 '"100"'，
            # 不剥引号 float() 会抛错 → 被 except 静默吞掉 → 面板改的数字参数永远不生效。
            raw = str(values[k]).strip().strip('"').strip("'").lower()
            cur = getattr(self, k, None)
            try:
                if isinstance(cur, bool):
                    # 严格解析：识别不了就保留原值，绝不静默关闭开关
                    if raw in ("1", "true", "yes", "on"):
                        parsed[k] = True
                    elif raw in ("0", "false", "no", "off"):
                        parsed[k] = False
                    continue
                if isinstance(cur, str):
                    # 字符串型配置（如 equity_mode=follow/fixed）：原样取裸值，
                    # 绝不走 float()（否则标识符会被解析失败后静默忽略）
                    parsed[k] = raw if raw else cur
                    continue
                if isinstance(cur, int):
                    parsed[k] = int(float(raw))
                else:
                    parsed[k] = float(raw)
            except (TypeError, ValueError):
                continue
        # 范围校验：出界的值拒收（保留原值）
        for k, (lo, hi) in self._RANGES.items():
            if k in parsed:
                try:
                    v = float(parsed[k])
                    if not (lo <= v <= hi):
                        parsed.pop(k, None)
                except (TypeError, ValueError):
                    parsed.pop(k, None)
        # 一致性：stop_min 必须小于 stop_max（两者都存在且至少一方本次被改过才判）
        smin, smax = parsed.get("stop_min", getattr(self, "stop_min", None)), \
            parsed.get("stop_max", getattr(self, "stop_max", None))
        try:
            if smin is not None and smax is not None and float(smin) >= float(smax):
                parsed.pop("stop_min", None)
                parsed.pop("stop_max", None)
        except (TypeError, ValueError):
            pass
        for k, v in parsed.items():
            setattr(self, k, v)
        return self
