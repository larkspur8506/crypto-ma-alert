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
from urllib.parse import unquote

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
        # 账本行（真 dict，走 hl_flows.classify）：用于「这一轮真的同步到了流水」的场景。
        self.ledger_rows = []

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
        return [dict(r) for r in self.ledger_rows]


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
                 flows_fresh: bool = True, **kw):
        """跑一轮 DD 判断（不走整轮 tick）：返回 (结果, 告警列表, executor)。

        `flows_fresh` 对应执行器的「本轮真的同步到流水了吗」（§4.4.1 高1）：
        默认 True 表示这一轮流水是新的，可以推进指数链；传 False 用于模拟闸门内跳过。
        """
        ex = self.executor(**kw)
        ex._flows_fresh = flows_fresh
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

    # ---- v2：整轮（真跑 _sync_flows 闸门 + 对账 + L1 + DD） ----
    @staticmethod
    def ledger_row(typ: str, ts: float, **delta) -> dict:
        """构造一条交易所账本行（走真 classify，不用自己拼流水）。"""
        d: dict = {"type": typ, **{k: str(v) for k, v in delta.items()}}
        return {"time": int(float(ts) * 1000), "hash": f"{typ}-{ts}", "delta": d}

    def run_round(self, equity: float, *, ts: float = NOW, ledger_rows=None,
                  gate: bool = False, **kw):
        """跑一整轮 `Executor.run()`，返回 (executor, 告警文案列表)。

        `gate=True`：把 `hl_flows_synced_at` 摆成「刚刚同步过」→ 命中 30 分钟间隔闸门，
        本轮**不会**调账本接口（`flows_fresh=False`）。注意闸门读的是真实时钟
        （`time.time()`），不是快照 ts —— 所以这里用 `time.time()` 而不是 NOW。
        `gate=False`：清掉水位，让本轮真的去拉账本（返回 `ledger_rows`）。
        """
        self.info.snap = _snap(equity, ts=ts)
        self.info.ledger_rows = list(ledger_rows or [])
        self.db.set_config("hl_flows_synced_at", str(int(time.time())) if gate else "0")
        ex = self.executor(**kw)
        return ex, ex.run()


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
        ex2._flows_fresh = True
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
        """流水接口挂了 → 不判、不 kill，24h 节流提示（v2：L1 也不再判）。"""
        self.seed_snaps([(NOW - 2000, 1000.0)])
        self.info.raise_ledger = True
        ex = self.executor()
        ex._sync_flows()                                   # 失败：只 warning，不抛
        self.assertFalse(ex._flows_fresh, "接口失败必须报「本轮没同步」")
        alerts = []
        res = ex._dd_guard(_snap(100.0), alerts)           # 权益塌了也不许判
        self.assertIsNone(res)
        self.assertFalse(self.db.hl_kill())
        self.assertIn("回撤口径暂不可用", self.texts(alerts)[0])
        # v2（§4.4.1 高1b）：文案不许再承诺「L1 还兜着」——它同样依赖本轮流水
        self.assertIn("当日亏损熔断同样未判", self.texts(alerts)[0])
        self.assertNotIn("仍在生效", self.texts(alerts)[0])
        # 面板可见的「等待流水同步」说明也被写上（§4.4.1）
        self.assertIn("等待流水同步", str(self.db.get_config_str(hl_dd.WAIT_KEY, "")))

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
        ex._flows_fresh = True
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

    def test_flows_unavailable_skips_l1_entirely(self):
        """【v2·高1b】流水不可用 → **本轮不判** L1（不再退回会误杀的旧口径）。

        v1 这里退回「不剔出入金」的旧公式：提现 100U 立刻变成 -10% → 误 kill。
        v2（§4.4.1 / §5）：宁可不判，等下一轮同步到流水再算。
        """
        self.seed_snaps([(DAY0 + 10, 1000.0)])
        self.add_flow(DAY0 + 20, -100.0, kind="withdraw")
        ex, alerts = self._round(900.0, raise_ledger=True)
        self.assertFalse(self.db.hl_kill(), f"提现不是亏损，不许误杀：{alerts}")
        self.assertFalse(any("已停止自动交易" in a for a in alerts), alerts)
        self.assertFalse(any("未剔除出入金" in a for a in alerts),
                         "旧口径已经删除，不该再出现「未剔除出入金」的算法分支")

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


