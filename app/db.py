"""SQLite 存储层（纯 stdlib sqlite3）。

表：
- configuration   用户可改参数（equity / risk_pct / ...），DB 优先于 env
- positions       信号与仓位（pending → open → closed / ignored）
- alert_logs      已推送提醒（幂等键 symbol+bar_ms+kind，sent_successfully 才算成功）
- bar_state       每个标的已处理到哪根 bar（增量 + 补报）
- feed_health     数据源健康度（心跳用）
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from functools import wraps
from typing import Any, Dict, List, Optional

from app import posfields
# v_ledger 视图 SQL 用到的同源口径常量（L③/M2：±300s 窗口与毫秒阈值只此一份，
# pnl.py 是唯一来源；app.hl.__init__ 只有常量、pnl 只用 stdlib，无循环导入）。
from app.hl.pnl import ENTRY_LOOKBACK_S, EXIT_LOOKAHEAD_S, TS_MS_THRESHOLD

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS configuration (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    signal_bar_ms INTEGER NOT NULL,
    status TEXT NOT NULL,              -- pending | open | closed | ignored
    side INTEGER NOT NULL DEFAULT 1,   -- +1 做多 / -1 做空
    entry_price REAL NOT NULL,         -- 信号建议入场价（次根开盘）
    stop_price REAL NOT NULL,
    stop_dist REAL NOT NULL,
    contracts INTEGER NOT NULL,
    coin_qty REAL NOT NULL,
    notional REAL NOT NULL,
    risk_u REAL NOT NULL,
    actual_entry REAL,                 -- 用户确认的实际成交价
    actual_contracts REAL,             -- 实际数量（HL 是币数量如 0.00095；MEXC 人工流是张数）
    actual_stop REAL,
    stop_placed INTEGER DEFAULT 0,
    exit_price REAL,
    exit_reason TEXT,                  -- stop | ma20 | manual | exchange（交易所侧已平仓）
    auto_closed INTEGER DEFAULT 0,     -- 1 = 系统按参考价自动关（用户可覆盖真实成交价）；0 = 用户已确认
    r_multiple REAL,
    -- PR-3：流水口径的净 PnL（Σclosed_pnl − 手续费），关账那一刻由执行器用
    -- hl_fills 聚合写入。pnl_source 说明来源：fills（有成交）/ no_fills（查不到
    -- 成交，net_pnl 为 NULL，绝不猜一个 0）。与 r_multiple（本地价差口径）并存，
    -- 差异超阈值会记 hl_events 事件。
    net_pnl REAL,
    pnl_source TEXT,
    equity_at_entry REAL,
    created_at REAL NOT NULL,
    opened_at REAL,
    closed_at REAL,
    UNIQUE(symbol, signal_bar_ms)
);
CREATE TABLE IF NOT EXISTS alert_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    bar_ms INTEGER,
    kind TEXT NOT NULL,                -- entry | exit | heartbeat | weekly | data_error
    message TEXT NOT NULL,
    sent_successfully INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    sent_at REAL,
    UNIQUE(symbol, bar_ms, kind)
);
CREATE TABLE IF NOT EXISTS bar_state (
    symbol TEXT PRIMARY KEY,
    last_bar_ms INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_health (
    symbol TEXT PRIMARY KEY,
    source TEXT,
    last_ok_at REAL,
    last_error TEXT,
    error_at REAL
);
-- symbol_state：面板「三标的动态」区块的每标的快照（引擎每次 tick 成功后按 symbol upsert）。
-- 纯展示缓存：面板只读它，**不参与任何信号/执行决策**（触发线与密集根数由 strategy 的
-- 现成序列算出，见 engine.symbol_snapshot）。
CREATE TABLE IF NOT EXISTS symbol_state (
    symbol TEXT PRIMARY KEY,
    price REAL,                        -- 最新一根未收盘 bar 的 close
    sma20 REAL,                        -- 最后一根已收盘 bar 的 SMA20（离场判定那条线）
    sma200 REAL,                       -- 最后一根已收盘 bar 的 SMA200（做空趋势过滤；持仓标的也照存）
    long_trigger REAL,                 -- 6 均线上轨 max（上破开多的线）
    short_trigger REAL,                -- 6 均线下轨 min（下破开空的线）
    dense_cnt INTEGER,                 -- 连续密集根数（展示封顶 6/6，库里存真实值）
    updated_at REAL
);
-- ---------------- HL 执行器（feat/hl-executor）----------------
-- hl_orders：所有下单动作的审计与幂等键。**先写 intent 再发请求**，
-- 这样进程在请求中途崩溃后，重启能看到「有意图但无结果」的单。
CREATE TABLE IF NOT EXISTS hl_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    coin TEXT NOT NULL,
    bar_ms INTEGER NOT NULL,
    purpose TEXT NOT NULL,             -- entry | stop | exit
    side INTEGER NOT NULL,
    sz REAL NOT NULL,
    px REAL,
    reduce_only INTEGER DEFAULT 0,
    status TEXT NOT NULL,              -- intent|planned|sent|resting|filled|cancelled|failed|skipped
    oid INTEGER,
    cloid TEXT,
    filled_sz REAL,
    avg_px REAL,
    fee REAL,
    error TEXT,
    raw TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(symbol, bar_ms, purpose)
);
CREATE TABLE IF NOT EXISTS hl_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tid TEXT UNIQUE,
    symbol TEXT, coin TEXT,
    side INTEGER, sz REAL, px REAL,
    fee REAL, fee_token TEXT, fee_quote REAL, dir TEXT, closed_pnl REAL, hash TEXT,
    reduce_only INTEGER DEFAULT 0,
    -- oid：产生这笔成交的交易所订单号。对账要判「这笔平仓成交是不是本仓的止损单
    -- 打出来的」（见 Executor._fill_close_proof），只靠 tid/价格无从判断；老库由
    -- _migrate 补列，老行留 NULL（NULL 一律按「不知道是哪张单」处理，fail-closed）。
    oid INTEGER,
    ts INTEGER, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hl_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, symbol TEXT, message TEXT NOT NULL, data TEXT,
    code_rev TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, equity REAL, withdrawable REAL, margin_used REAL,
    notional REAL, unrealized_pnl REAL, positions_json TEXT, created_at REAL NOT NULL
);
-- 出入金流水（feat/pnl-curve）：面板的盈亏曲线 = equity − Σflows。
-- 与 hl_snapshots 一样是**观测表**：只用来算「剔除了出入金的盈亏」，不参与交易决策。
-- kind: deposit|withdraw|send_in|send_out|vault_in|vault_out|other；
-- amount 带符号（正 = 流入账户）。UNIQUE(hash, ts) 让重复拉取天然幂等。
CREATE TABLE IF NOT EXISTS hl_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    hash TEXT,
    counterparty TEXT,
    dex TEXT,
    created_at REAL NOT NULL,
    UNIQUE(hash, ts)
);
-- 资金费流水（feat/funds-ledger）：总资金流水表的三源之一（另两源 hl_flows /
-- hl_fills，经 v_ledger 视图 union，**不复制数据**）。纯观测/记账表：只给
-- 「资金流水」区块和单粒度资金费收口读，不参与任何交易决策。
-- - amount_usd 带符号：HL userFunding delta.usdc 原值，**正 = 收入**（2026-10-24
--   主网实测：空头收资金费时 usdc 为正，符号口径是「入账视角」，别取反）。
-- - ref 幂等键：实测 userFunding 事件自带 hash 恒为全零（0x000...0，没有区分度），
--   所以 ref 优先取「非全零 hash」，否则退回 t(ms)+coin+amount 组合键 —— 组合键在
--   实测 112 条里无 (coin, ms) 重复（资金费每 coin 每小时最多一条），足够唯一。
-- - position_id：资金费时刻落在某 HL 仓位时间窗内 → 该 positions.id；否则 NULL
--   （渲染层如实显示「未归属」），回填/同步时一次定死，之后不改。
CREATE TABLE IF NOT EXISTS hl_funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,            -- 事件时刻 UTC epoch 秒
    coin TEXT NOT NULL,
    amount_usd REAL NOT NULL,    -- 有符号：HL userFunding delta.usdc，正=收入（不取反）
    position_id INTEGER,         -- 归集到的 positions.id，找不到归属为 NULL
    ref TEXT NOT NULL UNIQUE,    -- 幂等键：非全零 hash 优先，否则 t+coin+amount 组合键
    raw TEXT,                    -- 原始 JSON 存档
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hl_snap_ts ON hl_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_hl_flows_ts ON hl_flows(ts);
CREATE INDEX IF NOT EXISTS idx_hl_funding_ts ON hl_funding(ts);
CREATE INDEX IF NOT EXISTS idx_hl_events_created ON hl_events(created_at);
"""


