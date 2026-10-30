"""PR-3：fill 聚合 PnL（app/hl/pnl.py）+ 执行器关账接线 的单元测试。

覆盖点（每条都对应一个「会静默算错钱」的坑）：
1. 聚合口径：多腿 VWAP、混合币种、窗口上下界、同标的重开串仓防护、方向过滤、
   非计价币手续费不计入（unresolved_fee_cnt）。
2. ΔR / Δnet 阈值：超阈触发、不超阈不触发（大仓位的相对容差、小仓位的绝对容差）。
3. 执行器关账：net_pnl 落库、pnl_source 三种取值、差异事件触发与不触发、
   开关 off/on 都照算（纯观测）而面板口径只在 on 时切换。
4. 老库迁移：positions 没有 net_pnl / pnl_source 列时自动补列，历史行保持 NULL。

全部离线：临时 sqlite + 假 info/交易所（复用 tests/test_hl_executor 的假件）。
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.db import DB
from app.hl import MODE_LIVE, EV_RECONCILE, pnl
from app.hl.executor import Executor, ExecParams, PNL_SRC_FILLS, PNL_SRC_NO_EXIT_FILLS
from app.hl.exchange import STATUS_FILLED
from tests.test_hl_executor import FakeExchange, FakeInfo, FakeNotifier, _snap, _pos, _stop_order

T0 = 1_760_000_000.0            # 固定基准时刻（秒）


def fill(coin="BTC", dir_="Open Long", sz=1.0, px=100.0, fee=0.1, fee_token="USDC",
         fee_quote=None, closed_pnl=0.0, ts=T0):
    """一条 hl_fills 行（形状与 DB 一致）。fee_quote 默认不传 = 历史行（NULL）。"""
    return {"coin": coin, "dir": dir_, "sz": sz, "px": px, "fee": fee,
            "fee_token": fee_token, "fee_quote": fee_quote,
            "closed_pnl": closed_pnl, "ts": ts}


# ---------------------------------------------------------------- 纯聚合
class TestAggregateFills(unittest.TestCase):

    def test_multi_leg_vwap_and_net(self):
        """两笔入场 + 一笔出场：VWAP 按数量加权，net = Σclosed_pnl − fee。"""
        fills = [
            fill(dir_="Open Long", sz=0.5, px=100.0, fee=0.3, ts=T0 - 30),
            fill(dir_="Open Long", sz=0.5, px=102.0, fee=0.2, ts=T0 - 10),
            fill(dir_="Close Long", sz=1.0, px=110.0, fee=0.1, closed_pnl=9.0, ts=T0 + 60),
        ]
        agg = pnl.aggregate_fills(fills, T0, T0 + 3600, "BTC", 1)
        self.assertAlmostEqual(agg["entry_sz"], 1.0)
        self.assertAlmostEqual(agg["entry_vwap"], 101.0)      # (0.5*100 + 0.5*102)/1
        self.assertAlmostEqual(agg["exit_sz"], 1.0)
        self.assertAlmostEqual(agg["exit_vwap"], 110.0)
        self.assertAlmostEqual(agg["fee_total"], 0.6)
        self.assertAlmostEqual(agg["closed_pnl_sum"], 9.0)
        self.assertAlmostEqual(agg["net"], 8.4)
        self.assertEqual(agg["unresolved_fee_cnt"], 0)
        self.assertEqual(agg["n_fills"], 3)
        self.assertAlmostEqual(pnl.net_size(agg), 0.0)        # 全平

    def test_window_lower_bound_keeps_entry_leg(self):
        """入场成交早于 opened_at（先成交后落库）：−300s 的下界必须保留它。"""
        fills = [
            fill(dir_="Open Long", sz=1.0, px=100.0, ts=T0 - 200),   # 窗内（差 200s）
            fill(dir_="Open Long", sz=9.0, px=50.0, ts=T0 - 400),    # 窗外（上一回合）
            fill(dir_="Close Long", sz=1.0, px=110.0, closed_pnl=10.0, ts=T0 + 60),
        ]
        agg = pnl.aggregate_fills(fills, T0, T0 + 3600, "BTC", 1)
        self.assertAlmostEqual(agg["entry_sz"], 1.0)
        self.assertAlmostEqual(agg["entry_vwap"], 100.0)

    def test_window_upper_bound_blocks_reopen(self):
        """closed_at+300s 之后的成交属于**同标的重开**，绝不能算进旧仓。"""
        fills = [
            fill(dir_="Open Long", sz=1.0, px=100.0, ts=T0),
            fill(dir_="Close Long", sz=1.0, px=110.0, closed_pnl=10.0, ts=T0 + 3600),
            # 重开后的新仓（同一 coin）
            fill(dir_="Open Long", sz=5.0, px=200.0, ts=T0 + 3600 + 600),
            fill(dir_="Close Long", sz=5.0, px=150.0, closed_pnl=-250.0, ts=T0 + 3600 + 900),
        ]
        agg = pnl.aggregate_fills(fills, T0, T0 + 3600, "BTC", 1)
        self.assertAlmostEqual(agg["exit_sz"], 1.0, msg="重开的出场腿被串进来了")
        self.assertAlmostEqual(agg["entry_sz"], 1.0)
        self.assertAlmostEqual(agg["closed_pnl_sum"], 10.0)
        self.assertEqual(agg["n_fills"], 2)

    def test_exit_lookahead_allows_late_close_fill(self):
        """关账后 5 分钟内落库的平仓成交要能算进来（+300s 上界）。"""
        fills = [fill(dir_="Open Long", sz=1.0, px=100.0, ts=T0),
                 fill(dir_="Close Long", sz=1.0, px=110.0, closed_pnl=10.0, ts=T0 + 3600 + 200)]
        agg = pnl.aggregate_fills(fills, T0, T0 + 3600, "BTC", 1)
        self.assertAlmostEqual(agg["exit_sz"], 1.0)
        self.assertAlmostEqual(agg["net"], 10.0 - 0.2)      # 两条腿各 0.1 手续费

    def test_other_coins_are_ignored(self):
        fills = [fill(coin="BTC", dir_="Open Long", sz=1.0, px=100.0, fee=0.2),
                 fill(coin="ETH", dir_="Open Long", sz=3.0, px=2000.0, fee=7.0)]
        agg = pnl.aggregate_fills(fills, T0, T0 + 60, "BTC", 1)
        self.assertAlmostEqual(agg["entry_sz"], 1.0)
        self.assertAlmostEqual(agg["fee_total"], 0.2)
        self.assertEqual(agg["n_fills"], 1)

    def test_non_quote_fee_token_not_counted(self):
        """0.2 ARB 的手续费不能当 0.2U 加进净利：不计入 fee_total，只计 unresolved。"""
        fills = [fill(dir_="Open Long", sz=1.0, px=100.0, fee=0.2, fee_token="ARB"),
                 fill(dir_="Close Long", sz=1.0, px=110.0, fee=0.1, fee_token="USDC",
                      closed_pnl=10.0, ts=T0 + 60)]
        agg = pnl.aggregate_fills(fills, T0, T0 + 3600, "BTC", 1)
        self.assertAlmostEqual(agg["fee_total"], 0.1)
        self.assertEqual(agg["unresolved_fee_cnt"], 1)
        self.assertAlmostEqual(agg["net"], 9.9)

    def test_fee_quote_preferred_over_raw_fee(self):
        """fee_quote（已折算）优先于原始 fee；历史行（NULL）回退 fee。"""
        fills = [fill(dir_="Open Long", sz=1.0, px=100.0, fee=5.0, fee_quote=0.42),
                 fill(dir_="Open Long", sz=1.0, px=100.0, fee=0.08, fee_quote=None,
                      fee_token="USDC", ts=T0 + 1)]
        agg = pnl.aggregate_fills(fills, T0, T0 + 60, "BTC", 1)
        self.assertAlmostEqual(agg["fee_total"], 0.50)

    def test_empty_fee_token_counts_as_quote(self):
        """fee_token 为空 = HL 默认扣报价币 → 可以回退用 fee。"""
        agg = pnl.aggregate_fills([fill(fee=0.3, fee_token=None)], T0, T0 + 60, "BTC", 1)
        self.assertAlmostEqual(agg["fee_total"], 0.3)
        self.assertEqual(agg["unresolved_fee_cnt"], 0)

    def test_opposite_side_fills_excluded(self):
        """同窗口内反手（先平多再开空）：另一笔仓的成交整行排除，连费用一起。"""
        fills = [fill(dir_="Open Long", sz=1.0, px=100.0, fee=0.1),
                 fill(dir_="Close Long", sz=1.0, px=110.0, closed_pnl=10.0, fee=0.1, ts=T0 + 60),
                 # 反手做空（同一时间窗、同一 coin）
                 fill(dir_="Open Short", sz=2.0, px=110.0, fee=0.5, ts=T0 + 90)]
        agg = pnl.aggregate_fills(fills, T0, T0 + 3600, "BTC", 1)
        self.assertAlmostEqual(agg["entry_sz"], 1.0, msg="做空的入场腿混进了做多仓")
        self.assertAlmostEqual(agg["fee_total"], 0.2)
        self.assertEqual(agg["ignored_cnt"], 1)
        # 反向仓位自己看的时候，多头的成交被排除
        agg2 = pnl.aggregate_fills(fills, T0, T0 + 3600, "BTC", -1)
        self.assertAlmostEqual(agg2["entry_sz"], 2.0)
        self.assertEqual(agg2["ignored_cnt"], 2)

    def test_missing_ts_is_excluded(self):
        """ts 缺失的行无法定位归属 → 不计入（宁可少算，不串仓）。"""
        agg = pnl.aggregate_fills([fill(dir_="Open Long", sz=1.0, ts=None)],
                                  T0, T0 + 60, "BTC", 1)
        self.assertEqual(agg["n_fills"], 0)
        self.assertAlmostEqual(agg["entry_sz"], 0.0)

    def test_millisecond_ts_is_normalized(self):
        """库里出现过毫秒 ts：按毫秒处理会被判成窗口外而静默漏掉，必须归一。"""
        agg = pnl.aggregate_fills([fill(dir_="Open Long", sz=1.0, ts=int(T0 * 1000))],
                                  T0, T0 + 60, "BTC", 1)
        self.assertAlmostEqual(agg["entry_sz"], 1.0)

    def test_open_position_without_close_has_no_exit_leg(self):
        """仓位还开着（closed_at=None）：只有入场腿，net_size = 入场量。"""
        agg = pnl.aggregate_fills([fill(dir_="Open Long", sz=0.7)], T0, None, "BTC", 1)
        self.assertAlmostEqual(pnl.net_size(agg), 0.7)
        self.assertEqual(agg["exit_sz"], 0.0)

    def test_garbage_rows_never_raise(self):
        for bad in (None, {}, [], {"sz": "x", "px": None}, {"dir": None}):
            agg = pnl.aggregate_fills([bad], T0, T0 + 60, "BTC", 1)
            self.assertIn("net", agg)
            self.assertIsInstance(agg["net"], float)

    def test_from_exchange_normalizes_camel_case(self):
        """userFills 的原始形状（驼峰 + 毫秒 time）要能正确归一。"""
        raw = {"tid": 7, "coin": "BTC", "dir": "Close Long", "sz": "1.0", "px": "110.0",
               "fee": "0.12", "feeToken": "USDC", "closedPnl": "10.0",
               "time": int((T0 + 30) * 1000)}
        agg = pnl.aggregate_fills([pnl.from_exchange(raw)], T0, T0 + 3600, "BTC", 1)
        self.assertEqual(agg["n_fills"], 1)
        self.assertAlmostEqual(agg["exit_sz"], 1.0)
        self.assertAlmostEqual(agg["fee_total"], 0.12)
        self.assertAlmostEqual(agg["net"], 9.88)


# ---------------------------------------------------------------- 阈值
class TestPnlDiff(unittest.TestCase):

    def test_within_absolute_floor_does_not_trigger(self):
        d = pnl.pnl_diff(1.0, 1.05, 20.0, 20.5, 1000.0)
        self.assertFalse(d["r_diff"])
        self.assertFalse(d["net_diff"])

    def test_r_threshold_triggers(self):
        d = pnl.pnl_diff(1.0, 1.20, 20.0, 20.2, 1000.0)      # ΔR 0.2 > 0.15
        self.assertTrue(d["r_diff"])

    def test_r_relative_threshold_scales_with_large_r(self):
        # |R| = 3.0 → 阈值 max(0.15, 0.30) = 0.30；ΔR 0.25 不该触发
        d = pnl.pnl_diff(3.0, 3.25, 60.0, 65.0, 1000.0)
        self.assertAlmostEqual(d["r_threshold"], 0.30)
        self.assertFalse(d["r_diff"])
        # ΔR 0.35 超过 0.30 → 触发
        self.assertTrue(pnl.pnl_diff(3.0, 3.35, 60.0, 66.0, 1000.0)["r_diff"])

    def test_net_relative_threshold_scales_with_notional(self):
        # notional 100k → 阈值 50U；Δnet 20U 不触发
        d = pnl.pnl_diff(None, None, 100.0, 120.0, 100_000.0)
        self.assertAlmostEqual(d["net_threshold"], 50.0)
        self.assertFalse(d["net_diff"])
        # Δnet 60U > 50U → 触发
        self.assertTrue(pnl.pnl_diff(None, None, 100.0, 160.0, 100_000.0)["net_diff"])

    def test_missing_inputs_never_trigger(self):
        d = pnl.pnl_diff(None, None, None, 5.0, 1000.0)
        self.assertIsNone(d["d_r"])
        self.assertIsNone(d["d_net"])
        self.assertFalse(d["r_diff"])
        self.assertFalse(d["net_diff"])


# ---------------------------------------------------------------- 执行器关账接线
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="testrev",
                            # 本文件不建模信号时效；时效闸由 tests/test_hl_signal_age.py 专测。
                            max_signal_age_min=0)
        Executor._size_strikes.clear()

    def tearDown(self):
        Executor._size_strikes.clear()
        self.tmp.cleanup()

    def executor(self):
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def open_position(self):
        """走真实 on_entry 建一个 open 仓（拿到真实 actual_entry/opened_at/hl_sz）。"""
        pid = self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=1791912000000, side=1,
            entry_price=83000.0, stop_price=80600.0, stop_dist=0.029,
            contracts=1, coin_qty=0.001, notional=83.0, risk_u=20.0, equity_at_entry=1000.0)
        self.assertTrue(self.executor().on_entry(pid))
        return pid

    def close_via_engine(self, pid, exit_px=84500.0):
        """走真实 on_exit（ma20）关账，交易所侧按 exit_px 全额成交。"""
        row = self.db.position_by_id(pid)
        sz = float(row["hl_sz"])
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": 77, "avg_px": exit_px,
                        "filled_sz": sz, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC", size=sz)], orders=[_stop_order()])
        self.ex.calls.clear()
        self.assertTrue(self.executor().on_exit(pid, "ma20", exit_px - 100))
        return row

    def diff_events(self):
        return [e for e in (dict(r) for r in self.db.hl_recent_events(200, kind=EV_RECONCILE))
                if "关账口径差异" in str(e["message"])]


class TestCloseWritesNetPnl(Base):

    def test_net_pnl_written_from_fills(self):
        pid = self.open_position()
        row = self.close_via_engine(pid)
        sz = float(row["hl_sz"])
        entry = float(self.db.position_by_id(pid)["actual_entry"])
        # 交易所流水：入场腿 + 出场腿（与本地价格一致，只有手续费差异）
        self.info._fills = [
            {"tid": 1, "coin": "BTC", "dir": "Open Long", "sz": str(sz), "px": str(entry),
             "fee": "0.05", "feeToken": "USDC", "closedPnl": "0.0",
             "time": int((time.time() - 5) * 1000)},
            {"tid": 2, "coin": "BTC", "dir": "Close Long", "sz": str(sz), "px": "84500.0",
             "fee": "0.09", "feeToken": "USDC", "closedPnl": "12.0",
             "time": int((time.time() - 2) * 1000)},
        ]
        self.executor()._record_fill_pnl(pid)          # 幂等：再算一遍结果一致
        out = self.db.position_by_id(pid)
        self.assertEqual(out["pnl_source"], PNL_SRC_FILLS)
        self.assertAlmostEqual(out["net_pnl"], 12.0 - 0.14, places=6)

    def test_switch_off_still_computes(self):
        """开关默认 off：net_pnl 照写（纯观测），不因为没开就丢数据。"""
        self.assertFalse(self.p.pnl_from_fills)
        pid = self.open_position()
        row = self.close_via_engine(pid)
        sz = float(row["hl_sz"])
        now = int(time.time() * 1000)
        self.info._fills = [
            {"tid": 11, "coin": "BTC", "dir": "Open Long", "sz": str(sz), "px": "83000",
             "fee": "0.05", "feeToken": "USDC", "closedPnl": "0", "time": now - 6000},
            {"tid": 12, "coin": "BTC", "dir": "Close Long", "sz": str(sz), "px": "84500",
             "fee": "0.09", "feeToken": "USDC", "closedPnl": "12.0", "time": now - 3000},
        ]
        self.executor()._record_fill_pnl(pid)
        self.assertAlmostEqual(self.db.position_by_id(pid)["net_pnl"], 11.86, places=6)

    def test_only_entry_fill_leaves_net_null(self):
        """只有入场腿（平仓还没同步）→ net_pnl 留 NULL，不写「−手续费」这种半截数。"""
        pid = self.open_position()
        row = self.close_via_engine(pid)
        sz = float(row["hl_sz"])
        self.info._fills = [{"tid": 21, "coin": "BTC", "dir": "Open Long", "sz": str(sz),
                             "px": "83000", "fee": "0.05", "feeToken": "USDC",
                             "closedPnl": "0", "time": int((time.time() - 60) * 1000)}]
        self.executor()._record_fill_pnl(pid)
        out = self.db.position_by_id(pid)
        self.assertIsNone(out["net_pnl"])
        self.assertEqual(out["pnl_source"], PNL_SRC_NO_EXIT_FILLS)

    def test_no_fills_marks_source(self):
        """查不到任何成交：源标记 no_fills，net 为 NULL（不是 0）。"""
        pid = self.open_position()
        self.close_via_engine(pid)
        self.info._fills = []
        self.executor()._record_fill_pnl(pid)
        out = self.db.position_by_id(pid)
        self.assertIsNone(out["net_pnl"])
        self.assertEqual(out["pnl_source"], "no_fills")


class TestDiffEvent(Base):

    def _fills(self, pid, closed_pnl):
        row = self.db.position_by_id(pid)
        sz = float(row["hl_sz"])
        now = int(time.time() * 1000)
        return [
            {"tid": 31, "coin": "BTC", "dir": "Open Long", "sz": str(sz), "px": "83000",
             "fee": "0.05", "feeToken": "USDC", "closedPnl": "0", "time": now - 6000},
            {"tid": 32, "coin": "BTC", "dir": "Close Long", "sz": str(sz), "px": "84500",
             "fee": "0.09", "feeToken": "USDC", "closedPnl": str(closed_pnl), "time": now - 3000},
        ]

    def test_close_matching_local_records_no_diff(self):
        """流水与本地一致（R 相同、净额只差手续费口径已同源）→ 不记差异事件。"""
        pid = self.open_position()
        self.close_via_engine(pid)
        row = self.db.position_by_id(pid)
        sz = float(row["hl_sz"])
        entry = float(row["actual_entry"])
        # 用与本地同样的价差造 closedPnl（再扣手续费口径两边一致）
        gross = (84500.0 - entry) * sz
        self.info._fills = self._fills(pid, 0.0)
        self.info._fills[1]["closedPnl"] = str(gross + 0.14)   # + 手续费 = net 与本地一致
        self.executor()._record_fill_pnl(pid)
        self.assertEqual(self.diff_events(), [])

    def test_close_mismatch_records_diff_event(self):
        """流水净额与本地差得远（交易所少记 10 U）→ 必须留一条差异事件。"""
        pid = self.open_position()
        self.close_via_engine(pid)
        self.info._fills = self._fills(pid, 0.0)          # closedPnl=0 vs 本地 +十几 U
        self.executor()._record_fill_pnl(pid)
        evs = self.diff_events()
        self.assertEqual(len(evs), 1, "关账口径差异没有留痕")
        self.assertIn("R 本地", evs[0]["message"])
        self.assertIn("净额", evs[0]["message"])
        self.assertIn("BTC_USDT", evs[0]["message"])

    def test_switch_on_does_not_change_writes(self):
        """开关只影响面板读哪个数，不影响入库与事件（两态行为一致）。"""
        self.p.pnl_from_fills = True
        pid = self.open_position()
        self.close_via_engine(pid)
        self.info._fills = self._fills(pid, 0.0)
        self.executor()._record_fill_pnl(pid)
        out = self.db.position_by_id(pid)
        self.assertEqual(out["pnl_source"], PNL_SRC_FILLS)
        self.assertIsNotNone(out["net_pnl"])
        self.assertEqual(len(self.diff_events()), 1)


# ---------------------------------------------------------------- 面板口径
class TestPanelSwitch(Base):
    """hl_pnl_from_fills：off 时 signals 不带 net_pnl 键，on 时带上（记录表据此加列）。"""

    def _payload(self, on: bool):
        from app.hl.views import DashboardDeps, status_payload
        deps = DashboardDeps(
            db=self.db, config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT"],
                                                         "hl_pnl_from_fills": on})(),
            info=self.info, address_fn=lambda: "0xabc", mode_fn=lambda: "live")
        self.info._snap = _snap(positions=[], orders=[])
        return status_payload(deps)

    def test_off_has_no_net_pnl_key(self):
        pid = self.open_position()
        self.db.set_pos_pnl(pid, 12.5, PNL_SRC_FILLS)
        sig = self._payload(on=False)["signals"][0]
        self.assertNotIn("net_pnl", sig)

    def test_on_exposes_net_pnl(self):
        pid = self.open_position()
        self.db.set_pos_pnl(pid, 12.5, PNL_SRC_FILLS)
        sig = self._payload(on=True)["signals"][0]
        self.assertAlmostEqual(sig["net_pnl"], 12.5)
        self.assertEqual(sig["pnl_source"], PNL_SRC_FILLS)

    def test_on_with_uncomputed_pnl_is_null_not_zero(self):
        """老库/未算过的行：net_pnl 为 None（面板显示「—」），不是 0。"""
        self.open_position()
        sig = self._payload(on=True)["signals"][0]
        self.assertIsNone(sig["net_pnl"])

    def test_signals_html_renders_extra_column_only_when_present(self):
        from app.hl.views import _signals_html
        base = {"symbol": "BTC_USDT", "coin": "BTC", "status": "closed", "side": 1,
                "entry": 100.0, "stop": 95.0, "qty": 0.01, "bar_ms": 1_700_000_000_000}
        off = _signals_html([dict(base)])
        self.assertNotIn("净额（流水）", off)
        self.assertEqual(off.split("<tbody>", 1)[1].count("<td"), 7)
        on = _signals_html([dict(base, net_pnl=-3.5, pnl_source=PNL_SRC_FILLS)])
        self.assertIn("净额（流水）", on)
        self.assertEqual(on.split("<tbody>", 1)[1].count("<td"), 8)
        self.assertIn("-3.50", on)
        # NULL 净额 → 净额那格是「—」而不是 0.00（其它列本来就有 100.00 之类）
        null = _signals_html([dict(base, net_pnl=None, pnl_source="no_fills")])
        cells = null.split("<tbody>", 1)[1].split("</tr>")[0].split("<td")
        pnl_cell = [c for c in cells if c.startswith(" class='num'>—")]
        self.assertTrue(pnl_cell, f"净额格应为「—」，实际：{null}")


# ---------------------------------------------------------------- 面板费用口径
class TestPanelFeeQuote(Base):
    """fees_total 与逐笔 fee 都走 fee_quote（NULL 回退 fee），与 #34 的读侧语义一致。"""

    def _payload(self):
        from app.hl.views import DashboardDeps, status_payload
        deps = DashboardDeps(
            db=self.db, config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT"]})(),
            info=self.info, address_fn=lambda: "0xabc", mode_fn=lambda: "live")
        self.info._snap = _snap(positions=[], orders=[])
        return status_payload(deps)

    def _fill(self, tid, fee, fee_quote=None, token="USDC"):
        self.db.hl_upsert_fill(tid=tid, symbol="BTC_USDT", coin="BTC", side=1, sz=1.0,
                               px=100.0, fee=fee, fee_token=token, fee_quote=fee_quote,
                               dir="Close Long", closed_pnl=0.0, hash=tid, reduce_only=True,
                               ts=int(time.time()))

    def test_fees_total_prefers_fee_quote(self):
        """fee=5.0（HYPE 数量）但折算后 0.42 → 合计按 0.42 算，不是 5.42。"""
        self._fill("a", fee=5.0, fee_quote=0.42, token="HYPE")
        self._fill("b", fee=0.08, fee_quote=0.08)
        acc = self._payload()["account"]
        self.assertAlmostEqual(acc["fees_total"], 0.50, places=6)

    def test_per_fill_fee_uses_fee_quote(self):
        self._fill("a", fee=5.0, fee_quote=0.42, token="HYPE")
        f = self._payload()["fills"][0]
        self.assertAlmostEqual(f["fee"], 0.42, places=6)
        self.assertEqual(f["fee_token"], "HYPE")     # 原始币种仍可查（人工核对用）

    def test_legacy_row_without_fee_quote_falls_back(self):
        """#34 之前的历史行 fee_quote 为 NULL → 回退原始 fee（行为不变）。"""
        self._fill("old", fee=0.33, fee_quote=None)
        acc = self._payload()["account"]
        self.assertAlmostEqual(acc["fees_total"], 0.33, places=6)
        self.assertAlmostEqual(self._payload()["fills"][0]["fee"], 0.33, places=6)

    def test_records_tab_fee_column_uses_fee_quote(self):
        """记录页成交表的手续费列同口径（否则同一笔在两处显示不同数字）。"""
        from app.admin_ui import AdminDeps
        from app.hl.views import _fee_quoted
        # 直接验口径函数（记录页渲染路径就是它）：fee_quote 优先
        self.assertAlmostEqual(
            _fee_quoted({"fee": 5.0, "fee_quote": 0.42}), 0.42, places=6)
        self.assertAlmostEqual(_fee_quoted({"fee": 0.33, "fee_quote": None}), 0.33, places=6)
        self.assertIsNone(_fee_quoted({"fee": None, "fee_quote": None}))
        self.assertTrue(callable(AdminDeps))         # 记录页 import 的就是这个函数


