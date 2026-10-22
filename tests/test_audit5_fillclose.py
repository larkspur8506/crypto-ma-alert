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

        def spy(row, snap=None, consumed=None):
            seen["fills_in_db"] = len(self.db.hl_recent_fills(50))
            return orig(row, snap, consumed)

        ex._fill_close_proof = spy            # 实例属性覆盖绑定方法，不碰类
        ex.run()

        self.assertEqual(seen.get("fills_in_db"), 2,
                         "reconcile 之前必须先同步成交（2026-10-22 事故根因）")
        self.assertEqual(self.row()["status"], "closed")


class TestOverfillRefused(FillCloseBase):
    """A（对抗自查）：平仓腿合计**超过**持仓量 → 拒绝证明，退回 mismatch + kill。

    造景：同一窗口两笔 Close，各 QTY、价差巨大（82375 / 70000）。若照旧关账，仓位会被
    关在 76,187.5 这个「混合均价」上 —— 既不是任何真实成交价，还会把上一仓的成交算进
    本仓，凭空造一笔巨亏。正确行为：账不可归因 → 不动账、kill 让人看。
    """

    def overfill(self):
        now_ms = int(time.time() * 1000)
        base = {"coin": "BTC", "oid": STOP_OID, "dir": "Close Long", "time": now_ms - 60_000,
                "closedPnl": -1.0, "fee": 0.01, "feeToken": "USDC", "hash": "0xbbb"}
        return [dict(base, sz=QTY, px=82375.0, tid=900001),
                dict(base, sz=QTY, px=70000.0, tid=900002)]

    def test_mixed_overfill_is_not_booked(self):
        self.info._fills = self.overfill()
        ex = self.executor()
        alerts = ex.run()

        row = self.row()
        self.assertEqual(row["status"], "open", "超额成交不能拿来关账")
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertIsNone(row["exit_price"], "不许写混合均价")
        self.assertIsNone(row["exit_reason"])
        self.assertTrue(self.db.hl_kill(), "账不可归因必须 fail-closed（kill）")
        self.assertIn("本地有仓", self.alerts_text())
        self.assertNotIn("已在交易所被平仓", "\n".join(alerts))
        # 拒绝归拒绝，止损单也不能被顺手撤掉（仓还开着，撤了就是裸仓）
        self.assertEqual(self.stop_row()["status"], "resting")
        self.assertEqual([c for c in self.ex.calls if c[0] == "cancel"], [])


class NoCancelExchange(FakeExchange):
    """撤单必失败：模拟接口抖动，或那张单其实已经不在场上。"""

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        return {"ok": False, "status": None, "oid": oid,
                "error": "Order was never placed, already canceled, or filled.",
                "avg_px": None, "filled_sz": None, "raw": None}


class TestResidualStopCancelled(FillCloseBase):
    """B（对抗自查）：人工/外部平仓后，交易所侧残留的 reduce-only 止损必须撤掉。

    不撤的后果：仓没了、触发单还在，下一次同 coin 开仓会被它平掉。
    """

    def test_exchange_close_cancels_residual_stop(self):
        self.info._fills = _fills(oids=(OTHER_OID, OTHER_OID))
        ex = self.executor()
        ex.run()

        row = self.row()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["exit_reason"], "exchange")
        self.assertEqual(self.stop_row()["status"], "cancelled", "残留止损必须落终态")
        self.assertEqual([c for c in self.ex.calls if c[0] == "cancel"],
                         [("cancel", "BTC", STOP_OID)])
        self.assertNotIn("撤不掉", self.alerts_text())

        # 幂等：第二轮不再发撤单（不是每轮一次的交易所调用）
        ex.run()
        self.assertEqual(len([c for c in self.ex.calls if c[0] == "cancel"]), 1)

    def test_stop_fill_path_does_not_cancel(self):
        """reason='stop'：那张单本身已成交，绝不能再去撤。"""
        self.info._fills = _fills()
        self.executor().run()
        self.assertEqual(self.row()["exit_reason"], "stop")
        self.assertEqual([c for c in self.ex.calls if c[0] == "cancel"], [])


