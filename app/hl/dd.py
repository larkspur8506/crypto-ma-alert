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
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 阈值规则（唯一真相）
# 【v2·低4】覆盖值的合法区间与「告警线 < 熔断线」这两条规则**只在这里实现**：
# config.apply_db / admin_ui._parse_dd_thresholds / main._dd_pct 全部调用本模块，
# 不再各抄一份（v1 抄了四遍：改一处忘另一处就会让面板放行、装配拒收，同一个人
# 填同一个数字得到两种结果）。
MIN_PCT = 0.05                     # 覆盖值下界（5%：再低就是碰一下就响的噪音线）
MAX_KILL = 0.95                    # 熔断线上限（§6：kill <= 0.95，留 5% 不至于「必触发」）
# 【v2·低2】连续多少轮算出负指数就认为「口径卡死」，改用锚点重放兜底。
# 3 轮 ≈ 15 分钟（tick 5 分钟一轮）：偶发一次多半是流水边界抖动，连续三轮说明
# state 与真实权益已经脱节，再拖下去这条线永远不会再判。
NEG_REPLAY_STREAK = 3

# ---------------------------------------------------------------- 持久化键
# 都存 DB `configuration` 表（键值对 JSON），理由见规格 §4.1：
# hl_snapshots 只留 30 天，TWR 锚点会被清掉；若每轮从快照重算，回撤会「忘记」
# 历史峰值、从 100 重新起算 → 回撤被系统性低估。
STATE_KEY = "hl_dd_state"          # {"anchor_ts", anchor_eq, anchor_cumflow, index, peak_index, last_ts, last_eq, twr_skipped, updated_at}
# ⚠️ `anchor_cumflow`（锚点建立时的累计净入金）**只写不读**：留痕用（规格 §4.2 要求把
# 锚点的上下文一起存下来，事后能复算「这个锚点是在多少累计入金下建的」），判据**不依赖
# 它** —— 指数链用的是每格的 flow_k（见模块 docstring 的 r_k 公式），从不读这个字段。
# 所以：不要因为它没有读方就删掉（历史 state 里还有它，删了 parse 也没坏处但会丢留痕）。
ALERT_KEY = "hl_dd_alert_at"       # L2 告警节流（推送成功才写）
KILL_KEY = "hl_dd_kill_at"         # L3「仍在熔断中」重复提醒节流（推送成功才写）
UNAVAIL_KEY = "hl_dd_unavail_at"   # 「口径暂不可用/等待流水同步」节流（规格要求每 24h 一次）
# 【低6·v3】事件侧的节流键：与 UNAVAIL_KEY **分开**。UNAVAIL_KEY 只在推送成功后才写
# （推送失败要下轮重推），事件若也挂它，推送一失败就每轮多一条 dd 事件（288 条/天）。
# 这个键在「尝试写事件」时就写，与推送成败无关。
UNAVAIL_EV_KEY = "hl_dd_unavail_ev_at"
STUCK_KEY = "hl_dd_stuck_at"       # 【v2·低2】「口径卡死，请人工核对」节流（24h 一次）
WAIT_KEY = "hl_dd_wait_note"       # 【v2·高1】上一次「本轮没判」的原因（面板展示用；空 = 正常）

# 节流窗口：L2/L3 都是 6h（同一状态别每轮 tick 都喊），口径不可用 24h。
ALERT_THROTTLE_S = 6 * 3600.0
KILL_REPEAT_S = 6 * 3600.0
UNAVAIL_THROTTLE_S = 24 * 3600.0

BASE_INDEX = 100.0                 # TWR 起点（与面板收益指数同一约定）

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


