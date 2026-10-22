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
        """把宽限起点改到 `seconds` 秒前（模拟「已经等了一会儿」）。

        写的是**旧格式**（两段，无 escalated）—— 生产库里上一版留下的键就是这个形状，
        所以这也是「老键必须还能读」的回归输入。
        """
        pid = self.pid if pid is None else pid
        first = time.time() - seconds
        self.db.set_config(f"{GRACE_KEY_PREFIX}{pid}", f"{first:.3f}:3")

    def clean_world(self) -> None:
        """本地无仓、交易所无仓无挂单（对账必然 ok）。"""
        self.db.conn.execute("DELETE FROM positions")
        self.db.conn.commit()
        self.info._snap = _snap(equity=987.3, positions=[], orders=[])

    def bump_clean_gap(self, seconds: float = 61.0) -> None:
        """把「上一轮计数时刻」拨回 `seconds` 秒前（模拟一个真实的 5 分钟 tick 间隔）。

        M3 之后「连续两轮干净」有 60s 时间下限；测试里两次 run() 相隔毫秒，不拨这个
        时间戳的话第二轮本来就不该计数（那正是新用例要钉的行为）。
        """
        self.db.set_config("hl_kill_clean_last_ts", f"{time.time() - seconds:.3f}")


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
        """5. 宽限超时（first_seen 改到 1000s 前）→ problem + kill + mismatch + 升级标记。

        对抗审查 M1 之后**不再删键**：键改写为 `<first_seen>:<rounds>:1`（escalated），
        否则下一轮又会「第一次进入宽限」，无限循环。文案也改成真实 elapsed（M2 配套），
        不再是硬写的「15 分钟内」。
        """
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
        raw = self.grace_raw()
        self.assertNotEqual(raw, "", "超时不许删键（删了下一轮又从零进宽限 → M1 死循环）")
        self.assertEqual(raw.split(":")[2], "1", "第三段 escalated 必须置 1")
        text = "\n".join(alerts) + self.alerts_text()
        self.assertIn("仍未拿到成交证据", text)
        self.assertIn("已等待 10", text, "升级文案必须用真实 elapsed")
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
        self.clean_world()

    def test_autoclear_after_two_clean_rounds(self):
        """13. origin=reconcile + 连续 2 轮 clean（且间隔 ≥60s）→ 解除并推 1 条。"""
        self._clean_world()
        self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        ex = self.executor()

        ex.run()
        self.assertTrue(self.db.hl_kill(), "第 1 轮干净不够（阈值 %d）" % KILL_CLEAN_ROUNDS)
        # M3：两轮之间必须有时间下限（真实 tick 是 5 分钟一轮）。测试里两次 run() 相隔
        # 毫秒，把「上一轮计数时刻」拨回 61 秒前，才等价于第二个真 tick。
        self.bump_clean_gap()
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


class TestGraceEscalationSticky(GraceBase):
    """⑦ 对抗审查 M1：升级是**一次性判定**，不许超时后重进宽限（无限循环）。"""

    def test_escalated_grace_never_reenters_or_repushes(self):
        self.info._fills = []
        ex = self.executor()
        ex.run()                                     # 进宽限（第 1 轮）
        enters0 = len([m for m in self.notifier.messages if "进入宽限" in m])
        self.assertEqual(enters0, 1, "第一次进宽限只推 1 条（不许 _run_locked 再推一条）")
        events0 = len([m for m in self.events() if "进入宽限" in m])

        self.grace_age(1000)
        ex.run()                                     # 超时 → 升级 + kill
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.row()["hl_status"], "mismatch")
        self.assertEqual(len([m for m in self.notifier.messages if "进入宽限" in m]), enters0,
                         "升级那一轮不许再推「进入宽限」")

        for _ in range(3):                           # 之后每一轮都只报异常
            ex.run()

        self.assertEqual(len([m for m in self.notifier.messages if "进入宽限" in m]), enters0,
                         "已升级的仓不许再推「进入宽限」")
        self.assertEqual(len([m for m in self.events() if "进入宽限" in m]), events0,
                         "已升级的仓不许再记「进入宽限」")
        row = self.row()
        self.assertEqual(row["hl_status"], "mismatch", "状态不许在 mismatch/closing 之间来回翻")
        self.assertEqual(row["status"], "open")
        self.assertEqual(self.grace_raw().split(":")[2], "1", "escalated 标记必须留着")
        self.assertTrue(self.db.hl_kill())

    def test_clock_rollback_resets_instead_of_sticking_forever(self):
        """L3：first_seen 在 90 秒后的未来 → 视为无效状态、重置为 now（不能永久 closing）。"""
        self.info._fills = []
        pid = self.pid
        self.db.set_config(f"{GRACE_KEY_PREFIX}{pid}",
                           f"{time.time() + 90:.3f}:2")
        ex = self.executor()
        rec = ex.reconcile(_snap(equity=987.3, positions=[], orders=[]))

        self.assertEqual(len(rec["closing_pending"]), 1, "回拨后仍应在正常宽限里（elapsed≈0）")
        self.assertAlmostEqual(rec["closing_pending"][0]["elapsed_s"], 0.0, delta=5.0)
        self.assertEqual(self.row()["hl_status"], "closing")
        self.assertTrue(any("时钟回拨" in m for m in self.events()), "重置必须留事件")
        # 新起点 ≈ now（不是未来那个值）
        first = float(self.grace_raw().split(":")[0])
        self.assertLess(abs(first - time.time()), 30.0)
        self.assertFalse(self.db.hl_kill())