class TestResidualStopCancelFails(FillCloseBase):
    """B 失败分支：撤不掉 → 行保持 resting + EV_ERROR + 一条告警（不静默）。"""

    def test_failed_cancel_keeps_row_and_alerts(self):
        self.info._fills = _fills(oids=(OTHER_OID, OTHER_OID))
        self.ex = NoCancelExchange()
        ex = self.executor()
        ex.run()

        self.assertEqual(self.row()["status"], "closed")     # 关账本身仍然完成
        self.assertEqual(self.stop_row()["status"], "resting",
                         "撤单失败不能把行标成 cancelled（会骗过人工核对）")
        self.assertIn("cancel", [c[0] for c in self.ex.calls])

        text = self.alerts_text()
        self.assertIn("撤不掉", text)
        self.assertIn(str(STOP_OID), text)

        errs = [r["message"] for r in self.db.hl_recent_events(50, kind="error")]
        self.assertTrue(any("撤销失败" in m for m in errs), errs)


class TestMismatchStopExit(FillCloseBase):
    """事故第二入口（2026-10-22 16:03 线上）：对账已把仓打成 mismatch 后，4h 收盘的
    止损离场事件进来 —— 旧守卫直接让出接管权，引擎回落人工流并发「到 MEXC 核对」。

    新口径：mismatch + reason='stop' 放行，但**只**走「交易所已无仓 → 按真实成交价关账」
    这一条；条件不成立就让出接管权（return False），绝不下单。
    """

    def _mark_mismatch(self):
        self.db.set_pos_hl(self.pid, hl_status="mismatch")

    def test_mismatch_stop_closes_at_fill_price(self):
        self._mark_mismatch()
        self.info._fills = _fills()
        ex = self.executor()

        took_over = ex.on_exit(self.pid, "stop", STOP)

        self.assertTrue(took_over, "止损已成交，执行器必须接管关账，不能回落人工流")
        row = self.row()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["hl_status"], "closed")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertAlmostEqual(row["exit_price"], FILL_PX, places=4)   # 按真实成交价，不是参考价
        text = self.alerts_text()
        self.assertIn("🛑 止损离场", text)
        self.assertIn("本地已关账", text)
        self.assertNotIn("MEXC", text, "HL 仓已平，不能再给 MEXC 人工流指引")
        self.assertNotIn("order", [c[0] for c in self.ex.calls], "不该下任何单")

    def test_mismatch_stop_with_exchange_position_hands_off(self):
        self._mark_mismatch()
        self.info._snap = _snap(equity=987.3, positions=[_pos(coin="BTC", size=QTY,
                                                              entry=ENTRY)],
                                orders=[_stop_order(coin="BTC", trigger=STOP)])
        ex = self.executor()

        took_over = ex.on_exit(self.pid, "stop", STOP)

        self.assertFalse(took_over, "交易所仍有仓时 mismatch 行必须让出接管权")
        row = self.row()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertIsNone(row["exit_price"])
        # 既不能撤单也不能下单（拿错账去动交易所）
        self.assertEqual(self.ex.calls, [])

    def test_mismatch_stop_without_fill_price_hands_off(self):
        """mismatch + 交易所无仓但读不到成交价 → 不猜参考价关账，让出接管权。"""
        self._mark_mismatch()
        self.info._fills = []
        ex = self.executor()

        self.assertFalse(ex.on_exit(self.pid, "stop", STOP))
        row = self.row()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["hl_status"], "mismatch")

    def test_mismatch_non_stop_reason_still_hands_off(self):
        """放行只针对 stop：其它离场原因碰上 mismatch 一律照旧让出接管权。"""
        self._mark_mismatch()
        self.info._fills = _fills()
        ex = self.executor()

        self.assertFalse(ex.on_exit(self.pid, "ma20", ENTRY))
        row = self.row()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertEqual(self.ex.calls, [])