# ================================================================ 9. v2 回归（对抗审查 9 条）
# 规格 §0.2 / §9 第 9 组。**关键**：v1 的用例全是「先把流水写进库再跑一轮」，正好绕过
# 高1 那条缺陷 —— 这里的用例必须模拟「流水还没同步」（闸门内、接口失败、账本后到）。
class TestV2FlowFreshness(Base):
    """高1 / 高1b：流水没同步到本轮 → 不推进指数链、不判线（L2/L3/L1 全不判）。"""

    def _established(self):
        """摆一个「锚点已建立、index=100、上一轮权益 1000」的状态（等价首轮结束）。"""
        self.seed_snaps([(NOW - 4000, 1000.0)])
        hl_dd.save_state(self.db, {
            "anchor_ts": NOW - 4000, "anchor_eq": 1000.0, "anchor_cumflow": 0.0,
            "index": 100.0, "peak_index": 100.0, "last_ts": NOW - 3000, "last_eq": 1000.0,
            "twr_skipped": 0, "neg_streak": 0, "updated_at": NOW - 3000})

    def test_gate_round_does_not_advance_chain_nor_kill(self):
        """闸门内（30 分钟没真同步）：提现 500U 不许被算成亏损、不许误 kill。

        v1 的实测：这一轮 index 会算成 100×500/1000 = 50 → DD 50% → 误 kill
        「当日亏损 −50%」，而且 index 永久停在 50（后续流水同步到了也不回滚）。
        """
        self._established()
        ex, alerts = self.run_round(500.0, ts=NOW - 2400, gate=True)
        self.assertFalse(ex._flows_fresh, "闸门内必须报「本轮没同步」")
        self.assertEqual(ex._flows_skip, "尚未到流水同步间隔")
        self.assertEqual(self.info.ledger_calls, 0, "闸门内不该调账本接口")
        st = self.state()
        self.assertAlmostEqual(st["index"], 100.0, places=6, msg="闸门内不许推进指数链")
        self.assertEqual(st["last_ts"], NOW - 3000, "last_ts 必须原地不动")
        self.assertAlmostEqual(st["last_eq"], 1000.0, places=6, msg="last_eq 也不许动")
        self.assertFalse(self.db.hl_kill(), "闸门内不许判线：提现不是亏损")
        self.assertFalse(any("回撤" in a for a in alerts), alerts)
        self.assertFalse(any("已停止自动交易" in a for a in alerts), alerts)

    def test_next_fresh_round_uses_correct_flow(self):
        """下一轮真同步到那笔提现 → 用正确的 flow_k 推进，指数不吸收未知出入金。"""
        self._established()
        # ① 闸门内那一轮：权益掉到 500，但流水还没落地 → 不判、不推进
        self.run_round(500.0, ts=NOW - 2400, gate=True)
        # ② 走出闸门：账本把 30 分钟前的提现返回了
        row = self.ledger_row("withdraw", NOW - 2500, usdc="500")
        ex, alerts = self.run_round(500.0, ts=NOW - 600, ledger_rows=[row],
                                    gate=False)
        self.assertTrue(ex._flows_fresh)
        st = self.state()
        # 100 × (500 − (−500)) / 1000 = 100：出入金全额剔除，指数压根不动
        self.assertAlmostEqual(st["index"], 100.0, places=6,
                               msg=f"闸门内那笔提现不许污染指数链：{st['index']}")
        self.assertAlmostEqual(st["last_eq"], 500.0, places=6)
        self.assertFalse(self.db.hl_kill(), f"回滚后仍不该熔断：{alerts}")

    def test_clean_path_gives_identical_index(self):
        """对照组：「先把流水写进库再跑」→ 同一个 index（说明没有残留污染）。"""
        self._established()
        self.add_flow(NOW - 2500, -500.0, kind="withdraw")
        self.run_round(500.0, ts=NOW - 600, gate=False)
        self.assertAlmostEqual(self.state()["index"], 100.0, places=6)

    def test_ledger_failure_does_not_advance(self):
        """接口失败（不是闸门内）→ 同样不推进、不判线，但会提示 + 面板可见原因。"""
        self._established()
        self.info.raise_ledger = True
        ex, alerts = self.run_round(500.0, ts=NOW - 600, gate=False)
        self.assertFalse(ex._flows_fresh)
        self.assertAlmostEqual(self.state()["index"], 100.0, places=6)
        self.assertFalse(self.db.hl_kill())
        self.assertTrue(any("回撤口径暂不可用" in a for a in alerts), alerts)
        self.assertIn("等待流水同步", str(self.db.get_config_str(hl_dd.WAIT_KEY, "")))

    def test_wait_note_cleared_after_successful_advance(self):
        """恢复正常后必须把「等待流水同步」清掉，否则面板永远显示旧故障。"""
        self._established()
        self.db.set_config(hl_dd.WAIT_KEY, "等待流水同步（出入金流水接口读取失败）")
        self.run_round(500.0, ts=NOW - 600, ledger_rows=[], gate=False)
        self.assertEqual(str(self.db.get_config_str(hl_dd.WAIT_KEY, "")), "")

    def test_panel_shows_waiting_for_flow_sync(self):
        from app.hl.views import DashboardDeps, render_dashboard, status_payload
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 990.0)])
        self.set_state_dd(0.02)
        self.db.set_config("hl_flows_synced_at", str(int(NOW)))
        self.db.set_config(hl_dd.WAIT_KEY, "等待流水同步（出入金流水接口读取失败）")
        cfg = type("C", (), {"symbols": ["BTC_USDT"], "risk_pct": 0.015, "hl_dd_guard": True,
                             "hl_dd_warn_pct": 0.0, "hl_dd_kill_pct": 0.0})()
        deps = DashboardDeps(db=self.db, config_fn=lambda: cfg, info=self.info,
                             address_fn=lambda: "0xabc", mode_fn=lambda: "live",
                             now_fn=lambda: NOW)
        html = render_dashboard(status_payload(deps))
        self.assertIn("等待流水同步", html)
        # 2026-10-29 用户要求：固定口径括注「按流水同步节奏更新」已删；
        # dd_wait_note（真实的「等待流水同步（原因）」）仍是唯一的新鲜度告警出口
        self.assertNotIn("按流水同步节奏更新", html)