class TestGraceSnapshotFlap(GraceBase):
    """⑧ 对抗审查 M2：仓位在交易所「又出现」= 快照抖动 → 宽限时钟必须清零。"""

    def test_position_reappears_resets_clock_and_status(self):
        self.info._fills = []
        ex = self.executor()
        ex.run()                                     # 进宽限
        self.grace_age(800)                          # 已经等了 800s（离 900 只差 100s）
        self.assertEqual(self.row()["hl_status"], "closing")

        # 仓位又出现在交易所（快照抖动）：这一轮必须清键 + 回到 open + 记事件
        self.info._snap = _snap(equity=987.3,
                                positions=[_pos(coin="BTC", size=QTY, entry=ENTRY)],
                                orders=[_stop_order(coin="BTC", trigger=STOP)])
        rec = ex.reconcile(_snap(equity=987.3, positions=[_pos(coin="BTC", size=QTY, entry=ENTRY)],
                                 orders=[_stop_order(coin="BTC", trigger=STOP)]))
        self.assertTrue(rec["ok"], f"仓位回来了不该报异常：{rec['problems']}")
        self.assertEqual(self.row()["hl_status"], "open")
        self.assertEqual(self.grace_raw(), "", "抖动时必须清键（否则时钟继续走）")
        self.assertTrue(any("重新出现" in m for m in self.events()), "抖动必须留事件")

        # 下一次真的消失：从零开始计时（第 1 轮、new=True），而不是立刻升级
        self.info._snap = _snap(equity=987.3, positions=[], orders=[])
        rec2 = ex.reconcile(_snap(equity=987.3, positions=[], orders=[]))
        self.assertEqual(len(rec2["closing_pending"]), 1)
        cp = rec2["closing_pending"][0]
        self.assertEqual(cp["rounds"], 1, "重新计时 → 轮次从 1 开始")
        self.assertTrue(cp["new"])
        self.assertFalse(self.db.hl_kill(), "抖动前的 800s 不许被算进这一次的宽限")


class TestKillOriginHygiene(GraceBase):
    """⑨ 对抗审查 H1/H2/L6/M3：kill 的归因与解除必须是「只升不降、只对自家有效」。"""

    def test_manual_kill_not_overwritten_by_reconcile_problem(self):
        """H1：人工 kill 期间遇到对账异常 → origin 保持 manual，世界变干净也不解。"""
        self.db.hl_set_kill(True, "人工", origin="manual")
        self.info._fills = []
        ex = self.executor(close_grace=False)        # 差证据 → 直接 problem
        ex.run()
        self.assertEqual(self.db.hl_kill_origin(), "manual",
                         "对账异常不许把人工 kill 改写成 reconcile")
        self.assertTrue(self.db.hl_kill())

        # 世界变干净（本地仓被人工关掉）：连续多轮干净也不许自解
        self.clean_world()
        ex2 = self.executor()
        ex2.run()
        self.bump_clean_gap()
        ex2.run()
        ex2.run()
        self.assertTrue(self.db.hl_kill(), "人工 kill 永不自动解")
        self.assertEqual(self.db.hl_kill_origin(), "manual")

    def test_expiry_promotes_reconcile_kill_to_expiry(self):
        """H2：到期保护遇到 origin=reconcile 的 kill → 升级为 expiry，之后不再被自动解。"""
        from app.hl import agent as hl_agent

        self.clean_world()
        self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        hl_agent._kill(self.db, "agent 到期保护：清仓前先暂停开新仓",
                       lambda *a, **k: None)

        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.db.hl_kill_origin(), "expiry",
                         "到期保护必须把 reconcile 升级为 expiry")
        self.assertEqual(self.db.get_config_str("hl_exec_kill_reason"),
                         "agent 到期保护：清仓前先暂停开新仓")

        ex = self.executor()
        ex.run()
        self.bump_clean_gap()
        ex.run()
        self.assertTrue(self.db.hl_kill(), "到期 kill 永不自解（清仓后世界干净也不行）")
        self.assertEqual(self.db.hl_kill_origin(), "expiry")

    def test_expiry_does_not_rewrite_manual_kill(self):
        """H2 的另一半：人工 kill 的归因不许被到期保护改写。"""
        from app.hl import agent as hl_agent

        self.db.hl_set_kill(True, "人工", origin="manual")
        hl_agent._kill(self.db, "agent 到期保护", None)
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.db.hl_kill_origin(), "manual")
        self.assertEqual(self.db.get_config_str("hl_exec_kill_reason"), "人工")

    def test_autoclear_clears_kill_reason(self):
        """L6：自动解 kill 时原因必须一起清空（否则面板/看守解释一个不存在的停手）。"""
        self.clean_world()
        self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        ex = self.executor()
        ex.run()
        self.bump_clean_gap()
        alerts = ex.run()

        self.assertFalse(self.db.hl_kill())
        self.assertEqual(str(self.db.get_config_str("hl_exec_kill_reason") or ""), "",
                         "解除后 reason 必须被清空")
        self.assertEqual(len([m for m in alerts if "已自动恢复自动交易" in m]), 1)

    def test_second_clean_round_too_soon_is_not_counted(self):
        """M3：两次干净轮间隔 <60s → 不计数、也不归零（不能靠连点凑两轮）。"""
        self.clean_world()
        self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        ex = self.executor()

        ex.run()                                     # 计数 1，记时间戳
        self.assertEqual(str(self.db.get_config_str("hl_kill_clean_rounds", "0")), "1")
        ex.run()                                     # 毫秒级间隔 → 不计数
        self.assertEqual(str(self.db.get_config_str("hl_kill_clean_rounds", "0")), "1",
                         "间隔不足：不计数，也不归零")
        self.assertTrue(self.db.hl_kill(), "同一世界判两次不算「连续两轮干净」")

        self.bump_clean_gap()                        # 真正的下一个 tick
        ex.run()
        self.assertFalse(self.db.hl_kill(), "间隔够了才解除")


