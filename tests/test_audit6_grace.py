"""第六轮：对账失败**自动消化**（宽限期 + 证据 v2 + 窄口径自动解 kill）。

背景：2026-10-22 事故修完后（PR #44：交易所侧已平仓 → 按成交价关账），剩下的
「报对账异常 + kill」大多只是**时滞/标签问题**：

- 交易所刚成交、本地流水还没同步 → 上一轮实现会直接 mismatch + kill；
- ADL/清算成交的 dir 不写 "Close" → 判不出平仓腿；
- 窗口里混进别的仓的成交 → 归因/均价被污染；
- kill 已经消化的异常也不会自己恢复，永远等人工。

本文件钉住三件事（规格：宽限 900s 默认开、自动解 kill 默认开）：
1. **证据判定 v2**：cache 不足才定点回捞（最多 2 次）、ADL 腿、oid 锚定、双侧容差；
2. **宽限期** `hl_status='closing'`：不 kill、每轮重试、超时才升级；期间禁开新仓、
   引擎止损离场不许回落人工流；
3. **窄口径自动解 kill**：只对 origin='reconcile'、连续 2 轮干净才解除。

铁律：只用相对路径（CI 容器里没有部署机的绝对路径）；不 import app.main；
桩复用 tests/test_audit3_p1.py。
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
from app.hl import sizing as hl_sizing
from app.hl.executor import (GRACE_KEY_PREFIX, KILL_CLEAN_ROUNDS, REFETCH_ATTEMPTS,
                             Executor, ExecParams)
from app.hl.info import HlInfoError
from test_audit3_p1 import (FakeExchange, FakeInfo, SilentNotifier, _pos, _snap,
                            _stop_order)

BAR = 1792612800000
STOP_OID = 581840873520
OTHER_OID = 581840859768
QTY = 0.00638
ENTRY = 84479.0
STOP = 82386.33333333333
FILL_PX = 82375.0
GRACE_KEY = f"{GRACE_KEY_PREFIX}1"


def _fills(oids=(STOP_OID, STOP_OID), szs=(0.0025, 0.00388), px=FILL_PX, age_s=60,
           coin="BTC", dir_word="Close Long", pnl=None, tids=(419136180065962, 419136180065963)):
    """交易所成交桩。平仓腿 closedPnl 非 0，开仓腿为 0（与交易所真实数据一致）。"""
    now_ms = int(time.time() * 1000)
    if pnl is None:
        pnl = -5.26 if "Close" in str(dir_word) else 0.0
    out = []
    for tid, oid, sz in zip(tids, oids, szs):
        out.append({"coin": coin, "oid": oid, "dir": dir_word, "sz": sz, "px": px,
                    "time": now_ms - age_s * 1000, "tid": tid,
                    "closedPnl": pnl, "fee": 0.092671, "feeToken": "USDC", "hash": "0xaaa"})
    return out


class GraceInfo(FakeInfo):
    """带 `user_fills_by_time`（定点回捞）的桩，并记录调用次数/入参。

    `by_time` 是**每次调用按顺序**取的返回值列表（最后一个复用）：这样能区分
    「第一次回捞为空 → 第二次拿到成交」这种真实时滞。
    """

    def __init__(self, *a, by_time=None, **kw):
        super().__init__(*a, **kw)
        self.by_time = list(by_time or [])
        self.by_time_calls = []

    def user_fills_by_time(self, address, start_ms):
        self.by_time_calls.append(int(start_ms))
        if not self.by_time:
            return []
        idx = min(len(self.by_time_calls) - 1, len(self.by_time) - 1)
        got = self.by_time[idx]
        if isinstance(got, Exception):
            raise got
        return list(got)


class GraceBase(unittest.TestCase):
    """造景：本地 open 仓（BTC 0.00638）、交易所无仓 —— 与 2026-10-22 事故一致。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = SilentNotifier()
        self.info = GraceInfo(snap=_snap(equity=987.3, positions=[], orders=[]))
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, equity_min=20.0, risk_pct=0.0133, leverage=5.0,
                            code_rev="audit6")
        self.pid = self.open_position()

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------- 造景
    def open_position(self, symbol="BTC_USDT", coin="BTC", bar_ms=BAR, side=1,
                      entry=ENTRY, stop=STOP, qty=QTY, stop_oid=STOP_OID) -> int:
        pid = self.db.create_position(symbol=symbol, signal_bar_ms=bar_ms, status="open", side=side,
                                      entry_price=84455.0, stop_price=stop, stop_dist=0.0244943,
                                      contracts=64, coin_qty=qty, notional=540.5,
                                      risk_u=13.2395, equity_at_entry=1000.0)
        self.db.confirm_position(pid, actual_entry=entry, actual_contracts=qty,
                                 actual_stop=stop, stop_placed=True)
        self.db.set_pos_hl(pid, hl_status="open", hl_entry_oid=OTHER_OID,
                           hl_stop_oid=stop_oid, hl_sz=qty)
        self.db.hl_record_intent(symbol, coin, bar_ms, "entry", side, qty, px=entry,
                                 status="filled")
        self.db.hl_update_order(symbol, bar_ms, "entry", oid=OTHER_OID, filled_sz=qty,
                                avg_px=entry)
        self.db.hl_record_intent(symbol, coin, bar_ms, "stop", side, qty, px=stop,
                                 reduce_only=True, status="resting")
        self.db.hl_update_order(symbol, bar_ms, "stop", oid=stop_oid)
        return pid

    def executor(self, **kw) -> Executor:
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    # ---------------- 断言助手
    def row(self, pid=None):
        return self.db.position_by_id(self.pid if pid is None else pid)

    def alerts_text(self) -> str:
        return "\n".join(self.notifier.messages)

    def events(self, kind=None):
        return [r["message"] for r in self.db.hl_recent_events(80, kind=kind)]

    def grace_raw(self, pid=None) -> str:
        return str(self.db.get_config_str(
            f"{GRACE_KEY_PREFIX}{self.pid if pid is None else pid}", "") or "")

    def grace_age(self, seconds: float, pid=None) -> None:
        """把宽限起点改到 `seconds` 秒前（模拟「已经等了一会儿」）。"""
        pid = self.pid if pid is None else pid
        first = time.time() - seconds
        self.db.set_config(f"{GRACE_KEY_PREFIX}{pid}", f"{first:.3f}:3")


