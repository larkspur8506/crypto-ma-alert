"""HL 侧对抗审查回归（2026-10-30，第二波：agent2api 审查同期的 HL 执行器复查）。

- P2：flatten_all 部分撤单失败 → 整轮重试（fail-closed）；重试耗尽 → ok=False。
  修复前：部分撤单失败被静默吞掉，用户收到谎报「已撤掉全部挂单」的 ✅，
  孤儿 reduce-only 触发单留在交易所（会咬下一笔同标的开仓）。
- P3：清仓关账补清宽限键 + 补算 net_pnl（book_pnl 回调）。

复现脚本（修复前实锤）：/opt/data/backups/cma-review/repro_a_flatten_orphan.py
"""
import tempfile
import unittest
from pathlib import Path

from tests.test_agent_expiry import Base as AgentBase
from tests.test_agent_expiry import FakeExchange, FakeInfo, _pos

from app.hl import agent as A


class FlattenPartialCancelTests(AgentBase):
    def _local(self):
        pid = self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=0, side=1, entry_price=83000.0,
            stop_price=80600.0, stop_dist=0.03, contracts=1, coin_qty=0.01,
            notional=830.0, risk_u=20.0, equity_at_entry=1000.0)
        self.db.set_pos_hl(int(pid), hl_status="open", hl_sz=0.01)
        return int(pid)

    def test_partial_cancel_failure_retries_and_succeeds(self):
        """第一轮有 1 张撤单失败（单还活着）→ 整轮重试，第二轮干净 → ok=True。"""
        pid = self._local()
        info = FakeInfo(snap={"positions": [_pos("BTC", 0.01, 1, 83000.0)],
                              "open_orders": []})

        class Flaky(FakeExchange):
            def __init__(self, info=None):
                super().__init__(info=info)
                self.cancel_calls = 0

            def cancel_all_orders(self):
                self.cancel_calls += 1
                res = super().cancel_all_orders()
                if self.cancel_calls == 1:
                    res = dict(res)
                    res["raw"] = {"oids": [], "failed": [
                        {"oid": 12, "coin": "BTC", "error": "撤单异常：TimeoutError"}]}
                return res

        ex = Flaky(info=info)
        res = A.flatten_all(self.db, self.params, info, ex, self.notifier)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["cancel_failed"], [])
        self.assertEqual(ex.cancel_calls, 2, "部分撤单失败必须整轮重试")
        self.assertEqual(self.db.position_by_id(pid)["status"], "closed")

    def test_persistent_partial_cancel_failure_is_not_reported_as_success(self):
        """撤单一直撤不干净 → ok=False（走 cleanup_fail 告警），且**不许平仓**——
        平了仓留孤儿触发单 + ✅ 谎报，正是修复前实锤的危害。"""
        pid = self._local()
        info = FakeInfo(snap={"positions": [_pos("BTC", 0.01, 1, 83000.0)],
                              "open_orders": []})

        class AlwaysFlaky(FakeExchange):
            def cancel_all_orders(self):
                return {"ok": True, "status": "resting", "error": None,
                        "raw": {"oids": [], "failed": [
                            {"oid": 12, "coin": "BTC", "error": "撤单异常：TimeoutError"}]}}

        ex = AlwaysFlaky(info=info)
        res = A.flatten_all(self.db, self.params, info, ex, self.notifier)
        self.assertFalse(res["ok"])
        self.assertTrue(res["cancel_failed"])
        self.assertIn("撤单部分失败", res["error"])
        self.assertEqual(self.db.position_by_id(pid)["status"], "pending",
                         "撤不干净就不许平仓（本地行也不许动）")


class CleanupBookkeepingTests(AgentBase):
    def test_close_local_clears_grace_key_and_books_pnl(self):
        """P3：清仓关账（db 直写路径）要补清宽限键 + 补算流水口径 net_pnl。"""
        pid = self._local()
        grace_key = f"hl_close_grace:{pid}"
        self.db.set_config(grace_key, "1:1:0")
        booked_pnl = []
        n = A._close_local_positions(self.db, self.params, still_on_exchange=set(),
                                     prices={"BTC": 83000.0}, ev=None,
                                     book_pnl=booked_pnl.append)
        self.assertEqual(n, 1)
        self.assertEqual(booked_pnl, [pid], "net_pnl 补算回调必须被调用")
        self.assertEqual(self.db.get_config_str(grace_key, ""), "")

    def _local(self):
        pid = self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=0, side=1, entry_price=83000.0,
            stop_price=80600.0, stop_dist=0.03, contracts=1, coin_qty=0.01,
            notional=830.0, risk_u=20.0, equity_at_entry=1000.0)
        self.db.set_pos_hl(int(pid), hl_status="open", hl_sz=0.01)
        return int(pid)


if __name__ == "__main__":                                     # pragma: no cover
    unittest.main()
