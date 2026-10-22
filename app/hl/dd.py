"""回撤熔断（DD Guard）的口径：TWR 指数链 + 峰谷回撤 + 档位阈值。

**为什么单独一个模块**：同一个数字要出现在两个地方 —— 执行器（据此决定停手）和面板
（给人看）。两边各抄一份算法迟早会漂移（历史教训：止损距离、宽限秒数都各抄过一份，
改了一边忘另一边）。这里只有纯函数：不碰网络、不碰交易所，`db` 只当作「有个
get_config_str / set_config 的对象」用 —— 执行器推进状态，面板**只读**状态，
谁都不重算对方的账。

口径（规格 §3）：

    r_k     = (equity_k − flow_k) / equity_{k−1} − 1     # flow_k 落在 (t_{k−1}, t_k]
    index_k = index_{k−1} · (1 + r_k)                    # 起点 100
    DD(t)   = (peak_index − index(t)) / peak_index

**为什么必须剔除出入金**：不剔的话「提现」看起来就是亏钱（可能误熔断），「入金」会把
亏损稀释（该报的不报）。本账户 2026-10-21 转入 901.80U，账户权益曲线当天回撤瞬间归零，
而 TWR 口径一点没动 —— 这就是判据必须用 TWR、不能用账户权益的理由。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 持久化键
# 都存 DB `configuration` 表（键值对 JSON），理由见规格 §4.1：
# hl_snapshots 只留 30 天，TWR 锚点会被清掉；若每轮从快照重算，回撤会「忘记」
# 历史峰值、从 100 重新起算 → 回撤被系统性低估。
STATE_KEY = "hl_dd_state"          # {"anchor_ts", anchor_eq, anchor_cumflow, index, peak_index, last_ts, last_eq, twr_skipped, updated_at}
ALERT_KEY = "hl_dd_alert_at"       # L2 告警节流（推送成功才写）
KILL_KEY = "hl_dd_kill_at"         # L3「仍在熔断中」重复提醒节流（推送成功才写）
UNAVAIL_KEY = "hl_dd_unavail_at"   # 「口径暂不可用」节流（规格要求每 24h 一次）

# 节流窗口：L2/L3 都是 6h（同一状态别每轮 tick 都喊），口径不可用 24h。
ALERT_THROTTLE_S = 6 * 3600.0
KILL_REPEAT_S = 6 * 3600.0
UNAVAIL_THROTTLE_S = 24 * 3600.0

BASE_INDEX = 100.0                 # TWR 起点（与面板收益指数同一约定）
MAX_KILL = 0.95                    # 熔断线上限（§6：kill <= 0.95，留 5% 不至于「必触发」）

# ---------------------------------------------------------------- 档位
# 规格 §3.4：阈值**按当前 risk_pct 自动选**，不手工配。
# 为什么必须联动：2% 档 P(DD>30%)=60%、3% 档 96% —— 升了档却留着上一档的线，
# 等于给一个必然出现 30%+ 回撤的策略装一条 30% 的熔断线（长期误触发）。
# (risk_pct 上限, 告警线, 熔断线)；最后一档用 inf 兜住所有更大的 risk_pct。
TIERS = (
    (0.015, 0.25, 0.30),
    (0.025, 0.30, 0.40),
    (float("inf"), 0.40, 0.50),
)


def _num(v: Any, default: Optional[float] = 0.0) -> Optional[float]:
    """安全转 float：None/垃圾值/NaN/inf → default（NaN 会让所有比较都 False，
    等于静默关掉熔断 —— 必须挡在入口）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _get(o: Any, key: str, default: Any = None) -> Any:
    """dict / sqlite3.Row 统一取值。"""
    if isinstance(o, dict):
        return o.get(key, default)
    try:
        return o[key]
    except (IndexError, KeyError, TypeError):
        return default


def tier(risk_pct: Any) -> tuple:
    """按 risk_pct 选档 → (告警线, 熔断线)。

    读不出来（None/脏值）时按**最严档**（25/30）：宁可早一步喊人，也不要因为一个
    读坏的配置把线放到 40% 去（漏报比早报贵得多）。
    """
    r = _num(risk_pct, None)
    if r is None:
        r = TIERS[0][0]
    for cap, warn, kill in TIERS:
        if r <= cap:
            return warn, kill
    return TIERS[-1][1], TIERS[-1][2]                      # pragma: no cover


