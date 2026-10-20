"""PR-3：reconcile() 的**持仓数量对账**（两击规则）单元测试。

为什么要有这块：reconcile 以前只比「本地有仓 / 交易所无仓」和「有没有止损挂单」，
**不比数量**。部分成交（IOC 只成一半）、手工加减仓、同步缺腿都会让两边数量分叉，
而数量错会一路带进 PnL 与止损量（止损单是按下单时的 sz 挂的）。

规则（见 Executor._size_check）：
  1. |流水净量 − 交易所数量| ≤ 1 tick → 无事，strike 清零；
  2. 首次超差 → 记事件 + **全量回捞一次**（userFillsByTime，从 opened_at−300s）重算；
     回捞后一致 → 自愈，**不清 kill**、不计 strike；
  3. 回捞后仍超差 → 计一击（第 1 击不动作）；
  4. 连续 2 击 → 交回 reconcile 走既有「对账异常 → kill + 告警」口径（fail-closed）；
  5. kill_before 已置位（人工在处理）→ 不动作，只记决策事件。

全部离线：临时 sqlite + 假 info（带 user_fills_by_time）/ 假交易所。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.db import DB
from app.hl import EV_DECISION, EV_ERROR, EV_RECONCILE, MODE_LIVE
from app.hl.executor import SIZE_DIFF_STRIKES_TO_KILL, Executor, ExecParams
from tests.test_hl_executor import FakeExchange, FakeInfo, FakeNotifier, _snap, _pos, _stop_order

BAR = 1_791_912_000_000


class FakeInfoByTime(FakeInfo):
    """在 FakeInfo 上补 user_fills_by_time（全量回捞用）。

    `_by_time` 是「回捞时才能看到」的成交（模拟 userFills 滑动窗口漏掉的入场腿）；
    `_fills` 仍是 userFills（滑动窗口）的返回。回捞调用次数记在 calls_by_time。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._by_time = []
        self.calls_by_time = []
        self.by_time_raises = None

    def user_fills_by_time(self, address, start_ms, end_ms=None, aggregate_by_time=False):
        self.calls_by_time.append(int(start_ms))
        if self.by_time_raises:
            raise self.by_time_raises
        return list(self._by_time)


def ex_fill(tid, coin="BTC", dir_="Open Long", sz=1.0, px=83000.0, ts=None, fee="0.05",
            closed_pnl="0.0"):
    return {"tid": tid, "coin": coin, "dir": dir_, "sz": str(sz), "px": str(px),
            "fee": fee, "feeToken": "USDC", "closedPnl": str(closed_pnl),
            "time": int((ts if ts is not None else time.time() - 60) * 1000)}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.info = FakeInfoByTime()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, code_rev="testrev")
        Executor._size_strikes.clear()

    def tearDown(self):
        Executor._size_strikes.clear()
        self.tmp.cleanup()

    def executor(self):
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def open_position(self, sz=0.01):
        """建一个 open 仓（本地 hl_sz / actual_entry 齐备），不走真实下单。

        直接落库而不是走 on_entry：本文件测的是「对账」，不需要假交易所参与；
        opened_at 写「现在」，与成交 ts 对齐（否则窗口会把成交挡在外面）。
        """
        pid = self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=BAR, side=1, entry_price=83000.0,
            stop_price=80600.0, stop_dist=0.029, contracts=1, coin_qty=sz,
            notional=83000.0 * sz, risk_u=20.0, equity_at_entry=1000.0)
        self.db.set_pos_hl(pid, hl_status="open", hl_sz=sz, hl_stop_oid=99)
        self.db.confirm_position(pid, 83000.0, sz, 80600.0, True)
        self.db.conn.execute("UPDATE positions SET opened_at=? WHERE id=?",
                             (time.time() - 600, pid))
        self.db.conn.commit()
        return pid

    def add_local_fill(self, tid, dir_="Open Long", sz=0.01, px=83000.0, coin="BTC",
                       ts=None, closed_pnl=0.0, fee=0.05):
        self.db.hl_upsert_fill(
            tid=tid, symbol=f"{coin}_USDT", coin=coin, side=1, sz=sz, px=px,
            fee=fee, fee_token="USDC", fee_quote=fee, dir=dir_, closed_pnl=closed_pnl,
            hash=tid, reduce_only=("Close" in dir_),
            ts=int(ts if ts is not None else time.time() - 60))

    def snap_with(self, size, orders=None):
        """交易所侧有 BTC 仓（数量=size）+ 止损单在场（避免触发 missing_stop 分支）。"""
        return _snap(positions=[_pos("BTC", size=size)], orders=orders or [_stop_order()])

    def events(self, kind=None):
        return [dict(r) for r in self.db.hl_recent_events(200, kind=kind)]

    def size_events(self):
        return [e for e in self.events()
                if "数量对账" in str(e["message"]) or "数量对账不一致" in str(e["message"])]

    def kill_events(self):
        return self.events(EV_ERROR) + self.events("kill")


