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
    actual_contracts INTEGER,
    actual_stop REAL,
    stop_placed INTEGER DEFAULT 0,
    exit_price REAL,
    exit_reason TEXT,                  -- stop | ma20 | manual
    auto_closed INTEGER DEFAULT 0,     -- 1 = 系统按参考价自动关（用户可覆盖真实成交价）；0 = 用户已确认
    r_multiple REAL,
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
    fee REAL, fee_token TEXT, dir TEXT, closed_pnl REAL, hash TEXT,
    reduce_only INTEGER DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_hl_snap_ts ON hl_snapshots(ts);
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
        return cur.lastrowid

    @_locked
    def position_by_id(self, pid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()

    @_locked
    def confirm_position(self, pid: int, actual_entry: float, actual_contracts: int,
                         actual_stop: float, stop_placed: bool) -> None:
        self.conn.execute(
            "UPDATE positions SET status='open', actual_entry=?, actual_contracts=?, actual_stop=?, "
            "stop_placed=?, opened_at=? WHERE id=?",
            (actual_entry, actual_contracts, actual_stop, 1 if stop_placed else 0, now(), pid))
        self.conn.commit()

    @_locked
    def update_fill(self, pid: int, actual_entry: float, actual_contracts: int,
                    actual_stop: float, stop_placed: bool) -> None:
        """修改已确认的开仓数据（用户发现录错）。仓位已平仓时按新数据重算 R 倍数。"""
        p = self.position_by_id(pid)
        if not p:
            return
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
    def was_alerted(self, symbol: Optional[str], bar_ms: Optional[int], kind: str) -> bool:
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

    # ---------------- HL 执行器（feat/hl-executor）
    @_locked
    def hl_record_intent(self, symbol: str, coin: str, bar_ms: int, purpose: str, side: int,
                         sz: float, px: Optional[float] = None, reduce_only: bool = False,
                         cloid: Optional[str] = None, status: str = "intent") -> int:
        """登记一次下单意图（幂等键 symbol+bar_ms+purpose）。已存在则返回原 id（不覆盖）。"""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO hl_orders(symbol, coin, bar_ms, purpose, side, sz, px, reduce_only,"
            " status, cloid, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, coin, bar_ms, purpose, side, float(sz), px, 1 if reduce_only else 0,
             status, cloid, now(), now()))
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
        """落一笔成交（按交易所 tid 去重）。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO hl_fills(tid, symbol, coin, side, sz, px, fee, fee_token, dir,"
            " closed_pnl, hash, reduce_only, ts, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(kw.get("tid")), kw.get("symbol"), kw.get("coin"), kw.get("side"), kw.get("sz"),
             kw.get("px"), kw.get("fee"), kw.get("fee_token"), kw.get("dir"),
             kw.get("closed_pnl"), kw.get("hash"), 1 if kw.get("reduce_only") else 0,
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

    # ---------------- kill switch（存 configuration 表，DB 优先）
    @_locked
    def hl_kill(self) -> bool:
        return str(self.get_config().get("hl_exec_kill", "0")).strip() in ("1", "true", "True")

    @_locked
    def hl_set_kill(self, on: bool, reason: str = "") -> None:
        self.set_config("hl_exec_kill", 1 if on else 0)
        if reason:
            self.set_config("hl_exec_kill_reason", reason)
