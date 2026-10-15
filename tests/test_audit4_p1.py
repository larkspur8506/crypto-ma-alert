"""第四轮审计 P1/P2 回归测试（2026-10-15，独立复核发现后修）。

覆盖：
P1-a  pending 行**没有真的发过入场单**时不得当真实仓位对账
      （否则下一轮对账报「本地有仓但交易所无仓位」→ 误置 kill + 误标 mismatch）
P1-b  pending 行**已发出入场单**（成交后崩溃）仍要当真实仓位（保留第三轮 M4 的本意）
P1-c  on_entry 早期失败的文案不得再写「下一轮 tick 会重试」（引擎没有重试路径）
P2-a  平仓 IOC 部分成交 → exit 行必须落成 filled（否则永久停在 sent，看护当卡单告警）
P2-b  同一 4h bar 内二次离场尝试 → cloid 必须与第一次不同（HL 拒重复 cloid，
      而重试第一步已撤掉止损 → 撞号 = 裸仓）

只依赖相对路径（CI 容器无 /opt/data）；不 import app.main（模块级建 DB）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB
from app.hl import MODE_LIVE, PURPOSE_ENTRY, ST_FILLED, ST_RESTING, ST_SENT, ST_SKIPPED
from app.hl.executor import Executor, ExecParams
from app.hl.exchange import STATUS_FILLED, STATUS_RESTING

BAR = 1791912000000


def _snap(equity=1000.0, positions=(), orders=(), trusted=True):
    return {"ts": time.time(), "address": "0xabc", "equity": equity, "equity_total": equity,
            "withdrawable": equity, "spot_ok": True, "margin_used": 10.0, "notional": 0.0,
            "positions": list(positions), "open_orders": list(orders),
            "orders_trusted": trusted}


def _pos(coin="BTC", size=0.005, entry=81621.0, side=-1, upnl=-1.0):
    return {"coin": coin, "side": side, "size": size, "entry_px": entry, "mark_px": entry + 50,
            "position_value": size * entry, "liq_px": 0.0, "leverage": 5, "isolated": True,
            "margin_used": size * entry / 5, "unrealized_pnl": upnl, "roe": 0.0}


class FakeInfo:
    def __init__(self):
        self._snap = _snap()

    def snapshot(self, address=None):
        return self._snap

    def specs(self, coins):
        return {c: {"sz_decimals": 5, "max_leverage": 40} for c in coins}

    def user_fills(self, address=None):
        return []

    def agent_status(self, address=None):
        return {"agents": [{"name": "t", "address": "0x1"}], "max_valid_until": int(
            (time.time() + 180 * 86400) * 1000)}

    def mid(self, coin):
        return 83000.0

    def mids(self):
        return {"BTC": 83000.0}


class FakeExchange:
    def __init__(self):
        self.calls = []
        self.entry = None
        self.exit = None
        self.stop = None

    def mid(self, coin):
        return 83000.0

    def slippage_limit(self, coin, is_buy, mid=None):
        px = mid if mid else self.mid(coin)
        return px * (1 + 0.003) if is_buy else px * (1 - 0.003)

    def _cloid(self, symbol, bar_ms, purpose):
        from app.hl.exchange import make_cloid
        return make_cloid(symbol, bar_ms, purpose)

    def place_order(self, coin, is_buy, sz, limit_px, reduce_only=False, tif="Ioc",
                    trigger_px=None, cloid=None):
        self.calls.append({"coin": coin, "is_buy": is_buy, "sz": sz, "px": limit_px,
                           "reduce_only": reduce_only, "trigger_px": trigger_px,
                           "cloid": str(cloid)})
        res = self.stop if trigger_px is not None else (self.exit if reduce_only else self.entry)
        if res is None:
            return {"ok": True, "status": STATUS_FILLED, "oid": 1, "avg_px": limit_px,
                    "filled_sz": sz, "error": None, "raw": {}}
        return res

    def cancel(self, coin, oid):
        self.calls.append({"cancel": oid})
        return {"ok": True, "status": STATUS_RESTING, "oid": oid, "error": None,
                "avg_px": None, "filled_sz": None, "raw": None}

    def update_leverage(self, coin, leverage, cross=False):
        return {"ok": True, "status": STATUS_RESTING, "error": None, "oid": None,
                "avg_px": None, "filled_sz": None, "raw": None}


class Notifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


class Base(unittest.TestCase):
    mode = MODE_LIVE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = Notifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=self.mode, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, equity_min=20.0, risk_pct=0.02, leverage=5.0,
                            code_rev="audit4")

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self):
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def add_pending(self, symbol="BTC_USDT", side=-1):
        """引擎建行（status 默认 pending、hl_status NULL），尚未跑 on_entry。"""
        return self.db.create_position(symbol=symbol, signal_bar_ms=BAR, side=side,
                                       entry_price=81603.0, stop_price=83945.44,
                                       stop_dist=0.0287, contracts=1, coin_qty=0.005,
                                       notional=408.0, risk_u=20.0, equity_at_entry=1000.0)

    def open_pos(self, symbol="BTC_USDT", side=-1, sz=0.005):
        pid = self.add_pending(symbol=symbol, side=side)
        self.db.set_pos_hl(pid, hl_status="open", hl_entry_oid=1, hl_stop_oid=99, hl_sz=sz)
        self.db.confirm_position(pid, 81621.0, sz, 83945.44, True)
        self.db.hl_record_intent(symbol, "BTC", BAR, "stop", side, sz, px=83945.44,
                                 reduce_only=True, status=ST_RESTING)
        return pid


class TestP1PendingNotRealPosition(Base):
    """P1-a：没发过入场单的 pending 行不是仓位。"""

    def test_no_entry_record(self):
        pid = self.add_pending()
        ex = self.executor()
        rec = ex.reconcile(_snap())
        self.assertTrue(rec["ok"], f"不该报对账问题：{rec['problems']}")
        self.assertEqual(rec["problems"], [])
        self.assertIsNone(self.db.position_by_id(pid)["hl_status"], "不该被标 mismatch")

    def test_skipped_entry_record(self):
        """live 下 on_entry 读不到快照 → entry 行记 skipped —— 这是复现里那条路径。"""
        pid = self.add_pending()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_ENTRY, -1, 0.0,
                                 status=ST_SKIPPED)
        ex = self.executor()
        rec = ex.reconcile(_snap())
        self.assertTrue(rec["ok"], f"skipped 的 pending 不该算仓位：{rec['problems']}")

    def test_run_does_not_kill(self):
        """整轮 run()（含 kill 判定）不得因这种 pending 行而自锁。"""
        self.add_pending()
        ex = self.executor()
        ex._snapshot = lambda force=False: _snap()
        alerts = ex.run(force_snapshot=True)
        self.assertFalse(self.db.hl_kill(), f"误触发 kill switch，告警={alerts}")
        self.assertEqual([a for a in alerts if "对账异常" in a], [])

    def test_issued_entry_still_checked(self):
        """P1-b：已发出（sent/filled）的入场单 + pending 行 → 仍按真实仓位对账。"""
        pid = self.add_pending()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_ENTRY, -1, 0.005,
                                 px=81603.0, status=ST_SENT)
        ex = self.executor()
        rec = ex.reconcile(_snap())                       # 交易所已经什么都没有
        self.assertFalse(rec["ok"], "成交后崩溃的裸仓必须仍被报出来")
        self.assertIn("本地有仓", rec["problems"][0])
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "mismatch")


class TestP1AlertWording(Base):
    """P1-c：早期失败文案不得承诺不存在的重试。"""

    def test_no_false_retry_promise(self):
        pid = self.add_pending()
        ex = self.executor()
        ex._snapshot = lambda force=False: None          # 快照读不到 → 走早期失败分支
        self.assertTrue(ex.on_entry(pid))
        joined = "\n".join(self.notifier.messages)
        self.assertIn("未下单", joined)
        self.assertNotIn("下一轮 tick 会重试", joined)
        self.assertIn("不会自动重试", joined)


class TestP2PartialFillExitRow(Base):
    """P2-a：部分成交后 exit 行必须落成 filled（不再算卡单）。"""

    def test_exit_row_marked_filled(self):
        pid = self.open_pos(sz=0.005)
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": 777, "avg_px": 83000.0,
                        "filled_sz": 0.0025, "error": None, "raw": {}}
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": 778, "avg_px": None,
                        "filled_sz": None, "error": None, "raw": {}}
        ex = self.executor()
        ex._snapshot = lambda force=False: _snap(positions=[_pos()])
        exit_bar = ex._current_closed_bar_ms("BTC_USDT")          # 与执行器同源（无 bar_state → 4h 网格）
        self.assertTrue(ex.on_exit(pid, "ma20", 83000.0))
        row = self.db.hl_order("BTC_USDT", exit_bar, "exit")
        self.assertIsNotNone(row, "exit 行不见了")
        self.assertEqual(row["status"], ST_FILLED, "部分成交后 exit 行必须落成 filled")
        self.assertAlmostEqual(float(row["filled_sz"] or 0), 0.0025, places=8)
        stuck = [r for r in self.db.hl_stuck_orders(older_than_s=0)
                 if r["purpose"] == "exit"]
        self.assertEqual(stuck, [], f"exit 不该还算卡单：{[dict(r) for r in stuck]}")
        # 本地仍持仓（剩余量在交易所）
        self.assertEqual(self.db.position_by_id(pid)["status"], "open")


class TestP2RetryCloid(Base):
    """P2-b：同一离场 bar 内二次尝试的 cloid 必须不同。"""

    def test_second_attempt_new_cloid(self):
        pid = self.open_pos(sz=0.005)
        # 第一次：IOC 挂出未成（resting）→ 撤单 + 重挂止损
        self.ex.exit = {"ok": True, "status": STATUS_RESTING, "oid": 900, "avg_px": None,
                        "filled_sz": None, "error": None, "raw": {}}
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": 901, "avg_px": None,
                        "filled_sz": None, "error": None, "raw": {}}
        ex = self.executor()
        ex._snapshot = lambda force=False: _snap(positions=[_pos()])
        self.assertTrue(ex.on_exit(pid, "ma20", 83000.0))
        first = [c["cloid"] for c in self.ex.calls
                 if "cloid" in c and c.get("reduce_only") and c.get("trigger_px") is None]
        self.assertTrue(first, "第一次离场没有发单记录")
        bar = ex._current_closed_bar_ms("BTC_USDT") or BAR
        db_cloid_1 = self.db.hl_order("BTC_USDT", bar, "exit")["cloid"]
        self.assertEqual(db_cloid_1, first[0], "DB 记录的 cloid 必须与发给交易所的一致")

        time.sleep(1.05)                                  # 尝试时刻不同（真实场景隔 30 分钟）
        self.ex.calls.clear()
        self.assertTrue(ex.on_exit(pid, "ma20", 83000.0))  # 同 bar 第二次尝试
        second = [c["cloid"] for c in self.ex.calls
                  if "cloid" in c and c.get("reduce_only") and c.get("trigger_px") is None]
        self.assertTrue(second, "第二次离场没有发单记录")
        self.assertNotEqual(first[0], second[0],
                            "同一 bar 内二次离场复用了 cloid → HL 会拒单，而止损已被撤 = 裸仓")


if __name__ == "__main__":
    unittest.main()