class TestV2L1Freshness(Base):
    """高1b：L1 同样只在「本轮真同步到流水」时判（否则提现照样误杀）。"""

    def _round(self, equity, *, ts=NOW, gate=False, ledger_rows=None):
        self.info.snap = _snap(equity, ts=ts)
        self.info.ledger_rows = list(ledger_rows or [])
        self.db.set_config("hl_flows_synced_at", str(int(time.time())) if gate else "0")
        with mock.patch("app.hl.executor._today_start_ts", return_value=DAY0):
            ex = self.executor()
            alerts = ex.run()
        return ex, alerts

    def test_gate_skips_l1_so_withdrawal_is_not_a_loss(self):
        self.seed_snaps([(DAY0 + 10, 1000.0)])
        # 提现 500 尚未同步：库里一笔流水都没有，闸门内本轮拿不到
        ex, alerts = self._round(500.0, gate=True)
        self.assertFalse(ex._flows_fresh)
        self.assertFalse(self.db.hl_kill(),
                         f"闸门内 L1 不许判（否则提现 = −50% 误杀）：{alerts}")
        self.assertFalse(any("已停止自动交易" in a for a in alerts), alerts)

    def test_fresh_round_l1_still_catches_real_loss(self):
        """对照：真同步到流水（本轮无出入金）→ 真亏 10% 仍然熔断。"""
        self.seed_snaps([(DAY0 + 10, 1000.0)])
        ex, alerts = self._round(900.0, gate=False, ledger_rows=[])
        self.assertTrue(ex._flows_fresh)
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(str(self.db.get_config_str("hl_exec_kill_origin")), "daily_loss")


