"""MR 腿（QQQ）在 HL 面板上的显示契约 —— 纯函数级，不触网、不需要 fastapi。

钉住（2026-11-02 方案 panel-design-20261102.md 第五节）：

1. 持仓：`_mr_view` 把 `status.mr` 归一成 入场/标记/浮盈%/离场线/距离场%/距入场；
2. 空仓但启用：视图仍存在，`holding=False`，状态文案按相位给；
3. 停用且空仓：`_mr_view` → None（整行不出现）；
4. 对账不平：状态「对账不平」；
5. 动态卡 `_mr_card_line`：含 QQQ 标记、RSI2、离场线、距离场与结论；
6. 缺字段渲染 `—`，不把 None 写成 0。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.hl import views  # noqa: E402


def mr_status(*, enabled=True, holding=False, recon="ok", phase="in_window",
              with_signal=True):
    st = {"enabled": enabled, "state": "holding" if holding else "flat",
          "recon": recon, "last_session_date": "2026-10-30", "mark": 697.0}
    if with_signal:
        st["signal"] = {
            "last_session_date": "2026-10-30", "rsi2": 8.48, "threshold": 10.0,
            "entry_flag": True, "exit_flag": False, "exit_line": 685.25,
            "sessions_usable": 15, "next_eval_et": "2026-11-03T16:00:00-05:00",
            "window": {"date": "2026-11-02", "start_et": "09:30", "deadline_et": "12:30"},
            "phase": phase,
        }
    if holding:
        st["pos"] = {
            "signal_date": "2026-10-30", "entry_session": "2026-11-02",
            "qty": 0.0088, "entry_price": 28580.0, "notional": 251.5,
            "margin": 50.3, "leverage": 5.0, "entry_ts": 1793640000.0,
            "upnl": 1.76, "funding_paid": None, "mark": 28780.0,
            "upnl_pct": 0.6998,
            "exit_line": 685.25 if with_signal else None,
            "dist_to_exit_pct": -97.61 if with_signal else None,
            "next_eval_et": "2026-11-03T16:00:00-05:00",
        }
    return st


class MrViewTests(unittest.TestCase):
    def test_holding_view(self) -> None:
        v = views._mr_view({"mr": mr_status(holding=True)})
        assert v is not None
        self.assertTrue(v["holding"])
        self.assertEqual(v["entry"], 28580.0)
        self.assertEqual(v["mark"], 28780.0)
        self.assertAlmostEqual(v["dist_to_entry_pct"], (28780 / 28580 - 1) * 100, places=6)
        self.assertEqual(v["exit_line"], 685.25)
        self.assertEqual(v["leverage"], 5.0)
        self.assertEqual(v["status"], "持仓中")

    def test_enabled_flat_keeps_view(self) -> None:
        v = views._mr_view({"mr": mr_status(phase="signal_ready")})
        assert v is not None
        self.assertFalse(v["holding"])
        self.assertEqual(v["status"], "信号已出，等 2026-11-02 开盘")
        self.assertEqual(v["rsi2"], 8.48)

    def test_disabled_flat_is_none(self) -> None:
        self.assertIsNone(views._mr_view({"mr": mr_status(enabled=False)}))

    def test_recon_err(self) -> None:
        v = views._mr_view({"mr": mr_status(holding=True, recon="err")})
        assert v is not None
        self.assertEqual(v["status"], "对账不平")

    def test_missing_fields_are_none_not_zero(self) -> None:
        v = views._mr_view({"mr": mr_status(holding=True, with_signal=False)})
        assert v is not None
        self.assertIsNone(v["exit_line"])
        self.assertIsNone(v["dist_to_exit_pct"])

    def test_card_line_contents(self) -> None:
        line = views._mr_card_line({"mr": mr_status(holding=True)})
        for token in ("QQQ", "MR", "距入场", "RSI2", "离场线", "距离场", "持仓中"):
            self.assertIn(token, line)

    def test_card_line_absent_when_disabled(self) -> None:
        self.assertEqual(views._mr_card_line({"mr": mr_status(enabled=False)}), "")


class MrPhaseTests(unittest.TestCase):
    def test_phases(self) -> None:
        cases = {"in_window": "窗口内等成交", "expired": "窗口已过，等明日",
                 "no_signal": "空仓", "no_data": "无数据"}
        for phase, text in cases.items():
            self.assertEqual(views._mr_phase_text(mr_status(phase=phase)), text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
