"""feat/p2-copy：positions 双口径（MEXC 人工口径 / HL 真实口径）读侧收敛。

背景：`positions` 一行里躺着两套量口径 —— 引擎建行写 MEXC 人工口径
（`contracts`=张数 / `notional` / `risk_u` / `equity_at_entry`=配置 equity），
而 HL live 实际按生效基数 + coin 数量执行（成交后由执行器写 `actual_*` 并把
`hl_status` 置为非空）。读侧统一走 `app.posfields`：

① 历史 MEXC 行：面板 / 心跳仍读原字段（**钉住**，防收敛改动误伤人工流）
② HL 仓行：面板信号表 + 确认页摘要读 actual_*（真实币量 / 真实成交价）
③ `update_fill` 对 HL 仓拒绝（确认页 400 + GET edit_open 400 + DB ValueError），
   MEXC 仓的「修改开仓记录」照旧可用
④ 执行器在成交确认点把**实际生效基数**写回 `positions.equity_at_entry`
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, posfields                                          # noqa: E402
from app.db import DB                                                      # noqa: E402
from app.hl import MODE_LIVE                                               # noqa: E402
from app.hl.executor import Executor, ExecParams                           # noqa: E402
from tests.test_hl_executor import (FakeExchange, FakeInfo, _snap)          # noqa: E402


# ---------------------------------------------------------------- 测试 ① / ② 面板

class TestPanelSignalTable(unittest.TestCase):
    """面板「信号」表的数量口径（app/hl/views.py + app/posfields.py）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def payload(self):
        from app.hl.views import DashboardDeps, status_payload
        deps = DashboardDeps(
            db=self.db,
            config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT"]})(),
            info=None, address_fn=lambda: "", mode_fn=lambda: "live")
        return status_payload(deps)

    def mexc_row(self):
        """历史 MEXC 人工行：只有人工口径（hl_status 为空）。"""
        pid = self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=1_700_000_000_000, status="open", side=1,
            entry_price=100_000.0, stop_price=97_000.0, stop_dist=0.03, contracts=6,
            coin_qty=0.0006, notional=60.0, risk_u=18.0, equity_at_entry=1000.0)
        self.db.confirm_position(pid, 100_000.0, 6, 97_000.0, True)      # 人工确认流
        return pid

    def hl_row(self, actual_contracts=0.00084):
        """HL 执行器管的行：建行写 MEXC 口径，成交后写 actual_*（币量）。"""
        pid = self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=1_700_000_100_000, status="open", side=-1,
            entry_price=81_812.8, stop_price=84_060.0, stop_dist=0.02747,
            contracts=85, coin_qty=0.0085, notional=695.4, risk_u=19.1,
            equity_at_entry=1000.0)
        self.db.set_pos_hl(pid, hl_status="open", hl_stop_oid=7, hl_sz=actual_contracts)
        self.db.confirm_position(pid, 81_621.0, actual_contracts, 83_945.0, True,
                                 equity_at_entry=100.0)
        return pid

    # ---- ① 历史 MEXC 行读原字段（钉住不变）

    def test_mexc_row_keeps_raw_fields(self):
        self.mexc_row()
        sig = self.payload()["signals"][0]
        self.assertEqual(sig["contracts"], 6)                 # 原始张数保留（兼容字段）
        self.assertEqual(sig["qty"], 6)                       # 统一读侧：MEXC 行 = 张数
        self.assertEqual(sig["qty_unit"], "张")
        self.assertAlmostEqual(sig["entry"], 100_000.0)
        self.assertAlmostEqual(sig["stop"], 97_000.0)

    def test_mexc_row_panel_html_unchanged(self):
        """渲染层钉住：MEXC 行的数量格仍是原来的 6.00。"""
        from app.hl.views import _render_body
        self.mexc_row()
        html = _render_body(self.payload())
        self.assertIn("<h2>信号</h2>", html)
        seg = html.split("<h2>信号</h2>", 1)[1].split("<h2>", 1)[0]
        self.assertIn(">6.00<", seg, "MEXC 行的数量列应仍显示 6.00")

    # ---- ② HL 仓读 actual_*

    def test_hl_row_uses_actual_fields(self):
        self.hl_row()
        sig = self.payload()["signals"][0]
        self.assertEqual(sig["contracts"], 85, "原始张数仍在（兼容字段）")
        self.assertAlmostEqual(sig["qty"], 0.00084, msg="HL 仓数量必须是真实币量")
        self.assertEqual(sig["qty_unit"], "币量")
        self.assertAlmostEqual(sig["entry"], 81_621.0, msg="HL 仓入场价读 actual_entry")
        self.assertAlmostEqual(sig["stop"], 83_945.0, msg="HL 仓止损读 actual_stop")

    def test_hl_row_panel_html_shows_coin_qty(self):
        from app.hl.views import _render_body
        self.hl_row()
        html = _render_body(self.payload())
        seg = html.split("<h2>信号</h2>", 1)[1].split("<h2>", 1)[0]
        self.assertIn(">0.00084<", seg, "HL 仓数量列应显示真实币量")
        self.assertNotIn(">85.00<", seg, "HL 仓不能显示 MEXC 口径张数")

    # ---- posfields 单元语义（双口径判定的唯一来源）

    def test_posfields_dual_book_semantics(self):
        mexc = self.db.position_by_id(self.mexc_row())
        hl = self.db.position_by_id(self.hl_row())
        self.assertFalse(posfields.is_hl(mexc))
        self.assertTrue(posfields.is_hl(hl))
        self.assertEqual(posfields.qty(mexc), 6)
        self.assertAlmostEqual(posfields.qty(hl), 0.00084)
        self.assertAlmostEqual(posfields.notional(mexc), 60.0)
        self.assertAlmostEqual(posfields.notional(hl), 0.00084 * 81_621.0)
        self.assertAlmostEqual(posfields.risk_u(mexc), 18.0)
        self.assertAlmostEqual(posfields.risk_u(hl), 0.00084 * abs(83_945.0 - 81_621.0))
        self.assertAlmostEqual(posfields.equity_base(mexc), 1000.0)
        self.assertAlmostEqual(posfields.equity_base(hl), 100.0)      # 实际生效基数
        # 真实行情下 posfields 只读不写
        self.assertEqual(self.db.position_by_id(hl["id"])["contracts"], 85)