class TestV2ManualUnkill(Base):
    """高2 / 高2b：人工解 DD 熔断必须真能恢复；「重置基线」按钮只管基线。

    走真路由（TestClient）：规格要求「解除 kill 与重置峰值在**同一个请求**内完成」，
    只在纯函数层面测就盖不住这一点。
    """

    TOKEN = "dd-token"

    def _client(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from app.admin_ui import AdminDeps, build_admin_router
        except ImportError:                                        # pragma: no cover
            self.skipTest("fastapi 未安装")
        cfg = type("C", (), {"risk_pct": 0.015, "hl_dd_guard": True,
                             "hl_dd_warn_pct": 0.0, "hl_dd_kill_pct": 0.0})()
        deps = AdminDeps(db=self.db, config_fn=lambda: cfg, hl_deps=None,
                         mode_fn=lambda: "live", token_fn=lambda: self.TOKEN,
                         address_fn=lambda: "0xabc", private_key_fn=lambda: "")
        app = FastAPI()
        app.include_router(build_admin_router(deps))
        return TestClient(app, follow_redirects=False)

    def test_unkill_dd_resets_peak_then_no_rekill(self):
        self.set_state_dd(0.31)                       # index 69 / peak 100
        _, _, _ = self.dd_round(1000.0, ts=NOW - 100)
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(str(self.db.get_config_str("hl_exec_kill_origin")), "dd")
        before = self.state()

        r = self._client().post("/admin/switch/kill", data={"on": "0", "k": self.TOKEN})
        self.assertEqual(r.status_code, 303)
        self.assertFalse(self.db.hl_kill(), "人工解除必须真的解除")
        self.assertAlmostEqual(self.state()["peak_index"], before["index"], places=6,
                               msg="解除 dd 熔断必须同时把 peak 重置为当前 index")
        self.assertIn("回撤基线已重置", unquote(r.headers.get("location", "")))

        res, alerts, _ = self.dd_round(1000.0, ts=NOW)
        self.assertFalse(self.db.hl_kill(), f"解除后立刻又熔断 = 人工解形同虚设：{alerts}")
        self.assertAlmostEqual(res["dd"], 0.0, places=6)

    def test_reset_baseline_button_keeps_kill(self):
        """高2b：重置基线**不**解除 kill（两条独立动作）。"""
        self.set_state_dd(0.31)
        self.dd_round(1000.0)
        self.assertTrue(self.db.hl_kill())
        before = self.state()
        r = self._client().post("/admin/switch/dd_reset", data={"k": self.TOKEN})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.db.hl_kill(), "重置回撤基线不许顺手解除 kill")
        self.assertAlmostEqual(self.state()["peak_index"], before["index"], places=6)
        self.assertIn("回撤基线已重置", unquote(r.headers.get("location", "")))

    def test_unkill_manual_origin_does_not_touch_peak(self):
        """非 dd 来源的 kill 被解除时**不许**动回撤基线（那等于替用户绕过熔断）。"""
        self.set_state_dd(0.31)
        self.db.hl_set_kill(True, "人工停手", origin="manual")
        r = self._client().post("/admin/switch/kill", data={"on": "0", "k": self.TOKEN})
        self.assertEqual(r.status_code, 303)
        self.assertAlmostEqual(self.state()["peak_index"], 100.0, places=6)

    def test_reset_without_state_reports_instead_of_crashing(self):
        r = self._client().post("/admin/switch/dd_reset", data={"k": self.TOKEN})
        self.assertEqual(r.status_code, 303)
        self.assertIn("还没有回撤锚点", unquote(r.headers.get("location", "")))


