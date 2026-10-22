"""第五轮审计：交易所侧已平仓 → 对账按成交价关账、不误 kill（2026-10-22 事故回归）。

事故：BTC 多单的交易所挂单止损被触发成交，但本地账本只同步了 hl_fills、没关账；
下一轮对账看到「本地有仓、交易所无仓」就判 mismatch 并自动 kill，暂停自动交易。

修复口径（见 app/hl/executor.py:_fill_close_proof）：
- 对账在判 mismatch **之前**，先用本轮已同步的 hl_fills 自证「这仓已被交易所平掉」
  （开仓后、reduce_only/Close 腿、数量足额 −1 tick）；证据成立 → 按加权成交均价关账、
  挂单落 filled、推一条提醒，且**不** kill；
- 任一条件不满足 → 完全退回旧行为（mismatch + kill），即 fail-closed。

铁律：只用相对路径（CI 容器里没有部署机的绝对路径）；不 import app.main（模块级建库）。
假实现复用 tests/test_audit3_p1.py 的 FakeInfo / FakeExchange / SilentNotifier / _snap。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import DB
from app.hl import MODE_LIVE
from app.hl.executor import Executor, ExecParams
from app.hl.info import HlInfoError
from test_audit3_p1 import FakeExchange, FakeInfo, SilentNotifier, _pos, _snap, _stop_order

BAR = 1792612800000            # 2026-10-21 16:00Z 那根
STOP_OID = 581840873520        # 交易所挂单止损单号（事故里被触发的那张）
OTHER_OID = 581840859768       # 另一张单（入场单 / 人工单）—— 用来区分 stop vs exchange
QTY = 0.00638                  # 本地持仓量（hl_sz）
ENTRY = 84479.0
STOP = 82386.33333333333
FILL_PX = 82375.0              # 止损实际成交价（两笔均价）


def _fills(oids=(STOP_OID, STOP_OID), szs=(0.0025, 0.00388), px=FILL_PX,
           age_s=60, coin="BTC", dir_word="Close Long"):
    """造交易所成交：默认 = 事故原样（两笔 Close Long，合计 0.00638，oid 都是止损单）。"""
    now_ms = int(time.time() * 1000)
    out = []
    for i, (oid, sz) in enumerate(zip(oids, szs)):
        out.append({"coin": coin, "oid": oid, "dir": dir_word, "sz": sz, "px": px,
                    "time": now_ms - age_s * 1000, "tid": 419136180065962 + i,
                    "closedPnl": -5.26, "fee": 0.092671, "feeToken": "USDC", "hash": "0xaaa"})
    return out


class FillCloseBase(unittest.TestCase):
    """一套真实 DB + 假交易所。默认造景 = 事故现场（本地有仓、交易所无仓、止损已成交）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = SilentNotifier()
        self.info = FakeInfo(snap=_snap(equity=987.3, positions=[], orders=[]))
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, equity_min=20.0, risk_pct=0.0133, leverage=5.0,
                            code_rev="audit5")
        self.pid = self.open_position()

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------- 造景
    def open_position(self) -> int:
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=BAR, status="open", side=1,
                                      entry_price=84455.0, stop_price=STOP, stop_dist=0.0244943,
                                      contracts=64, coin_qty=0.0064, notional=540.5,
                                      risk_u=13.2395, equity_at_entry=1000.0)
        self.db.confirm_position(pid, actual_entry=ENTRY, actual_contracts=QTY,
                                 actual_stop=STOP, stop_placed=True)
        self.db.set_pos_hl(pid, hl_status="open", hl_entry_oid=OTHER_OID,
                           hl_stop_oid=STOP_OID, hl_sz=QTY)
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, "entry", 1, QTY, px=84470.5,
                                 status="filled")
        self.db.hl_update_order("BTC_USDT", BAR, "entry", oid=OTHER_OID, filled_sz=QTY,
                                avg_px=ENTRY)
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, "stop", 1, QTY, px=STOP,
                                 reduce_only=True, status="resting")
        self.db.hl_update_order("BTC_USDT", BAR, "stop", oid=STOP_OID)
        return pid

    def executor(self, **kw) -> Executor:
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    # ---------------- 断言助手
    def alerts_text(self) -> str:
        return "\n".join(self.notifier.messages)

    def row(self):
        return self.db.position_by_id(self.pid)

    def stop_row(self):
        return self.db.hl_order("BTC_USDT", BAR, "stop")

    def reconcile_events(self) -> list:
        return [r["message"] for r in self.db.hl_recent_events(50, kind="reconcile")]