# ---------------------------------------------------------------- 测试 ① / ② / ③ 确认页

class TestConfirmPageDualBook(unittest.TestCase):
    """确认页（app/main.py）：HL 仓读 actual_* + 修改入口收口。"""

    def setUp(self):
        try:
            import fastapi                                              # noqa: F401
            from fastapi.testclient import TestClient
        except ImportError:                                            # pragma: no cover
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        self.tmp = tempfile.mkdtemp()
        os.environ["DB_PATH"] = os.path.join(self.tmp, "confirm.db")
        os.environ["ENABLE_SCHEDULER"] = "false"
        os.environ["ADMIN_TOKEN"] = "dual-admin"
        os.environ["TOKEN_SECRET"] = "dual-secret"
        os.environ["BASE_URL"] = "https://dual.example"
        os.environ["DRY_RUN"] = "true"
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        self.mod = mod
        self.TestClient = TestClient

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mexc_open(self, bar_ms=1_700_000_200_000):
        pid = self.mod.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=bar_ms, status="open", side=1,
            entry_price=100_000.0, stop_price=97_000.0, stop_dist=0.03, contracts=6,
            coin_qty=0.0006, notional=60.0, risk_u=18.0, equity_at_entry=1000.0)
        self.mod.db.confirm_position(pid, 100_000.0, 6, 97_000.0, True)
        return pid

    def _hl_closed(self, bar_ms=1_700_000_300_000):
        pid = self.mod.db.create_position(
            symbol="ETH_USDT", signal_bar_ms=bar_ms, status="open", side=1,
            entry_price=3_000.0, stop_price=2_900.0, stop_dist=0.0333, contracts=400,
            coin_qty=4.0, notional=12_000.0, risk_u=40.0, equity_at_entry=1000.0)
        self.mod.db.set_pos_hl(pid, hl_status="open", hl_stop_oid=9, hl_sz=0.031)
        self.mod.db.confirm_position(pid, 3_010.0, 0.031, 2_910.0, True,
                                     equity_at_entry=100.0)
        return pid

    def _token(self, pid, action):
        return self.mod.engine.sign_token(self.mod.config.token_secret, pid, action)

    # ---- ② HL 仓摘要页读 actual_*

    def test_hl_summary_shows_actual_and_hides_edit(self):
        pid = self._hl_closed()
        with self.TestClient(self.mod.app) as c:
            r = c.get(f"/c/{pid}", params={"a": "view", "t": self._token(pid, "view")})
        self.assertEqual(r.status_code, 200)
        self.assertIn("3,010.00", r.text)                     # actual_entry
        self.assertIn("2,910.00", r.text)                     # actual_stop
        self.assertIn("0.031 币量", r.text)                    # actual_contracts
        self.assertNotIn("400 币量", r.text, "不能显示 MEXC 张数")
        self.assertNotIn("修改开仓记录", r.text, "HL 仓必须隐藏修改开仓入口")
        self.assertIn("hl_fills", r.text, "要有去向面板核对的引导")

    def test_mexc_summary_keeps_edit_button(self):
        pid = self._mexc_open()
        with self.TestClient(self.mod.app) as c:
            r = c.get(f"/c/{pid}", params={"a": "view", "t": self._token(pid, "view")})
        self.assertEqual(r.status_code, 200)
        self.assertIn("6 张", r.text)
        self.assertIn("修改开仓记录", r.text, "MEXC 人工仓的修改入口必须保留")

    # ---- ③ update_fill 对 HL 仓收口

    def test_hl_edit_form_and_post_rejected(self):
        pid = self._hl_closed()
        tok_open = self._token(pid, "edit_open")
        with self.TestClient(self.mod.app) as c:
            r = c.get(f"/c/{pid}", params={"a": "edit_open", "t": tok_open})
            self.assertEqual(r.status_code, 400, "HL 仓不应给出修改开仓表单")
            r2 = c.post(f"/c/{pid}", data={"a": "open", "edit": "1",
                                           "t": self._token(pid, "open"),
                                           "actual_entry": "3999", "actual_stop": "3800",
                                           "actual_contracts": "999"})
            self.assertEqual(r2.status_code, 400, "HL 仓的修改开仓 POST 必须 400")
        row = self.mod.db.position_by_id(pid)
        self.assertAlmostEqual(row["actual_entry"], 3_010.0, msg="HL 仓数据不得被写脏")
        self.assertAlmostEqual(row["actual_contracts"], 0.031)
        self.assertAlmostEqual(row["equity_at_entry"], 100.0)

    def test_hl_update_fill_db_layer_raises(self):
        """db 层双保险：直接调 update_fill 也要拒绝（路由绕过也拦得住）。"""
        pid = self._hl_closed()
        with self.assertRaises(ValueError):
            self.mod.db.update_fill(pid, 3_999.0, 999, 3_800.0, True)
        self.assertAlmostEqual(self.mod.db.position_by_id(pid)["actual_entry"], 3_010.0)

    def test_mexc_edit_still_works(self):
        """MEXC 人工仓的「修改开仓记录」照旧可用（不能被双口径改动误伤）。"""
        pid = self._mexc_open()
        with self.TestClient(self.mod.app) as c:
            r = c.post(f"/c/{pid}", data={"a": "open", "edit": "1",
                                          "t": self._token(pid, "open"),
                                          "actual_entry": "101000", "actual_stop": "98000",
                                          "actual_contracts": "6"})
            self.assertEqual(r.status_code, 200)
        row = self.mod.db.position_by_id(pid)
        self.assertAlmostEqual(row["actual_entry"], 101_000.0)
        self.assertAlmostEqual(row["actual_stop"], 98_000.0)

    # ---- ② 心跳文案（engine 的 positions 量字段读者）

    def test_heartbeat_dual_book(self):
        cfg = type("C", (), {"alert_prefix": "", "symbols": ["BTC_USDT", "ETH_USDT"]})()
        self._mexc_open()
        txt = engine.heartbeat_text(self.mod.db, cfg)
        self.assertIn("BTC_USDT: 持仓中 6 张 @", txt, "MEXC 行心跳文案不变")
        pid = self._hl_closed()
        txt = engine.heartbeat_text(self.mod.db, cfg)
        self.assertIn("ETH_USDT: 持仓中 0.031 币量 @ 3,010", txt)
        self.assertNotIn("400 张", txt, "HL 仓不能按 MEXC 张数报")

    # ---- review_fill 的下单基数来自 equity_at_entry

    def test_review_fill_uses_row_equity_base(self):
        pid = self._hl_closed()
        row = self.mod.db.position_by_id(pid)
        base = posfields.equity_base(row, self.mod.config.equity)
        self.assertAlmostEqual(base, 100.0)
        m_cfg = self.mod.current_config()
        rev = engine.review_fill(m_cfg, row["symbol"], row["entry_price"], row["contracts"],
                                 3_010.0, 2_910.0, 0.031, equity=base)
        self.assertAlmostEqual(rev["equity"], 100.0, msg="体检基数必须是实际生效基数")
        self.assertAlmostEqual(rev["target_risk_u"], 100.0 * m_cfg.risk_pct)