class TestSizeWithinTolerance(Base):

    def test_matching_size_is_quiet(self):
        pid = self.open_position(sz=0.01)
        self.add_local_fill("f1", sz=0.01)
        rec = self.executor().reconcile(self.snap_with(0.01))
        self.assertTrue(rec["ok"], rec["problems"])
        self.assertEqual(rec["size_warn"], [])
        self.assertEqual(self.size_events(), [])
        self.assertEqual(self.info.calls_by_time, [])       # 一致时不做回捞（省一次请求）

    def test_one_tick_difference_is_within_tolerance(self):
        """1 tick（BTC szDecimals=5 → 1e-5）以内的差是浮点噪声，不算超差。"""
        pid = self.open_position(sz=0.01)
        self.add_local_fill("f1", sz=0.01)
        rec = self.executor().reconcile(self.snap_with(0.01 + 1e-5))
        self.assertTrue(rec["ok"], rec["problems"])
        self.assertEqual(self.size_events(), [])

    def test_strike_cleared_when_size_comes_back_in_sync(self):
        """之前记了一击，这轮数量对上了 → strike 清零（下一轮重新计数）。"""
        pid = self.open_position(sz=0.01)
        self.add_local_fill("f1", sz=0.01)
        ex = self.executor()
        ex.reconcile(self.snap_with(0.02))                  # 超差 → 1 击
        key = f"{pid}:BTC"
        self.assertEqual(Executor._size_strikes.get(key), 1)
        ex.reconcile(self.snap_with(0.01))                  # 数量恢复
        self.assertNotIn(key, Executor._size_strikes)


class TestSizeRefetchSelfHeals(Base):

    def test_first_mismatch_triggers_one_refetch_and_heals(self):
        """核心场景：本地流水缺出场腿 → 首次超差 → 回捞补齐 → 自愈且**不清 kill**。"""
        pid = self.open_position(sz=0.01)
        # 本地只有入场腿（0.02），交易所只剩 0.01 → 差 0.01 > 1 tick
        self.add_local_fill("f1", sz=0.02)
        # 回捞能看到被 userFills 滑出窗口的那笔平仓成交
        self.info._by_time = [ex_fill("x1", dir_="Close Long", sz=0.01, px=84000.0,
                                     closed_pnl="10.0")]
        self.db.set_config("hl_exec_kill", 1)               # 进来时 kill 已置位
        rec = self.executor().reconcile(self.snap_with(0.01), kill_before=True)
        self.assertTrue(rec["ok"], rec["problems"])
        self.assertEqual(len(self.info.calls_by_time), 1, "首次超差必须回捞一次")
        # 回捞起点 = opened_at − 300s
        self.assertLessEqual(self.info.calls_by_time[0],
                             int((time.time() - 600 - 300) * 1000) + 1000)
        # 本地流水被补齐（0.02 入 − 0.01 出 = 0.01）
        self.assertEqual(len(self.db.hl_recent_fills(10)), 2)
        self.assertNotIn(f"{pid}:BTC", Executor._size_strikes)
        self.assertEqual(self.events(EV_DECISION), [])      # 自愈不该走「kill 已置位」分支

    def test_heal_does_not_touch_kill_state(self):
        """回捞自愈时绝不改 kill（kill 是人工/熔断的产物，观测路径无权清）。"""
        self.open_position(sz=0.01)
        self.add_local_fill("f1", sz=0.02)
        self.info._by_time = [ex_fill("x1", dir_="Close Long", sz=0.01, px=84000.0,
                                     closed_pnl="10.0")]
        self.db.set_config("hl_exec_kill", 1)
        self.executor().reconcile(self.snap_with(0.01), kill_before=True)
        self.assertTrue(self.db.hl_kill(), "自愈路径不该清掉 kill")


