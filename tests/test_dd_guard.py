"""回撤熔断（DD Guard，规格 feat/dd-guard §1~§9）的单元测试。

覆盖八组（对应规格 §9）：
  1. 口径正确性（TWR/DD/出入金/bootstrap 重放）
  2. 状态持久化（锚点一次性、30 天清理后仍记得峰值、重启续算、时钟回拨）
  3. 阈值判线（边界 24.9/25.0/29.9/30.0、档位联动、手动覆盖与拒收）
  4. fail-closed（流水坏/无锚点/spot_ok=False 都不 kill）
  5. kill 行为（origin=dd、不平仓、不自解、L2 不置 kill）
  6. L1 当日亏损口径修复（提现、入金、流水不可用、窗口左开区间）
  7. 面板渲染（断言 HTML 字符串）
  8. 顺序纪律（_sync_flows 必须早于回撤判断）

全部离线：假 info / 假 exchange + 临时 sqlite，不触网。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.db import DB
from app.hl import MODE_LIVE
from app.hl import dd as hl_dd
from app.hl import flows as hl_flows
from app.hl.executor import Executor, ExecParams, _today_start_ts

NOW = 1_760_000_000.0                    # 固定「现在」，避免用例只在某一天有效
DAY0 = _today_start_ts(NOW)              # 该时刻对应的北京时间零点（L1 基线日界）
DAY = 86400.0


# ---------------------------------------------------------------- 桩
def _snap(equity: float, *, ts: float = NOW, spot_ok: bool = True,
          positions=(), orders=(), equity_total=None) -> dict:
    """账户快照（口径与 HlInfo.snapshot 一致：统一账户下 equity_total 才是账户总值）。"""
    return {"ts": ts, "address": "0xabc", "equity": 0.0,
            "equity_total": float(equity if equity_total is None else equity_total),
            "spot_usdc": float(equity), "spot_ok": spot_ok, "unified": True,
            "withdrawable": float(equity), "margin_used": 0.0, "notional": 0.0,
            "positions": list(positions), "open_orders": list(orders),
            "orders_trusted": True}


class FakeInfo:
    def __init__(self, snap=None):
        self.snap = snap or _snap(1000.0)
        self.raise_ledger = False
        self.ledger_calls = 0
        self.snapshot_calls = 0

    def snapshot(self, address, mids=None):
        self.snapshot_calls += 1
        return dict(self.snap)

    def all_mids(self):
        return {"BTC": 83000.0}

    def specs(self, coins=None):
        return {"BTC": {"sz_decimals": 5, "max_leverage": 40, "only_isolated": False}}

    def open_orders(self, address):
        return list(self.snap.get("open_orders") or [])

    def user_fills(self, address):
        return []

    def ledger_updates(self, address, start_ms=0, deadline_ts=None):
        self.ledger_calls += 1
        if self.raise_ledger:
            raise RuntimeError("ledger api down")
        return []


class FakeExchange:
    """只记账不成交的写通道：用来断言「熔断没有发出任何平仓单」。"""

    available = True
    init_error = ""

    def __init__(self):
        self.calls = []

    def mid(self, coin):
        return 83000.0

    def slippage_limit(self, coin, is_buy, mid=None):
        px = mid if mid else self.mid(coin)
        return px * (1 + 0.003) if is_buy else px * (1 - 0.003)

    def update_leverage(self, coin, leverage, cross=False):
        self.calls.append(("leverage", coin))
        return {"ok": True, "status": "resting", "error": None, "oid": None,
                "avg_px": None, "filled_sz": None, "raw": None}

    def place_order(self, coin, is_buy, sz, limit_px, reduce_only=False, tif="Ioc",
                    trigger_px=None, cloid=None):
        self.calls.append(("order", coin, reduce_only, trigger_px))
        return {"ok": True, "status": "filled", "oid": 1, "avg_px": limit_px,
                "filled_sz": sz, "error": None, "raw": {}}

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        return {"ok": True, "status": "resting", "oid": oid, "error": None,
                "avg_px": None, "filled_sz": None, "raw": None}

    def writes(self):
        return [c for c in self.calls if c[0] in ("order", "cancel")]


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


# ---------------------------------------------------------------- 基类
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        # risk_pct=0.015 = 1.5% 档 → 告警 25% / 熔断 30%（用户拍板的那两条线）。
        # 默认就取这一档，边界用例才和规格 §9 的 24.9/25.0/29.9/30.0 对得上。
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.015, leverage=5.0, code_rev="testrev")

    def tearDown(self):
        self.tmp.cleanup()

    # ---- 工具 ----
    def executor(self, **kw) -> Executor:
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def seed_snaps(self, points) -> None:
        """写快照：(ts, equity) 列表；equity 走 equity_total（统一账户口径）。"""
        for ts, eq in points:
            self.db.hl_add_snapshot({"ts": ts, "equity_total": eq})

    def add_flow(self, ts: float, amount: float, kind: str = "deposit") -> None:
        self.db.hl_add_flow({"ts": ts, "kind": kind, "amount": amount,
                             "hash": f"h{ts}-{amount}"})

    def state(self):
        return hl_dd.load_state(self.db)

    def dd_round(self, equity: float, *, ts: float = NOW, spot_ok: bool = True,
                 flows_ok: bool = True, **kw):
        """跑一轮 DD 判断（不走整轮 tick）：返回 (结果, 告警列表, executor)。"""
        ex = self.executor(**kw)
        ex._flows_ok = flows_ok
        alerts = []
        res = ex._dd_guard(_snap(equity, ts=ts, spot_ok=spot_ok), alerts)
        return res, alerts, ex

    def set_state_dd(self, pct: float, *, peak: float = 100.0, ts: float = NOW,
                     last_eq: float = 1000.0) -> None:
        """直接摆一个「回撤恰好 pct」的状态，用于边界判线（不依赖快照序列）。"""
        hl_dd.save_state(self.db, {
            "anchor_ts": NOW - 5000, "anchor_eq": 1000.0, "anchor_cumflow": 0.0,
            "index": peak * (1.0 - pct), "peak_index": peak,
            "last_ts": ts, "last_eq": last_eq, "twr_skipped": 0, "updated_at": ts})

    @staticmethod
    def texts(alerts):
        return [a[1] if isinstance(a, tuple) else a for a in alerts]

    def events(self, kind=None):
        return [dict(r) for r in self.db.hl_recent_events(50, kind=kind)]


# ---------------------------------------------------------------- 1. 口径正确性
class TestCalculation(Base):
    def test_index_equals_equity_ratio_without_flows(self):
        """无出入金：TWR 指数 = 权益相对变化，DD = 峰谷回撤。"""
        self.seed_snaps([(NOW - 3000, 1000.0), (NOW - 2000, 900.0), (NOW - 1000, 1100.0)])
        res, alerts, _ = self.dd_round(990.0)
        # 100 × (900/1000) × (1100/900) × (990/1100) = 99，峰值 110
        self.assertAlmostEqual(res["index"], 99.0, places=6)
        self.assertAlmostEqual(res["peak_index"], 110.0, places=6)
        self.assertAlmostEqual(res["dd"], 0.10, places=6)
        self.assertEqual(alerts, [], "10% 回撤不该有任何告警")

    def test_deposit_does_not_move_drawdown(self):
        """有入金：入金前后 DD 不跳变（10-21 那笔 901.8U 的关键回归）。"""
        self.seed_snaps([(NOW - 3000, 1000.0), (NOW - 2000, 900.0)])
        self.add_flow(NOW - 1500, 900.0)                  # 亏 10% 之后入金 900
        res, _, _ = self.dd_round(1800.0)                 # 权益 900 + 900 = 1800
        self.assertAlmostEqual(res["index"], 90.0, places=6)
        self.assertAlmostEqual(res["dd"], 0.10, places=6,
                               msg="入金把权益顶回去，但回撤必须还是 10%（不是 0）")

    def test_withdraw_is_not_counted_as_loss(self):
        """有提现：提现不被算成亏损。"""
        self.seed_snaps([(NOW - 3000, 1000.0), (NOW - 2000, 1000.0)])
        self.add_flow(NOW - 1500, -500.0, kind="withdraw")
        res, alerts, _ = self.dd_round(500.0)
        self.assertAlmostEqual(res["index"], 100.0, places=6)
        self.assertAlmostEqual(res["dd"], 0.0, places=6)
        self.assertEqual(alerts, [])

    def test_prev_equity_non_positive_skips_step(self):
        """上一格权益 <= 0：不猜，指数原地不动、twr_skipped +1。"""
        self.seed_snaps([(NOW - 3000, 1000.0)])
        hl_dd.save_state(self.db, {
            "anchor_ts": NOW - 3000, "anchor_eq": 1000.0, "anchor_cumflow": 0.0,
            "index": 100.0, "peak_index": 100.0, "last_ts": NOW - 1000, "last_eq": 0.0,
            "twr_skipped": 0, "updated_at": NOW - 1000})
        res, _, _ = self.dd_round(500.0)
        self.assertAlmostEqual(res["index"], 100.0, places=6)
        self.assertEqual(res["twr_skipped"], 1)
        st = self.state()
        self.assertEqual(st["twr_skipped"], 1)
        self.assertAlmostEqual(st["last_eq"], 500.0, places=6)


class TestBootstrapReplay(Base):
    """§4.3.1：首次建立锚点必须逐张快照重放，不能「锚点 → 当前」一步算。"""

    def _seed_big_deposit(self):
        # 线上真实场景的骨架：锚点 100.79 → 亏到 95 → 入金 901.8 → 现在 991
        self.seed_snaps([(NOW - 4000, 100.79), (NOW - 3000, 95.0),
                         (NOW - 2000, 996.8), (NOW - 1000, 991.0)])
        self.add_flow(NOW - 2500, 901.8)

    def test_replay_differs_from_naive_single_step(self):
        """锚点跨一次入金：重放结果 ≠ 朴素一步（钉死假回撤 bug）。"""
        self._seed_big_deposit()
        res, _, _ = self.dd_round(991.0)
        expect = 100.0 * (95.0 / 100.79) * ((996.8 - 901.8) / 95.0) * (991.0 / 996.8)
        naive = 100.0 * (991.0 - 901.8) / 100.79        # 把入金当亏损的错误算法
        self.assertAlmostEqual(res["index"], expect, places=6)
        self.assertNotAlmostEqual(res["index"], naive, places=2)
        # 假回撤要比真回撤深 4 个百分点以上（这正是会让没亏到线的账户报警的原因）
        self.assertGreater(1.0 - naive / 100.0, res["dd"] + 0.04)

    def test_replay_matches_flows_curve_twr(self):
        """重放结果与 flows.curve() 的 twr 末值同窗口一致（口径没分叉）。"""
        self._seed_big_deposit()
        snaps = self.db.hl_snapshots(0.0, 20000)
        flows = self.db.hl_flows_since(0.0)
        replay = hl_dd.bootstrap(snaps, flows, anchor_ts=NOW - 4000, anchor_eq=100.79)
        curve = hl_flows.curve(snaps, flows, now=NOW, window_s=30 * DAY)
        self.assertAlmostEqual(replay["index"], curve["twr"][-1][1], places=6)
        self.assertEqual(curve["meta"]["twr_skipped"], replay["twr_skipped"])

    def test_peak_index_is_max_of_series(self):
        """重放后 peak_index = 期间 index 最大值（峰值不被遗忘）。"""
        self.seed_snaps([(NOW - 3000, 1000.0), (NOW - 2000, 1200.0), (NOW - 1000, 900.0)])
        replay = hl_dd.bootstrap(self.db.hl_snapshots(0.0, 20000),
                                 self.db.hl_flows_since(0.0),
                                 anchor_ts=NOW - 3000, anchor_eq=1000.0)
        self.assertAlmostEqual(replay["index"], 90.0, places=6)
        self.assertAlmostEqual(replay["peak_index"], 120.0, places=6)

    def test_replay_is_idempotent(self):
        """同一批快照重放两次，结果完全一致；同一轮重复跑不推进。"""
        self._seed_big_deposit()
        snaps = self.db.hl_snapshots(0.0, 20000)
        flows = self.db.hl_flows_since(0.0)
        a = hl_dd.bootstrap(snaps, flows, anchor_ts=NOW - 4000, anchor_eq=100.79)
        b = hl_dd.bootstrap(snaps, flows, anchor_ts=NOW - 4000, anchor_eq=100.79)
        self.assertEqual(a, b)
        r1, _, _ = self.dd_round(991.0, ts=NOW)
        st1 = self.state()
        r2, _, _ = self.dd_round(500.0, ts=NOW)          # 同一秒再跑一次
        st2 = self.state()
        self.assertEqual(st1, st2, "同一 ts 重复跑不许把收益率算两遍")
        self.assertAlmostEqual(r2["index"], r1["index"], places=9)
        self.assertFalse(r2["advanced"])


# ---------------------------------------------------------------- 2. 状态持久化
class TestPersistence(Base):
    def test_anchor_built_once_and_index_not_reset(self):
        self.seed_snaps([(NOW - 2000, 1000.0)])
        r1, _, _ = self.dd_round(950.0, ts=NOW - 1000)
        self.assertAlmostEqual(r1["index"], 95.0, places=6)
        r2, _, _ = self.dd_round(900.0, ts=NOW)
        self.assertAlmostEqual(r2["index"], 90.0, places=6,
                               msg="第二轮必须从状态续算（95×900/950），不能重建锚点回 100")
        anchors = [e for e in self.events("dd") if "锚点建立" in (e["message"] or "")]
        self.assertEqual(len(anchors), 1, "锚点只建立一次")

    def test_state_survives_snapshot_prune(self):
        """hl_snapshots 被 30 天清理后，峰值与指数仍来自持久化状态。"""
        self.seed_snaps([(NOW - 2000, 1000.0)])
        self.dd_round(950.0, ts=NOW - 1000)
        self.db.conn.execute("DELETE FROM hl_snapshots")
        self.db.conn.commit()
        res, alerts, _ = self.dd_round(940.0, ts=NOW)
        self.assertIsNotNone(res, "有状态时不该因为快照被清空就判不可用")
        self.assertAlmostEqual(res["index"], 94.0, places=6)      # 95 × 940/950
        self.assertAlmostEqual(res["peak_index"], 100.0, places=6)  # 峰值没被遗忘
        self.assertAlmostEqual(res["dd"], 0.06, places=6)
        self.assertEqual(alerts, [])

    def test_new_executor_instance_continues(self):
        """重启（新 Executor 实例）后从状态续算，不重置锚点。"""
        self.seed_snaps([(NOW - 2000, 1000.0)])
        self.dd_round(950.0, ts=NOW - 1000)
        ex2 = Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)
        ex2._flows_ok = True
        res = ex2._dd_guard(_snap(900.0, ts=NOW), [])
        self.assertAlmostEqual(res["index"], 90.0, places=6)
        self.assertEqual(self.state()["anchor_eq"], 1000.0)

    def test_clock_rollback_does_not_advance(self):
        self.seed_snaps([(NOW - 2000, 1000.0)])
        self.dd_round(950.0, ts=NOW)
        before = self.state()
        res, _, _ = self.dd_round(100.0, ts=NOW - 600)     # 时钟回拨
        self.assertFalse(res["advanced"])
        self.assertAlmostEqual(res["index"], 95.0, places=6)
        self.assertEqual(self.state(), before, "不推进时不许写库")


# ---------------------------------------------------------------- 3. 阈值判线
class TestThresholds(Base):
    def test_boundary_249_no_alert(self):
        self.set_state_dd(0.249)
        res, alerts, _ = self.dd_round(1000.0)
        self.assertEqual(alerts, [])
        self.assertFalse(self.db.hl_kill())

    def test_boundary_250_alerts(self):
        self.set_state_dd(0.25)
        res, alerts, _ = self.dd_round(1000.0)
        self.assertEqual(len(alerts), 1)
        self.assertIn("策略回撤 25.0%（≥ 告警线 25%）", self.texts(alerts)[0])
        self.assertFalse(self.db.hl_kill(), "L2 只推送，不停手")

    def test_boundary_299_alerts_only(self):
        self.set_state_dd(0.299)
        res, alerts, _ = self.dd_round(1000.0)
        self.assertEqual(len(alerts), 1)
        self.assertFalse(self.db.hl_kill())

    def test_boundary_300_kills(self):
        self.set_state_dd(0.30)
        res, alerts, _ = self.dd_round(1000.0)
        self.assertTrue(self.db.hl_kill())
        self.assertIn("回撤熔断", self.texts(alerts)[0])

    def test_tier_follows_risk_pct(self):
        """档位联动：改 risk_pct，线必须同批跟着走（§3.4）。"""
        self.assertEqual((hl_dd.thresholds(0.01)["warn"], hl_dd.thresholds(0.01)["kill"]),
                         (0.25, 0.30))
        self.assertEqual((hl_dd.thresholds(0.02)["warn"], hl_dd.thresholds(0.02)["kill"]),
                         (0.30, 0.40))
        self.assertEqual((hl_dd.thresholds(0.03)["warn"], hl_dd.thresholds(0.03)["kill"]),
                         (0.40, 0.50))
        # 35% 回撤：2% 档只告警；3% 档连告警都不到
        self.set_state_dd(0.35)
        _, alerts, _ = self.dd_round(1000.0, risk_pct=0.02)
        self.assertEqual(len(alerts), 1)
        self.assertFalse(self.db.hl_kill())
        self.db.set_config("hl_exec_kill", 0)
        self.db.set_config("hl_dd_alert_at", 0)
        _, alerts2, _ = self.dd_round(1000.0, risk_pct=0.03)
        self.assertEqual(alerts2, [], "3% 档告警线是 40%，35% 不该喊")
        # 1.5% 档：35% ≥ 30% → 熔断
        _, _, _ = self.dd_round(1000.0, risk_pct=0.01)
        self.assertTrue(self.db.hl_kill())

    def test_manual_override_applies(self):
        self.set_state_dd(0.12)
        _, alerts, _ = self.dd_round(1000.0, dd_warn_pct=0.10, dd_kill_pct=0.20)
        self.assertEqual(len(alerts), 1)
        self.assertFalse(self.db.hl_kill())
        self.set_state_dd(0.22)
        self.db.set_config("hl_dd_alert_at", 0)
        _, alerts2, _ = self.dd_round(1000.0, dd_warn_pct=0.10, dd_kill_pct=0.20)
        self.assertTrue(self.db.hl_kill())
        self.assertIn("熔断线 20%", self.texts(alerts2)[0])

    def test_warn_not_less_than_kill_is_rejected(self):
        """warn >= kill：两者一起退回档位默认（不采用这个自相矛盾的组合）。"""
        th = hl_dd.thresholds(0.02, 0.45, 0.40)
        self.assertTrue(th["rejected"])
        self.assertEqual((th["warn"], th["kill"]), (0.30, 0.40))
        th2 = hl_dd.thresholds(0.02, 0.30, 0.30)
        self.assertTrue(th2["rejected"])

    def test_config_apply_db_rejects_inconsistent_pair(self):
        from app.config import Config
        c = Config()
        c.apply_db({"hl_dd_warn_pct": "0.45", "hl_dd_kill_pct": "0.40"})
        self.assertEqual((c.hl_dd_warn_pct, c.hl_dd_kill_pct), (0.0, 0.0),
                         "自相矛盾的一对必须一起拒收（保留原值 0=自动）")
        c.apply_db({"hl_dd_warn_pct": "0.35", "hl_dd_kill_pct": "0.45"})
        self.assertEqual((c.hl_dd_warn_pct, c.hl_dd_kill_pct), (0.35, 0.45))
        c.apply_db({"hl_dd_kill_pct": "0.97"})            # 越界（>0.95）拒收
        self.assertEqual(c.hl_dd_kill_pct, 0.45)
        c.apply_db({"hl_dd_warn_pct": "0"})               # 0 = 回到档位自动
        self.assertEqual(c.hl_dd_warn_pct, 0.0)


# ---------------------------------------------------------------- 4. fail-closed
class TestFailClosed(Base):
    def test_flows_unavailable_does_not_kill(self):
        self.seed_snaps([(NOW - 2000, 1000.0)])
        self.info.raise_ledger = True
        ex = self.executor()
        ex._sync_flows()                                   # 失败：只 warning，不抛
        alerts = []
        res = ex._dd_guard(_snap(100.0), alerts)           # 权益塌了也不许判
        self.assertIsNone(res)
        self.assertFalse(self.db.hl_kill())
        self.assertIn("回撤口径暂不可用", self.texts(alerts)[0])
        self.assertIn("L1 当日亏损熔断仍在生效", self.texts(alerts)[0])

    def test_no_anchor_does_not_kill(self):
        self.seed_snaps([(NOW - 3000, 0.0), (NOW - 2000, 0.0)])   # 全是「还没本金」
        res, alerts, _ = self.dd_round(100.0)
        self.assertIsNone(res)
        self.assertFalse(self.db.hl_kill())
        self.assertIn("找不到正权益快照", self.texts(alerts)[0])

    def test_spot_not_ok_does_not_kill(self):
        self.seed_snaps([(NOW - 2000, 1000.0)])
        res, alerts, _ = self.dd_round(100.0, spot_ok=False)
        self.assertIsNone(res)
        self.assertFalse(self.db.hl_kill())
        self.assertEqual(alerts, [], "spot_ok=False 只跳过，不喊人（L1 也会跳过）")

    def test_guard_switch_off_disables_l2_l3(self):
        self.set_state_dd(0.80)
        res, alerts, _ = self.dd_round(1000.0, dd_guard=False)
        self.assertIsNone(res)
        self.assertFalse(self.db.hl_kill())
        self.assertEqual(alerts, [])


# ---------------------------------------------------------------- 5. kill 行为
class TestKill(Base):
    def test_kill_origin_is_dd(self):
        self.set_state_dd(0.31)
        self.dd_round(1000.0)
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(str(self.db.get_config_str("hl_exec_kill_origin")), "dd")
        self.assertNotEqual(str(self.db.get_config_str("hl_exec_kill_origin")), "reconcile")

    def test_kill_does_not_close_positions(self):
        """L3 触发**不平仓**：假 exchange 上不许出现任何 reduce-only 平仓单。"""
        pos = {"coin": "BTC", "side": 1, "size": 0.01, "entry_px": 83000.0, "mark_px": 83000.0,
               "position_value": 830.0, "liq_px": 0.0, "leverage": 5, "isolated": True,
               "margin_used": 166.0, "unrealized_pnl": 0.0, "roe": 0.0,
               "funding_since_open": 0.0}
        stop = {"coin": "BTC", "oid": 7, "is_buy": False, "sz": 0.01, "limit_px": 0.0,
                "reduce_only": True, "trigger_px": 80600.0, "is_trigger": True,
                "timestamp": int(NOW * 1000)}
        self.info.snap = _snap(1000.0, positions=[pos], orders=[stop])
        self.seed_snaps([(NOW - 2000, 1000.0)])
        self.set_state_dd(0.31)
        ex = self.executor()
        ex._flows_ok = True
        alerts = []
        ex._dd_guard(self.info.snap, alerts)
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.ex.writes(), [], "熔断只停新仓，不许发任何下单/撤单")

    def test_dd_kill_is_not_auto_cleared(self):
        """origin='dd' 不在窄口径里：跑两轮干净也仍在 kill。"""
        self.set_state_dd(0.31)
        self.dd_round(1000.0)
        self.assertTrue(self.db.hl_kill())
        ex = self.executor()
        rec = {"ok": True, "problems": [], "unknown": False, "fill_closed": [],
               "closing_pending": []}
        snap = {"orders_trusted": True, "open_orders": [], "positions": []}
        for _ in range(2):
            ex._maintain_kill_autoclear(rec, snap)
        self.assertTrue(self.db.hl_kill(), "dd 熔断只能人工解除")

    def test_l2_does_not_set_kill(self):
        self.set_state_dd(0.26)
        self.dd_round(1000.0)
        self.assertFalse(self.db.hl_kill())

    def test_existing_kill_origin_is_not_rewritten(self):
        """已有别的来源造成停手：保留原 origin（不改写归因）。"""
        self.db.hl_set_kill(True, "人工停手", origin="manual")
        self.set_state_dd(0.31)
        self.dd_round(1000.0)
        self.assertEqual(str(self.db.get_config_str("hl_exec_kill_origin")), "manual")

    def test_repeat_reminder_is_throttled(self):
        """已在 dd 熔断里：6h 内不再重复推。"""
        self.set_state_dd(0.31)
        _, alerts1, _ = self.dd_round(1000.0)
        self.assertEqual(len(alerts1), 1)
        self.db.set_config("hl_dd_kill_at", str(int(NOW)))      # 模拟推送成功回写
        _, alerts2, _ = self.dd_round(1000.0, ts=NOW + 600)
        self.assertEqual(alerts2, [])
        _, alerts3, _ = self.dd_round(1000.0, ts=NOW + 7 * 3600)
        self.assertEqual(len(alerts3), 1)
        self.assertIn("仍在生效", self.texts(alerts3)[0])


# ---------------------------------------------------------------- 6. L1 口径修复
class TestDailyLoss(Base):
    def _round(self, equity: float, *, raise_ledger: bool = False):
        self.info.snap = _snap(equity)
        self.info.raise_ledger = raise_ledger
        with mock.patch("app.hl.executor._today_start_ts", return_value=DAY0):
            ex = self.executor()
            alerts = ex.run()
        return ex, alerts

    def test_withdrawal_does_not_trigger_l1(self):
        """当日提现 100U：旧口径 -10% 会误熔断，新口径 0%。"""
        self.seed_snaps([(DAY0 + 10, 1000.0)])
        self.add_flow(DAY0 + 20, -100.0, kind="withdraw")
        ex, alerts = self._round(900.0)
        self.assertFalse(self.db.hl_kill(), "提现不是亏损")
        self.assertFalse(any("当日亏损" in a for a in alerts))

    def test_deposit_then_loss_is_measured_on_strategy_capital(self):
        """当日入金 500 后真亏 60 → 亏损率 6%（不是 4.0%），能触发。"""
        self.seed_snaps([(DAY0 + 10, 1000.0)])
        self.add_flow(DAY0 + 20, 500.0)
        ex, alerts = self._round(1440.0)
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(str(self.db.get_config_str("hl_exec_kill_origin")), "daily_loss")
        self.assertIn("当日亏损 -6.0%", " ".join(alerts))

    def test_deposit_then_small_loss_does_not_trigger(self):
        """同一场景只亏 40（4%）→ 不触发（旧口径会被入金稀释成 4.0% 也一样，
        但亏 45 时旧口径 4.5%、新口径 4.5% —— 关键差别在「入金不计入本金」）。"""
        self.seed_snaps([(DAY0 + 10, 1000.0)])
        self.add_flow(DAY0 + 20, 500.0)
        ex, alerts = self._round(1460.0)
        self.assertFalse(self.db.hl_kill())

    def test_flows_unavailable_falls_back_to_old_formula(self):
        """流水不可用 → 退回旧口径，并在文案里标注「未剔除出入金」。"""
        self.seed_snaps([(DAY0 + 10, 1000.0)])
        self.add_flow(DAY0 + 20, -100.0, kind="withdraw")
        ex, alerts = self._round(900.0, raise_ledger=True)
        self.assertTrue(self.db.hl_kill(), "退回旧口径时提现会被当成 -10% 亏损（保守）")
        self.assertTrue(any("未剔除出入金" in a for a in alerts), alerts)

    def test_flow_window_starts_at_first_snapshot_not_midnight(self):
        """窗口左开区间钉在**首快照时刻**：零点→首快照之间的入金不许被扣两次。

        50U 入金落在零点后 0.5s、首快照在零点后 1s（权益已含这 50）、之后真亏 10：
          错误窗口 (day0, now] → -5.71%（会误触发 5% 线）
          正确窗口 (base_ts, now] → -0.95%
        """
        self.seed_snaps([(DAY0 + 1, 1050.0)])
        self.add_flow(DAY0 + 0.5, 50.0)
        ex, alerts = self._round(1040.0)
        self.assertFalse(self.db.hl_kill(), f"同一笔入金被扣两次就会误熔断：{alerts}")
        self.assertFalse(any("当日亏损" in a for a in alerts))
        ev = self.events("kill")
        self.assertEqual(ev, [])
        # 反证：把窗口左端换成 day0 就会跌破 -5%（说明这条用例真的在钉边界）
        wrong = ((1040.0 - 1050.0) - hl_dd.net_flow(self.db.hl_flows_since(0.0), DAY0, NOW)) / 1050.0
        self.assertLessEqual(wrong, -0.05)


# ---------------------------------------------------------------- 7. 面板渲染
class TestPanel(Base):
    def _payload(self):
        from app.hl.views import DashboardDeps, status_payload
        # 面板的 flows_ok 判据 = 「有过一次成功同步」或「本地已有流水」（views 既有口径）；
        # 这里写水位就够，不必真的造一笔入金。
        self.db.set_config("hl_flows_synced_at", str(int(NOW)))
        cfg = type("C", (), {"symbols": ["BTC_USDT"], "risk_pct": 0.015,
                             "hl_dd_guard": True, "hl_dd_warn_pct": 0.0,
                             "hl_dd_kill_pct": 0.0})()
        deps = DashboardDeps(db=self.db, config_fn=lambda: cfg, info=self.info,
                             address_fn=lambda: "0xabc", mode_fn=lambda: "live",
                             now_fn=lambda: NOW)
        return status_payload(deps)

    def test_render_shows_drawdown_and_status(self):
        from app.hl.views import render_dashboard
        # 两个点让 pnl 曲线能画出来（旧图例行必须还在）
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 990.0)])
        self.set_state_dd(0.0205)                       # DD 2.05%
        html = render_dashboard(self._payload())
        self.assertIn("策略回撤 2.05%", html)
        self.assertIn("当前状态：", html)
        self.assertIn("正常", html)
        self.assertIn("告警线 25%", html)
        self.assertIn("熔断线 30%", html)
        self.assertIn("实线=累计盈亏（U，左轴）", html, "旧图例行不许动")
        self.assertEqual(html.count("策略回撤"), 1, "同一口径全页只出现一次")

    def test_render_when_unavailable(self):
        from app.hl.views import render_dashboard
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 990.0)])
        html = render_dashboard(self._payload())        # 没有 hl_dd_state
        self.assertIn("策略回撤 —", html)
        self.assertIn("暂不可用", html)

    def test_render_killed_state(self):
        from app.hl.views import render_dashboard
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 990.0)])
        self.set_state_dd(0.31)
        self.dd_round(1000.0)                           # 真触发一次 kill，origin=dd
        html = render_dashboard(self._payload())
        self.assertIn("已熔断", html)

    def test_render_when_guard_disabled(self):
        """总开关关掉：状态词复用「暂不可用」，括号里说明是自己关的。"""
        from app.hl.views import DashboardDeps, render_dashboard, status_payload
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 990.0)])
        self.db.set_config("hl_flows_synced_at", str(int(NOW)))
        cfg = type("C", (), {"symbols": ["BTC_USDT"], "risk_pct": 0.02,
                             "hl_dd_guard": False, "hl_dd_warn_pct": 0.0,
                             "hl_dd_kill_pct": 0.0})()
        deps = DashboardDeps(db=self.db, config_fn=lambda: cfg, info=self.info,
                             address_fn=lambda: "0xabc", mode_fn=lambda: "live",
                             now_fn=lambda: NOW)
        payload = status_payload(deps)
        self.assertEqual(payload["curve"]["meta"]["dd_status"], "off")
        html = render_dashboard(payload)
        self.assertIn("暂不可用（回撤熔断已关闭）", html)

    def test_api_curve_meta_has_dd_fields(self):
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 990.0)])
        self.set_state_dd(0.0205)
        meta = self._payload()["curve"]["meta"]
        for key in ("dd_ok", "dd_pct", "dd_index", "dd_peak_index", "dd_warn_pct",
                    "dd_kill_pct", "dd_status", "dd_status_text"):
            self.assertIn(key, meta, f"curve.meta 缺 {key}（§10.5）")
        self.assertAlmostEqual(meta["dd_pct"], 0.0205, places=4)
        self.assertEqual(meta["dd_status"], "normal")


# ---------------------------------------------------------------- 8. 顺序纪律
class TestOrder(Base):
    def test_sync_flows_runs_before_drawdown(self):
        order = []
        ex = self.executor()
        real_sync = ex._sync_flows
        real_dd = ex._dd_guard

        def sync(*a, **k):
            order.append("sync_flows")
            return real_sync(*a, **k)

        def dd(snap, alerts):
            order.append("dd_guard")
            return real_dd(snap, alerts)

        ex._sync_flows = sync
        ex._dd_guard = dd
        self.info.snap = _snap(1000.0)
        ex.run()
        self.assertEqual(order, ["sync_flows", "dd_guard"],
                         "流水同步必须早于回撤判断（否则用最长 30 分钟前的流水算回撤）")


# ---------------------------------------------------------------- 附：面板阈值输入（§6）
class TestAdminThresholds(Base):
    """「策略与保护」页的两个阈值输入框：留空不改、越界/自相矛盾两条一起拒收。

    这里只测纯函数与渲染片段（不搭 FastAPI app）：路由层只是把这两个函数接上，
    它的表单语义（缺字段=不变）已由 test_admin_ui 的既有用例覆盖。
    """

    CFG = type("C", (), {"risk_pct": 0.02, "hl_dd_warn_pct": 0.0, "hl_dd_kill_pct": 0.0})

    def test_empty_inputs_write_nothing(self):
        from app.admin_ui import _parse_dd_thresholds
        self.assertEqual(_parse_dd_thresholds("", "", self.CFG), ({}, ""),
                         "留空 = 保持原值，绝不能把空串写成 0")
        self.assertEqual(_parse_dd_thresholds(None, None, self.CFG), ({}, ""))

    def test_parse_valid_pair(self):
        from app.admin_ui import _parse_dd_thresholds
        vals, msg = _parse_dd_thresholds("0.35", "0.45", self.CFG)
        self.assertEqual(vals, {"hl_dd_warn_pct": 0.35, "hl_dd_kill_pct": 0.45})
        self.assertEqual(msg, "")

    def test_parse_rejects_inconsistent_or_out_of_range(self):
        from app.admin_ui import _parse_dd_thresholds
        for warn, kill in (("0.45", "0.40"), ("0.30", "0.30"), ("1.2", "0.40"), ("abc", "0.40")):
            vals, msg = _parse_dd_thresholds(warn, kill, self.CFG)
            self.assertEqual(vals, {}, f"{warn}/{kill} 应被拒收（两条都不写）")
            self.assertTrue(msg)

    def test_fields_show_override_not_auto_value(self):
        from app.admin_ui import _dd_threshold_fields
        html = _dd_threshold_fields(self.CFG, {})
        self.assertIn("name='hl_dd_warn_pct'", html)
        self.assertIn("name='hl_dd_kill_pct'", html)
        self.assertIn("value=''", html, "自动档位时输入框必须留空（否则一保存就变显式覆盖）")
        self.assertIn("告警 30% / 熔断 40%", html, "当前生效值要写在小字里（risk_pct=2% 档）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
