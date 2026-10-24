"""HL 执行器：把信号变成订单，把订单变成可审计的记录。

设计原则（可靠性优先）：
- **fail-closed**：任何不确定（读不到账户、对账不一致、止损挂不上、SDK 不可用）→ 不交易，
  必要时立刻平掉已开仓位。宁可丢机会，不可留无保护仓位。
- **幂等**：每根 bar 的每个动作有唯一 cloid + 唯一 DB 键；重放/重试都不会双开。
- **可审计**：先落 intent 再发请求；每步都写 hl_events；面板/看护都读这些表。
- **可回滚**：模式 off/dry/live 三态，任何时刻可以退回「只提醒」。

本模块不 import 官方 SDK（那在 exchange.py 里）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import math
import time
import threading
import weakref
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from app.hl import (MODE_DRY, MODE_LIVE, MODE_OFF, PURPOSE_ENTRY, PURPOSE_EXIT, PURPOSE_EXIT_REHANG,
                    PURPOSE_STOP,
                    ST_CANCELLED, ST_FAILED, ST_FILLED, ST_INTENT, ST_PLANNED, ST_RESTING, ST_SENT,
                    ST_SKIPPED,
                    EV_DECISION, EV_DD, EV_ERROR, EV_HEARTBEAT, EV_KILL, EV_ORDER, EV_RECONCILE)
from app.hl import sizing as hl_sizing
from app.hl import pnl as hl_pnl
from app.hl import flows as hl_flows
from app.hl import funding as hl_funding
from app.hl import dd as hl_dd
from app.hl.exchange import STATUS_DRY, STATUS_FILLED, STATUS_RESTING, make_cloid
from app.engine import DISPLAY_TZ, TZ_LABEL
from app.hl.info import HlInfoError
from app.sizing import effective_risk_pct
from app import symbol_registry as registry

logger = logging.getLogger(__name__)

# coin 映射的**默认值从单一注册表派生**（app/symbol_registry.py）——上一版手写
# {"BTC_USDT": "BTC", "ETH_USDT": "ETH"} 漏了 SOL，靠 `coin()` 的 split 兜底才没炸；
# split 兜底已删除（见 Executor.coin），所以这里必须与注册表同源。
DEFAULT_COIN_MAP = {s: registry.hl_coin(s) for s in registry.known_symbols()}

# 手续费的**计价币**白名单：只有这些币种的 fee 才能直接当 U 累加（1:1）。
# HL 允许用 HYPE 抵扣或按其他币计费，此时 fee 是「币的数量」——
# 直接加进净利会把 0.2 ARB 当 0.2 U，误差可达几个数量级。
QUOTE_FEE_TOKENS = frozenset({"USDC", "USDT", "USD"})

# 「费用币种未折算」事件的去重 marker（存已提示过的 tid，逗号分隔）：
# user_fills 的窗口会重复返回同一笔成交，不记 marker 就会每轮刷一条事件。
FEE_UNQUOTED_KEY = "hl_fee_unquoted_tids"
FEE_UNQUOTED_MAX = 200

# 数量对账（PR-3）的「两击」阈值：一位小数 tick 的容差不够 —— HL 的数量是
# 10^-szDecimals 的整数倍，浮点往返（字符串→float→SQLite REAL）本身就会差几十个
# 末位，所以按「≤1 tick 视为一致」，并额外给一个相对噪声容差兜住大数量。
SIZE_DIFF_STRIKES_TO_KILL = 2

# 规格（szDecimals）的进程级 TTL 缓存（对抗审查 M6）：``info.specs()`` 走的是
# ``meta`` 一次**真实 POST**（还带 3 次重试），而 ``_size_tick`` 只在「证据不足的
# 每一轮」「数量对账」这些分支被调 —— 执行器/接口不健康时就是每 5 分钟重复打一次，
# 白吃限频窗口。szDecimals 是交易所静态元数据（只在上市/精度调整时变），缓存 1 小时
# 足够；代价是精度真的被交易所调整后，最多 1 小时内仍按旧 tick 判容差（1e-5 级差异，
# 远小于任何真实的数量对账差）。
# 键用 info **对象本身**（WeakKeyDictionary）：生产单账户、多个 Executor 实例共用同一
# info → 真正省掉重复 POST；而测试桩/多账户各自一个 info，互不污染（用 id() 会踩
# 地址复用 —— 新桩对象复用旧地址就会读到别人的 tick）。info 不可弱引用时退化成不缓存。
# 失败**不写缓存**：接口抖动不能被钉成一小时的错误答案，下一轮必须重试。
SPEC_TICK_TTL_S = 3600.0
_TICK_CACHE: "weakref.WeakKeyDictionary[Any, Dict[str, Tuple[float, float]]]" = \
    weakref.WeakKeyDictionary()

# ---------------------------------------------------------------- 闸门（PR-4）
# 1) 静置闸门（settling gate）：入场**成交回报**之后 15s 内，该 coin-side 的持仓状态
#    （数量、挂单列表）还在收敛中，此时按快照去撤单/复价/补挂重试或者再开一次仓，
#    用的都是「上一个瞬间」的世界。窗口**有界且不可续期**：只由那一次成交回报起算，
#    后续任何事件（对账、补挂、平仓回报）都不延长它 —— 可续期就等于可以被无限拖延，
#    而它的目的是「等一小会儿」，不是「一直等」。
SETTLE_WINDOW_S = 15.0
# 2) churn 熔断：同一 coin-side 在 CHURN_WINDOW_S 内 cancel/replace（撤单或改价重挂）
#    达到上限就停手。阈值可配（ExecParams.churn_max ← hl_churn_max），窗口固定：
#    「10 分钟内 5 次」是运维口径，窗口本身不该随手改（改了就无法和告警文案对照）。
CHURN_WINDOW_S = 600.0
CHURN_MAX_DEFAULT = 5

# 动作类别：决定一次动作要过哪几道闸。**放行清单是刻意写死的** ——
#   首次止损挂单 / 平仓 属于「保护动作」，任何闸门都不能延迟它们（延迟 = 裸仓窗口），
#   所以它们不查闸；其余动作（新开仓、撤单复价补挂重试）才需要过闸。
ACT_ENTRY = "entry"                    # 新开仓：静置 + churn
ACT_REPAIR = "repair"                  # 修复类补挂（stop:fix）：静置 + churn
ACT_REPLACE = "replace"                # 离场路径的「撤止损 → 重挂」：只过 churn
                                       # （静置不查：那时止损已被撤，再等就是裸仓）
ACT_STOP_FIRST = "stop_first"          # 首次止损挂单：不过闸（安全动作）
ACT_CLOSE = "close"                    # 平仓：不过闸（保护动作）

GATE_OK = "ok"
GATE_SETTLE = "settle"
GATE_CHURN = "churn"

_GATE_NONE_ACTIONS = (ACT_STOP_FIRST, ACT_CLOSE)

# pnl_source 取值（positions.pnl_source，面板/报告据此判断该不该采信 net_pnl）
PNL_SRC_FILLS = "fills"          # 有成交，net_pnl 来自 hl_fills 聚合
PNL_SRC_NO_FILLS = "no_fills"    # 窗内查不到成交（net_pnl 为 NULL，不猜 0）
PNL_SRC_NO_EXIT_FILLS = "no_exit_fills"   # 只有入场腿（平仓成交还没同步），不写半截净额

# ---------------------------------------------------------------- 宽限期（对账失败自动消化）
# hl_status 的一个新取值：与 open/pending/mismatch/closed 并列。
#   closing = 「交易所侧已无仓，但还没拿到足够成交证据」—— 既不是 open（不能在别的
#   路径上被当成正常持仓去补挂止损），也不是 mismatch（还不是真矛盾，只是时滞）。
HL_ST_CLOSING = "closing"

# 宽限状态的存储键：`hl_close_grace:<pid>` → "<first_seen>:<rounds>:<escalated>"（configuration 表）。
# 为什么落在 DB 而不是进程内存：build_executor() 每轮新建实例、容器还会重启，
# 宽限期必须跨轮/跨重启计时（内存版等于每轮都重新开始等 = 永远不 kill）。
GRACE_KEY_PREFIX = "hl_close_grace:"
GRACE_DEFAULT_S = 900.0          # 15 分钟（用户拍板）：≈ 3 个 tick 的时滞余量
GRACE_MIN_S, GRACE_MAX_S = 300.0, 3600.0

# 第三段 escalated：1 = 这个仓**已经升级过**（problem + mismatch），宽限期到此结束。
# 为什么需要它（对抗审查 M1）：超时那一轮以前直接**删键**，下一轮键不存在 → 又走
# 「第一次进入宽限」→ 再等满 900s → 再升级 → 删键。结果是 mismatch/closing 每 15 分钟
# 来回翻、每轮推一条「进入宽限」、对账恢复文案把 6h 节流清零。升级是一次性判定，
# 状态必须留在键里；旧格式（无第三段）按 0 解析，老数据照样能读。
GRACE_ESCALATED = 1
# 时钟回拨容差（对抗审查 L3）：first_seen 比 now 还大这么多 → 状态不可信（容器重启后
# 宿主机时钟被校正/换了时区），重置为 now，否则 elapsed 永远是负数 → 永久 closing。
GRACE_CLOCK_ROLLBACK_S = 60.0
# 宽限期内**不再每轮回捞**从第几轮开始（对抗审查 L2）：回捞是 2 次交易所调用，
# 宽限 15 分钟 ≈ 3 轮；前几轮值得试（时滞通常就一两轮），之后每轮都打只会在限频
# 窗口里给自己找 429，之后只读本地 cache。
GRACE_REFETCH_MAX_ROUND = 4

# 窄口径自解 kill 的连续干净轮次计数（configuration 表整数）与阈值。
# 为什么是 2 而不是 1：一轮干净可能只是「这一轮恰好没读到问题」（快照抖动、
# 挂单接口一时返回空），连干净两轮才说明账真的自己收敛了。
KILL_CLEAN_KEY = "hl_kill_clean_rounds"
KILL_CLEAN_ROUNDS = 2
# 「连续两轮」必须有**时间下限**（对抗审查 M3）：run() 是 5 分钟一轮，但手工触发、
# 重启后的追赶 tick、脚本调 reconcile 时两次 run() 可以只隔 0.17 秒 —— 那样的「两轮」
# 只是对同一份快照判了两次，不构成「账自己收敛了」。用上一轮计数的时刻兜住：
# 间隔不足 KILL_CLEAN_MIN_GAP_S 的轮次**不计数也不归零**（它既不是又干净了一轮，
# 也不是脏了），等下一个真 tick 再说。
KILL_CLEAN_TS_KEY = "hl_kill_clean_last_ts"
KILL_CLEAN_MIN_GAP_S = 60.0

# 定点回捞成交（证据不足那一轮才发，最多 2 次、间隔 1s）：
#   窗口起点 = opened_at − 120s（与本地 _fill_close_proof 的下界同一口径），
#   只用开始时间而不是「开始+结束」—— 平仓成交一定在 opened_at 之后，
#   给它一个更晚的结束时间反而会漏掉「刚成交」的那一笔。
REFETCH_ATTEMPTS = 2
REFETCH_GAP_S = 1.0
REFETCH_MARGIN_MS = 120_000


# ---------------------------------------------------------------- 推送文案（人类可读）

def _fmt_u(x, nd: int = 2) -> str:
    return f"{x:,.{nd}f}"


def _side_word(side: int) -> str:
    return "做多" if int(side or 1) >= 0 else "做空"


def _fmt_num(v, nd: int = 2) -> str:
    """可空数字格式化：None（算不出来）显示 '—'，不要把 None 当 0 打出去。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):+.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def fmt_entry_alert(coin: str, side: int, sz: float, entry_px: float, stop: float,
                    stop_dist: float, notional: float, margin: float, liq: float,
                    risk_u: float, equity: float, signal_px: float, oid, stop_oid,
                    when: "dt.datetime", tz_label: str,
                    leverage: float = 5.0) -> str:
    """开仓回报：价格 / 数量 / 保证金 / 强平 / 风险 分行排，别糊成一坨。

    equity 传**生效基数**（effective_equity），不是配置 equity —— follow 模式下
    两者默认不同，传错会让「权益的 X%」差一个数量级。
    """
    drift = ((entry_px - signal_px) / signal_px * 100) if signal_px else 0.0
    stop_pct = ((stop - entry_px) / entry_px * 100) if entry_px else 0.0
    pct = (risk_u / equity * 100) if equity else 0.0
    return "\n".join([
        f"【HL执行】✅ 已开仓 · {coin} {_side_word(side)} {leverage:g}x 逐仓",
        "",
        f"成交价    {_fmt_u(entry_px)}",
        f"数量      {sz:g} {coin}（名义 {_fmt_u(notional, 1)} U）",
        f"止损价    {_fmt_u(stop)}（{stop_pct:+.2f}%）",
        f"保证金    ≈{_fmt_u(margin, 1)} U",
        f"强平价    {_fmt_u(liq)}",
        f"风险      {_fmt_u(risk_u)} U（权益的 {pct:.1f}%）",
        f"滑点      {drift:+.2f}%（信号价 {_fmt_u(signal_px)}）",
        f"时间      {when.strftime('%m-%d %H:%M')} {tz_label}",
        f"单号      开仓 {oid} · 止损 {stop_oid}",
    ])


def fmt_exit_alert(coin: str, side: int, entry_px: float, exit_px: float, sz: float,
                   reason: str, r_multiple: float, gross_pnl: float, fees: float,
                   opened_at, when: "dt.datetime", tz_label: str) -> str:
    """平仓回报：说清**为什么平**（止损/策略离场）+ 价格 + 盈亏（含手续费）。"""
    net = gross_pnl - fees
    if reason == "stop":
        head, why = "🛑 止损离场", "止损单被触发"
    elif reason == "ma20":
        head = "📈 策略止盈" if net > 0 else ("📉 策略离场" if net < 0 else "➖ 策略离场")
        why = "收盘跌破 SMA20" if int(side or 1) >= 0 else "收盘站上 SMA20"
    else:
        head, why = "已平仓", str(reason)
    hold = ""
    try:
        mins = max(0, int((when.timestamp() - float(opened_at)) // 60))
        if mins >= 1440:
            d, h = mins // 1440, mins % 1440 // 60
            hold = f"{d} 天" + (f" {h} 小时" if h else "")
        elif mins >= 60:
            h, m = mins // 60, mins % 60
            hold = f"{h} 小时" + (f" {m} 分钟" if m else "")
        else:
            hold = f"{mins} 分钟"
    except (TypeError, ValueError):
        pass
    lines = [
        f"【HL执行】{head} · {coin} {_side_word(side)}",
        "",
        f"离场原因  {why}",
        f"平仓价    {_fmt_u(exit_px)}（开仓 {_fmt_u(entry_px)}）",
        f"本次盈亏  {net:+,.2f} U（{r_multiple:+.2f}R）",
        f"盈亏构成  价差 {gross_pnl:+,.2f} U · 手续费 {_fmt_u(fees)} U",
    ]
    if hold:
        lines.append(f"持仓时长  {hold}")
    lines.append(f"时间      {when.strftime('%m-%d %H:%M')} {tz_label}")
    return "\n".join(lines)


@dataclass
class ExecParams:
    mode: str = MODE_OFF
    account_address: str = ""
    private_key: str = ""
    base_url: str = "https://api.hyperliquid.xyz"
    coin_map: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COIN_MAP))
    leverage: float = 5.0
    risk_pct: float = 0.02
    # 空单风险减半（feat/short-risk-half）：开 = 做空腿的单笔风险按 0.5 倍折算
    # （app/sizing.py:effective_risk_pct 是唯一生效点）。默认**关**。
    # 只有开仓 sizing 读它：止损价/距离、离场、DD Guard、对账一概不读 —— 它调的是
    # 「下一笔空单开多大」，不是「已持仓怎么管」。
    short_risk_half: bool = False
    equity: float = 1000.0
    equity_mode: str = "follow"          # follow = 跟随实时权益（带下限）；fixed = 用上面的固定值
    equity_min: float = 20.0
    notify_fills: bool = True            # 成交回报推送（面板开关 fill_push）；异常提醒不受此开关影响
    max_slippage_pct: float = 0.003
    # 名义上限（单标的 / 合计，单位=权益倍数）。**默认 0 = 不设限**（用户拍板 2026-10-14）：
    # 仓位已由「风险 ÷ 止损距离」定死（单笔风险恒 risk_pct），上限只会误伤合法信号
    # —— 实测 1000U 账户下两个标的都用紧止损（1.5%）时合计 2.67x > 2.5x，第二个仓会被卡掉。
    # 留成可选（>0 生效）而不是删掉：万一将来想加暴露度护栏，改配置即可。
    max_notional_mult: float = 0.0
    max_total_notional_mult: float = 0.0
    max_orders_per_day: int = 4
    max_fail_streak: int = 3
    max_daily_loss_pct: float = 0.05
    deadman_minutes: int = 30
    # 死手开关（HL 原生 scheduleCancel）：默认**关闭**。理由见 docs/HL_RUNBOOK.md —— 它的语义是
    # 「机器人失联就撤掉所有挂单」，会连持仓的止损单一起撤（对持仓型策略是反向保险），
    # 且低成交量账户本身就调不通（每轮报错）。要启用必须显式开 hl_use_schedule_cancel。
    use_schedule_cancel: bool = False
    min_notional: float = 10.0
    snapshot_min_interval_s: float = 240.0
    stop_max_hard: float = 0.10           # 执行时止损距离超此值直接放弃
    # 净 PnL 口径开关（PR-3）：**默认关** = 只用流水口径**观测**（照算 net_pnl、
    # 照记差异事件），面板/统计仍读本地价差口径；开 = 面板逐仓净额改读 net_pnl。
    # 默认关的理由：流水口径依赖 hl_fills 已同步完整，上线初期两边数字不一致时，
    # 先让差异被记录、被人工核对，再决定要不要切换读数（切换是可回滚的一行配置）。
    pnl_from_fills: bool = False
    # 交易所侧已平仓 → 对账时按成交价关账（2026-10-22 事故修复）。默认**开**。
    # 背景：交易所挂单止损被触发时，_sync_fills 只写 hl_fills、不动仓位/挂单状态，
    # 旧逻辑下一轮对账看到「本地有仓、交易所无仓」就直接判 mismatch 并 kill。
    # 开 = 对账在判 mismatch **之前**先用本轮已同步的平仓成交自证「这仓确实被交易所
    # 平掉了」，证据成立才按成交价关账、落挂单终态、提醒，且不 kill；任一条件不满足
    # （无成交/数量不足/精度不可知/oid 不明）一律退回旧行为 —— 严格证据下才动账，
    # fail-closed。设 False 即回到旧行为（应急回滚开关）：**两条路径一起关** ——
    # 对账不再按成交证据关账，`on_exit` 的「mismatch + stop」放行也一并失效
    # （见 _on_exit_locked 的守卫），否则「关掉」只剩半截，止损成交仍会被自动关账。
    fill_close: bool = True
    # 对账失败自动消化（2026-10-22 事故后第二轮，规格见 docs/HL_RUNBOOK.md §「宽限期」）：
    #   close_grace    —— 开 = 对账发现「交易所无仓、但本轮拿不到成交证据」时先挂起
    #                     （hl_status='closing'、不 kill、每轮重试 + 定点回捞），
    #                     超过 close_grace_s 才升级成 mismatch + kill。
    #                     关 = 完全回到「第一轮没证据就 mismatch + kill」的旧行为。
    #   close_grace_s  —— 宽限秒数（Config 默认 900 = 15 分钟，允许 300–3600）。
    #   kill_autoclear —— 开 = **只对 origin='reconcile' 的 kill**，连续 2 轮干净后
    #                     自动解除并推一条；其它来源（人工/熔断/数量/到期）永不自动解除。
    close_grace: bool = True
    close_grace_s: float = GRACE_DEFAULT_S
    kill_autoclear: bool = True
    # churn 熔断阈值（PR-4）：同一 coin-side 在 CHURN_WINDOW_S 内 cancel/replace
    # 达到这次数 → 停手 + 告警（走既有 _alert 通道）。可配（面板/DB 键 hl_churn_max），
    # 0 或负数 = 关闭熔断（只保留静置闸门）。默认 5：实测正常一轮修不出 5 次撤挂，
    # 到这个量级必然是「止损挂不上 → 撤了重挂 → 又挂不上」的循环，此时停手比继续试安全
    # （继续试只会制造更多无保护的瞬间，且交易所侧限频也会打满）。
    churn_max: int = CHURN_MAX_DEFAULT
    # 出入金流水同步间隔（秒）：盈亏曲线要剔出入金，流水从交易所账本拉。
    # 低频事件（本账户至今 2 笔）→ 不必每轮拉，默认 30 分钟。范围 300~86400，
    # 由 Config.hl_flow_sync_s / DB 键同名控制；**没有面板开关**（用户明确要求）。
    flow_sync_s: int = 1800
    # 回撤熔断（DD Guard）：判据 = 策略口径 TWR 峰谷回撤（已剔除出入金）。
    #   dd_guard    —— 总开关。关 = L2/L3 全不生效（L1 修好口径后照旧生效）。
    #   dd_warn_pct / dd_kill_pct —— 覆盖阈值；0 = 按 risk_pct 档位自动（§3.4）。
    # 默认全开 + 全自动：升档时线必须同批跟着走，所以默认不把 25/30 抄成常量。
    dd_guard: bool = True
    dd_warn_pct: float = 0.0
    dd_kill_pct: float = 0.0
    code_rev: str = ""

    @property
    def follow(self) -> bool:
        return str(self.equity_mode or "follow").lower() != "fixed"

    @property
    def live(self) -> bool:
        return self.mode == MODE_LIVE


# L①：资金费同步失败的 error 事件按 24h 去重（参照 flows.py _FLOW_UNKNOWN_DEDUP_S
# 先例）：失败会随 30 分钟闸门反复出现，每轮都记会把 hl_events 刷成同一句话的
# 复读机；24h 内一条 + warning 日志每轮照记，足够喊人又不刷屏。
_FUNDING_FAIL_DEDUP_S = 24 * 3600.0