class TestGraceHousekeeping(GraceBase):
    """⑩ 对抗审查 L1/L2/L5：垃圾键、限频、以及「宽限中别说已恢复」。"""

    def test_close_local_clears_grace_key(self):
        """L1：关账后宽限键必须消失（否则 configuration 表永久残留）。"""
        self.info._fills = []
        ex = self.executor()
        ex.run()
        self.assertNotEqual(self.grace_raw(), "")

        ex._close_local(self.pid, FILL_PX, "stop", QTY)

        self.assertEqual(self.grace_raw(), "", "关账必须清宽限键")
        self.assertEqual(self.row()["status"], "closed")
        self.assertEqual(self.row()["hl_status"], "closed")

    def test_late_grace_stops_refetching(self):
        """L2：宽限第 4 轮之后只读 cache（不再每轮打 userFillsByTime）。"""
        self.info._fills = []
        self.info.by_time = [[]]                     # 回捞永远拿不到证据（仓必须还挂着）
        ex = self.executor()
        ex.run()                                     # 第 1 轮
        self.assertTrue(self.info.by_time_calls, "前几轮应当回捞（时滞通常一两轮）")

        pid = self.pid
        self.info.by_time_calls.clear()
        self.db.set_config(f"{GRACE_KEY_PREFIX}{pid}", f"{time.time() - 100:.3f}:4")
        rec = ex.reconcile(_snap(equity=987.3, positions=[], orders=[]))

        self.assertEqual(self.info.by_time_calls, [], "宽限后半程不许再打接口")
        self.assertEqual(len(rec["closing_pending"]), 1, "仍应在宽限中（不升级、不关账）")
        self.assertEqual(self.row()["status"], "open")

    def test_no_recovery_alert_while_grace_running(self):
        """L5：closing_pending 非空时不许推「对账已恢复正常」。"""
        self.info._fills = []
        ex = self.executor()
        ex.run()                                     # 进宽限
        self.db.set_config("hl_recon_alert_at", f"{time.time():.0f}")   # 上一次告警节流在

        alerts = ex.run()

        self.assertNotIn("对账已恢复正常", "\n".join(alerts),
                         "宽限期还在进行时不许说「已恢复正常」")
        self.assertEqual([a for a in alerts if "对账已恢复正常" in a], [])

    def test_recovery_alert_returns_after_grace_resolved(self):
        """对照：宽限结束（证据到齐、关账）后恢复文案照常推。"""
        self.info._fills = []
        ex = self.executor()
        ex.run()
        self.db.set_config("hl_recon_alert_at", f"{time.time():.0f}")

        self.info._fills = _fills()                  # 成交到了
        alerts = ex.run()

        self.assertEqual(self.row()["status"], "closed")
        self.assertIn("对账已恢复正常", "\n".join(alerts))


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
