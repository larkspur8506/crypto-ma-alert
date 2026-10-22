"""运行时配置：DB configuration 表优先，其次 env，最后代码默认。

DB 列 > env > 代码默认 —— 改代码默认值不会影响线上已有 DB 行；
改 /admin/rules 落 DB 即生效，无需重启。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, List

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


def _as_dd_float(v: Any) -> float:
    """回撤阈值的宽松解析：脏值/None → 0.0（= 按档位自动）。

    这里**不抛错**：库里的配置可能被人手工改坏，一个读不出来的阈值不该让整份配置
    装配失败（那样连交易都起不来）；退回 0 = 用档位默认线，保护还在。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f or f in (float("inf"), float("-inf")):
        return 0.0
    return f


def _dd_thresholds(risk_pct: float, warn: float, kill: float):
    """薄封装：延迟 import，避免 config 在模块导入期就拖上 app.hl（循环依赖）。"""
    from app.hl import dd as hl_dd
    return hl_dd.thresholds(risk_pct, warn, kill)


@dataclass
class Config:
    # 策略（可被 DB 覆盖的放 db_overridable）
    # 默认三标的（BTC/ETH/SOL）。线上容器用 compose 的 SYMBOLS env 覆盖（当前
    # 显式设成 BTC_USDT,ETH_USDT —— 靠 env 控制上线节奏，不随代码默认值改变）。
    symbols: List[str] = field(default_factory=lambda: [s for s in _env(
        "SYMBOLS", "BTC_USDT,ETH_USDT,SOL_USDT").split(",") if s])
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

    # API 钱包（agent）到期保护：≤3 天时自动清仓。默认**开** —— 到期后执行器彻底
    # 失去下单能力，仓位想平也平不了；这个开关存在的意义是让用户能主动关掉。
    # 语义与推送开关一致：识别不了一律按「开」（见 app.hl.agent.cleanup_enabled）。
    agent_expiry_cleanup: bool = _env("AGENT_EXPIRY_CLEANUP", "1").lower() in ("1", "true", "yes", "on")
    # 净 PnL 口径（PR-3）：关 = 面板/记录表仍用交易所 closedPnl 口径（本地价差），
    # 开 = 逐仓净额改读 net_pnl（由 hl_fills 聚合的流水口径）。**默认关**，理由见
    # app/hl/executor.py:ExecParams.pnl_from_fills（先观测差异、再切换读数）。
    # net_pnl 列两态都写，开关只影响「读哪个数」，不丢数据、可回滚。
    hl_pnl_from_fills: bool = _env("HL_PNL_FROM_FILLS", "0").lower() in ("1", "true", "yes", "on")
    # 交易所侧已平仓 → 对账时按成交价关账（2026-10-22 事故修复）。**默认开**：
    # 交易所挂单止损触发后本地不关账，旧逻辑会让下一轮对账误判 mismatch 并 kill。
    # 开 = 对账先拿已同步的平仓成交自证，证据严格成立才关账；不成立完全退回旧行为
    # （fail-closed）。设 0 即回到旧行为（应急回滚，与 hl_pnl_from_fills 同处可热改）。
    hl_fill_close: bool = _env("HL_FILL_CLOSE", "1").lower() in ("1", "true", "yes", "on")
    # 对账失败自动消化（2026-10-22 事故后第二轮）：宽限期 + 窄口径自解 kill。
    # `hl_close_grace`：对账发现「交易所无仓、但本轮拿不到成交证据」时**先挂起**，
    #   `hl_status='closing'`、不 kill，每轮重试（兼定点回捞），最多 `hl_close_grace_s` 秒；
    #   超时才升级成 mismatch + kill。默认 **开**（15 分钟）——「交易所刚成交、本地流水
    #   还没同步」是最常见的时滞，用 kill 打断自动交易并不能让它更快对上账。
    hl_close_grace: bool = _env("HL_CLOSE_GRACE", "1").lower() in ("1", "true", "yes", "on")
    # 宽限秒数：默认 900（15 分钟 ≈ 3 个 tick）。允许范围 300–3600，越界保留原值
    # （apply_db 语义；不静默关掉宽限功能）。
    hl_close_grace_s: float = _env_f("HL_CLOSE_GRACE_S", 900.0)
    # `hl_kill_autoclear`：**只对 origin=reconcile 的 kill** 生效 —— 连续 2 轮干净
    # （对账一致、无未知、本地无 open/pending/closing、无挂单）才自动解除。
    # 人工/熔断/数量/到期拉起的 kill 永不自动解除。
    hl_kill_autoclear: bool = _env("HL_KILL_AUTOCLEAR", "1").lower() in ("1", "true", "yes", "on")
    # churn 熔断阈值（PR-4）：同一 coin-side 在 10 分钟内撤单/改价重挂达到这次数就停手。
    # 0 = 关闭熔断（只留 15s 静置闸门）。默认 5 —— 正常修复不会到 5 次，到了就是
    # 「止损挂不上 → 撤 → 重挂」的循环，继续试只会制造更多无保护瞬间。
    hl_churn_max: int = _env_i("HL_CHURN_MAX", 5)
    # extraAgents 查不到有效期时的手工兜底（毫秒时间戳字符串；空=没有）
    hl_agent_valid_until_ms: str = _env("HL_AGENT_VALID_UNTIL_MS", "")
    # 出入金流水同步间隔（秒）。面板的盈亏曲线要剔除出入金，数据源是交易所账本；
    # 流水是**低频**事件（本账户至今 2 笔），没必要每轮都拉，默认 30 分钟。
    # 只有参数、**没有面板开关**（用户明确要求）：正常情况不需要人管它。
    hl_flow_sync_s: int = _env_i("HL_FLOW_SYNC_S", 1800)

    # 回撤熔断（DD Guard，规格见分支 feat/dd-guard）：判据是**策略口径**峰谷回撤
    # （TWR 链、剔除了出入金），而不是账户权益回撤 —— 后者天生被入金/提现污染。
    # 总开关默认**开**：它是「温水煮青蛙」唯一的兜底，关掉等于只剩日内 5% 那条线
    # （每天亏 4% 的系统永远不会触发它）。
    hl_dd_guard: bool = _env("HL_DD_GUARD", "1").lower() in ("1", "true", "yes", "on")
    # 阈值覆盖：0 = 按当前 risk_pct 档位自动（§3.4：1.5% 档 25/30、2.5% 档 30/40、
    # 3% 档 40/50）。留 0 而不是把默认值抄进来，是为了「改 risk_pct 线自动跟着走」。
    hl_dd_warn_pct: float = _env_f("HL_DD_WARN_PCT", 0.0)
    hl_dd_kill_pct: float = _env_f("HL_DD_KILL_PCT", 0.0)

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
                   "equity_mode", "equity_min", "agent_expiry_cleanup",
                   "hl_agent_valid_until_ms", "hl_pnl_from_fills", "hl_churn_max",
                   "hl_fill_close", "hl_close_grace", "hl_close_grace_s",
                   "hl_kill_autoclear", "hl_flow_sync_s",
                   "hl_dd_guard", "hl_dd_warn_pct", "hl_dd_kill_pct")

    # 数值配置的合法范围：apply_db 收敛到这里，超出范围/自相矛盾的组合直接拒收
    # （保留原值）——否则 stop_min>stop_max 会让全部信号静默消失、leverage=0 会让
    # 每轮 tick 抛 ZeroDivisionError，而面板数值看着完全「正常」。
    _RANGES = {
        "risk_pct": (0.0001, 0.5),
        "leverage": (0.5, 125.0),
        # 止损距离上限必须保证「强平晚于止损」（逐仓 5x 强平约 -19.5%）：
        # 静态上限 0.5 会让 stop_max=0.5 的配置合法入库 —— 止损单永远在强平价之外
        # 不会被触发（审计三轮 M4）。收紧到 0.25 并在 apply_db 里与杠杆联动二次校验。
        "stop_min": (0.0001, 0.25),
        "stop_max": (0.0001, 0.25),
        "equity": (1.0, 100_000_000.0),
        "equity_min": (1.0, 100_000_000.0),
        "short_regime_ma": (0, 1000),
        # 出入金流水同步间隔：下限 300s（比 tick 间隔还密没有意义，账本本身就是低频），
        # 上限 86400s（一天一次）—— 再长面板的盈亏曲线就会长时间停在旧流水上。
        "hl_flow_sync_s": (300, 86400),
        # churn 熔断阈值（PR-4）：0 = 关闭（只留静置闸门），上限 100 防止「配成 10000
        # 等于关掉保护还自以为开着」。
        "hl_churn_max": (0, 100),
        # 平仓宽限秒数：默认 900（15 分钟）。下界 300 = 一个 tick 周期，再小就等于
        # 没有宽限（第一轮没证据立刻 kill）；上界 3600 = 一小时，再长就是「忘了这回事」，
        # 期间该标的还禁止开新仓。越界保留原值（不静默关功能）。
        "hl_close_grace_s": (300.0, 3600.0),
        # 回撤阈值覆盖（0 = 按 risk_pct 档位自动，见 app/hl/dd.py 的 TIERS）：
        # 下界 0.05 保证「覆盖值」不会是 5% 这种一碰就响的噪音线，上界 0.95 与
        # 规格 §6 的 kill <= 0.95 同值（见 apply_db 里对 kill 的二次校验）。
        "hl_dd_warn_pct": (0.05, 0.95),
        "hl_dd_kill_pct": (0.05, 0.95),
    }

    # 字符串配置允许「显式清空」的键。
    # 目前**为空**：`set_config` 存的是 '""'，而 apply_db 对空串一律当「没传」——
    # 那正是我们想要的语义（面板留空 = 不改动）。曾经把 hl_agent_valid_until_ms
    # 放进来，结果「保存其它参数」的空输入把手工兜底值抹掉了（M3）；要清空兜底
    # 走「API 有效期」卡片里的「清除兜底」按钮（显式、单字段、可见）。
    _CLEARABLE_STR: tuple = ()

    def apply_db(self, values: dict) -> "Config":
        # 先解析成 dict，统一做范围/一致性校验后再 setattr
        parsed: dict = {}
        for k in self.OVERRIDABLE:
            if k in values and k in self._CLEARABLE_STR:
                raw0 = str(values[k]).strip().strip('"').strip("'")
                if raw0 == "":
                    parsed[k] = ""
                    continue
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
                except (TypeError, ValueError):
                    parsed.pop(k, None)
                    continue
                # 回撤阈值：0 = 「按 risk_pct 档位自动」（规格 §6 明写 0 是合法值）。
                # 它不在 0.05~0.95 里，若被通用范围校验拒收，「清掉覆盖/回到自动档位」
                # 就变成只能改库的操作 —— 面板上留空又等于「不改」，用户没有出路。
                if k in ("hl_dd_warn_pct", "hl_dd_kill_pct") and v == 0.0:
                    continue
                if not (lo <= v <= hi):
                    parsed.pop(k, None)
        # 一致性：回撤告警线必须严格小于熔断线，否则两条线自相矛盾（告警永远晚于
        # 熔断）。规格 §6：不成立时**两者一起**拒收、保留原值。
        # 用 app/hl/dd.py 的同一份解析（含 risk_pct 档位联动）：只填告警线 0.35 而熔断线
        # 走 2.5% 档的 0.40 是合法的，填 0.45 就不合法 —— 这个判断不能在这里另抄一份，
        # 否则面板/装配/写入三处会慢慢分叉。
        dw = _as_dd_float(parsed.get("hl_dd_warn_pct", getattr(self, "hl_dd_warn_pct", 0.0)))
        dk = _as_dd_float(parsed.get("hl_dd_kill_pct", getattr(self, "hl_dd_kill_pct", 0.0)))
        if ("hl_dd_warn_pct" in parsed or "hl_dd_kill_pct" in parsed) and (dw > 0 or dk > 0):
            risk = _as_dd_float(parsed.get("risk_pct", getattr(self, "risk_pct", 0.02)))
            if _dd_thresholds(risk, dw, dk)["rejected"]:
                parsed.pop("hl_dd_warn_pct", None)
                parsed.pop("hl_dd_kill_pct", None)
        # 一致性：stop_min 必须小于 stop_max（两者都存在且至少一方本次被改过才判）
        smin, smax = parsed.get("stop_min", getattr(self, "stop_min", None)), \
            parsed.get("stop_max", getattr(self, "stop_max", None))
        try:
            if smin is not None and smax is not None and float(smin) >= float(smax):
                parsed.pop("stop_min", None)
                parsed.pop("stop_max", None)
        except (TypeError, ValueError):
            pass
        # 杠杆联动（审计三轮 M4）：逐仓强平约在 1/leverage，止损必须显著靠近内侧
        # （留 50% 余量，与 sizing.max_safe_leverage 同口径），否则止损单永远等不到
        # 触发就被强平 —— 拒收 stop_max。
        lev = parsed.get("leverage", getattr(self, "leverage", None))
        try:
            if lev is not None and smax is not None:
                cap = 0.5 / float(lev)
                if float(smax) > cap:
                    parsed.pop("stop_max", None)
        except (TypeError, ValueError):
            pass
        for k, v in parsed.items():
            setattr(self, k, v)
        return self