class TestV2StopRepairUnderDdKill(Base):
    """高3：`origin='dd'` 不得阻断止损补挂（裸奔比停手更贵）。"""

    BAR = 1_700_000_000_000

    @staticmethod
    def _exchange_pos():
        return {"coin": "BTC", "side": 1, "size": 0.01, "entry_px": 83000.0, "mark_px": 83000.0,
                "position_value": 830.0, "liq_px": 0.0, "leverage": 5, "isolated": True,
                "margin_used": 166.0, "unrealized_pnl": 0.0, "roe": 0.0,
                "funding_since_open": 0.0}

    def _position(self):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=self.BAR, side=1,
                                      entry_price=83000.0, stop_price=80600.0, stop_dist=0.029,
                                      contracts=1, coin_qty=0.001, notional=83.0, risk_u=20.0,
                                      equity_at_entry=1000.0)
        return dict(self.db.position_by_id(pid))

    def test_dd_kill_still_repairs_missing_stop(self):
        row = self._position()
        self.db.hl_set_kill(True, "策略回撤 31.2%（≥ 熔断线 30%）", origin="dd")
        snap = _snap(1000.0, positions=[self._exchange_pos()], orders=[])
        ex = self.executor()
        ex._repair_missing_stop(row, snap, kill_before=True)
        writes = self.ex.writes()
        self.assertEqual(len(writes), 1, "回撤熔断期间必须照常补挂止损（否则裸奔仓永不自解）")
        kind, coin, reduce_only, trigger_px = writes[0]
        self.assertEqual(kind, "order")
        self.assertEqual(coin, "BTC")
        self.assertTrue(reduce_only, "补挂的必须是减仓单")
        self.assertAlmostEqual(trigger_px, 80600.0, delta=1.0)

    def test_manual_kill_still_skips_repair(self):
        """保持原行为：人工/对账 kill 期间不动交易所（避免与人的操作打架）。"""
        row = self._position()
        snap = _snap(1000.0, positions=[self._exchange_pos()], orders=[])
        for origin in ("manual", "reconcile"):
            with self.subTest(origin=origin):
                self.db.hl_set_kill(True, "停手", origin=origin)
                self.ex.calls.clear()
                self.executor()._repair_missing_stop(row, snap, kill_before=True)
                self.assertEqual(self.ex.writes(), [], f"{origin} kill 期间不许动交易所")


class TestV2SameRoundKill(Base):
    """中1：同轮内 kill 只允许「未触发 → 触发」单向；文案按最终状态写。"""

    def _prepare(self, dd_pct):
        self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        self.db.set_config("hl_kill_clean_rounds", "1")          # 本轮再来一次干净就凑够 2 轮
        self.db.set_config("hl_kill_clean_last_ts", f"{NOW - 3600.0:.3f}")
        self.seed_snaps([(NOW - 4000, 1000.0)])
        self.set_state_dd(dd_pct, ts=NOW - 1000)

    def test_dd_kill_blocks_autoclear_wording(self):
        self._prepare(0.31)                                      # DD 31% ≥ 熔断线 30%
        ex, alerts = self.run_round(1000.0, ts=NOW, gate=False)
        joined = "\n".join(alerts)
        self.assertTrue(self.db.hl_kill(), "对账刚收敛也仍要停手（两种停手都要停）")
        self.assertNotIn("已自动恢复自动交易", joined, f"同一轮不许出现矛盾文案：{joined}")
        self.assertIn("对账异常已收敛", joined)
        self.assertIn("仍处暂停", joined)

    def test_autoclear_wording_kept_when_dd_quiet(self):
        """对照：DD 没到线时，自解文案照旧（不然就把 v1 的行为改坏了）。"""
        self._prepare(0.05)
        ex, alerts = self.run_round(1000.0, ts=NOW, gate=False)
        self.assertFalse(self.db.hl_kill())
        self.assertTrue(any("已自动恢复自动交易" in a for a in alerts), alerts)