# ---------------------------------------------------------------- 测试 ④ equity 写回

class TestEquityWritebackOnEntry(unittest.TestCase):
    """执行器成交确认点把**实际生效基数**写回 positions.equity_at_entry。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="testrev")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pid(self):
        return self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=1_791_912_000_000, status="pending", side=1,
            entry_price=83_000.0, stop_price=80_600.0, stop_dist=0.0289, contracts=1,
            coin_qty=0.001, notional=83.0, risk_u=20.0, equity_at_entry=1000.0)

    def test_entry_writes_back_effective_equity(self):
        info = FakeInfo(snap=_snap(equity=137.0))                # 实时权益 137U
        pid = self._pid()
        ex = Executor(self.db, self.p, info, FakeExchange())
        self.assertTrue(ex.on_entry(pid), "live 下开仓成功应返回 True（已接管）")
        row = self.db.position_by_id(pid)
        self.assertEqual(row["hl_status"], "open")
        # floor(137/10)*10 = 130 ≠ 配置的 1000 → 写回的必须是生效基数
        self.assertAlmostEqual(row["equity_at_entry"], 130.0,
                               msg="equity_at_entry 必须写回实际生效基数（配置 1000 不算数）")
        self.assertAlmostEqual(row["actual_contracts"], row["hl_sz"])
        self.assertLess(abs(row["actual_entry"] - info._mids["BTC"]) / info._mids["BTC"], 0.005,
                        "成交价应落在 mid 附近的 IOC 滑点带内")

    def test_dry_mode_does_not_touch_position(self):
        """dry 不下单 → 不确认、不写回（基数仍是建行时的配置值）。"""
        from app.hl import MODE_DRY
        self.p.mode = MODE_DRY
        pid = self._pid()
        ex = Executor(self.db, self.p, FakeInfo(snap=_snap(equity=137.0)), FakeExchange())
        self.assertFalse(ex.on_entry(pid))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "pending")
        self.assertAlmostEqual(row["equity_at_entry"], 1000.0)

    def test_confirm_position_without_equity_keeps_old_value(self):
        """不传 equity_at_entry = 行为不变（MEXC 人工确认流走这条）。"""
        pid = self._pid()
        self.db.confirm_position(pid, 83_000.0, 1, 80_600.0, True)
        row = self.db.position_by_id(pid)
        self.assertAlmostEqual(row["equity_at_entry"], 1000.0)
        self.assertEqual(row["status"], "open")


if __name__ == "__main__":                                                 # pragma: no cover
    unittest.main()