class TestEvidenceV2(GraceBase):
    """① 证据判定 v2：cache 优先、回捞兜底、ADL 腿、oid 锚定、双侧容差。"""

    def test_complete_fills_close_immediately_without_grace(self):
        """1. 成交当轮齐全 → 直接关账，不进宽限（closing_pending 为空）。"""
        self.info._fills = _fills()
        ex = self.executor()
        # 生产顺序（run() 第 1 步）是「先同步成交、再对账」；这里显式复现该顺序，
        # 否则直接调 reconcile 时 hl_fills 还是空的，会被当成「证据未到」进宽限。
        ex._sync_fills()
        rec = ex.reconcile(_snap(equity=987.3, positions=[], orders=[]))

        self.assertEqual(rec["closing_pending"], [], "证据齐全不该进宽限")
        self.assertEqual(len(rec["fill_closed"]), 1)
        self.assertEqual(rec["fill_closed"][0]["source"], "cache")
        row = self.row()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertAlmostEqual(row["exit_price"], FILL_PX, places=4)
        self.assertEqual(self.grace_raw(), "", "没进过宽限就不该留键")
        self.assertEqual(self.info.by_time_calls, [], "证据齐全时一次回捞都不该打")

    def test_adl_leg_without_close_dir_is_evidence(self):
        """6. ADL/清算：dir 不含 Close、reduce_only=0，但 closedPnl != 0 → 仍能关账。"""
        self.info._fills = _fills(dir_word="Auto-Deleveraging", pnl=-6.1)
        ex = self.executor()
        ex.run()

        row = self.row()
        self.assertEqual(row["status"], "closed", "ADL 成交必须被认作平仓腿")
        self.assertEqual(row["exit_reason"], "stop")     # oid 仍锚在本仓止损单上
        self.assertAlmostEqual(row["exit_price"], FILL_PX, places=4)
        self.assertFalse(self.db.hl_kill())

    def test_open_leg_with_pnl_is_not_evidence(self):
        """反例：dir 写着 Open 的腿一律不算（哪怕桩数据给了非 0 的 closedPnl）。"""
        self.info._fills = _fills(dir_word="Open Long", pnl=-5.26)
        ex = self.executor(close_grace=False)
        ex.run()

        self.assertEqual(self.row()["status"], "open", "开仓腿不能当平仓证据")
        self.assertTrue(self.db.hl_kill(), "差证据 → fail-closed")

    def test_oid_anchoring_ignores_other_position_legs(self):
        """7. 窗口里混进别仓的平仓腿 → 只按本仓止损单的成交关账，不被混合。"""
        mixed = _fills() + _fills(oids=(OTHER_OID,), szs=(0.01,), px=80000.0,
                                  dir_word="Close Long", tids=(910001,))
        self.info._fills = mixed
        ex = self.executor()
        ex.run()

        row = self.row()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["exit_reason"], "stop", "oid 锚定优先 → 归因为止损")
        self.assertAlmostEqual(row["exit_price"], FILL_PX, places=4,
                               msg="均价只能用止损单那两笔，不能把 80000 混进来")
        self.assertFalse(self.db.hl_kill())

    def test_tick_unavailable_uses_relative_tolerance(self):
        """8. tick 读不到（specs 抛错）→ 用相对容差仍能关账，不 kill。"""

        class RaisingSpecs(GraceInfo):
            def specs(self, coins=None):
                raise HlInfoError("meta 不可用")

        self.info = RaisingSpecs(snap=_snap(equity=987.3, positions=[], orders=[]))
        self.info._fills = _fills()
        ex = self.executor()
        ex.run()

        row = self.row()
        self.assertEqual(row["status"], "closed", "精度不可知不该把证据全废掉")
        self.assertFalse(self.db.hl_kill())

    def test_quantity_mismatch_after_refetch_fails_closed(self):
        """9. 回捞后数量仍不符 → problem + kill；回捞最多 REFETCH_ATTEMPTS 次。"""
        self.info._fills = _fills(szs=(0.0025, 0.0020))       # 合计 0.0045 < 0.00638
        self.info.by_time = [_fills(szs=(0.0025, 0.0020))]    # 回捞也拿不到足额
        ex = self.executor(close_grace=False)
        ex.run()

        self.assertEqual(self.row()["status"], "open")
        self.assertEqual(self.row()["hl_status"], "mismatch")
        self.assertTrue(self.db.hl_kill(), "差证据必须 fail-closed")
        self.assertEqual(len(self.info.by_time_calls), REFETCH_ATTEMPTS,
                         "回捞只在证据不足时发生，且不超过 REFETCH_ATTEMPTS 次")

    def test_partial_fill_never_closes(self):
        """双侧容差的下界：只平了一部分 → 不是证据（剩余敞口不能被抹掉）。"""
        self.info._fills = _fills(szs=(0.0025, 0.0020))
        ex = self.executor(close_grace=False)
        ex.run()
        self.assertEqual(self.row()["status"], "open")
        self.assertTrue(self.db.hl_kill())

    def test_two_rows_consume_fills_once(self):
        """10. 两条本地行抢同一批成交 → 第一条关账，第二条 fail-closed（consumed 生效）。"""
        pid2 = self.open_position(bar_ms=BAR - 4 * 3600 * 1000, stop_oid=STOP_OID + 1)
        self.info._fills = _fills()
        ex = self.executor(close_grace=False)
        ex.run()

        rows = {self.pid: self.row(), pid2: self.db.position_by_id(pid2)}
        statuses = sorted(r["status"] for r in rows.values())
        self.assertEqual(statuses, ["closed", "open"], "只能关一个仓")
        loser = [r for r in rows.values() if r["status"] == "open"][0]
        self.assertEqual(loser["hl_status"], "mismatch")
        self.assertTrue(self.db.hl_kill())


