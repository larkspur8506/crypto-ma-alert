"""PR-4：两道闸门的单元测试（**全部离线**，复用 test_hl_executor 的假件）。

被测的两件事（见 app/hl/executor.py 顶部常量与 Executor._gate_check）：

1. **静置闸门（settling gate，15s 有界不可续期）**
   入场成交回报后该 coin-side 置 15s：期间禁止撤单/复价/补挂重试与新开仓；
   **豁免**首次止损挂单、平仓、只读查询。窗口只由成交回报起算，任何后续事件
   都不能把它推后；到期自动清除。

2. **churn 熔断（10 分钟 ≥ N 次 cancel/replace）**
   同一 coin-side 在窗口内撤单/改价重挂达到阈值 → 停手 + 事件 + 告警；
   阈值可配（`ExecParams.churn_max` ← `hl_churn_max`，默认 5）；窗口滑出后自动恢复；
   **不置 kill**（熔断不是对账异常）。

为什么这些测试重要：这两道闸门唯一的价值就是「不拦错、不漏拦」——
拦了首次止损 = 裸仓；漏拦补挂重试 = 撤-挂-撤-挂风暴。
"""
from __future__ import annotations

import time
import tempfile
import unittest
from pathlib import Path

from app.db import DB
from app.hl import EV_DECISION, MODE_LIVE
from app.hl.exchange import STATUS_ERROR, STATUS_FILLED, STATUS_RESTING
from app.hl.executor import (ACT_CLOSE, ACT_ENTRY, ACT_REPAIR, ACT_REPLACE, ACT_STOP_FIRST,
                             CHURN_WINDOW_S, GATE_CHURN, GATE_OK, GATE_SETTLE,
                             SETTLE_WINDOW_S, Executor, ExecParams)
from tests.test_hl_executor import FakeExchange, FakeInfo, FakeNotifier, _pos, _snap, _stop_order

BAR = 1_791_912_000_000


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, code_rev="testrev",
                            # 本文件测的是闸门/静置/熔断的布线，不建模信号时效；
                            # 时效闸（默认 30 分钟）由 tests/test_hl_signal_age.py 专测。
                            max_signal_age_min=0)
        self._reset_gate_state()

    def tearDown(self):
        self._reset_gate_state()
        self.tmp.cleanup()

    @staticmethod
    def _reset_gate_state():
        """闸门状态是类属性（跨实例累计）—— 用例之间必须清干净，否则互相污染。"""
        Executor._settle_until.clear()
        Executor._churn_hits.clear()
        Executor._churn_alerted.clear()
        Executor._gate_ev_round.clear()

    def executor(self, **kw):
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    # ---- 造数据
    def add_signal(self, symbol="BTC_USDT", side=1, entry=83000.0, stop=80600.0):
        return self.db.create_position(symbol=symbol, signal_bar_ms=BAR, side=side,
                                       entry_price=entry, stop_price=stop,
                                       stop_dist=abs(entry - stop) / entry, contracts=1,
                                       coin_qty=0.001, notional=entry * 0.001,
                                       risk_u=20.0, equity_at_entry=1000.0)

    def open_position(self):
        """走真实 on_entry 建一个 open 仓（会顺带上静置闸门）。"""
        pid = self.add_signal()
        self.assertTrue(self.executor().on_entry(pid))
        self.ex.calls.clear()
        return pid

    def open_position_without_gate(self):
        """开仓后立刻清掉静置窗口：用于「只想测 churn」的用例。

        这不是绕过被测逻辑 —— 静置闸门本身另有专门的用例（TestSettleGate），
        这里只是把时间轴快进到「窗口已过」的状态。
        """
        pid = self.open_position()
        Executor._settle_until.clear()
        return pid

    # ---- 断言助手
    def events(self, kind=None):
        return [dict(r) for r in self.db.hl_recent_events(50, kind=kind)]

    def order_calls(self, trigger=True):
        """FakeExchange.place_order 记录：(order, coin, is_buy, sz, px, reduce_only, trigger_px, cloid)"""
        if trigger:
            return [c for c in self.ex.calls if c[0] == "order" and c[6] is not None]
        return [c for c in self.ex.calls if c[0] == "order" and c[6] is None]

    def gate_events(self):
        return [e for e in self.events(EV_DECISION) if "闸门" in e["message"]]

    def churn_key(self, coin="BTC", side=1):
        return (str(self.db.path), coin, int(side))