class TestStopFillClosesLocal(FillCloseBase):
    """1. 足额成交 + oid = 止损单 → 按成交价关账，不 kill，stop 行落 filled。"""

    def test_closes_at_fill_price_without_kill(self):
        self.info._fills = _fills()
        ex = self.executor()
        alerts = ex.run()

        row = self.row()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["hl_status"], "closed")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertAlmostEqual(row["exit_price"], FILL_PX, places=4)
        # R = (82375 − 84479) / |84479 − 82386.33| ≈ −1.005 —— 用真实价差算，不是拍 0
        self.assertAlmostEqual(row["r_multiple"], (FILL_PX - ENTRY) / abs(ENTRY - STOP), places=6)

        self.assertFalse(self.db.hl_kill(), "止损已成交不能触发 kill")
        self.assertNotIn("对账异常", self.alerts_text())
        self.assertEqual([m for m in self.reconcile_events() if "对账异常" in m], [])

        stop = self.stop_row()
        self.assertEqual(stop["status"], "filled")
        self.assertAlmostEqual(stop["filled_sz"], QTY, places=8)
        self.assertAlmostEqual(stop["avg_px"], FILL_PX, places=4)

        stop_alerts = [m for m in alerts if "🛑 止损已在交易所成交" in m]
        self.assertEqual(len(stop_alerts), 1, alerts)
        self.assertIn("未暂停自动交易", stop_alerts[0])
        self.assertIn("BTC 做多", stop_alerts[0])

    def test_fill_oid_persisted(self):
        """证据链前提：hl_fills 必须记住 oid（老库缺这一列就永远判不出 stop）。"""
        self.info._fills = _fills()
        self.executor().run()
        fills = self.db.hl_recent_fills(10)
        self.assertEqual(len(fills), 2)
        self.assertEqual({int(f["oid"]) for f in fills}, {STOP_OID})
        self.assertEqual({int(f["reduce_only"]) for f in fills}, {1})


class TestExternalClose(FillCloseBase):
    """2. 足额成交但 oid ≠ 止损单 → 人工/外部平仓，reason=exchange。"""

    def test_marks_exchange_reason(self):
        self.info._fills = _fills(oids=(OTHER_OID, OTHER_OID))
        ex = self.executor()
        alerts = ex.run()

        row = self.row()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["exit_reason"], "exchange")
        self.assertAlmostEqual(row["exit_price"], FILL_PX, places=4)
        self.assertFalse(self.db.hl_kill())
        self.assertNotIn("对账异常", self.alerts_text())
        info_alerts = [m for m in alerts if "ℹ️ BTC 已在交易所被平仓" in m]
        self.assertEqual(len(info_alerts), 1, alerts)
        # 不是止损单打的 → 不能把止损行标成成交
        self.assertNotEqual(self.stop_row()["status"], "filled")


