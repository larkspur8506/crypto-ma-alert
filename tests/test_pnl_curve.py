"""tests/test_pnl_curve.py —— 盈亏曲线（已剔除出入金）+ 收益指数 TWR。

规格见 cma-pnl-curve-spec.md §四（12 条清单）。全部离线：临时 sqlite + 假 info，
不触网、不读真实配置。

覆盖三层：
1. `app/hl/flows.py`：账本行 → 带符号流水（分类表）、未知类型告警、读侧曲线数学。
2. `HlInfo.ledger_updates`：分页拼接与死循环防御。
3. `Executor._sync_flows` + `views` 渲染：失败不写水位、面板不画误导曲线、
   标题/图例/右轴/四个数字都在，且全页不再出现「权益曲线」。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.db import DB                                            # noqa: E402
from app.hl import flows as hl_flows                             # noqa: E402
from app.hl.info import HlInfo, HlInfoError                      # noqa: E402

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.hl.views import DashboardDeps, build_router, render_dashboard, status_payload
except ImportError:                                              # pragma: no cover
    FastAPI = TestClient = None
    DashboardDeps = build_router = render_dashboard = status_payload = None

ME = "0xTestAccount000000000000000000000000"
OTHER = "0xOtherAccount00000000000000000000000"
NOW = 1760000000.0
DAY = 86400.0
NOTE = "出入金流水读取失败，盈亏曲线暂不可用（系统会自动重试）"


# ---------------------------------------------------------------- 构造辅助
def ledger_row(typ, ms=1_700_000_000_000, hash_="0xhash", **delta):
    """一条 `userNonFundingLedgerUpdates` 原始行（形状同交易所返回）。"""
    return {"time": ms, "hash": hash_, "delta": dict(delta, type=typ)}


def snap(ts, equity):
    return {"ts": ts, "equity": equity}


def flow(ts, amount, kind="deposit"):
    return {"ts": ts, "amount": amount, "kind": kind}


class FakeInfo:
    """只实现 views 需要的 snapshot()。"""

    def __init__(self, equity=987.31, raise_=False):
        self.equity = equity
        self.raise_ = raise_
        self.calls = 0

    def snapshot(self, address, mids=None):
        self.calls += 1
        if self.raise_:
            raise RuntimeError("boom")
        return {"ts": NOW, "address": address, "equity": self.equity,
                "withdrawable": self.equity, "margin_used": 0.0, "notional": 0.0,
                "positions": [], "open_orders": [], "orders_trusted": True}


class BaseDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pnl_curve_")
        self.db = DB(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ================================================================ 1~4 分类
class TestClassify(unittest.TestCase):
    """§四.1 分类规则：符号对不对、金额回落、内部划转忽略。"""

    def test_deposit_and_withdraw_carry_sign(self):
        d = hl_flows.classify(ledger_row("deposit", usdc="100.79"), ME)
        self.assertEqual(d["kind"], "deposit")
        self.assertAlmostEqual(d["amount"], 100.79, places=6, msg="入金必须是正（流入）")
        self.assertAlmostEqual(d["ts"], 1_700_000_000.0, msg="time 是毫秒，落库要转秒")

        w = hl_flows.classify(ledger_row("withdraw", ms=1_700_000_001_000, usdc="50"), ME)
        self.assertEqual(w["kind"], "withdraw")
        self.assertAlmostEqual(w["amount"], -50.0, places=6, msg="出金必须是负（流出）")

    def test_send_direction_follows_account_side(self):
        into = hl_flows.classify(
            ledger_row("send", hash_="0xa", user=OTHER, destination=ME, usdcValue="901.8"), ME)
        self.assertEqual(into["kind"], "send_in")
        self.assertAlmostEqual(into["amount"], 901.8, places=6)
        self.assertEqual(into["counterparty"], OTHER.lower())

        out = hl_flows.classify(
            ledger_row("send", hash_="0xb", user=ME, destination=OTHER, usdcValue="12.5"), ME)
        self.assertEqual(out["kind"], "send_out")
        self.assertAlmostEqual(out["amount"], -12.5, places=6)
        self.assertEqual(out["counterparty"], OTHER.lower())

    def test_send_self_transfer_is_ignored(self):
        """自己转自己：钱没离开账户，权益不变 → 不能算流水。"""
        self.assertIsNone(hl_flows.classify(
            ledger_row("send", user=ME, destination=ME, usdcValue="5"), ME))

    def test_send_falls_back_to_amount_when_usdcValue_missing(self):
        f = hl_flows.classify(
            ledger_row("send", user=OTHER, destination=ME, amount="7.25"), ME)
        self.assertEqual(f["kind"], "send_in")
        self.assertAlmostEqual(f["amount"], 7.25, places=6)

    def test_vault_directions(self):
        """金库：存进去钱离开交易账户（权益降），取回来钱回到账户（权益升）。"""
        d = hl_flows.classify(ledger_row("vaultDeposit", usdc="40"), ME)
        self.assertEqual(d["kind"], "vault_out")
        self.assertAlmostEqual(d["amount"], -40.0)
        w = hl_flows.classify(ledger_row("vaultWithdraw", usdc="40"), ME)
        self.assertEqual(w["kind"], "vault_in")
        self.assertAlmostEqual(w["amount"], 40.0)

    def test_internal_transfer_types_are_ignored(self):
        """§四.2 现货↔合约划转不是出入金：计进去会让 pnl 白跳一个台阶。"""
        for typ in ("accountClassTransfer", "internalTransfer", "spotTransfer",
                    "subAccountTransfer"):
            with self.subTest(typ=typ):
                self.assertIsNone(hl_flows.classify(
                    ledger_row(typ, usdc="1000", user=ME, destination=ME), ME),
                    f"{typ} 应被忽略")

    def test_unknown_type_counted_as_external_flow(self):
        """§四.3 未知类型按外部出入金计入（否则曲线凭空跳变），符号沿用交易所。"""
        f = hl_flows.classify(ledger_row("someNewLedgerType", usdc="3.5"), ME)
        self.assertEqual(f["kind"], "other")
        self.assertAlmostEqual(f["amount"], 3.5)
        self.assertEqual(f["type"], "someNewLedgerType")
        neg = hl_flows.classify(ledger_row("someNewLedgerType", usdc="-2"), ME)
        self.assertAlmostEqual(neg["amount"], -2.0, msg="缺符号时沿用交易所给的数")

    def test_unknown_type_without_amount_is_dropped(self):
        """不知道金额 = 不猜（记 warning，不进曲线）。"""
        self.assertIsNone(hl_flows.classify(ledger_row("mysteryType"), ME))

    def test_ignored_types_do_not_move_pnl(self):
        """§四.2 端到端：内部划转不产生流水，pnl 曲线不跳。"""
        rows = [ledger_row("accountClassTransfer", ms=1_700_000_000_000, usdc="1000", user=ME),
                ledger_row("internalTransfer", ms=1_700_000_001_000, usdc="500", user=ME)]
        raw = [hl_flows.classify(r, ME) for r in rows]
        self.assertEqual([x for x in raw if x], [], "内部划转不应产生任何流水")


# ================================================================ 4/3 落库
class TestRecordFlows(BaseDb):
    """§四.3/4：幂等去重 + 未知类型只告警一次。"""

    def test_same_hash_and_ts_written_twice_yields_one_row(self):
        f = hl_flows.classify(ledger_row("deposit", hash_="0xdup", usdc="100.79"), ME)
        self.assertTrue(self.db.hl_add_flow(f))
        self.assertFalse(self.db.hl_add_flow(dict(f)), "同 (hash, ts) 第二次必须被忽略")
        rows = self.db.hl_flows_since(0)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["amount"], 100.79)

        # 再走一遍完整链路（模拟「重拉昨天的页」）：条数不变
        res = hl_flows.record_flows(self.db, [ledger_row("deposit", hash_="0xdup",
                                                        usdc="100.79")], ME)
        self.assertEqual(res["added"], 0)
        self.assertEqual(len(self.db.hl_flows_since(0)), 1)

    def test_unknown_type_logs_flow_unknown_event_once(self):
        rows = [ledger_row("someNewLedgerType", ms=1_700_000_000_000, hash_="0xu1", usdc="3.5")]
        first = hl_flows.record_flows(self.db, rows, ME)
        self.assertEqual(first["added"], 1)
        self.assertEqual(first["unknown"], ["someNewLedgerType"])
        evs = self.db.hl_recent_events(50, kind=hl_flows.EV_FLOW_UNKNOWN)
        self.assertEqual(len(evs), 1, "未知类型应记一次事件")
        self.assertIn("someNewLedgerType", evs[0]["message"])

        # 第二笔同类型：流水照样入账，但事件不重复写（24h 去重）
        second = hl_flows.record_flows(
            self.db, [ledger_row("someNewLedgerType", ms=1_700_000_009_000, hash_="0xu2",
                                 usdc="1")], ME)
        self.assertEqual(second["added"], 1)
        self.assertEqual(len(self.db.hl_recent_events(50, kind=hl_flows.EV_FLOW_UNKNOWN)), 1)
        self.assertEqual(len(self.db.hl_flows_since(0)), 2)

    def test_flow_sum_and_last_ts(self):
        hl_flows.record_flows(self.db, [
            ledger_row("deposit", ms=1_700_000_000_000, hash_="0xs1", usdc="100.79"),
            ledger_row("send", ms=1_700_000_500_000, hash_="0xs2", user=OTHER,
                       destination=ME, usdcValue="901.8"),
            ledger_row("withdraw", ms=1_700_000_600_000, hash_="0xs3", usdc="10"),
        ], ME)
        self.assertAlmostEqual(self.db.hl_flow_sum(), 992.59, places=6)
        self.assertAlmostEqual(self.db.hl_flow_last_ts(), 1_700_000_600.0, places=6)


# ================================================================ 曲线数学
class TestCurveMath(unittest.TestCase):
    """§四.5~8：入金/出金不影响 pnl，TWR 剔除时点，回撤由 pnl 算。"""

    def test_deposit_does_not_move_pnl(self):
        snaps = [snap(NOW - 3 * DAY, 100.0), snap(NOW - 2 * DAY, 100.0),
                 snap(NOW - DAY, 1000.0)]
        flows = [flow(NOW - 4 * DAY, 100.0),          # 本金（窗口外也要计入！）
                 flow(NOW - DAY, 900.0, "send_in")]   # 入金 900，权益同步 +900
        c = hl_flows.curve(snaps, flows, now=NOW)
        self.assertEqual([p[1] for p in c["pnl"]], [0.0, 0.0, 0.0],
                         "入金不能变成盈亏：曲线必须是平的")
        self.assertEqual([p[1] for p in c["twr"]], [100.0, 100.0, 100.0],
                         "入金也不能抬高收益指数")
        self.assertAlmostEqual(c["meta"]["net_deposit"], 1000.0)
        self.assertAlmostEqual(c["meta"]["pnl_total"], 0.0)

    def test_withdraw_does_not_move_pnl(self):
        snaps = [snap(NOW - 2 * DAY, 1000.0), snap(NOW - DAY, 500.0)]
        flows = [flow(NOW - 3 * DAY, 1000.0), flow(NOW - DAY, -500.0, "withdraw")]
        c = hl_flows.curve(snaps, flows, now=NOW)
        self.assertEqual([p[1] for p in c["pnl"]], [0.0, 0.0],
                         "出金不是亏损：pnl 不能跟着掉")
        self.assertEqual([p[1] for p in c["twr"]], [100.0, 100.0])
        self.assertAlmostEqual(c["meta"]["net_deposit"], 500.0)

    def test_twr_removes_deposit_timing(self):
        """+10% → 入金 → +10%：指数 ≈121（不是 120，也不是按总本金摊薄的 115.8）。"""
        snaps = [snap(NOW - 4 * DAY, 100.0), snap(NOW - 3 * DAY, 110.0),
                 snap(NOW - 2 * DAY, 200.0),        # 110 + 入金 90
                 snap(NOW - DAY, 220.0)]            # 200 再 +10%
        flows = [flow(NOW - 5 * DAY, 100.0), flow(NOW - 2 * DAY, 90.0, "send_in")]
        c = hl_flows.curve(snaps, flows, now=NOW)
        self.assertAlmostEqual(c["twr"][-1][1], 121.0, places=6)
        self.assertAlmostEqual(c["meta"]["twr_total_pct"], 21.0, places=6)
        # pnl 按 U 计：10（第一段）+ 20（入金后第二段）= 30
        self.assertAlmostEqual(c["pnl"][-1][1], 30.0, places=6)
        self.assertAlmostEqual(c["meta"]["pnl_total"], 30.0, places=6)

    def test_max_dd_pnl_from_pnl_curve(self):
        snaps = [snap(NOW - 3 * DAY, 100.0), snap(NOW - 2 * DAY, 80.0),
                 snap(NOW - DAY, 90.0)]
        flows = [flow(NOW - 4 * DAY, 100.0)]
        c = hl_flows.curve(snaps, flows, now=NOW)
        self.assertAlmostEqual(c["meta"]["max_dd_pnl"], 20.0, places=6,
                               msg="100 → 80 → 90 的回撤是 20U（峰谷差）")

    def test_max_dd_ignores_deposit_step(self):
        """入金把权益顶上去后又跌回来：回撤按 pnl 算 = 0（不是权益的台阶差）。"""
        snaps = [snap(NOW - 3 * DAY, 100.0), snap(NOW - 2 * DAY, 1000.0),
                 snap(NOW - DAY, 950.0)]
        flows = [flow(NOW - 4 * DAY, 100.0), flow(NOW - 2 * DAY, 900.0, "send_in")]
        # 950 里 -50 是真实亏损（入金 900 之前是 100）
        c = hl_flows.curve(snaps, flows, now=NOW)
        self.assertAlmostEqual(c["pnl"][-1][1], -50.0, places=6)
        self.assertAlmostEqual(c["meta"]["max_dd_pnl"], 50.0, places=6,
                               msg="权益从 1000 掉到 950 不是回撤 950，只有真实亏损 50")

    def test_leading_zero_equity_snapshots_are_trimmed(self):
        """入金到账前的 0 权益快照不是「亏 100%」：整段裁掉，曲线从第一笔本金开始。

        2026-10-22 上线后实测：10-14 02:59 起有一批 equity=0 的快照，03:44 入金 100.79，
        旧算法把这段算成「回撤 100.79」——本用例钉住新口径（裁剪 + 期初权益为基线）。
        """
        t0 = NOW - 2 * DAY
        snaps = [snap(t0, 0.0), snap(t0 + 300, 0.0), snap(t0 + 2700, 100.79),
                 snap(t0 + 3000, 100.79)]
        flows = [flow(t0 + 2700, 100.79, "send_in")]     # 入金与首个 >0 快照同一时刻
        c = hl_flows.curve(snaps, flows, now=NOW)
        self.assertEqual(len(c["pnl"]), 2, "0 权益快照被裁掉")
        self.assertEqual([p[1] for p in c["pnl"]], [0.0, 0.0], "入金不能变成盈亏")
        self.assertAlmostEqual(c["meta"]["max_dd_pnl"], 0.0, places=6,
                               msg="不能再出现 100.79 的假回撤")
        self.assertAlmostEqual(c["meta"]["equity_base"], 100.79)
        self.assertEqual(c["meta"]["twr_skipped"], 0)

    def test_twr_skips_step_when_equity_hits_zero_midseries(self):
        """中途被清空（权益归零）再有钱进来：那一步不猜，指数原地不动。"""
        snaps = [snap(NOW - 3 * DAY, 100.0), snap(NOW - 2 * DAY, 0.0),
                 snap(NOW - DAY, 100.0)]
        c = hl_flows.curve(snaps, [], now=NOW)
        # 归零那一步是真实的 -100%（没有任何流水，钱是亏没的），指数归 0；
        # 之后 prev_eq=0，下一步不猜 → 指数原地不动、twr_skipped +1。
        self.assertEqual(c["twr"][1][1], 0.0)
        self.assertEqual(c["meta"]["twr_skipped"], 1)
        self.assertEqual(c["twr"][-1][1], 0.0, "不猜：指数原地不动")

    def test_no_positive_equity_returns_empty_curve(self):
        c = hl_flows.curve([snap(NOW - DAY, 0.0), snap(NOW, 0.0)], [], now=NOW)
        self.assertEqual(c["pnl"], [])
        self.assertEqual(c["meta"]["points"], 0)

    def test_window_filters_snapshots_but_not_flows(self):
        snaps = [snap(NOW - 90 * DAY, 999.0), snap(NOW - DAY, 120.0),
                 snap(NOW - 2 * DAY, 110.0)]
        flows = [flow(NOW - 120 * DAY, 100.0)]
        c = hl_flows.curve(snaps, flows, now=NOW, window_s=30 * DAY)
        self.assertEqual(len(c["pnl"]), 2, "30 天窗口外的快照不画")
        # 窗口外的本金被折进「期初权益」基线（equity_base=120），窗口内盈亏 = 110−120。
        # 窗口内两点按时间升序是 110（−2D）→ 120（−1D），期初权益 = 110。
        self.assertAlmostEqual(c["pnl"][-1][1], 10.0, places=6,
                               msg="窗口内盈亏 = 期末权益 − 期初权益（本金已在基线里）")
        self.assertAlmostEqual(c["meta"]["equity_base"], 110.0, places=6)
        self.assertAlmostEqual(c["meta"]["net_deposit"], 100.0)


# ================================================================ 真实数据对账
class TestRealAccountRecon(unittest.TestCase):
    """线上账户实测：两笔 `send` 入金 100.79 + 901.80，当前权益 987.31。

    累计盈亏必须 = 987.31 − 1002.59 = −15.28U（误差 < 0.01）。
    """

    DEPOSITS = (100.79, 901.80)

    def test_pnl_total_matches_exchange_ledger(self):
        equity_now = 987.31
        flows = [flow(NOW - 30 * DAY, self.DEPOSITS[0], "send_in"),
                 flow(NOW - 2 * DAY, self.DEPOSITS[1], "send_in")]
        snaps = [snap(NOW - 30 * DAY, 100.79), snap(NOW - 5 * DAY, 85.31),
                 snap(NOW - 2 * DAY, equity_now), snap(NOW - DAY, equity_now)]
        c = hl_flows.curve(snaps, flows, now=NOW)
        meta = c["meta"]
        self.assertAlmostEqual(meta["net_deposit"], 1002.59, places=6)
        self.assertAlmostEqual(meta["pnl_total"], -15.28, places=6)
        self.assertLess(abs(meta["pnl_total"] - (equity_now - 1002.59)), 0.01,
                        "累计盈亏 = 权益 − 净入金")
        # 反例钉一下：旧「权益曲线」看到的是 100.79 → 987.31 的 +886.52 假暴涨
        self.assertGreater(equity_now - 100.79, 800.0)
        # 亏损必须显示为负数（页面红字靠这个符号）
        self.assertLess(meta["pnl_total"], 0)


# ================================================================ 分页
class TestLedgerPaging(unittest.TestCase):
    """§四.10 分页拼接 + 死循环防御。"""

    @staticmethod
    def _info(rows):
        info = HlInfo(base_url="https://example.invalid", retries=1)

        def post(payload):
            start = int(payload.get("startTime") or 0)
            return [r for r in rows if int(r["time"]) >= start][:HlInfo.LEDGER_PAGE]

        info.post = post                                        # 不触网
        return info

    def test_two_pages_are_stitched(self):
        rows = [ledger_row("deposit", ms=i + 1, hash_=f"0x{i}", usdc="1")
                for i in range(HlInfo.LEDGER_PAGE + 3)]         # 500 + 3
        info = self._info(rows)
        got = info.ledger_updates(ME, start_ms=0)
        self.assertEqual(len(got), HlInfo.LEDGER_PAGE + 3, "两页必须拼起来")
        times = [int(r["time"]) for r in got]
        self.assertEqual(times, sorted(times), "拼起来的顺序必须是时间升序")
        self.assertEqual(len({int(r["time"]) for r in got}), len(got), "不能有重复行")

    def test_stuck_cursor_does_not_loop_forever(self):
        rows = [ledger_row("deposit", ms=1000, hash_=f"0xs{i}", usdc="1")
                for i in range(HlInfo.LEDGER_PAGE)]
        info = self._info(rows)
        got = info.ledger_updates(ME, start_ms=0)               # 游标不前进 → 立刻停
        self.assertEqual(len(got), HlInfo.LEDGER_PAGE)

    def test_deadline_stops_paging(self):
        rows = [ledger_row("deposit", ms=i + 1, hash_=f"0xd{i}", usdc="1")
                for i in range(HlInfo.LEDGER_PAGE * 2)]
        info = self._info(rows)
        self.assertEqual(info.ledger_updates(ME, start_ms=0, deadline_ts=0.0), [],
                         "预算已过：一页都不发，交给下一轮")

    def test_repull_is_idempotent_in_db(self):
        """§四.10 后半句：拼起来之后靠 (hash, ts) 去重。"""
        tmp = tempfile.mkdtemp(prefix="pnl_page_")
        try:
            db = DB(os.path.join(tmp, "t.db"))
            rows = [ledger_row("send", ms=i + 1, hash_=f"0xp{i}", user=OTHER,
                               destination=ME, usdcValue="1")
                    for i in range(HlInfo.LEDGER_PAGE + 3)]
            info = self._info(rows)
            first = hl_flows.record_flows(db, info.ledger_updates(ME, 0), ME)
            self.assertEqual(first["added"], HlInfo.LEDGER_PAGE + 3)
            second = hl_flows.record_flows(db, info.ledger_updates(ME, 0), ME)
            self.assertEqual(second["added"], 0, "重拉同一页不能写第二行")
            self.assertEqual(len(db.hl_flows_since(0)), HlInfo.LEDGER_PAGE + 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 执行器同步
class LedgerInfo(FakeInfo):
    """带 ledger_updates 的假 info：可配返回行或抛错，并记录调用次数。"""

    def __init__(self, rows=None, raise_=False):
        super().__init__()
        self.rows = rows or []
        self.raise_ = raise_
        self.ledger_calls = 0
        self.starts = []

    def ledger_updates(self, address, start_ms=0, deadline_ts=None, **kw):
        self.ledger_calls += 1
        self.starts.append(start_ms)
        if self.raise_:
            raise HlInfoError("userNonFundingLedgerUpdates 连续 3 次失败: 500")
        return list(self.rows)


class TestExecutorFlowSync(BaseDb):
    """§四.9：流水拉取失败 → 不写水位、只记事件、不影响交易。"""

    def _exec(self, info):
        from app.hl import MODE_LIVE
        from app.hl.executor import Executor, ExecParams
        p = ExecParams(mode=MODE_LIVE, account_address=ME, private_key="0xkey",
                       equity=1000.0, code_rev="testrev")
        ex = Executor(self.db, p, info, None)          # exchange=None：_sync_flows 不用它
        return ex, p

    def test_success_writes_flows_and_watermark(self):
        rows = [ledger_row("send", ms=1_700_000_000_000, hash_="0xr1", user=OTHER,
                           destination=ME, usdcValue="100.79"),
                ledger_row("send", ms=1_700_090_000_000, hash_="0xr2", user=OTHER,
                           destination=ME, usdcValue="901.8")]
        info = LedgerInfo(rows=rows)
        ex, _ = self._exec(info)
        self.assertEqual(ex._sync_flows(now=NOW), 2)
        self.assertEqual(len(self.db.hl_flows_since(0)), 2)
        self.assertAlmostEqual(self.db.hl_flow_sum(), 1002.59, places=6)
        self.assertEqual(self.db.get_config_str("hl_flows_synced_at"), str(int(NOW)))
        self.assertEqual(info.starts[0], 0, "首次同步必须全量回填（start_ms=0）")

    def test_interval_gate_skips_fresh_sync(self):
        info = LedgerInfo(rows=[ledger_row("deposit", hash_="0xg", usdc="10")])
        ex, p = self._exec(info)
        p.flow_sync_s = 1800
        self.assertEqual(ex._sync_flows(now=NOW), 1)
        self.assertEqual(ex._sync_flows(now=NOW + 60), 0, "30 分钟内不重复拉")
        self.assertEqual(info.ledger_calls, 1)
        # 过了间隔就续拉，且从「最后一条流水 − 60s」开始
        ex._sync_flows(now=NOW + 3600)
        self.assertEqual(info.ledger_calls, 2)
        self.assertEqual(info.starts[1], max(0, 1_700_000_000 - 60) * 1000)

    def test_failure_leaves_no_watermark_and_logs_event(self):
        info = LedgerInfo(raise_=True)
        ex, _ = self._exec(info)
        self.assertEqual(ex._sync_flows(now=NOW), 0)
        self.assertEqual(self.db.get_config_str("hl_flows_synced_at", ""), "",
                         "失败绝不能写水位（否则面板会以为流水是新的）")
        self.assertEqual(len(self.db.hl_flows_since(0)), 0)
        evs = self.db.hl_recent_events(20, kind="error")
        self.assertTrue(evs, "失败必须留一条 error 事件")
        self.assertIn("出入金流水同步失败", evs[0]["message"])


# ================================================================ 参数装配
class TestSyncIntervalParam(unittest.TestCase):
    """§五：只加参数、不加面板开关（用户明确要求）。"""

    def test_config_declares_range(self):
        from app.config import Config
        self.assertIn("hl_flow_sync_s", Config.OVERRIDABLE)
        self.assertEqual(Config._RANGES["hl_flow_sync_s"], (300, 86400))

    def test_no_panel_row_for_it(self):
        path = os.path.join(ROOT, "app", "admin_ui.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("hl_flow_sync_s", src,
                         "用户要求：这个参数只走 env/DB，不在面板加开关行")


# ================================================================ 面板渲染
class TestFlowsFailureView(BaseDb):
    """§四.9 读侧：拉不到流水 → flows_ok=False + 那句提示 + 不画曲线。"""

    def _deps(self, info=None):
        return DashboardDeps(
            db=self.db, config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT"]})(),
            info=info or FakeInfo(), address_fn=lambda: ME, mode_fn=lambda: "live",
            now_fn=lambda: NOW)

    def _seed_snapshots(self):
        for ts, eq in ((NOW - 3 * DAY, 900.0), (NOW - 2 * DAY, 950.0), (NOW - DAY, 987.31)):
            self.db.hl_add_snapshot({"ts": ts, "equity": eq, "positions": []})

    def test_payload_flags_flows_not_ok(self):
        if status_payload is None:
            self.skipTest("fastapi 未安装")
        self._seed_snapshots()
        meta = status_payload(self._deps())["curve"]["meta"]
        self.assertIs(meta["flows_ok"], False)
        self.assertEqual(meta["flows_note"], NOTE)

    def test_page_shows_note_and_draws_no_curve(self):
        if render_dashboard is None:
            self.skipTest("fastapi 未安装")
        self._seed_snapshots()
        html = render_dashboard(status_payload(self._deps()))
        self.assertIn(NOTE, html)
        self.assertNotIn("<polyline", html, "流水不可用时不许画任何曲线（含退回权益）")
        self.assertIn("盈亏曲线（已剔除出入金）", html)
        self.assertNotIn("权益曲线", html)

    def test_flows_ok_once_any_flow_or_watermark_exists(self):
        if status_payload is None:
            self.skipTest("fastapi 未安装")
        self._seed_snapshots()
        self.db.set_config("hl_flows_synced_at", str(int(NOW)))    # 同步过但确实没流水
        meta = status_payload(self._deps())["curve"]["meta"]
        self.assertIs(meta["flows_ok"], True)
        self.assertEqual(meta["flows_note"], "")


class TestPanelRender(BaseDb):
    """§四.11：新标题、两种图例词面、右轴刻度、四个数字，且全页无「权益曲线」。"""

    def setUp(self):
        super().setUp()
        if render_dashboard is None:
            self.skipTest("fastapi 未安装")
        # 真实形状：两笔 send 入金（100.79 / 901.80），当前权益 987.31
        for ts, amt, h in ((NOW - 30 * DAY, 100.79, "0xreal1"),
                           (NOW - 2 * DAY, 901.80, "0xreal2")):
            self.db.hl_add_flow({"ts": ts, "kind": "send_in", "amount": amt, "hash": h,
                                 "counterparty": OTHER, "dex": ""})
        self.db.set_config("hl_flows_synced_at", str(int(NOW)))
        for ts, eq in ((NOW - 30 * DAY, 100.79), (NOW - 5 * DAY, 85.31),
                       (NOW - 2 * DAY, 987.31), (NOW - DAY, 987.31)):
            self.db.hl_add_snapshot({"ts": ts, "equity": eq, "positions": []})
        self.deps = DashboardDeps(
            db=self.db, config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT"]})(),
            info=FakeInfo(), address_fn=lambda: ME, mode_fn=lambda: "live",
            now_fn=lambda: NOW)
        app = FastAPI()
        app.include_router(build_router(self.deps))
        self.client = TestClient(app)
        self.payload = status_payload(self.deps)
        self.html = render_dashboard(self.payload)

    def test_new_title_and_no_old_title_anywhere(self):
        self.assertIn("<h2>盈亏曲线（已剔除出入金）</h2>", self.html)
        self.assertNotIn("权益曲线", self.html, "旧标题必须彻底消失（同一事实一种说法）")
        self.assertNotIn("暂无权益快照", self.html)

    def test_legend_says_both_axes(self):
        self.assertIn("实线=累计盈亏（U，左轴）", self.html)
        self.assertIn("虚线=收益指数（起点 100，右轴）", self.html)

    def test_four_numbers_visible(self):
        meta = self.payload["curve"]["meta"]
        self.assertAlmostEqual(meta["pnl_total"], -15.28, places=2)
        line = (f"累计盈亏 {meta['pnl_total']:+,.2f}U · 净入金 {meta['net_deposit']:,.2f}U · "
                f"最大回撤 {meta['max_dd_pnl']:,.2f}U · 收益指数 "
                f"{meta['twr_total_pct']:+.2f}%（起点 100）")
        self.assertIn(line, self.html)
        # 关键数字本身也在（防止格式串写错后「看着有、其实没有」）
        self.assertIn("累计盈亏 -15.28U", self.html)
        self.assertIn("净入金 1,002.59U", self.html)

    def test_svg_has_pnl_and_twr_polylines_and_right_axis(self):
        self.assertIn("<polyline", self.html)
        self.assertIn("class='gtwr'", self.html, "TWR 必须叠加为虚线")
        self.assertIn("class='gbase'", self.html, "0 基准线必须在")
        meta = self.payload["curve"]["meta"]
        self.assertIs(meta["flows_ok"], True)
        # 右轴两个刻度：**当前展示窗口**里 TWR 的 min/max（7 天窗口优先）
        pnl_pts = self.payload["curve"]["pnl"]
        cut = pnl_pts[-1][0] - 7 * DAY
        shown_twr = [v for t, v in self.payload["curve"]["twr"] if t >= cut]
        self.assertGreaterEqual(len(shown_twr), 2)
        for v in (min(shown_twr), max(shown_twr)):
            self.assertIn(f"{v:,.1f}", self.html, f"右轴缺刻度 {v}")

    def test_pnl_svg_splits_green_and_red_at_zero(self):
        from app.hl.views import _pnl_svg
        pts = [[NOW - 300, -10.0], [NOW - 200, 5.0], [NOW - 100, -3.0]]
        svg = _pnl_svg(pts, [[NOW - 300, 100.0], [NOW - 200, 108.0], [NOW - 100, 104.0]])
        self.assertIn("class='gneg'", svg)
        self.assertIn("class='gpos'", svg)
        self.assertIn("class='gbase'", svg)

    def test_svg_placeholder_when_fewer_than_two_points(self):
        from app.hl.views import _pnl_svg
        svg = _pnl_svg([[NOW, 0.0]], [])
        self.assertIn("暂无数据（等执行器落快照", svg)
        self.assertNotIn("<polyline", svg)

    def test_curve_query_param_is_dead(self):
        plain = self.client.get("/hl").text
        legacy = self.client.get("/hl?curve=equity").text
        self.assertEqual(plain, legacy, "?curve= 已废弃：不得复活旧视图")
        self.assertNotIn("权益曲线", legacy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