class TestV2SubAccountTransfer(Base):
    """中2：`subAccountTransfer` 是「钱真的离开/进入本账户」，不能当内部划转忽略。"""

    ME, OTHER = "0xabc", "0xdef"

    def test_out_transfer_is_outflow(self):
        f = hl_flows.classify(self.ledger_row("subAccountTransfer", NOW, user=self.ME,
                                              destination=self.OTHER, usdcValue="300"), self.ME)
        self.assertIsNotNone(f, "转出 300U 必须有流水，否则权益下降 = 假亏损")
        self.assertEqual(f["kind"], "sub_out")
        self.assertAlmostEqual(f["amount"], -300.0, places=6)
        self.assertEqual(f["counterparty"], self.OTHER)

    def test_in_transfer_is_inflow(self):
        f = hl_flows.classify(self.ledger_row("subAccountTransfer", NOW, user=self.OTHER,
                                              destination=self.ME, usdc="120"), self.ME)
        self.assertEqual(f["kind"], "sub_in")
        self.assertAlmostEqual(f["amount"], 120.0, places=6)

    def test_unrelated_and_self_transfer_ignored(self):
        self.assertIsNone(hl_flows.classify(self.ledger_row(
            "subAccountTransfer", NOW, user=self.OTHER, destination="0xzzz", usdc="5"), self.ME))
        self.assertIsNone(hl_flows.classify(self.ledger_row(
            "subAccountTransfer", NOW, user=self.ME, destination=self.ME, usdc="5"), self.ME))

    def test_three_internal_types_still_ignored(self):
        for typ in ("accountClassTransfer", "internalTransfer", "spotTransfer"):
            with self.subTest(typ=typ):
                self.assertIsNone(hl_flows.classify(
                    self.ledger_row(typ, NOW, usdc="1000", user=self.ME, destination=self.ME),
                    self.ME), f"{typ} 是内部划转，必须继续忽略")
        self.assertNotIn("subAccountTransfer", hl_flows.IGNORED_TYPES)

    def test_sub_transfer_does_not_move_drawdown(self):
        """端到端：转出 300 → 权益 700 → 回撤仍是 0（v1 会算成 30% 假熔断）。"""
        self.seed_snaps([(NOW - 4000, 1000.0)])
        hl_dd.save_state(self.db, {
            "anchor_ts": NOW - 4000, "anchor_eq": 1000.0, "anchor_cumflow": 0.0,
            "index": 100.0, "peak_index": 100.0, "last_ts": NOW - 3000, "last_eq": 1000.0,
            "twr_skipped": 0, "neg_streak": 0, "updated_at": NOW - 3000})
        self.add_flow(NOW - 2500, -300.0, kind="sub_out")
        res, alerts, _ = self.dd_round(700.0)
        self.assertAlmostEqual(res["index"], 100.0, places=6)
        self.assertAlmostEqual(res["dd"], 0.0, places=6)
        self.assertFalse(self.db.hl_kill(), alerts)