def norm_override(v: Any) -> float:
    """把「阈值覆盖值」归一成 `0.0`（= 按档位自动）或一个**合法**覆盖值。

    读侧专用（`main._dd_pct` 装配执行器参数时用）：脏值 / NaN / 负值 / 越界一律当
    「没设覆盖」，退回档位默认值。为什么越界不钳到边界：钳值会造出一个用户从没写过
    的阈值，比「忽略这个值、用档位默认」更难解释。
    """
    f = _num(v, None)
    if f is None or f <= 0.0:
        return 0.0
    return f if MIN_PCT <= f <= MAX_KILL else 0.0


def pct_ok(v: Any) -> bool:
    """覆盖值是否可接受：0（= 自动档位）或落在 [MIN_PCT, MAX_KILL]。

    写侧（面板表单 / config.apply_db）用这个判，不自己再写一遍区间：
    这是 `MIN_PCT`/`MAX_KILL` 的**唯一**判定实现（§6 低4）。
    """
    f = _num(v, None)
    if f is None:
        return False
    if f == 0.0:
        return True
    return MIN_PCT <= f <= MAX_KILL


def thresholds(risk_pct: Any = 0.02, warn: Any = 0.0, kill: Any = 0.0) -> Dict[str, Any]:
    """解析生效阈值：档位默认 + 可选覆盖（0/空 = 用档位值）。

    校验（规格 §6）：覆盖值必须落在 `[MIN_PCT, MAX_KILL]`，且 `warn < kill`。
    不成立时**两者一起**退回档位默认 —— 只退回一个会造出「告警线 40% / 熔断线 30%」
    这种自相矛盾的组合（告警永远晚于熔断），读起来像个 bug。
    `rejected=True` 让调用方（面板/事件）能说明「你填的值没生效」，而不是静默吞掉。
    """
    tw, tk = tier(risk_pct)
    raw_w = _num(warn, 0.0) or 0.0
    raw_k = _num(kill, 0.0) or 0.0
    w = norm_override(raw_w) or tw                         # 0 = 自动档位（§6）
    k = norm_override(raw_k) or tk
    # 显式给了越界值也算「拒收」（否则 0.97 会静默变成「自动」，用户以为生效了）
    out_of_range = (raw_w > 0 and not pct_ok(raw_w)) or (raw_k > 0 and not pct_ok(raw_k))
    rejected = bool(out_of_range) or not (float(w) < float(k))
    if rejected:
        w, k = tw, tk
    return {"warn": float(w), "kill": float(k), "tier_warn": float(tw), "tier_kill": float(tk),
            "auto": not (norm_override(raw_w) or norm_override(raw_k)), "rejected": rejected}



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
    st["neg_streak"] = int(_num(obj.get("neg_streak"), 0.0) or 0)
    st["updated_at"] = _num(obj.get("updated_at"), 0.0) or 0.0
    # 荒谬状态直接判废：锚点/峰值必须为正，last_ts 必须是真实时间戳。
    # **`index == 0` 是合法值**（权益真的亏光了 → DD=100% → 该熔断，§4.9 低1）：
    # 以前这里用 `<= 0` 判废，于是「亏光」被当成「状态坏了」→ 重新 bootstrap 回 100
    # → 面板显示 DD 0%，下一轮又 100%，状态机原地打转，永远不会熔断。
    if st["anchor_ts"] <= 0 or st["anchor_eq"] <= 0:
        return None
    if st["index"] < 0 or st["peak_index"] <= 0 or st["last_ts"] <= 0:
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
    payload["neg_streak"] = int(st.get("neg_streak") or 0)
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
    """推进一格（规格 §4.3 第 6 步）。返回**新** dict（不改入参）。

    - `ts_now <= last_ts`（时钟回拨 / 同一秒重复）→ 原样返回：再推一次会把这段
      收益率算两遍（手动 tick、重启后追赶 tick 都会撞上）。
    - `last_eq <= 0`（上一格没有本金）→ r 无定义，不猜：指数不动，twr_skipped +1。
    - 算出负指数 → **不返回 None**，而是返回带 `neg=True` 的状态（§4.9 低2）：
      仍然推进 `last_ts`（否则窗口越拖越宽、这条线永远不再判），`neg_streak` +1，
      index 保持不动（绝不把负数写进状态）。上层据此记 error、决定是否锚点重放。
      注意 `index == 0` 是**合法**的（真的亏光了），那种情况 DD 会是 100%，该熔断。
    - 输入缺 `index`（状态坏到没法续算）→ None：交给调用方 bootstrap，绝不拿 0 猜。
    """
    out = dict(st or {})
    t = _num(ts_now, None)
    eq = _num(equity_now, None)
    if t is None or eq is None:
        return None
    if t <= (_num(st.get("last_ts"), 0.0) or 0.0):
        return out
    prev_eq = _num(st.get("last_eq"), 0.0) or 0.0
    index = _num(st.get("index"), None)
    if index is None:
        return None
    if prev_eq > 0:
        index = index * ((eq - (_num(flow_k, 0.0) or 0.0)) / prev_eq)
    else:
        out["twr_skipped"] = int(out.get("twr_skipped") or 0) + 1
    # 时间窗**无条件**推进：哪怕是坏值那一轮也要过掉这一格，否则同一段永远算不完。
    out["last_ts"] = t
    out["last_eq"] = eq
    if index < 0:
        out["neg_streak"] = int(st.get("neg_streak") or 0) + 1
        out["neg"] = True
        return out
    out["index"] = index
    out["peak_index"] = max(_num(st.get("peak_index"), 0.0) or 0.0, index)
    out["neg_streak"] = 0
    out["neg"] = False
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
    """把锚点 + 重放结果拼成待持久化的状态。

    【v2·低1】所有数值兜底都改成显式 `is None`：以前写的是
    `replay.get("index") or BASE_INDEX`，于是**真实的 index=0（权益亏光）被换成 100**
    → 面板显示 DD 0%，下一轮又算出 0，状态机永远在 100 附近打转、就是不熔断。
    `index == 0` 是合法状态，只有「键缺失（None）」才该用兜底值。
    """
    idx = replay.get("index")
    idx = BASE_INDEX if idx is None else float(idx)
    peak = replay.get("peak_index")
    peak = idx if peak is None else float(peak)
    last_ts = replay.get("last_ts")
    last_ts = float(anchor_ts) if last_ts is None else float(last_ts)
    last_eq = replay.get("last_eq")
    last_eq = float(anchor_eq) if last_eq is None else float(last_eq)
    return {
        "anchor_ts": float(anchor_ts), "anchor_eq": float(anchor_eq),
        # 【低7】只写不读：留痕用，判据不依赖它（见 STATE_KEY 处的注释）。
        "anchor_cumflow": float(anchor_cumflow),
        "index": idx,
        "peak_index": max(peak, idx),
        "last_ts": last_ts,
        "last_eq": last_eq,
        "twr_skipped": int(replay.get("twr_skipped") or 0),
        "neg_streak": 0,
        "updated_at": float(now),
    }


def reset_peak(db: Any, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """把 `peak_index` 重置为**当前 index**（回撤从此刻重新计量）。

    【v2·高2】「人工解熔断」必须真能恢复：dd 熔断永不自解，但峰值不重置的话，
    用户解除 kill 之后下一轮 DD 仍然是 31% → 立刻又置 kill ⇒ 「人工解」形同虚设。
    熔断的语义是「从现在起重新评估风险」，不是「永远不许恢复」。

    返回新状态；没有状态 / 指数不可用 → None（旧状态不动，绝不编一个基线）。
    `index <= 0` 时**不重置**：权益已经亏光（DD=100%），把峰值抹成 0 只会让 DD
    归零、把真正的爆仓藏起来 —— 那种情况应该继续拦着等人核对。
    """
    st = load_state(db)
    if st is None:
        return None
    idx = float(st["index"])
    if idx <= 0:
        return None
    st["peak_index"] = idx
    st["updated_at"] = float(now) if now is not None else time.time()
    save_state(db, st)
    return st