class TestInsufficientEvidence(FillCloseBase):
    """3/4. 证据不足（部分成交 / 完全没有成交）→ 严格退回旧行为：mismatch + kill。"""

    def _assert_old_behaviour(self):
        row = self.row()
        self.assertTrue(self.db.hl_kill(), "证据不足时必须 fail-closed（kill）")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertIn("本地有仓", self.alerts_text())
        self.assertEqual(self.stop_row()["status"], "resting")

    def test_partial_fills_do_not_close(self):
        # 只平了 0.0045 < 0.00638 − 1 tick：仓位还在，不能按平仓价关掉整仓
        self.info._fills = _fills(szs=(0.0025, 0.0020))
        self.executor().run()
        self._assert_old_behaviour()

    def test_no_closing_fills(self):
        self.info._fills = []
        self.executor().run()
        self._assert_old_behaviour()

    def test_only_open_leg_fill(self):
        """只有开仓腿成交（dir 不含 Close）→ 不能拿它当平仓证据凑数量。"""
        self.info._fills = _fills(oids=(OTHER_OID, OTHER_OID), dir_word="Open Long")
        self.executor().run()
        self._assert_old_behaviour()


class TestExchangeStillHoldsPosition(FillCloseBase):
    """5. 交易所仍有该 coin 仓位 → 不走新分支（仓位不动、绝不误关账）。"""

    def test_no_close_when_exchange_has_position(self):
        self.info._fills = _fills()
        self.info._snap = _snap(equity=987.3, positions=[_pos(coin="BTC", size=QTY,
                                                              entry=ENTRY)],
                                orders=[_stop_order(coin="BTC", trigger=STOP)])
        alerts = self.executor().run()

        row = self.row()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["hl_status"], "open")
        self.assertIsNone(row["exit_price"])
        self.assertFalse(self.db.hl_kill())
        self.assertNotIn("交易所成交", "\n".join(alerts))
        self.assertEqual(self.stop_row()["status"], "resting")


class FlakyFillsInfo(FakeInfo):
    """user_fills 抛错：模拟读接口抖动。"""

    def user_fills(self, address):
        raise HlInfoError("userFills 不可用")


class TestFillsReadFailure(FillCloseBase):
    """6. user_fills 抛 HlInfoError → 不崩、不关账、走原 kill 路径。"""

    def test_info_error_falls_back(self):
        self.info = FlakyFillsInfo(snap=_snap(equity=987.3, positions=[], orders=[]))
        ex = self.executor()
        ex.run()                              # 不抛异常

        row = self.row()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.stop_row()["status"], "resting")


class TestSwitchOff(FillCloseBase):
    """7. 开关 fill_close=False → 即使证据齐全也走旧行为。"""

    def test_disabled_keeps_old_behaviour(self):
        self.info._fills = _fills()
        self.executor(fill_close=False).run()

        row = self.row()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.stop_row()["status"], "resting")
        self.assertIn("本地有仓", self.alerts_text())


class TestIdempotent(FillCloseBase):
    """8. 幂等：第一轮关账，第二轮无问题、不重复推送、仓位仍 closed。"""

    def test_second_round_is_quiet(self):
        self.info._fills = _fills()
        ex = self.executor()
        first = ex.run()
        self.assertEqual(len([m for m in first if "🛑" in m]), 1)
        sent = len(self.notifier.messages)

        second = ex.run()
        self.assertEqual([m for m in second if "🛑" in m or "ℹ️" in m], [],
                         f"第二轮不该重复提醒：{second}")
        self.assertEqual(len(self.notifier.messages), sent)
        row = self.row()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertFalse(self.db.hl_kill())


class TestSyncBeforeReconcile(FillCloseBase):
    """顺序回归：对账跑的时候，本轮成交必须已经在 hl_fills 里（否则证明永远拿不到）。"""

    def test_fills_visible_during_reconcile(self):
        self.info._fills = _fills()
        ex = self.executor()
        seen = {}
        orig = ex._fill_close_proof

        def spy(row, snap=None):
            seen["fills_in_db"] = len(self.db.hl_recent_fills(50))
            return orig(row, snap)

        ex._fill_close_proof = spy            # 实例属性覆盖绑定方法，不碰类
        ex.run()

        self.assertEqual(seen.get("fills_in_db"), 2,
                         "reconcile 之前必须先同步成交（2026-10-22 事故根因）")
        self.assertEqual(self.row()["status"], "closed")


if __name__ == "__main__":
    unittest.main()