class TestV2IndexZero(Base):
    """低1：`index == 0`（权益亏光）是合法状态，不许被 `or` 兜底换成 100。"""

    def test_zero_survives_parse_and_drawdown_is_100pct(self):
        hl_dd.save_state(self.db, {
            "anchor_ts": NOW - 5000, "anchor_eq": 1000.0, "anchor_cumflow": 0.0,
            "index": 0.0, "peak_index": 100.0, "last_ts": NOW - 1000, "last_eq": 1000.0,
            "twr_skipped": 0, "neg_streak": 0, "updated_at": NOW})
        st = self.state()
        self.assertIsNotNone(st, "index=0 不许被 parse_state 判废")
        self.assertEqual(st["index"], 0.0)
        self.assertAlmostEqual(hl_dd.drawdown(st["index"], st["peak_index"]), 1.0, places=9)

    def test_make_state_does_not_replace_zero_or_zero_last_eq(self):
        st = hl_dd.make_state(anchor_ts=NOW - 5000, anchor_eq=1000.0, anchor_cumflow=0.0,
                              replay={"index": 0.0, "peak_index": 100.0, "last_ts": NOW,
                                      "last_eq": 0.0, "twr_skipped": 0}, now=NOW)
        self.assertEqual(st["index"], 0.0, "`replay.get(...) or BASE_INDEX` 必须改成 is None")
        self.assertEqual(st["peak_index"], 100.0)
        self.assertEqual(st["last_eq"], 0.0, "last_eq=0（没有本金）也是合法值")

    def test_judges_100pct_and_kills(self):
        self.set_state_dd(1.0)                                  # index = 0
        res, alerts, _ = self.dd_round(0.0)
        self.assertAlmostEqual(res["dd"], 1.0, places=9)
        self.assertTrue(self.db.hl_kill(), "亏光 = DD 100% → 必须熔断")
        self.assertIn("策略回撤 100.0%", self.texts(alerts)[0])

    def test_panel_renders_100pct(self):
        from app.hl.views import DashboardDeps, render_dashboard, status_payload
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 0.0)])
        self.set_state_dd(1.0)
        self.db.set_config("hl_flows_synced_at", str(int(NOW)))
        cfg = type("C", (), {"symbols": ["BTC_USDT"], "risk_pct": 0.015, "hl_dd_guard": True,
                             "hl_dd_warn_pct": 0.0, "hl_dd_kill_pct": 0.0})()
        deps = DashboardDeps(db=self.db, config_fn=lambda: cfg, info=self.info,
                             address_fn=lambda: "0xabc", mode_fn=lambda: "live",
                             now_fn=lambda: NOW)
        payload = status_payload(deps)
        self.assertAlmostEqual(payload["curve"]["meta"]["dd_pct"], 1.0, places=6)
        self.assertIn("策略回撤 100.00%", render_dashboard(payload))


class TestV2NegativeIndex(Base):
    """低2：负指数不再「不写库 → 永久卡死」，而是推进时间窗 + 连续 3 轮锚点重放兜底。"""

    def setUp(self):
        super().setUp()
        self.seed_snaps([(NOW - 4000, 1000.0)])
        hl_dd.save_state(self.db, {
            "anchor_ts": NOW - 4000, "anchor_eq": 1000.0, "anchor_cumflow": 0.0,
            "index": 100.0, "peak_index": 100.0, "last_ts": NOW - 2000, "last_eq": 1000.0,
            "twr_skipped": 0, "neg_streak": 0, "updated_at": NOW - 2000})
        # 本轮快照不入库：否则重放窗口里会带进这笔「矛盾权益」，那是另一条用例的事。
        self.p.snapshot_min_interval_s = 1e9

    def _neg_round(self, ts: float):
        """权益只有 100 却报了一笔 +500 入金 → (100−500)/1000 < 0（自相矛盾）。"""
        return self.run_round(100.0, ts=ts, gate=False,
                              ledger_rows=[self.ledger_row("deposit", ts - 50, usdc="500")])

    def test_negative_round_advances_last_ts(self):
        self._neg_round(NOW - 1900)
        st = self.state()
        self.assertEqual(st["last_ts"], NOW - 1900,
                         "负值轮必须推进时间窗（v1 不写 state → 窗口越拖越宽、永远不再判）")
        self.assertEqual(st["neg_streak"], 1)
        self.assertEqual(st["index"], 100.0, "负指数不许写进状态")
        self.assertFalse(self.db.hl_kill(), "负值轮不判线")

    def test_three_negative_rounds_trigger_anchor_replay(self):
        self._neg_round(NOW - 1900)
        self._neg_round(NOW - 1800)
        self.assertEqual(self.state()["neg_streak"], 2, "第 2 轮还不该兜底")
        ex, alerts = self._neg_round(NOW - 1700)
        joined = "\n".join(alerts)
        self.assertIn("口径卡死", joined)
        self.assertIn("人工核对", joined)
        st = self.state()
        self.assertEqual(st["neg_streak"], 0, "重放后计数归零")
        self.assertEqual(st["last_ts"], NOW - 4000,
                         "已用锚点重放重建（回到锚点/最新可用快照），不再卡在矛盾数据上")
        self.assertAlmostEqual(st["index"], 100.0, places=6)