def thresholds(risk_pct: Any = 0.02, warn: Any = 0.0, kill: Any = 0.0) -> Dict[str, Any]:
    """解析生效阈值：档位默认 + 可选覆盖（0/空 = 用档位值）。

    校验（规格 §6）：必须 `warn < kill` 且 `kill <= 0.95`。
    不成立时**两者一起**退回档位默认 —— 只退回一个会造出「告警线 40% / 熔断线 30%」
    这种自相矛盾的组合（告警永远晚于熔断），读起来像个 bug。
    `rejected=True` 让调用方（面板/事件）能说明「你填的值没生效」，而不是静默吞掉。
    """
    tw, tk = tier(risk_pct)
    w = _num(warn, 0.0) or tw                             # 0 = 自动档位（§6）
    k = _num(kill, 0.0) or tk
    rejected = False
    if not (float(w) < float(k)) or float(k) > MAX_KILL:
        w, k, rejected = tw, tk, True
    return {"warn": float(w), "kill": float(k), "tier_warn": float(tw), "tier_kill": float(tk),
            "auto": not (_num(warn, 0.0) or _num(kill, 0.0)), "rejected": rejected}


# ---------------------------------------------------------------- 流水
def net_flow(flows: Iterable[Any], lo: Optional[float] = None,
             hi: Optional[float] = None) -> float:
    """Σ 落在 **(lo, hi]** 的带符号流水（正 = 净流入）。

    左端**严格开区间**：落在锚点/上一步时刻的那笔流水已经体现在那一格的权益里，
    再加一遍就是重复计入（入金会被算成收益，等于凭空抹掉一段亏损）。
    """
    lo_f = _num(lo, None)
    hi_f = _num(hi, None)
    total = 0.0
    for f in flows or []:
        ts = _num(_get(f, "ts"), None)
        if ts is None:
            continue
        if lo_f is not None and ts <= lo_f:
            continue
        if hi_f is not None and ts > hi_f:
            continue
        total += _num(_get(f, "amount"), 0.0) or 0.0
    return total


def cum_flow(flows: Iterable[Any], upto: Optional[float] = None) -> float:
    """Σ flows(≤ upto) —— 锚点当时的累计净入金 / 告警文案里的「净入金」。"""
    return net_flow(flows, None, upto)


# ---------------------------------------------------------------- 状态持久化
_STATE_FLOAT_KEYS = ("anchor_ts", "anchor_eq", "anchor_cumflow", "index",
                     "peak_index", "last_ts", "last_eq")