def _funding_fail_logged(db: Any, now: float) -> bool:
    """24h 内是否已经记过「资金费同步失败」error 事件（按 data.src 标记识别）。"""
    try:
        rows = db.hl_recent_events(200, kind=EV_ERROR) or []
    except Exception:                                            # noqa: BLE001
        return False
    for r in rows:
        try:
            created = float(r["created_at"] or 0.0)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if now - created > _FUNDING_FAIL_DEDUP_S:
            break                      # 按 id DESC 返回，后面的只会更老
        try:
            data = json.loads(r["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        if str((data or {}).get("src") or "") == "funding_sync":
            return True
    return False


def _today_start_ts(now: Optional[float] = None) -> float:
    """当日 0 点（**北京时间**，与展示层一致；旧版 UTC 日界会让「今日下单数/
    今日已实现」比用户直觉早/晚 8 小时）。"""
    tz = dt.timezone(dt.timedelta(hours=int(os.environ.get("DISPLAY_TZ_OFFSET", "8"))))
    d = dt.datetime.fromtimestamp(now if now is not None else dt.datetime.now(
        dt.timezone.utc).timestamp(), tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class _RoundNote:
    """本轮「待定文案」：措辞取决于本轮**结束时**的 kill 状态（规格 §4.5 中1）。

    为什么需要它：对账自动解 kill 与回撤熔断（以及当日亏损线）在**同一轮**里都可能
    动 kill。v1 在自解那一刻就把「✅ 已自动恢复自动交易」放进 alerts，于是同一轮
    稍后又置了 kill 时，用户会收到两条自相矛盾的消息。v2 的做法是：先把「对账已
    收敛」这件事记成这个标记，等本轮所有判据都走完、kill 终态确定后再渲染文案。
    """

    __slots__ = ("kind", "data")

    def __init__(self, kind: str, **data: Any) -> None:
        self.kind = kind
        self.data = data

    def render(self, kill: bool, reason: str) -> str:
        n = int(self.data.get("rounds") or 0)
        if self.kind != "autoclear":                             # pragma: no cover
            return ""
        if not kill:
            return (f"【HL执行】✅ 已自动恢复自动交易（对账连续 {n} 轮正常，"
                    f"此前的对账异常已自动消化）")
        # 本轮后面又被别的判据要求停手（回撤熔断 / 当日亏损）：停手优先，
        # 但必须说清「对账这条线已经没事了」，而不是让用户以为对账还在异常。
        return (f"【HL执行】ℹ️ 对账异常已收敛（连续 {n} 轮正常），"
                f"但{reason or '其它保护'}仍要求停手 → 自动交易仍处暂停，未解除。")


class Executor:
    # **进程级**写路径互斥：build_executor() 每轮 tick 都新建 Executor（main.py:168），
    # 实例级锁只能串行同一对象内的 run/on_entry/on_exit —— 跨 job（tick vs
    # hl_monitor vs 到期清仓）完全无效。用类属性 + 一把全局锁，让「撤单→下单→落库」
    # 的序列在整个进程里串行。
    _op_lock = threading.Lock()

    # 数量对账「两击」的 strike 计数（PR-3）。**必须放类属性**：执行器每轮 tick 由
    # build_executor() 新建实例，实例属性活不过一轮，永远数不到第二次。
    # 只存进程内存（重启清零可接受）：重启后至少还要重新连续超差两次才 fail-closed，
    # 代价是晚一轮动作，而写进 DB 会引入「僵尸 strike」（几天不重启就误平仓）的风险。
    _size_strikes: Dict[str, int] = {}
    _size_strikes_lock = threading.Lock()

    # 静置闸门（PR-4）：(账本路径, coin, side) → 解禁的 deadline（time.time() 绝对秒）。
    # **必须类属性**：build_executor() 每轮 tick 新建实例，实例属性活不过一轮，
    # 成交回报写在 A 实例、15s 内的对账跑在 B 实例 —— 放实例上等于闸门只挡住自己这一轮。
    # 只存「什么时候解禁」而不是「还有多少秒」：剩余秒数每读一次都要重算，存绝对时间
    # 就不可能被读取方续期（从数据形状上堵死「越等越久」）。
    _settle_until: Dict[Tuple[str, str, int], float] = {}
    _settle_lock = threading.Lock()

    # churn 计数（PR-4）：(账本路径, coin, side) → [动作时刻, ...]（只留窗口内的）。
    # 同样放类属性（跨实例累计）；**只存进程内存**，重启清零可接受 —— 重启本身就
    # 打断了撤挂风暴，而写 DB 会引入「几天前的旧记录把标的永久熔断」的风险。
    _churn_hits: Dict[Tuple[str, str, int], List[float]] = {}
    _churn_lock = threading.Lock()
    # 已就该轮 churn 熔断告警过的键 → 最后告警时刻（同一窗口内只喊一次，
    # 否则每轮 tick 都推一条，真异常会被自己刷掉）。
    _churn_alerted: Dict[Tuple[str, str, int], float] = {}

    # 闸门事件的**每轮限频**状态：轮次号 + 键 → 最后一次记事件的轮次号。
    # 「同 coin-side 每轮最多一条」靠比较这两个数实现（一次 tick 里补挂重试 + 开仓
    # 可能连撞两次闸，全记会把 15s 窗口刷成一堆同样的行）。
    _gate_round_no: int = 0
    _gate_ev_round: Dict[Tuple[str, str, int], int] = {}
    _gate_ev_lock = threading.Lock()

    def __init__(self, db, params: ExecParams, info, exchange, notifier=None):
        self.db = db
        self.p = params
        self.info = info
        self.ex = exchange
        self.notifier = notifier
        # 闸门状态的作用域：**(账本路径, coin, side)**。为什么不只用 (coin, side)：
        # 同一进程里可能先后跑多个账本（测试用临时 DB、运维脚本指向别的库），
        # 跨账本共享熔断就是「A 库的撤挂风暴把 B 库的标的停手」。生产里 db.path 恒定，
        # 跨 tick 新建的 Executor 共用同一个作用域 —— 跨实例累计的语义不受影响。
        self._gate_scope = str(getattr(db, "path", "") or "")

        # _on_exit 自己的那次同步撞车，10s 内不重复打同一个接口（同一实例内判断，
        # 跨实例本来就会重新同步，语义不变）。
        self._last_sync_at = 0.0
        # 【v2·高1】本轮 `_sync_flows` 是否**真的调了账本接口并成功落库**。
        # 与「本地这份流水可信」是两件事：v1 把「30 分钟闸门内跳过」也当成可信 → 闸门内
        # 发生的出入金被全额算成盈亏（提现 500U → 误 kill「当日亏损 −50%」），而且指数链
        # 已经推进、后续流水同步到了也不会回滚。v2 铁律：DD/L1 只在 True 时推进与判线。
        # 默认 False：没跑过同步就当作「本轮没同步」，一切判据跳过（fail-closed）。
        self._flows_fresh = False
        # 上一轮没判的原因（只用于面板提示与日志，不参与任何判断）。
        self._flows_skip = ""
        # 本轮对账是否已经自动解过 kill（§4.5 中1：同轮 kill 只允许「未触发→触发」
        # 单向；若后面又置 kill，收尾文案要说清「对账已收敛但仍停手」）。
        self._round_autocleared = 0
        # L②：info 不具备 userFunding 能力的提示只发一次（闸门 30 分钟一轮，
        # 每轮都 warn 会把日志刷成噪音；首次之后保持安静，换 info 实例也不重发 ——
        # 能力缺失是配置事实，不是抖动故障）。
        self._funding_warned = False

    # ------------------------------------------------------------ 工具
    def _coin_or_none(self, symbol: str) -> Optional[str]:
        """解析 symbol → HL 合约名；未命中返回 None（**不抛**）。

        三层顺序与行情层 `market.hl_coin` 一致：executor coin_map（= env/DB 覆盖，
        由 main.py 从 `HL_COIN_MAP` 装配）→ 注册表 → None。
        供闸门/容错路径使用（那些路径不能因为一个配置错误就抛异常崩掉整轮 tick）。
        """
        m = self.p.coin_map or {}
        if symbol in m:
            return m[symbol]
        try:
            return registry.hl_coin(symbol)
        except registry.SymbolNotConfigured:
            return None

    def coin(self, symbol: str) -> str:
        """symbol → HL 合约名；未配置即抛 `SymbolNotConfigured`（fail-closed）。

        **去掉了 `symbol.split("_")[0]` 兜底**：`1000PEPE_USDT` 会拼出 `1000PEPE`
        这种（在这个执行器里）并不存在的 HL 名 → 静默打错 API、可能下错标的的单。
        未注册/未配置一律抛出，调用方要么显性拦截（入场闸门）要么降级告警，
        绝不允许静默猜一个名字。
        """
        c = self._coin_or_none(symbol)
        if c is None:
            raise registry.SymbolNotConfigured(
                f"未配置 HL coin 映射：{symbol}（不在注册表 symbol_registry / executor coin_map）")
        return c

    def _alert_fill(self, text: str) -> None:
        """成交回报（开仓/平仓成功）。受面板「成交回报推送」开关控制；异常类提醒一律走 _alert。

        开关关闭时也落库（kind=exec_alert_fills，不进补推白名单——开关是用户意图），
        这样 alert_logs 里有完整审计轨迹，且开关重新打开后可见历史。
        """
        if not getattr(self.p, "notify_fills", True):
            try:
                self.db.log_alert(None, None, "exec_alert_fills", text, True)
            except Exception as e:                                   # noqa: BLE001
                logger.error("[hl-exec] 成交回报落库失败：%s", e)
            return
        self._alert(text, kind="exec_alert_fills")

    def _alert(self, text: str, symbol: Optional[str] = None,
               kind: str = "exec_alert") -> None:
        """执行器告警：直发 + 落 alert_logs。

        「平仓失败（止损已撤）」「止损单丢失」这类告警一推失败就是永久丢
        （引擎侧走 _emit 有 24h 补推，执行器侧以前完全没有）—— 现在也写进
        alert_logs（bar_ms=NULL + 时间窗去重，同 data_error 的约定），
        main._retry_failed_alerts 会照常补推。
        """
        ok = False
        try:
            ok = bool(self.notifier is not None and self.notifier.send_message(text))
        except Exception as e:                                       # noqa: BLE001
            logger.error("[hl-exec] 推送失败：%s", e)
            ok = False
        try:
            self.db.log_alert(symbol, None, kind, text, ok)
        except Exception as e:                                       # noqa: BLE001
            logger.error("[hl-exec] 告警落库失败（补推将不可用）：%s", e)
        return ok

    def _event(self, kind: str, message: str, symbol: Optional[str] = None,
               data: Any = None) -> None:
        try:
            self.db.hl_log_event(kind, message, symbol=symbol, data=data, code_rev=self.p.code_rev)
        except Exception as e:                                       # noqa: BLE001
            logger.error("[hl-exec] 写事件失败：%s", e)

    def _snapshot(self, force: bool = False):
        """读账户快照；>=snapshot_min_interval_s 才落库一次（面板曲线用）。失败返回 None。"""
        try:
            snap = self.info.snapshot(self.p.account_address)
        except HlInfoError as e:
            self._event(EV_ERROR, f"读取账户失败：{e}")
            return None
        if not self.p.account_address:
            return snap
        last = self.db.hl_latest_snapshot()
        if force or last is None or (snap["ts"] - float(last["ts"])) >= self.p.snapshot_min_interval_s:
            self.db.hl_add_snapshot(snap)
        return snap

    # ------------------------------------------------------------ 对账
    def reconcile(self, snap: Optional[dict] = None,
                  kill_before: Optional[bool] = None) -> dict:
        """以交易所为权威核对本地状态。返回 {"ok":bool,"problems":[str,...]}。

        ``kill_before``：**本轮开始前**的 kill 状态（见 ``_run_locked``）。数量对账的
        fail-closed 用它判断「要不要动手」—— 进来时就已 kill 说明人工正在核对，
        这边再去改 kill/推告警只会互相打架（与 ``_repair_missing_stop`` 同一口径）。
        """
        snap = snap if snap is not None else self._snapshot()
        if snap is None:
            return {"ok": False, "problems": ["读不到账户状态（info 失败）"], "unknown": True,
                    "fill_closed": [], "closing_pending": []}
        ex_pos = {p["coin"]: p for p in snap.get("positions") or []}
        # 挂单列表可信才允许判断「止损有无」：回落老接口时读不到 isTrigger，会把有止损读成没止损，
        # 据此自动补挂/平仓 = 误平仓。缺字段按**不可信**处理 —— 与面板（views）和看护
        # （watchdog）同口径（fail-closed）：唯一生产来源 info.snapshot 总会显式写这个键，
        # 缺键只可能出现在桩/半成品数据里，那种情况宁可只告警、不自动动仓。
        trusted = bool(snap.get("orders_trusted", False))
        problems: List[str] = []
        missing_stop: List[dict] = []
        unconfirmed: List[str] = []
        fill_closed: List[dict] = []
        closing_pending: List[dict] = []
        # 本轮已被某个仓消耗掉的成交 tid（M5）：一笔成交只能关一个仓，见 _fill_close_proof。
        consumed_tids: set = set()
        local_open = [r for r in self.db.recent_positions(50)
                      if r["status"] in ("open", "pending")
                      # hl_status 为 NULL 也算：on_entry 在「入场成交→落库」之间崩溃时
                      # 仓位行还没来得及写 hl_status='open'，这样的仓同样需要止损保护/对账，
                      # 否则只能走到 kill 分支当「人工单」处理。
                      # 审计三轮 M4：status='pending' 也纳入 —— on_entry 在「交易所成交
                      # → confirm_position」之间崩溃时行还是 pending，真实裸仓以前只会被
                      # 打成「人工单」kill+告警，永不补挂止损。
                      # 审计四轮 P1：pending 行**必须真的发过入场单**才算真实仓位。
                      # on_entry 在读数据阶段就退出（快照/规格/mid 失败 → entry 行记成
                      # skipped）时交易所什么都没有，无条件纳入会让下一轮对账报
                      # 「本地有仓但交易所无仓位」→ 误置 kill + 误标 mismatch。
                      and (r["status"] == "open" or self._entry_issued(r))
                      # 宽限期（closing）：**必须**每轮重试 —— 它正是「交易所无仓、
                      # 成交证据还没到」那个状态，不纳入就永远等不到证据（也永远
                      # 不会超时升级），宽限期就成了「永久挂起」。
                      and (r["hl_status"] or "") in ("open", "mismatch", "", HL_ST_CLOSING)]
        seen = set()
        size_problems: List[str] = []
        size_warn: List[str] = []
        for r in local_open:
            # coin 解析不出（未注册）：无法与交易所仓比对，也不许拿猜的名字去补挂/平仓
            # → 记成问题（fail-closed，走既有告警/停手口径），但不抛异常打断整轮对账。
            coin = self._coin_or_none(r["symbol"])
            if coin is None:
                problems.append(f"未配置 HL coin 映射：{r['symbol']}（本地仓 #{r['id']}，无法对账）")
                continue
            seen.add(coin)
            if coin not in ex_pos:
                # 交易所侧没有这个仓 **不等于** 本地记错了：挂单止损被触发成交后，
                # _sync_fills 只写 hl_fills，仓位/挂单状态要等人或 4h 收盘才更新 ——
                # 旧逻辑在这里直接判 mismatch 并 kill（2026-10-22 事故：止损已成交，
                # 本地被误判成「账实不符」并停了自动交易）。
                # 所以先用**本轮已同步的平仓成交**证明「这仓确实被交易所平掉了」：
                # 证据成立 → 按真实成交价关账 + 挂单落终态 + 提醒，且不 kill；
                # 证据不成立（无成交/数量不足/精度不可知/开关关）→ 严格退回原行为。
                proof = self._fill_close_proof(
                    r, snap, consumed_tids,
                    allow_refetch=self._allow_refetch(r))
                if proof is not None:
                    # 「宽限期内终于拿到证据」与「第一轮就有证据」走同一条关账路径，
                    # 唯一的区别是要把宽限键删掉（否则下一轮还会按「仍在宽限」拦开仓）。
                    was_closing = (r["hl_status"] or "") == HL_ST_CLOSING
                    self._close_local(int(r["id"]), proof["px"], proof["reason"], proof["sz"])
                    # 这批成交已归属本仓：同轮后面的行不得再拿它关账（M5）。
                    consumed_tids.update(proof.get("tids") or [])
                    self._mark_closing_order_filled(r, proof)
                    # 人工/外部平仓：那张**没成交**的 reduce-only 止损还挂在交易所上，
                    # 必须搬掉，否则会咬下一笔开仓（见 _cancel_residual_stops）。
                    # reason=='stop' 不走这里：那张单就是成交的那张，撤它没有意义。
                    if proof["reason"] == "exchange":
                        self._cancel_residual_stops(r, coin)
                    self._grace_clear(int(r["id"]))
                    self._event(EV_ORDER,
                                f"{r['symbol']} 交易所侧已平仓（{proof['reason']}），"
                                f"本地按成交价 {proof['px']:.2f} 关账"
                                + (f"（证据来源 {proof.get('source')}）" if proof.get("source") else ""),
                                symbol=r["symbol"], data=proof)
                    fill_closed.append({"position_id": int(r["id"]), "symbol": r["symbol"],
                                        "coin": coin, "grace_ended": was_closing, **proof})
                    continue                       # 不进 problems、不 kill
                # 证据不足。宽限期开着 → 先挂起（closing，不 kill），超时才升级；
                # 关掉 → 完全回到「立刻 mismatch + kill」的旧行为（一格都不改）。
                pend = self._grace_step(r, problems)
                if pend is not None:
                    closing_pending.append(pend)
                continue
            # 快照抖动（对抗审查 M2）：上一轮交易所没有这个仓（已进宽限），这一轮又出现了
            # —— 那不是「被平掉了」，只是快照抖了一下。必须**把宽限时钟清零**：否则键里的
            # first_seen 还在往前走，等它下一次真消失时 elapsed 已接近上限，第一轮就直接
            # 升级成 mismatch + kill（把一次抖动当成了真矛盾），而这一轮明明已经证明仓还在。
            if (r["hl_status"] or "") == HL_ST_CLOSING:
                self._grace_clear(int(r["id"]))
                self.db.set_pos_hl(int(r["id"]), hl_status="open")
                self._event(EV_RECONCILE,
                            f"{r['symbol']} 交易所侧仓位重新出现（快照抖动），"
                            f"平仓宽限期作废、下一轮重新计时",
                            symbol=r["symbol"], data={"pid": int(r["id"]), "coin": coin})
            # 数量对账（PR-3）：以前只比「有没有仓」，数量错了（部分成交、IOC 只成一半、
            # 手工加减仓）会一路带进 PnL。两击规则见 _size_check。
            # 传 dict(r)：_size_* 里用 .get()，而 sqlite3.Row 没有 get（会 AttributeError）。
            if tx := self._size_check(dict(r), ex_pos[coin], kill_before=kill_before):
                (size_problems if tx["kill"] else size_warn).append(tx["message"])
            if not self._has_stop(snap, coin):
                if trusted:
                    problems.append(f"{r['symbol']} 有仓但**没有止损挂单**")
                    missing_stop.append(dict(r))
                else:
                    unconfirmed.append(f"{r['symbol']} 有仓但无法确认止损"
                                       f"（挂单接口回落，读不到触发单）")
        for coin in ex_pos:
            if coin not in seen:
                problems.append(f"交易所有 {coin} 仓位但本地无记录（人工单？）")
        # 数量对账的「要不要动作」在 _size_check 里已经判过 kill_before：
        # kill 分支并入 problems（走既有告警口径），真正置 kill 放在这里一次完成 ——
        # 于是直接调 reconcile() 的路径（agent 到期保护等）也同样是 fail-closed，
        # 不依赖调用方是否走了 _run_locked。
        problems.extend(size_problems)
        if size_problems:
            if kill_before is None:
                kill_before = bool(self.db.hl_kill())
            if not kill_before:
                # origin='size'：数量对账造成的 kill **不**自动解除 —— 它代表
                # 「本地记的量与交易所不一致」，自动解等于把一个还在的矛盾静音。
                self.db.hl_set_kill(True, "持仓数量对账连续不一致", origin="size")
                self._event(EV_KILL, "持仓数量对账连续不一致 → 自动 kill switch",
                            data={"problems": size_problems})
        # 清掉已经不在 open 列表里的 strike 键：仓位关掉后没人会再走「数量一致」分支，
        # 留着就是内存泄漏（长期运行的容器会慢慢攒，每标的一行）。
        self._strike_prune({int(r["id"]) for r in local_open})
        return {"ok": not problems, "problems": problems, "unknown": False,
                "missing_stop": missing_stop, "unconfirmed": unconfirmed,
                "size_warn": size_warn, "size_kill": bool(size_problems),
                "fill_closed": fill_closed, "closing_pending": closing_pending}

    # ------------------------------------------------------------ 交易所侧已平仓（成交证据）
    def _fill_close_proof(self, row, snap: Optional[dict] = None,
                          consumed: Optional[set] = None,
                          allow_refetch: bool = True) -> Optional[dict]:
        """证明该本地仓已在交易所侧被平掉：返回 {"px","sz","reason","tids","oids","source"} 或 None。

        **v2（对账失败自动消化）**：先用本轮已同步的 `hl_fills` 判；不够（无平仓腿 /
        数量不符）才做**一次定点回捞**（`user_fills_by_time` 从 opened_at−120s 起，
        最多 2 次、间隔 1s，落库后重判）。回捞只在「证据不足」的那一轮发生 —— 证据
        齐全时一次多余的接口都不打（这条约束比「多捞几次更保险」重要：每轮都打就是
        在限频窗口里给自己找 429，最后连对账本身都读不到数）。

        判定口径（每一格都是「不成立就不许动账」）：
        - **平仓腿**：`reduce_only==1` **或** dir 含 "Close" **或** `closedPnl != 0`。
          最后一条兜 ADL/清算：那种成交的 dir 可能不是 Close，但 closedPnl 一定非 0。
        - **oid 锚定优先**：`hl_stop_oid` 非空、且窗口里真有该 oid 的平仓腿 → **只用
          这些成交**做合计与均价，reason="stop"；否则用全部平仓腿，reason="exchange"。
          （旧版要求「所有腿都出自止损单」才算 stop，窗口里混进别的仓的人工平仓腿时
          会把整笔判成 exchange 并撤掉止损 —— 归因错了，数字还可能被混合。）
        - **双侧容差**：`abs(total − qty) <= tol`，tol = 1 数量 tick；tick 读不到时
          用 `qty × 1e-6`（浮点往返量级，**不猜精度**）。不再只有下界 —— 超额平仓
          同样不是这一仓的证据（详见 _judge_close_fills）。
        - `consumed`（同轮 tid 复用）保留：一笔成交只能关一个仓。

        任一条件不满足 → None（fail-closed：调用方进宽限期，超时才 mismatch + kill）。
        """
        if not bool(getattr(self.p, "fill_close", True)):
            return None
        if snap is None:                       # 没有快照就证明不了「交易所无仓」
            return None
        symbol = str(row["symbol"])
        coin = self._coin_or_none(symbol)
        if coin is None:                       # 认不出合约 → 不可能把成交归对
            return None
        ex_coins = {p.get("coin") for p in (snap or {}).get("positions") or []}
        if coin in ex_coins:                   # 交易所仍有仓 → 这不是「已平仓」
            return None
        # 持仓量：hl_sz 是执行器记的真实数量；缺失（老行/半截写入）才回落确认页的
        # actual_contracts。两者都没有 = 不知道平多少才算平完，直接放弃证明。
        qty = self._num(row["hl_sz"])
        if qty <= 0:
            qty = self._num(row["actual_contracts"])
        if qty <= 0:
            return None
        # 成交时间下界：仓位开仓时刻（缺 opened_at 用 created_at）。不设下界就会把
        # 同一标的**上一仓**的历史平仓成交当成这一仓的平仓证据 —— 那是凭空的关账。
        opened = row["opened_at"] if row["opened_at"] is not None else row["created_at"]
        if opened is None:
            return None
        lo = float(opened) - 120.0             # 120s 容差：成交 ts 与本地 opened_at 有秒级漂移
        # ① 本轮已同步的成交（零额外接口）
        proof = self._judge_close_fills(row, symbol, coin, qty, lo, consumed)
        if proof is not None:
            return {**proof, "source": "cache"}
        # ② 不足 → 定点回捞。最多 2 次、每次之后重判（拿到就停，不为「保险」多打一次）。
        #    `allow_refetch=False`（宽限已经进行到后半程，见 _allow_refetch）→ 只认 cache：
        #    这一轮立刻按「无证据」返回，交给宽限期继续等下一轮的 _sync_fills。
        if not allow_refetch:
            logger.info("[hl-exec] %s 平仓证据不足，宽限后半程不再回捞（只读本地成交）",
                        symbol)
            return None
        self._event(EV_RECONCILE,
                    f"{symbol} 平仓证据不足 → 定点回捞成交（最多 {REFETCH_ATTEMPTS} 次）",
                    symbol=symbol, data={"pid": int(row["id"]), "coin": coin, "qty": qty})
        for attempt in range(REFETCH_ATTEMPTS):
            if attempt:
                time.sleep(REFETCH_GAP_S)
            if not self._refetch_close_fills(opened):
                continue                       # 空/异常 = 无证据，继续下一次（不抛）
            proof = self._judge_close_fills(row, symbol, coin, qty, lo, consumed)
            if proof is not None:
                return {**proof, "source": "refetch"}
        return None

    def _judge_close_fills(self, row, symbol: str, coin: str, qty: float, lo: float,
                           consumed: Optional[set] = None) -> Optional[dict]:
        """按**本地已落库**的成交判一次证据（纯读，不打接口）。不成立返回 None。

        单独抽出来的原因：v2 要在「回捞前/回捞后」用**同一套**判据各判一次 ——
        两处各写一遍必然漂移，而这里漂移一格就是「凭一批对不上的成交把仓关掉」。
        """
        try:
            fills = self.db.hl_fills_window(coin, lo=lo)
        except Exception as e:                                        # noqa: BLE001
            logger.warning("[hl-exec] 读取成交失败，本轮不做已平仓证明：%s", e)
            return None
        # 容差：1 个数量 tick；精度读不到（specs 失败/桩）→ 用浮点往返量级，
        # **不猜**一个 szDecimals（猜错差 100 倍，两种结论都不可信）。
        tol = self._size_tick(coin)
        if tol is None:
            tol = qty * 1e-6
        legs: List[dict] = []
        for f in fills:
            sym = f["symbol"]
            if sym and sym != symbol:          # coin 相同但 symbol 不同（映射变更）→ 不算
                continue
            if not self._is_closing_leg(f):
                continue
            sz, px = self._num(f["sz"]), self._num(f["px"])
            if sz <= 0 or px <= 0:
                continue
            legs.append({"sz": sz, "px": px, "oid": f["oid"], "tid": f["tid"]})
        if not legs:
            return None
        # oid 锚定：本仓止损单打出的那些腿**优先**，命中就只用它们（归因 + 均价都干净）；
        # 没命中才把全部平仓腿当证据，归因为「人工/外部平仓」。
        reason = "exchange"
        stop_oid = row["hl_stop_oid"]
        if stop_oid not in (None, ""):
            anchored = [c for c in legs
                        if c["oid"] is not None and str(c["oid"]) == str(stop_oid)]
            if anchored:
                legs = anchored
                reason = "stop"
        # 同一轮里这批成交已被别的仓用掉了 → 不重复归因（M5）。否则两个同 coin 的
        # 本地 open 行会各自拿**同一批**成交关账，一个真实仓位被抹掉两次。
        if consumed:
            used = {str(c["tid"]) for c in legs}
            if used & set(consumed):
                return None
        total = sum(c["sz"] for c in legs)
        # **双侧**容差：少于持仓量 = 只平了一部分（交易所还有残余量，按均价关整仓会把
        # 剩余敞口从账本里抹掉）；多于持仓量 = 窗口里混进了不属于这一仓的成交
        # （照 2×持仓算下去会把仓关在一个既不是止损价也不是任何成交价的混合均价上，
        # 凭空造一笔巨亏）。两种都不是证据 → None，fail-closed。
        if abs(total - qty) > tol:
            return None
        px = sum(c["px"] * c["sz"] for c in legs) / total
        return {"px": px, "sz": total, "reason": reason,
                "tids": [str(c["tid"]) for c in legs],
                "oids": sorted({str(c["oid"]) for c in legs if c["oid"] is not None})}

    def _is_closing_leg(self, f) -> bool:
        """这笔成交是不是**平仓腿**（v2 三判据取或）。

        ADL / 强制清算的 dir 写法不一定是 "Close"（HL 里可能是 "Auto-Deleveraging"
        之类），但 `closedPnl` 一定非 0 —— 只看 dir 会把「交易所确实平了仓」的证据
        漏掉，然后白白进宽限、超时 kill。
        反过来，dir 里写着 "Open" 就是**开仓腿**，一律不算（把开仓腿算成平仓腿 =
        用开仓价把仓关掉，凭空造一笔 R=0 的平仓，比不关账更糟）。最后一条判据依赖
        closedPnl 非 0，而个别交易所/桩数据会给开仓腿也带上非 0 的 closedPnl，
        所以先用 "Open" 把这条路封死。
        """
        d = str(f["dir"] or "")
        if "Open" in d:
            return False
        try:
            if int(f["reduce_only"] or 0) == 1:
                return True
        except (TypeError, ValueError, KeyError, IndexError):
            pass
        if "Close" in d:
            return True
        return abs(self._num(f["closed_pnl"])) > 0.0

    def _refetch_close_fills(self, opened) -> int:
        """定点回捞该账户从 opened_at−120s 起的成交并落库；返回新增条数（失败/空 = 0，不抛）。

        与 `_refetch_fills`（数量对账那条）的区别只在窗口起点与用途：那条从
        opened_at−300s 起、为了重算流水净量；这条从 −120s 起，只服务「这仓是不是
        被交易所平掉了」。两者都不新增**每轮**调用 —— 只在各自的证据不足分支里打。
        """
        if not self.p.account_address:
            return 0
        try:
            start_ms = int(float(opened) * 1000) - REFETCH_MARGIN_MS
        except (TypeError, ValueError):
            return 0
        fn = getattr(self.info, "user_fills_by_time", None)
        if callable(fn):
            try:
                got = fn(self.p.account_address, start_ms) or []
            except Exception as e:                                     # noqa: BLE001
                # 回捞是**修复手段**：它失败只能说明这次没拿到证据，绝不能把整轮对账带崩
                # （那才是更危险的连锁反应）。按「无证据」处理，交给宽限期。
                logger.warning("[hl-exec] 定点回捞成交失败（按无证据处理）：%s", e)
                return 0
        else:
            # 桩/老客户端没有 userFillsByTime：退回滑动窗口（userFills）再按 opened_at 过滤。
            # 生产 info 一定有 by_time 版本；这条只为「直接调 reconcile（未经 run 的同步）」
            # 和测试桩留一条可用的回捞路径，避免它们被误判成「无证据」而进宽限。
            logger.info("[hl-exec] 无 userFillsByTime，回捞退回 userFills 滑动窗口")
            try:
                got = self.info.user_fills(self.p.account_address) or []
            except Exception as e:                                     # noqa: BLE001
                logger.warning("[hl-exec] 退回 userFills 回捞失败（按无证据处理）：%s", e)
                return 0
        if not isinstance(got, list):
            return 0
        rows = [x for x in got if isinstance(x, dict)]
        if not rows:
            return 0
        try:
            return int(self._sync_fills(limit=len(rows), fills=rows) or 0)
        except Exception as e:                                         # noqa: BLE001
            logger.warning("[hl-exec] 回捞成交落库失败（按无证据处理）：%s", e)
            return 0

    # ------------------------------------------------------------ 宽限期（closing）
    def _grace_key(self, pid: int) -> str:
        return f"{GRACE_KEY_PREFIX}{int(pid)}"

    def _grace_s(self) -> float:
        """生效宽限秒数；脏值/越界退回默认 900（不静默关功能、也不放大成一整天）。"""
        try:
            v = float(getattr(self.p, "close_grace_s", GRACE_DEFAULT_S))
        except (TypeError, ValueError):
            return GRACE_DEFAULT_S
        if not (GRACE_MIN_S <= v <= GRACE_MAX_S):
            logger.warning("[hl-exec] close_grace_s=%.0f 越界（允许 %g–%g），按 %.0f 处理",
                           v, GRACE_MIN_S, GRACE_MAX_S, GRACE_DEFAULT_S)
            return GRACE_DEFAULT_S
        return v

    def _grace_read(self, pid: int) -> Optional[dict]:
        """读该仓的宽限状态 {"first_seen","rounds","escalated"}；没有/读不懂 → None（不抛）。

        新格式 `<first_seen>:<rounds>:<escalated>`；**旧格式**（只有两段，宽限期第一版
        写进生产库的那种）第三段按 0 解析 —— 升级状态只对「本版之后写入的行」有意义，
        不能让老键在升级后读不出来而重新进宽限（那就成了 M1 的同一个坑）。
        """
        try:
            raw = str(self.db.get_config_str(self._grace_key(pid), "") or "").strip()
        except Exception as e:                                         # noqa: BLE001
            logger.warning("[hl-exec] 读宽限状态失败（按未进入宽限处理）：%s", e)
            return None
        if not raw:
            return None
        parts = raw.split(":")
        if len(parts) < 2:
            return None
        try:
            first, rounds = float(parts[0]), int(float(parts[1] or 0))
        except (TypeError, ValueError):
            return None
        if first <= 0:
            return None
        escalated = 0
        if len(parts) >= 3:
            try:
                escalated = GRACE_ESCALATED if int(float(parts[2] or 0)) else 0
            except (TypeError, ValueError):
                escalated = 0
        return {"first_seen": first, "rounds": max(0, rounds), "escalated": escalated}

    def _grace_write(self, pid: int, first_seen: float, rounds: int,
                     escalated: int = 0) -> None:
        try:
            self.db.set_config(self._grace_key(pid),
                               f"{float(first_seen):.3f}:{int(rounds)}:{int(escalated)}")
        except Exception as e:                                         # noqa: BLE001
            logger.warning("[hl-exec] 写宽限状态失败（本轮按未进入宽限处理）：%s", e)

    def _grace_clear(self, pid: int) -> None:
        """删掉宽限键（成交证据到位 / 快照抖动 / 关账都必须删）。

        **超时升级不删**（见 `_grace_step`）：那一支改写 escalated 标记，否则下一轮
        又当成「第一次进入宽限」（对抗审查 M1 的无限循环）。

        写法上做了一次兼容：`del_config` 是本轮新加的 db 方法，注入的替身 DB
        （测试/脚本）可能没有 —— 退化成写空串。空串在 `_grace_read` 里同样等于
        「没有宽限状态」，语义一致，不会因为少一个方法就把宽限期卡死。
        """
        try:
            fn = getattr(self.db, "del_config", None)
            if callable(fn):
                fn(self._grace_key(pid))
            else:
                self.db.set_config(self._grace_key(pid), "")
        except Exception as e:                                         # noqa: BLE001
            logger.warning("[hl-exec] 清宽限状态失败（下一轮会按已超时处理）：%s", e)

    def _allow_refetch(self, row) -> bool:
        """本轮是否允许定点回捞成交（对抗审查 L2）。

        宽限期前几轮值得回捞（时滞通常就一两轮），但**每轮都捞**是每轮多打两次
        `userFillsByTime`：宽限期一长（我们允许到 1 小时），等于在限频窗口里主动
        给自己攒 429，最后连对账本身都读不到数。所以第 `GRACE_REFETCH_MAX_ROUND` 轮
        之后只读本地 cache（每轮 `_sync_fills` 仍在拉 userFills，cache 并不是死的）。
        不在宽限里的行（第一轮就想证明）一律允许 —— 这时回捞正是该做的事。
        """
        if (row["hl_status"] or "") != HL_ST_CLOSING:
            return True
        try:
            st = self._grace_read(int(row["id"]))
        except Exception:                                              # noqa: BLE001
            return True
        if not st:
            return True
        return int(st["rounds"]) < GRACE_REFETCH_MAX_ROUND

    def _grace_step(self, row, problems: List[str]) -> Optional[dict]:
        """证据不足时的宽限期一步：进入 / 继续 / 超时升级 / 已升级再报。返回 closing_pending 条目或 None。

        四种情形（规格 §2 + 对抗审查 M1/L3）：
        - 宽限开关关 → **完全**回到旧行为（problem + mismatch + kill），一格都不改；
        - 键不存在 → 写 first_seen、置 hl_status='closing'、记「进入宽限」事件、不 kill；
        - 键存在未超时 → 只累计轮次，不 kill（**不重复推**「进入宽限」）；
        - 键存在已超时 → 升级：problem（→ kill）+ mismatch + **写 escalated 标记**
          （**不删键**：删了下一轮就又「第一次进入宽限」，见下方 M1 注释）；
        - 键已带 escalated → 只报 problem + mismatch，不重新计时、不重新推送。
        """
        pid = int(row["id"])
        symbol = str(row["symbol"])
        # `fill_close` 是**最外层**开关：关掉它 = 上一轮上线的全部自动关账能力都回旧行为，
        # 宽限自然也不该独自生效（否则「回滚」只回滚了一半，账本仍会被自动改写）。
        if not (bool(getattr(self.p, "fill_close", True))
                and bool(getattr(self.p, "close_grace", True))):
            problems.append(f"{symbol} 本地有仓（#{pid}）但交易所无仓位")
            self.db.set_pos_hl(pid, hl_status="mismatch")
            return None
        grace_s = self._grace_s()
        now = time.time()
        st = self._grace_read(pid)
        if st is None:
            self._grace_write(pid, now, 1)
            self.db.set_pos_hl(pid, hl_status=HL_ST_CLOSING)
            self._event(EV_RECONCILE,
                        f"{symbol} 平仓成交待核对，进入宽限（最多 {grace_s:g} 秒）",
                        symbol=symbol, data={"pid": pid, "grace_s": grace_s})
            # 只在这一支推送（同一仓第二次进来 rounds>0，不会再走到这里）：
            # 用户需要知道「交易已经平了、账在核对中」，而不是从面板自己去猜状态词。
            self._alert(f"【HL执行】⏳ {symbol} 已探测到交易所无仓，平仓成交待核对 —— "
                        f"已进入宽限（最多 {int(grace_s // 60)} 分钟，自动重试；"
                        f"期间该标的暂不开新仓）")
            return {"position_id": pid, "symbol": symbol, "first_seen": now,
                    "elapsed_s": 0.0, "rounds": 1, "new": True, "grace_s": grace_s}
        first = float(st["first_seen"])
        rounds = int(st["rounds"])
        # L3 时钟回拨：first_seen 在未来（容器重启/宿主机校时/换时区之后）→ elapsed 恒为
        # 0，这行会永远停在 closing（既不超时也不恢复）。判定状态不可信 → 从此刻重新计时，
        # 留一条事件让人能查。
        if first - now > GRACE_CLOCK_ROLLBACK_S:
            self._event(EV_RECONCILE,
                        f"{symbol} 宽限起点在 {first - now:.0f} 秒后的未来（时钟回拨），"
                        f"重置为当前时间重新计时",
                        symbol=symbol, data={"pid": pid, "first_seen": first, "now": now})
            first, rounds = now, max(1, rounds)
            self._grace_write(pid, first, rounds)
        # M1：已经升级过 → 只报、不再进宽限。以前超时删键，下一轮又「第一次进入宽限」，
        # 于是 mismatch/closing 每 15 分钟翻一次、每轮推一条、还把 6h 节流清零。
        if int(st.get("escalated") or 0):
            elapsed = max(0.0, now - first)
            self.db.set_pos_hl(pid, hl_status="mismatch")
            problems.append(f"{symbol} 已探测到交易所无仓，已等待 {elapsed:.0f} 秒仍未拿到"
                            f"成交证据（{rounds} 轮重试，此前已升级为对账异常）")
            return None
        rounds += 1
        elapsed = max(0.0, now - first)
        if elapsed < grace_s:
            self._grace_write(pid, first, rounds)
            if (row["hl_status"] or "") != HL_ST_CLOSING:
                # 行可能是上一版逻辑留下的 open/mismatch：宽限期内统一钉成 closing，
                # 否则面板/看护/开仓闸门读到的状态与事实（正在等证据）不一致。
                self.db.set_pos_hl(pid, hl_status=HL_ST_CLOSING)
            return {"position_id": pid, "symbol": symbol, "first_seen": first,
                    "elapsed_s": elapsed, "rounds": rounds, "new": False, "grace_s": grace_s}
        # 超时：升级成真矛盾 → 交人（problem → _run_locked 里 kill）。
        # **不删键**（M1）：改写成 escalated=1，让这个仓稳定停在「已升级」，而不是下一轮
        # 再从零开始等 900 秒；证明后来齐了的话，关账路径会顺手把键清掉。
        self._grace_write(pid, first, rounds, escalated=GRACE_ESCALATED)
        self.db.set_pos_hl(pid, hl_status="mismatch")
        # 文案用**真实 elapsed**（M2 配套）：硬写「N 分钟内」在快照抖过、时钟被重置过
        # 的情况下会与事实不符（明明才等 30s 却写「15 分钟内」），事后复盘会被误导。
        msg = (f"{symbol} 已探测到交易所无仓，已等待 {elapsed:.0f} 秒"
               f"（宽限上限 {grace_s:g} 秒）仍未拿到成交证据（{rounds} 轮重试）")
        problems.append(msg)
        self._event(EV_RECONCILE, msg + " → 升级为对账异常（停手等人工核对）", symbol=symbol,
                    data={"pid": pid, "grace_s": grace_s, "rounds": rounds,
                          "elapsed_s": elapsed, "escalated": True})
        return None

    def closing_symbols(self) -> set:
        """当前处于宽限期（hl_status='closing'）的标的集合。

        开仓闸门与面板共用这一份读法：宽限期间**禁止该标的开新仓** —— 上一笔平仓
        的成交还没核对完就再开一笔，一旦那笔的成交后来被归到新仓上，两个仓的归属
        就再也分不开了（成交只能按 coin + 时间窗归因）。
        """
        out = set()
        try:
            for r in self.db.recent_positions(50):
                if (r["hl_status"] or "") == HL_ST_CLOSING and r["status"] in ("open", "pending"):
                    out.add(str(r["symbol"]))
        except Exception as e:                                         # noqa: BLE001
            logger.warning("[hl-exec] 读 closing 行失败（本轮不拦开仓）：%s", e)
        return out

    def _mark_closing_order_filled(self, row, proof: dict) -> Optional[dict]:
        """把产生这笔平仓的挂单行落终态（否则面板/看护会一直报「卡单」）。

        幂等：已经 filled/cancelled 的行不再改；一行都没命中只记事件、不报错 ——
        这条路径是在「账已经对上了」之后跑的，不能因为挂单表缺行就把关账回滚。
        """
        symbol = str(row["symbol"])
        bar_ms = int(row["signal_bar_ms"] or 0)
        cands: List[dict] = []
        for purpose in (PURPOSE_STOP, PURPOSE_STOP + ":fix", PURPOSE_EXIT_REHANG):
            o = self.db.hl_order(symbol, bar_ms, purpose)
            # 已落终态的行不再改（重复调用无害，也不把 cancelled 的止损「复活」）
            if o is not None and o["status"] not in (ST_FILLED, ST_CANCELLED, ST_FAILED,
                                                     ST_SKIPPED):
                cands.append(dict(o))
        # 再扫最近挂单：止损可能在别的 bar 补挂（stop:fix 记在原 bar，exit:rehang 在
        # 离场 bar），只看本 bar 会漏。只取仍「活着」的行（resting/sent）——已经
        # cancelled/filled 的行不该被这笔平仓再改一次。
        try:
            for o in self.db.hl_recent_orders(100):
                d = dict(o)
                if d["symbol"] != symbol:
                    continue
                if d["purpose"] not in (PURPOSE_STOP, PURPOSE_STOP + ":fix", PURPOSE_EXIT_REHANG):
                    continue
                if d["status"] not in (ST_RESTING, ST_SENT):
                    continue
                if any(c["id"] == d["id"] for c in cands):
                    continue
                cands.append(d)
        except Exception as e:                                        # noqa: BLE001
            logger.warning("[hl-exec] 扫最近挂单失败（忽略）：%s", e)
        oids = {str(o) for o in (proof.get("oids") or [])}
        hit = None
        if oids:
            for c in cands:
                if c["oid"] is not None and str(c["oid"]) in oids:
                    hit = c
                    break
        # 没有 oid 证据时**一行都不落**（对抗审查 M1）。曾经这里有「恰好一行候选就落」
        # 的兜底，但候选来自 hl_recent_orders(100) 的**全标的、跨 bar** 扫描 ——
        # 「恰好一行」完全可能是别的仓/别的 bar 上那张**根本没成交**的止损，把它写成
        # filled 会让面板/看护以为「这个仓的止损已成交」，同时掩盖真正卡住的那张单。
        # 而升级前已同步的成交 oid 恒为 NULL（hl_fills.tid UNIQUE + INSERT OR IGNORE，
        # 迁移只补列不回填），这条兜底在真实老库上会真的命中 → 必须删掉，只记事件。
        if hit is None:
            self._event(EV_ORDER,
                        f"{symbol} 已按成交价关账，但没定位到对应挂单行（oid 不明/无候选）",
                        symbol=symbol, data={"oids": sorted(oids), "candidates": len(cands)})
            return None
        self.db.hl_update_order(hit["symbol"], int(hit["bar_ms"]), hit["purpose"],
                                status=ST_FILLED, filled_sz=proof["sz"], avg_px=proof["px"])
        return hit

    def _cancel_residual_stops(self, row, coin: str) -> None:
        """人工/外部平仓后，撤掉交易所侧**残留**的 reduce-only 止损单。

        为什么必须动这一手（对抗自查 B）：``_fill_close_proof`` 判定 reason=='exchange'
        的依据是「这笔平仓成交的 oid 不是本仓的 hl_stop_oid」—— 也就是说那张 reduce-only
        止损**没有成交**、此刻仍挂在账户上。仓位已经没了、触发单还在，它对下一次开仓
        是致命的：同一 coin 再开仓时它会被触发，把新仓直接平掉（等于用上一笔的挂单毁掉
        下一笔交易），而本地账本会以为新仓有保护。
        reason=='stop' 绝不进来：那张单本身就是成交的那张，既不该撤也无从撤。

        只撤本地库里有**明确 oid**、状态仍是 resting/sent 的 stop / stop:fix / exit:rehang
        行：没有 oid 就不知道撤哪张，退化成 cancel_all 会把别的仓的保护单一起撤掉。
        不新增每轮交易所调用 —— 只有「这一轮真的判定为已平仓」才会发 cancel。

        失败一律 fail-closed：行保持原状 + EV_ERROR + 一条告警喊人。撤不掉可能只是
        接口抖动/单已不在场，但对账户来说「还在不在」读不准，宁可喊人确认，也不能
        静默留一张幽灵触发单。
        """
        symbol = str(row["symbol"])
        purposes = (PURPOSE_STOP, PURPOSE_STOP + ":fix", PURPOSE_EXIT_REHANG)
        cands: List[dict] = []
        for purpose in purposes:
            try:
                o = self.db.hl_order(symbol, int(row["signal_bar_ms"] or 0), purpose)
            except Exception as e:                                    # noqa: BLE001
                logger.warning("[hl-exec] 读挂单行失败（%s/%s）：%s", symbol, purpose, e)
                continue
            if o is not None:
                cands.append(dict(o))
        scan_failed = None
        try:
            # 止损可能在**别的 bar** 上补挂（stop:fix 记原 bar，exit:rehang 记离场 bar），
            # 只按本 bar 三元键取一条会漏 —— 与 _mark_closing_order_filled 同口径。
            for o in self.db.hl_recent_orders(100):
                d = dict(o)
                if d["symbol"] != symbol or d["purpose"] not in purposes:
                    continue
                if any(c["id"] == d["id"] for c in cands):
                    continue
                cands.append(d)
        except Exception as e:                                        # noqa: BLE001
            scan_failed = str(e)
            logger.warning("[hl-exec] 扫挂单表失败（残留止损可能漏撤）：%s", e)

        failed: List[str] = []
        for c in cands:
            # 只碰「活着」且有 oid 的行：已 filled/cancelled 的行没有可撤的东西，
            # 没有 oid 的行不知道撤哪张。
            if c["status"] not in (ST_RESTING, ST_SENT) or not c["oid"]:
                continue
            oid = int(c["oid"])
            try:
                res = self.ex.cancel(coin, oid)
            except Exception as e:                                    # noqa: BLE001
                res = {"ok": False, "error": f"撤单异常：{e}"}
            if bool(res.get("ok")):
                self.db.hl_update_order(symbol, int(c["bar_ms"]), c["purpose"],
                                        status=ST_CANCELLED)
                self._event(EV_ORDER,
                            f"{symbol} 人工/外部平仓后撤掉残留止损单 {oid}（{c['purpose']}）",
                            symbol=symbol, data={"oid": oid, "purpose": c["purpose"]})
                continue
            err = str(res.get("error") or "未知错误")[:300]
            # 行**不**改状态（仍 resting）：本地认为它可能还在场上，看护/面板照旧会提，
            # 人工核对时不会被「已撤」骗过去。
            self.db.hl_update_order(symbol, int(c["bar_ms"]), c["purpose"], error=err)
            self._event(EV_ERROR,
                        f"{symbol} 已判定人工/外部平仓，但残留止损单 {oid} 撤销失败（{err}）"
                        f"→ 行保持 {c['status']}，需人工撤单",
                        symbol=symbol, data={"oid": oid, "purpose": c["purpose"], "error": err})
            failed.append(f"{oid}（{err}）")

        if failed:
            self._alert(f"【HL执行】⚠️ {symbol} 已在交易所被平仓（人工/外部），"
                        f"但残留的 reduce-only 止损单撤不掉：{'；'.join(failed)}。"
                        f"该单若仍在场上，下次开仓可能被它平掉 —— 请到 HL 手动撤单并核对。",
                        symbol=symbol)
        elif scan_failed:
            self._alert(f"【HL执行】⚠️ {symbol} 已按成交价关账，但挂单表读取失败"
                        f"（{scan_failed[:120]}），无法确认是否还残留 reduce-only 止损单 —— "
                        f"请到 HL 看一眼挂单。", symbol=symbol)

    # ------------------------------------------------------------ 数量对账（PR-3）
    def _tick_bucket(self) -> Optional[Dict[str, Tuple[float, float]]]:
        """取本 info 的 tick 缓存桶（M6）。info 不可弱引用 → 返回 None（不缓存）。"""
        try:
            bucket = _TICK_CACHE.get(self.info)
            if bucket is None:
                bucket = {}
                _TICK_CACHE[self.info] = bucket
            return bucket
        except TypeError:            # 桩/带 __slots__ 的实现不可弱引用：退化成不缓存
            return None

    def _size_tick(self, coin: str) -> Optional[float]:
        """该合约 1 tick 的数量（10^-szDecimals）。读不到规格返回 None → 由调用方跳过。

        不猜精度：拿一个默认 szDecimals 去当容差，等于用一个可能差 100 倍的尺子判
        「数量一致/不一致」，两种结论都不可信。

        读到的值按 (info, coin) 进 1 小时 TTL 缓存（见 SPEC_TICK_TTL_S）：这一步是真实
        POST，且只在「证据不足/对账」分支才走 —— 不缓存就是执行器坏着每 5 分钟打一次。
        """
        bucket = self._tick_bucket()
        now = time.time()
        if bucket is not None:
            hit = bucket.get(coin)
            if hit is not None and hit[0] > now:
                return hit[1]
        try:
            specs = self.info.specs([coin]).get(coin) or {}
            dec = specs.get("sz_decimals")
            if dec is None:
                return None                      # 规格缺字段：不缓存，下一轮再试
            tick = 10.0 ** -int(dec)
        except (HlInfoError, TypeError, ValueError) as e:              # noqa: BLE001
            logger.warning("[hl-exec] 数量对账读不到 %s 规格，本轮跳过：%s", coin, e)
            return None                          # 失败不缓存（抖动不能钉成一小时的错答案）
        if bucket is not None:
            bucket[coin] = (now + SPEC_TICK_TTL_S, tick)
        return tick

    def _agg_window(self, coin: str, opened_at: Any, closed_at: Any,
                    side: Any) -> Dict[str, Any]:
        """按时间窗读出该标的成交并聚合（读库失败 → 空聚合，不抛）。"""
        lo, hi = hl_pnl.window_bounds(opened_at, closed_at)
        fills: List[Any] = []
        if lo is not None or opened_at is not None:
            try:
                fills = self.db.hl_fills_window(coin, lo, hi)
            except Exception as e:                                    # noqa: BLE001
                logger.warning("[hl-exec] 取成交失败（按无成交处理）：%s", e)
        return hl_pnl.aggregate_fills(fills, opened_at, closed_at, coin, side)

    def _size_agg(self, row: dict, coin: str) -> Optional[dict]:
        """本地 open 仓的成交流水聚合（窗口 = opened_at−300s 至今）。

        「至今」而不是到某个 closed_at：仓位还开着，出场腿随时可能只有一半。
        查不到任何成交返回 None（没有依据就不判 —— 首次同步还没跑完时不该报不一致）。
        """
        if row.get("opened_at") is None:
            return None
        agg = self._agg_window(coin, row.get("opened_at"), None, row.get("side"))
        return agg if agg["n_fills"] > 0 else None

    def _size_check(self, row: dict, ex_pos: dict,
                    kill_before: Optional[bool] = None) -> Optional[dict]:
        """本地开仓 vs 交易所仓位的**数量**对账，两击规则。返回 None = 一切正常。

        规则（为什么是两击）
        -------------------
        数量差可能来自「成交还没同步完」「回捞窗口漏了一笔」，也可能真的错了。第一次
        超差只做**一次全量回捞**（userFillsByTime 从 opened_at−300s 起）并重算 ——
        实测多数差异是本地流水缺腿，回捞即自愈；回捞后仍超差才计一击，连续两击才
        fail-closed（等价于 _repair_missing_stop 的「先修、修不好才停手」）。
        这样一次同步延迟不会把执行器打成 kill（误停的代价：错过合法信号 + 人工介入）。

        kill_before：**本轮开始前**的 kill 状态。已置位说明人工正在核对，这里只提示、
        不再触发 kill（判据用「本轮开始前」而不是「现在」，理由同 _repair_missing_stop）。
        """
        coin = self._coin_or_none(row["symbol"])
        pid = int(row["id"])
        if coin is None:
            return None                                   # 上层已记 problem，不重复
        tick = self._size_tick(coin)
        if tick is None:
            return None                                   # 规格不可读 → 本轮不判（下轮重试）
        agg = self._size_agg(row, coin)
        if agg is None:
            return None                                   # 没有任何成交：无从判断

        def _diff(a: dict) -> float:
            return abs(hl_pnl.net_size(a) - float(ex_pos.get("size") or 0.0))

        def _within(a: dict) -> bool:
            net = hl_pnl.net_size(a)
            # 容差 = 1 tick + 相对噪声（float→SQLite REAL 往返在数量大时会掉末位）
            noise = 1e-9 * max(abs(net), abs(float(ex_pos.get("size") or 0.0)))
            return _diff(a) <= tick + noise

        key = f"{pid}:{coin}"
        if _within(agg):
            self._strike_clear(key)
            return None

        # 第一次超差：先回捞一次，别急着记账
        first = _diff(agg)
        self._event(EV_RECONCILE,
                    f"{row['symbol']} 数量对账首次超差：流水净量 {hl_pnl.net_size(agg):g} vs "
                    f"交易所 {float(ex_pos.get('size') or 0.0):g}（差 {first:g}，tick {tick:g}）"
                    f" → 全量回捞重算",
                    symbol=row["symbol"],
                    data={"pid": pid, "coin": coin, "flow_sz": hl_pnl.net_size(agg),
                          "ex_sz": ex_pos.get("size"), "diff": first, "tick": tick})
        refetched = self._refetch_fills(row.get("opened_at"))
        if refetched:
            agg2 = self._size_agg(row, coin)
            if agg2 is not None and _within(agg2):
                # 回捞修好了差异：清零 strike，本轮不采取任何动作
                self._strike_clear(key)
                self._event(EV_RECONCILE,
                            f"{row['symbol']} 数量对账经回捞后一致"
                            f"（{hl_pnl.net_size(agg2):g} vs {float(ex_pos.get('size') or 0.0):g}），"
                            f"strike 已清零", symbol=row["symbol"])
                return None
            if agg2 is not None:
                agg = agg2

        strikes = self._strike_add(key)
        detail = (f"{row['symbol']} 持仓数量对账不一致（第 {strikes} 次）："
                  f"流水净量 {hl_pnl.net_size(agg):g} vs 交易所 "
                  f"{float(ex_pos.get('size') or 0.0):g}（差 {_diff(agg):g} > tick {tick:g}）")
        if strikes < SIZE_DIFF_STRIKES_TO_KILL:
            self._event(EV_RECONCILE, detail + f"，未达 {SIZE_DIFF_STRIKES_TO_KILL} 次，继续观察",
                        symbol=row["symbol"],
                        data={"pid": pid, "strikes": strikes, "flow_sz": hl_pnl.net_size(agg),
                              "ex_sz": ex_pos.get("size")})
            return {"kill": False, "message": detail}

        # 连续两击 → fail-closed。置 kill 的动作留在 reconcile 里统一做（一处写、
        # 一处追责），这里只把「该停手」这个结论交上去 —— 于是直接调 reconcile 的
        # 路径（agent 到期保护等）也同样是 fail-closed，不依赖走没走 _run_locked。
        # kill_before 已置位：人工在处理，只提示不动它（判据用本轮开始前的值）。
        if kill_before is None:
            kill_before = bool(self.db.hl_kill())
        if kill_before:
            self._event(EV_DECISION, detail + "；kill 早已置位，本轮不重复触发（等人工核对）",
                        symbol=row["symbol"])
            return {"kill": False, "message": detail}
        self._event(EV_ERROR,
                    detail + f" → 连续 {strikes} 次不一致，fail-closed（暂停自动交易，等人工核对）",
                    symbol=row["symbol"],
                    data={"pid": pid, "strikes": strikes, "flow_sz": hl_pnl.net_size(agg),
                          "ex_sz": ex_pos.get("size"), "tick": tick})
        return {"kill": True, "message": detail + f" → 连续 {strikes} 次不一致，已 fail-closed"}

    # strike 计数（进程内存，见类属性注释）
    def _strike_clear(self, key: str) -> None:
        with self._size_strikes_lock:
            self._size_strikes.pop(key, None)

    def _strike_add(self, key: str) -> int:
        with self._size_strikes_lock:
            self._size_strikes[key] = int(self._size_strikes.get(key, 0)) + 1
            return self._size_strikes[key]

    def _strike_prune(self, alive_pids: set) -> None:
        """丢掉已关仓位（不在本轮 open 列表里）的 strike 键，避免进程内存无界增长。"""
        with self._size_strikes_lock:
            for key in [k for k in self._size_strikes if int(k.split(":")[0]) not in alive_pids]:
                self._size_strikes.pop(key, None)

    def _refetch_fills(self, since_ts: Any) -> int:
        """从 opened_at−300s 起全量回捞该账户成交并落库（按 tid 幂等）。

        与 `_sync_fills` 的区别：后者只取最近 100 笔（`userFills` 的滑动窗口），
        持仓跨日/多标的并行时**入场腿早就滑出去了**，回捞窗口必须显式从开仓前取。
        失败返回 0（不抛）：回捞失败只是「这次没修好」，不该打断整轮对账。
        """
        if not self.p.account_address:
            return 0
        try:
            since = float(since_ts)
        except (TypeError, ValueError):
            return 0
        start_ms = int((since - hl_pnl.ENTRY_LOOKBACK_S) * 1000)
        rows: List[dict] = []
        try:
            fn = getattr(self.info, "user_fills_by_time", None)
            if callable(fn):
                got = fn(self.p.account_address, start_ms) or []
            else:                                        # 桩/老客户端：退回滑动窗口
                got = self.info.user_fills(self.p.account_address) or []
            if isinstance(got, list):
                rows = [x for x in got if isinstance(x, dict)]
        except Exception as e:                                         # noqa: BLE001
            # 广口径兜住：回捞是**修复手段**，它失败只能说明这次没修好，绝不能把
            # 整轮对账/熔断/心跳带崩（那才是最危险的连锁反应）。
            logger.warning("[hl-exec] 全量回捞成交失败（忽略）：%s", e)
            return 0
        if not rows:
            return 0
        try:
            return self._sync_fills(limit=len(rows), fills=rows)
        except Exception as e:                                         # noqa: BLE001
            logger.warning("[hl-exec] 回捞成交落库失败（忽略）：%s", e)
            return 0


    # ------------------------------------------------------------ 闸门（PR-4）
    def _gate_key(self, coin: str, side: Any) -> Tuple[str, str, int]:
        """闸门状态的键：**(账本路径, coin, side)**（见 `__init__` 里的 _gate_scope）。"""
        return (self._gate_scope, str(coin), int(side if side is not None else 1))

    def _gate_act(self, action: str, coin: str, side: Any,
                  symbol: Optional[str] = None) -> bool:
        """动作前过闸的统一入口：True = 放行；False = 拦下（已记事件，调用方直接 return）。

        每个动作路径都调它，而不是各自 `if settle or churn: ...` —— 见 _gate_check
        的注释（逐处写必然漂移，漏一处就是陈旧状态下的下单，多处一处就是裸仓）。
        """
        verdict, reason = self._gate_check(action, coin, side, symbol=symbol)
        if verdict == GATE_OK:
            return True
        self._gate_event(action, coin, side, verdict, reason, symbol=symbol)
        return False

    def _churn_note(self, coin: str, side: Any) -> int:
        """一次 cancel/replace 动作**真的发出去之后**记数（返回窗口内次数）。

        只在动作真的发出后记：闸门拦下的（没发出去的）动作不算 churn —— 否则
        「被熔断拦住」会继续给熔断加数，窗口永远滑不出去，等于永久停手。
        """
        return self._churn_record(coin, side)

    def _settle_arm(self, coin: str, side: Any) -> None:
        """入场**成交回报**后给该 coin-side 上 15s 静置窗口（PR-4 任务 1）。

        只由成交回报调用（`_on_entry_locked` 拿到 STATUS_FILLED 之后）。写入是
        「覆盖成 now+15s」而不是「累加」：即便同一秒来了两笔成交（部分成交分两笔），
        解禁时间也只是往后推到「最后一笔成交 + 15s」，**不会**因为别的事件无限延长。
        """
        key = self._gate_key(coin, side)
        try:
            with self._settle_lock:
                self._settle_until[key] = time.time() + SETTLE_WINDOW_S
        except Exception as e:                                       # noqa: BLE001
            # 闸门是**附加保护**：内存状态写失败不能反过来把开仓/平仓带崩（fail-open 到
            # 「没有闸门」，而不是「永远开着闸门」—— 后者会把所有动作永久拦死）。
            logger.warning("[hl-exec] 静置闸门写入失败（本轮不设闸）：%s", e)

    def _settle_remaining(self, coin: str, side: Any) -> float:
        """该 coin-side 的静置剩余秒数；已过期（或从没设过）返回 0 并顺手清键。

        过期即清：不清的话 `_settle_until` 会随标的数量缓慢增长（长期运行的容器里
        每个 coin-side 一条，永不回收）。**只读，绝不续期** —— 这是「不可续期」的实现点：
        没有任何代码路径会用「当前时刻」重写一个更晚的 deadline，除了下一次成交回报。
        """
        key = self._gate_key(coin, side)
        now = time.time()
        try:
            with self._settle_lock:
                deadline = self._settle_until.get(key)
                if deadline is None:
                    return 0.0
                left = float(deadline) - now
                if left <= 0:
                    self._settle_until.pop(key, None)
                    return 0.0
                return left
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 静置闸门读取失败（按无闸放行）：%s", e)
            return 0.0

    def _churn_record(self, coin: str, side: Any, now: Optional[float] = None) -> int:
        """记一次 cancel/replace 动作，返回**窗口内**累计次数。

        窗口 = [now − CHURN_WINDOW_S, now]：滑出窗口的记录当场丢掉（计数永远只反映
        「最近 10 分钟真发生了多少次撤挂」），于是熔断会随着窗口滑动**自动恢复**，
        不需要任何人工解锁动作（也能避免「忘了清熔断导致永久停手」）。
        """
        key = self._gate_key(coin, side)
        ts = time.time() if now is None else float(now)
        lo = ts - CHURN_WINDOW_S
        try:
            with self._churn_lock:
                hits = [t for t in (self._churn_hits.get(key) or []) if t > lo]
                hits.append(ts)
                self._churn_hits[key] = hits
                return len(hits)
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] churn 计数失败（按未超限放行）：%s", e)
            return 0

    def _churn_count(self, coin: str, side: Any, now: Optional[float] = None) -> int:
        """窗口内已发生的 cancel/replace 次数（只读，不记新动作）。"""
        key = self._gate_key(coin, side)
        ts = time.time() if now is None else float(now)
        lo = ts - CHURN_WINDOW_S
        try:
            with self._churn_lock:
                hits = [t for t in (self._churn_hits.get(key) or []) if t > lo]
                if hits:
                    self._churn_hits[key] = hits
                else:
                    self._churn_hits.pop(key, None)
                return len(hits)
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] churn 读取失败（按未超限放行）：%s", e)
            return 0

    def _churn_limit(self) -> int:
        """生效阈值：<=0 = 熔断关闭（只留静置闸门）。配置脏值时退回默认，不静默关掉保护。"""
        try:
            n = int(getattr(self.p, "churn_max", CHURN_MAX_DEFAULT))
        except (TypeError, ValueError):
            return CHURN_MAX_DEFAULT
        return n

    def _churn_tripped(self, coin: str, side: Any) -> bool:
        limit = self._churn_limit()
        return limit > 0 and self._churn_count(coin, side) >= limit

    # ------------------------------------------------------------ 只读闸门状态（/why）
    def gate_state(self, coin: str, side: Any = 1) -> Dict[str, Any]:
        """该 coin-side 的闸门状态快照（`/why` 诊断用，**纯只读**）。

        只回答「现在被哪道闸挡着」，绝不做任何动作、不记事件、不告警、不置 kill：
        诊断接口会在面板每次刷新时被调用，任何副作用（哪怕只是记一条事件）都会被
        调成新的噪音源。

        实现上**复用 `_settle_remaining` / `_churn_count`**（不是重算一遍时间差），
        原因：这两个函数各自带锁、带「过期即清」的回收逻辑，且与 `_gate_check` 读的是
        同一份类属性状态 —— 另写一份读法就会出现「诊断说还剩 3s、闸门其实已放行」这种
        最误导人的情形。二者的共同点是都只**收紧垃圾**（清过期键），不续期、不加数：

        - `_settle_remaining` 过期即 pop（注释已写明「只读，绝不续期」）；
        - `_churn_count` 只把滑出窗口的旧记录丢掉（窗口语义本来就该这样）。
        """
        left = float(self._settle_remaining(coin, side) or 0.0)
        n = int(self._churn_count(coin, side) or 0)
        limit = int(self._churn_limit())
        # 熔断阈值 <=0 = 熔断关闭（只留静置闸门），此时 tripped 恒 False 且文案要说清
        tripped = bool(limit > 0 and n >= limit)
        if tripped:
            verdict = GATE_CHURN
        elif left > 0:
            verdict = GATE_SETTLE
        else:
            verdict = GATE_OK
        return {
            "coin": str(coin),
            "side": int(side if side is not None else 1),
            "settle_remaining_s": round(left, 1),
            "churn_count": n,
            "churn_limit": limit,
            "churn_tripped": tripped,
            "verdict": verdict,
            "settle_window_s": SETTLE_WINDOW_S,
            "churn_window_s": CHURN_WINDOW_S,
        }

    @classmethod
    def gate_state_for(cls, db, coin: str, side: Any = 1,
                       churn_max: Optional[int] = None) -> Dict[str, Any]:
        """**不建实例**读闸门状态（面板/诊断用）。

        build_executor() 每轮 tick 新建实例、且装配失败会返回 None（mode=off），可面板
        只想知道「现在有没有闸」—— 它不该因为没装配执行器就拿不到答案。闸门状态全部是
        类属性，所以这里造一个只带 `_gate_scope` 与 `p.churn_max` 的轻量载体，
        真正读值的仍是 `gate_state` → `_settle_remaining`/`_churn_count`
        （同一份实现、同一把锁，没有第二条读法）。

        `_gate_scope` 必须与生产执行器一致（= db.path），否则同一个库里两套键互不相干。
        """
        probe = cls.__new__(cls)
        probe._gate_scope = str(getattr(db, "path", "") or "")
        probe.p = SimpleNamespace(churn_max=(CHURN_MAX_DEFAULT if churn_max is None
                                            else int(churn_max)))
        return probe.gate_state(coin, side)

    def _gate_check(self, action: str, coin: str, side: Any,
                    symbol: Optional[str] = None) -> Tuple[str, str]:
        """动作前的统一闸门。返回 (verdict, reason)，verdict ∈ {ok, settle, churn}。

        为什么是一张表而不是每处各写一遍判断：动作点有 5 处（见各调用点的 act=），
        逐处写必然漂移 —— 某个点漏了静置、另一个点把首次止损也拦了，前者制造陈旧
        状态下的动作，后者制造裸仓。类别 → 是否过闸的映射只在本函数一处。

        **保护动作零延迟**：首次止损挂单与平仓直接 return ok，连内存都不读
        （不在闸门内做任何可能抛异常的事）。
        """
        if action in _GATE_NONE_ACTIONS:
            return GATE_OK, ""
        limit = self._churn_limit()
        # ① churn：先看次数（已熔断就没必要再谈静置 —— 停手是更强的结论）
        if limit > 0:
            n = self._churn_count(coin, side)
            if n >= limit:
                self._churn_alert_once(coin, side, n, limit, symbol)
                return GATE_CHURN, (f"churn 熔断：10 分钟内已 {n} 次撤单/重挂"
                                    f"（阈值 {limit}），本 coin-side 修复类动作暂停到窗口滑出")
        # ② 静置：只对「新开仓 / 修复类补挂」查。
        #    **离场路径的重挂不查静置**：那条路径进到这里时止损**已经被撤掉**（平仓
        #    IOC 没成交），再等 15s 就是纯裸仓 —— 延迟它等于用闸门制造风险。它的
        #    频率问题由 churn 熔断管（见上），那一条不会推迟任何动作，只会把
        #    「重挂」换成「市价平」（两者都是保护动作）。
        if action in (ACT_ENTRY, ACT_REPAIR):
            left = self._settle_remaining(coin, side)
            if left > 0:
                # 修复类**首次**保护动作豁免：本 coin-side 最近 10 分钟还没有任何
                # 撤/挂动作（churn=0），说明这次补挂是「仓刚成交、止损没挂上」的第一手
                # 保护 —— 拦它等于裸仓等 15s。要拦的是「补挂重试」：第二次起说明
                # 前一次已经发过单、只是快照还没刷新，重复发只会挂出重复止损。
                if not (action == ACT_REPAIR and self._churn_count(coin, side) == 0):
                    return GATE_SETTLE, (f"静置中（成交回报后 {SETTLE_WINDOW_S:g}s 窗口，"
                                         f"剩余 {left:.0f}s）：暂不做重复动作")
        return GATE_OK, ""

    def _churn_alert_once(self, coin: str, side: Any, n: int, limit: int,
                          symbol: Optional[str] = None) -> bool:
        """churn 熔断告警：**同一窗口内只推一次**（返回是否本次真推了）。

        每轮 tick 都会重新过闸，不去重就是 5 分钟一条、直到窗口滑出 —— 把真正需要
        人看的「止损挂不上」淹掉。窗口滑出后计数归零，下次再熔断会重新告警。
        """
        key = self._gate_key(coin, side)
        now = time.time()
        try:
            with self._churn_lock:
                last = self._churn_alerted.get(key)
                if last is not None and now - float(last) < CHURN_WINDOW_S:
                    return False
                self._churn_alerted[key] = now
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] churn 告警去重状态读写失败（照常告警）：%s", e)
        side_word = _side_word(int(side or 1))
        self._event(EV_DECISION,
                    f"{symbol or coin} {side_word} churn 熔断：10 分钟内 {n} 次撤单/重挂"
                    f"（阈值 {limit}）→ 暂停修复类动作，等窗口滑出（不置 kill）",
                    symbol=symbol,
                    data={"coin": coin, "side": int(side or 1), "hits": n, "limit": limit,
                          "window_s": CHURN_WINDOW_S})
        self._alert(f"【HL执行】⚠️ {coin} {side_word} 撤单/重挂过于频繁"
                    f"（10 分钟内 {n} 次，阈值 {limit}）—— 已暂停该标的的修复类动作"
                    f"（补挂/复价），等 10 分钟窗口滑出后自动恢复。\n"
                    f"平仓与首次止损挂单不受影响；kill 未置位（这不是对账异常）。"
                    f"若持续出现，多半是止损单被交易所拒单，请看一眼交易所。")
        return True

    def _gate_event(self, action: str, coin: str, side: Any, verdict: str,
                    reason: str, symbol: Optional[str] = None) -> None:
        """闸门拦截留痕。**同 coin-side 每轮 tick 最多一条**（轮次标记见 _gate_new_round）。"""
        if not self._gate_event_allowed(coin, side):
            return
        left = self._settle_remaining(coin, side)
        self._event(EV_DECISION, f"{symbol or coin} {_side_word(side)} 动作被闸门拦下"
                                 f"（{action}）：{reason}（剩余 {left:.0f}s）",
                    symbol=symbol,
                    data={"coin": coin, "side": int(side or 1), "action": action,
                          "verdict": verdict, "remaining_s": round(left, 1)})

    def _gate_event_allowed(self, coin: str, side: Any) -> bool:
        """限频：同 coin-side 在**同一轮 tick** 内只记一条闸门事件。

        实现用一个进程内「轮次号 + 已记过的键集合」，轮次号在 `run()` 每次进入时自增、
        也由 on_entry/on_exit 各自新开一轮 —— 一次 tick 里可能连续撞多次闸（补挂重试
        每轮一次、开仓一次），全记就会把 15s 窗口刷成 10 条同样的行。
        """
        key = self._gate_key(coin, side)
        try:
            with self._gate_ev_lock:
                if self._gate_ev_round.get(key) == self._gate_round_no:
                    return False
                self._gate_ev_round[key] = self._gate_round_no
                return True
        except Exception:                                            # noqa: BLE001
            return True                                              # 记不上就照常记

    def _gate_new_round(self) -> None:
        """开一轮（tick / on_entry / on_exit）：闸门事件限频按轮重置。"""
        try:
            with self._gate_ev_lock:
                self._gate_round_no += 1
        except Exception:                                            # noqa: BLE001
            pass

    def _repair_missing_stop(self, row: dict, snap: dict,
                             kill_before: Optional[bool] = None) -> None:
        """**确认**有仓却没有止损挂单 → 先自动补挂原止损价；补不上就市价平掉。

        与开仓路径同一口径：不允许无保护持仓存在。用独立 cloid（stop:fix）避免与原单撞号。
        补挂成功也不解锁 kill —— 异常已经发生，由人工核对后决定是否恢复自动交易。
        """
        symbol = str(row.get("symbol"))
        # 清仓进行中（agent 到期保护）→ 绝不补挂/平仓：清仓刚撤完单，这里再挂回去
        # 就是「一边清一边补」，最后清仓复查会一直看到有仓，来回打。
        from app.hl import agent as _agent_mod
        if _agent_mod.cleanup_in_progress():
            self._event(EV_DECISION, f"到期清仓进行中：{symbol} 暂不补挂止损", symbol=symbol)
            return
        # 本轮开始前 kill 就已经置位（人工触发/熔断/上次对账异常）→ 不动交易所：
        # 此时人工正在核对，自动补挂/市价平会与人的操作打架。
        # 注意判据是「**本轮开始前**」而不是「现在」—— 本轮对账发现「有仓没止损」
        # 会先把 kill 拉起来，再走这个补挂修复；拿现在的值判会把修复彻底堵死
        # （那就成了「有裸仓也不修」）。见 _run_locked 传入的 kill_before。
        if kill_before is None:
            kill_before = bool(self.db.hl_kill())
        if kill_before:
            # 【v2·高3】`origin='dd'`（回撤熔断）**不得**阻断止损补挂：「停开新仓」与
            # 「保护现有仓」是两件事。裸奔仓的风险远大于「熔断期间多挂一个保护单」——
            # 止损单是**减风险**动作，不是加仓动作，永远应该被允许。
            # v1 在这里无差别早退：实测「有仓 + 缺止损 + origin='dd'」→ 交易所写操作
            # 为空 → 裸奔仓不修不平，而 dd 永不自解 → 永远没人来修。
            try:
                origin = str(self.db.hl_kill_origin() or "")
            except Exception:                                       # noqa: BLE001
                origin = ""
            if origin != "dd":
                self._event(EV_DECISION, f"kill 已触发：{symbol} 暂不自动补挂止损", symbol=symbol)
                return
            self._event(EV_DECISION,
                        f"回撤熔断期间仍补挂止损：{symbol}（减风险动作不受停手限制）",
                        symbol=symbol)
        # 映射缺失 → 不知道交易所上的标的，**绝不动交易所**（补挂/平仓都不做），只提示
        coin = self._coin_or_none(symbol)
        if coin is None:
            self._event(EV_ERROR, f"未配置 HL coin 映射：{symbol}，不自动补挂止损", symbol=symbol)
            self._alert(f"【HL执行】⛔ {symbol} 未配置 HL coin 映射，无法自动补挂止损/平仓"
                        f"（fail-closed）—— 请人工核对交易所并处理。")
            return
        bar_ms = int(row.get("signal_bar_ms") or 0)
        side = int(row.get("side") or 1)
        # 非 live 模式绝不动交易所：dry 的 place_order 返回 STATUS_DRY（非
        # RESTING/FILLED），按失败处理会误走「补挂失败→市价平仓」分支，
        # 推出吓人且失实的告警（实际什么都没发）。dry 下只提示人工。
        if not self.p.live:
            self._event(EV_ERROR, f"dry 模式：{symbol} 止损缺失，不动交易所，仅提示",
                        symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 交易所仓位缺止损单，但执行器不是 live 模式，"
                        f"未自动处理 —— 请人工查看并处理。")
            return
        stop = float(row.get("stop_price") or 0.0)
        sz = float(row.get("hl_sz") or 0.0)
        if sz <= 0:                                   # 本地没记数量 → 以交易所为准
            ex_pos = {p["coin"]: p for p in (snap.get("positions") or [])}
            sz = abs(float((ex_pos.get(coin) or {}).get("size") or 0.0))
        if stop <= 0 or sz <= 0:
            self._event(EV_ERROR, f"{symbol} 止损丢失且参数不全（stop={stop} sz={sz}），未动作",
                        symbol=symbol)
            self._alert(f"【HL执行】🛑 {coin} 止损单丢失，但本地缺少止损价/数量，**未能自动处理**，"
                        f"请立刻人工查看交易所持仓！")
            return
        try:
            sz_dec = int((self.info.specs([coin]).get(coin) or {}).get("sz_decimals", 5))
        except HlInfoError:
            sz_dec = 5
        px = hl_sizing.round_price(stop, sz_dec)
        # 闸门（PR-4）：修复类补挂是「重复动作」，要过静置 + churn。
        #   静置（成交回报后 15s）→ **只延迟**，绝不升级成平仓：刚成交那 15s 里
        #   「补挂」与「已挂好但快照还没刷新」无法区分，据此平仓等于每笔成交都平一次。
        #     例外：churn=0 的**首次**保护补挂不过静置（见 _gate_check），否则就是裸仓等 15s。
        #   churn 熔断（10 分钟 N 次）→ 停补挂，但仓位不能裸着 → 直接走下面的市价平仓
        #   （保护动作不过闸）。这正是本 PR 要打断的「撤-挂-撤-挂」循环的出口。
        verdict, reason = self._gate_check(ACT_REPAIR, coin, side, symbol=symbol)
        if verdict != GATE_OK:
            self._gate_event(ACT_REPAIR, coin, side, verdict, reason, symbol=symbol)
            if verdict == GATE_SETTLE:
                return                            # 等 15s 窗口过去，本轮不动交易所
            res = {"ok": False, "status": "gated",
                   "error": f"churn 熔断（{reason}）：停止补挂"}
        else:
            # 计数在**真正发单之前**记：修复尝试本身就是 churn（挂不上的止损被反复
            # 重挂正是要熔断的对象）。放在成功之后记会让「一直失败」永远数不到阈值。
            self._churn_note(coin, side)
            try:
                res = self.ex.place_order(coin, side != 1, sz, px, reduce_only=True,
                                          trigger_px=px,
                                          cloid=self._attempt_cloid(symbol, bar_ms,
                                                                   PURPOSE_STOP + ":fix"))
            except Exception as ex:                                 # noqa: BLE001
                res = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        if res.get("ok") and res.get("status") in (STATUS_RESTING, STATUS_FILLED):
            self.db.hl_update_order(symbol, bar_ms, PURPOSE_STOP, status=res["status"],
                                    oid=res.get("oid"), error=None)
            self.db.set_pos_hl(int(row["id"]), hl_stop_oid=res.get("oid"))
            self._event(EV_ORDER, f"{symbol} 止损丢失 → 已自动补挂 @ {px:,.2f}", symbol=symbol)
            self._alert(f"【HL执行】🔧 {coin} 止损单丢失，已自动补挂（原价 {px:,.2f}，"
                        f"新单号 {res.get('oid')}，{sz} {coin}）。自动交易已暂停（kill），"
                        f"核对后可恢复。")
            return
        # 补挂失败 → 无保护持仓不允许存在 → 市价平掉
        self._event(EV_ERROR, f"{symbol} 止损补挂失败（{res.get('error')}）→ 立即平仓",
                    symbol=symbol, data={"place": res})
        try:
            limit_px = hl_sizing.round_price(self.ex.slippage_limit(coin, side != 1), sz_dec)
        except Exception as ex:                                     # noqa: BLE001
            limit_px = float((snap.get("positions") or [{}])[0].get("mark_px") or px)
            logger.warning("[hl-exec] 平仓限价计算失败（用标价）：%s", ex)
        try:
            close = self.ex.place_order(coin, side != 1, sz, limit_px, reduce_only=True)
        except Exception as ex:                                     # noqa: BLE001
            close = {"ok": False, "status": "error", "error": f"{type(ex).__name__}: {ex}"}
        ok_close = bool(close.get("ok")) and close.get("status") == STATUS_FILLED
        if ok_close:
            self._close_local(int(row["id"]), float(close.get("avg_px") or limit_px),
                              "stop_missing", sz)
        self._alert(f"【HL执行】🛑 {coin} 止损单丢失且补挂失败（{res.get('error')}），"
                    f"已{'市价平仓' if ok_close else '尝试平仓但结果异常'}（{close.get('status')}）。"
                    f"**请立刻人工核对交易所**！")

    @staticmethod
    def _has_stop(snap: dict, coin: str) -> bool:
        for o in snap.get("open_orders") or []:
            if o.get("coin") == coin and o.get("reduce_only") and o.get("is_trigger"):
                return True
        return False

    # ------------------------------------------------------------ 闸门
    def _entry_gates(self, symbol: str, plan, snap: dict, equity: Optional[float] = None) -> List[str]:
        p = self.p
        bad: List[str] = []
        # ① coin 映射（第一道显性闸门）：未注册/未配置 → 拒绝开仓。
        #    没有映射就没法定位交易所标的，影子模式也不能静默走「猜一个前缀」的旧路。
        coin = self._coin_or_none(symbol)
        if coin is None:
            bad.append(f"未配置 HL coin 映射：{symbol}（拒绝开仓）")
        # 宽限期（closing）：同标的有「平仓成交待核对」的行 → 直接拦，不依赖仓位槽。
        # 理由：那一笔的成交还没归属确定，此时再开一笔同标的的仓，两边成交在
        # (coin, 时间窗) 上就混在一起了 —— 关账会归错仓、数量对账也会来回抖。
        # 它只持续到证据到齐（或超时升级），不是长期停手。
        if symbol in self.closing_symbols():
            bad.append(f"{symbol} 平仓成交待核对，暂不开仓")
        if self.db.hl_kill():
            bad.append("kill switch 已触发")
        # 闸门（PR-4）：新开仓属于「重复动作」——同一 coin-side 刚有成交回报（15s 静置）
        # 或正在撤挂风暴里（churn 熔断）时都不许再开。放这里（而不是 on_entry 里另写一段）
        # 是因为这条路径已经有统一的「拒绝 → 落 skipped 意图 + 告警」出口，闸门拒绝与
        # 其它闸门拒绝落库口径一致，事后复盘只有一处可查。
        if coin is not None:
            verdict, reason = self._gate_check(ACT_ENTRY, coin, int(getattr(plan, "side", 1) or 1),
                                              symbol=symbol)
            if verdict != GATE_OK:
                self._gate_event(ACT_ENTRY, coin, int(getattr(plan, "side", 1) or 1), verdict,
                                 reason, symbol=symbol)
                bad.append(f"闸门拦截开仓：{reason}")
        if self.p.live and not self.ex.available:
            # 只在 live 校验写通道：dry 期不下单、不需要私钥（否则「先跑影子不用给私钥」就不成立）
            bad.append(f"写通道不可用：{self.ex.init_error or '未配置凭据'}")
        if plan.below_min_notional:
            bad.append(f"名义 {plan.notional:.1f}U 低于交易所下限 {p.min_notional:g}U")
        eq = float(equity if equity is not None else p.equity)
        total = float((snap or {}).get("notional") or 0.0)
        if p.max_notional_mult and p.max_notional_mult > 0:          # 0 = 不设限（默认）
            cap = eq * p.max_notional_mult
            if plan.notional > cap:
                bad.append(f"名义 {plan.notional:.0f}U 超过单标的上限 {cap:.0f}U")
        if p.max_total_notional_mult and p.max_total_notional_mult > 0:
            if total + plan.notional > eq * p.max_total_notional_mult:
                bad.append(f"总名义将达 {total + plan.notional:.0f}U，超过上限 "
                           f"{eq * p.max_total_notional_mult:.0f}U")
        if plan.stop_dist > p.stop_max_hard:
            bad.append(f"执行时止损距离 {plan.stop_dist * 100:.2f}% 超过硬上限 {p.stop_max_hard * 100:g}%")
        if plan.leverage > plan.max_safe_leverage:
            bad.append(f"杠杆 {plan.leverage:g}x 超过安全上限 {plan.max_safe_leverage:.1f}x")
        today = _today_start_ts()
        entries_today = len([r for r in self.db.hl_recent_orders(100)
                             if r["purpose"] == PURPOSE_ENTRY and float(r["created_at"]) >= today
                             and r["status"] in (ST_SENT, ST_RESTING, ST_FILLED)])
        if entries_today >= p.max_orders_per_day:
            bad.append(f"当日下单已达 {entries_today} 次（上限 {p.max_orders_per_day}）")
        fails = len([r for r in self.db.hl_recent_orders(50)
                     if r["status"] == ST_FAILED and time.time() - float(r["created_at"]) < 3600])
        if fails >= p.max_fail_streak:
            bad.append(f"近一小时失败 {fails} 次（上限 {p.max_fail_streak}）")
        for coin_pos in (snap or {}).get("positions") or []:
            # coin 解析不出时不做仓位查重（比对不了），但上面的映射闸门已经拦下来了
            if coin is not None and coin_pos["coin"] == coin:
                bad.append(f"交易所已有 {coin} 仓位，拒绝重复开仓")
        return bad

    # ------------------------------------------------------------ 开仓
    def _current_closed_bar_ms(self, symbol: str) -> int:
        """当前最后一根已收盘 4h bar 的开盘时刻（平仓 cloid 用）。

        取 bar_state 里引擎已处理的进度；读不到时按当前时间对齐 4h 网格推导。
        返回 0 表示完全无法确定（调用方回落入场 bar）。
        """
        try:
            last = self.db.get_last_bar(symbol)
            if last:
                return int(last)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            now_ms = int(time.time() * 1000)
            return now_ms - (now_ms % (4 * 3600 * 1000))
        except Exception:                                        # noqa: BLE001
            return 0

    def _attempt_cloid(self, symbol: str, bar_ms: int, purpose: str) -> str:
        """重试类下单的 cloid：在 (symbol, bar, purpose) 之外再掺一个**尝试时刻**。

        HL 的 cloid 账户级唯一（重复 cloid 直接被拒，官方文档亦然）。只按
        (symbol, bar, purpose) 推导时，同一个 4h 内第二次尝试会拿到与第一次**完全
        相同**的 cloid：平仓重试被拒 → 而重试的第一步已经撤掉了止损 = 裸仓
        （审计四轮 P2，复现见 tests/test_audit4_p1.py）。这里掺进当前秒数：
        - 两次真实尝试至少隔一个引擎 tick（30 分钟），秒数必不同 → 永不撞号；
        - 同一秒内的「同一次尝试重放」（崩溃后立刻重跑）会拿到同一个值，
          保留幂等语义（且每次尝试含多次网络往返 + _op_lock 串行，撞秒实际不可达）。
        DB 里记的 cloid 与发给交易所的严格同源（都用这个函数）。
        """
        return make_cloid(symbol, bar_ms, f"{purpose}@{int(time.time())}")

    def _entry_issued(self, row) -> bool:
        """该 pending 行是否**真的向交易所发过**入场单。

        只有「已发出/在场/已成交」三种状态才算真实仓位（成交后崩溃的那种裸仓
        正是 sent/resting/filled）；没发过（intent/skipped/无记录）的 pending 行
        是「信号到了但本轮没能下单」，交易所侧什么都没有，不该当仓位对账。
        读不到记录时按真实仓位处理（fail-closed：宁可多对一次账，不漏保护）。
        """
        try:
            o = self.db.hl_order(str(row["symbol"]), int(row["signal_bar_ms"] or 0),
                                 PURPOSE_ENTRY)
        except Exception:                                        # noqa: BLE001
            return True
        if o is None:
            return False
        return (o["status"] or "") in (ST_SENT, ST_RESTING, ST_FILLED)

    def on_entry(self, position_id: int) -> bool:
        """接管一次开仓。返回 True = 已接管（调用方不要再走人工确认流）。"""
        with self._op_lock:
            self._gate_new_round()
            return self._on_entry_locked(position_id)

    def _on_entry_locked(self, position_id: int) -> bool:
        row = self.db.position_by_id(position_id)
        if row is None or self.p.mode == MODE_OFF:
            return False
        symbol, side = row["symbol"], int(row["side"] or 1)
        bar_ms = int(row["signal_bar_ms"])
        # ② 入口容错闸门：coin 映射缺失 → 不进入下单流程（dry 也不能抛崩整轮 tick）。
        #    落一条 skipped 意图留痕 + 告警（live 下引擎按 alert_logs 补推）。
        #    该行的 coin 列写原始 symbol —— 没有映射可用；行状态是 skipped，绝不会用于发单。
        coin = self._coin_or_none(symbol)
        if coin is None:
            self.db.hl_record_intent(symbol, symbol, bar_ms, PURPOSE_ENTRY, side, 0.0,
                                     status=ST_SKIPPED)
            self._event(EV_ERROR, f"未配置 HL coin 映射：{symbol}，拒绝开仓", symbol=symbol)
            self._alert(f"【HL执行】⛔ {symbol} 未配置 HL coin 映射，**拒绝开仓**"
                        f"（fail-closed：无法定位交易所标的，绝不猜合约名）。"
                        f"请到 app/symbol_registry.py 登记该标的。")
            return True

        # 已有意图/结果 → 不重复处理（幂等）
        existing = self.db.hl_order(symbol, bar_ms, PURPOSE_ENTRY)
        if existing is not None and existing["status"] not in (ST_SKIPPED,):
            self._event(EV_DECISION, f"{symbol} 本 bar 已有 entry 记录（{existing['status']}），跳过",
                        symbol=symbol, data={"order_id": existing["id"]})
            return True

        snap = self._snapshot(force=True)
        if snap is None:
            self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_ENTRY, side, 0.0,
                                     status=ST_SKIPPED)
            self._event(EV_ERROR, "读不到账户，放弃本次开仓", symbol=symbol)
            if self.p.live:
                # live 下不能回落人工卡：人工卡按配置 equity（1000U）算金额，与
                # 真实账户差一个数量级，照着下单就是风险放大（审计三轮 P1-H5）。
                # 已接管 → 引擎只补推这条告警（有 alert_logs 补推兜底）。
                self._alert(f"【HL执行】⚠️ {coin} 信号到达但读不到账户快照，本轮**未下单**。"
                            f"这笔信号不会自动重试（占用的仓位槽约 8 小时后自动释放）。")
                return True
            return False

        try:
            specs = self.info.specs([coin]).get(coin) or {}
            sz_decimals = int(specs.get("sz_decimals", 0))
        except HlInfoError as e:
            self._event(EV_ERROR, f"读不到合约规格，放弃开仓：{e}", symbol=symbol)
            if self.p.live:
                self._alert(f"【HL执行】⚠️ {coin} 信号到达但读不到合约规格，本轮**未下单**。"
                            f"这笔信号不会自动重试（占用的仓位槽约 8 小时后自动释放）。")
                return True
            return False

        mid = 0.0
        try:
            mid = self.ex.mid(coin)
        except Exception as e:                                       # noqa: BLE001
            self._event(EV_ERROR, f"取不到 {coin} mid，放弃开仓：{e}", symbol=symbol)
            if self.p.live:
                self._alert(f"【HL执行】⚠️ {coin} 信号到达但取不到行情，本轮**未下单**。"
                            f"这笔信号不会自动重试（占用的仓位槽约 8 小时后自动释放）。")
                return True
            return False
        if mid <= 0:
            self._event(EV_ERROR, f"{coin} mid 非法（{mid}），放弃开仓", symbol=symbol)
            if self.p.live:
                self._alert(f"【HL执行】⚠️ {coin} 信号到达但行情非法，本轮**未下单**。"
                            f"这笔信号不会自动重试（占用的仓位槽约 8 小时后自动释放）。")
                return True
            return False

        # 用**当前 mid** 当入场价重算数量（风险归一），止损仍用信号给的止损价
        stop = float(row["stop_price"])
        eff_equity = self.effective_equity(snap)          # 跟随实时权益（带下限）
        # 空单风险减半的**执行器侧生效点**：与引擎侧同一个 helper、同一份常数。
        # 只折算风险比例 —— mid/stop/杠杆/最小名义全部原样，止损距离因此不变。
        plan = hl_sizing.size_order_hl(coin, sz_decimals, mid, stop, eff_equity,
                                      effective_risk_pct(self.p.risk_pct, side,
                                                         bool(getattr(self.p, "short_risk_half",
                                                                      False))),
                                      self.p.leverage, side=side,
                                      min_notional=self.p.min_notional)
        gates = self._entry_gates(symbol, plan, snap, equity=eff_equity)
        if gates:
            self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_ENTRY, side, plan.sz,
                                     px=mid, status=ST_SKIPPED)
            self._event(EV_DECISION, "开仓被闸门拒绝：" + "；".join(gates), symbol=symbol,
                        data={"plan": plan.__dict__})
            if self.p.live:
                self._alert(f"【HL执行】{coin} 信号被闸门拒绝（未下单）：\n- " + "\n- ".join(gates))
            return True

        status = ST_PLANNED if not self.p.live else ST_INTENT
        self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_ENTRY, side, plan.sz,
                                 px=mid, cloid=make_cloid(symbol, bar_ms, PURPOSE_ENTRY),
                                 status=status)
        self.db.hl_log_event(EV_ORDER, f"{symbol} entry 意图：{plan.sz} {coin} @~{mid:,.1f}"
                                       f"（止损 {stop:,.1f}，风险 {plan.risk_u:.1f}U）",
                             symbol=symbol, data={"plan": plan.__dict__, "live": self.p.live},
                             code_rev=self.p.code_rev)
        if not self.p.live:
            self._event(EV_DECISION, "dry 模式：只记录计划不下单（人工提醒照常发出）", symbol=symbol,
                        data={"plan": plan.__dict__})
            # 关键：dry 必须返回 False，让引擎照常发人工操作卡 ——
            # 否则信号被静默吞掉，仓位停在 pending 占住该标的的槽，影子期反而没有可比对的人工动作。
            self._alert("\n".join([
                f"【HL影子】{coin} 若为实盘会开仓 {plan.sz} {coin} @~{mid:,.1f}",
                f"止损 {stop:,.1f}（{plan.stop_dist * 100:.2f}%）｜风险 {plan.risk_u:.1f}U｜"
                f"名义 {plan.notional:,.0f}U｜杠杆 {plan.leverage:g}x",
                "（dry 模式：未下任何单，下面的人工提醒照常）",
            ]))
            return False

        # 1) 逐仓杠杆
        lev = self.ex.update_leverage(coin, int(round(self.p.leverage)), cross=False)
        if not lev["ok"]:
            return self._entry_failed(symbol, bar_ms, f"设置杠杆失败：{lev['error']}")

        # 2) 入场 IOC 限价
        try:
            limit_px = hl_sizing.round_price(self.ex.slippage_limit(coin, side == 1, mid),
                                             sz_decimals)
        except Exception as e:                                       # noqa: BLE001
            return self._entry_failed(symbol, bar_ms, f"计算限价失败：{e}")
        res = self.ex.place_order(coin, side == 1, plan.sz, limit_px,
                                  cloid=self.ex._cloid(symbol, bar_ms, PURPOSE_ENTRY))
        if not res["ok"] or res["status"] not in (STATUS_FILLED, STATUS_RESTING):
            return self._entry_failed(symbol, bar_ms, f"入场失败：{res['error']}")
        if res["status"] == STATUS_RESTING:
            # IOC 不该挂着；挂上了说明限价不够激进 → 撤掉并放弃（不追价）
            self.ex.cancel(coin, int(res["oid"] or 0))
            self._churn_note(coin, side)          # 撤单也是 churn（PR-4 口径）
            return self._entry_failed(symbol, bar_ms, "IOC 未成交（限价不够激进），已撤单放弃")
        filled_sz = float(res["filled_sz"] or plan.sz)
        avg_px = float(res["avg_px"] or mid)
        self.db.hl_update_order(symbol, bar_ms, PURPOSE_ENTRY, status=ST_FILLED,
                                oid=res.get("oid"), filled_sz=filled_sz, avg_px=avg_px,
                                raw=str(res.get("raw"))[:2000])
        # 静置闸门开窗（PR-4）：从**成交回报这一刻**起 15s，该 coin-side 的重复动作
        # （再开仓 / 修复类补挂 / 复价）暂停 —— 交易所的持仓与挂单列表此刻还在收敛。
        # 放在「确认成交之后、挂止损之前」：止损（下面 ACT_STOP_FIRST）在放行清单里，
        # 于是这 15s 绝不会因为闸门而出现裸仓，同时它保护的是成交后紧跟的那串重复动作。
        self._settle_arm(coin, side)

        # 3) 止损单（硬要求）
        #    闸门：这是 ACT_STOP_FIRST（放行清单）——**首张止损单永不过闸**：
        #    它出现的那一刻仓位已经成交、还没有任何保护，任何延迟都是裸仓窗口。
        stop_res = self.ex.place_order(coin, side != 1, filled_sz,
                                       hl_sizing.round_price(stop, sz_decimals),
                                       reduce_only=True, trigger_px=hl_sizing.round_price(stop, sz_decimals),
                                       cloid=self.ex._cloid(symbol, bar_ms, PURPOSE_STOP))
        self.db.hl_record_intent(symbol, coin, bar_ms, PURPOSE_STOP, side, filled_sz,
                                 px=stop, reduce_only=True, status=ST_SENT,
                                 cloid=make_cloid(symbol, bar_ms, PURPOSE_STOP))
        if not stop_res["ok"] or stop_res["status"] not in (STATUS_RESTING, STATUS_FILLED):
            self.db.hl_update_order(symbol, bar_ms, PURPOSE_STOP, status=ST_FAILED,
                                    error=str(stop_res.get("error"))[:300])
            # 没有保护的仓位不允许存在 → 立刻平掉。
            # 平仓限价必须按**平仓方向**算滑点（side != 1），不能用上面开仓方向的 limit_px
            try:
                close_px = hl_sizing.round_price(
                    self.ex.slippage_limit(coin, side != 1, mid), sz_decimals)
            except Exception:                                        # noqa: BLE001
                close_px = limit_px
            close = self.ex.place_order(coin, side != 1, filled_sz, close_px, reduce_only=True)
            closed_ok = bool(close.get("ok")) and close.get("status") == STATUS_FILLED
            msg = (f"【HL执行】⚠️ {coin} 止损单挂失败（{stop_res['error']}），"
                   + ("已立即平仓" if closed_ok else f"平仓未确认（{close.get('status')}）——仓位可能仍在交易所")
                   + "。本次未持仓，请人工核对。")
            self._event(EV_ERROR, f"止损挂单失败 → 立即平仓：{stop_res['error']}",
                        symbol=symbol, data={"close": close})
            self.db.set_pos_hl(position_id, hl_status="aborted")
            self._alert(msg)
            return True
        self.db.hl_update_order(symbol, bar_ms, PURPOSE_STOP, status=ST_RESTING,
                                oid=stop_res.get("oid"), raw=str(stop_res.get("raw"))[:2000])

        # 4) 落仓位
        self.db.set_pos_hl(position_id, hl_status="open", hl_entry_oid=res.get("oid"),
                           hl_stop_oid=stop_res.get("oid"), hl_sz=filled_sz)
        # 双口径收敛：引擎建行时写的是**配置** equity（1000U），这里成交确认时把
        # **实际生效基数**（eff_equity，跟随实时权益的档位基数）覆盖回去 ——
        # positions.equity_at_entry 是唯一字段，读侧（review_fill 的「权益的 X%」等）
        # 自然读到真实基数，不需要别处再判口径。
        self.db.confirm_position(position_id, avg_px, float(filled_sz), stop, True,
                                 equity_at_entry=eff_equity)
        plan2 = hl_sizing.HlOrderPlan(**{**plan.__dict__, "entry_price": avg_px,
                                         "sz": filled_sz, "notional": filled_sz * avg_px})
        self._alert_fill(fmt_entry_alert(
            coin=coin, side=side, sz=filled_sz, entry_px=avg_px, stop=stop,
            stop_dist=plan2.stop_dist, notional=plan2.notional, margin=plan2.margin,
            liq=plan2.liq_price, risk_u=plan2.risk_u, equity=eff_equity,
            signal_px=float(row["entry_price"] or avg_px), oid=res.get("oid"),
            stop_oid=stop_res.get("oid"), when=dt.datetime.now(DISPLAY_TZ), tz_label=TZ_LABEL,
            leverage=float(self.p.leverage or 5.0)))
        self._event(EV_ORDER, f"{symbol} 开仓完成：{filled_sz} @ {avg_px:,.2f}，止损单已挂",
                    symbol=symbol, data={"risk_u": plan2.risk_u, "notional": plan2.notional})
        # 5) 开仓确认时的数量核对（PR-3）：下单前算的 sz 与真实成交可能差一个 tick
        #    （IOC 部分成交时差得更多），而对账要等下一轮才跑。这里同步一次成交、比对
        #    一次，差异只记事件不动作 —— 下一轮 reconcile 的两击规则才是真正兜底的手。
        try:
            self._check_entry_size(position_id, coin, filled_sz, avg_px)
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 开仓后数量核对失败（忽略，下一轮对账兜底）：%s", e)
        return True

    def _check_entry_size(self, position_id: int, coin: str, filled_sz: float,
                          avg_px: float) -> None:
        """开仓成交确认后比一次「本地记账量 vs 交易所成交流水量」。

        为什么在开仓点就要比：止损单是按下单计划量挂的，若真实成交只成了一半，
        那一刻就已经是「止损覆盖量 > 持仓量」（平不掉的部分裸奔）。这里不修（修要
        改下单/止损逻辑，PR-3 明确不动），只把差异**立刻**记成事件，让它在下一次
        5 分钟对账之前就可见；两击规则的正式判定留给 reconcile。
        """
        if filled_sz <= 0 or not self.p.account_address:
            return
        row = self.db.position_by_id(position_id)
        if row is None:
            return
        tick = self._size_tick(coin)
        if tick is None:
            return
        try:
            self._sync_fills()
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 开仓后同步成交失败（数量核对跳过）：%s", e)
            return
        agg = self._size_agg(dict(row), coin)
        if agg is None:
            return                                    # 成交还没进库：交给下一轮对账
        flow = hl_pnl.net_size(agg)
        if abs(flow - float(filled_sz)) <= tick:
            return
        self._event(EV_RECONCILE,
                    f"{row['symbol']} 开仓数量与流水不一致：成交回报 {float(filled_sz):g} vs "
                    f"流水净量 {flow:g}（差 {abs(flow - float(filled_sz)):g}，tick {tick:g}）"
                    f"—— 止损按 {float(filled_sz):g} 挂的，请核对；下一轮对账会继续跟进",
                    symbol=str(row["symbol"]),
                    data={"pid": int(position_id), "coin": coin, "filled_sz": float(filled_sz),
                          "flow_sz": flow, "entry_vwap": agg["entry_vwap"],
                          "reported_entry": float(avg_px), "tick": tick})

    def _entry_failed(self, symbol: str, bar_ms: int, msg: str) -> bool:
        # 文案用 HL 合约名（解析不出就退回 symbol，仅影响文案，不影响落库/状态）
        coin = self._coin_or_none(symbol) or symbol
        self.db.hl_update_order(symbol, bar_ms, PURPOSE_ENTRY, status=ST_FAILED,
                               error=str(msg)[:300])
        self._event(EV_ERROR, f"开仓失败：{msg}", symbol=symbol)
        if self.p.live:
            self._alert(f"【HL执行】⚠️ {coin} 开仓失败：{msg}")
        return True

    # ------------------------------------------------------------ 离场
    def on_exit(self, position_id: int, reason: str, ref_price: float) -> bool:
        """接管一次离场。返回 True = 已接管（调用方不要再动 DB 状态）。"""
        with self._op_lock:
            self._gate_new_round()
            return self._on_exit_locked(position_id, reason, ref_price)

    def _on_exit_locked(self, position_id: int, reason: str, ref_price: float) -> bool:
        row = self.db.position_by_id(position_id)
        if row is None or self.p.mode == MODE_OFF:
            return False
        # 守卫：只有「本地还认为仓在交易所」的行才由执行器接管。
        # 例外**仅**给 `mismatch` + 止损事件（2026-10-22 事故的第二入口）：对账已把该仓
        # 打成 mismatch（交易所无仓、本地有仓）后，4h 收盘的止损离场进来时旧守卫直接
        # return False → 执行器让出接管权 → 引擎回落到**人工流**，发出「到 MEXC 核对
        # 持仓已平」这种对 HL 仓完全错误的指引（实盘 16:03 就这么发生）。
        # 这里只放行，不改语义：真正的关账必须落在下面「reason=='stop' 且交易所无仓」
        # 那一支（用真实成交价）；那支不成立（交易所有仓/快照不可读）会逐条 return False，
        # 绝不允许 mismatch 行进到「撤止损 → 下平仓单」那一段去赌方向。
        hl_status = row["hl_status"] or ""
        # 放行必须同时受开关门控（对抗审查 M3）：此前只关掉了对账侧的 _fill_close_proof，
        # 这条「mismatch + stop」放行照旧生效 → 面板把 hl_fill_close 关掉后仍会自动关账，
        # 「关 = 回到旧行为」的说法与实现不符。现在关 = 两条路径一起回到旧行为：
        # mismatch 行直接让出接管权（引擎走人工提醒流）。
        fill_close_on = bool(getattr(self.p, "fill_close", True))
        # 宽限期（closing）与 mismatch 同一口径：都是「交易所侧已经没有这个仓了」的行，
        # 只有 reason=='stop' 才放行 —— 也只有它下面那一支「交易所无仓」的关账分支能用。
        if hl_status != "open" and not (fill_close_on and reason == "stop"
                                        and hl_status in ("mismatch", HL_ST_CLOSING)):
            return False
        symbol, side = row["symbol"], int(row["side"] or 1)
        # 离场也不能猜 coin：解析不出就**让出接管权**（return False）→ 引擎照常走人工提醒流，
        # 而不是拿一个猜出来的合约名去平仓/撤单（可能平错标的）。
        coin = self._coin_or_none(symbol)
        if coin is None:
            self._event(EV_ERROR, f"未配置 HL coin 映射：{symbol}，不自动离场", symbol=symbol)
            self._alert(f"【HL执行】⛔ {symbol} 未配置 HL coin 映射，无法自动平仓/撤单"
                        f"（fail-closed）—— 请人工核对交易所并处理。")
            return False
        bar_ms = int(row["signal_bar_ms"])

        # 平仓/重挂止损的 cloid 用「当前离场 bar」而不是入场 bar：
        # ① 同一仓位多次触发离场（首次失败后下个 4h 重试）时 cloid 必须变化，
        #    否则被交易所按重复 cloid 拒单，自动离场永久报废；
        # ② 重挂止损与开仓止损（PURPOSE_STOP@入场bar）天然不同源，不再撞号。
        exit_bar_ms = self._current_closed_bar_ms(symbol) or bar_ms
        sz = float(row["hl_sz"] or 0.0)

        snap = self._snapshot(force=True)
        ex_pos = {p["coin"]: p for p in (snap or {}).get("positions") or []}
        # 止损已由交易所触发（stop 事件）→ 用真实成交价关账，不发平仓单
        if reason == "stop" and snap is not None and coin not in ex_pos:
            px, fills = self._last_fill_px(coin)
            if px is None and hl_status == "mismatch":
                # mismatch 行 + 读不到成交价：账本本来就不一致，再按参考价关一次等于
                # 「猜一个价把仓抹掉」。让出接管权（引擎会喊人去交易所核对），不关账。
                self._event(EV_ERROR, f"{symbol} mismatch 止损离场但读不到成交价，未关账",
                            symbol=symbol)
                return False
            if px is None and hl_status == HL_ST_CLOSING:
                # 宽限期内读不到成交价：**接管但不关账**，也不许回落人工流。
                # 这条行本来就归「对账自动消化」管（下一轮会继续重试/回捞），此时把
                # 用户喊去「到 MEXC 核对持仓已平」既错（HL 仓）又多余（系统会自己处理）。
                self._event(EV_DECISION,
                            f"{symbol} 止损离场：交易所已无该仓，成交价核对中（宽限内自动重试）",
                            symbol=symbol)
                self._alert(f"【HL执行】ℹ️ {coin} 交易所已无该仓，成交价核对中"
                            f"（宽限内自动重试），无需人工操作")
                return True
            self._close_local(position_id, px or ref_price, "stop", sz)
            r = self._r_multiple(row, px or ref_price)
            self._alert(f"【HL执行】🛑 止损离场 · {coin} {'做多' if side == 1 else '做空'}\n"
                        f"止损单已由交易所触发 @ {px or ref_price:,.2f}，本地已关账（R {r:+.2f}）")
            return True
        if reason == "stop" and snap is None:
            if hl_status == "mismatch":
                # mismatch 行 + 快照读不到：既证明不了「已被交易所平掉」，也确认不了
                # 「还在不在」—— 不下结论，让出接管权（旧行为本来就不接管这种行）。
                return False
            # 快照读不到 ≠ 仓位没了。不能按参考价关账 —— 万一仓位还在（且止损单也丢了），
            # 本地关账后就没人管它了。只提醒，留给下一轮/人工。
            self._event(EV_ERROR, f"{symbol} 止损事件但快照不可读，未关账（下一轮再核对）",
                        symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 判定止损离场，但账户快照读取失败，"
                        f"本地暂未关账 —— 请人工核对交易所。")
            return True

        if hl_status in ("mismatch", HL_ST_CLOSING):
            # 走到这里 = 交易所**仍有**该 coin 的仓（上面「无仓」那支已返回）。
            # 这两类行只允许走「已确认被交易所平掉」那一条路径；账本已经不一致/待核对时
            # 再由执行器撤止损/下平仓单，就是拿一本错账去真实下单 —— 让出接管权，交人工。
            self._event(EV_DECISION,
                        f"{symbol} {hl_status} 行止损离场但交易所仍有仓，让出接管权",
                        symbol=symbol)
            return False

        if not self.p.live:
            # 真实仓位在交易所、执行器却不是 live：绝不能替引擎把本地关账（会造成本地/交易所不一致），
            # 交给人工，并且必须喊出来。
            self._event(EV_DECISION, f"dry 模式：不代平真实仓位 {symbol}（{reason}）", symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 触发离场（{reason}）但执行器不是 live 模式，"
                        f"交易所仍有真实仓位 —— 请人工平仓并撤掉止损单")
            return True

        # 1) 先撤掉止损单（否则可能与新平仓单互相打架）。
        #    审计三轮 M1：首次平仓失败后重挂的止损记在 exit:rehang（当前离场 bar）、
        #    对账补挂在 stop:fix —— 只撤 PURPOSE_STOP 会漏掉这两类，留下幽灵触发单。
        #    撤单动作撤的是「交易所侧当前挂着的止损」，所以把 DB 里三种 purpose 的
        #    未撤单记录全查一遍，按 oid 去重逐个撤。
        #    PR-4 闸门：这一串撤单是**平仓动作的前置步骤**（ACT_CLOSE，放行清单内）——
        #    拦它就等于「止损撤不掉所以平不了」，反而是把人锁在一个他想离开的仓位里；
        #    但它照样**计入 churn**：真正的「撤-挂-撤-挂」风暴往往就是从这里开始的，
        #    计数不能因为动作被豁免而漏掉。
        cancelled_purposes = []
        for purpose in (PURPOSE_STOP, PURPOSE_STOP + ":fix", PURPOSE_EXIT_REHANG):
            so = self.db.hl_order(symbol, bar_ms, purpose)
            if purpose == PURPOSE_EXIT_REHANG:
                # exit:rehang 键在离场 bar 上，同一仓可能有多条（多次离场尝试），
                # hl_order 只按三元键取一条 → 用 hl_recent_orders 扫全部
                for r in self.db.hl_recent_orders(100):
                    if (r["symbol"] == symbol and r["purpose"] == purpose
                            and r["status"] in (ST_RESTING, ST_SENT) and r["oid"]):
                        self.ex.cancel(coin, int(r["oid"]))
                        self._churn_note(coin, side)
                        self.db.hl_update_order(symbol, int(r["bar_ms"]), purpose,
                                                status="cancelled")
                        cancelled_purposes.append(purpose)
                continue
            if so is not None and so["oid"] and so["status"] in (ST_RESTING, ST_SENT, ST_FILLED):
                self.ex.cancel(coin, int(so["oid"]))
                self._churn_note(coin, side)
                self.db.hl_update_order(symbol, bar_ms, purpose, status="cancelled")
                cancelled_purposes.append(purpose)
        # 2) 平仓（reduce-only IOC）。限价必须按**平仓方向**算滑点
        #    （买入平空 = mid×(1+s)，卖出平多 = mid×(1−s)；用开仓方向的限价会让
        #     卖单拿到比 mid 高的最低可接受价 → 滑点保护完全失效）。
        if sz <= 0:
            sz = abs(float(ex_pos.get(coin, {}).get("size") or 0.0))
        if sz <= 0:
            self._event(EV_ERROR, f"{symbol} 离场时无法确定持仓量，跳过（请人工处理）", symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 离场异常：交易所查不到该仓位，已跳过，请人工核对")
            return True
        try:
            sz_dec = int((self.info.specs([coin]).get(coin) or {}).get("sz_decimals", 5))
        except HlInfoError:
            sz_dec = 5
        try:
            limit_px = hl_sizing.round_price(self.ex.slippage_limit(coin, side != 1), sz_dec)
        except Exception as e:                                       # noqa: BLE001
            limit_px = ref_price
            self._event(EV_ERROR, f"平仓限价计算失败（用参考价）：{e}", symbol=symbol)
        # cloid 按「尝试时刻」区分：同一 4h 内第二次离场尝试不能再复用同一个值
        # （HL 拒重复 cloid，而重试第一步已撤掉止损 → 撞号就是裸仓，见 _attempt_cloid）
        exit_cloid = self._attempt_cloid(symbol, exit_bar_ms, PURPOSE_EXIT)
        self.db.hl_record_intent(symbol, coin, exit_bar_ms, PURPOSE_EXIT, side, sz, px=limit_px,
                                 reduce_only=True, status=ST_SENT,
                                 cloid=exit_cloid)
        res = self.ex.place_order(coin, side != 1, sz, limit_px, reduce_only=True,
                                  cloid=exit_cloid)
        if not res["ok"]:
            self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FAILED,
                                    error=str(res.get("error"))[:300])
            self._event(EV_ERROR, f"平仓失败：{res['error']}", symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 平仓失败：{res['error']}\n"
                        f"（止损单已撤！请立即人工平仓或手动挂回止损）")
            return True
        if res["status"] != STATUS_FILLED:
            # IOC 挂出未成（resting）≠ 已平仓。绝不能按限价关账 —— 交易所仓位还在，
            # 而止损单已被撤掉。处理：撤掉这张挂单、**立即挂回原止损**（保护仓位）、
            # 拉人工提醒；不关本地账，让下一轮 on_exit / 对账继续跟进。
            self.ex.cancel(coin, int(res.get("oid") or 0))
            self._churn_note(coin, side)
            self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FAILED,
                                    error="平仓 IOC 未成交（已撤单并挂回止损）")
            # 重挂止损用独立 purpose（exit:rehang）：与开仓止损（stop@入场bar）、
            # 事后补挂（stop:fix）都不同源 —— HL 的 cloid 账户级永久唯一，
            # 复用任何一个都会被拒单，重挂失败=仓位裸奔。
            re_stop_px = hl_sizing.round_price(float(row["actual_stop"] or row["stop_price"]), sz_dec)
            rehang_cloid = self._attempt_cloid(symbol, exit_bar_ms, PURPOSE_EXIT_REHANG)
            # 闸门（PR-4）：重挂止损是「replace」，要过 churn（不过静置 —— 上面已经
            # 撤掉了止损，这里再等 15s 就是纯裸仓）。熔断时**不重挂**，直接落到下面
            # 已有的「重挂失败 → 市价平仓」分支：仓位一样有保护（平掉），但不再往
            # 交易所打第 N 张注定被拒的触发单 —— 这正是要打断的循环。
            if self._gate_act(ACT_REPLACE, coin, side, symbol=symbol):
                self._churn_note(coin, side)
                re_stop = self.ex.place_order(
                    coin, side != 1, sz, re_stop_px,
                    reduce_only=True, trigger_px=re_stop_px,
                    cloid=rehang_cloid)
            else:
                re_stop = {"ok": False, "status": "gated",
                           "error": "churn 熔断：停止重挂止损，改为市价平仓"}
            re_ok = bool(re_stop.get("ok")) and re_stop.get("status") in (STATUS_RESTING, STATUS_FILLED)
            self.db.hl_record_intent(symbol, coin, exit_bar_ms, PURPOSE_EXIT_REHANG, side, sz,
                                     px=re_stop_px, reduce_only=True,
                                     status=(re_stop.get("status") if re_ok else ST_FAILED),
                                     cloid=rehang_cloid,
                                     error=None if re_ok else str(re_stop.get("error"))[:300])
            if re_ok:
                self._event(EV_ERROR, f"{symbol} 平仓未成交（resting），已撤单并重挂止损"
                                      f"（{re_stop.get('status')} @ {re_stop_px:,.2f}）", symbol=symbol)
                self._alert(f"【HL执行】⚠️ {coin} 平仓单未成交（已撤）。"
                            f"止损单已重新挂回 @ {re_stop_px:,.2f}。请人工核对并处理。")
                return True
            # 重挂失败 → 不允许无保护持仓存在 → 立即市价平掉（与开仓挂止损失败同一口径）
            self._event(EV_ERROR, f"{symbol} 平仓未成交且止损重挂失败"
                                  f"（{re_stop.get('error')}）→ 立即平仓", symbol=symbol)
            try:
                close_px = hl_sizing.round_price(self.ex.slippage_limit(coin, side != 1), sz_dec)
            except Exception:                                        # noqa: BLE001
                close_px = limit_px
            close = self.ex.place_order(coin, side != 1, sz, close_px, reduce_only=True)
            closed_ok = bool(close.get("ok")) and close.get("status") == STATUS_FILLED
            if closed_ok:
                px = float(close.get("avg_px") or close_px)
                self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FILLED,
                                        oid=close.get("oid"), filled_sz=close.get("filled_sz"),
                                        avg_px=px, raw=str(close.get("raw"))[:2000])
                self._close_local(position_id, px, reason, sz)
                r = self._r_multiple(row, px)
                self._alert(f"【HL执行】⚠️ {coin} 平仓未成交、止损重挂失败，已市价平仓"
                            f" @ {px:,.2f}（R {r:+.2f}）。请人工核对。")
            else:
                self._alert(f"【HL执行】🛑 {coin} 平仓未成交且止损重挂失败、市价平仓未确认"
                            f"（{close.get('status')}）——仓位可能仍在交易所且无止损，"
                            f"请立即人工处理！")
            return True
        if res.get("filled_sz") is not None and float(res["filled_sz"] or 0) + 1e-12 < sz:
            # 部分成交（审计三轮 P1-H3）：这轮的第一步已把原止损撤掉了，
            # 剩余量没有保护 —— 必须按剩余量把止损挂回去（exit:rehang 口径，
            # cloid 用当前离场 bar，与全量重挂同一来源），且本地**不**关账，
            # 留 open 让下一轮 on_exit / 对账继续跟进。
            filled = float(res["filled_sz"] or 0)
            remain = sz - filled
            # 审计四轮 P2：这张 IOC 已经结束了（成交一半），必须把 exit 行改成 filled。
            # 旧代码不更新它 → hl_orders 里永久停在 sent，看护把它当「卡住的意图单」
            # 每轮告警、面板对账判 mismatch（复现见 tests/test_audit4_p1.py）。
            # 本地仓**不**关账（剩余量还在交易所，由下面重挂止损 + 下一轮继续离场）。
            try:
                self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FILLED,
                                        oid=res.get("oid"), filled_sz=filled,
                                        avg_px=res.get("avg_px"),
                                        raw=str(res.get("raw"))[:2000])
            except Exception as ex:                                  # noqa: BLE001
                logger.warning("[hl-exec] 部分成交更新 exit 行失败（忽略）：%s", ex)
            self._event(EV_ORDER, f"{symbol} 平仓部分成交 {filled}/{sz}，剩余 {remain} 重挂止损",
                        symbol=symbol, data={"filled": filled, "remain": remain})
            try:
                sz_dec = int((self.info.specs([coin]).get(coin) or {}).get("sz_decimals", 5))
            except HlInfoError:
                sz_dec = 5
            re_px = hl_sizing.round_price(float(row["actual_stop"] or row["stop_price"]), sz_dec)
            rehang_cloid = self._attempt_cloid(symbol, exit_bar_ms, PURPOSE_EXIT_REHANG)
            # 闸门（PR-4）：部分成交后重挂剩余量止损，同样按 replace 过 churn。
            # 熔断时不重挂 → 落到下面的「重挂失败 → 市价平掉剩余量」（保护动作不过闸）。
            if self._gate_act(ACT_REPLACE, coin, side, symbol=symbol):
                self._churn_note(coin, side)
                try:
                    re_stop = self.ex.place_order(
                        coin, side != 1, remain, re_px, reduce_only=True, trigger_px=re_px,
                        cloid=rehang_cloid)
                except Exception as ex:                               # noqa: BLE001
                    re_stop = {"ok": False, "error": f"{type(ex).__name__}: {ex}",
                               "status": "error"}
            else:
                re_stop = {"ok": False, "status": "gated",
                           "error": "churn 熔断：停止重挂剩余量止损，改为市价平仓"}
            re_ok = bool(re_stop.get("ok")) and re_stop.get("status") in (STATUS_RESTING, STATUS_FILLED)
            try:
                self.db.hl_record_intent(symbol, coin, exit_bar_ms, PURPOSE_EXIT_REHANG, side,
                                         remain, px=re_px, reduce_only=True,
                                         status=(re_stop.get("status") if re_ok else ST_FAILED),
                                         cloid=rehang_cloid,
                                         error=None if re_ok else str(re_stop.get("error"))[:300])
            except Exception as ex:                                   # noqa: BLE001
                logger.warning("[hl-exec] 部分成交重挂记录失败（忽略）：%s", ex)
            if re_ok:
                self._event(EV_ERROR, f"{symbol} 部分成交后止损已按剩余量 {remain} 重挂",
                            symbol=symbol)
                self._alert(f"【HL执行】⚠️ {coin} 平仓部分成交（{filled}/{sz}），剩余 {remain} "
                            f"{coin} 已重挂止损 @ {re_px:,.2f}，本地保持持仓，下一轮继续离场。")
            else:
                self._event(EV_ERROR, f"{symbol} 部分成交后止损重挂失败（{re_stop.get('error')}）"
                                      f" → 尝试市价平掉剩余量", symbol=symbol)
                try:
                    close_px2 = hl_sizing.round_price(self.ex.slippage_limit(coin, side != 1), sz_dec)
                except Exception:                                    # noqa: BLE001
                    close_px2 = limit_px
                try:
                    close2 = self.ex.place_order(coin, side != 1, remain, close_px2,
                                                 reduce_only=True)
                except Exception as ex:                              # noqa: BLE001
                    close2 = {"ok": False, "status": "error",
                              "error": f"{type(ex).__name__}: {ex}"}
                c2_ok = bool(close2.get("ok")) and close2.get("status") == STATUS_FILLED
                self._alert(f"【HL执行】🛑 {coin} 平仓部分成交且剩余量止损重挂失败，"
                            f"已尝试市价平剩余 {remain}（{'成功' if c2_ok else '结果异常，请立即人工核对'}）。")
                if c2_ok and float(close2.get("filled_sz") or 0) + 1e-12 >= remain:
                    px2 = float(close2.get("avg_px") or close_px2)
                    self._close_local(position_id, px2, reason, sz)
                    r = self._r_multiple(row, px2)
                    self._event(EV_ORDER, f"{symbol} 剩余量已平 @ {px2:,.2f}（{reason}）",
                                symbol=symbol, data={"r": r})
                    return True
            return True    # 本地不关账（或已按剩余平完关账），本轮结束
        px = float(res["avg_px"] or limit_px)
        self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FILLED,
                                oid=res.get("oid"), filled_sz=res.get("filled_sz"), avg_px=px,
                                raw=str(res.get("raw"))[:2000])
        self._close_local(position_id, px, reason, sz)
        r = self._r_multiple(row, px)
        gross = (px - float(row["actual_entry"] or row["entry_price"])) * sz * (1 if side == 1 else -1)
        # 平仓手续费在下一轮 _sync_fills 才会进库 —— 先同步一次再算，否则净利少算平仓费
        try:
            self._sync_fills()
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 平仓前同步成交失败（手续费可能少算）：%s", e)
        fees = self._fees_since(coin, row["opened_at"])
        self._alert_fill(fmt_exit_alert(
            coin=coin, side=side, entry_px=float(row["actual_entry"] or row["entry_price"]),
            exit_px=px, sz=sz, reason=reason, r_multiple=r, gross_pnl=gross, fees=fees,
            opened_at=row["opened_at"], when=dt.datetime.now(DISPLAY_TZ), tz_label=TZ_LABEL))
        self._event(EV_ORDER, f"{symbol} 平仓完成 @ {px:,.2f}（{reason}）", symbol=symbol,
                    data={"r": r})
        return True

    @staticmethod
    def _num(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def effective_equity(self, snap: Optional[dict]) -> float:
        """下单基数。follow：按交易所实时权益（floor 到 10U、带下限）；读不到则回落到配置值。

        为什么要跟随：写在配置里的基数不会随账户盈亏变化 —— 涨了不复利、跌了相对风险悄悄变大。
        为什么要取整：几毛钱的浮亏浮盈不该让仓位跳来跳去（也避免每次下单都换一个数）。
        只设下限（用户拍板）：入金到 1000U 就按 1000 算，不加上限。
        """
        base = float(self.p.equity or 0.0)
        if not self.p.follow or not snap:
            return base
        # 统一账户现货读取失败 → equity_total = 0 + 浮盈，不可信（P1-H1，审计三轮）。
        # 熔断分支早就显式检查 spot_ok（run 里「数据不可信，宁可跳过」），定仓路径
        # 却没用它 → 无仓时 equity_total≤0 回落到配置 equity=1000U，真钱 100U 的账户
        # 会按 20U 风险定仓（10 倍）。这里必须同样 fail-closed：回落 equity_min，
        # 绝不放大风险。
        if not bool(snap.get("spot_ok", True)):
            logger.warning("[hl-exec] 现货读取失败（spot_ok=False），权益不可信，"
                           "回落 equity_min=%s", self.p.equity_min)
            return float(self.p.equity_min or 20.0)
        live = float(snap.get("equity_total", snap.get("equity")) or 0.0)
        if live <= 0:
            logger.warning("[hl-exec] 实时权益读到 %.4f，回落 equity_min=%.2f（不再用配置基数，"
                           "防风险放大）", live, self.p.equity_min)
            return float(self.p.equity_min or 20.0)
        eff = math.floor(live / 10.0) * 10.0
        if self.p.equity_min and eff < self.p.equity_min:
            eff = float(self.p.equity_min)
            if eff > live:
                # equity_min 配置超过真实权益 → 单笔风险被放大（live/eff 倍）。只记事件提醒，
                # 不阻断（用户拍板过「不加上限」，但配错下限值得被看见）。
                self._event(EV_DECISION,
                            f"equity_min={eff:g}U 高于实时权益 {live:.2f}U —— 生效基数被抬高，"
                            f"实际单笔风险约为目标的 {eff / live:.2f} 倍，请核对配置")
        self._note_equity_change(base, eff)
        return eff

    def _note_equity_change(self, prev: float, now: float) -> None:
        """基数发生显著变化（>20%）时留一条可追溯的事件 —— 入金/提现/大额浮亏都看得见。"""
        try:
            last = float(self.db.get_config_str("hl_last_equity_base") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        if last and abs(now - last) / max(last, 1e-9) < 0.2:
            return
        self.db.set_config("hl_last_equity_base", now)
        self._event(EV_DECISION, f"下单基数 {last:g} → {now:g} U（跟随实时权益）",
                    data={"prev": last, "now": now, "config_base": prev})

    def _record_fill_pnl(self, position_id: int) -> None:
        """用 hl_fills 聚合出该仓的**流水口径**净额，落库并与本地口径比对。

        口径（与 scripts/fill_recon_report.py 同源，见 app/hl/pnl.py）：
          net_fill  = Σclosed_pnl − fee_total（时间窗 [opened_at−300s, closed_at+300s]）
          net_local = (exit_px − entry) × side × sz − fee_total
        `net_local` 用**同一份** fee_total：这样 Δnet 只反映价差/数量的出入（本地记录
        最容易错的地方），不会因为两侧手续费口径不同而恒有差（PR-2 报告同款取舍）。

        **必须有出场腿才写净额**：关账那一刻平仓成交往往还没落库（同步是另一条路径），
        只有入场腿时 net = 0 − 入场费 ≈「亏了一笔手续费」，这比留空更误导。缺出场腿
        时只标 `no_exit_fills` 等下一轮/报告补（不写半截数字）。
        完全没有成交（人工 MEXC 流、首次同步未跑完）→ net_pnl=NULL + `no_fills`，
        绝不写 0 —— 「0 收益」和「不知道」在统计里是完全不同的两件事。
        """
        row = self.db.position_by_id(position_id)
        if row is None:
            return
        coin = self._coin_or_none(str(row["symbol"]))
        if coin is None:
            return
        lo, hi = hl_pnl.window_bounds(row["opened_at"], row["closed_at"])
        if lo is None:
            return                                   # 没有 opened_at：无法定窗口
        # 平仓成交通常在关账后几十秒才进库：先同步一次再算，否则必然只有入场腿。
        # 10s 内已经同步过就不再打接口（_on_exit 刚同步过一次，重复拉只会拖慢关账）。
        if time.time() - float(getattr(self, "_last_sync_at", 0.0) or 0.0) > 10.0:
            try:
                self._sync_fills()
            except Exception as e:                                   # noqa: BLE001
                logger.warning("[hl-exec] 关账后同步成交失败（净额可能缺出场腿）：%s", e)
        agg = self._agg_window(coin, row["opened_at"], row["closed_at"], row["side"])
        if agg["n_fills"] <= 0:
            self.db.set_pos_pnl(position_id, None, PNL_SRC_NO_FILLS)
            return
        if agg["exit_sz"] <= 0:
            self.db.set_pos_pnl(position_id, None, PNL_SRC_NO_EXIT_FILLS)
            return
        net_fill = float(agg["net"])
        self.db.set_pos_pnl(position_id, net_fill, PNL_SRC_FILLS)

        # ---- 与本地口径对比（纯观测，不阻断任何流程）
        entry = float(row["actual_entry"] or row["entry_price"] or 0.0)
        exit_px = float(row["exit_price"] or 0.0)
        side = int(row["side"] or 1)
        sz = float(row["hl_sz"] or row["actual_contracts"] or 0.0)
        stop = float(row["actual_stop"] or row["stop_price"] or 0.0)
        if not (entry and exit_px and sz):
            return
        gross_local = (exit_px - entry) * (1 if side == 1 else -1) * sz
        net_local = gross_local - float(agg["fee_total"])
        risk = abs(entry - stop)
        r_local = (((exit_px - entry) * (1 if side == 1 else -1)) / risk) if risk > 0 else None
        r_fill = (net_fill / (risk * sz)) if (risk > 0 and sz > 0) else None
        notional = entry * sz
        diff = hl_pnl.pnl_diff(r_local, r_fill, net_local, net_fill, notional)
        if not (diff["r_diff"] or diff["net_diff"]):
            return
        # 用 EV_RECONCILE：这是「本地记录 vs 交易所流水」的对账差异，不是下单动作，
        # 也不该混进 kill/error 的语义里（不新增枚举，看护与面板的现有分类照旧）。
        self._event(EV_RECONCILE,
                    f"{row['symbol']} 关账口径差异：R 本地 {_fmt_num(r_local)} / 流水 "
                    f"{_fmt_num(r_fill)}（Δ{_fmt_num(diff['d_r'], 3)}，阈值 "
                    f"{diff['r_threshold']:.3f}）；净额 本地 {net_local:+.2f} / 流水 "
                    f"{net_fill:+.2f}（Δ{diff['d_net']:+.2f}，阈值 "
                    f"{diff['net_threshold']:.2f}）",
                    symbol=str(row["symbol"]),
                    data={"pid": int(position_id), "coin": coin,
                          "r_local": r_local, "r_fill": r_fill,
                          "net_local": net_local, "net_fill": net_fill,
                          "fee_total": agg["fee_total"], "entry_sz": agg["entry_sz"],
                          "exit_sz": agg["exit_sz"], "sz_local": sz,
                          "unresolved_fee_cnt": agg["unresolved_fee_cnt"],
                          "diff": diff})

    def _fees_since(self, coin: str, since_ts) -> float:
        """该仓位开仓以来累计手续费（读本地 hl_fills；读不到就返回 0，不影响主流程）。

        下界用「opened_at − 120s」：入场成交发生在 opened_at 写库**之前**，
        用 >= opened_at 会把入场那笔手续费排除掉（净利被高估）。

        取数用**时间窗**（hl_fills_since）而不是「最近 N 条」：多标的并行 + 持仓跨日时，
        500 条滚动窗口会被别的币的成交挤满，目标币的费用被静默漏算（净利虚高）。
        累加优先 ``fee_quote``（已折算成 U），历史行 fee_quote 为 NULL 回退用 ``fee``；
        两者都缺的行跳过（不猜）。
        """
        try:
            since = float(since_ts or 0) - 120.0
        except (TypeError, ValueError):
            return 0.0
        if since < 0:
            since = 0.0
        total = 0.0
        for r in self.db.hl_fills_since(since):
            try:
                if str(r["coin"]) != coin:
                    continue
                ts = r["ts"]
                # 窗口按 created_at（入库时间）取，而「这笔钱什么时候花的」是 ts。
                # 回填/重启导致两者错位时，ts 有值就再卡一次下界；ts 缺失时信窗口。
                if ts is not None and float(ts) < since:
                    continue
                val = r["fee_quote"] if r["fee_quote"] is not None else r["fee"]
                if val is None:
                    continue
                total += float(val)
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        return total

    def _fee_quote(self, fee: float, fee_token: Any) -> Optional[float]:
        """把交易所手续费折算成计价币金额；无法折算返回 None（宁可缺，也不假装是 U）。

        只有 fee_token 属于 QUOTE_FEE_TOKENS 时 fee 才是 U 计价（1:1）。其余币种
        （ARB/HYPE…）需要汇率才能折算，本地没有可信汇率源 → 留 None，由
        ``_fees_since`` 跳过该笔，同时由调用方记一条事件等人工补。
        """
        tok = str(fee_token or "").strip().upper()
        if tok not in QUOTE_FEE_TOKENS:
            return None
        return float(fee or 0.0)

    def _note_unquoted_fee(self, tid: str, fee_token: Any, coin: str, fee: float) -> None:
        """「费用币种未折算」事件一仓一条：同一 tid 只记一次（marker 存 configuration）。

        为什么去重：user_fills 每次返回最近一段窗口，同一笔成交每轮都会出现；不去重
        就是每轮一条事件，把 hl_events 刷成噪声，真异常反而被埋掉。
        """
        try:
            seen = str(self.db.get_config_str(FEE_UNQUOTED_KEY) or "")
            known = [t for t in seen.split(",") if t]
            if tid in known:
                return
            known.append(tid)
            # 只留最近 FEE_UNQUOTED_MAX 个，避免 configuration 行无限增长
            self.db.set_config(FEE_UNQUOTED_KEY, ",".join(known[-FEE_UNQUOTED_MAX:]))
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 手续费去重 marker 读写失败：%s", e)
        self._event(EV_ORDER,
                    f"{coin} 成交 {tid} 手续费以 {fee_token or '未知币种'} 计价（{fee:g}），"
                    f"未能折算成 U 已跳过计入净利，请人工核对",
                    data={"tid": tid, "fee_token": fee_token, "fee": fee})

    def _sync_fills(self, limit: int = 100,
                    fills: Optional[List[dict]] = None) -> int:
        """把交易所的成交同步进 hl_fills（按 tid 去重，幂等）。返回新增条数。

        fee_quote 在写入时就算好（见 ``_fee_quote``）：读侧（``_fees_since``、
        面板净利）不该重复做币种判断，否则每多一个读点就多一处漏判的机会。

        ``fills``：调用方已取到的成交列表（数量对账的**全量回捞**用，见
        ``_refetch_fills``）。不传 = 走原来的 ``user_fills`` 滑动窗口。
        """
        if not self.p.account_address:
            return 0
        if fills is None:
            fills = self.info.user_fills(self.p.account_address) or []
        self._last_sync_at = time.time()
        known = {str(r["tid"]) for r in self.db.hl_recent_fills(500)}
        added = 0
        reverse = {v: k for k, v in (self.p.coin_map or {}).items()}
        for f in fills[:limit]:
            tid = str(f.get("tid") or f.get("hash") or "")
            if not tid or tid in known:
                continue
            coin = str(f.get("coin") or "")
            direction = str(f.get("dir") or "")
            fee = self._num(f.get("fee"))
            fee_token = f.get("feeToken")
            fee_quote = self._fee_quote(fee, fee_token)
            self.db.hl_upsert_fill(
                tid=tid, symbol=reverse.get(coin, f"{coin}_USDT"), coin=coin,
                side=1 if "Long" in direction else -1, sz=self._num(f.get("sz")),
                px=self._num(f.get("px")), fee=fee,
                fee_token=fee_token, fee_quote=fee_quote, dir=direction,
                closed_pnl=self._num(f.get("closedPnl")), hash=f.get("hash"),
                reduce_only=("Close" in direction),
                # oid 是「哪张单打出的这笔成交」：对账要靠它把平仓成交归到本仓的止损单
                # （见 _fill_close_proof）。交易所没给就存 NULL —— 不猜。
                oid=f.get("oid"),
                ts=int(f["time"]) // 1000 if f.get("time") else None)
            added += 1
            if fee_quote is None:
                self._note_unquoted_fee(tid, fee_token, coin, fee)
        if added:
            self._event(EV_ORDER, f"同步 {added} 笔交易所成交到本地", data={"added": added})
        return added

    # ------------------------------------------------------------ 出入金流水
    FLOW_SYNC_DEFAULT = 1800          # 秒；与 Config.hl_flow_sync_s / ExecParams.flow_sync_s 同值
    FLOW_SYNC_BUDGET_S = 20.0         # 单轮最多花多少秒（分页 + 重试都在内）

    def _sync_flows(self, now: Optional[float] = None) -> int:
        """拉 `userNonFundingLedgerUpdates` → 分类落 `hl_flows`（幂等）。返回新增条数。

        间隔：距上次**成功**同步 < `flow_sync_s` 就整段跳过（流水是低频事件，每轮拉
        纯浪费交易所请求）。首次运行 `start_ms=0` 全量回填；之后从
        `最后一条流水时间 − 60s` 续拉（留一分钟重叠吸收交易所的边界抖动，靠
        `UNIQUE(hash, ts)` 去重，不会写两行）。

        失败：只 `logger.warning` + 记 `error` 事件，**不写** `hl_flows_synced_at`
        （下轮会再试；面板读侧看到没有成功标记且无流水 → 不画曲线并显示提示文案）。

        **本轮是否真同步了** 通过实例属性 `self._flows_fresh` 告诉调用方（§4.4.1）：
        只有「真的调了接口且落库成功」才是 True。闸门内跳过 / 接口失败 / 没有地址
        一律 False —— 调用方（DD Guard、L1）必须据此**不推进、不判线**。
        不能用「距上次同步 < 某值」去猜：那正是 v1 把闸门内当成「可信」的原因。
        """
        now = time.time() if now is None else float(now)
        # 每轮先当「本轮没同步」，成功路径再翻成 True —— 这样任何异常分支都不会
        # 留下上一轮的 True（fail-closed：算不出回撤就不判，见 _dd_guard）。
        self._flows_fresh = False
        self._flows_skip = ""
        try:
            interval = int(getattr(self.p, "flow_sync_s", self.FLOW_SYNC_DEFAULT)
                           or self.FLOW_SYNC_DEFAULT)
        except (TypeError, ValueError):
            interval = self.FLOW_SYNC_DEFAULT
        try:
            synced_at = float(str(self.db.get_config_str("hl_flows_synced_at", "0") or "0"))
        except (TypeError, ValueError):
            synced_at = 0.0
        if synced_at and (now - synced_at) < interval:
            # 未到间隔：**本轮没有调接口**，所以「本轮拿不到新流水」。本地这份是上一次
            # 成功同步的，读曲线照旧可信；但**判据不能用它**：这 30 分钟内的出入金还
            # 在交易所那边没落地，拿它当 flow_k=0 就是把出入金当盈亏（§4.4.1 高1）。
            self._flows_skip = "尚未到流水同步间隔"
            return 0                        # 未到间隔，本轮不拉

        address = str(getattr(self.p, "account_address", "") or "")
        if not address:
            self._flows_skip = "未配置账户地址"
            return 0
        last_ts = self.db.hl_flow_last_ts()
        start_ms = 0 if last_ts is None else int(max(0.0, last_ts - 60.0) * 1000)
        try:
            rows = self.info.ledger_updates(
                address, start_ms=start_ms,
                deadline_ts=time.time() + self.FLOW_SYNC_BUDGET_S)
        except Exception as e:                                       # noqa: BLE001
            # 账本接口坏了不影响交易：只记一次事件 + warning，下轮再来
            logger.warning("[hl-exec] 拉取出入金流水失败（下轮重试）：%s", e)
            self._flows_skip = "出入金流水接口读取失败"
            self._event(EV_ERROR, f"出入金流水同步失败（盈亏曲线暂用旧数据）：{e}"[:300],
                        data={"start_ms": start_ms, "error": str(e)[:300]})
            return 0

        res = hl_flows.record_flows(self.db, rows, address, now=now)
        # 流水落库成功 = 本轮回撤/L1 可以按「剔除出入金」的口径算（§3.1 / §5）。
        self._flows_fresh = True
        # 只有**成功**才写时间水位：失败时写它会让「刚失败」看起来像「刚同步过」，
        # 面板会以为流水是新的，而实际上一条都没有。
        try:
            self.db.set_config("hl_flows_synced_at", str(int(now)))
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 写 hl_flows_synced_at 失败（忽略）：%s", e)
        if res["added"]:
            logger.info("[hl-exec] 新增 %d 笔出入金流水（拉到 %d 条）", res["added"], res["seen"])

        # 资金费同步搭同一趟闸门（feat/funds-ledger，spec：不新增调度 job、不加限流
        # 负担）：只有这轮真的开了流水闸门，才顺带拉一次 userFunding。失败只记事件
        # + warning、下轮随闸门重试 —— **绝不影响 _flows_fresh**（那是流水自己的可信
        # 标记，回撤/DD Guard 靠它 fail-closed；资金费是纯记账，没资格弄脏它）。
        # 失败事件 24h 去重（L①）：事件是喊人的，复读 48 遍不是喊人是刷屏。
        if not hasattr(self.info, "user_funding_history"):
            # 测试替身/降级 info 没有 userFunding 能力 → 跳过，但首次要说一声（L②），
            # 不再静默——静默跳过会让「资金费为什么一直是空的」无从排查。
            if not self._funding_warned:
                self._funding_warned = True
                logger.warning("[hl-exec] info 不具备 userFunding 能力，资金费同步跳过")
        elif hasattr(self.db, "hl_funding_last_ts"):
            try:
                hl_funding.sync_funding(
                    self.info, self.db, address,
                    deadline_ts=time.time() + self.FLOW_SYNC_BUDGET_S)
            except Exception as e:                                   # noqa: BLE001
                logger.warning("[hl-exec] 拉取资金费失败（下轮随闸门重试）：%s", e)
                if not _funding_fail_logged(self.db, time.time()):
                    self._event(EV_ERROR, f"资金费同步失败（流水表暂缺资金费行）：{e}"[:300],
                                data={"error": str(e)[:300], "src": "funding_sync"})
        return res["added"]


    # ------------------------------------------------------------ 回撤熔断（DD Guard）
    # 规格 §3：判据是**策略口径**的峰谷回撤（TWR 链、剔除出入金），不是账户权益回撤。
    # 本段只做三件事：只读计算、置 kill、推送告警 —— 不平仓、不撤单、不改下单语义
    # （§2 / §11：熔断只挡「新增风险」，持仓按既有规则离场）。
    def _flows_all(self) -> Optional[List[Any]]:
        """读全部出入金流水（一生几百条级别，一次读全表最省事）。读不到 → None。

        窗口语义不在这里做：`hl_dd.net_flow(flows, lo, hi)` 是唯一的左开区间实现
        （`(lo, hi]`），三处调用（锚点累计、每步 flow_k、L1 当日累计）共用它，
        免得「同一个窗口」在不同地方各写一遍、边界差一笔。
        """
        try:
            return list(self.db.hl_flows_since(0.0))
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-exec] 读出入金流水失败（回撤/当日亏损退回保守口径）：%s", e)
            return None

    def _dd_due(self, key: str, window_s: float, now: float) -> bool:
        """节流判断：距上次**推送成功**的时间 >= window_s 才再推。

        节流键只在推送成功后才写（_run_locked 末尾的 tuple 机制），所以这里读到的
        一定是「用户真的看到过」的时刻，而不是「我们尝试过」的时刻。
        """
        try:
            last = float(str(self.db.get_config_str(key, "0") or "0"))
        except (TypeError, ValueError):
            last = 0.0
        return (float(now) - last) >= float(window_s)

    def _dd_unavailable(self, alerts: List[str], reason: str, now: float) -> None:
        """口径不可用：**不判、不 kill**，只在 24h 内最多提示一次（§8）。

        为什么不是每轮都喊：口径坏掉通常要人修（流水接口/权限），每 5 分钟一条
        会把真正要看的告警淹掉。事件与告警同一个节流窗口 —— 否则 hl_events 里每轮
        一条「口径暂不可用」，记录页会被刷满，反而看不见真正的事件。

        【v2·高1b】文案不再承诺「L1 仍在生效」：L1 同样依赖本轮流水，闸门内/接口失败
        时它也不判（旧口径会把提现当亏损误杀）。这里必须说清「两条线本轮都没判」，
        否则用户会以为还有一条兜底在看着。
        """
        # 【低6·v3】事件与告警各有一个 24h 窗口，**挂在不同的键上**：
        #   - 告警：键只在**推送成功**后回写（_run_locked 末尾的 tuple 机制）→ 推送失败
        #     下轮重推，用户最终一定看得到；
        #   - 事件：键在**尝试**时就写（与推送成败无关）→ 否则推送一失败（电报挂了/网络
        #     抖动），告警键永远是 0，下一轮 _dd_due 又放行，hl_events 里每轮多一条一模
        #     一样的 dd 事件，一天 288 条把记录页刷满，真正要看的告警被淹没。
        #     v2 只是把事件和告警塞进**同一个**窗口，所以「推送失败」这条路径上事件仍然
        #     每轮重复（tests/test_dd_guard_remaining.py 里有回归用例钉住）。
        if self._dd_due(hl_dd.UNAVAIL_EV_KEY, hl_dd.UNAVAIL_THROTTLE_S, now):
            try:
                self.db.set_config(hl_dd.UNAVAIL_EV_KEY, str(int(float(now))))
            except Exception as ex:                               # noqa: BLE001
                logger.warning("[hl-exec] dd 事件节流键写入失败（忽略）：%s", ex)
            self._event(EV_DD, f"回撤口径暂不可用（{reason}），本轮未做回撤/当日亏损判断",
                        data={"reason": reason, "ts": now})
        if not self._dd_due(hl_dd.UNAVAIL_KEY, hl_dd.UNAVAIL_THROTTLE_S, now):
            return
        alerts.append((hl_dd.UNAVAIL_KEY,
                       f"【HL执行】ℹ️ 回撤口径暂不可用（{reason}），本轮未做回撤判断；"
                       f"当日亏损熔断同样未判（它也要本轮流水才算得准）。"
                       f"下一轮同步到流水后会立刻恢复。"))

    def _dd_note_wait(self, note: str) -> None:
        """把「本轮为什么没判」记进 configuration（面板可见；空串 = 已恢复正常）。

        只在内容变化时写库：这个键会随每轮 tick 被调用，每轮都写一次 configuration
        是没必要的写放大。
        """
        try:
            cur = str(self.db.get_config_str(hl_dd.WAIT_KEY, "") or "")
        except Exception:                                          # noqa: BLE001
            cur = ""
        if cur == str(note or ""):
            return
        try:
            self.db.set_config(hl_dd.WAIT_KEY, str(note or ""))
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-exec] 写回撤等待说明失败（忽略）：%s", e)


    def _dd_bootstrap(self, flows: List[Any], now_ts: float) -> Optional[Dict[str, Any]]:
        """首次建立锚点 + **逐张快照链式重放**（§4.3.1），返回待持久化状态。

        为什么必须重放而不是「锚点 → 当前」一步算：锚点距今可能已经跨过一次入金，
        一步算等于把那笔入金当成亏损（实测 index 88.75 vs 97.68，DD 差 7.8 个百分点，
        够让没亏到线的账户凭空触发 25% 告警）。重放把每一步的流水单独扣掉，入金前后
        指数自然连续。

        重放范围用**全表**：30 天前的快照正是 bootstrap 要用的（prune 之后靠 state 续算）。
        """
        anchor = None
        try:
            anchor = self.db.hl_first_positive_snapshot()
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-exec] 取首张正权益快照失败（改用扫描）：%s", e)
        snaps: List[Any] = []
        try:
            # 【v2·低3】必须取**最近** N 张：`hl_snapshots(0, N)` 是 ASC LIMIT N，
            # 表里超过 N 张时拿到的是最老的 N 张，最近那段被丢掉 → 最后一步落后于
            # 真实最新（实测 20005 张时落后 0.3h），被压成一步算 = 把跨过的入金当亏损。
            snaps = list(self.db.hl_snapshots_recent(20000) or [])
        except AttributeError:
            # 兼容老库/替身没有新方法：退回旧取法（仍比不重放好）
            try:
                snaps = list(self.db.hl_snapshots(0.0, 20000) or [])
            except Exception as e:                                 # noqa: BLE001
                logger.warning("[hl-exec] 读快照全表失败（无法 bootstrap 回撤）：%s", e)
                return None
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-exec] 读快照全表失败（无法 bootstrap 回撤）：%s", e)
            return None

        if anchor is None:
            # 兼容替身/老库没有 hl_first_positive_snapshot：自己扫一遍（只认 equity>0）
            for r in snaps:
                try:
                    if float(r["equity"] or 0.0) > 0:
                        anchor = r
                        break
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
        if anchor is None:
            return None
        try:
            a_ts = float(anchor["ts"] or 0.0)
            a_eq = float(anchor["equity"] or 0.0)
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if a_ts <= 0 or a_eq <= 0:
            return None

        replay = hl_dd.bootstrap(snaps, flows, anchor_ts=a_ts, anchor_eq=a_eq)
        st = hl_dd.make_state(anchor_ts=a_ts, anchor_eq=a_eq,
                              anchor_cumflow=hl_dd.cum_flow(flows, a_ts),
                              replay=replay, now=now_ts)
        try:
            hl_dd.save_state(self.db, st)
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-exec] 写 hl_dd_state 失败（下轮重新 bootstrap）：%s", e)
        self._event(
            EV_DD,
            f"回撤锚点建立：锚点权益 {a_eq:,.2f}U（{len(snaps)} 张快照重放 {replay['points']} 步）"
            f" → 指数 {st['index']:.2f} · 峰值 {st['peak_index']:.2f}",
            data={"anchor_ts": a_ts, "anchor_eq": a_eq, "index": round(st["index"], 6),
                  "peak_index": round(st["peak_index"], 6), "points": replay["points"],
                  "twr_skipped": replay["twr_skipped"]})
        return st

    @staticmethod
    def _dd_warn_text(dd_pct: float, th: dict, equity: float, net_dep: float) -> str:
        """L2 文案：必须**说清当前动作**（L2 不停手），否则用户会以为已经停了。"""
        return ("【HL执行】⚠️ 策略回撤 %.1f%%（≥ 告警线 %g%%）\n"
                "峰值以来最大回撤，当前权益 %s U，净入金 %s U。\n"
                "已暂停开新仓吗？未暂停 —— 达 %g%% 会自动熔断（停开新仓，不平现有仓）。"
                % (dd_pct * 100.0, th["warn"] * 100.0, f"{equity:,.2f}",
                   f"{net_dep:,.2f}", th["kill"] * 100.0))

    @staticmethod
    def _dd_kill_text(dd_pct: float, th: dict, repeat: bool = False) -> str:
        head = ("🛑 回撤熔断仍在生效：策略回撤 %.1f%% ≥ 熔断线 %g%%" if repeat
                else "🛑 回撤熔断：策略回撤 %.1f%% ≥ 熔断线 %g%%")
        return ("【HL执行】" + (head % (dd_pct * 100.0, th["kill"] * 100.0)) +
                "\n已停止开新仓（持仓按既有规则离场，不强制平仓）。\n"
                "请人工核对后到「开关」页解除。")

    def _dd_guard(self, snap: dict, alerts: List[str]) -> Optional[Dict[str, Any]]:
        """算一轮策略口径回撤并判线（L2 告警 / L3 熔断）。返回本轮结果，未判 → None。

        流程严格照 §4.3（v2）：
          本轮流水新鲜？ → 读状态 → （无锚点则 bootstrap 重放）→ 推进一格 → 写回 → 判线。
        `ts_now <= last_ts`（时钟回拨/同秒重复）时**不推进也不写库**，直接用现有
        index 判线 —— 再推一次会把这段收益率算两遍。
        """
        if not bool(getattr(self.p, "dd_guard", True)):
            return None
        now_ts = float(snap.get("ts") or time.time())
        equity_now = float(snap.get("equity_total", snap.get("equity")) or 0.0)
        # 现货读不到时 equity_total 只剩浮盈（不是账户总值）→ 算出来的回撤是假的
        if not bool(snap.get("spot_ok", True)):
            logger.warning("[hl-exec] 现货余额读取失败（spot_ok=False），本轮跳过回撤判断")
            return None
        # 【v2·高1 / §4.4.1】第一步就是「本轮真的同步到流水了吗」：
        # 闸门内跳过 / 接口失败 → **不推进指数链、不判线**。v1 把闸门内当成「本地这份
        # 可信」，于是 flow_k=0 → 这 30 分钟内的出入金被全额算成盈亏，而且指数链只进
        # 不退（实测提现 500U → 误 kill 当日亏损 −50%、index 永久停在 50）。
        if not self._flows_fresh:
            reason = self._flows_skip or "本轮未同步到出入金流水"
            if reason == "尚未到流水同步间隔":
                # 闸门内跳过是**正常节奏**（流水 30 分钟一次、tick 5 分钟一轮）：
                # 不推「口径不可用」（那会变成一天一条恒定噪音），但同样不推进、不判。
                logger.debug("[hl-exec] 流水闸门内：本轮不推进回撤指数、不判线")
            else:
                self._dd_unavailable(alerts, reason, now_ts)
                self._dd_note_wait(f"等待流水同步（{reason}）")
            return None
        flows = self._flows_all()
        if flows is None:
            self._dd_unavailable(alerts, "读取出入金流水失败", now_ts)
            self._dd_note_wait("等待流水同步（读取出入金流水失败）")
            return None

        st = hl_dd.load_state(self.db)
        if st is None:
            st = self._dd_bootstrap(flows, now_ts)
            if st is None:
                self._dd_unavailable(alerts, "找不到正权益快照，无法建立回撤锚点", now_ts)
                return None

        prev_ts = float(st["last_ts"])
        flow_k = hl_dd.net_flow(flows, prev_ts, now_ts)      # 落在 (last_ts, now] 的净流水
        new = hl_dd.step(st, ts_now=now_ts, equity_now=equity_now, flow_k=flow_k)
        if new is None:
            # 状态坏到没法续算（缺 index / 时间戳非法）：不要让这条线永久停在这里，
            # 清掉状态让下一轮用锚点重放重建（与低2 同一个自愈思路）。
            self._event(EV_ERROR, "回撤状态无法续算（状态字段缺失），已清空 → 下轮用锚点重放重建",
                        data={"last_ts": st.get("last_ts"), "equity": equity_now})
            try:
                self.db.set_config(hl_dd.STATE_KEY, "")
            except Exception as e:                                 # noqa: BLE001
                logger.warning("[hl-exec] 清空 hl_dd_state 失败（忽略）：%s", e)
            return None
        if new.get("neg"):
            self._dd_negative(new, flows, alerts, now_ts, flow_k, equity_now)
            return None
        advanced = float(new.get("last_ts") or 0.0) > prev_ts
        if advanced:
            new["updated_at"] = now_ts
            try:
                hl_dd.save_state(self.db, new)
            except Exception as e:                                 # noqa: BLE001
                logger.warning("[hl-exec] 写 hl_dd_state 失败（下轮从旧状态续算）：%s", e)
            # 这一轮真的用**正确的 flow_k** 推进了 → 之前「等待流水同步」的说明作废。
            self._dd_note_wait("")

        dd_pct = hl_dd.drawdown(new.get("index"), new.get("peak_index"))
        if dd_pct is None:
            return None
        th = hl_dd.thresholds(getattr(self.p, "risk_pct", 0.02),
                              getattr(self.p, "dd_warn_pct", 0.0),
                              getattr(self.p, "dd_kill_pct", 0.0))
        res = {"ok": True, "dd": dd_pct, "index": float(new["index"]),
               "peak_index": float(new["peak_index"]), "warn": th["warn"], "kill": th["kill"],
               "auto": th["auto"], "advanced": advanced, "ts": now_ts,
               "twr_skipped": int(new.get("twr_skipped") or 0)}

        killed = False
        origin = ""
        try:
            killed = bool(self.db.hl_kill())
            origin = str(self.db.hl_kill_origin() or "")
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-exec] 读 kill 状态失败（按未触发处理）：%s", e)

        if dd_pct >= th["kill"]:
            if not killed:
                self.db.hl_set_kill(
                    True, f"策略回撤 {dd_pct * 100:.1f}%（≥ 熔断线 {th['kill'] * 100:g}%）",
                    origin="dd")
                self._event(EV_KILL,
                            f"策略回撤 {dd_pct * 100:.1f}% ≥ 熔断线 {th['kill'] * 100:g}% → "
                            f"kill（origin=dd，只停开新仓，人工解除）",
                            data={"dd": round(dd_pct, 6), "index": round(res["index"], 6),
                                  "peak_index": round(res["peak_index"], 6),
                                  "warn": th["warn"], "kill": th["kill"]})
                alerts.append((hl_dd.KILL_KEY, self._dd_kill_text(dd_pct, th, repeat=False)))
            elif origin == "dd":
                # 已经在 dd 熔断里：每 6h 提醒一次「仍在熔断中」，别每轮都喊
                if self._dd_due(hl_dd.KILL_KEY, hl_dd.KILL_REPEAT_S, now_ts):
                    alerts.append((hl_dd.KILL_KEY, self._dd_kill_text(dd_pct, th, repeat=True)))
            else:
                # 已有别的来源造成停手（人工/对账/当日亏损/数量/到期）：**不改写 origin**。
                # 改写会把「人工停手」变成「可自解的对账 kill」或丢掉归因（与 H1 守卫同口径）。
                self._event(EV_KILL,
                            f"策略回撤 {dd_pct * 100:.1f}% ≥ 熔断线，但 kill 已由 "
                            f"{origin or '未知来源'} 触发：保留原 origin，不重复置位",
                            data={"origin": origin, "dd": round(dd_pct, 6)})
            res["killed"] = True
        elif dd_pct >= th["warn"] and not killed:
            res["killed"] = False
            if self._dd_due(hl_dd.ALERT_KEY, hl_dd.ALERT_THROTTLE_S, now_ts):
                net_dep = hl_dd.cum_flow(flows, now_ts)
                alerts.append((hl_dd.ALERT_KEY,
                               self._dd_warn_text(dd_pct, th, equity_now, net_dep)))
        else:
            res["killed"] = killed
        return res

    def _dd_negative(self, new: Dict[str, Any], flows: List[Any], alerts: List[str],
                     now_ts: float, flow_k: float, equity_now: float) -> None:
        """【v2·低2】指数算出负值：权益与流水自相矛盾（多半是流水缺了一笔大额提现）。

        v1 的写法是「返回 None 且不写 state」→ `last_ts` 永不推进 → 窗口越拖越宽，
        权益真的塌了也**永远不判** DD（永久卡死）。v2 三步：
          1. 记 error 事件；2. **推进 last_ts**（窗口不再无限拖长，指数原地不动）；
          3. 连续 >= `NEG_REPLAY_STREAK` 轮 → 推一条「口径卡死，请人工核对」+ 用锚点
             重放兜底（bootstrap 一次重建 index/peak，自愈路径）。
        本轮**仍然不判线**（负值不猜）。
        """
        streak = int(new.get("neg_streak") or 0)
        new["updated_at"] = now_ts
        try:
            # 只推进 last_ts/last_eq/neg_streak；index 保持上一个合法值（绝不写负数）
            hl_dd.save_state(self.db, new)
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-exec] 写 hl_dd_state 失败（负值轮，下轮重试）：%s", e)
        self._event(EV_ERROR,
                    f"回撤指数算出负值（权益/流水自相矛盾），本轮不判（连续 {streak} 轮）",
                    data={"prev_eq": new.get("last_eq"), "equity": equity_now,
                          "flow_k": round(flow_k, 6), "neg_streak": streak})
        if streak < hl_dd.NEG_REPLAY_STREAK:
            return
        # 卡死自愈：清掉状态，用锚点重放重建一条可信的链（这一步每轮都会尝试，
        # 但告警 24h 只推一次 —— 不要把一个持续存在的数据问题喊成百万条推送）。
        rebuilt = None
        try:
            self.db.set_config(hl_dd.STATE_KEY, "")
            rebuilt = self._dd_bootstrap(flows, now_ts)
        except Exception as e:                                     # noqa: BLE001
            logger.warning("[hl-exec] 口径卡死后的锚点重放失败（下轮再试）：%s", e)
        if not self._dd_due(hl_dd.STUCK_KEY, hl_dd.UNAVAIL_THROTTLE_S, now_ts):
            return
        self._event(EV_DD,
                    f"回撤口径卡死（连续 {streak} 轮算出负指数）→ "
                    f"{'已用锚点重放重建基线' if rebuilt else '锚点重放也失败'}，请人工核对流水/权益",
                    data={"neg_streak": streak, "rebuilt": bool(rebuilt),
                          "index": (rebuilt or {}).get("index")})
        alerts.append((hl_dd.STUCK_KEY,
                       f"【HL执行】⚠️ 回撤口径卡死（连续 {streak} 轮算出负指数，"
                       f"通常是出入金流水缺了一笔或权益口径异常），"
                       f"{'已用锚点重放重建基线' if rebuilt else '锚点重放也失败（找不到正权益快照）'}，"
                       f"本轮未判回撤。请人工核对出入金流水与账户权益。"))


    def _r_multiple(self, row, exit_px: float) -> float:
        entry = float(row["actual_entry"] or row["entry_price"])
        stop = float(row["actual_stop"] or row["stop_price"])
        side = int(row["side"] or 1)
        risk = abs(entry - stop)
        if risk <= 0:
            return 0.0
        move = (exit_px - entry) if side == 1 else (entry - exit_px)
        return move / risk

    def _close_local(self, position_id: int, px: float, reason: str, sz: float) -> None:
        """本地关账（所有关账路径的唯一出口）。

        PR-3：关账前后各做一件事（都在关账**之后**算，因为要先有 closed_at）：
          - 用 hl_fills 聚合出流水口径净额写进 `positions.net_pnl`（`pnl_source` 标注
            来源）—— 无论开关开没开都写，纯观测；
          - 与本地价差口径 `r_multiple` 比一遍，差异超阈值记一条事件（不阻断、不告警）：
            这类偏差既可能是本地记错价、也可能是手续费/部分成交，先让它被看见。
        任何一步失败都不影响关账本身（关账是主流程，观测是附加）。
        """
        self.db.close_position(position_id, px, reason, auto=False)
        self.db.set_pos_hl(position_id, hl_status="closed")
        # 关账后宽限键必须清掉（对抗审查 L1）：仓已经 closed，再没有人会去读/删这个键，
        # 它就成了 configuration 表里的永久垃圾（每个仓一条）；面板的「剩余宽限」也会
        # 拿旧 first_seen 算出一个不存在的倒计时。这里统一清 —— 所有关账路径都走本方法，
        # 于是「关账 → 键消失」只需要在一个地方保证。
        self._grace_clear(int(position_id))
        try:
            self._record_fill_pnl(position_id)
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 关账后计算流水口径净利失败（忽略）：%s", e)

    def _last_fill_px(self, coin: str) -> Tuple[Optional[float], List[dict]]:
        """从交易所成交里取最近一笔该 coin 的**平仓**成交价（用于止损已触发的关账）。

        必须过滤 dir 含 "Close" —— 否则同标的重开后会取到开仓价当平仓价，R 值全错。

        没有平仓腿时返回 ``None`` + 该币成交原文（**证据不足，不给价**；对抗审查 M2）。
        以前这里 ``source = closing or mine``：一旦窗口里只有开仓腿，就把**开仓价**
        当成交价返回，调用方拿它关账 → R=0、exit_reason='stop'、还不 kill —— 一笔
        凭空捏造在开仓价上的平仓，比不关账更糟。第二个返回值只为诊断（日志/事件里
        能看到「我读到了哪些成交」），调用方不得拿它当价格。
        """
        try:
            fills = self.info.user_fills(self.p.account_address) or []
        except HlInfoError:
            return None, []
        mine = [f for f in fills if f.get("coin") == coin]
        if not mine:
            return None, []
        closing = [f for f in mine if "Close" in str(f.get("dir") or "")]
        if not closing:                  # 没有平仓腿 → 不给价（见 docstring）
            return None, mine[:8]
        px = None
        for f in closing[:8]:
            try:
                px = float(f.get("px"))
            except (TypeError, ValueError):
                continue
            break
        return px, mine[:8]

    # ------------------------------------------------------------ 到期保护清仓
    def flatten_for_expiry(self, reason: str = "agent 到期保护") -> dict:
        """API 钱包到期前的保护性清仓（撤全部挂单 → 逐仓市价平 → 复查 → 本地关账）。

        互斥：持**进程级** ``_op_lock``（与 run/on_entry/on_exit 同一把，所有 Executor
        实例共用）—— 清仓期间不可能有新的下单序列插进来；调用方（app.main）另持
        进程内 ``_tick_lock`` 与 tick 互斥。同时置 `agent.cleanup_session()` 标记，
        让 `_repair_missing_stop` 这类「顺手动交易所」的路径主动让路。
        kill 置位由调用方在**调本方法之前**完成（见 app.hl.agent.check_once）。

        返回 app.hl.agent.flatten_all 的结果字典；本方法不抛异常。
        """
        from app.hl import agent as agent_mod
        with self._op_lock:
            with agent_mod.cleanup_session():
                self._event(EV_DECISION, f"到期保护清仓开始：{reason}")
                res = agent_mod.flatten_all(self.db, self.p, self.info, self.ex, self.notifier,
                                            log_event=self._event)
                self._event(EV_ORDER if res.get("ok") else EV_ERROR,
                            f"到期保护清仓{'完成' if res.get('ok') else '失败'}："
                            f"{res.get('error') or f'{len(res.get('closed') or [])} 个标的已平'}")
                return res

    # ------------------------------------------------------------ 窄口径自动解 kill
    def _kill_clean_read(self) -> int:
        """连续干净轮次计数（configuration 表；读不懂按 0 —— 宁可多数两轮）。"""
        try:
            return max(0, int(float(str(self.db.get_config_str(KILL_CLEAN_KEY, "0") or "0"))))
        except Exception:                                             # noqa: BLE001
            return 0

    def _kill_clean_ts_read(self) -> float:
        """上一轮**被计数**的时刻（0 = 还没有计数过）；读不懂按 0。"""
        try:
            return max(0.0, float(str(self.db.get_config_str(KILL_CLEAN_TS_KEY, "0") or "0")))
        except Exception:                                             # noqa: BLE001
            return 0.0

    def _kill_clean_ts_write(self, ts: float) -> None:
        try:
            self.db.set_config(KILL_CLEAN_TS_KEY, f"{max(0.0, float(ts)):.3f}")
        except Exception as e:                                        # noqa: BLE001
            logger.warning("[hl-exec] 写干净轮次时间戳失败（本轮不解除 kill）：%s", e)

    def _kill_clean_write(self, n: int) -> None:
        try:
            self.db.set_config(KILL_CLEAN_KEY, str(int(max(0, n))))
        except Exception as e:                                        # noqa: BLE001
            logger.warning("[hl-exec] 写干净轮次计数失败（本轮不解除 kill）：%s", e)
        # 计数归零 = 新的一段「连续干净」开始了：连同时间戳一起清，否则新一段的第一轮
        # 会被上一段留下的时间戳挡住（最多白等一个间隔）。
        if int(n) <= 0:
            self._kill_clean_ts_write(0.0)

    def _round_is_clean(self, rec: dict, snap: Optional[dict]) -> bool:
        """本轮算不算「干净」—— 四条全真才算，任一不满足就归零重数。

        1. 对账 ok（problems 为空）且**不是** unknown：快照读不到 = 什么都不知道，
           不是「干净」（把「读不到账户」当成恢复条件，等于接口一抖就自解 kill）；
        2. 本地没有 open/pending 行：有仓还在，账就还没对上（closing 行的 status 也是
           open，所以这一条同时覆盖「宽限期待核对」）；
        3. 挂单列表可信（`orders_trusted`）且确实为空：读不到挂单就不知道有没有残留
           触发单，凭这个解除 kill 可能放着一堆幽灵单去开新仓；
        4. kill 仍然是 origin='reconcile'（由调用方判，见 _maintain_kill_autoclear）。
        """
        if rec.get("unknown") or not rec.get("ok"):
            return False
        try:
            for r in self.db.recent_positions(50):
                if r["status"] in ("open", "pending"):
                    return False
        except Exception as e:                                        # noqa: BLE001
            logger.warning("[hl-exec] 读本地仓位失败（本轮不解除 kill）：%s", e)
            return False
        if not isinstance(snap, dict) or not bool(snap.get("orders_trusted", False)):
            return False
        return not (snap.get("open_orders") or [])

    def _maintain_kill_autoclear(self, rec: dict, snap: Optional[dict] = None) -> List[str]:
        """对账之后维护「连续干净轮次」，够 2 轮就把**对账自己造成的** kill 解除。

        为什么只认 origin='reconcile'（窄口径）：自动解除的本质是「这个停手是时滞
        造成的假警报，现在证据齐了」。人工按的 kill（可能是人看到了机器看不见的东西）、
        当日亏损熔断（钱真的亏了）、数量对账（本地记的量错了）、到期保护（钱包要过期）
        都不是时滞 —— 对这四类自解 = 把真问题静音。所以只解除自己造成的那一种。

        为什么放在对账**之后**：判据全部来自本轮对账结果，先判等于用上一轮的世界
        决定要不要放开交易。
        """
        alerts: List[str] = []
        try:
            killed = bool(self.db.hl_kill())
            origin = str(self.db.hl_kill_origin() or "")
        except Exception as e:                                        # noqa: BLE001
            logger.warning("[hl-exec] 读 kill 状态/来源失败（本轮不解除）：%s", e)
            return alerts
        if not killed or origin != "reconcile":
            # 非「对账造成的」kill（或本来就没 kill）：计数归零，避免旧计数留到下一次
            # 对账异常时被直接凑成「已经干净两轮」。
            self._kill_clean_write(0)
            return alerts
        if not self._round_is_clean(rec, snap):
            self._kill_clean_write(0)
            return alerts
        # M3：两次「干净轮」之间必须有最小间隔。run() 正常 5 分钟一轮，但手工触发/重启
        # 后的追赶 tick 可以只隔 0.2 秒 —— 那只是对同一份世界判了两次，凑不出「账自己
        # 收敛了」。间隔不足：**不计数、也不归零**（这一轮既不是新的干净证据，也没脏）。
        now = time.time()
        last_ts = self._kill_clean_ts_read()
        if last_ts > 0 and (now - last_ts) < KILL_CLEAN_MIN_GAP_S:
            logger.info("[hl-exec] 干净轮间隔 %.1fs < %gs，本轮不计数（避免同一世界凑两轮）",
                        now - last_ts, KILL_CLEAN_MIN_GAP_S)
            return alerts
        n = self._kill_clean_read() + 1
        self._kill_clean_write(n)
        self._kill_clean_ts_write(now)
        if not bool(getattr(self.p, "kill_autoclear", True)):
            return alerts                    # 开关关 = 永不自解（计数照记，便于观测）
        if n < KILL_CLEAN_ROUNDS:
            return alerts
        self.db.hl_set_kill(False, "", origin="auto")
        self._kill_clean_write(0)
        self._event(EV_KILL, "对账异常已自动恢复（止损/平仓已按成交价入账）",
                    data={"rounds": n, "origin": "reconcile"})
        # 【v2·中1】文案**不能在这里写死**：本轮稍后 L1/DD 还会再判一遍，如果它们又把
        # kill 拉起来，「已自动恢复自动交易」就成了一条自相矛盾的消息（实测同轮两条
        # 告警：先「已恢复」再「已熔断」）。这里只放一个待定标记，本轮所有判据走完后
        # 在 _run_locked 收尾处按**最终** kill 状态渲染（见 _RoundNote）。
        self._round_autocleared = n
        alerts.append(_RoundNote("autoclear", rounds=n))
        return alerts

    # ------------------------------------------------------------ 每轮
    def run(self, force_snapshot: bool = False) -> List[str]:
        """每轮 tick 调用：快照 → 同步成交 → 对账 → 熔断 → 死手开关 → 心跳。返回告警文案。"""
        with self._op_lock:
            self._gate_new_round()          # 闸门事件限频按轮重置（PR-4）
            return self._run_locked(force_snapshot)

    def _run_locked(self, force_snapshot: bool = False) -> List[str]:
        alerts: List[str] = []
        if self.p.mode == MODE_OFF:
            return alerts
        # 本轮「对账是否自解过 kill」标记按轮重置（§4.5 中1）。
        self._round_autocleared = 0
        # 本轮开始时的 kill 状态：下面「对账异常 → 自动 kill」会把它拉起来，
        # 之后判断「要不要自动补挂止损」必须用这个**进来时**的值（否则修复永远被自己挡）。
        kill_before = bool(self.db.hl_kill())
        snap = self._snapshot(force=force_snapshot)
        if snap is None:
            # 读不到快照也要写心跳（带 degraded 标记）：
            # 看守判活只看 heartbeat 事件的时间 —— 整轮 return 的话，
            # 「活着但读不到账户」会被误报成「执行器死了」。
            self._event(EV_HEARTBEAT, "executor 心跳（读不到账户快照，已降级）",
                        data={"kill": self.db.hl_kill(), "mode": self.p.mode, "degraded": True})
            alerts.append("【HL执行】⚠️ 读不到 HL 账户快照（已连续重试），本轮跳过对账/熔断/成交同步。"
                          "若持续出现请检查网络或 HL 状态。")
            return alerts

        # 1) 同步成交到本地（面板的「最近成交/手续费累计/今日与累计已实现」全靠 hl_fills；
        #    在此之前**没有任何生产代码写过这张表** → 真交易后面板这三项会一直是 0）
        #    顺序**必须在对账之前**：对账要用「本轮刚同步进来的平仓成交」证明「本地有仓、
        #    交易所无仓」是交易所侧止损已成交（见 _fill_close_proof），而不是账实不符。
        #    以前这一步在第 4 位，对账永远比成交同步早一轮 → 止损成交后必被误判 mismatch
        #    并 kill（2026-10-22 事故）。这里不新增任何交易所调用：每轮原本各调一次，
        #    只是换顺序；失败仍必须被吞掉（同步只是观测，坏了不能拖累对账/交易）。
        try:
            self._sync_fills()
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 同步成交失败（不影响交易）：%s", e)

        # 1b) 同步出入金流水 —— **必须早于回撤判断**（§4.4）。
        #     以前它在第 4b 步、熔断判断在第 3 步，等于拿最长 30 分钟前的流水算回撤
        #     （那段时间里的入金会被当成亏损 → 假回撤/假熔断）。`_sync_flows` 自带
        #     30 分钟间隔闸门 + 20s 预算 + 失败只 warning，提前**不增加**交易所调用频率。
        #     与 PR #44 把 `_sync_fills` 提到对账之前是同一纪律：判据必须基于本轮的数据。
        # 【v2·高1】先当「本轮没同步」：`_sync_flows` 内部成功才会翻成 True（它也可能
        # 在中途抛异常被下面吞掉 —— 那时必须是 False，否则回撤会拿脏数据判线）。
        self._flows_fresh = False
        self._flows_skip = "流水同步异常"
        try:
            self._sync_flows()
        except Exception as e:                                       # noqa: BLE001
            self._flows_fresh = False
            self._flows_skip = "流水同步异常"
            logger.warning("[hl-exec] 同步出入金流水失败（不影响交易）：%s", e)

        # 2) 对账
        rec = self.reconcile(snap, kill_before=kill_before)
        if not rec["ok"]:
            problems = rec["problems"]
            self._event(EV_RECONCILE, "对账异常：" + "；".join(problems), data=rec)
            if not rec.get("unknown"):
                # origin='reconcile'：只有这类 kill 允许被「连续 N 轮干净」自动解除
                # （见 _maintain_kill_autoclear）。人工/熔断/数量/到期永不自解。
                # H1（对抗审查）：**已经在 kill 态时不改写 origin**。以前这里无条件写，
                # 于是「人工 kill → 某轮对账异常 → origin 被改写成 reconcile → 世界变干净
                # → 窄口径自解」—— 人工 kill 就这么被悄悄解除了，违反「人工 kill 永不
                # 自动解」。与数量对账（_size_check 同一守卫）同口径：只告警、不动 kill。
                if kill_before:
                    self._event(EV_KILL,
                                "对账异常（kill 已处于触发状态，保留原 origin=%s，"
                                "不改写、不自动解除）" % (self.db.hl_kill_origin() or "unknown"),
                                data={"origin_before": self.db.hl_kill_origin()})
                else:
                    self.db.hl_set_kill(True, "对账异常", origin="reconcile")
                    self._event(EV_KILL, "对账异常 → 自动 kill switch", data=rec)
            # 告警去重：kill 态下每轮 run() 都会走到这里（5 分钟一轮），
            # 同一状态只推送一次 + 每 6 小时提醒一次仍在等人工；恢复由下方 else 分支报告。
            key = "hl_recon_alert_at"
            last_alert = float(self.db.get_config_str(key) or 0)
            due = (time.time() - last_alert) >= 6 * 3600
            if due:
                # 审计三轮 M2：节流键必须在**推送成功后**再写 —— 以前先写后发，
                # 中间任一步异常（补挂/死手/同步抛错）会把该轮告警吞掉且 6h 静默。
                # 现在把文案先放进 alerts（末尾统一 _alert，成功才落库记账），
                # 节流键改为在 _alert 成功后由 pending_throttle 回写。
                alerts.append(("hl_recon_alert_at",
                               "【HL执行】⚠️ 对账异常（已暂停自动交易）：\n- " + "\n- ".join(problems)))
            # 确认「有仓但没止损」→ 不允许无保护持仓存在：先补挂，补不上就市价平掉
            for row in rec.get("missing_stop") or []:
                self._repair_missing_stop(row, snap, kill_before=kill_before)
        elif rec.get("unconfirmed"):
            # 读不到触发单：只提示、绝不动作（否则会误平仓）；同一状态只喊一次
            items = rec["unconfirmed"]
            self.db.hl_log_event(EV_RECONCILE, "无法确认止损（挂单接口回落）：" + "；".join(items),
                                 code_rev=self.p.code_rev)
            if str(self.db.get_config_str("hl_stop_unconfirmed") or "") != "1":
                self.db.set_config("hl_stop_unconfirmed", "1")
                alerts.append("【HL执行】⚠️ 无法确认止损（挂单接口临时回落）：\n- "
                              + "\n- ".join(items)
                              + "\n未做任何动作。请看一眼面板/交易所；恢复后我会再报一次。")
        else:
            if str(self.db.get_config_str("hl_stop_unconfirmed") or "") == "1":
                self.db.set_config("hl_stop_unconfirmed", "0")
                alerts.append("【HL执行】✅ 挂单接口已恢复，止损状态可正常确认")
            # 对账恢复正常：清掉告警节流标记（下次异常立即推送），并告知一次
            if float(self.db.get_config_str("hl_recon_alert_at") or 0) > 0:
                self.db.set_config("hl_recon_alert_at", "0")
                # L5（对抗审查）：宽限期还在进行（closing_pending 非空）时**不许**报
                # 「对账已恢复正常」。problems 为空只说明「暂时没有矛盾」，不是「账已对上」：
                # 对着一个还在等成交证据的仓说恢复，用户刚放心，下一刻就超时 kill。
                if not rec.get("unconfirmed") and not rec.get("closing_pending"):
                    alerts.append("【HL执行】✅ 对账已恢复正常")
            # 数量对账处于「观察期」（首次超差、已回捞、未到两击）→ 不写「对账一致」：
            # 明明有未解释的数量差，日志说一致会误导事后复盘。
            # 宽限期（closing）同理：问题列表为空 ≠ 账已对上，它是「还在等证据」——
            # 写「对账一致」会让事后复盘以为当时就已经对上了。
            if rec.get("size_warn"):
                self.db.hl_log_event(EV_RECONCILE,
                                     "数量对账观察中：" + "；".join(rec["size_warn"]),
                                     code_rev=self.p.code_rev)
            elif rec.get("closing_pending"):
                self.db.hl_log_event(
                    EV_RECONCILE,
                    "平仓成交待核对（宽限中）：" + "；".join(
                        f"{c['symbol']} 剩 {max(0.0, c['grace_s'] - c['elapsed_s']):.0f}s"
                        f"（第 {c['rounds']} 轮）" for c in rec["closing_pending"]),
                    code_rev=self.p.code_rev)
            else:
                self.db.hl_log_event(EV_RECONCILE, "对账一致", code_rev=self.p.code_rev)

        # 宽限期提醒（规格 §2.5）：进入宽限**只推 1 条**，且由 `_grace_step` 的
        # 「第一次写宽限键」那一支负责推 —— 这里以前又拼了一条几乎一样的文案，同一件事
        # 推两遍（短信箱里两条「已进入宽限」，人会以为是两个仓）。同一仓后续轮次
        # new=False，本来也不推。保留这段循环只为留一条诊断事件（不推送）。
        for cp in rec.get("closing_pending") or []:
            if not cp.get("new"):
                continue
            self._event(EV_RECONCILE,
                        f"{cp['symbol']} 宽限开始（最迟 {int(cp['grace_s'] // 60)} 分钟后"
                        f"升级为对账异常；该标的宽限期内不开新仓）",
                        symbol=cp["symbol"], data={"pid": cp.get("position_id")})

        # 窄口径自动解 kill（规格 §4）：只对**对账自己造成的** kill 生效。
        alerts.extend(self._maintain_kill_autoclear(rec, snap))

        # 3) 当日亏损熔断（L1）。基线 = 北京时间零点那张首快照的权益，当前 = 实时权益。
        #    口径（规格 §5 v2）：
        #      daily_loss_rate = ((equity_now − equity_day0) − Σflows(ts_day0_snapshot < ts ≤ now))
        #                        / equity_day0
        #    为什么剔流水：旧口径不含出入金 —— 提现 = 假亏损（可能误 kill），入金把亏损
        #    稀释（该报的不报）。
        #    为什么左端是**首快照自己的时间戳**而不是 day0：落在「零点 → 首快照」之间的
        #    入金已经体现在 equity_day0 里了，再扣一次 = 同一笔钱算两遍 = 假亏损
        #    （实测一笔 50U 落在零点后 0.5s 时，错误口径 −5.78% 会误触发 5% 线，正确口径 −0.96%）。
        #    【v2·高1b】本轮没真同步到流水（闸门内 / 接口失败）→ **不判**：旧口径会把
        #    提现当亏损误杀（实测提现 500U → 误 kill −50%）。慢判 > 错判。
        day0 = _today_start_ts()
        base = self.db.hl_snapshots(since_ts=day0, limit=1)
        spot_ok = bool(snap.get("spot_ok", True)) if snap else False
        flows = self._flows_all()
        flows_fresh = bool(self._flows_fresh) and flows is not None
        if base and not self.db.hl_kill():
            # 统一账户下 snap["equity"]（永续口径）恒为 0 → 必须用账户总值，否则熔断永不触发
            start_eq = float(base[0]["equity"] or 0.0)
            now_eq = float(snap.get("equity_total", snap.get("equity")) or 0.0)
            now_ts = float(snap.get("ts") or time.time())
            # 现货读取失败时 equity_total 只剩浮盈 —— 数据不可信，宁可跳过也不误熔断
            if not spot_ok:
                logger.warning("[hl-exec] 现货余额读取失败，本轮跳过亏损熔断判断（数据不可信）")
            elif start_eq > 0 and not flows_fresh:
                # 不判。留痕分两档（避免正常节奏刷满事件表）：
                #   闸门内跳过（30 分钟同步间隔内的常规轮次）→ 只打 info 日志；
                #   接口失败/无地址 → 记一条事件，并在面板写「等待流水同步」。
                if self._flows_skip == "尚未到流水同步间隔":
                    logger.debug("[hl-exec] 流水闸门内：本轮跳过当日亏损熔断判断")
                else:
                    self._event(EV_DD,
                                f"本轮 L1 未判（{self._flows_skip or '未同步到出入金流水'}），"
                                f"等下一轮同步到流水再判", data={"skip": self._flows_skip})
            elif start_eq > 0:
                try:
                    base_ts = float(base[0]["ts"] or day0)
                except (KeyError, IndexError, TypeError, ValueError):
                    base_ts = float(day0)
                flow_today = hl_dd.net_flow(flows, base_ts, now_ts)
                loss_rate = ((now_eq - start_eq) - flow_today) / start_eq
                if loss_rate <= -self.p.max_daily_loss_pct:
                    self.db.hl_set_kill(True, "当日亏损熔断", origin="daily_loss")
                    self._event(EV_KILL, f"当日亏损 {loss_rate * 100:.1f}% → kill",
                                data={"start_equity": start_eq, "equity": now_eq,
                                      "flows": round(flow_today, 6), "base_ts": base_ts})
                    alerts.append(f"【HL执行】⚠️ 当日亏损 {loss_rate * 100:.1f}%"
                                  f"（阈值 -{self.p.max_daily_loss_pct * 100:g}%）→ 已停止自动交易")

        # 3b) 回撤熔断（DD Guard，L2 告警 / L3 熔断）。判据 = **策略口径**峰谷回撤
        #     （TWR 链、剔除出入金），看的是「温水煮青蛙」型累计回撤 —— 日内 5% 那条线
        #     每天重置，一个每天亏 4% 的系统永远不会触发它。
        #     只置 kill + 推送，**不平仓**：持仓按既有规则（止损单/SMA20 离场）走完。
        try:
            self._dd_guard(snap, alerts)
        except Exception as e:                                       # noqa: BLE001
            # 口径算错绝不能连累交易主流程；但也不能静默：记一条事件留痕。
            logger.warning("[hl-exec] 回撤熔断计算失败（本轮不判）：%s", e)
            self._event(EV_ERROR, f"回撤熔断计算失败（本轮不判）：{e}"[:300])

        # 3c) 交易所侧已按成交证据关账的仓：推一条提醒让人知道「止损/人工平仓已经生效、
        #     账已对上」。**不设节流键**：一笔仓只会关一次，天然只推一次；重复推送只可能
        #     来自重复关账，那本身就是要看见的事。这些仓不进 problems，所以也不触发 kill。
        #     文案里的「未暂停自动交易」必须按**本轮最终**的 kill 状态写（§4.5 中1）：
        #     以前这段跑在 L1/DD 之前，本轮后面刚把 kill 拉起来时它会说「未暂停」——
        #     用户读到的是上一刻的世界。把它挪到所有 kill 判据之后。
        killed_now = bool(self.db.hl_kill())
        kill_reason = str(self.db.get_config_str("hl_exec_kill_reason") or "").strip()
        if killed_now:
            tail = f"但系统仍处暂停状态（{kill_reason or '原因未记录'}）"
        else:
            tail = "未暂停自动交易"
        for fc in rec.get("fill_closed") or []:
            pos_row = self.db.position_by_id(fc["position_id"])
            side = int((pos_row["side"] if pos_row is not None else 1) or 1)
            grace_note = ("（宽限期内已核对到成交，账已对上）" if fc.get("grace_ended") else "")
            if fc["reason"] == "stop":
                r = self._r_multiple(pos_row, fc["px"]) if pos_row is not None else 0.0
                alerts.append(f"【HL执行】🛑 止损已在交易所成交 · {fc['coin']} {_side_word(side)} "
                              f"@ {fc['px']:,.2f}，本地已按成交价关账（R {r:+.2f}），{tail}{grace_note}")
            else:
                alerts.append(f"【HL执行】ℹ️ {fc['coin']} 已在交易所被平仓（人工/外部）"
                              f"@ {fc['px']:,.2f}，本地已按成交价关账，{tail}{grace_note}")

        # 3d) 文案收尾（§4.5 中1）：本轮「待定文案」按**最终** kill 状态渲染。
        #     同轮内 kill 只允许「未触发 → 触发」这一方向；若对账刚自解又被回撤熔断
        #     （或当日亏损线）拉起来，最终状态仍是「停手」，文案就不能说「已恢复」。
        for i, a in enumerate(alerts):
            if isinstance(a, _RoundNote):
                alerts[i] = a.render(bool(self.db.hl_kill()),
                                     str(self.db.get_config_str("hl_exec_kill_reason")
                                         or "").strip())
        if self._round_autocleared and bool(self.db.hl_kill()):
            self._event(EV_KILL,
                        "本轮对账已收敛，但其它保护仍要求停手：kill 保留（同轮只允许单向变化）",
                        data={"rounds": self._round_autocleared,
                              "reason": str(self.db.get_config_str("hl_exec_kill_reason")
                                            or "")})
        self._round_autocleared = 0

        # 4) 死手开关（HL 原生 scheduleCancel）：**默认关闭**，见 ExecParams.use_schedule_cancel。
        #    关闭时这一步完全不碰交易所；即使在开启状态下调用失败/抛异常，也绝不外泄 ——
        #    后面的心跳、看守判活等主流程一步都不能被它拖累。
        #
        #    【高3 附加路径】kill 态**也要续期**（旧代码在这里写了 `not self.db.hl_kill()`）。
        #    先想清楚这个接口的语义：`scheduleCancel(minutes)` = 「N 分钟内没有新的
        #    scheduleCancel 调用，就撤掉该地址**全部**挂单」——包括 reduce-only 止损单。
        #    对**有持仓**的账户，「撤全部挂单」不是保护动作，而是**撤掉止损 = 裸奔**。
        #    所以一旦 kill 置位就不再续期，后果是：定时器照常到期 → 止损被撤 → 裸仓，
        #    而 dd/manual 的 kill 永不自解，人不在时这个窗口可以持续几天。
        #    因此这里**不再区分 kill**：只要 live 且开关开着就续期（续期本身不动任何
        #    挂单，只是把「撤单」这个动作再往后推一个窗口）。死手开关的本意是「执行器
        #    停摆才撤单」，而 kill 是「停止开新仓」——两件事，不该让后者触发前者。
        #    ⚠️ 它的副作用（执行器真停摆时仍会连止损一起撤）没有消失：这个功能默认关闭，
        #    开启前必须读 docs/HL_RUNBOOK.md 的「真机验证发现 #3」。
        if self.p.live and getattr(self.p, "use_schedule_cancel", False):
            try:
                r = self.ex.schedule_cancel(minutes=self.p.deadman_minutes)
            except Exception as ex:                                   # noqa: BLE001
                r = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
            flagged = str(self.db.get_config_str("hl_deadman_err") or "") == "1"
            try:
                if r["ok"]:
                    if flagged:
                        self.db.set_config("hl_deadman_err", "0")
                        self._event(EV_ERROR, "死手开关恢复可用")
                elif not flagged:
                    self.db.set_config("hl_deadman_err", "1")
                    self._event(EV_ERROR,
                                f"死手开关设置失败（仅记一次，恢复时再报）：{r['error']}",
                                data={"error": str(r["error"])[:300]})
            except Exception as ex:                                   # noqa: BLE001
                logger.warning("[hl-exec] 死手开关记账失败（忽略）：%s", ex)

        # （出入金流水已提前到 1b 步：回撤判断要用本轮的流水，见 §4.4）
        # 5) 心跳（看守据此判断执行器存活）
        self._event(EV_HEARTBEAT,
                    f"executor 心跳（equity {snap.get('equity_total', snap.get('equity'))}）",
                    data={"kill": self.db.hl_kill(), "mode": self.p.mode})
        for a in alerts:
            if isinstance(a, _RoundNote):                            # 兜底：极端分支里没渲染
                a = a.render(bool(self.db.hl_kill()),
                             str(self.db.get_config_str("hl_exec_kill_reason") or "").strip())
            if isinstance(a, tuple):
                key, text = a
                if self._alert(text):
                    # 推送成功才写节流键（失败则下轮再推，见 M2）
                    try:
                        self.db.set_config(key, str(int(time.time())))
                    except Exception as ex:                           # noqa: BLE001
                        logger.warning("[hl-exec] 节流键写入失败（忽略）：%s", ex)
            else:
                self._alert(a)
        return [a[1] if isinstance(a, tuple)
                else (a.render(bool(self.db.hl_kill()),
                               str(self.db.get_config_str("hl_exec_kill_reason") or "").strip())
                      if isinstance(a, _RoundNote) else a)
                for a in alerts]