class TestSizeTwoStrikes(Base):

    def _persistent_mismatch(self, sz_local=0.02, sz_ex=0.01):
        """本地流水真的缺腿，且回捞也补不上（交易所那笔成交不存在）。"""
        pid = self.open_position(sz=sz_ex)
        self.add_local_fill("f1", sz=sz_local)
        self.info._by_time = []                             # 回捞也看不到缺的那笔
        return pid

    def test_first_strike_does_not_kill(self):
        pid = self._persistent_mismatch()
        rec = self.executor().reconcile(self.snap_with(0.01))
        # 第 1 击：只进 size_warn（观察期，不并入 problems、不 kill）
        self.assertEqual(len(rec["size_warn"]), 1)
        self.assertFalse(rec["size_kill"])
        self.assertFalse(self.db.hl_kill())
        self.assertEqual(Executor._size_strikes.get(f"{pid}:BTC"), 1)
        self.assertTrue(any("第 1 次" in e["message"] for e in self.size_events()))

    def test_second_consecutive_strike_fails_closed(self):
        pid = self._persistent_mismatch()
        ex = self.executor()
        ex.reconcile(self.snap_with(0.01))                  # 第 1 击
        self.assertFalse(self.db.hl_kill())
        rec = ex.reconcile(self.snap_with(0.01))            # 第 2 击
        self.assertFalse(rec["ok"])
        self.assertTrue(rec["size_kill"])
        self.assertTrue(any("数量对账" in p for p in rec["problems"]))
        self.assertTrue(self.db.hl_kill(), "连续 2 次超差必须 fail-closed")
        self.assertEqual(Executor._size_strikes[f"{pid}:BTC"], SIZE_DIFF_STRIKES_TO_KILL)

    def test_run_kills_and_alerts_on_second_strike(self):
        """走真实 run()：第 2 击时 kill 置位 + 告警推送（含「对账异常」文案）。"""
        self._persistent_mismatch()
        # 让 run() 的快照返回交易所 0.01 仓
        self.info._snap = self.snap_with(0.01)
        ex = self.executor()
        ex.run(force_snapshot=True)                         # 第 1 击
        self.assertFalse(self.db.hl_kill())
        ex.run(force_snapshot=True)                         # 第 2 击 → kill
        self.assertTrue(self.db.hl_kill())
        self.assertTrue(any("对账异常" in m for m in self.notifier.messages),
                        self.notifier.messages)

    def test_kill_stays_put_when_kill_before_set(self):
        """kill_before=True（人工已在核对）→ 第 2 击也不重复触发，只记决策事件。

        「不动」的验证点：不写 kill 事件、不推告警、kill 保持原值（这里是 True，
        但也可能是 False —— 判据只看进来时的值）。
        """
        self._persistent_mismatch()
        self.db.set_config("hl_exec_kill", 1)
        ex = self.executor()
        ex.reconcile(self.snap_with(0.01), kill_before=True)
        kill_events_before = len(self.events("kill"))
        err_before = len(self.events(EV_ERROR))
        rec = ex.reconcile(self.snap_with(0.01), kill_before=True)
        self.assertFalse(rec["size_kill"], "kill 已置位时不该再走 fail-closed 报告")
        self.assertEqual(len(self.events("kill")), kill_events_before)
        self.assertEqual(len(self.events(EV_ERROR)), err_before)
        self.assertTrue(any("kill 早已置位" in e["message"]
                            for e in self.events(EV_DECISION)))

    def test_kill_before_false_still_kills(self):
        """反例保护：kill_before 显式 False（本轮开始时没 kill）→ 第 2 击照常置 kill。"""
        self._persistent_mismatch()
        ex = self.executor()
        ex.reconcile(self.snap_with(0.01), kill_before=False)
        self.assertFalse(self.db.hl_kill())
        ex.reconcile(self.snap_with(0.01), kill_before=False)
        self.assertTrue(self.db.hl_kill())

    def test_strikes_are_per_position(self):
        """strike 按 (仓位, coin) 计数：另一笔仓的超差不能把这一笔推进到第 2 击。"""
        pid = self.open_position(sz=0.01)
        self.add_local_fill("f1", sz=0.02)
        self.info._by_time = []
        ex = self.executor()
        ex.reconcile(self.snap_with(0.02))                  # pid 一致 → 第 1 击（差 0）
        # 造第二笔仓（ETH），它自己的超差算自己的
        pid2 = self.db.create_position(symbol="ETH_USDT", signal_bar_ms=BAR + 1, side=1,
                                      entry_price=2500.0, stop_price=2400.0, stop_dist=0.04,
                                      contracts=1, coin_qty=1.0, notional=2500.0,
                                      risk_u=100.0, equity_at_entry=1000.0)
        self.db.set_pos_hl(pid2, hl_status="open", hl_sz=1.0)
        self.db.confirm_position(pid2, 2500.0, 1.0, 2400.0, True)
        self.add_local_fill("eth1", coin="ETH", sz=5.0, px=2500.0)
        snap = _snap(positions=[_pos("BTC", size=0.01), _pos("ETH", size=1.0, entry=2500.0)],
                     orders=[_stop_order("BTC"), _stop_order("ETH", trigger=2400.0)])
        ex.reconcile(snap)
        self.assertEqual(Executor._size_strikes.get(f"{pid}:BTC"), 1)
        self.assertEqual(Executor._size_strikes.get(f"{pid2}:ETH"), 1)
        self.assertFalse(self.db.hl_kill())