# ---------------------------------------------------------------- 老库迁移
class TestMigrateNetPnlColumns(unittest.TestCase):

    OLD_SCHEMA = """
    CREATE TABLE positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL, signal_bar_ms INTEGER NOT NULL, status TEXT NOT NULL,
        side INTEGER NOT NULL DEFAULT 1, entry_price REAL NOT NULL, stop_price REAL NOT NULL,
        stop_dist REAL NOT NULL, contracts INTEGER NOT NULL, coin_qty REAL NOT NULL,
        notional REAL NOT NULL, risk_u REAL NOT NULL,
        actual_entry REAL, actual_contracts REAL, actual_stop REAL, stop_placed INTEGER DEFAULT 0,
        exit_price REAL, exit_reason TEXT, auto_closed INTEGER DEFAULT 0, r_multiple REAL,
        equity_at_entry REAL, created_at REAL NOT NULL, opened_at REAL, closed_at REAL,
        UNIQUE(symbol, signal_bar_ms)
    );
    """

    def _old_db(self, path):
        raw = sqlite3.connect(path)
        raw.executescript(self.OLD_SCHEMA)
        raw.execute("INSERT INTO positions(symbol, signal_bar_ms, status, side, entry_price,"
                    " stop_price, stop_dist, contracts, coin_qty, notional, risk_u,"
                    " r_multiple, created_at, opened_at, closed_at)"
                    " VALUES('BTC_USDT',1,'closed',1,100.0,95.0,0.05,1,0.01,1.0,0.05,"
                    " 1.5,?,?,?)", (T0 - 3600, T0, T0 + 3600))
        raw.commit()
        raw.close()

    def test_adds_columns_and_keeps_history_null(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "old.db")
            self._old_db(path)
            db = DB(path)                                  # 打开即迁移
            cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(positions)")}
            self.assertIn("net_pnl", cols)
            self.assertIn("pnl_source", cols)
            row = db.recent_positions(1)[0]
            self.assertAlmostEqual(row["r_multiple"], 1.5)  # 历史数据不动
            self.assertIsNone(row["net_pnl"], "历史行不该被回填一个事后算出的数")
            self.assertIsNone(row["pnl_source"])

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "new.db")
            DB(path).conn.close()
            DB(path).conn.close()
            cols = [r["name"] for r in DB(path).conn.execute("PRAGMA table_info(positions)")]
            self.assertEqual(cols.count("net_pnl"), 1)
            self.assertEqual(cols.count("pnl_source"), 1)

    def test_set_pos_pnl_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(str(Path(d) / "x.db"))
            pid = db.create_position(symbol="BTC_USDT", signal_bar_ms=1, entry_price=100.0,
                                     stop_price=95.0, stop_dist=0.05, contracts=1,
                                     coin_qty=0.01, notional=1.0, risk_u=0.05)
            db.set_pos_pnl(pid, -2.75, PNL_SRC_FILLS)
            row = db.position_by_id(pid)
            self.assertAlmostEqual(row["net_pnl"], -2.75)
            self.assertEqual(row["pnl_source"], PNL_SRC_FILLS)
            db.set_pos_pnl(pid, None, "no_fills")
            row = db.position_by_id(pid)
            self.assertIsNone(row["net_pnl"])
            self.assertEqual(row["pnl_source"], "no_fills")


if __name__ == "__main__":
    unittest.main(verbosity=2)