class TestSettleGate(Base):
    """静置闸门：拦截 / 过期 / 豁免 / 不可续期。"""

    def test_arm_on_fill_and_idempotent_value(self):
        """成交回报后开窗，剩余秒数 ≈ 15s（不是 0 也不是无限）。"""
        self.open_position()
        left = self.executor()._settle_remaining("BTC", 1)
        self.assertGreater(left, 0)
        self.assertLessEqual(left, SETTLE_WINDOW_S + 0.5)

    def test_expires_and_self_clears(self):
        """15s 硬过期：把 deadline 放到过去 → 剩余 0 且键被清掉（不泄漏）。"""
        self.open_position()
        key = self.churn_key()
        self.assertIn(key, Executor._settle_until)
        Executor._settle_until[key] = time.time() - 0.01
        self.assertEqual(self.executor()._settle_remaining("BTC", 1), 0.0)
        self.assertNotIn(key, Executor._settle_until, "过期后必须自清，否则内存只增不减")

    def test_not_extendable_by_other_events(self):
        """**不可续期**：对账/补挂尝试/其它动作都不能把 deadline 往后推。"""
        self.open_position()
        key = self.churn_key()
        deadline0 = Executor._settle_until[key]
        ex = self.executor()
        # 各种「本会碰交易所」的动作：补挂修复（被静置拦下）、开仓闸门、churn 计数
        ex._repair_missing_stop({"id": 1, "symbol": "BTC_USDT", "signal_bar_ms": BAR,
                                 "side": 1, "stop_price": 80600.0, "hl_sz": 0.01},
                                _snap(positions=[_pos("BTC")], orders=[]))
        ex._gate_check(ACT_ENTRY, "BTC", 1)
        ex._churn_note("BTC", 1)
        self.assertEqual(Executor._settle_until[key], deadline0,
                         "任何事件都不许续期（可续期 = 可以被无限拖延）")
        # 剩余秒数只会随时间减少
        self.assertLess(ex._settle_remaining("BTC", 1), SETTLE_WINDOW_S)

    def test_blocks_new_entry(self):
        """窗口内不许再开仓：闸门拒绝 → 落 skipped 意图 + 事件，且没发任何单。"""
        self.open_position()                        # BTC 多单，已开窗
        self.db.create_position(symbol="BTC_USDT", signal_bar_ms=BAR + 1, side=1,
                               entry_price=83000.0, stop_price=80600.0,
                               stop_dist=0.029, contracts=1, coin_qty=0.001,
                               notional=83.0, risk_u=20.0, equity_at_entry=1000.0)
        ex = self.executor()
        pid2 = [r for r in self.db.recent_positions(10) if r["signal_bar_ms"] == BAR + 1][0]["id"]
        self.assertTrue(ex.on_entry(pid2))
        self.assertEqual(self.order_calls(), [], "静置窗口内不得发任何单")
        self.assertTrue(any("静置" in m for m in self.notifier.messages))
        self.assertTrue(any("静置" in e["message"] for e in self.gate_events()))

    def test_blocks_repair_retry_but_not_first_protection(self):
        """补挂重试被静置拦下；首次补挂（churn=0）不被拦 —— 否则就是裸仓等 15s。"""
        pid = self.open_position()                   # 静置窗口开着、churn=0
        snap = _snap(positions=[_pos("BTC")], orders=[])
        row = dict(self.db.position_by_id(pid))
        ex = self.executor()
        # ① 首次补挂：豁免 → 真的挂出去
        ex._repair_missing_stop(row, snap)
        self.assertEqual(len(self.order_calls()), 1, "首次止损补挂必须放行（安全动作）")
        # ② 第二次（churn=1，窗口还没过）→ 被静置拦下
        self.ex.calls.clear()
        ex2 = self.executor()
        ex2._repair_missing_stop(row, snap)
        self.assertEqual(self.ex.calls, [], "补挂重试在静置窗口内必须停手")
        self.assertTrue(any("静置" in e["message"] for e in self.gate_events()))

    def test_exempts_first_stop_and_close(self):
        """放行清单：首次止损挂单与平仓**永不**被闸门延迟（否则就是裸仓）。"""
        ex = self.executor()
        ex._settle_arm("BTC", 1)
        self.assertEqual(ex._gate_check(ACT_STOP_FIRST, "BTC", 1)[0], GATE_OK)
        self.assertEqual(ex._gate_check(ACT_CLOSE, "BTC", 1)[0], GATE_OK)
        # 对照：同一时刻的新开仓被静置拦下
        self.assertEqual(ex._gate_check(ACT_ENTRY, "BTC", 1)[0], GATE_SETTLE)

    def test_exit_replace_not_blocked_by_settle(self):
        """离场路径的重挂**不查静置**：那时止损已被撤掉，再等 15s 就是纯裸仓。

        它的频率问题交给 churn（churn 拦下来的处置是「市价平」，仍是保护动作）。
        """
        ex = self.executor()
        ex._settle_arm("BTC", 1)
        self.assertEqual(ex._gate_check(ACT_REPLACE, "BTC", 1)[0], GATE_OK,
                         "静置不得延迟已经决定的重挂止损（会制造裸仓）")
        # 但 churn 照样能拦住它
        for _ in range(5):
            ex._churn_note("BTC", 1)
        self.assertEqual(ex._gate_check(ACT_REPLACE, "BTC", 1)[0], GATE_CHURN)

    def test_close_still_works_during_settle(self):
        """端到端：静置窗口内 on_exit 照样撤止损 + 平仓（保护动作不受闸门影响）。"""
        pid = self.open_position()                   # 窗口开着
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": 55, "avg_px": 84500.0,
                        "filled_sz": 0.01, "error": None, "raw": {}}
        self.assertTrue(self.executor().on_exit(pid, "ma20", 84400.0))
        kinds = [c[0] for c in self.ex.calls]
        self.assertEqual(kinds[:2], ["cancel", "order"], "平仓路径不得被静置闸门拦住")
        self.assertEqual(self.db.position_by_id(pid)["status"], "closed")

    def test_other_coin_side_unaffected(self):
        """闸门按 coin-side 独立：BTC 多单静置不影响 ETH 空单的开仓判断。"""
        ex = self.executor()
        ex._settle_arm("BTC", 1)
        self.assertEqual(ex._gate_check(ACT_ENTRY, "ETH", -1)[0], GATE_OK)

    def test_gate_event_rate_limited_per_round(self):
        """同 coin-side 每轮 tick 最多一条闸门事件（否则 15s 窗口刷成一堆同样的行）。"""
        ex = self.executor()
        ex._settle_arm("BTC", 1)
        ex._churn_note("BTC", 1)          # churn>0 → 这次修复算「重试」（首次保护是豁免的）
        ex._gate_new_round()
        n0 = len(self.gate_events())
        for _ in range(3):
            ex._gate_act(ACT_REPAIR, "BTC", 1, symbol="BTC_USDT")
        self.assertEqual(len(self.gate_events()) - n0, 1, "同一轮只记一条")
        ex._gate_new_round()                          # 下一轮 → 再记一条
        ex._gate_act(ACT_REPAIR, "BTC", 1, symbol="BTC_USDT")
        self.assertEqual(len(self.gate_events()) - n0, 2)


