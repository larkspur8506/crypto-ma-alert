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
import logging
import os
import math
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.hl import (MODE_DRY, MODE_LIVE, MODE_OFF, PURPOSE_ENTRY, PURPOSE_EXIT, PURPOSE_EXIT_REHANG,
                    PURPOSE_STOP,
                    ST_FAILED, ST_FILLED, ST_INTENT, ST_PLANNED, ST_RESTING, ST_SENT, ST_SKIPPED,
                    EV_DECISION, EV_ERROR, EV_HEARTBEAT, EV_KILL, EV_ORDER, EV_RECONCILE)
from app.hl import sizing as hl_sizing
from app.hl import pnl as hl_pnl
from app.hl.exchange import STATUS_DRY, STATUS_FILLED, STATUS_RESTING, make_cloid
from app.engine import DISPLAY_TZ, TZ_LABEL
from app.hl.info import HlInfoError
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

# pnl_source 取值（positions.pnl_source，面板/报告据此判断该不该采信 net_pnl）
PNL_SRC_FILLS = "fills"          # 有成交，net_pnl 来自 hl_fills 聚合
PNL_SRC_NO_FILLS = "no_fills"    # 窗内查不到成交（net_pnl 为 NULL，不猜 0）
PNL_SRC_NO_EXIT_FILLS = "no_exit_fills"   # 只有入场腿（平仓成交还没同步），不写半截净额


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
    code_rev: str = ""

    @property
    def follow(self) -> bool:
        return str(self.equity_mode or "follow").lower() != "fixed"

    @property
    def live(self) -> bool:
        return self.mode == MODE_LIVE


