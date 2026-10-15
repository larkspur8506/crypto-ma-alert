"""第四轮复核 P3 回归测试（2026-10-15）：口径统一 + 彩排脚本落地。

覆盖：
P3-a  `orders_trusted` 缺字段时执行器按**不可信**处理（与 views/watchdog 同口径，
      fail-closed）——不据此自动补挂/平仓，只告警
P3-b  `scripts/hl_rehearse.py` 真的能跑：合成信号走完开仓 + 离场两条提醒、用临时库、
      零网络（不 import app.main 的 notifier），退出码 0
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import DB                                     # noqa: E402
from app.hl import MODE_LIVE                              # noqa: E402
from app.hl.executor import Executor, ExecParams          # noqa: E402

BAR = 1791912000000


def _snap(positions=(), orders=(), trusted=None):
    snap = {"ts": time.time(), "address": "0xabc", "equity": 0.0, "equity_total": 100.0,
            "spot_usdc": 100.0, "spot_hold": 0.0, "spot_ok": True, "unified": True,
            "withdrawable": 0.0, "margin_used": 1.0, "notional": 0.0,
            "positions": list(positions), "open_orders": list(orders)}
    if trusted is not None:
        snap["orders_trusted"] = trusted
    return snap


def _pos(coin="BTC", size=0.005, entry=81621.0, side=-1):
    return {"coin": coin, "side": side, "size": size, "entry_px": entry, "mark_px": entry,
            "position_value": size * entry, "liq_px": 0.0, "leverage": 5, "isolated": True,
            "margin_used": size * entry / 5, "unrealized_pnl": 0.0, "roe": 0.0}


class FakeInfo:
    def snapshot(self, address=None):
        return _snap()

    def specs(self, coins):
        return {c: {"sz_decimals": 5} for c in coins}


class FakeExchange:
    def __init__(self):
        self.calls = []

    def place_order(self, *a, **kw):
        self.calls.append(("order", a, kw))
        return {"ok": True, "status": "resting", "oid": 1, "avg_px": None,
                "filled_sz": None, "error": None, "raw": None}

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))

    def mid(self, coin):
        return 81621.0

    def slippage_limit(self, coin, is_buy, mid=None):
        return 81621.0


class TestOrdersTrustedDefaultFailClosed(unittest.TestCase):
    """P3-a：快照缺 orders_trusted 时不得据此动仓。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xk",
                            equity=1000.0, code_rev="audit4p3")
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=BAR, side=-1,
                                      entry_price=81603.0, stop_price=83945.44, stop_dist=0.0287,
                                      contracts=1, coin_qty=0.005, notional=408.0, risk_u=20.0,
                                      equity_at_entry=1000.0)
        self.db.set_pos_hl(pid, hl_status="open", hl_entry_oid=1, hl_stop_oid=99, hl_sz=0.005)
        self.db.confirm_position(pid, 81621.0, 0.005, 83945.44, True)
        self.pid = pid

    def tearDown(self):
        self.tmp.cleanup()

    def _exec(self):
        return Executor(self.db, self.p, FakeInfo(), self.ex)

    def test_missing_key_treated_as_untrusted(self):
        rec = self._exec().reconcile(_snap(positions=[_pos()], orders=[]))   # 无 orders_trusted
        self.assertEqual(rec["missing_stop"], [], "缺字段时不该判「有仓没止损」→ 不会自动补挂/平仓")
        self.assertTrue(rec["unconfirmed"], "应记为「无法确认止损」")
        self.assertEqual([c for c in self.ex.calls if c[0] == "order"], [],
                         "不可信数据下不得下发任何订单")

    def test_explicit_trusted_still_repairs(self):
        rec = self._exec().reconcile(_snap(positions=[_pos()], orders=[], trusted=True))
        self.assertEqual(len(rec["missing_stop"]), 1, "可信数据下仍要能发现裸奔仓位")


class TestRehearseScript(unittest.TestCase):
    """P3-b：彩排脚本必须真跑通（合成信号 → 开仓提醒 + 离场提醒），且零网络。"""

    def test_runs_end_to_end_offline(self):
        import importlib.util
        import re
        path = ROOT / "scripts" / "hl_rehearse.py"
        src = path.read_text(encoding="utf-8")
        # 只禁止**真的 import** 生产模块（注释/文档里提到 app.main 是说明，不算）
        self.assertFalse(re.search(r"^\s*(from|import)\s+app\.main", src, re.M),
                         "彩排脚本绝不能 import 生产 notifier")
        spec = importlib.util.spec_from_file_location("hl_rehearse", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from app import market
        original_fetch = market.fetch
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = mod.main()
        finally:
            market.fetch = original_fetch            # 还原 patch，避免影响其它测试
        out = buf.getvalue()
        self.assertEqual(rc, 0, f"彩排脚本退出码应为 0，输出：\n{out[-800:]}")
        self.assertIn("仅打印，未发网络", out)
        self.assertIn("开仓", out)
        self.assertIn("离场", out)
        self.assertIn("cma-rehearse-", out, "必须用独立临时库")
        self.assertIn("未发送任何网络推送", out)


if __name__ == "__main__":
    unittest.main()