class TestChurnBreaker(Base):
    """churn 熔断：计数到阈值停手 / 阈值可配 / 窗口滑出恢复 / 不置 kill。"""

    def test_default_threshold_is_five(self):
        self.assertEqual(self.p.churn_max, 5)

    def test_trips_at_threshold_and_counts_window(self):
        ex = self.executor()
        for i in range(1, 5):
            self.assertEqual(ex._churn_record("BTC", 1), i)
            self.assertFalse(ex._churn_tripped("BTC", 1), f"第 {i} 次不该熔断（阈值 5）")
        self.assertEqual(ex._churn_record("BTC", 1), 5)
        self.assertTrue(ex._churn_tripped("BTC", 1), "第 5 次达到阈值 → 熔断")

    def test_threshold_is_configurable(self):
        ex = self.executor(churn_max=2)
        ex._churn_record("BTC", 1)
        self.assertFalse(ex._churn_tripped("BTC", 1))
        ex._churn_record("BTC", 1)
        self.assertTrue(ex._churn_tripped("BTC", 1), "churn_max=2 → 第 2 次就熔断")

    def test_zero_disables_breaker(self):
        """0 = 关闭熔断（只留静置闸门）：配 0 的人明确表示不要这道闸。"""
        ex = self.executor(churn_max=0)
        for _ in range(10):
            ex._churn_record("BTC", 1)
        self.assertFalse(ex._churn_tripped("BTC", 1))
        self.assertEqual(ex._gate_check(ACT_REPAIR, "BTC", 1)[0], GATE_OK)

    def test_window_slides_out_and_recovers(self):
        """窗口滑出后自动恢复：不需要任何人解锁。"""
        ex = self.executor()
        base = time.time() - CHURN_WINDOW_S - 5          # 5 次都发生在窗口之外
        for _ in range(5):
            ex._churn_record("BTC", 1, now=base)
        self.assertFalse(ex._churn_tripped("BTC", 1), "滑出窗口的记录不再计入")
        self.assertEqual(ex._churn_count("BTC", 1), 0)

    def test_partial_slide_keeps_recent_hits(self):
        """只滑出老记录：窗口内还剩 2 次（阈值 5）→ 不熔断；再来 3 次才熔断。"""
        ex = self.executor()
        old = time.time() - CHURN_WINDOW_S - 1
        for _ in range(3):
            ex._churn_record("BTC", 1, now=old)
        for _ in range(2):
            ex._churn_record("BTC", 1)
        self.assertFalse(ex._churn_tripped("BTC", 1))
        ex._churn_record("BTC", 1)
        ex._churn_record("BTC", 1)
        ex._churn_record("BTC", 1)
        self.assertTrue(ex._churn_tripped("BTC", 1))

    def test_blocks_repair_and_falls_back_to_market_close(self):
        """熔断后补挂停手，但仓位不能裸着 → 直接市价平（保护动作不过闸）。"""
        pid = self.open_position_without_gate()
        ex = self.executor()
        for _ in range(5):
            ex._churn_note("BTC", 1)                   # 先把窗口打满
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[])
        ex.run(force_snapshot=True)
        self.assertEqual(self.order_calls(), [], "熔断期间不得再补挂触发单")
        self.assertTrue(self.order_calls(trigger=False), "补挂停手 → 必须市价平仓，不留裸仓")
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "closed")

    def test_breaker_does_not_set_kill(self):
        """熔断 ≠ 对账异常：只停手 + 告警，kill 语义不动（不置位）。

        注意这里**不能**用 run() 来验：`run()` 里的对账发现「有仓无止损」本身就会
        置 kill（那是既有语义，与本闸门无关）。所以直接验闸门路径。
        """
        ex = self.executor(churn_max=3)
        for _ in range(3):
            ex._churn_note("BTC", 1)
        self.assertFalse(ex._gate_act(ACT_REPAIR, "BTC", 1, symbol="BTC_USDT"))
        self.assertFalse(self.db.hl_kill(), "churn 熔断不置 kill")
        self.assertFalse(any(e["kind"] == "kill" for e in self.events()), "也不写 kill 事件")
        self.assertTrue(any("撤单/重挂过于频繁" in m for m in self.notifier.messages),
                        "必须走既有告警通道让人看见")

    def test_alert_once_per_window(self):
        """告警去重：每轮都撞闸，但同一窗口只推一条（否则真异常被自己刷掉）。"""
        ex = self.executor()
        for _ in range(5):
            ex._churn_note("BTC", 1)
        for _ in range(3):
            ex._gate_new_round()
            ex._gate_act(ACT_REPAIR, "BTC", 1, symbol="BTC_USDT")
        alerts = [m for m in self.notifier.messages if "撤单/重挂过于频繁" in m]
        self.assertEqual(len(alerts), 1, "同一窗口只告警一次")

    def test_replace_blocked_by_breaker_falls_back_to_close(self):
        """离场路径的重挂：熔断时不重挂 → 落到既有「重挂失败 → 市价平仓」分支。

        这条是「撤-挂-撤-挂」风暴的真正出口：平仓 IOC 没成交、重挂止损被熔断
        拦住 → 立刻市价平，而不是继续往交易所打第 N 张注定被拒的触发单。
        """
        pid = self.open_position_without_gate()
        ex = self.executor()
        for _ in range(5):
            ex._churn_note("BTC", 1)
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        # 平仓 IOC 未成交（resting）→ 走「撤单 + 重挂止损」分支
        self.ex.exit = {"ok": True, "status": STATUS_RESTING, "oid": 66, "avg_px": None,
                        "filled_sz": None, "error": None, "raw": {}}
        ex.on_exit(pid, "ma20", 84400.0)
        # 熔断下不许有新的触发单；但必须有市价平仓单
        self.assertEqual(self.order_calls(), [], "熔断期间不得重挂止损触发单")
        self.assertTrue(self.order_calls(trigger=False), "重挂被拦 → 必须市价平仓")
        self.assertFalse(self.db.hl_kill())

    def test_breaker_blocks_new_entry(self):
        """熔断期间也不许新开仓（同属重复动作）。"""
        ex = self.executor()
        for _ in range(5):
            ex._churn_note("BTC", 1)
        verdict, reason = ex._gate_check(ACT_ENTRY, "BTC", 1)
        self.assertEqual(verdict, GATE_CHURN)
        self.assertIn("churn", reason)

    def test_repair_success_resets_nothing_but_window_handles_recovery(self):
        """补挂成功后计数不归零（churn 是「动作频率」，不是「失败次数」），
        但窗口滑出后一定恢复 —— 两条合起来保证「不会永久停手」。"""
        self.open_position_without_gate()
        ex = self.executor()
        for _ in range(5):
            ex._churn_note("BTC", 1)
        self.assertTrue(ex._churn_tripped("BTC", 1))
        Executor._churn_hits[ex._gate_key("BTC", 1)] = [
            time.time() - CHURN_WINDOW_S - 1 for _ in range(5)]
        self.assertFalse(ex._churn_tripped("BTC", 1), "窗口滑出后必须自动恢复")


