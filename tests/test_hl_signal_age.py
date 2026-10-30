"""信号时效闸（feat/signal-age-gate）：只允许在信号 bar 收盘后 N 分钟内建仓。

与 Lighter 侧同一份策略、同一个闸门。这里覆盖四件事：
- 超龄 bar（收盘 210 分钟）→ 开仓被拒，落一条 skipped 意图 + 事件里写明拒绝理由；
- 新鲜 bar（收盘 5 分钟）→ 照常开仓（入场 + 止损两张单）；
- `max_signal_age_min=0` → 永不拦（连 210 分钟也放行）；
- 边界闭区间：正好 30 分钟放行、31 分钟拦；以及 env 原值解析（空/`0`/脏值/负数）。

时间用 `time.time` 打桩固定（`NOW`），否则「正好 30 分钟」这类边界用例会被真实的
几毫秒延迟判成 30.0x → 偶发红灯。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.db import DB
from app.hl import MODE_DRY, MODE_LIVE
from app.hl.executor import (SIGNAL_AGE_DEFAULT_MIN, ExecParams, Executor,
                             parse_max_signal_age_min)

from tests.test_hl_executor import FakeExchange, FakeInfo, FakeNotifier

BAR_MS = 4 * 3600 * 1000
NOW = 1_800_000_000.0            # 固定时钟（与用例无关，只要求做减法时一致）


def bar_for_age_minutes(minutes: float, now: float = NOW) -> int:
    """反解出一根「收盘于 now − minutes」的 4h bar 的开盘时刻（毫秒）。

    年龄 = now − (bar 开盘 + 4h)，所以 bar 开盘 = now − 年龄 − 4h。
    """
    return int((now - minutes * 60.0) * 1000) - BAR_MS


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="testrev")

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self):
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def add_signal(self, bar_ms: int, symbol="BTC_USDT", side=1,
                   entry=83000.0, stop=80600.0) -> int:
        return self.db.create_position(symbol=symbol, signal_bar_ms=bar_ms, side=side,
                                       entry_price=entry, stop_price=stop,
                                       stop_dist=abs(entry - stop) / entry, contracts=1,
                                       coin_qty=0.001, notional=entry * 0.001,
                                       risk_u=20.0, equity_at_entry=1000.0)

    def orders(self, purpose=None):
        return [r for r in self.db.hl_recent_orders(50)
                if purpose is None or r["purpose"] == purpose]

    def events(self):
        """DB 语义：最新在前。"""
        return [dict(r) for r in self.db.hl_recent_events(50)]

    def newest_event(self):
        evs = self.events()
        self.assertTrue(evs, "没有任何事件")
        return evs[0]

    def order_calls(self):
        return [c for c in self.ex.calls if c[0] == "order"]

    def enter_at(self, minutes: float, now: float = NOW, **params):
        """在「信号 bar 收盘于 now − minutes」的条件下跑一次开仓，返回仓位 id。"""
        for k, v in params.items():
            setattr(self.p, k, v)
        pid = self.add_signal(bar_for_age_minutes(minutes, now))
        with mock.patch("time.time", return_value=now):
            self.executor().on_entry(pid)
        return pid


class TestParseKnob(unittest.TestCase):
    """env 原值 → 分钟数：空/脏值/负数一律退回 30（不静默关保护），`0` 才是关闭。"""

    def test_default_is_30(self):
        self.assertEqual(SIGNAL_AGE_DEFAULT_MIN, 30)
        self.assertEqual(ExecParams().max_signal_age_min, 30)

    def test_empty_falls_back_to_default(self):
        self.assertEqual(parse_max_signal_age_min(""), 30)
        self.assertEqual(parse_max_signal_age_min("   "), 30)
        self.assertEqual(parse_max_signal_age_min(None), 30)

    def test_zero_means_off(self):
        self.assertEqual(parse_max_signal_age_min("0"), 0)

    def test_dirty_falls_back_to_default(self):
        self.assertEqual(parse_max_signal_age_min("abc"), 30)
        self.assertEqual(parse_max_signal_age_min("nan"), 30)
        self.assertEqual(parse_max_signal_age_min("-5"), 30)

    def test_valid_value_and_float_string(self):
        self.assertEqual(parse_max_signal_age_min("45"), 45)
        self.assertEqual(parse_max_signal_age_min(" 45 "), 45)
        self.assertEqual(parse_max_signal_age_min('"20"'), 20)
        self.assertEqual(parse_max_signal_age_min("30.9"), 30)

    def test_infinite_falls_back_to_default(self):
        """'inf'/'1e999'：float 解析得了、int() 转 OverflowError —— 按读坏处理退回 30。

        2026-10-30 复审补的边角：原实现只捕 TypeError/ValueError，一个
        HL_MAX_SIGNAL_AGE_MIN=inf 会把 hl_params 装配链路整个崩掉。
        """
        self.assertEqual(parse_max_signal_age_min("inf"), 30)
        self.assertEqual(parse_max_signal_age_min("1e999"), 30)
        self.assertEqual(parse_max_signal_age_min("-inf"), 30)


class TestSignalAgeGate(Base):
    def test_stale_bar_blocks_entry(self):
        self.enter_at(210)
        skipped = [r for r in self.orders("entry") if r["status"] == "skipped"]
        self.assertEqual(len(skipped), 1, "超龄信号必须落一条 skipped 意图")
        msg = self.newest_event()["message"]
        self.assertIn("拒绝追单", msg)
        self.assertIn("210", msg)          # 实测年龄写进理由，便于复盘
        self.assertEqual(self.order_calls(), [], "超龄信号不许向交易所发任何单")

    def test_fresh_bar_enters(self):
        self.enter_at(5)
        self.assertEqual(self.orders("entry")[0]["status"], "filled")
        self.assertEqual(len(self.order_calls()), 2)          # 入场 + 止损
        self.assertIn("已开仓", self.notifier.messages[-1])

    def test_zero_disables_gate(self):
        self.enter_at(210, max_signal_age_min=0)
        self.assertEqual(self.orders("entry")[0]["status"], "filled")
        self.assertFalse([e for e in self.events() if "拒绝追单" in str(e["message"])])

    def test_dry_gate_rejection_still_alerts(self):
        """dry 影子也必须把「实盘会被拒」推出来（2026-10-30 复审 P3）。

        旧实现只在 live 下 _alert：影子期闸门拒绝是静默的（事件落了库但零推送），
        会误以为这笔放行了。不发人工卡是有意的——实盘同样被拒，没有人工动作可做。
        """
        self.enter_at(210, mode=MODE_DRY)
        self.assertTrue(any("闸门拒绝" in m for m in self.notifier.messages),
                         "dry 下闸门拒绝也要推送")
        self.assertFalse(self.order_calls())

    def test_boundary_exactly_30_allowed(self):
        self.enter_at(30)
        self.assertEqual(self.orders("entry")[0]["status"], "filled",
                         "闭区间：正好 30 分钟要放行")

    def test_boundary_31_blocked(self):
        self.enter_at(31)
        self.assertEqual(self.orders("entry")[0]["status"], "skipped")
        self.assertIn("拒绝追单", self.newest_event()["message"])

    def test_custom_limit_larger_passes(self):
        self.enter_at(45, max_signal_age_min=60)
        self.assertEqual(self.orders("entry")[0]["status"], "filled")

    def test_custom_limit_smaller_blocks(self):
        self.enter_at(45, max_signal_age_min=15)
        self.assertEqual(self.orders("entry")[0]["status"], "skipped")

    def test_exit_not_blocked_by_stale_signal(self):
        """闸门只拦开仓：信号过期了，离场/止损该走还得走（绝不能延后）。"""
        pid = self.enter_at(5)
        row = self.db.position_by_id(pid)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "open")
        self.ex.calls.clear()
        with mock.patch("time.time", return_value=NOW + 5 * 3600):    # 5 小时后再离场
            self.assertTrue(self.executor().on_exit(pid, "ma20", 84500.0))
        self.assertTrue(any(c[0] == "order" for c in self.ex.calls),
                        "离场不许被时效闸拦")


if __name__ == "__main__":
    unittest.main()