def now() -> float:
    return time.time()


def _locked(fn):
    """DB 方法级锁：单连接被多线程共享，必须串行化（RLock 支持同线程嵌套调用）。"""
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # 单连接被 scheduler 线程 + FastAPI 线程池共享：无锁并发会静默丢数据
        # （实测 600 次复合操作 83 次 InterfaceError/OperationalError、75 条丢失）。
        # 用 RLock 把所有访问串行化；RLock 允许同线程嵌套（方法互调）。
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """轻量迁移：老库补新列（CREATE TABLE IF NOT EXISTS 不会改已存在的表）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)")}
        if "side" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN side INTEGER NOT NULL DEFAULT 1")
            logger.info("[db] 迁移：positions 新增 side 列")
        if "auto_closed" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN auto_closed INTEGER DEFAULT 0")
            # 历史行：引擎自动关的（exit_reason 为 stop/ma20）视为可覆盖，其余视为用户已确认
            self.conn.execute("UPDATE positions SET auto_closed=1 "
                              "WHERE status='closed' AND exit_reason IN ('stop','ma20')")
            logger.info("[db] 迁移：positions 新增 auto_closed 列")
        # HL 执行器列（feat/hl-executor）：执行器模式下由执行器写，人工确认流不参与
        for name, ddl in (("hl_status", "ALTER TABLE positions ADD COLUMN hl_status TEXT"),
                          ("hl_entry_oid", "ALTER TABLE positions ADD COLUMN hl_entry_oid INTEGER"),
                          ("hl_stop_oid", "ALTER TABLE positions ADD COLUMN hl_stop_oid INTEGER"),
                          ("hl_sz", "ALTER TABLE positions ADD COLUMN hl_sz REAL")):
            if name not in cols:
                self.conn.execute(ddl)
                logger.info("[db] 迁移：positions 新增 %s 列", name)
        # 流水口径净 PnL（PR-3）：老库没有这两列。历史已关仓位的 net_pnl 保持 NULL
        # ——「当时没算」是事实，回填一个用今天的代码算出来的数会掩盖当时的偏差。
        for name, ddl in (("net_pnl", "ALTER TABLE positions ADD COLUMN net_pnl REAL"),
                          ("pnl_source", "ALTER TABLE positions ADD COLUMN pnl_source TEXT")):
            if name not in cols:
                self.conn.execute(ddl)
                logger.info("[db] 迁移：positions 新增 %s 列", name)
        # 面板「三标的动态」：老库的 symbol_state 建表时还没有 sma200 列
        # （CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。
        sscols = {r["name"] for r in self.conn.execute("PRAGMA table_info(symbol_state)")}
        if sscols and "sma200" not in sscols:
            self.conn.execute("ALTER TABLE symbol_state ADD COLUMN sma200 REAL")
            logger.info("[db] 迁移：symbol_state 新增 sma200 列")
        # 手续费币种归一（PR-1）：交易所成交的 fee 可能以非计价币（如 ARB/HYPE）计价，
        # 直接按 U 累加会把「币的数量」当「U」加进净利。fee_quote = 折算后的 U 金额，
        # 无法折算时留 NULL（读取侧回退到 fee，历史行行为不变）。
        fcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(hl_fills)")}
        if fcols and "fee_quote" not in fcols:
            self.conn.execute("ALTER TABLE hl_fills ADD COLUMN fee_quote REAL")
            logger.info("[db] 迁移：hl_fills 新增 fee_quote 列")
        # 统一账户权益口径修正的幂等标记（见 app/hl/equity_fix.py）：只在一次性迁移里写 1。
        scols = {r["name"] for r in self.conn.execute("PRAGMA table_info(hl_snapshots)")}
        if scols and "equity_fixed" not in scols:
            self.conn.execute("ALTER TABLE hl_snapshots ADD COLUMN equity_fixed INTEGER NOT NULL DEFAULT 0")
            logger.info("[db] 迁移：hl_snapshots 新增 equity_fixed 列")
        # 成交所属订单号（2026-10-22 事故修复）：老库补列，历史行保持 NULL ——
        # 「当时没记」是事实，回填一个猜出来的 oid 会让对账把平仓归错单。
        if fcols and "oid" not in fcols:
            self.conn.execute("ALTER TABLE hl_fills ADD COLUMN oid INTEGER")
            logger.info("[db] 迁移：hl_fills 新增 oid 列")
        # 总资金流水视图（feat/funds-ledger）：union hl_flows / hl_fills / hl_funding
        # 三源，**不复制数据**（单一事实来源在各自表里）。视图每次启动 DROP+CREATE：
        # CREATE VIEW IF NOT EXISTS 不会更新已存在视图的定义，代码升级改了口径后
        # 老库会一直用旧视图；视图是纯读的无状态对象，重建零风险（老库直接建新视图，
        # 无 ALTER 风险）。归集口径见视图内注释（与 app/hl/pnl.py 的 ±300s 窗口一致，
        # 窗口数值 import 自 pnl.py 同源，M2/L③ 修复：±300s 与毫秒阈值别各写一个数）。
        # fills 的 ts 统一毫秒→秒归一（M2）：历史导入/手工数据出现过毫秒行（>1e12），
        # 不归一的话该行排到表尾、永不归集、其后 running balance 全错。归一规则与
        # pnl._ts_seconds 同源（TS_MS_THRESHOLD），别处不得另立判据。
        fill_ts = (f"(CASE WHEN COALESCE(l.ts, l.created_at) > {TS_MS_THRESHOLD:.0f}"
                   f" THEN COALESCE(l.ts, l.created_at) / 1000.0"
                   f" ELSE COALESCE(l.ts, l.created_at) END)")
        self.conn.execute("DROP VIEW IF EXISTS v_ledger")
        self.conn.execute(f"""
            CREATE VIEW v_ledger AS
            -- 出入金：amount 落库时已带符号（正=流入），send_in/send_out 等全部照原值
            SELECT f.ts AS ts,
                   f.kind AS kind,
                   f.amount AS amount_usd,
                   NULL AS position_id,
                   '' AS coin,
                   'flow:' || f.id AS ref,
                   0 AS fee_missing,
                   '' AS fee_token,
                   'flow' AS src
            FROM hl_flows f
            UNION ALL
            -- 成交：金额 = closed_pnl − fee_quote（手续费并入成交净额，已拍板口径）。
            -- fee_quote 为 NULL 的老行按 0 计入金额，但 fee_missing=1 交渲染层如实
            -- 标注（M3 修复：成因按 fee_token 分两种 —— 非计价币 → 「费用未折算」，
            -- 真老数据 → 「老数据缺手续费」；fee_token 原样透传给渲染层判断）。
            -- position 归集：与引擎（app/hl/pnl.py window_bounds）同窗口
            -- [opened_at−300, closed_at+300]（常量同源 import），coin 用
            -- positions.symbol 前缀（BTC_USDT → BTC，与执行器写 hl_fills
            -- 的 symbol/coin 对应关系一致）；同 coin 多仓命中取最近开的一仓；
            -- ts 为 NULL 的成交无法定位归属 → position_id 留 NULL。
            SELECT {fill_ts} AS ts,
                   CASE
                     WHEN lower(COALESCE(l.dir, '')) LIKE '%open%' THEN 'fill_open'
                     WHEN lower(COALESCE(l.dir, '')) LIKE '%close%' THEN 'fill_close'
                     ELSE 'fill'
                   END AS kind,
                   (COALESCE(l.closed_pnl, 0) - COALESCE(l.fee_quote, 0)) AS amount_usd,
                   (SELECT p.id FROM positions p
                     WHERE l.coin IS NOT NULL AND l.coin <> '' AND l.ts IS NOT NULL
                       AND substr(p.symbol, 1, instr(p.symbol || '_', '_') - 1) = l.coin
                       AND {fill_ts} >= (p.opened_at - {ENTRY_LOOKBACK_S:.0f})
                       AND {fill_ts} <= (COALESCE(p.closed_at, 1.0e15) + {EXIT_LOOKAHEAD_S:.0f})
                     ORDER BY p.opened_at DESC LIMIT 1) AS position_id,
                   COALESCE(l.coin, '') AS coin,
                   'fill:' || COALESCE(l.tid, CAST(l.id AS TEXT)) AS ref,
                   CASE WHEN l.fee_quote IS NULL THEN 1 ELSE 0 END AS fee_missing,
                   COALESCE(l.fee_token, '') AS fee_token,
                   'fill' AS src
            FROM hl_fills l
            UNION ALL
            -- 资金费：amount_usd 原样（正=收入），position_id 已在同步/回填时归集
            SELECT g.ts AS ts,
                   'funding' AS kind,
                   g.amount_usd AS amount_usd,
                   g.position_id AS position_id,
                   g.coin AS coin,
                   'fund:' || g.ref AS ref,
                   0 AS fee_missing,
                   '' AS fee_token,
                   'funding' AS src
            FROM hl_funding g
        """)

    # ---------------- configuration
    @_locked
    def get_config(self) -> Dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM configuration")}

    @_locked
    def get_config_str(self, key: str, default: str = "") -> str:
        """读一个配置值并做 JSON 解码（set_config 用 json.dumps 存，中文会被转义）。"""
        raw = self.get_config().get(key)
        if raw is None:
            return default
        try:
            val = json.loads(raw)
        except (TypeError, ValueError):
            return str(raw)
        return default if val is None else str(val)

    @_locked
    def set_config(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO configuration(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), now()))
        self.conn.commit()

    @_locked
    def del_config(self, key: str) -> None:
        """删掉一个配置键（宽限期 `hl_close_grace:<pid>` 这类「有生命周期的状态」用）。

        `set_config` 只能写不能删：宽限到期/证明成功必须真的**消失**，
        否则「键存在」这条判据永远成立（下一轮又被当成还在宽限期）。
        """
        self.conn.execute("DELETE FROM configuration WHERE key=?", (str(key),))
        self.conn.commit()

    # ---------------- bar state
    @_locked
    def get_last_bar(self, symbol: str) -> Optional[int]:
        r = self.conn.execute("SELECT last_bar_ms FROM bar_state WHERE symbol=?", (symbol,)).fetchone()
        return int(r["last_bar_ms"]) if r else None

    @_locked
    def set_last_bar(self, symbol: str, bar_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO bar_state(symbol, last_bar_ms, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET last_bar_ms=excluded.last_bar_ms, updated_at=excluded.updated_at",
            (symbol, bar_ms, now()))
        self.conn.commit()

    # ---------------- positions
    @_locked
    def open_position(self, symbol: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM positions WHERE symbol=? AND status IN ('pending','open') "
            "ORDER BY signal_bar_ms DESC LIMIT 1", (symbol,)).fetchone()

    @_locked
    def create_position(self, **kw) -> int:
        cols = ("symbol, signal_bar_ms, status, side, entry_price, stop_price, stop_dist, contracts, "
                "coin_qty, notional, risk_u, created_at, equity_at_entry")
        vals = (kw["symbol"], kw["signal_bar_ms"], kw.get("status", "pending"), kw.get("side", 1),
                kw["entry_price"], kw["stop_price"], kw["stop_dist"], kw["contracts"],
                kw["coin_qty"], kw["notional"], kw["risk_u"], now(), kw.get("equity_at_entry"))
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO positions({cols}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
        self.conn.commit()
        if cur.rowcount == 0:
            # INSERT OR IGNORE 被 UNIQUE(symbol, signal_bar_ms) 忽略时，lastrowid
            # 残留同连接上一次成功 INSERT 的 id（可能是别的表）——直接返回会拿到
            # 跨表的陈旧 id（确认链接 404 / on_entry 操作错仓）。必须回查真实行。
            row = self.conn.execute(
                "SELECT id FROM positions WHERE symbol=? AND signal_bar_ms=?",
                (kw["symbol"], kw["signal_bar_ms"])).fetchone()
            return int(row["id"]) if row is not None else 0
        return int(cur.lastrowid) if cur.lastrowid is not None else 0

    @_locked
    def position_by_id(self, pid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()

    @_locked
    def confirm_position(self, pid: int, actual_entry: float, actual_contracts: float,
                         actual_stop: float, stop_placed: bool,
                         equity_at_entry: Optional[float] = None) -> None:
        """确认开仓（pending → open）。

        `equity_at_entry`：**可选**的实际生效基数写回（双口径收敛）。引擎建行时写的是
        配置 equity（如 1000U），HL 执行器按生效基数（如 100U）成交后在确认点覆盖它，
        这样 review_fill 的「权益的 X%」等读者自然读到真实基数。不传 = 行为不变。
        """
        sets = ("UPDATE positions SET status='open', actual_entry=?, actual_contracts=?, "
                "actual_stop=?, stop_placed=?, opened_at=?")
        vals: List[Any] = [actual_entry, actual_contracts, actual_stop,
                           1 if stop_placed else 0, now()]
        if equity_at_entry is not None:
            sets += ", equity_at_entry=?"
            vals.append(float(equity_at_entry))
        sets += " WHERE id=?"
        vals.append(pid)
        self.conn.execute(sets, vals)
        self.conn.commit()

    @_locked
    def update_fill(self, pid: int, actual_entry: float, actual_contracts: float,
                    actual_stop: float, stop_placed: bool) -> None:
        """修改已确认的开仓数据（用户发现录错）。仓位已平仓时按新数据重算 R 倍数。

        **只服务 MEXC 人工流**：HL 仓（`hl_status` 非空）的真实成交在交易所侧，
        本地改正会与 `hl_fills`/交易所持仓脱节（表单里的「张数/成交金额换算」是
        MEXC 面值口径，写进去就是脏数据）→ 直接拒绝，让用户去面板核对。
        """
        p = self.position_by_id(pid)
        if not p:
            return
        if posfields.is_hl(p):
            raise ValueError("HL 执行器管理的仓位不支持在确认页修改开仓记录，"
                             "请到面板「记录」页按 hl_fills 核对真实成交")
        risk = abs(actual_entry - actual_stop)
        sets = "actual_entry=?, actual_contracts=?, actual_stop=?, stop_placed=?"
        vals: List[Any] = [actual_entry, actual_contracts, actual_stop, 1 if stop_placed else 0]
        if p["status"] == "closed" and p["exit_price"] is not None and risk > 0:
            exit_px = float(p["exit_price"])
            move = (exit_px - actual_entry) if (p["side"] or 1) == 1 else (actual_entry - exit_px)
            sets += ", r_multiple=?"
            vals.append(move / risk)
        vals.append(pid)
        self.conn.execute(f"UPDATE positions SET {sets} WHERE id=?", vals)
        self.conn.commit()

    @_locked
    def ignore_position(self, pid: int) -> None:
        self.conn.execute("UPDATE positions SET status='ignored', closed_at=? WHERE id=?", (now(), pid))
        self.conn.commit()

    @_locked
    def close_position(self, pid: int, exit_price: float, exit_reason: str, auto: bool = False) -> None:
        """关闭仓位。auto=True 表示引擎按参考价自动关（exit_price 只是参考，用户可覆盖）。"""
        p = self.position_by_id(pid)
        if not p:
            return
        entry = p["actual_entry"] or p["entry_price"]
        stop = p["actual_stop"] or p["stop_price"]
        side = p["side"] or 1
        risk = abs(entry - stop)
        move = (exit_price - entry) if side == 1 else (entry - exit_price)
        r = move / risk if risk > 0 else 0.0
        self.conn.execute(
            "UPDATE positions SET status='closed', exit_price=?, exit_reason=?, r_multiple=?, "
            "auto_closed=?, closed_at=? WHERE id=?",
            (exit_price, exit_reason, r, 1 if auto else 0, now(), pid))
        self.conn.commit()

    @_locked
    def recent_positions(self, limit: int = 20) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM positions ORDER BY id DESC LIMIT ?", (limit,)))

    @_locked
    def closed_positions(self, since: float = 0.0) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM positions WHERE status='closed' AND closed_at>=? ORDER BY closed_at", (since,)))

    # ---------------- alert logs（幂等 + 成功后标记）
    @_locked
    def was_alerted(self, symbol: Optional[str], bar_ms: Optional[int], kind: str,
                    within_s: Optional[float] = None) -> bool:
        """是否已推送过。

        bar_ms 为 NULL 的提醒（data_error/心跳类）没有天然幂等键，NULL 会匹配
        任意历史成功行 → 「每个标的一生只报一次故障」。对这类加时间窗：
        within_s 秒内报过才算重复，窗口过后允许再报（故障持续时每 2h 提醒一次）。
        """
        if bar_ms is None and within_s is not None:
            cutoff = now() - within_s
            r = self.conn.execute(
                "SELECT 1 FROM alert_logs WHERE symbol IS ? AND bar_ms IS NULL AND kind=?"
                " AND sent_successfully=1 AND created_at > ?",
                (symbol, kind, cutoff)).fetchone()
            return bool(r)
        r = self.conn.execute(
            "SELECT 1 FROM alert_logs WHERE symbol IS ? AND bar_ms IS ? AND kind=? AND sent_successfully=1",
            (symbol, bar_ms, kind)).fetchone()
        return bool(r)

    @_locked
    def log_alert(self, symbol: Optional[str], bar_ms: Optional[int], kind: str, message: str,
                  success: bool) -> None:
        self.conn.execute(
            "INSERT INTO alert_logs(symbol, bar_ms, kind, message, sent_successfully, attempts, created_at, sent_at) "
            "VALUES(?,?,?,?,?,1,?,?) "
            "ON CONFLICT(symbol, bar_ms, kind) DO UPDATE SET message=excluded.message, "
            "sent_successfully=excluded.sent_successfully, attempts=alert_logs.attempts+1, sent_at=excluded.sent_at",
            (symbol, bar_ms, kind, message, 1 if success else 0, now(), now() if success else None))
        self.conn.commit()

    @_locked
    def recent_alerts(self, limit: int = 20) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM alert_logs ORDER BY id DESC LIMIT ?", (limit,)))

    # ---------------- 数据源告警观察期（feat/feed-alert-defer）
    # 生命周期：首次失败 → log_alert(kind='data_error_pending', success=False) 落库
    # 不推送；恢复 → resolve_pending_feed_errors 就地标记（sent_successfully=1，
    # 永不推送）；持续未恢复 ≥ 阈值 → promote_feed_error 转正为 data_error 推送。
    @_locked
    def pending_feed_error(self, symbol: str) -> Optional[sqlite3.Row]:
        """该标的当前「观察中」的数据源告警行（没有则 None）。

        已恢复的行 sent_successfully=1（恢复 = 这行永远不需要推送了，按「已了结」
        记账，这样补推/心跳失败计数/看守脚本这些按 sent=0 捞行的消费者天然跳过
        它）；已转正的行 kind 已改成 data_error —— 两类都匹配不上，返回非 None
        即「观察中且未推送」。
        """
        return self.conn.execute(
            "SELECT * FROM alert_logs WHERE symbol=? AND kind='data_error_pending'"
            " AND sent_successfully=0 ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()

    @_locked
    def feed_error_alerted_since(self, symbol: str, since: float) -> bool:
        """本故障窗口内（since = 该标的最近一次 feed 成功时刻）是否已有 data_error 行。

        「同一故障事件只推一次」的锚点：pending 转正后行不再是 pending，若不查
        这个，下一轮失败 tick 会另开一条 pending → 又过 30 分钟二次推送。恢复过
        的窗口 last_ok_at 会越过旧行的 created_at，所以新故障窗口能正常另开。
        """
        r = self.conn.execute(
            "SELECT 1 FROM alert_logs WHERE symbol=? AND kind='data_error'"
            " AND created_at>? LIMIT 1", (symbol, since)).fetchone()
        return bool(r)

    @_locked
    def promote_feed_error(self, pending_id: int, message: str, success: bool) -> None:
        """pending 行转正：kind 改 data_error（message 由调用方剥掉观察后缀）。

        推送失败也照转（sent_successfully=0）——交给既有 24h 补推通道重试，而
        不是退回 pending（故障已经持续超过阈值了，再等一个观察期没有意义）。
        """
        self.conn.execute(
            "UPDATE alert_logs SET kind='data_error', message=?, sent_successfully=?,"
            " sent_at=? WHERE id=?",
            (message, 1 if success else 0, now() if success else None, pending_id))
        self.conn.commit()

    @_locked
    def resolve_pending_feed_errors(self, symbol: str, note: str) -> int:
        """feed 恢复成功：该标的未推送的 pending 行就地追加恢复标记（永不推送）。

        返回标记的行数（0 = 没有观察中的行）。sent_successfully 置 1 =「该行已
        了结、无需任何后续推送动作」，不是「推送成功」——message 里的
        「已恢复（未推送）」是给人看的真话。
        """
        cur = self.conn.execute(
            "UPDATE alert_logs SET message=message||?, sent_successfully=1, sent_at=?"
            " WHERE symbol=? AND kind='data_error_pending' AND sent_successfully=0",
            (note, now(), symbol))
        self.conn.commit()
        return cur.rowcount


    @_locked
    def failed_alerts_since(self, since: float) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM alert_logs WHERE sent_successfully=0 AND created_at>=? ORDER BY id DESC", (since,)))

    @_locked
    def mark_alert_sent(self, aid: int) -> None:
        """补推成功后标记（供 _retry_failed_alerts 用）。"""
        self.conn.execute("UPDATE alert_logs SET sent_successfully=1, sent_at=? WHERE id=?",
                          (now(), aid))
        self.conn.commit()

    @_locked
    def prune_alert_logs(self, keep_days: float = 60.0, keep_rows: int = 5000) -> None:
        """清 alert_logs：bar_ms=NULL 的超期行 + 超出 keep_rows 的旧行。

        必须走 @_locked（曾经直接裸用共享连接，跨线程 commit 会打乱事务边界）。
        """
        import time as _time
        cutoff = _time.time() - keep_days * 86400
        self.conn.execute(
            "DELETE FROM alert_logs WHERE bar_ms IS NULL AND created_at < ?", (cutoff,))
        self.conn.execute(
            "DELETE FROM alert_logs WHERE id < (SELECT MIN(id) FROM "
            "(SELECT id FROM alert_logs ORDER BY id DESC LIMIT ?))", (keep_rows,))
        self.conn.commit()

    # ---------------- feed health
    @_locked
    def mark_feed(self, symbol: str, source: str, ok: bool, error: str = "") -> None:
        if ok:
            self.conn.execute(
                "INSERT INTO feed_health(symbol, source, last_ok_at) VALUES(?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET source=excluded.source, last_ok_at=excluded.last_ok_at",
                (symbol, source, now()))
        else:
            self.conn.execute(
                "INSERT INTO feed_health(symbol, last_error, error_at) VALUES(?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET last_error=excluded.last_error, error_at=excluded.error_at",
                (symbol, error, now()))
        self.conn.commit()

    @_locked
    def feed_status(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM feed_health ORDER BY symbol"))

    # ---------------- symbol_state（面板「三标的动态」的展示缓存）
    @_locked
    def set_symbol_state(self, symbol: str, price: Optional[float] = None,
                         sma20: Optional[float] = None, long_trigger: Optional[float] = None,
                         short_trigger: Optional[float] = None,
                         dense_cnt: Optional[int] = None,
                         sma200: Optional[float] = None) -> None:
        """按 symbol upsert（主键即幂等键）：同一标的二次写覆盖旧值，永不产生第二行。

        字段允许为 NULL（持仓标的的触发线/密集根数没有意义）—— 面板遇到 NULL 会略去
        对应子行，所以这里不做任何补零/兜底。
        """
        self.conn.execute(
            "INSERT INTO symbol_state(symbol, price, sma20, long_trigger, short_trigger,"
            " dense_cnt, sma200, updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET price=excluded.price, sma20=excluded.sma20,"
            " long_trigger=excluded.long_trigger, short_trigger=excluded.short_trigger,"
            " dense_cnt=excluded.dense_cnt, sma200=excluded.sma200,"
            " updated_at=excluded.updated_at",
            (symbol, price, sma20, long_trigger, short_trigger, dense_cnt, sma200, now()))
        self.conn.commit()

    @_locked
    def symbol_state(self, symbol: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM symbol_state WHERE symbol=?", (symbol,)).fetchone()

    @_locked
    def symbol_states(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM symbol_state ORDER BY symbol"))

    # ---------------- HL 执行器（feat/hl-executor）
    @_locked
    def hl_record_intent(self, symbol: str, coin: str, bar_ms: int, purpose: str, side: int,
                         sz: float, px: Optional[float] = None, reduce_only: bool = False,
                         cloid: Optional[str] = None, status: str = "intent",
                         error: Optional[str] = None) -> int:
        """登记一次下单意图（幂等键 symbol+bar_ms+purpose）。已存在则返回原 id（不覆盖）。"""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO hl_orders(symbol, coin, bar_ms, purpose, side, sz, px, reduce_only,"
            " status, cloid, error, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, coin, bar_ms, purpose, side, float(sz), px, 1 if reduce_only else 0,
             status, cloid, error, now(), now()))
        self.conn.commit()
        if cur.rowcount:
            return int(cur.lastrowid or 0)
        row = self.hl_order(symbol, bar_ms, purpose)
        return int(row["id"]) if row else 0

    @_locked
    def hl_order(self, symbol: str, bar_ms: int, purpose: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM hl_orders WHERE symbol=? AND bar_ms=? AND purpose=?",
            (symbol, bar_ms, purpose)).fetchone()

    @_locked
    def hl_update_order(self, symbol: str, bar_ms: int, purpose: str, **kw) -> None:
        allowed = ("status", "oid", "filled_sz", "avg_px", "fee", "error", "raw", "px", "sz")
        sets, vals = [], []
        for k, v in kw.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        sets.append("updated_at=?")
        vals.append(now())
        vals.extend([symbol, bar_ms, purpose])
        self.conn.execute(f"UPDATE hl_orders SET {', '.join(sets)} "
                          "WHERE symbol=? AND bar_ms=? AND purpose=?", vals)
        self.conn.commit()

    @_locked
    def hl_recent_orders(self, limit: int = 30) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM hl_orders ORDER BY id DESC LIMIT ?", (limit,)))

    @_locked
    def hl_stuck_orders(self, older_than_s: float = 180.0) -> List[sqlite3.Row]:
        """长时间停在 intent/sent 的单 = 可能已发出但结果未知（崩溃/超时），需人工或对账处理。"""
        return list(self.conn.execute(
            "SELECT * FROM hl_orders WHERE status IN ('intent','sent') AND created_at < ?"
            " ORDER BY id DESC", (now() - older_than_s,)))

    @_locked
    def hl_upsert_fill(self, **kw) -> None:
        """落一笔成交（按交易所 tid 去重）。

        ``fee`` 是交易所原值，币种由 ``fee_token`` 指明；``fee_quote`` 是折算成计价币
        （U）后的金额 —— 只有 fee_token 属于计价币时才等于 fee，其余留 None 由调用方
        决定怎么标注（见 Executor._sync_fills）。列顺序与 CREATE TABLE 一致。
        ``oid`` 是产生这笔成交的订单号（对账判「哪张单平的仓」用），老数据可能为 None。
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO hl_fills(tid, symbol, coin, side, sz, px, fee, fee_token,"
            " fee_quote, dir, closed_pnl, hash, reduce_only, oid, ts, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(kw.get("tid")), kw.get("symbol"), kw.get("coin"), kw.get("side"), kw.get("sz"),
             kw.get("px"), kw.get("fee"), kw.get("fee_token"), kw.get("fee_quote"),
             kw.get("dir"), kw.get("closed_pnl"), kw.get("hash"),
             1 if kw.get("reduce_only") else 0, kw.get("oid"),
             kw.get("ts"), now()))
        self.conn.commit()

    @_locked
    def hl_recent_fills(self, limit: int = 30) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM hl_fills ORDER BY id DESC LIMIT ?", (limit,)))

    @_locked
    def hl_fills_since(self, since_ts: float) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM hl_fills WHERE created_at >= ? ORDER BY id", (since_ts,)))

    @_locked
    def hl_fills_window(self, coin: str, lo: Optional[float] = None,
                        hi: Optional[float] = None) -> List[sqlite3.Row]:
        """按**成交时间 ts** 取某标的的成交（不是入库时间 created_at）。

        为什么不能用 ``hl_fills_since``：它按 created_at 过滤 —— 首次同步会把几个月前
        的历史成交一次写进来（created_at=今天、ts=三个月前），按入库时间取窗口等于
        「把整段历史都算进当前这一仓」。聚合必须按 ts（见 app/hl/pnl.py）。
        ts 为 NULL 的行无法定位归属，一律不返回（宁可少算，不串仓）。
        """
        sql = "SELECT * FROM hl_fills WHERE coin=? AND ts IS NOT NULL"
        args: List[Any] = [str(coin)]
        if lo is not None:
            sql += " AND ts >= ?"
            args.append(float(lo))
        if hi is not None:
            sql += " AND ts <= ?"
            args.append(float(hi))
        sql += " ORDER BY ts ASC, id ASC"
        return list(self.conn.execute(sql, args))

    @_locked
    def hl_log_event(self, kind: str, message: str, symbol: Optional[str] = None,
                     data: Any = None, code_rev: str = "") -> None:
        self.conn.execute(
            "INSERT INTO hl_events(kind, symbol, message, data, code_rev, created_at) VALUES(?,?,?,?,?,?)",
            (kind, symbol, message, json.dumps(data, ensure_ascii=False) if data is not None else None,
             code_rev, now()))
        self.conn.commit()

    @_locked
    def hl_recent_events(self, limit: int = 30, kind: Optional[str] = None) -> List[sqlite3.Row]:
        if kind:
            return list(self.conn.execute(
                "SELECT * FROM hl_events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)))
        return list(self.conn.execute("SELECT * FROM hl_events ORDER BY id DESC LIMIT ?", (limit,)))

    @_locked
    def hl_last_event_at(self, kind: Optional[str] = None) -> Optional[float]:
        if kind:
            r = self.conn.execute("SELECT max(created_at) AS t FROM hl_events WHERE kind=?", (kind,)).fetchone()
        else:
            r = self.conn.execute("SELECT max(created_at) AS t FROM hl_events").fetchone()
        return float(r["t"]) if r and r["t"] else None

    @_locked
    def hl_add_snapshot(self, snap: Dict[str, Any]) -> None:
        positions = snap.get("positions") or []
        upnl = sum(float(p.get("unrealized_pnl") or 0.0) for p in positions)
        self.conn.execute(
            "INSERT INTO hl_snapshots(ts, equity, withdrawable, margin_used, notional, unrealized_pnl,"
            " positions_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (float(snap.get("ts") or now()),
             # 优先存「账户总值」：统一账户下 equity（永续口径）恒为 0，存它曲线会全是 0
             float(snap.get("equity_total", snap.get("equity")) or 0.0),
             float(snap.get("withdrawable") or 0.0), float(snap.get("margin_used") or 0.0),
             float(snap.get("notional") or 0.0), upnl,
             json.dumps(positions, ensure_ascii=False), now()))
        self.conn.commit()

    @_locked
    def hl_snapshots(self, since_ts: float = 0.0, limit: int = 4000) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM hl_snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT ?", (since_ts, limit)))

    @_locked
    def hl_latest_snapshot(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM hl_snapshots ORDER BY ts DESC LIMIT 1").fetchone()

    @_locked
    def hl_snapshots_recent(self, limit: int = 20000) -> List[sqlite3.Row]:
        """**最近** N 张快照，仍按 ts 升序返回（回撤 bootstrap 专用）。

        为什么不能复用 `hl_snapshots(0, N)`：那个是 `ORDER BY ts ASC LIMIT N`，
        表里超过 N 张时取到的是**最老**的 N 张 —— 最近的一段快照被丢掉，bootstrap
        的最后一步落后于真实最新（实测 20005 张时落后 0.3h），那一段被压成一步算，
        正是「把跨过的入金当亏损」要防的形态。先 DESC 取最近 N 张再反转。
        """
        rows = list(self.conn.execute(
            "SELECT * FROM hl_snapshots ORDER BY ts DESC LIMIT ?", (int(limit),)))
        rows.reverse()
        return rows


    @_locked
    def hl_first_positive_snapshot(self) -> Optional[sqlite3.Row]:
        """全表**最早一张 equity>0** 的快照（回撤熔断的 TWR 锚点，见 app/hl/dd.py）。

        为什么另起一个方法而不是 `hl_snapshots(0, 1)`：执行器先跑起来、钱后到账时，
        最前面会有一批 equity=0 的快照（本账户实测 100.79U 到账前就是），按 LIMIT 1
        取到的是「还没有本金」那一张 —— 拿它做锚点等于把入金当收益。
        也不带 30 天窗口：锚点可能比窗口更老，而 bootstrap 正是要用全表重放。
        """
        return self.conn.execute(
            "SELECT * FROM hl_snapshots WHERE equity > 0 ORDER BY ts ASC LIMIT 1").fetchone()

    # ---------------- 出入金流水（feat/pnl-curve）
    @_locked
    def hl_add_flow(self, flow: Dict[str, Any]) -> bool:
        """写一条流水。`INSERT OR IGNORE` + `UNIQUE(hash, ts)` = 幂等（重复拉取不写两行）。

        返回 True = 真的插了一行（新流水），False = 已存在被忽略。返回值给执行器做
        「本轮新拉到几笔」的日志，也给测试断言去重；**不参与任何交易判断**。
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO hl_flows(ts, kind, amount, hash, counterparty, dex, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (float(flow.get("ts") or 0.0), str(flow.get("kind") or "other"),
             float(flow.get("amount") or 0.0), flow.get("hash"),
             flow.get("counterparty") or None, flow.get("dex") or None, now()))
        self.conn.commit()
        return bool(cur.rowcount)

    @_locked
    def hl_flows_since(self, since_ts: float = 0.0) -> List[sqlite3.Row]:
        """按时间升序返回流水（读侧算前缀和用；总量是「一生几百条」级别）。"""
        return list(self.conn.execute(
            "SELECT * FROM hl_flows WHERE ts >= ? ORDER BY ts ASC, id ASC", (float(since_ts),)))

    @_locked
    def hl_flow_sum(self, since_ts: float = 0.0) -> float:
        """净入金合计（正 = 净流入）。"""
        r = self.conn.execute(
            "SELECT sum(amount) AS s FROM hl_flows WHERE ts >= ?", (float(since_ts),)).fetchone()
        return float(r["s"]) if r and r["s"] is not None else 0.0

    @_locked
    def hl_flow_last_ts(self) -> Optional[float]:
        """最后一条流水的交易所时间（秒）。None = 本地一条都没有（→ 首次全量回填）。"""
        r = self.conn.execute("SELECT max(ts) AS t FROM hl_flows").fetchone()
        return float(r["t"]) if r and r["t"] else None

    # ---------------- 资金费流水（feat/funds-ledger，纯观测/记账） ----------------
    @_locked
    def hl_add_funding(self, row: Dict[str, Any]) -> bool:
        """写一条资金费。`INSERT OR IGNORE` + `ref` UNIQUE = 幂等（重复拉取不写两行）。

        返回 True = 真的插了一行。与 hl_add_flow 同款约定：返回值只给同步日志和
        测试断言用，**不参与任何交易判断**。amount_usd 必须是交易所原值（正=收入），
        取反的活儿谁都不许干 —— 符号口径只在渲染层解释。
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO hl_funding(ts, coin, amount_usd, position_id, ref, raw, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (float(row.get("ts") or 0.0), str(row.get("coin") or ""),
             float(row.get("amount_usd") or 0.0), row.get("position_id"),
             str(row.get("ref") or ""), row.get("raw"), now()))
        self.conn.commit()
        return bool(cur.rowcount)

    @_locked
    def hl_funding_last_ts(self) -> Optional[float]:
        """最后一条资金费的交易所时间（秒）。None = 本地一条都没有（→ 全量回填）。"""
        r = self.conn.execute("SELECT max(ts) AS t FROM hl_funding").fetchone()
        return float(r["t"]) if r and r["t"] else None

    @_locked
    def hl_funding_origin_ts(self) -> Optional[float]:
        """回填起点：账户首张正权益快照 与 最早 hl_flows.ts 取**早者**。

        为什么取早者：两把尺子量的都是「账户从什么时候开始有钱」—— 快照是执行器
        眼里的（可能有执行器先跑、钱后到的 0 权益期，所以用首张正权益），流水是
        链上转账眼里的（钱到账即有行）。取更早的那个，宁可多拉一段（ref 幂等会
        去重），也不能让第一笔资金费掉在起点之前被漏掉。
        """
        snap = self.hl_first_positive_snapshot()
        r = self.conn.execute("SELECT min(ts) AS t FROM hl_flows").fetchone()
        first_flow = float(r["t"]) if r and r["t"] else None
        cands: List[float] = []
        if snap is not None and snap["ts"]:
            cands.append(float(snap["ts"]))
        if first_flow is not None:
            cands.append(first_flow)
        return min(cands) if cands else None

    @_locked
    def hl_funding_by_position(self) -> Dict[int, float]:
        """{position_id: Σ amount_usd}（单粒度资金费收口用；无资金费行的仓不在字典里）。"""
        out: Dict[int, float] = {}
        for r in self.conn.execute(
                "SELECT position_id, sum(amount_usd) AS s FROM hl_funding"
                " WHERE position_id IS NOT NULL GROUP BY position_id"):
            out[int(r["position_id"])] = float(r["s"])
        return out

    # ---------------- 总资金流水（v_ledger 视图，只读） ----------------
    @_locked
    def hl_ledger_rows(self) -> List[sqlite3.Row]:
        """全量总账（v_ledger），时间正序（同刻按 ref 排序保证稳定）。

        running balance 必须**全量正序**算完再由渲染层切片分页 —— 用当前页切片
        现算余额会把「页首之前的所有历史」漏掉，余额全是错的（spec 明令禁止）。
        总量是「一生几百条」级别，一次读全表最省事。
        """
        return list(self.conn.execute(
            "SELECT * FROM v_ledger ORDER BY ts ASC, ref ASC"))

    @_locked
    def hl_ledger_sum(self) -> float:
        """Σ全部流水（对账行用）。空表 = 0.0。"""
        r = self.conn.execute("SELECT sum(amount_usd) AS s FROM v_ledger").fetchone()
        return float(r["s"]) if r and r["s"] is not None else 0.0

    # ---------------- 记录页列表子页（feat/list-subpages，纯只读） ----------------
    #
    # 六个列表子页（/admin/list/*）共用一张**白名单注册表**：可筛选字段、时间列、
    # 排序键都只从这里拼 SQL —— 路由层传上来的键不在这张表里就被忽略（用户输入
    # 永远走参数绑定，不拼进 SQL）。只读：不碰任何既有表结构与写入路径。
    LIST_SUBPAGES: Dict[str, Dict[str, Any]] = {
        # time_unit：时间列的存储单位（日期筛选边界由 admin_ui 按它换算好再传进来）。
        # - hl_orders / hl_events / alert_logs 用 created_at（db.now()，秒）；
        # - hl_fills 按「成交时间」筛（与展示列一致）：交易所历史行毫秒/秒混存，
        #   沿用 v_ledger 同一条归一表达式（TS_MS_THRESHOLD 同源，不另立判据）；
        # - positions.signal_bar_ms 是毫秒（4h bar 的 bar_open_ms）。
        "orders": {"table": "hl_orders", "time_expr": "created_at", "time_unit": "s",
                   "filters": ("coin", "purpose")},
        "fills": {"table": "hl_fills",
                  "time_expr": ("(CASE WHEN COALESCE(ts, created_at) > "
                                f"{TS_MS_THRESHOLD:.0f} THEN COALESCE(ts, created_at) / 1000.0"
                                " ELSE COALESCE(ts, created_at) END)"),
                  "time_unit": "s", "filters": ("coin", "dir")},
        "ledger": {"table": "v_ledger", "time_expr": "ts", "time_unit": "s",
                   "filters": ("kind", "coin", "position_id")},
        "events": {"table": "hl_events", "time_expr": "created_at", "time_unit": "s",
                   "filters": ("kind",)},
        "alerts": {"table": "alert_logs", "time_expr": "created_at", "time_unit": "s",
                   "filters": ("kind",)},
        "positions": {"table": "positions", "time_expr": "signal_bar_ms", "time_unit": "ms",
                      "filters": ("symbol", "status")},
    }

    @classmethod
    def _list_subpage_spec(cls, name: str) -> Dict[str, Any]:
        spec = cls.LIST_SUBPAGES.get(str(name or ""))
        if spec is None:
            raise ValueError(f"未知列表子页：{name!r}")
        return spec

    @classmethod
    def _list_subpage_where(cls, spec: Dict[str, Any], filters: Optional[Dict[str, Any]],
                            time_from: Optional[float], time_to: Optional[float]) -> tuple:
        """WHERE 子句拼装（字段白名单 + 参数绑定；日期边界已是时间列同单位的 epoch）。"""
        where: List[str] = []
        args: List[Any] = []
        for key in spec["filters"]:
            val = (filters or {}).get(key)
            if val in (None, ""):
                continue
            where.append(f"{key} = ?")
            args.append(val)
        if time_from is not None:
            where.append(f"{spec['time_expr']} >= ?")
            args.append(time_from)
        if time_to is not None:
            where.append(f"{spec['time_expr']} <= ?")
            args.append(time_to)
        return where, args

    @_locked
    def list_subpage_rows(self, name: str, limit: int = 20, before: Optional[int] = None,
                          after: Optional[int] = None, filters: Optional[Dict[str, Any]] = None,
                          time_from: Optional[float] = None,
                          time_to: Optional[float] = None) -> List[sqlite3.Row]:
        """列表子页取数：id 游标分页（不 OFFSET），返回**展示序**（最新在前）。

        游标稳定性：``before=<id>`` 取 id 更旧的紧邻一页、``after=<id>`` 取更新的
        紧邻一页 —— 翻页期间插入新行（id 更大）只会出现在「更新」方向的最外侧，
        已翻到的页内容不变（OFFSET 会整体漂移，spec 明令禁止）。
        ``after`` 方向必须先正序取紧邻边界的 limit 行再反转：直接 DESC LIMIT 拿到
        的是「全表最新的 limit 行」，中间插新行时页内容会跳变。

        ``ledger`` 例外：余额列要**全量正序**预计算后再切片（复用 PR #53 口径，
        见 app/hl/funding.running_balances），所以不做 SQL 级游标 —— 一次返回该
        筛选条件下全量正序（ts ASC, ref ASC，与 hl_ledger_rows 同一条排序），
        v_ledger 是「一生几百条」级别，一次读全表最省事；切片在 admin_ui 按
        (ts, ref) 边界做，新流水只会追加在正序尾部，老页不受影响。
        ``time_from`` / ``time_to``：时间列同单位的 epoch 边界（含端点）。
        """
        spec = self._list_subpage_spec(name)
        where, args = self._list_subpage_where(spec, filters, time_from, time_to)
        if name == "ledger":
            sql = f"SELECT * FROM {spec['table']}"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY ts ASC, ref ASC"
            return list(self.conn.execute(sql, args))
        if after is not None and before is None:
            where.append("id > ?")
            args.append(int(after))
            sql = f"SELECT * FROM {spec['table']} WHERE " + " AND ".join(where)
            rows = list(self.conn.execute(sql + " ORDER BY id ASC LIMIT ?",
                                          [*args, int(limit)]))
            rows.reverse()
            return rows
        if before is not None:
            where.append("id < ?")
            args.append(int(before))
        sql = f"SELECT * FROM {spec['table']}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        return list(self.conn.execute(sql, [*args, int(limit)]))

    @_locked
    def list_subpage_count(self, name: str, filters: Optional[Dict[str, Any]] = None,
                           time_from: Optional[float] = None, time_to: Optional[float] = None,
                           id_gte: Optional[int] = None, id_lte: Optional[int] = None) -> int:
        """同筛选条件下 COUNT（页眉「共 Z 条」与「第 X–Y 条」的序号都用它）。

        ``id_gte`` / ``id_lte``：算「某行在筛选结果里的最新序号」用 —— 序号 =
        count(id >= 该行 id)，这样页眉数字与行内容永远同源（不靠 Python 数行数）。
        """
        spec = self._list_subpage_spec(name)
        where, args = self._list_subpage_where(spec, filters, time_from, time_to)
        if id_gte is not None:
            where.append("id >= ?")
            args.append(int(id_gte))
        if id_lte is not None:
            where.append("id <= ?")
            args.append(int(id_lte))
        sql = f"SELECT count(*) AS c FROM {spec['table']}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        r = self.conn.execute(sql, args).fetchone()
        return int(r["c"]) if r else 0

    @_locked
    def list_subpage_options(self, name: str, key: str) -> List[str]:
        """筛选下拉框的选项：该列的去重非空值（升序）。key 必须在该表白名单里。"""
        spec = self._list_subpage_spec(name)
        if key not in spec["filters"]:
            raise ValueError(f"{name} 不支持筛选字段：{key!r}")
        rows = self.conn.execute(
            f"SELECT DISTINCT {key} AS v FROM {spec['table']} "
            f"WHERE {key} IS NOT NULL AND {key} <> '' ORDER BY {key}").fetchall()
        return [str(r["v"]) for r in rows]

    @_locked
    def prune_hl_telemetry(self, keep_days: float = 30.0, keep_events: int = 20000,
                           keep_snapshots: int = 20000) -> None:
        """清 hl_events / hl_snapshots（执行器心跳/对账每 5 分钟各写一行，
        live 下 ~864 行/天不清理会无限膨胀）。保留：
        - 心跳/对账类事件只留 keep_days 天（保留窗口内的告警/订单事件不删，
          按数量截断兜底）；快照同理。
        - 告警价值的事件（error/kill/decision/order）不按天删，只按总量截断。
        """
        import time as _time
        cutoff = _time.time() - keep_days * 86400
        self.conn.execute(
            "DELETE FROM hl_events WHERE created_at < ? AND kind IN ('heartbeat', 'reconcile')",
            (cutoff,))
        self.conn.execute(
            "DELETE FROM hl_events WHERE id < (SELECT MIN(id) FROM "
            "(SELECT id FROM hl_events ORDER BY id DESC LIMIT ?))", (keep_events,))
        self.conn.execute(
            "DELETE FROM hl_snapshots WHERE ts < ?", (cutoff,))
        self.conn.execute(
            "DELETE FROM hl_snapshots WHERE id < (SELECT MIN(id) FROM "
            "(SELECT id FROM hl_snapshots ORDER BY id DESC LIMIT ?))", (keep_snapshots,))
        self.conn.commit()

    @_locked
    def set_pos_hl(self, pid: int, **kw) -> None:
        allowed = ("hl_status", "hl_entry_oid", "hl_stop_oid", "hl_sz")
        sets, vals = [], []
        for k, v in kw.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(pid)
        self.conn.execute(f"UPDATE positions SET {', '.join(sets)} WHERE id=?", vals)
        self.conn.commit()

    @_locked
    def set_pos_pnl(self, pid: int, net_pnl: Optional[float], source: str) -> None:
        """写流水口径净额（PR-3）。`net_pnl=None` 是合法值（查不到成交），
        但那时 `source` 必须写明原因（no_fills），读侧据此区分「0 收益」与「不知道」。"""
        self.conn.execute(
            "UPDATE positions SET net_pnl=?, pnl_source=? WHERE id=?",
            (None if net_pnl is None else float(net_pnl), str(source or ""), int(pid)))
        self.conn.commit()

    # ---------------- kill switch（存 configuration 表，DB 优先）
    @_locked
    def hl_kill(self) -> bool:
        # 走 get_config_str（json.loads 解引号）+ 完整真值集合。旧版直接读原始 JSON
        # 文本不剥引号 —— 手工把 "1" 写进 DB 会被读成「未触发」，kill 失效比误触发危险。
        raw = str(self.get_config_str("hl_exec_kill", "0") or "0").strip().strip('"').strip("'").lower()
        return raw in ("1", "true", "yes", "on")

    @_locked
    def hl_kill_origin(self) -> str:
        """这次 kill 是**谁**拉起来的：manual / reconcile / daily_loss / size / expiry / auto。

        自动解除只认 `reconcile`（对账自己造成的），其它来源（人工、熔断、数量、
        到期）一律不动 —— 这是「窄口径」的唯一判据，读侧不许自己再推断。
        老库里没有这个键（升级前触发的 kill）→ 返回空串，等价于「来源不明 = 人工」。
        """
        return str(self.get_config_str("hl_exec_kill_origin", "") or "").strip()

    @_locked
    def hl_set_kill(self, on: bool, reason: str = "", origin: str = "manual") -> None:
        """置/清 kill。``origin`` 必须由调用点显式给出（见各调用点注释）。

        origin 只做**归因**，不改变 kill 的语义：它唯一的用途是让
        「对账异常已自动消化」那条窄口径能确认「这个 kill 是我自己造成的」，
        从而允许连续 N 轮干净后自动解除（见 Executor._run_locked）。
        默认 "manual"：新增调用点忘了传时段成人工 kill —— 偏保守（永不自解），
        不会出现「谁都能自解」的宽松默认。
        """
        self.set_config("hl_exec_kill", 1 if on else 0)
        self.set_config("hl_exec_kill_origin", str(origin or "manual"))
        if reason:
            self.set_config("hl_exec_kill_reason", reason)
        elif not on:
            # 解除 kill 时把原因一并清空（对抗审查 L6）：以前只写不清，解除之后
            # `hl_exec_kill_reason` 还留着上一次的原因，面板/看守/告警正文都会拿它
            # 去解释一个**已经不存在**的停手（「未暂停自动交易（对账异常）」这种自相矛盾）。
            self.set_config("hl_exec_kill_reason", "")