class TestM1NoOidDoesNotMarkAnyOrder(FillCloseBase):
    """M1（高危）：成交没有 oid 时，任何挂单行都不得被写成 filled。

    候选来自 ``hl_recent_orders(100)`` 的**全标的、跨 bar** 扫描 —— 「恰好一行候选」
    完全可能是别的仓/别的 bar 上那张**没成交**的止损。升级前已同步的成交 oid 恒为
    NULL（`hl_fills.tid` UNIQUE + INSERT OR IGNORE，迁移只补列不回填），所以老的
    「恰好一行就落」兜底会真命中 → 删掉，只记事件。
    """

    OTHER_BAR = BAR - 4 * 3600 * 1000          # 上一根 4h bar（另一个仓的止损）

    def _foreign_stop_row(self, bar_ms=None, purpose="stop"):
        bar_ms = self.OTHER_BAR if bar_ms is None else bar_ms
        self.db.hl_record_intent("BTC_USDT", "BTC", bar_ms, purpose, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting")
        self.db.hl_update_order("BTC_USDT", bar_ms, purpose, oid=STOP_OID + 7)
        return bar_ms

    def _proof(self):
        return {"px": FILL_PX, "sz": QTY, "reason": "exchange",
                "tids": ["419136180065962", "419136180065963"], "oids": []}

    def _order_events(self):
        return [r["message"] for r in self.db.hl_recent_events(50, kind="order")]

    def test_two_candidates_without_oid_are_untouched(self):
        other_bar = self._foreign_stop_row()
        ex = self.executor()
        hit = ex._mark_closing_order_filled(dict(self.row()), self._proof())

        self.assertIsNone(hit, "无 oid 证据时不许挑一行落终态")
        self.assertEqual(self.stop_row()["status"], "resting")
        self.assertEqual(self.db.hl_order("BTC_USDT", other_bar, "stop")["status"], "resting")
        self.assertTrue(any("没定位到对应挂单行" in m for m in self._order_events()))

    def test_single_foreign_candidate_is_untouched(self):
        """本 bar 没有可落的 stop 行 → 唯一候选是**别的 bar** 那张（旧代码会落它）。"""
        self.db.hl_update_order("BTC_USDT", BAR, "stop", status="cancelled")
        other_bar = self._foreign_stop_row(purpose="stop:fix")
        ex = self.executor()

        self.assertIsNone(ex._mark_closing_order_filled(dict(self.row()), self._proof()))
        self.assertEqual(self.db.hl_order("BTC_USDT", other_bar, "stop:fix")["status"], "resting",
                         "别的 bar 上没成交的止损不能被写成 filled")


class TestM2StopExitNeedsCloseLeg(FillCloseBase):
    """M2（假关账）：`_last_fill_px` 没有 Close 腿时不许拿开仓价冒充平仓价。"""

    def test_only_open_leg_hands_off_without_kill(self):
        self.db.set_pos_hl(self.pid, hl_status="mismatch")     # 事故第二入口的现场
        self.info._fills = _fills(oids=(OTHER_OID, OTHER_OID), dir_word="Open Long")
        ex = self.executor()

        px, fills = ex._last_fill_px("BTC")
        self.assertIsNone(px, "只有开仓腿时必须返回 None")
        self.assertEqual(len(fills), 2, "成交原文仍要返回，供诊断")

        self.assertFalse(ex.on_exit(self.pid, "stop", STOP))
        row = self.row()
        self.assertEqual(row["status"], "open", "不许按开仓价关账")
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertIsNone(row["exit_price"])
        self.assertIsNone(row["exit_reason"])
        self.assertFalse(self.db.hl_kill(), "让出接管权不等于 kill")
        self.assertEqual(self.ex.calls, [], "更不许下单/撤单")
        errs = [r["message"] for r in self.db.hl_recent_events(50, kind="error")]
        self.assertTrue(any("读不到成交价" in m for m in errs), errs)


class TestM3SwitchOffCoversExitPath(FillCloseBase):
    """M3：开关关掉 = 两条路径一起回到旧行为（含 on_exit 的 mismatch+stop 放行）。"""

    def test_disabled_switch_does_not_take_over_mismatch_stop(self):
        self.db.set_pos_hl(self.pid, hl_status="mismatch")
        self.info._fills = _fills()                 # 证据齐全，但开关关着
        ex = self.executor(fill_close=False)

        self.assertFalse(ex.on_exit(self.pid, "stop", STOP), "关掉开关后不该接管")
        row = self.row()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertIsNone(row["exit_price"])
        self.assertEqual(self.ex.calls, [])


class TestM4SnapshotRequired(FillCloseBase):
    """M4：没有快照 → 证明不成立（否则 ex_coins 为空，等于 fail-open）。"""

    def test_missing_snapshot_returns_none(self):
        self.info._fills = _fills()
        ex = self.executor()
        ex._sync_fills()
        row = dict(self.row())

        self.assertIsNone(ex._fill_close_proof(row), "缺省快照不许当成「交易所无仓」")
        self.assertIsNone(ex._fill_close_proof(row, None))
        # 对照：给了快照（且确实无仓）时证据是成立的 —— 证明上面真的是被 snap 挡住的
        proof = ex._fill_close_proof(row, _snap(equity=987.3, positions=[], orders=[]))
        self.assertIsNotNone(proof)
        self.assertEqual(proof["reason"], "stop")


class TestM5FillsConsumedOncePerRound(FillCloseBase):
    """M5（重复关账）：同轮同 coin 两个本地 open 行不得用**同一批**成交各关一次。"""

    def _second_position(self) -> int:
        bar2 = BAR - 4 * 3600 * 1000
        pid2 = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=bar2, status="open",
                                       side=1, entry_price=ENTRY, stop_price=STOP,
                                       stop_dist=0.0244943, contracts=64, coin_qty=QTY,
                                       notional=540.5, risk_u=13.2395, equity_at_entry=1000.0)
        self.db.confirm_position(pid2, actual_entry=ENTRY, actual_contracts=QTY,
                                 actual_stop=STOP, stop_placed=True)
        self.db.set_pos_hl(pid2, hl_status="open", hl_entry_oid=OTHER_OID + 1,
                           hl_stop_oid=STOP_OID + 1, hl_sz=QTY)
        self.db.hl_record_intent("BTC_USDT", "BTC", bar2, "entry", 1, QTY, px=ENTRY,
                                 status="filled")
        self.db.hl_update_order("BTC_USDT", bar2, "entry", oid=OTHER_OID + 1,
                                filled_sz=QTY, avg_px=ENTRY)
        self.db.hl_record_intent("BTC_USDT", "BTC", bar2, "stop", 1, QTY, px=STOP,
                                 reduce_only=True, status="resting")
        self.db.hl_update_order("BTC_USDT", bar2, "stop", oid=STOP_OID + 1)
        return pid2

    def test_second_row_fails_closed(self):
        pid2 = self._second_position()
        self.info._fills = _fills()            # 一批成交，合计刚好 QTY
        ex = self.executor()
        ex.run()

        rows = {self.pid: self.row(), pid2: self.db.position_by_id(pid2)}
        statuses = sorted(r["status"] for r in rows.values())
        self.assertEqual(statuses, ["closed", "open"], "只能关一个仓，另一个必须留给人看")
        loser = [r for r in rows.values() if r["status"] == "open"][0]
        self.assertEqual(loser["hl_status"], "mismatch")
        self.assertIsNone(loser["exit_price"])
        self.assertTrue(self.db.hl_kill(), "第二行证据不足 → fail-closed（mismatch + kill）")