class TestRefetch(GraceBase):
    """② 定点回捞：cache 为空才打，最多 2 次，拿到就走 refetch 路径。"""

    def test_cache_empty_refetch_closes_with_source(self):
        """4. user_fills 为空、回捞有成交 → 走 refetch 关账。"""
        self.info._fills = []
        self.info.by_time = [_fills()]
        ex = self.executor()
        ex._sync_fills()                      # 模拟本轮同步（空）
        proof = ex._fill_close_proof(dict(self.row()),
                                     _snap(equity=987.3, positions=[], orders=[]))

        self.assertIsNotNone(proof)
        self.assertEqual(proof["source"], "refetch")
        self.assertEqual(proof["reason"], "stop")
        self.assertAlmostEqual(proof["sz"], QTY, places=8)
        self.assertEqual(len(self.info.by_time_calls), 1, "第一次就拿到 → 不该再打第二次")
        # 回捞窗口起点 = opened_at − 120s（毫秒）
        opened = float(self.row()["opened_at"])
        self.assertAlmostEqual(self.info.by_time_calls[0] / 1000.0, opened - 120.0, delta=2.0)

    def test_second_round_refetch_closes_and_clears_grace(self):
        """3. 首轮无成交进宽限 → 第二轮回捞到成交 → 关账 + 删 grace 键 + 不 kill。"""
        self.info._fills, self.info.by_time = [], [[]]
        ex = self.executor()
        ex.run()
        self.assertEqual(self.row()["hl_status"], "closing")
        self.assertNotEqual(self.grace_raw(), "", "首轮应写入宽限键")

        self.info.by_time = [_fills()]
        ex.run()

        row = self.row()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["hl_status"], "closed")
        self.assertAlmostEqual(row["exit_price"], FILL_PX, places=4)
        self.assertEqual(self.grace_raw(), "", "关账后宽限键必须删掉")
        self.assertFalse(self.db.hl_kill())
        self.assertIn("宽限期内已核对到成交", self.alerts_text())

    def test_refetch_error_is_not_evidence(self):
        """回捞抛异常 → 按无证据处理（不抛、不崩整轮）。"""
        self.info._fills = []
        self.info.by_time = [HlInfoError("userFillsByTime 不可用")]
        ex = self.executor()
        ex.run()                              # 不抛
        self.assertEqual(self.row()["hl_status"], "closing")
        self.assertFalse(self.db.hl_kill())
        self.assertEqual(len(self.info.by_time_calls), REFETCH_ATTEMPTS)