class TestGatesWiring(Base):
    """调用点覆盖：确认每条动作路径真的接到闸门上（不是只有 helper 存在）。"""

    def test_gate_action_classes(self):
        ex = self.executor()
        ex._settle_arm("BTC", 1)
        # 入场 / 修复 过静置；重挂只过 churn（不查静置）；首次止损 / 平仓 不过闸
        self.assertNotEqual(ex._gate_check(ACT_ENTRY, "BTC", 1)[0], GATE_OK)
        ex._churn_note("BTC", 1)          # 修复重试才算「重复动作」
        self.assertNotEqual(ex._gate_check(ACT_REPAIR, "BTC", 1)[0], GATE_OK)
        self.assertEqual(ex._gate_check(ACT_REPLACE, "BTC", 1)[0], GATE_OK)
        self.assertEqual(ex._gate_check(ACT_STOP_FIRST, "BTC", 1)[0], GATE_OK)
        self.assertEqual(ex._gate_check(ACT_CLOSE, "BTC", 1)[0], GATE_OK)

    def test_entry_arms_settle_after_fill(self):
        """on_entry 成功开仓 = 成交回报 → 必然开窗（否则这道闸门等于不存在）。"""
        self.open_position()
        self.assertTrue(self.executor()._settle_remaining("BTC", 1) > 0)

    def test_stop_failure_path_still_closes(self):
        """开仓挂止损失败 → 立即平仓：这条最危险的路径不能被静置闸门干扰。"""
        self.ex = FakeExchange(stop={"ok": False, "status": STATUS_ERROR,
                                     "error": "trigger rejected", "oid": None,
                                     "avg_px": None, "filled_sz": None, "raw": None})
        pid = self.add_signal()
        self.assertTrue(self.executor().on_entry(pid))
        self.assertTrue(self.order_calls(trigger=False), "止损挂失败必须立刻市价平仓")
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "aborted")

    def test_churn_counts_cancel_in_exit_path(self):
        """离场路径的撤单也计入 churn（撤-挂风暴常从这里开始，不能漏计）。"""
        pid = self.open_position_without_gate()
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        self.ex.exit = {"ok": False, "status": STATUS_ERROR, "error": "market closed",
                        "oid": None, "avg_px": None, "filled_sz": None, "raw": None}
        self.executor().on_exit(pid, "ma20", 84400.0)
        self.assertGreaterEqual(self.executor()._churn_count("BTC", 1), 1,
                                "撤止损必须计入 churn")