class CountingSpecsInfo(FakeInfo):
    """记录 specs() 调用次数：M6 要证明「第二轮不再打接口」。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.spec_calls = []

    def specs(self, coins=None):
        self.spec_calls.append(tuple(coins or ()))
        return super().specs(coins)


class RaisingSpecsInfo(FakeInfo):
    """specs() 永远失败：失败**不许**进缓存（否则一小时都按「无精度」静默跳过）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.spec_calls = 0

    def specs(self, coins=None):
        self.spec_calls += 1
        raise HlInfoError("meta 不可用")


class TestM6SpecsTickCached(FillCloseBase):
    """M6：`_size_tick` 的 specs() 是真实 POST，必须按 (info, coin) 缓 1 小时。"""

    def test_second_round_does_not_hit_specs(self):
        # 交易所有仓 → 走数量对账 → 每轮都会问一次 tick
        self.info = CountingSpecsInfo(
            snap=_snap(equity=987.3, positions=[_pos(coin="BTC", size=QTY, entry=ENTRY)],
                       orders=[_stop_order(coin="BTC", trigger=STOP)]))
        ex = self.executor()
        ex.run()
        first = len(self.info.spec_calls)
        self.assertGreaterEqual(first, 1)
        ex.run()
        self.assertEqual(len(self.info.spec_calls), first,
                         f"第二轮不该再调 specs（实际 {self.info.spec_calls}）")

    def test_failure_is_not_cached(self):
        self.info = RaisingSpecsInfo(snap=_snap(equity=987.3, positions=[], orders=[]))
        ex = self.executor()
        self.assertIsNone(ex._size_tick("BTC"))
        self.assertIsNone(ex._size_tick("BTC"))
        self.assertEqual(self.info.spec_calls, 2, "失败必须每轮重试，不能被缓存")

    def test_tick_value_unchanged(self):
        self.info = CountingSpecsInfo(snap=_snap(equity=987.3, positions=[], orders=[]))
        ex = self.executor()
        self.assertAlmostEqual(ex._size_tick("BTC"), 1e-5, places=12)