class TestGracePeriod(GraceBase):
    """③ 宽限期：不 kill、每轮重试、超时升级、禁开新仓、离场文案。"""

    def test_first_round_without_fills_enters_grace(self):
        """2. 首轮无成交 → closing + 不 kill + closing_pending 一条 + 事件只记一次。"""
        self.info._fills = []
        ex = self.executor()
        rec = ex.reconcile(_snap(equity=987.3, positions=[], orders=[]))

        self.assertEqual(len(rec["closing_pending"]), 1)
        cp = rec["closing_pending"][0]
        self.assertEqual(cp["position_id"], self.pid)
        self.assertEqual(cp["rounds"], 1)
        self.assertTrue(cp["new"])
        self.assertAlmostEqual(cp["grace_s"], 900.0, places=6)
        row = self.row()
        self.assertEqual(row["hl_status"], "closing")
        self.assertEqual(row["status"], "open", "宽限期不关账（怕抹掉真实仓位）")
        self.assertFalse(self.db.hl_kill(), "宽限期内不许 kill")
        self.assertNotEqual(self.grace_raw(), "")

        # 再跑一轮：仍然不 kill、不重复推「进入宽限」
        ex.run()
        self.assertEqual(len([m for m in self.events() if "进入宽限" in m]), 1,
                         "同一仓只记一次「进入宽限」")
        self.assertEqual(len([m for m in self.notifier.messages if "进入宽限" in m]), 1,
                         "同一仓只推一条「进入宽限」")
        self.assertNotIn("对账异常", self.alerts_text())

    def test_grace_timeout_escalates_to_problem_and_kill(self):
        """5. 宽限超时（first_seen 改到 1000s 前）→ problem + kill + mismatch + 删键。"""
        self.info._fills = []
        ex = self.executor()
        ex.run()
        self.assertEqual(self.row()["hl_status"], "closing")

        self.grace_age(1000)
        alerts = ex.run()

        row = self.row()
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertEqual(row["status"], "open")
        self.assertTrue(self.db.hl_kill(), "超时必须升级成对账异常")
        self.assertEqual(self.grace_raw(), "", "升级后宽限键必须删掉")
        text = "\n".join(alerts) + self.alerts_text()
        self.assertIn("15 分钟内未拿到成交证据", text)
        self.assertIn("对账异常", text)
        self.assertEqual(self.db.hl_kill_origin(), "reconcile")

    def test_entry_gate_blocks_symbol_in_grace(self):
        """11. 宽限期间禁开新仓（同标的），且不抛异常。"""
        self.info._fills = []
        ex = self.executor()
        ex.run()
        self.assertEqual(self.row()["hl_status"], "closing")

        plan = hl_sizing.size_order_hl("BTC", 5, 84479.0, STOP, 1000.0, 0.02, 5.0,
                                      side=1, min_notional=10.0)
        snap = _snap(equity=987.3, positions=[], orders=[])
        bad = ex._entry_gates("BTC_USDT", plan, snap)          # 不抛
        text = "；".join(bad)
        self.assertIn("平仓成交待核对，暂不开仓", text)

        # 别的标的照常（不是「全局停手」）
        other = ex._entry_gates("ETH_USDT", plan, snap)
        self.assertNotIn("平仓成交待核对", "；".join(other))

    def test_exit_stop_takes_over_while_closing(self):
        """12. closing + on_exit(stop) → 接管（True）、不发 MEXC 文案、正文含「核对中/无需人工」。"""
        self.info._fills = []
        ex = self.executor()
        ex.run()
        self.assertEqual(self.row()["hl_status"], "closing")

        took = ex.on_exit(self.pid, "stop", STOP)

        self.assertTrue(took, "宽限期内的 closing 行必须接管，不许回落人工流")
        row = self.row()
        self.assertEqual(row["status"], "open", "没有成交价就不许关账")
        self.assertEqual(row["hl_status"], "closing")
        text = self.alerts_text()
        self.assertIn("核对中", text)
        self.assertIn("无需人工", text)
        self.assertNotIn("MEXC", text, "HL 仓不许出现 MEXC 人工流指引")
        self.assertEqual([c for c in self.ex.calls if c[0] in ("order", "cancel")], [],
                         "不许下单/撤单")

    def test_exit_stop_closes_when_fill_price_known(self):
        """closing + 有成交价 → 按成交价关账 + 🛑 文案（同一守卫的另一支）。"""
        self.info._fills = []
        ex = self.executor()
        ex.run()
        self.info._fills = _fills()
        took = ex.on_exit(self.pid, "stop", STOP)

        self.assertTrue(took)
        row = self.row()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertAlmostEqual(row["exit_price"], FILL_PX, places=4)
        self.assertIn("🛑 止损离场", self.alerts_text())

    def test_closing_row_not_treated_as_open_elsewhere(self):
        """closing 行不会被当成正常持仓去补挂止损（否则一边核对一边挂单）。"""
        self.info._fills = []
        ex = self.executor()
        rec = ex.reconcile(_snap(equity=987.3, positions=[], orders=[]))
        self.assertEqual(rec["missing_stop"], [], "交易所无仓 → 不该进缺止损分支")
        self.assertEqual([c for c in self.ex.calls if c[0] == "order"], [])


