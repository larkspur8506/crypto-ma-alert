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
import time
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
"""


def now() -> float:
    return time.time()


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
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

    # ---------------- configuration
    def get_config(self) -> Dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM configuration")}

    def set_config(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO configuration(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), now()))
        self.conn.commit()

    # ---------------- bar state
    def get_last_bar(self, symbol: str) -> Optional[int]:
        r = self.conn.execute("SELECT last_bar_ms FROM bar_state WHERE symbol=?", (symbol,)).fetchone()
        return int(r["last_bar_ms"]) if r else None

    def set_last_bar(self, symbol: str, bar_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO bar_state(symbol, last_bar_ms, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET last_bar_ms=excluded.last_bar_ms, updated_at=excluded.updated_at",
            (symbol, bar_ms, now()))
        self.conn.commit()

    # ---------------- positions
    def open_position(self, symbol: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM positions WHERE symbol=? AND status IN ('pending','open') "
            "ORDER BY signal_bar_ms DESC LIMIT 1", (symbol,)).fetchone()

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

    def position_by_id(self, pid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()

    def confirm_position(self, pid: int, actual_entry: float, actual_contracts: int,
                         actual_stop: float, stop_placed: bool) -> None:
        self.conn.execute(
            "UPDATE positions SET status='open', actual_entry=?, actual_contracts=?, actual_stop=?, "
            "stop_placed=?, opened_at=? WHERE id=?",
            (actual_entry, actual_contracts, actual_stop, 1 if stop_placed else 0, now(), pid))
        self.conn.commit()

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

    def ignore_position(self, pid: int) -> None:
        self.conn.execute("UPDATE positions SET status='ignored', closed_at=? WHERE id=?", (now(), pid))
        self.conn.commit()

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

    def recent_positions(self, limit: int = 20) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM positions ORDER BY id DESC LIMIT ?", (limit,)))

    def closed_positions(self, since: float = 0.0) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM positions WHERE status='closed' AND closed_at>=? ORDER BY closed_at", (since,)))

    # ---------------- alert logs（幂等 + 成功后标记）
    def was_alerted(self, symbol: Optional[str], bar_ms: Optional[int], kind: str) -> bool:
        r = self.conn.execute(
            "SELECT 1 FROM alert_logs WHERE symbol IS ? AND bar_ms IS ? AND kind=? AND sent_successfully=1",
            (symbol, bar_ms, kind)).fetchone()
        return bool(r)

    def log_alert(self, symbol: Optional[str], bar_ms: Optional[int], kind: str, message: str,
                  success: bool) -> None:
        self.conn.execute(
            "INSERT INTO alert_logs(symbol, bar_ms, kind, message, sent_successfully, attempts, created_at, sent_at) "
            "VALUES(?,?,?,?,?,1,?,?) "
            "ON CONFLICT(symbol, bar_ms, kind) DO UPDATE SET message=excluded.message, "
            "sent_successfully=excluded.sent_successfully, attempts=alert_logs.attempts+1, sent_at=excluded.sent_at",
            (symbol, bar_ms, kind, message, 1 if success else 0, now(), now() if success else None))
        self.conn.commit()

    def recent_alerts(self, limit: int = 20) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM alert_logs ORDER BY id DESC LIMIT ?", (limit,)))

    def failed_alerts_since(self, since: float) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM alert_logs WHERE sent_successfully=0 AND created_at>=? ORDER BY id DESC", (since,)))

    # ---------------- feed health
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

    def feed_status(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM feed_health ORDER BY symbol"))