class TestM7AlertWordingFollowsKillState(FillCloseBase):
    """M7：别的标的 fail-closed 已 kill 时，关账提醒不得声称「未暂停自动交易」。"""

    def _adding_eth_mismatch(self) -> int:
        """ETH 本地有仓、交易所无仓、无成交 → 同轮 mismatch + kill。"""
        pid = self.db.create_position(symbol="ETH_USDT", signal_bar_ms=BAR, status="open", side=1,
                                      entry_price=2500.0, stop_price=2400.0, stop_dist=0.04,
                                      contracts=10, coin_qty=0.5, notional=1250.0,
                                      risk_u=50.0, equity_at_entry=1000.0)
        self.db.confirm_position(pid, actual_entry=2500.0, actual_contracts=0.5,
                                 actual_stop=2400.0, stop_placed=True)
        self.db.set_pos_hl(pid, hl_status="open", hl_entry_oid=777, hl_stop_oid=778, hl_sz=0.5)
        return pid

    def test_no_false_reassurance(self):
        self._adding_eth_mismatch()
        self.info._fills = _fills()            # BTC 足额成交 → 正常关账、不 kill
        ex = self.executor()
        alerts = ex.run()

        self.assertTrue(self.db.hl_kill())
        stop_alerts = [m for m in alerts if "🛑 止损已在交易所成交" in m]
        self.assertEqual(len(stop_alerts), 1, alerts)
        self.assertNotIn("未暂停自动交易", stop_alerts[0],
                         "系统已暂停时不能写「未暂停」")
        self.assertIn("暂停状态", stop_alerts[0])
        self.assertIn("对账异常", stop_alerts[0], "要带上 kill 原因")


class TestM8ExitReasonRendered(unittest.TestCase):
    """M8：`exchange` 在摘要/确认页要显示中文（不能把英文枚举印给用户）。"""

    def test_exchange_label_and_wiring(self):
        from app import engine
        self.assertEqual(engine._exit_reason_plain("exchange", 1), "交易所侧已平仓（人工/外部）")
        self.assertEqual(engine._exit_reason_plain("stop", 1), "触发止损")
        # 未知值兜底原文（不许崩、也不许编一个看着像结论的词）
        self.assertEqual(engine._exit_reason_plain("weird_reason", 1), "weird_reason")

        src = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text("utf-8")
        self.assertNotIn("（{row['exit_reason']}）", src, "摘要页仍在直接印英文枚举")
        self.assertNotIn("（{row['exit_reason']}，", src, "确认页仍在直接印英文枚举")
        self.assertGreaterEqual(src.count("_exit_label(row)"), 2,
                                "摘要页与确认页都要走 _exit_label")


if __name__ == "__main__":
    unittest.main()