class TestKillAutoclear(GraceBase):
    """④ 窄口径自动解 kill：只认 origin='reconcile' + 连续 KILL_CLEAN_ROUNDS 轮干净。"""

    def _clean_world(self):
        """本地无仓、交易所无仓无挂单（对账必然 ok）。"""
        self.db.conn.execute("DELETE FROM positions")
        self.db.conn.commit()
        self.info._snap = _snap(equity=987.3, positions=[], orders=[])

    def test_autoclear_after_two_clean_rounds(self):
        """13. origin=reconcile + 连续 2 轮 clean → 解除并推 1 条。"""
        self._clean_world()
        self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        ex = self.executor()

        ex.run()
        self.assertTrue(self.db.hl_kill(), "第 1 轮干净不够（阈值 %d）" % KILL_CLEAN_ROUNDS)
        alerts = ex.run()

        self.assertFalse(self.db.hl_kill(), "连续 2 轮干净应自动解除")
        self.assertEqual(len([m for m in alerts if "已自动恢复自动交易" in m]), 1)
        self.assertEqual(self.db.hl_kill_origin(), "auto")
        self.assertTrue(any("对账异常已自动恢复" in m for m in self.events("kill")),
                        "解除必须留事件")
        self.assertEqual(str(self.db.get_config_str("hl_kill_clean_rounds", "0")), "0")

    def test_dirty_round_resets_counter(self):
        """13b. 中间夹一轮不干净 → 计数归零，不解 kill。"""
        self._clean_world()
        self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        ex = self.executor()

        ex.run()                                     # 干净 → 1
        self.info._snap = _snap(equity=987.3, positions=[_pos(coin="ETH", size=0.5,
                                                             entry=2500.0)],
                                orders=[])           # 交易所有仓、本地没有 → problem
        ex.run()                                     # 不干净 → 归零
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(str(self.db.get_config_str("hl_kill_clean_rounds", "0")), "0")

        self.info._snap = _snap(equity=987.3, positions=[], orders=[])
        ex.run()                                     # 干净 → 1
        self.assertTrue(self.db.hl_kill(), "归零后要重新数满 2 轮")

    def test_manual_kill_never_autoclears(self):
        """14. 人工 kill（origin=manual）连续多轮 clean 也不解除。"""
        self._clean_world()
        self.db.hl_set_kill(True, "人工", origin="manual")
        ex = self.executor()
        for _ in range(4):
            ex.run()
        self.assertTrue(self.db.hl_kill(), "人工 kill 永不自解")
        self.assertEqual(self.db.hl_kill_origin(), "manual")

    def test_size_kill_never_autoclears(self):
        """数量对账/熔断/到期拉起的 kill 同样不在自动解除范围（窄口径）。"""
        self._clean_world()
        for origin in ("size", "daily_loss", "expiry"):
            self.db.hl_set_kill(True, "x", origin=origin)
            ex = self.executor()
            ex.run()
            ex.run()
            self.assertTrue(self.db.hl_kill(), f"origin={origin} 不该被自动解除")

    def test_clean_requires_trusted_empty_orders(self):
        """挂单列表不可信/非空 → 不算干净（不许凭「读不到挂单」放开交易）。"""
        self._clean_world()
        self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        snap = _snap(equity=987.3, positions=[], orders=[])
        snap["orders_trusted"] = False
        self.info._snap = snap
        ex = self.executor()
        ex.run()
        ex.run()
        self.assertTrue(self.db.hl_kill())

        snap["orders_trusted"] = True
        snap["open_orders"] = [_stop_order(coin="BTC")]
        ex.run()
        ex.run()
        self.assertTrue(self.db.hl_kill(), "还有挂单（幽灵止损）时不许恢复交易")