def _today_start_ts(now: Optional[float] = None) -> float:
    """当日 0 点（**北京时间**，与展示层一致；旧版 UTC 日界会让「今日下单数/
    今日已实现」比用户直觉早/晚 8 小时）。"""
    tz = dt.timezone(dt.timedelta(hours=int(os.environ.get("DISPLAY_TZ_OFFSET", "8"))))
    d = dt.datetime.fromtimestamp(now if now is not None else dt.datetime.now(
        dt.timezone.utc).timestamp(), tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


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

    def __init__(self, db, params: ExecParams, info, exchange, notifier=None):
        self.db = db
        self.p = params
        self.info = info
        self.ex = exchange
        self.notifier = notifier
        # 最近一次成功同步成交的时刻（实例级）。关账路径里「同步 → 算净额」会与
        # _on_exit 自己的那次同步撞车，10s 内不重复打同一个接口（同一实例内判断，
        # 跨实例本来就会重新同步，语义不变）。
        self._last_sync_at = 0.0

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
            return {"ok": False, "problems": ["读不到账户状态（info 失败）"], "unknown": True}
        ex_pos = {p["coin"]: p for p in snap.get("positions") or []}
        # 挂单列表可信才允许判断「止损有无」：回落老接口时读不到 isTrigger，会把有止损读成没止损，
        # 据此自动补挂/平仓 = 误平仓。缺字段按**不可信**处理 —— 与面板（views）和看护
        # （watchdog）同口径（fail-closed）：唯一生产来源 info.snapshot 总会显式写这个键，
        # 缺键只可能出现在桩/半成品数据里，那种情况宁可只告警、不自动动仓。
        trusted = bool(snap.get("orders_trusted", False))
        problems: List[str] = []
        missing_stop: List[dict] = []
        unconfirmed: List[str] = []
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
                      and (r["hl_status"] or "") in ("open", "mismatch", "")]
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
                problems.append(f"{r['symbol']} 本地有仓（#{r['id']}）但交易所无仓位")
                self.db.set_pos_hl(r["id"], hl_status="mismatch")
                continue
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
                self.db.hl_set_kill(True, "持仓数量对账连续不一致")
                self._event(EV_KILL, "持仓数量对账连续不一致 → 自动 kill switch",
                            data={"problems": size_problems})
        # 清掉已经不在 open 列表里的 strike 键：仓位关掉后没人会再走「数量一致」分支，
        # 留着就是内存泄漏（长期运行的容器会慢慢攒，每标的一行）。
        self._strike_prune({int(r["id"]) for r in local_open})
        return {"ok": not problems, "problems": problems, "unknown": False,
                "missing_stop": missing_stop, "unconfirmed": unconfirmed,
                "size_warn": size_warn, "size_kill": bool(size_problems)}

    # ------------------------------------------------------------ 数量对账（PR-3）
    def _size_tick(self, coin: str) -> Optional[float]:
        """该合约 1 tick 的数量（10^-szDecimals）。读不到规格返回 None → 由调用方跳过。

        不猜精度：拿一个默认 szDecimals 去当容差，等于用一个可能差 100 倍的尺子判
        「数量一致/不一致」，两种结论都不可信。
        """
        try:
            specs = self.info.specs([coin]).get(coin) or {}
            dec = specs.get("sz_decimals")
            if dec is None:
                return None
            return 10.0 ** -int(dec)
        except (HlInfoError, TypeError, ValueError) as e:              # noqa: BLE001
            logger.warning("[hl-exec] 数量对账读不到 %s 规格，本轮跳过：%s", coin, e)
            return None

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
            self._event(EV_DECISION, f"kill 已触发：{symbol} 暂不自动补挂止损", symbol=symbol)
            return
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
        try:
            res = self.ex.place_order(coin, side != 1, sz, px, reduce_only=True,
                                      trigger_px=px,
                                      cloid=self._attempt_cloid(symbol, bar_ms, PURPOSE_STOP + ":fix"))
        except Exception as ex:                                     # noqa: BLE001
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
        if self.db.hl_kill():
            bad.append("kill switch 已触发")
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
        plan = hl_sizing.size_order_hl(coin, sz_decimals, mid, stop, eff_equity,
                                      self.p.risk_pct, self.p.leverage, side=side,
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
            return self._entry_failed(symbol, bar_ms, "IOC 未成交（限价不够激进），已撤单放弃")
        filled_sz = float(res["filled_sz"] or plan.sz)
        avg_px = float(res["avg_px"] or mid)
        self.db.hl_update_order(symbol, bar_ms, PURPOSE_ENTRY, status=ST_FILLED,
                                oid=res.get("oid"), filled_sz=filled_sz, avg_px=avg_px,
                                raw=str(res.get("raw"))[:2000])

        # 3) 止损单（硬要求）
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
            return self._on_exit_locked(position_id, reason, ref_price)

    def _on_exit_locked(self, position_id: int, reason: str, ref_price: float) -> bool:
        row = self.db.position_by_id(position_id)
        if row is None or self.p.mode == MODE_OFF:
            return False
        if (row["hl_status"] or "") not in ("open",):
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
            self._close_local(position_id, px or ref_price, "stop", sz)
            r = self._r_multiple(row, px or ref_price)
            self._alert(f"【HL执行】🛑 止损离场 · {coin} {'做多' if side == 1 else '做空'}\n"
                        f"止损单已由交易所触发 @ {px or ref_price:,.2f}，本地已关账（R {r:+.2f}）")
            return True
        if reason == "stop" and snap is None:
            # 快照读不到 ≠ 仓位没了。不能按参考价关账 —— 万一仓位还在（且止损单也丢了），
            # 本地关账后就没人管它了。只提醒，留给下一轮/人工。
            self._event(EV_ERROR, f"{symbol} 止损事件但快照不可读，未关账（下一轮再核对）",
                        symbol=symbol)
            self._alert(f"【HL执行】⚠️ {coin} 判定止损离场，但账户快照读取失败，"
                        f"本地暂未关账 —— 请人工核对交易所。")
            return True

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
                        self.db.hl_update_order(symbol, int(r["bar_ms"]), purpose,
                                                status="cancelled")
                        cancelled_purposes.append(purpose)
                continue
            if so is not None and so["oid"] and so["status"] in (ST_RESTING, ST_SENT, ST_FILLED):
                self.ex.cancel(coin, int(so["oid"]))
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
            self.db.hl_update_order(symbol, exit_bar_ms, PURPOSE_EXIT, status=ST_FAILED,
                                    error="平仓 IOC 未成交（已撤单并挂回止损）")
            # 重挂止损用独立 purpose（exit:rehang）：与开仓止损（stop@入场bar）、
            # 事后补挂（stop:fix）都不同源 —— HL 的 cloid 账户级永久唯一，
            # 复用任何一个都会被拒单，重挂失败=仓位裸奔。
            re_stop_px = hl_sizing.round_price(float(row["actual_stop"] or row["stop_price"]), sz_dec)
            rehang_cloid = self._attempt_cloid(symbol, exit_bar_ms, PURPOSE_EXIT_REHANG)
            re_stop = self.ex.place_order(
                coin, side != 1, sz, re_stop_px,
                reduce_only=True, trigger_px=re_stop_px,
                cloid=rehang_cloid)
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
            try:
                re_stop = self.ex.place_order(
                    coin, side != 1, remain, re_px, reduce_only=True, trigger_px=re_px,
                    cloid=rehang_cloid)
            except Exception as ex:                                   # noqa: BLE001
                re_stop = {"ok": False, "error": f"{type(ex).__name__}: {ex}", "status": "error"}
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
                ts=int(f["time"]) // 1000 if f.get("time") else None)
            added += 1
            if fee_quote is None:
                self._note_unquoted_fee(tid, fee_token, coin, fee)
        if added:
            self._event(EV_ORDER, f"同步 {added} 笔交易所成交到本地", data={"added": added})
        return added

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
        try:
            self._record_fill_pnl(position_id)
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 关账后计算流水口径净利失败（忽略）：%s", e)

    def _last_fill_px(self, coin: str) -> Tuple[Optional[float], List[dict]]:
        """从交易所成交里取最近一笔该 coin 的**平仓**成交价（用于止损已触发的关账）。

        必须过滤 dir 含 "Close" —— 否则同标的重开后会取到开仓价当平仓价，R 值全错。
        """
        try:
            fills = self.info.user_fills(self.p.account_address) or []
        except HlInfoError:
            return None, []
        mine = [f for f in fills if f.get("coin") == coin]
        if not mine:
            return None, []
        closing = [f for f in mine if "Close" in str(f.get("dir") or "")]
        source = closing or mine          # 没有平仓成交时退回全量（供调用方诊断）
        px = None
        for f in source[:8]:
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

    # ------------------------------------------------------------ 每轮
    def run(self, force_snapshot: bool = False) -> List[str]:
        """每轮 tick 调用：快照 → 对账 → 熔断 → 死手开关 → 心跳。返回告警文案。"""
        with self._op_lock:
            return self._run_locked(force_snapshot)

    def _run_locked(self, force_snapshot: bool = False) -> List[str]:
        alerts: List[str] = []
        if self.p.mode == MODE_OFF:
            return alerts
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

        # 1) 对账
        rec = self.reconcile(snap, kill_before=kill_before)
        if not rec["ok"]:
            problems = rec["problems"]
            self._event(EV_RECONCILE, "对账异常：" + "；".join(problems), data=rec)
            if not rec.get("unknown"):
                self.db.hl_set_kill(True, "对账异常")
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
                if not rec.get("unconfirmed"):
                    alerts.append("【HL执行】✅ 对账已恢复正常")
            # 数量对账处于「观察期」（首次超差、已回捞、未到两击）→ 不写「对账一致」：
            # 明明有未解释的数量差，日志说一致会误导事后复盘。
            if rec.get("size_warn"):
                self.db.hl_log_event(EV_RECONCILE,
                                     "数量对账观察中：" + "；".join(rec["size_warn"]),
                                     code_rev=self.p.code_rev)
            else:
                self.db.hl_log_event(EV_RECONCILE, "对账一致", code_rev=self.p.code_rev)

        # 2) 当日亏损熔断（基线 = 当日第一张快照；当前值用刚读到的实时权益）
        day0 = _today_start_ts()
        base = self.db.hl_snapshots(since_ts=day0, limit=1)
        spot_ok = bool(snap.get("spot_ok", True)) if snap else False
        if base and not self.db.hl_kill():
            # 统一账户下 snap["equity"]（永续口径）恒为 0 → 必须用账户总值，否则熔断永不触发
            start_eq = float(base[0]["equity"] or 0.0)
            now_eq = float(snap.get("equity_total", snap.get("equity")) or 0.0)
            # 现货读取失败时 equity_total 只剩浮盈 —— 数据不可信，宁可跳过也不误熔断
            if not spot_ok:
                logger.warning("[hl-exec] 现货余额读取失败，本轮跳过亏损熔断判断（数据不可信）")
            elif start_eq > 0 and (now_eq - start_eq) / start_eq <= -self.p.max_daily_loss_pct:
                self.db.hl_set_kill(True, "当日亏损熔断")
                self._event(EV_KILL, f"当日亏损 {((now_eq - start_eq) / start_eq) * 100:.1f}% → kill",
                            data={"start_equity": start_eq, "equity": now_eq})
                alerts.append(f"【HL执行】⚠️ 当日亏损 {(now_eq - start_eq) / start_eq * 100:.1f}%"
                              f"（阈值 -{self.p.max_daily_loss_pct * 100:g}%）→ 已停止自动交易")

        # 3) 死手开关（HL 原生 scheduleCancel）：**默认关闭**，见 ExecParams.use_schedule_cancel。
        #    关闭时这一步完全不碰交易所；即使在开启状态下调用失败/抛异常，也绝不外泄 ——
        #    后面的成交同步、心跳、看守判活等主流程一步都不能被它拖累。
        if self.p.live and not self.db.hl_kill() and getattr(self.p, "use_schedule_cancel", False):
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

        # 4) 同步成交到本地（面板的「最近成交/手续费累计/今日与累计已实现」全靠 hl_fills；
        #    在此之前**没有任何生产代码写过这张表** → 真交易后面板这三项会一直是 0）
        try:
            self._sync_fills()
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[hl-exec] 同步成交失败（不影响交易）：%s", e)

        # 5) 心跳（看守据此判断执行器存活）
        self._event(EV_HEARTBEAT,
                    f"executor 心跳（equity {snap.get('equity_total', snap.get('equity'))}）",
                    data={"kill": self.db.hl_kill(), "mode": self.p.mode})
        for a in alerts:
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
        return [a[1] if isinstance(a, tuple) else a for a in alerts]
