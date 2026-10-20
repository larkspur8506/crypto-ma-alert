"""`/why` 诊断：每个标的「现在为什么没开仓 / 开了什么仓」的只读装配层。

**红线（与 PR-5 验收一致）**：本模块只读。
- 不新增任何下单调用，不改下单/止损逻辑，不碰 `Executor._gate_check/_settle/_churn` 的写路径；
- 信号侧结论一律由 `strategy.explain_entry` → `strategy.evaluate_entry` 给出（本模块不重写
  任何判定），闸门侧一律由 `Executor.gate_state_for` → `_settle_remaining`/`_churn_count`
  读出（本模块不重算时间差）。

为什么单独一层而不是直接写在面板里：
1. 面板每次渲染都会调用它，绝不能有副作用；纯装配层好审、好测（不依赖 FastAPI 也能跑）；
2. 运维在容器里也能走同一条链路（`python -m app.why`），面板与命令行永远说同一句话；
3. 取数（HL 4h K 线）是这里唯一的外部 I/O，失败按标的降级成一行错误，不牵连其它标的。

为什么不读 symbol_state：那张表存的是现价/触发线/密集根数，够画「三标的动态」，但回答不了
「六步里被哪一步拦下」—— 那需要 ind[i-1]、squeeze[i-1]、SMA200、止损距离同时在场。所以
这里按 tick 同口径现取 K 线再算。

缓存分三层，各自解决不同的问题：
- `_FEED_CACHE`（K 线，TTL 120s）：面板连刷/多 Tab 不该每次都打行情接口。4h 级别的数据
  120s 内不会变（新 bar 也在下一个 tick 才被处理），过期即重新取。
- `_WHY_CACHE`（六步，键 = 标的 + 最后一根已收盘 bar + 阈值指纹）：250 根分位计算
  ~0.4s/标的，只在「新 bar 收盘」或「参数变了」时重算 —— 参数指纹少一项就会出现
  「面板改完阈值，/why 还按旧门槛解释」这种最难自查的不一致。
- 仓位与闸门**不进缓存**：一个是「现在有没有仓」（决定槽位占用），一个是「现在有没有闸」
  （15s / 10 分钟窗口），都必须每次现读。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app import market, posfields, strategy
from app import symbol_registry as registry

logger = logging.getLogger(__name__)

FEED_TTL_S = 300.0      # K 线缓存寿命。取 5 分钟的理由：信号侧只随「4h bar 收盘」变化，
                        # 300s 的行情陈旧不可能改变六步结论；而面板刷新是同步路径，
                        # 每次现拉 3 个标的 × 5000 根会把首屏拖慢好几秒。
                        # 闸门与仓位**不走这个缓存**（见 build_rows 末尾），它们必须实时。

_WHY_CACHE: Dict[Tuple[str, int, Tuple], Dict[str, Any]] = {}
_WHY_CACHE_MAX = 32
_FEED_CACHE: Dict[str, Tuple[float, List[strategy.Bar]]] = {}


def _cfg_sig(cfg: Any) -> Tuple:
    """阈值指纹：参数一变（面板热改 stop_min/方向开关/分位…）缓存立刻失效。"""
    def g(name: str, default: Any = None) -> Any:
        return getattr(cfg, name, default)
    return (g("stop_min"), g("stop_max"), bool(g("enable_long", True)),
            bool(g("enable_shorts", True)), int(g("short_regime_ma", 0) or 0),
            int(g("band_window", strategy.BAND_WINDOW)),
            float(g("band_quantile", strategy.BAND_QUANTILE)),
            int(g("min_squeeze_bars", strategy.MIN_SQUEEZE_BARS)),
            int(g("hl_churn_max", 5)))


def _coin_of(symbol: str) -> Optional[str]:
    """symbol → HL 合约名；未登记返回 None（**不抛**，诊断不该因配置错误崩掉）。"""
    try:
        return registry.hl_coin(symbol)
    except registry.SymbolNotConfigured:
        return None
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[why] coin 解析失败 %s：%s", symbol, e)
        return None


def gates_for(db: Any, cfg: Any, coin: str) -> Dict[str, Dict[str, Any]]:
    """多/空两侧的闸门状态（各一条）。任何异常 → 两个空状态，面板显示「—」而非炸掉。"""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from app.hl.executor import Executor
        churn_max = int(getattr(cfg, "hl_churn_max", 5))
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[why] 读闸门状态失败（面板显示 —）：%s", e)
        return {"long": {}, "short": {}}
    for key, side in (("long", 1), ("short", -1)):
        try:
            out[key] = Executor.gate_state_for(db, coin, side, churn_max=churn_max)
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[why] %s %s 闸门状态失败：%s", coin, key, e)
            out[key] = {}
    return out


def _position_part(db: Any, symbol: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """仓位侧（只读）：返回 (position dict | None, 备注文案)。

    有 open/pending 行时 walk 根本不评估入场（同一标的至多 1 仓）—— 这种情况必须讲清楚，
    否则运维会去改参数，而真正该做的是处理手上这一仓。
    """
    try:
        prow = db.open_position(symbol)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[why] %s 读仓位失败：%s", symbol, e)
        return None, ""
    if prow is None:
        return None, ""
    try:
        st = str(prow["status"] or "")
        pos = {
            "id": int(prow["id"]),
            "status": st,
            "side": int(prow["side"] or 1),
            "qty": float(posfields.qty(prow) or 0.0),
            "qty_unit": posfields.qty_unit(prow),
            "entry": posfields.entry_px(prow),
            "stop": posfields.stop_px(prow),
            "signal_bar_ms": int(prow["signal_bar_ms"] or 0),
        }
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[why] %s 仓位行解析失败：%s", symbol, e)
        return None, ""
    note = ""
    if st in ("open", "pending"):
        word = "持仓中" if st == "open" else "待确认（占着槽位）"
        note = (f"{word}：同一标的至多 1 仓，本轮不评估新入场；"
                f"六步只是「假如无仓」的行情读数")
    return pos, note


def why_row(db: Any, cfg: Any, symbol: str, bars: Sequence[strategy.Bar]) -> Dict[str, Any]:
    """单个标的的诊断行（纯计算 + 只读 DB 查询；行情由调用方给，便于测试与复用）。

    口径与 `engine._tick_symbol` 严格对齐：
    - i = 最后一根**已收盘** bar（`len(bars)-2`），入场参考价 = 最后一根未收盘 bar 的开盘价
      （`bars[i+1].open`）—— 这正是 walk 里 `entry_price=bars[i + 1].open` 的同一取值；
    - 阈值全部取 cfg（DB > env > 默认），不读 env、不另设默认。
    """
    ind = strategy.compute_indicators(bars)
    sq = strategy.squeeze_flags(ind["band"], cfg.band_window, cfg.band_quantile,
                                cfg.min_squeeze_bars)
    i = len(bars) - 2
    entry_px = float(bars[i + 1].open) if 0 <= i + 1 < len(bars) else None
    # 单根密集序列只算一次，同时喂给 explain_entry（展示「密集 n 根」）——
    # 不传的话 explain_entry 内部会为展示再算一遍（5000 根上 ~0.4s，白花的）。
    raw = strategy.squeeze_flags(ind["band"], cfg.band_window, cfg.band_quantile, 1)
    why = strategy.explain_entry(symbol, bars, ind, sq, i, entry_price=entry_px,
                                 stop_min=cfg.stop_min, stop_max=cfg.stop_max,
                                 allow_long=cfg.enable_long, allow_short=cfg.enable_shorts,
                                 short_regime_ma=cfg.short_regime_ma,
                                 band_window=cfg.band_window,
                                 band_quantile=cfg.band_quantile,
                                 min_squeeze_bars=cfg.min_squeeze_bars,
                                 squeeze_raw=raw)
    pos, note = _position_part(db, symbol)
    row: Dict[str, Any] = {
        "symbol": symbol,
        "coin": _coin_of(symbol) or symbol,
        "price": float(bars[-1].close) if bars else None,
        "bars": len(bars),
        "why": why,
        "position": pos,
        "note": note,
        "error": None,
        "gates": {},          # 由 build_rows 填（面板渲染前现读）
        "gate_side": 0,       # 关注侧：持仓方向 > 信号方向 > 0（无方向）
        "gate": None,
    }
    row["gate_side"] = _gate_side(row)
    return row


def _gate_side(row: Dict[str, Any]) -> int:
    """闸门要显示哪一侧：持仓（open）方向优先，其次信号方向，都没有则 0（面板给双侧）。"""
    pos = row.get("position") or {}
    if str(pos.get("status") or "") == "open":
        return int(pos.get("side") or 1)
    why = row.get("why") or {}
    if why.get("fired"):
        return int(why.get("side") or 0)
    return 0


def _fetch_bars(fetch, symbol: str, now_ms: Optional[int]) -> List[strategy.Bar]:
    """取 K 线（带 TTL 缓存）。缓存键只有 symbol：TTL 内同一标的的数据不该有第二种答案。"""
    hit = _FEED_CACHE.get(symbol)
    now = time.time()
    if hit is not None and now - hit[0] < FEED_TTL_S:
        return hit[1]
    feed = fetch(symbol, now_ms)
    bars = list(feed.bars)
    if len(bars) < 2:
        raise market.FeedError(f"{symbol}: 只有 {len(bars)} 根 K 线")
    _FEED_CACHE[symbol] = (now, bars)
    return bars


def _fetch_many(fetch, syms: Sequence[str], now_ms: Optional[int]) -> Dict[str, Any]:
    """并发取各标的的 K 线（**只有网络 I/O**，不碰 DB、不碰闸门状态）。

    为什么值得并发：冷缓存时每个标的要 2s 左右（5000 根 4h K 线），三个标的串行就是
    6s 的首次首屏 —— 面板是同步路由，用户会以为服务卡死了。并发后 ≈ 单标的耗时。
    失败按标的收敛成异常值，由调用方降级成一行错误（一个标的取数失败不牵连其它标的）。
    """
    if len(syms) <= 1:
        out: Dict[str, Any] = {}
        for s in syms:
            try:
                out[s] = _fetch_bars(fetch, s, now_ms)
            except Exception as e:                                # noqa: BLE001
                out[s] = e
        return out
    import concurrent.futures as cf
    out: Dict[str, Any] = {}
    with cf.ThreadPoolExecutor(max_workers=min(4, len(syms))) as pool:
        futs = {pool.submit(_fetch_bars, fetch, s, now_ms): s for s in syms}
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                out[s] = fut.result()
            except Exception as e:                                # noqa: BLE001
                out[s] = e
    return out


def build_rows(db: Any, cfg: Any, symbols: Optional[Sequence[str]] = None,
               fetch=None, now_ms: Optional[int] = None) -> List[Dict[str, Any]]:
    """每个标的装配一行（面板 / `python -m app.why` 共用入口）。

    `fetch` 可注入（测试用假行情，绝不触网）；默认走 `market.fetch`（与 tick 同一条链路，
    含 fail-closed 与根数校验）。
    """
    fetch = fetch or market.fetch
    syms: List[str] = [str(s) for s in (symbols if symbols is not None
                                        else (getattr(cfg, "symbols", None) or []))]
    sig = _cfg_sig(cfg)
    feeds = _fetch_many(fetch, syms, now_ms)
    rows: List[Dict[str, Any]] = []
    for symbol in syms:
        coin = _coin_of(symbol) or symbol
        bars = feeds.get(symbol)
        if isinstance(bars, Exception) or bars is None:
            # 一个标的取数失败不能拖累其它标的（与 tick 的 per-symbol try/except 同思路）
            e = bars if isinstance(bars, Exception) else RuntimeError("未取到行情")
            logger.warning("[why] %s 取行情失败：%s", symbol, e)
            rows.append({"symbol": symbol, "coin": coin, "price": None, "bars": 0,
                         "why": None, "position": None, "note": "",
                         "error": f"{type(e).__name__}: {e}",
                         "gates": gates_for(db, cfg, coin), "gate_side": 0, "gate": None})
            continue

        # ---- 缓存命中路径：**先用缓存键判断，命中就不算分位**（冷启动的性能全在这里） ----
        key = (symbol, int(bars[-2].open_ms), sig)
        cached = _WHY_CACHE.get(key)
        if cached is not None:
            # 仓位可能刚变（成交/确认/平仓），而六步缓存还活着 —— 仓位侧现读覆盖，
            # 否则面板会拿一根旧 bar 的仓位状态讲「为什么没开仓」。
            pos, note = _position_part(db, symbol)
            row = dict(cached)
            row["position"], row["note"] = pos, note
        else:
            try:
                row = why_row(db, cfg, symbol, bars)
            except Exception as e:                                # noqa: BLE001
                logger.exception("[why] %s 诊断计算失败：%s", symbol, e)
                rows.append({"symbol": symbol, "coin": coin, "price": None,
                             "bars": len(bars), "why": None, "position": None, "note": "",
                             "error": f"{type(e).__name__}: {e}",
                             "gates": {}, "gate_side": 0, "gate": None})
                continue
            if len(_WHY_CACHE) >= _WHY_CACHE_MAX:
                _WHY_CACHE.clear()   # 只保留「最近这根 bar」的读数，简单且有界
            _WHY_CACHE[key] = row
        row = dict(row)
        row["gate_side"] = _gate_side(row)
        # 闸门状态**不进缓存**：它是实时量（剩余秒数、10 分钟窗口），必须每次现读。
        row["gates"] = gates_for(db, cfg, row["coin"])
        gkey = "long" if int(row.get("gate_side") or 0) >= 0 else "short"
        row["gate"] = row["gates"].get(gkey) or {}
        rows.append(row)
    return rows


def clear_cache() -> None:
    """清缓存（测试用；参数热改已由阈值指纹 + TTL 兜住）。"""
    _WHY_CACHE.clear()
    _FEED_CACHE.clear()


# ---------------------------------------------------------------- 命令行 / 文本文案
_GATE_TXT = {"ok": "放行", "settle": "静置中", "churn": "熔断"}


def format_text(rows: Sequence[Dict[str, Any]]) -> str:
    """纯文本（容器内 `python -m app.why` 与日志用）：每标的一段，人话优先。"""
    out: List[str] = []
    for r in rows:
        out.append(f"== {r.get('coin') or r.get('symbol')} ({r.get('symbol')})")
        if r.get("error"):
            out.append(f"   行情/计算失败：{r['error']}")
        why = r.get("why") or {}
        pos = r.get("position") or {}
        if pos:
            word = "持仓中" if pos.get("status") == "open" else f"状态 {pos.get('status')}"
            side = "多" if int(pos.get("side") or 1) > 0 else "空"
            out.append(f"   仓位：{word} {side} {pos.get('qty'):g} {pos.get('qty_unit') or ''}"
                       f" @ {_num(pos.get('entry'))}，止损 {_num(pos.get('stop'))}")
        if why:
            state = ("信号成立（" + ("做多" if why.get("side") == 1 else "做空") + "）"
                     if why.get("fired") else "未开仓")
            out.append(f"   信号：{state} · 拦截于 {why.get('blocked_by')}")
            out.append(f"   原因：{why.get('reason')}")
            for st in why.get("steps") or []:
                out.append(f"     [{'✓' if st.get('ok') else '✗'}] {st.get('key')}: {st.get('detail')}")
            if why.get("drift"):
                out.append("   ⚠️ 六步推导与 evaluate_entry 结果不一致（口径漂移，需排查）")
        if r.get("note"):
            out.append(f"   注：{r['note']}")
        for key, label in (("long", "多"), ("short", "空")):
            g = (r.get("gates") or {}).get(key) or {}
            if not g:
                out.append(f"   闸门（{label}）：—")
                continue
            out.append(f"   闸门（{label}）：{_GATE_TXT.get(str(g.get('verdict') or ''), str(g.get('verdict') or '—'))}"
                       f" · 静置剩余 {g.get('settle_remaining_s')}s"
                       f" · churn {g.get('churn_count')}/{g.get('churn_limit')}"
                       f"{'（熔断）' if g.get('churn_tripped') else ''}")
    return "\n".join(out)


def _num(v: Any) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _ro_db(path: str):
    """**只读** DB 适配器（`python -m app.why` 用）。

    为什么不直接 `DB(path)`：那个构造函数会 executescript(SCHEMA) + 跑迁移，对线上库
    是写打开（虽然 DDL 幂等、实际不改内容，但诊断工具不该持有写意图）。这里用
    `mode=ro` 的 URI 打开：**任何写操作都会被 SQLite 自己拒绝**，命令行的「只读」就是
    物理保证，而不是靠自觉。

    只暴露诊断真正用到的几个读接口（`path` 给闸门作用域用，必须与生产执行器同一个值）。
    """
    import sqlite3

    class _RoDB:
        def __init__(self, p: str):
            self.path = p                       # 与 DB.path 同语义（闸门 _gate_scope 用它）
            self.conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
            self.conn.row_factory = sqlite3.Row

        def open_position(self, symbol: str):
            return self.conn.execute(
                "SELECT * FROM positions WHERE symbol=? AND status IN ('pending','open') "
                "ORDER BY signal_bar_ms DESC LIMIT 1", (symbol,)).fetchone()

        def get_config(self) -> dict:
            try:
                return {r["key"]: r["value"] for r in self.conn.execute(
                    "SELECT key, value FROM configuration")}
            except sqlite3.Error:
                return {}                        # 老库没有这张表也不影响诊断

    return _RoDB(path)


def main() -> int:                                                # pragma: no cover - 手工运维入口
    """容器内手工诊断：`python -m app.why`（只读；不加参数 = 按配置标的全量）。"""
    import os
    from app.config import Config

    db_path = os.environ.get("DB_PATH") or "/app/data/crypto_ma.db"
    if not os.path.exists(db_path):
        # 只读模式不会替你建库：路径写错/挂载没起来要直接说清楚，而不是抛 sqlite 的
        # "unable to open database file"（那会被误读成权限问题）。
        print(f"库不存在：{db_path}（用 DB_PATH 指定；容器内默认 /app/data/crypto_ma.db）")
        return 2
    db = _ro_db(db_path)
    cfg = Config()
    try:
        cfg.apply_db(db.get_config())
    except Exception as e:                                        # noqa: BLE001
        print(f"（配置回读失败，按默认口径诊断：{e}）")
    print(format_text(build_rows(db, cfg)))
    return 0


if __name__ == "__main__":                                        # pragma: no cover
    raise SystemExit(main())