class TestSwitchesOff(GraceBase):
    """⑤ 开关关掉 = 回到旧行为（两个默认值都可回滚）。"""

    def test_close_grace_off_kills_on_first_round(self):
        """15a. close_grace=False → 首轮无成交就 problem + kill + mismatch。"""
        self.info._fills = []
        ex = self.executor(close_grace=False)
        ex.run()

        row = self.row()
        self.assertEqual(row["hl_status"], "mismatch")
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.grace_raw(), "", "关掉宽限就不该写键")
        self.assertIn("本地有仓", self.alerts_text())

    def test_kill_autoclear_off_keeps_kill(self):
        """15b. kill_autoclear=False → 连续多轮干净也不解除。"""
        self.db.conn.execute("DELETE FROM positions")
        self.db.conn.commit()
        self.info._snap = _snap(equity=987.3, positions=[], orders=[])
        self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        ex = self.executor(kill_autoclear=False)
        for _ in range(KILL_CLEAN_ROUNDS + 2):
            alerts = ex.run()
        self.assertTrue(self.db.hl_kill())
        self.assertEqual([m for m in alerts if "已自动恢复自动交易" in m], [])

    def test_fill_close_off_disables_everything(self):
        """fill_close=False 仍然是最外层开关：证据 v2 / 宽限一律不生效。"""
        self.info._fills = _fills()
        ex = self.executor(fill_close=False)
        ex.run()
        self.assertEqual(self.row()["status"], "open")
        self.assertEqual(self.row()["hl_status"], "mismatch")
        self.assertTrue(self.db.hl_kill())


class TestPanelWording(unittest.TestCase):
    """⑥ 面板（规格 §6）：closing → 「平仓待核对」+ 剩余宽限；未知值兜底原文。"""

    def test_status_word_and_remaining_minutes(self):
        from app.hl import views

        self.assertEqual(views.hl_status_text("closing"), "平仓待核对")
        self.assertEqual(views.hl_status_text("mismatch"), "账实不符")
        self.assertEqual(views.hl_status_text("weird_new_state"), "weird_new_state",
                         "未知值兜底原文，不编一个看着像结论的词")

        html = views._signals_html([{
            "symbol": "BTC_USDT", "coin": "BTC", "status": "open", "side": 1,
            "entry": 84479.0, "stop": STOP, "qty": QTY, "bar_ms": BAR,
            "hl_status": "closing", "hl_status_text": "平仓待核对",
            "grace_left_s": 900.0,
        }])
        self.assertIn("持仓中", html)
        self.assertIn("平仓待核对", html)
        self.assertIn("剩 15 分钟", html)

        # 宽限将要超时：仍然显示（超时前不留空白），显示 0 分钟
        html0 = views._signals_html([{
            "symbol": "BTC_USDT", "status": "open", "side": 1, "qty": QTY,
            "hl_status": "closing", "grace_left_s": 5.0,
        }])
        self.assertIn("剩 1 分钟", html0)

    def test_normal_rows_have_no_extra_line(self):
        from app.hl import views

        html = views._signals_html([{
            "symbol": "BTC_USDT", "status": "open", "side": 1, "qty": QTY,
        }])
        self.assertNotIn("平仓待核对", html)
        self.assertNotIn("账实不符", html)


if __name__ == "__main__":
    unittest.main()