class TestSizeSkipCases(Base):

    def test_no_fills_skips_check(self):
        """一笔成交都没有（首次同步还没跑）→ 不判数量，也不回捞。"""
        self.open_position(sz=0.01)
        rec = self.executor().reconcile(self.snap_with(0.05), kill_before=False)
        self.assertTrue(rec["ok"], rec["problems"])
        self.assertEqual(self.info.calls_by_time, [])
        self.assertEqual(self.size_events(), [])

    def test_unknown_spec_skips_check(self):
        """读不到 szDecimals → 不判（不猜精度），也不回捞。"""
        self.open_position(sz=0.01)
        self.add_local_fill("f1", sz=0.05)
        self.info._specs = {}                              # 规格读不到
        rec = self.executor().reconcile(self.snap_with(0.01), kill_before=False)
        self.assertTrue(rec["ok"], rec["problems"])
        self.assertEqual(self.info.calls_by_time, [])

    def test_refetch_failure_is_not_fatal(self):
        """回捞抛异常 → 照常计 strike，不打断整轮对账。"""
        pid = self.open_position(sz=0.01)
        self.add_local_fill("f1", sz=0.05)
        self.info.by_time_raises = RuntimeError("network down")
        rec = self.executor().reconcile(self.snap_with(0.01), kill_before=False)
        self.assertEqual(Executor._size_strikes.get(f"{pid}:BTC"), 1)
        self.assertTrue(rec["ok"])                          # 第 1 击不算 problem

    def test_closed_position_not_checked(self):
        """已关账的仓不参与数量对账（它本来就不该在交易所）。"""
        pid = self.open_position(sz=0.01)
        self.add_local_fill("f1", sz=0.05)
        self.db.close_position(pid, 84000.0, "ma20")
        self.db.set_pos_hl(pid, hl_status="closed")
        rec = self.executor().reconcile(self.snap_with(0.01), kill_before=False)
        self.assertEqual(self.size_events(), [])


class TestRunLogging(Base):

    def test_observation_state_is_not_logged_as_consistent(self):
        """观察期（第 1 击）不能写「对账一致」—— 有未解释的数量差就该留痕。"""
        self.open_position(sz=0.01)
        self.add_local_fill("f1", sz=0.05)
        self.info._by_time = []
        self.info._snap = self.snap_with(0.01)
        self.executor().run(force_snapshot=True)
        recent = self.events(EV_RECONCILE)
        self.assertTrue(any("数量对账观察中" in e["message"] for e in recent), recent)
        self.assertFalse(any(e["message"] == "对账一致" for e in recent))


class TestEntrySizeCheck(Base):
    """开仓确认点的数量核对（_check_entry_size）：差异只记事件、不阻断下单结果。"""

    def _open(self, fills=()):
        self.info._fills = list(fills)
        pid = self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=BAR, side=1, entry_price=83000.0,
            stop_price=80600.0, stop_dist=0.029, contracts=1, coin_qty=0.001,
            notional=83.0, risk_u=20.0, equity_at_entry=1000.0)
        self.assertTrue(self.executor().on_entry(pid))
        return pid

    def _fill(self, sz, dir_="Open Long", px=83000.0):
        return ex_fill("e1", dir_=dir_, sz=sz, px=px)

    def test_matching_flow_is_quiet(self):
        pid = self._open([self._fill("0.00833")])          # 与计划量同（假交易所全成）
        row = self.db.position_by_id(pid)
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "open")
        evs = [e for e in self.events() if "开仓数量与流水不一致" in e["message"]]
        # 假交易所的成交回报 sz = plan.sz（_pos 未参与），这里只要有成交且一致就不报
        self.assertEqual(evs, [], f"正常成交不该报警：{evs}")

    def test_partial_fill_records_event(self):
        """成交流水只有回报量的一半 → 立刻记一条，且**不影响**开仓结果。"""
        pid = self._open([self._fill("0.004")])
        row = self.db.position_by_id(pid)
        self.assertEqual(row["hl_status"], "open")         # 开仓照常完成（不改下单逻辑）
        evs = [e for e in self.events() if "开仓数量与流水不一致" in e["message"]]
        self.assertEqual(len(evs), 1, evs)
        self.assertIn("止损按", evs[0]["message"])

    def test_no_fill_yet_is_silent(self):
        self._open([])
        self.assertEqual([e for e in self.events() if "开仓数量与流水不一致" in e["message"]], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