class TestThresholdReadFromConfig(unittest.TestCase):
    """`hl_churn_max` 走既有 config 键模式：DB > env > 默认，脏值退回 5。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self):
        from app.config import Config
        return Config()

    def test_default_is_five(self):
        self.assertEqual(self._cfg().hl_churn_max, 5)

    def test_db_override_parsed(self):
        cfg = self._cfg()
        cfg.apply_db({"hl_churn_max": '"3"'})          # set_config 存的是 JSON 字符串
        self.assertEqual(cfg.hl_churn_max, 3)

    def test_out_of_range_rejected_keeps_previous(self):
        cfg = self._cfg()
        cfg.apply_db({"hl_churn_max": "100000"})       # 超出 _RANGES → 保留原值
        self.assertEqual(cfg.hl_churn_max, 5)

    def test_main_assembles_exec_params(self):
        """main._churn_max：脏值/缺键退回 5（不退回 0 —— 0 是「关掉保护」）。"""
        from app.main import _churn_max
        self.assertEqual(_churn_max(type("C", (), {"hl_churn_max": 3})()), 3)
        self.assertEqual(_churn_max(type("C", (), {"hl_churn_max": "7"})()), 7)
        self.assertEqual(_churn_max(type("C", (), {"hl_churn_max": "abc"})()), 5)
        self.assertEqual(_churn_max(type("C", (), {})()), 5)
        self.assertEqual(_churn_max(type("C", (), {"hl_churn_max": -2})()), 5)


if __name__ == "__main__":
    unittest.main()