def parse_state(raw: Any) -> Optional[Dict[str, Any]]:
    """把 DB 里的 `hl_dd_state` 文本解析成状态 dict；不成立返回 None。

    返回 None 的后果是「重新 bootstrap 一次」（从快照重放），比拿半截状态续算安全：
    缺 index/peak 的状态会让回撤凭空归零，那正是要防的低估。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        # 兼容被 set_config 二次编码的形态（'"{\\"index\\": 100}"'）
        try:
            obj = json.loads(json.loads(text))
        except (TypeError, ValueError):
            return None
    if not isinstance(obj, dict):
        return None
    st: Dict[str, Any] = {}
    for k in _STATE_FLOAT_KEYS:
        v = _num(obj.get(k), None)
        if v is None:
            return None
        st[k] = float(v)
    st["twr_skipped"] = int(_num(obj.get("twr_skipped"), 0.0) or 0)
    st["updated_at"] = _num(obj.get("updated_at"), 0.0) or 0.0
    # 荒谬状态直接判废：锚点/指数必须为正，last_ts 必须是真实时间戳。
    if st["anchor_ts"] <= 0 or st["anchor_eq"] <= 0:
        return None
    if st["index"] <= 0 or st["peak_index"] <= 0 or st["last_ts"] <= 0:
        return None
    return st


def load_state(db: Any) -> Optional[Dict[str, Any]]:
    """读 `hl_dd_state`（读不到/读坏了 → None，由调用方决定是否 bootstrap）。"""
    try:
        raw = db.get_config_str(STATE_KEY, "")
    except Exception as e:                                     # noqa: BLE001
        logger.warning("[hl-dd] 读 hl_dd_state 失败（按无状态处理）：%s", e)
        return None
    return parse_state(raw)


def save_state(db: Any, st: Dict[str, Any]) -> None:
    """写回状态（读-改-写，每轮覆盖）。

    存的是 **JSON 文本**：`set_config` 内部还会 json.dumps 一次（存成带引号的字符串），
    读侧 `get_config_str` 正好把它还原成这段文本 —— 与 hl_flows_synced_at 同一约定。
    """
    payload = {k: st.get(k) for k in _STATE_FLOAT_KEYS}
    payload["twr_skipped"] = int(st.get("twr_skipped") or 0)
    payload["updated_at"] = _num(st.get("updated_at"), 0.0) or 0.0
    db.set_config(STATE_KEY, json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------- 计算
def bootstrap(snaps: Iterable[Any], flows: Iterable[Any], *,
              anchor_ts: float, anchor_eq: float) -> Dict[str, Any]:
    """从锚点起**逐张快照**链式重放 TWR，得到 index / peak_index（规格 §4.3.1）。

    为什么不能「锚点 → 当前」一步算：首次上线时锚点距今可能已经跨过一次入金，
    一步算等于把那笔入金当成了亏损（实测 88.75 vs 97.68，DD 差 7.8 个百分点，
    足以让没亏到线的账户凭空触发 25% 告警）。逐点重放把每一步的流水都单独扣掉，
    入金前后指数自然连续。

    `snaps` 传**全表**（不是 30 天窗口）：30 天前的快照正是 bootstrap 要用的历史。
    同一秒多张快照：只把最后一根的权益当分母，不额外算一步（否则收益率会被算两遍）。
    """
    pts: List[tuple] = []
    a_ts = _num(anchor_ts, None)
    a_eq = _num(anchor_eq, None)
    if a_ts is None or a_eq is None or a_ts <= 0 or a_eq <= 0:
        return {"index": BASE_INDEX, "peak_index": BASE_INDEX, "last_ts": 0.0,
                "last_eq": 0.0, "twr_skipped": 0, "points": 0}
    for s in snaps or []:
        t = _num(_get(s, "ts"), None)
        eq = _num(_get(s, "equity"), None)
        if t is None or eq is None or t < a_ts:
            continue
        pts.append((float(t), float(eq)))
    pts.sort(key=lambda x: x[0])

    index = BASE_INDEX
    peak = BASE_INDEX
    skipped = 0
    prev_ts, prev_eq = float(a_ts), float(a_eq)
    steps = 0
    for t, eq in pts:
        if t < prev_ts:
            continue                                  # 乱序保护（正常已按 ts 升序）
        if t == prev_ts:
            prev_eq = eq                              # 同一秒多张：最后一根当分母
            continue
        if prev_eq > 0:
            flow_k = net_flow(flows, prev_ts, t)       # 落在 (t_prev, t] 的净流水
            index = index * ((eq - flow_k) / prev_eq)
        else:
            skipped += 1                              # 上一格没有本金：不猜，指数不动
        peak = max(peak, index)
        prev_ts, prev_eq = t, eq
        steps += 1
    return {"index": index, "peak_index": peak, "last_ts": prev_ts,
            "last_eq": prev_eq, "twr_skipped": skipped, "points": steps}


def step(st: Dict[str, Any], *, ts_now: float, equity_now: float,
         flow_k: float = 0.0) -> Optional[Dict[str, Any]]:
    """推进一格（规格 §4.3 第 5 步）。返回**新** dict（不改入参）。

    - `ts_now <= last_ts`（时钟回拨 / 同一秒重复）→ 原样返回：再推一次会把这段
      收益率算两遍（手动 tick、重启后追赶 tick 都会撞上）。
    - `last_eq <= 0`（上一格没有本金）→ r 无定义，不猜：指数不动，twr_skipped +1。
    - 算出的指数为负 → 返回 None（权益/流水自相矛盾，宁可这轮不判也不砍错）。
      注意 index == 0 是**合法**的（真的亏光了），那种情况 DD 会是 100%，该熔断。
    """
    out = dict(st or {})
    t = _num(ts_now, None)
    eq = _num(equity_now, None)
    if t is None or eq is None:
        return None
    if t <= float(st.get("last_ts") or 0.0):
        return out
    prev_eq = float(st.get("last_eq") or 0.0)
    index = float(st.get("index") or 0.0)
    if prev_eq > 0:
        index = index * ((eq - (_num(flow_k, 0.0) or 0.0)) / prev_eq)
    else:
        out["twr_skipped"] = int(out.get("twr_skipped") or 0) + 1
    if index < 0:
        return None
    out["index"] = index
    out["peak_index"] = max(float(st.get("peak_index") or 0.0), index)
    out["last_ts"] = t
    out["last_eq"] = eq
    return out


def drawdown(index: Any, peak_index: Any) -> Optional[float]:
    """DD = (peak − index) / peak（正数 = 回撤幅度）。算不出 → None（不猜）。"""
    i = _num(index, None)
    p = _num(peak_index, None)
    if i is None or p is None or p <= 0:
        return None
    return max(0.0, (p - i) / p)


def make_state(*, anchor_ts: float, anchor_eq: float, anchor_cumflow: float,
               replay: Dict[str, Any], now: float) -> Dict[str, Any]:
    """把锚点 + 重放结果拼成待持久化的状态。"""
    return {
        "anchor_ts": float(anchor_ts), "anchor_eq": float(anchor_eq),
        "anchor_cumflow": float(anchor_cumflow),
        "index": float(replay.get("index") or BASE_INDEX),
        "peak_index": float(replay.get("peak_index") or BASE_INDEX),
        "last_ts": float(replay.get("last_ts") or anchor_ts),
        "last_eq": float(replay.get("last_eq") or anchor_eq),
        "twr_skipped": int(replay.get("twr_skipped") or 0),
        "updated_at": float(now),
    }