class TestV2BootstrapLatest(Base):
    """低3：bootstrap 必须取**最近** N 张快照（旧 ASC LIMIT 取的是最老的 N 张）。"""

    def test_bootstrap_uses_latest_snapshots(self):
        n = 20005
        rows = [(NOW - n + i, 1000.0 + (i % 7) * 0.5) for i in range(n)]
        with self.db.conn:
            self.db.conn.executemany(
                "INSERT INTO hl_snapshots(ts, equity, created_at) VALUES(?,?,?)",
                [(t, e, NOW) for t, e in rows])
        newest = rows[-1][0]
        # 旧取法（ORDER BY ts ASC LIMIT 20000）停在倒数第 5 张 —— 必须能暴露这个差
        old = self.db.hl_snapshots(0.0, 20000)
        self.assertLess(float(old[-1]["ts"]), newest, "数据集必须能暴露旧取法的漏洞")
        ex = self.executor()
        st = ex._dd_bootstrap(self.db.hl_flows_since(0.0), NOW)
        self.assertIsNotNone(st)
        self.assertEqual(st["last_ts"], newest,
                         f"bootstrap 的 last_ts 必须是表内最新 ts（旧取法落后 "
                         f"{newest - float(old[-1]['ts']):.0f}s）")


class TestV2ThresholdSingleSource(Base):
    """低4：阈值范围规则只有一处真相（app/hl/dd.py），三处入口结论一致。"""

    CFG = type("C", (), {"risk_pct": 0.02, "hl_dd_warn_pct": 0.0, "hl_dd_kill_pct": 0.0})

    def test_three_entry_points_agree(self):
        from app.admin_ui import _parse_dd_thresholds
        from app.config import Config
        from app.main import _dd_pct
        cases = [("0.35", "0.45", True), ("0.02", "0.40", False), ("0.45", "0.40", False),
                 ("0.30", "0.30", False), ("0.97", "0.98", False), ("0.60", "0.70", True)]
        for warn, kill, ok in cases:
            with self.subTest(warn=warn, kill=kill):
                th = hl_dd.thresholds(0.02, float(warn), float(kill))
                self.assertEqual(not th["rejected"], ok, "dd.thresholds 是基准")
                vals, msg = _parse_dd_thresholds(warn, kill, self.CFG)
                self.assertEqual(bool(vals), ok, f"面板判据必须与 dd 一致：{msg}")
                c = Config()
                c.apply_db({"hl_dd_warn_pct": warn, "hl_dd_kill_pct": kill})
                self.assertEqual((c.hl_dd_warn_pct, c.hl_dd_kill_pct) != (0.0, 0.0), ok,
                                 "config.apply_db 必须与 dd 一致")
                for raw in (warn, kill):
                    self.assertEqual(
                        _dd_pct(type("X", (), {"v": raw})(), "v"),
                        hl_dd.norm_override(float(raw)), "main._dd_pct 必须用 dd.norm_override")

    def test_ranges_table_no_longer_duplicates_the_rule(self):
        from app.config import Config
        self.assertNotIn("hl_dd_warn_pct", Config._RANGES)
        self.assertNotIn("hl_dd_kill_pct", Config._RANGES)
        self.assertEqual((hl_dd.MIN_PCT, hl_dd.MAX_KILL), (0.05, 0.95))


if __name__ == "__main__":
    unittest.main(verbosity=2)
