"""DD Guard「对抗审查遗留低危项」的定点回归测试（第二轮小修）。

对应规格 `/opt/data/cache/scratch/cma-ddguard-spec-remaining.md`：

  1. 【低3】面板四态：DD 越线但 kill **实态为假** → `over_kill`，文案不许出现「已熔断」
  2. 【高3 附加路径】死手开关在 kill 态也要续期（到点撤全部挂单 = 连 reduce-only 止损一起撤）
  3. 【低6】`_dd_unavailable` 的事件节流：**推送失败**时不许每轮多一条 dd 事件
  4. 【低5】矛盾阈值：拒收（DB 保留原值）+ 面板上能看见「没生效 / 自相矛盾」
  5. dd kill 期间「有仓 + 缺止损」→ 交易所**收到**补挂写操作（回归 v2 高3，防回退）
  6. 【断言质量】当日入金 500 后亏 40 → 用**数值**钉住「亏损率按当日策略本金算」
  7. 【低7】`anchor_cumflow` 只写不读（留痕），判据不依赖它

为什么单开一个文件、不并入 `tests/test_dd_guard.py`：那一份是 PR#49 的验收套件
（按规格 §9 的八组组织，1300+ 项里的一大块）。本文件只放「第二轮审查遗留项」的
定点回归 —— 分开后「这一轮到底钉了什么」能一眼看全，将来回滚也只需看这一个文件。
全部离线：假 info / 假交易所 + 临时 sqlite，不触网。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.admin_ui import _dd_threshold_fields, _parse_dd_thresholds
from app.config import Config
from app.db import DB
from app.hl import MODE_LIVE
from app.hl import dd as hl_dd
from app.hl.executor import Executor, ExecParams, _today_start_ts
from app.hl.exchange import make_cloid

NOW = 1_760_000_000.0                    # 固定「现在」，避免用例只在某一天有效
DAY0 = _today_start_ts(NOW)              # 该时刻对应的北京时间零点（L1 基线日界）


# ---------------------------------------------------------------- 桩
def _snap(equity: float, *, ts: float = NOW, positions=(), orders=()) -> dict:
    """账户快照（统一账户下 `equity_total` 才是账户总值，口径同 HlInfo.snapshot）。"""
    return {"ts": ts, "address": "0xabc", "equity": 0.0, "equity_total": float(equity),
            "spot_usdc": float(equity), "spot_ok": True, "unified": True,
            "withdrawable": float(equity), "margin_used": 0.0, "notional": 0.0,
            "positions": list(positions), "open_orders": list(orders),
            "orders_trusted": True}


class FakeInfo:
    def __init__(self, snap=None):
        self.snap = snap or _snap(1000.0)
        self.ledger_rows = []
        self.raise_ledger = False

    def snapshot(self, address, mids=None):
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
        if self.raise_ledger:
            raise RuntimeError("ledger api down")
        return [dict(r) for r in self.ledger_rows]


class FakeExchange:
    """只记账的写通道：用来断言「交易所到底收到了什么」。"""

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
        self.calls.append(("order", coin, reduce_only, trigger_px, cloid))
        return {"ok": True, "status": "resting", "oid": 77, "avg_px": limit_px,
                "filled_sz": sz, "error": None, "raw": {}}

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        return {"ok": True, "status": "resting", "oid": oid, "error": None,
                "avg_px": None, "filled_sz": None, "raw": None}

    def schedule_cancel(self, time_ms=None, minutes=30):
        self.calls.append(("schedule_cancel", minutes))
        return {"ok": True, "error": ""}

    def writes(self):
        return [c for c in self.calls if c[0] in ("order", "cancel")]

    def deadman_calls(self):
        return [c for c in self.calls if c[0] == "schedule_cancel"]


class FakeNotifier:
    def __init__(self, ok=True):
        self.messages = []
        self.ok = ok

    def send_message(self, text):
        self.messages.append(text)
        return self.ok


# ---------------------------------------------------------------- 基类
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.info = FakeInfo()
        self.ex = FakeExchange()
        # risk_pct=0.015 = 1.5% 档 → 告警 25% / 熔断 30%
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.015, leverage=5.0, code_rev="testrev")

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self, notifier=None, **kw) -> Executor:
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex,
                        notifier=notifier if notifier is not None else FakeNotifier())

    def seed_snaps(self, points) -> None:
        for ts, eq in points:
            self.db.hl_add_snapshot({"ts": ts, "equity_total": eq})

    def add_flow(self, ts: float, amount: float, kind: str = "deposit") -> None:
        self.db.hl_add_flow({"ts": ts, "kind": kind, "amount": amount,
                             "hash": f"h{ts}-{amount}"})

    def set_state_dd(self, pct: float, *, peak: float = 100.0, ts: float = NOW) -> None:
        """直接摆一个「回撤恰好 pct」的状态（不依赖快照序列）。"""
        hl_dd.save_state(self.db, {
            "anchor_ts": NOW - 5000, "anchor_eq": 1000.0, "anchor_cumflow": 0.0,
            "index": peak * (1.0 - pct), "peak_index": peak,
            "last_ts": ts, "last_eq": 1000.0, "twr_skipped": 0, "updated_at": ts})

    def events(self, kind=None):
        return [dict(r) for r in self.db.hl_recent_events(50, kind=kind)]

    # ---- 面板 ----
    def payload(self, *, guard: bool = True):
        from app.hl.views import DashboardDeps, status_payload
        self.db.set_config("hl_flows_synced_at", str(int(NOW)))
        cfg = type("C", (), {"symbols": ["BTC_USDT"], "risk_pct": 0.015,
                             "hl_dd_guard": guard, "hl_dd_warn_pct": 0.0,
                             "hl_dd_kill_pct": 0.0})()
        deps = DashboardDeps(db=self.db, config_fn=lambda: cfg, info=self.info,
                             address_fn=lambda: "0xabc", mode_fn=lambda: "live",
                             now_fn=lambda: NOW)
        return status_payload(deps)


# ------------------------------------------------ 1/2. 面板四态（低3）
class TestPanelStateWords(Base):
    """【低3】「已熔断」只允许在 kill **实态为真** 时出现。"""

    def _meta(self):
        return self.payload()["curve"]["meta"]

    def test_over_kill_when_dd_over_line_but_kill_false(self):
        """DD=35%（> 熔断线 30%）但 kill 实态为假 → over_kill，且文案不许说「已熔断」。

        复现场景：人工解熔断后峰值还没重置（或 monitor 还没跑到下一轮）——
        旧判据 `pct >= kill or (killed and origin == "dd")` 会显示「已熔断」，
        用户以为还在停手，实际交易已经恢复、仓位开得出去。
        """
        from app.hl.views import render_dashboard
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 650.0)])
        self.set_state_dd(0.35)
        self.assertFalse(self.db.hl_kill(), "前置：kill 实态必须是假")
        payload = self.payload()
        meta = payload["curve"]["meta"]
        self.assertEqual(meta["dd_status"], "over_kill")
        self.assertIn("未停手", meta["dd_status_text"])
        self.assertNotIn("已熔断", meta["dd_status_text"])
        html = render_dashboard(payload)
        self.assertIn("已越熔断线（当前未停手）", html)
        self.assertNotIn("已熔断", html, "越线但没停手时全页不许出现「已熔断」")
        # 用 warn 色：它是「需要注意」，不是「已停手」
        self.assertIn("<span class='warn'>已越熔断线（当前未停手）</span>", html)

    def test_killed_when_kill_really_on_with_origin_dd(self):
        """DD=35% + kill 实态为真（origin=dd）→ killed，文案带「停手中」。"""
        from app.hl.views import render_dashboard
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 650.0)])
        self.set_state_dd(0.35)
        self.db.hl_set_kill(True, "策略回撤 35%", origin="dd")
        payload = self.payload()
        meta = payload["curve"]["meta"]
        self.assertEqual(meta["dd_status"], "killed")
        self.assertIn("已熔断", meta["dd_status_text"])
        self.assertIn("停手", meta["dd_status_text"])
        html = render_dashboard(payload)
        self.assertIn("已熔断（停手中）", html)
        self.assertIn("<span class='neg'>已熔断（停手中）</span>", html)
        self.assertNotIn("未停手", html)

    def test_dd_back_below_line_still_killed_by_dd_origin(self):
        """对照：DD 回到线下但 dd kill 还在（永不自解）→ 仍是 killed（v1 行为不许坏）。"""
        self.seed_snaps([(NOW - 86400, 1000.0), (NOW - 3600, 990.0)])
        self.set_state_dd(0.05)
        self.db.hl_set_kill(True, "策略回撤 35%", origin="dd")
        self.assertEqual(self._meta()["dd_status"], "killed")


# ------------------------------------------------ 死手开关（高3 附加路径）
class TestDeadmanUnderKill(Base):
    """kill 态也必须续期 scheduleCancel：不续期 = 到点撤掉全部挂单（含 reduce-only 止损）。"""

    def test_kill_state_still_renews_deadman(self):
        self.db.hl_set_kill(True, "策略回撤 35%", origin="dd")
        ex = self.executor(use_schedule_cancel=True)
        ex.run(force_snapshot=True)
        self.assertIn(("schedule_cancel", 30), self.ex.calls,
                      "kill 期间不续期 → 定时器到期会连止损一起撤掉（裸奔）")

    def test_renewal_does_not_touch_orders(self):
        """续期只是把「撤单」往后推：它本身**不动**任何挂单/仓位。"""
        self.db.hl_set_kill(True, "策略回撤 35%", origin="dd")
        ex = self.executor(use_schedule_cancel=True)
        ex.run(force_snapshot=True)
        self.assertEqual([c for c in self.ex.calls if c[0] == "cancel"], [],
                         "scheduleCancel 不该触发撤单")
        self.assertEqual(len(self.ex.deadman_calls()), 1)

    def test_switch_off_never_calls_exchange(self):
        """默认关闭（线上现状）：kill 与否都不许碰这个接口。"""
        self.db.hl_set_kill(True, "策略回撤 35%", origin="dd")
        self.executor().run(force_snapshot=True)
        self.assertEqual(self.ex.deadman_calls(), [])


# ------------------------------------------------ 3. _dd_unavailable 事件节流（低6）
class TestUnavailableEventThrottle(Base):
    """推送失败时，`hl_events` 里 kind='dd' 的事件不许每轮都多一条。"""

    def test_push_failure_does_not_duplicate_event(self):
        ex = self.executor(notifier=FakeNotifier(ok=False))
        for i in range(3):                       # 连续 3 轮推送都失败
            alerts = []
            ex._dd_unavailable(alerts, "读取出入金流水失败", NOW + i * 300)
            self.assertEqual(len(alerts), 1,
                             "推送失败必须下轮重推（节流键只在成功后回写）——用户最终要看到")
        evs = [e for e in self.events("dd") if "回撤口径暂不可用" in e["message"]]
        self.assertEqual(len(evs), 1, f"推送失败时事件每轮重复（实际 {len(evs)} 条）")

    def test_next_event_after_window(self):
        """24h 窗口过去后允许再记一条（不是永久静音）。"""
        ex = self.executor(notifier=FakeNotifier(ok=False))
        ex._dd_unavailable([], "读取出入金流水失败", NOW)
        ex._dd_unavailable([], "读取出入金流水失败", NOW + hl_dd.UNAVAIL_THROTTLE_S + 1)
        evs = [e for e in self.events("dd") if "回撤口径暂不可用" in e["message"]]
        self.assertEqual(len(evs), 2)

    def test_push_success_throttles_both(self):
        """推送成功：事件与告警都进 24h 窗口（各自最多一条）。"""
        ex = self.executor(notifier=FakeNotifier(ok=True))
        round1 = []
        ex._dd_unavailable(round1, "读取出入金流水失败", NOW)
        self.assertEqual(len(round1), 1)
        self.db.set_config(hl_dd.UNAVAIL_KEY, str(int(NOW)))    # 模拟推送成功后的回写
        round2 = []
        ex._dd_unavailable(round2, "读取出入金流水失败", NOW + 300)
        self.assertEqual(round2, [], "窗口内不再推")
        evs = [e for e in self.events("dd") if "回撤口径暂不可用" in e["message"]]
        self.assertEqual(len(evs), 1)


# ------------------------------------------------ 4. 矛盾阈值（低5）
class TestContradictoryThresholdsVisible(Base):
    """矛盾阈值拒收后，用户必须在**面板上**看见「你填的值没生效」。"""

    def _cfg(self, warn: float = 0.30, kill: float = 0.40) -> Config:
        cfg = Config()
        cfg.risk_pct = 0.02                  # 2% 档：告警 30% / 熔断 40%
        cfg.hl_dd_warn_pct = warn
        cfg.hl_dd_kill_pct = kill
        return cfg

    def test_parse_rejects_with_explicit_message(self):
        cfg = self._cfg()
        out, msg = _parse_dd_thresholds("0.45", "0.35", cfg)
        self.assertEqual(out, {}, "自相矛盾 → 两条都不写库（保留原值，语义不改）")
        self.assertIn("没生效", msg)
        self.assertIn("自相矛盾", msg)

    def test_apply_db_keeps_old_values(self):
        """装配层同样拒收：内存里保留原值（不是把矛盾值吃进去）。"""
        cfg = self._cfg()
        cfg.apply_db({"hl_dd_warn_pct": "0.45", "hl_dd_kill_pct": "0.35"})
        self.assertAlmostEqual(cfg.hl_dd_warn_pct, 0.30, places=6)
        self.assertAlmostEqual(cfg.hl_dd_kill_pct, 0.40, places=6)

    def test_field_shows_live_values_and_reject_rule(self):
        """输入框下方常驻小字：当前生效值 + 「自相矛盾会被拒收、不生效」。"""
        html = _dd_threshold_fields(self._cfg(), {})
        self.assertIn("当前生效：告警 30% / 熔断 40%", html)
        self.assertIn("自相矛盾", html)
        self.assertIn("不生效", html)

    def test_field_warns_when_db_pair_is_contradictory(self):
        """DB 里真存着矛盾值时：明说「你填的值没生效，已按档位默认线执行」。"""
        html = _dd_threshold_fields(self._cfg(warn=0.45, kill=0.35), {})
        self.assertIn("自相矛盾", html)
        self.assertIn("你填的值没生效", html)


# ------------------------------------------------ 5. dd kill 期间补挂止损（回归 v2 高3）
class TestStopRepairUnderDdKill(Base):
    """dd kill 期间「有仓 + 缺止损」→ 交易所必须收到补挂写操作（防回退）。"""

    BAR = 1_700_000_000_000

    @staticmethod
    def _exchange_pos():
        return {"coin": "BTC", "side": 1, "size": 0.01, "entry_px": 83000.0,
                "mark_px": 83000.0, "position_value": 830.0, "liq_px": 0.0,
                "leverage": 5, "isolated": True, "margin_used": 166.0,
                "unrealized_pnl": 0.0, "roe": 0.0, "funding_since_open": 0.0}

    def _row(self):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=self.BAR, side=1,
                                      entry_price=83000.0, stop_price=80600.0, stop_dist=0.029,
                                      contracts=1, coin_qty=0.001, notional=83.0, risk_u=20.0,
                                      equity_at_entry=1000.0)
        return dict(self.db.position_by_id(pid))

    def test_dd_kill_still_rehangs_stop(self):
        row = self._row()
        self.db.hl_set_kill(True, "策略回撤 35%（≥ 熔断线 30%）", origin="dd")
        snap = _snap(1000.0, positions=[self._exchange_pos()], orders=[])
        with mock.patch("app.hl.executor.time.time", return_value=NOW):
            self.executor()._repair_missing_stop(row, snap, kill_before=True)
        writes = self.ex.writes()
        self.assertEqual(len(writes), 1, "回撤熔断期间必须照常补挂止损（否则裸奔仓永不自解）")
        kind, coin, reduce_only, trigger_px, cloid = writes[0]
        self.assertEqual(kind, "order")
        self.assertEqual(coin, "BTC")
        self.assertTrue(reduce_only, "补挂的必须是减仓单")
        self.assertAlmostEqual(trigger_px, 80600.0, delta=1.0)
        # 补挂用独立 cloid（purpose='stop:fix'），与原止损单的 cloid 不撞号 ——
        # HL 的 cloid 账户级唯一，撞号会被直接拒单，那就等于没补。
        self.assertEqual(cloid, make_cloid("BTC_USDT", self.BAR, f"stop:fix@{int(NOW)}"))
        self.assertNotEqual(cloid, make_cloid("BTC_USDT", self.BAR, f"stop@{int(NOW)}"))


# ------------------------------------------------ 6. 入金后的亏损率口径（断言质量）
class TestDailyLossRateNumeric(Base):
    """当日入金 500 后亏 40：亏损率必须按**当日策略本金**算，用数值钉住。

    口径（app/hl/executor.py 3a 步）：
        loss_rate = ((now_eq − start_eq) − flow_today) / start_eq
    旧口径（v1）分母是**含入金的本金**，同一个场景只算出 −L/(start+500)：
        本金 1000 + 入金 500 − 亏 40 → 新 −4.0% / 旧 −2.7%
        本金  870 + 入金 500 − 亏 40 → 新 −4.6% / 旧 −2.9%
    本用例把阈值临时收到 3.5%（仅测试参数，线上仍是 5%），让「算出来的那个数字」被
    写进告警文案 → 直接断言数值，而不是只断言「有没有触发」。
    """

    def _round(self, equity: float, *, start_eq: float, max_daily: float):
        self.seed_snaps([(DAY0 + 10, start_eq)])
        self.add_flow(DAY0 + 20, 500.0)
        self.info.snap = _snap(equity)
        self.db.set_config("hl_flows_synced_at", "0")
        with mock.patch("app.hl.executor._today_start_ts", return_value=DAY0):
            ex = self.executor(max_daily_loss_pct=max_daily)
            alerts = ex.run()
        return ex, alerts

    def test_numeric_rate_1000_capital(self):
        """本金 1000：新口径 −4.0%（旧口径会算成 −2.7% → 漏报）。"""
        ex, alerts = self._round(1460.0, start_eq=1000.0, max_daily=0.035)
        joined = " ".join(alerts)
        self.assertIn("当日亏损 -4.0%", joined,
                      f"必须按策略本金 1000 算（−40/1000）；旧口径分母 1500 → −2.7%：{joined}")
        self.assertNotIn("-2.7%", joined)
        self.assertEqual(str(self.db.get_config_str("hl_exec_kill_origin")), "daily_loss")

    def test_numeric_rate_870_capital(self):
        """本金 870：新口径 −4.6%（旧口径 −2.9%）。规格 §6 提到的两个数字在这里对齐。"""
        ex, alerts = self._round(1330.0, start_eq=870.0, max_daily=0.04)
        joined = " ".join(alerts)
        self.assertIn("当日亏损 -4.6%", joined, f"−40/870 = −4.6%：{joined}")
        self.assertNotIn("-2.9%", joined)

    def test_same_scenario_under_default_5pct_does_not_kill(self):
        """同一个场景在**线上 5%** 线下不触发（−4.0% 还没到线）。"""
        ex, alerts = self._round(1460.0, start_eq=1000.0, max_daily=0.05)
        self.assertFalse(self.db.hl_kill())
        self.assertFalse(any("当日亏损" in a for a in alerts), alerts)


# ------------------------------------------------ 7. anchor_cumflow 死字段（低7）
class TestAnchorCumflowIsTraceOnly(Base):
    """`anchor_cumflow` 只写不读：字段保留（留痕），判据不依赖它。"""

    def test_state_keeps_field_for_trace(self):
        st = hl_dd.make_state(anchor_ts=NOW - 100, anchor_eq=1000.0, anchor_cumflow=901.8,
                              replay={"index": 100.0, "peak_index": 100.0,
                                      "last_ts": NOW - 100, "last_eq": 1000.0,
                                      "twr_skipped": 0}, now=NOW)
        self.assertAlmostEqual(st["anchor_cumflow"], 901.8, places=6,
                               msg="字段必须保留（§4.2 留痕），不许因为是死字段就删")

    def test_step_ignores_anchor_cumflow(self):
        """把 anchor_cumflow 改成离谱的值，推进结果完全不变（证明判据不读它）。"""
        base = {"anchor_ts": NOW - 1000, "anchor_eq": 1000.0, "index": 100.0,
                "peak_index": 100.0, "last_ts": NOW - 1000, "last_eq": 1000.0,
                "twr_skipped": 0, "neg_streak": 0, "updated_at": NOW - 1000}
        a = hl_dd.step(dict(base, anchor_cumflow=0.0), ts_now=NOW, equity_now=900.0,
                       flow_k=0.0)
        b = hl_dd.step(dict(base, anchor_cumflow=1e9), ts_now=NOW, equity_now=900.0,
                       flow_k=0.0)
        self.assertAlmostEqual(a["index"], b["index"], places=9)
        self.assertAlmostEqual(a["peak_index"], b["peak_index"], places=9)
        self.assertAlmostEqual(a["index"], 90.0, places=6)


if __name__ == "__main__":
    import os
    os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "t_ddrem.db"))
    unittest.main()
