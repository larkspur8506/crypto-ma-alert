"""资金费流水（hl_funding）：拉取、归集、总账余额 —— 纯观测/记账，零交易路径。

为什么单独成模块
------------------------------------------------------------------
总资金流水表（feat/funds-ledger）的第三源就是资金费：出入金在 hl_flows、
成交在 hl_fills（都是既有功能的事实来源，本轮一行写入逻辑都不动），资金费
此前没有任何落库，只在面板上有一个「持仓期内合计」的只读读数。本模块只做
三件事：① 把 HL `userFunding` 事件归一成带符号的流水行；② 幂等落库 + 仓位
归集；③ 给渲染层算 running balance 的纯函数。**不触网**（采集走既有的
HlInfo.post，挂点在执行器 `_sync_flows` 的 30 分钟闸门内，不新增调度 job）。

userFunding 实测响应形状（2026-10-24 主网，POST /info）
------------------------------------------------------------------
请求：``{"type": "userFunding", "user": addr, "startTime": ms, "endTime": ms}``
响应：list，**按 time 升序**，startTime / endTime 都是**包含**语义（实测
startTime 命中的那条会再返回一次）；单页上限未触发（112 条 < 500）。分页
拿满 500 用 ``last.time`` **含边界**续拉（cursor = last_t，同毫秒重拉由
ref 幂等吸收）—— 不能用 last.time+1：那会把同毫秒跨币簇的其余成员永久
跳过（H1 修复，见 fetch_funding docstring）。每条：

    {"time": 1791936000000,                       # 毫秒
     "hash": "0x0000...0000",                     # **实测恒为全零**，无区分度
     "delta": {"type": "funding", "coin": "BTC",
               "usdc": "0.006007",                # 字符串十进制，**正=收入**
               "szi": "-0.00084",                 # 事件时点的仓位数
               "fundingRate": "0.0000125",
               "nSamples": 7}}

两个拍板（都写进了代码）：
- **符号**：usdc 原值入 `amount_usd`，正=收入，任何一层都不取反（与面板
  「资金费（正=收入）」的展示口径一致；交易所 cumFunding.sinceOpen 的
  成本视角取反发生在 views.status_payload，与这里无关）。
- **幂等键 ref**：hash 恒为全零 → 没法当 UNIQUE 键，退回
  ``f"{time_ms}|{coin}|{usdc}"`` 组合键。资金费每 coin 每小时最多一条，
  实测 112 条无 (coin, ms) 重复，组合键足够唯一。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional

# 归集窗口与 pnl 同源（±300s，别各写一个数）；见下方 ATTR_* 注释。
from app.hl.pnl import ENTRY_LOOKBACK_S as ATTR_LOOKBACK_S
from app.hl.pnl import EXIT_LOOKAHEAD_S as ATTR_LOOKAHEAD_S

logger = logging.getLogger(__name__)

# userFunding 单页上限（与 userNonFundingLedgerUpdates 同为 500；拿满才续拉）
FUNDING_PAGE = 500
FUNDING_MAX_PAGES = 20
# 零 hash：userFunding 事件实测全部是这个值，等于「没有 hash」
ZERO_HASH = "0x" + "0" * 64
# 与引擎同款归集窗口（app/hl/pnl.py：ENTRY_LOOKBACK_S / EXIT_LOOKAHEAD_S）：
# opened_at 是「成交确认写库」的时刻，真实入场腿可能早至多 300s；closed_at
# 同理。资金费落在窗口内 → 归该仓。拍板：两边都用 ±300s 闭区间，与引擎
# 的窗口口径完全一致（spec 允许「写明选择即可」）。数值从 pnl.py import
# 同源（L③：三处 ±300s 抽同一来源，消除漂移面）。
#
# 增量水位重叠：最后一条时间往前多拉 60s，吸收交易所边界抖动（ref 幂等去重）
OVERLAP_S = 60.0


def _num(v: Any, default: Any = 0.0) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def coin_of_symbol(symbol: Any) -> str:
    """positions.symbol → HL coin（BTC_USDT → BTC）。

    与执行器写 hl_fills 时的默认对应关系（reverse 映射 miss → f"{coin}_USDT"）
    一致：取下划线前缀。注册表将来若有「前缀 ≠ HL 名」的标的，这里会归集
    不上 → position_id 留 NULL（渲染层如实显示「未归属」），不会错归。
    """
    s = str(symbol or "").strip()
    return s.split("_", 1)[0] if s else ""


# ---------------------------------------------------------------- 归一化
def parse_event(row: Any) -> Optional[Dict[str, Any]]:
    """一条 userFunding 事件 → ``{ts, coin, amount_usd, hash, raw}``；脏值返回 None。

    - `ts` 秒（交易所 `time` 是毫秒）；time <= 0 / 缺失 → 丢弃（没法排序也没法归集）。
    - `amount_usd` = delta.usdc **原值**（正=收入，不取反）；usdc 缺失/非数 → 丢弃
      （不知道金额的资金费记进去就是脏账）。
    - `raw` 存原始 JSON（对账存档）。
    """
    if not isinstance(row, dict):
        return None
    ts_ms = _num(row.get("time"), 0.0)
    if ts_ms <= 0:
        return None
    delta = row.get("delta")
    if not isinstance(delta, dict):
        return None
    # userFunding 只回资金费事件，但 type 缺失/非 funding 说明形状变了：宁丢勿脏
    if str(delta.get("type") or "") != "funding":
        return None
    coin = str(delta.get("coin") or "").strip()
    if not coin:
        return None
    usdc = delta.get("usdc")
    if usdc is None:
        return None
    try:
        amount = float(usdc)
    except (TypeError, ValueError):
        logger.warning("[hl-funding] usdc 字段脏值（丢弃该条）：%s", str(row)[:200])
        return None
    if amount != amount or amount in (float("inf"), float("-inf")):
        return None
    return {"ts": ts_ms / 1000.0, "coin": coin, "amount_usd": amount,
            "hash": row.get("hash"), "raw": json.dumps(row, ensure_ascii=False)}


def ref_of(ev: Dict[str, Any]) -> str:
    """幂等键：非全零 hash 优先，否则 t(ms)+coin+amount 组合键（见模块 docstring）。"""
    h = str(ev.get("hash") or "").strip()
    if h and h.lower() != ZERO_HASH:
        return f"hl:{h}"
    return f"{int(round(_num(ev.get('ts')) * 1000))}|{ev.get('coin')}|{_num(ev.get('amount_usd')):+.6f}"


# ---------------------------------------------------------------- 仓位归集
def match_position(positions: Iterable[Any], coin: str, ts: float,
                   lookback_s: float = ATTR_LOOKBACK_S,
                   lookahead_s: float = ATTR_LOOKAHEAD_S) -> Optional[int]:
    """资金费时刻落在哪条仓位的窗口内 → 返回该 positions.id；没有 → None。

    - 窗口：``[opened_at − lookback, closed_at + lookahead]``（与引擎
      pnl.window_bounds 同口径；closed_at 为 NULL = 还在持 → 只卡下界）。
    - opened_at 为 NULL（pending/ignored，从没真正开过）→ 不匹配。
    - 同 coin 多仓命中（快速重开）：取 **opened_at 最近**的一仓 —— 资金费按
      小时计，紧挨着重开时落在「后一仓开完之后」的可能性远大于前一仓。
    - coin 匹配用 positions.symbol 前缀（coin_of_symbol），对不上就 NULL，
      绝不猜（错归属比不归属更难排查）。
    """
    t = _num(ts, 0.0)
    best: Optional[int] = None
    best_opened = -1.0
    for p in positions or []:
        try:
            sym = p["symbol"]
            pid = p["id"]
            opened = p["opened_at"]
            closed = p["closed_at"]
        except (KeyError, IndexError, TypeError):
            continue
        if coin_of_symbol(sym) != str(coin or ""):
            continue
        o = _num(opened, 0.0)
        if opened is None or o <= 0:
            continue
        hi = _num(closed, None)
        if t < o - lookback_s:
            continue
        if hi is not None and t > hi + lookahead_s:
            continue
        if o >= best_opened:
            best, best_opened = int(pid), o
    return best


# ---------------------------------------------------------------- 落库
def record_funding(db: Any, events: Iterable[Any],
                   now: Optional[float] = None) -> Dict[str, Any]:
    """归一 + 归集 + 幂等落库。返回 ``{"added", "seen", "skipped"}``。

    归集用的仓位清单一次性读出来（recent_positions 全量，一生几十条）：
    每条事件查一次库是纯浪费。单条落库失败只跳过该条，不让一批脏数据把
    整轮同步掀翻。
    """
    try:
        positions = list(db.recent_positions(100000) or [])
    except Exception as e:                                       # noqa: BLE001
        logger.warning("[hl-funding] 读仓位清单失败（本批全部不归集）：%s", e)
        positions = []
    added = seen = skipped = 0
    for row in events or []:
        seen += 1
        ev = parse_event(row)
        if ev is None:
            skipped += 1
            continue
        ev["ref"] = ref_of(ev)
        ev["position_id"] = match_position(positions, ev["coin"], ev["ts"])
        try:
            if db.hl_add_funding(ev):
                added += 1
        except Exception as e:                                   # noqa: BLE001
            logger.warning("[hl-funding] 资金费落库失败（忽略该条）：%s", e)
            skipped += 1
    return {"added": added, "seen": seen, "skipped": skipped}


# ---------------------------------------------------------------- 拉取（分页）
def fetch_funding(info: Any, address: str, start_ms: int,
                  deadline_ts: Optional[float] = None,
                  max_pages: int = FUNDING_MAX_PAGES) -> List[dict]:
    """分页拉 userFunding，返回**原始事件**列表（归一在 record_funding）。

    分页语义（H1 修复后口径）：拿满 FUNDING_PAGE 用 ``last.time`` **含边界**续拉
    （cursor = last_t，不是 last_t+1）；同一毫秒被重复拉到的行由 ref UNIQUE +
    INSERT OR IGNORE 幂等吸收。旧口径 ``last_t + 1`` 会永久丢同毫秒跨币事件：
    HL 多币资金费在同一毫秒成簇（整点/小时边界），页边界落在簇内且簇后有更晚
    事件抬高水位时，被跳过的行本轮取不到、下轮水位已越过，永不补回且无任何标记。
    终止条件三重保险，不会死循环：① 页不满（< FUNDING_PAGE）= 拉完了；
    ② 游标没前进（last_t <= cursor，单毫秒簇 ≥ 500 条才会触发，实测不可能）→
    告警停页；③ `max_pages` / `deadline_ts` 预算兜底。
    """
    out: List[dict] = []
    cursor = max(0, int(start_ms))
    for _ in range(max(1, int(max_pages))):
        if deadline_ts is not None and time.time() >= float(deadline_ts):
            logger.warning("[hl-funding] 拉取超过时间预算（已取 %d 条），本轮放弃剩余页",
                           len(out))
            break
        data = info.user_funding_history(address, cursor)
        if not isinstance(data, list):
            raise RuntimeError(f"userFunding 返回了非预期结构：{str(data)[:120]}")
        out.extend([r for r in data if isinstance(r, dict)])
        if len(data) < FUNDING_PAGE:
            break
        last_t = _num((data[-1] or {}).get("time"), 0.0)
        if last_t <= 0 or int(last_t) <= cursor:          # 游标没前进 → 防死循环
            logger.warning("[hl-funding] 分页游标未前进（time=%s），停止分页", last_t)
            break
        cursor = int(last_t)          # 含边界续拉：同毫秒簇重拉一次，ref 幂等去重
    return out


def sync_funding(info: Any, db: Any, address: str,
                 deadline_ts: Optional[float] = None) -> Dict[str, Any]:
    """增量同步/首轮回填：水位 = 本地 max(ts)，None 时用回填起点（spec 拍板口径）。

    起点 = 首张正权益快照 与 最早 hl_flows.ts 的**早者**（db.hl_funding_origin_ts）。
    水位重叠 60s + ref UNIQUE 兜底幂等。返回 record_funding 的计数。
    """
    if not address:
        return {"added": 0, "seen": 0, "skipped": 0}
    last_ts = db.hl_funding_last_ts()
    if last_ts is None:
        origin = db.hl_funding_origin_ts()
        start_ms = int(max(0.0, origin) * 1000) if origin is not None else 0
    else:
        start_ms = int(max(0.0, last_ts - OVERLAP_S) * 1000)
    rows = fetch_funding(info, address, start_ms, deadline_ts=deadline_ts)
    res = record_funding(db, rows)
    if res["added"]:
        logger.info("[hl-funding] 新增 %d 条资金费（拉到 %d 条，起点 %s）",
                    res["added"], res["seen"],
                    "回填" if last_ts is None else f"{last_ts:.0f}")
    return res


# ---------------------------------------------------------------- 读侧：总账余额
def running_balances(rows_asc: Iterable[Any]) -> List[float]:
    """**全量正序**算 running balance（期初 0 + 逐行累加），返回与输入等长的余额列。

    这是 spec 点名最容易写错的地方：分页切片之后**绝不能**用当前页现算余额 ——
    页首之前的全部历史会漏掉。所以这里只吃「全量、正序」的 v_ledger 行，调用方
    先算这个再切片。排序由 db.hl_ledger_rows 保证（ts ASC, ref ASC）。
    """
    out: List[float] = []
    bal = 0.0
    for r in rows_asc or []:
        try:
            amt = float(r["amount_usd"])
        except (KeyError, IndexError, TypeError, ValueError):
            amt = 0.0
        bal += amt
        out.append(bal)
    return out


# 类型 → 白话（面板铁律：黑话改白话；send_in/send_out 之外的真实 kind 也要有人说人话）
KIND_TXT = {
    "deposit": "入金", "send_in": "入金", "sub_in": "子账户转入",
    "vault_in": "金库转回", "withdraw": "出金", "send_out": "出金",
    "sub_out": "转出至子账户", "vault_out": "转入金库", "other": "其它流水",
    "fill_open": "开仓成交", "fill_close": "平仓成交", "fill": "成交",
    "funding": "资金费",
}


def kind_text(kind: Any) -> str:
    k = str(kind or "")
    return KIND_TXT.get(k, k or "—")
