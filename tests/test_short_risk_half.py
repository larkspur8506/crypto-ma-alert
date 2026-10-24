"""空单风险减半开关（short_risk_half）的回归测试。

功能一句话：开关打开时**做空腿每一笔**的下单风险按 0.5 倍计算（风险U / 名义 /
张数全链路减半），多单与止损位完全不变；默认关闭，关闭时全链路行为与改动前
逐字节一致（两棵代码树的签名 diff 另行人工核对，见 PR 说明）。

覆盖（对应规格「测试要求」逐条）：
1. 配置：默认 0 / env 覆盖 / DB 覆盖优先于 env / 脏值（"abc"、"2"、空串）保留原值
2. helper 真值表：`effective_risk_pct` 的 side × 开关 2×2
3. 引擎路径（`size_order`，走 `engine.walk` 真调用点）：空单减半、多单不变、
   关闭时与改动前的 golden 值一致
4. 执行器路径（`size_order_hl` + `Executor.on_entry` 真路径）：sz / notional /
   risk / margin 减半，stop_dist 不变
5. 止损价与止损距离不因开关改变
6. 提醒文案：开空卡显示**生效后**百分比，多单卡不变，关闭时与现状一致
7. 低于最小张数：小权益 + 宽止损，减半后明确跳过（落 skipped + 事件说明）
8. 面板：开关页渲染、POST 写库 + 事件记「旧→新」、缺字段保持原值、"0" 必须是关
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, strategy as S                                    # noqa: E402
from app.config import Config                                            # noqa: E402
from app.db import DB                                                    # noqa: E402
from app.hl import MODE_DRY, MODE_LIVE                                   # noqa: E402
from app.hl import sizing as hl_sizing                                   # noqa: E402
from app.sizing import SHORT_HALF_SCALE, effective_risk_pct, size_order  # noqa: E402
from tests.test_strategy import (breakout_bar, flat_bar,                # noqa: E402
                                 squeeze_then_breakdown, trend_then_flat)


def cfg(**kw) -> Config:
    """固定口径的配置（与 tests/test_engine.py 同一套）。"""
    c = Config()
    c.equity, c.risk_pct, c.leverage = 1000.0, 0.02, 5.0
    c.stop_min, c.stop_max = 0.015, 0.10
    c.enable_long, c.enable_shorts = True, True
    c.token_secret = "test-secret"
    c.base_url = "https://ma.example.com"
    c.alert_prefix = "【双均线】"
    c.short_risk_half = False
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _short_setup():
    """做空信号夹具：趋势后横盘密集 → 向下跌破 band 下沿 → 次根（入场参考）。"""
    bars, i = squeeze_then_breakdown()
    ind = S.compute_indicators(bars)
    return bars, ind, S.squeeze_flags(ind["band"]), i


def _long_setup():
    base = trend_then_flat()
    bo = breakout_bar(base, 0.04)
    bars = base + [bo, flat_bar(bo, bo.close)]
    ind = S.compute_indicators(bars)
    return bars, ind, S.squeeze_flags(ind["band"]), len(bars) - 2


def _walk(side: str, half: bool):
    """跑一遍 engine.walk 的**真调用点**，返回 (event, cfg)。"""
    bars, ind, sq, i = _short_setup() if side == "short" else _long_setup()
    c = cfg(short_risk_half=half)
    events, _pos = engine.walk(c, "BTC_USDT", bars, ind, sq, None, i, i, i)
    assert events and events[0].kind == "entry", events
    return events[0], c


# ================================================================ 8. helper
class TestEffectiveRiskPct(unittest.TestCase):
    """唯一生效点的 2×2 真值表 —— 两条 sizing 路径都只认这一个判据。"""

    def test_scale_constant_is_half(self):
        # 倍率写死在一处，且就是 0.5（不是可调参数、不进 _RANGES）
        self.assertEqual(SHORT_HALF_SCALE, 0.5)
        self.assertNotIn("short_risk_half", Config._RANGES)
        self.assertNotIn("short_risk_half_scale", Config._RANGES)

    def test_truth_table(self):
        rp = 0.02
        self.assertEqual(effective_risk_pct(rp, 1, False), 0.02)     # 多单·关
        self.assertEqual(effective_risk_pct(rp, 1, True), 0.02)      # 多单·开 → 不变
        self.assertEqual(effective_risk_pct(rp, -1, False), 0.02)    # 空单·关 → 不变
        self.assertEqual(effective_risk_pct(rp, -1, True), 0.01)     # 空单·开 → 减半

    def test_off_returns_input_untouched(self):
        """关闭时必须原值返回（不 float() 归一），否则「逐字节一致」无从谈起。"""
        sentinel = 0.02
        self.assertIs(effective_risk_pct(sentinel, -1, False), sentinel)
        self.assertIs(effective_risk_pct(sentinel, 1, True), sentinel)

    def test_other_sides_never_scaled(self):
        for side in (0, 2, -2):
            self.assertEqual(effective_risk_pct(0.03, side, True), 0.03)


# ================================================================ 1. 配置
class TestConfig(unittest.TestCase):
    """DB > env > 默认；脏值保留原值（fail 方向 = 维持现状，绝不静默翻转）。"""

    def setUp(self):
        self._env = os.environ.get("SHORT_RISK_HALF")
        os.environ.pop("SHORT_RISK_HALF", None)
        self._cfgm = importlib.import_module("app.config")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("SHORT_RISK_HALF", None)
        else:
            os.environ["SHORT_RISK_HALF"] = self._env
        importlib.reload(self._cfgm)          # 还原类体（= 模块导入时读 env）的默认值

    def _fresh(self):
        """重新导入 app.config 让类体重新读一次 env（Config 的默认值在类体里求值）。"""
        return importlib.reload(self._cfgm).Config

    def test_default_is_off(self):
        self.assertIs(self._fresh()().short_risk_half, False)

    def test_env_can_enable(self):
        for raw in ("1", "true", "yes", "on", "TRUE"):
            os.environ["SHORT_RISK_HALF"] = raw
            self.assertIs(self._fresh()().short_risk_half, True, raw)

    def test_env_dirty_and_zero_are_off(self):
        for raw in ("0", "false", "no", "off", "abc", ""):
            os.environ["SHORT_RISK_HALF"] = raw
            self.assertIs(self._fresh()().short_risk_half, False, raw)

    def test_in_overridable(self):
        self.assertIn("short_risk_half", Config.OVERRIDABLE)

    def test_db_overrides_env(self):
        os.environ["SHORT_RISK_HALF"] = "1"
        c = self._fresh()()
        self.assertTrue(c.short_risk_half)
        # DB 里显式 0（set_config 走 json.dumps → 值可能是 '"0"' 也可能是 0）→ 关
        c.apply_db({"short_risk_half": '"0"'})
        self.assertFalse(c.short_risk_half, "DB 必须优先于 env")
        c.apply_db({"short_risk_half": 1})
        self.assertTrue(c.short_risk_half)

    def test_db_on_when_env_off(self):
        c = Config()
        c.apply_db({"short_risk_half": '"1"'})
        self.assertTrue(c.short_risk_half)

    def test_dirty_db_values_keep_previous(self):
        # 从「关」出发：脏值不许把它翻成开
        for raw in ("abc", "2", "-1", "tru"):
            c = Config()
            c.apply_db({"short_risk_half": raw})
            self.assertFalse(c.short_risk_half, raw)
        # 从「开」出发：脏值同样不许把它翻成关（fail 方向 = 维持现状）
        for raw in ("abc", "2", ""):
            c = Config()
            c.short_risk_half = True
            c.apply_db({"short_risk_half": raw})
            self.assertTrue(c.short_risk_half, raw)

    def test_missing_key_keeps_previous(self):
        c = Config()
        c.short_risk_half = True
        c.apply_db({"equity": 500})
        self.assertTrue(c.short_risk_half)

    def test_db_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(str(Path(tmp) / "t.db"))
            db.set_config("short_risk_half", 1)
            c = Config().apply_db(db.get_config())
            self.assertTrue(c.short_risk_half)
            db.set_config("short_risk_half", 0)
            self.assertFalse(Config().apply_db(db.get_config()).short_risk_half)


# ================================================================ 3/5. 引擎路径
class TestEngineSizingPath(unittest.TestCase):
    """`engine.walk → size_order` 是真调用点：开关必须在这里就生效。"""

    def test_short_halves_risk_notional_and_contracts(self):
        off, _ = _walk("short", False)
        on, _ = _walk("short", True)
        self.assertEqual(off.sig.side, -1)
        # 关闭时的 golden（改动前实测值，钉子防止「顺手改了默认口径」）
        self.assertEqual(off.order.contracts, 34475)
        self.assertAlmostEqual(off.order.notional, 428.0071, places=4)
        self.assertAlmostEqual(off.order.risk_u, 19.9998, places=4)
        # 打开后：张数 floor 到一半，风险/名义跟着减半（≤ 1 张的取整误差）
        self.assertEqual(on.order.contracts, 17237)
        self.assertAlmostEqual(on.order.risk_u / off.order.risk_u, 0.5, places=3)
        self.assertAlmostEqual(on.order.notional / off.order.notional, 0.5, places=3)
        self.assertLessEqual(on.order.contracts * 2, off.order.contracts)

    def test_long_untouched(self):
        off, _ = _walk("long", False)
        on, _ = _walk("long", True)
        self.assertEqual(off.sig.side, 1)
        self.assertEqual(off.order.contracts, 36918)
        self.assertEqual(on.order.contracts, off.order.contracts)
        self.assertAlmostEqual(on.order.notional, off.order.notional, places=9)
        self.assertAlmostEqual(on.order.risk_u, off.order.risk_u, places=9)
        self.assertAlmostEqual(on.order.margin, off.order.margin, places=9)

    def test_stop_price_and_distance_unchanged(self):
        """止损价/止损距离是信号属性，开关一律不许动它们。"""
        off, _ = _walk("short", False)
        on, _ = _walk("short", True)
        self.assertEqual(on.sig.entry_price, off.sig.entry_price)
        self.assertEqual(on.sig.stop_price, off.sig.stop_price)
        self.assertEqual(on.sig.stop_dist, off.sig.stop_dist)
        self.assertEqual(on.order.stop_price, off.order.stop_price)
        self.assertEqual(on.order.stop_dist, off.order.stop_dist)   # 逐位相等，不是近似
        self.assertEqual(on.order.entry_price, off.order.entry_price)

    def test_off_matches_pre_change_golden(self):
        """开关关闭 == 改动前：固定输入固定输出（golden 值来自 origin/main 实测）。"""
        ev, c = _walk("short", False)
        o = ev.order
        self.assertEqual((o.symbol, o.contracts), ("BTC_USDT", 34475))
        self.assertAlmostEqual(o.coin_qty, 3.4475, places=9)
        self.assertAlmostEqual(o.notional, 428.0071, places=4)
        self.assertAlmostEqual(o.margin, 85.6014, places=4)
        self.assertAlmostEqual(o.risk_u, 19.9998, places=4)
        self.assertAlmostEqual(o.stop_dist, 0.04672775, places=8)
        self.assertFalse(o.below_minimum)
        # 文案也与改动前一致（风险行逐字相同）
        self.assertIn("风险：20.0U（权益 1,000U × 2%）", engine.format_entry(c, ev.sig, o, 1))


# ================================================================ 6. 文案
class TestAlertCopy(unittest.TestCase):
    def test_short_card_shows_effective_pct(self):
        ev, c = _walk("short", True)
        text = engine.format_entry(c, ev.sig, ev.order, 1)
        self.assertIn("风险：10.0U（权益 1,000U × 1%）", text)
        self.assertNotIn("× 2%", text)

    def test_short_card_off_keeps_original_text(self):
        ev, c = _walk("short", False)
        text = engine.format_entry(c, ev.sig, ev.order, 1)
        self.assertIn("风险：20.0U（权益 1,000U × 2%）", text)

    def test_long_card_never_changes(self):
        ev_off, c_off = _walk("long", False)
        ev_on, c_on = _walk("long", True)
        a = engine.format_entry(c_off, ev_off.sig, ev_off.order, 1)
        b = engine.format_entry(c_on, ev_on.sig, ev_on.order, 1)
        self.assertEqual(a, b)
        self.assertIn("× 2%", b)

    def test_executor_managed_card_shows_effective_pct(self):
        ev, c = _walk("short", True)
        text = engine.format_entry(c, ev.sig, ev.order, 1, executor_managed=True)
        self.assertIn("风险：单笔 1.00% 权益", text)
        ev, c = _walk("long", True)
        text = engine.format_entry(c, ev.sig, ev.order, 1, executor_managed=True)
        self.assertIn("风险：单笔 2.00% 权益", text)


# ================================================================ 7. 低于最小张数
class TestBelowMinimum(unittest.TestCase):
    """减半后名义可能跌破交易所下限 → 必须明确跳过，绝不下 0 张或畸形单。"""

    def test_mexc_degrades_to_minimum_and_says_so(self):
        # 权益 50U + 9% 宽止损：满额恰好 1 张（不低于最小张数），减半后 0 张 → 退化 + 明示
        entry, stop, eq = 83_000.0, 90_470.0, 50.0
        full = size_order("BTC_USDT", entry, stop, eq, effective_risk_pct(0.02, -1, False), 5.0, side=-1)
        half = size_order("BTC_USDT", entry, stop, eq, effective_risk_pct(0.02, -1, True), 5.0, side=-1)
        self.assertFalse(full.below_minimum)
        self.assertTrue(half.below_minimum, "减半后应低于最小张数")
        self.assertEqual(half.contracts, full.contracts)      # 退化成最小可下单量，不是 0 张

    def test_mexc_card_flags_the_degradation_with_effective_pct(self):
        sig = S.Signal(symbol="BTC_USDT", bar_index=0, bar_open_ms=0, entry_price=83_000.0,
                       stop_price=90_470.0, stop_dist=(90_470.0 - 83_000.0) / 83_000.0,
                       ma_max=90_470.0, ma_min=80_000.0, band=1.0, side=-1)
        c = cfg(equity=50.0, short_risk_half=True)
        order = size_order("BTC_USDT", sig.entry_price, sig.stop_price, c.equity,
                           effective_risk_pct(c.risk_pct, sig.side, True), c.leverage,
                           side=sig.side)
        text = engine.format_entry(c, sig, order, 1)
        self.assertIn("已退化到最小张数", text)
        self.assertIn("× 1%", text)                     # 目标风险口径也是生效后的 1%
        self.assertIn("0.7U", text)                     # 1 张的实际风险只有 0.75U


# ================================================================ 4. 执行器路径
class TestExecutorSizingPath(unittest.TestCase):
    """`size_order_hl` 层：sz / notional / risk / margin 减半，stop_dist 不变。"""

    ENTRY, STOP_SHORT, STOP_LONG, EQ = 83_000.0, 85_400.0, 80_600.0, 1000.0

    def _plan(self, side, half):
        return hl_sizing.size_order_hl("BTC", 5, self.ENTRY,
                                       self.STOP_SHORT if side == -1 else self.STOP_LONG,
                                       self.EQ, effective_risk_pct(0.02, side, half),
                                       5.0, side=side)

    def test_short_halves_every_size_field(self):
        off, on = self._plan(-1, False), self._plan(-1, True)
        self.assertAlmostEqual(off.sz, 0.00833, places=6)
        self.assertAlmostEqual(on.sz, 0.00416, places=6)
        self.assertEqual(on.sz, 0.00416)                    # floor 到 szDecimals 后的实际量
        self.assertLessEqual(on.sz, off.sz / 2)             # 只可能少下，绝不多下
        for f in ("notional", "risk_u", "margin"):
            self.assertAlmostEqual(getattr(on, f) / getattr(off, f), 0.5, places=2, msg=f)
            self.assertLessEqual(getattr(on, f), getattr(off, f) / 2 + 1e-9, f)

    def test_short_geometry_unchanged(self):
        off, on = self._plan(-1, False), self._plan(-1, True)
        self.assertEqual(on.stop_dist, off.stop_dist)
        self.assertEqual(on.stop_price, off.stop_price)
        self.assertEqual(on.entry_price, off.entry_price)
        self.assertEqual(on.max_safe_leverage, off.max_safe_leverage)
        self.assertEqual(on.liq_price, off.liq_price)     # 同一个入场价/杠杆 → 同一强平价

    def test_long_untouched(self):
        off, on = self._plan(1, False), self._plan(1, True)
        self.assertEqual(on.sz, off.sz)
        self.assertEqual(on.notional, off.notional)
        self.assertEqual(on.risk_u, off.risk_u)


class _ExecutorHarness(unittest.TestCase):
    """Executor 真路径的公共夹具：复用 tests/test_hl_executor.py 的假 info / 假交易所。

    直接继承那边的 Base 会连 live 模式分支一起继承（Base.mode = MODE_LIVE），
    dry / live 分组各自需要不同默认模式，所以这里用「实例化 + 手动 setUp」的方式
    拿一个独立 harness，并在本用例结束时 teardown（临时库不留垃圾）。
    """

    def _harness(self):
        from tests.test_hl_executor import Base
        inst = Base("run")
        inst.setUp()
        self.addCleanup(inst.tearDown)
        return inst


class TestExecutorDryPath(_ExecutorHarness):
    """dry 真路径：开关打开后 planned 行的 sz 与影子卡风险都减半。"""

    def _run(self, side, half):
        inst = self._harness()
        pid = inst.add_signal(side=side, entry=83_000.0,
                              stop=85_400.0 if side == -1 else 80_600.0)
        ex = inst.executor(mode=MODE_DRY, short_risk_half=half)
        ex.on_entry(pid)
        return inst

    def test_dry_plan_and_shadow_card_full(self):
        inst = self._run(-1, False)
        self.assertAlmostEqual(inst.orders("entry")[0]["sz"], 0.00833, places=6)
        self.assertTrue(any("风险 20.0U" in m for m in inst.notifier.messages),
                        inst.notifier.messages)

    def test_dry_plan_and_shadow_card_halved(self):
        inst = self._run(-1, True)
        self.assertAlmostEqual(inst.orders("entry")[0]["sz"], 0.00416, places=6)
        self.assertTrue(any("风险 10.0U" in m for m in inst.notifier.messages),
                        inst.notifier.messages)

    def test_dry_long_plan_untouched_by_switch(self):
        inst = self._run(1, True)
        self.assertAlmostEqual(inst.orders("entry")[0]["sz"], 0.00833, places=6)


class TestExecutorBelowMinimum(_ExecutorHarness):
    """减半后名义跌破 10U → 明确跳过（skipped + 事件/推送说明），绝不下单。"""

    def _run(self, half):
        inst = self._harness()
        pid = inst.add_signal(side=-1, entry=83_000.0, stop=90_470.0)
        ex = inst.executor(mode=MODE_LIVE, short_risk_half=half, equity=50.0,
                           equity_mode="fixed")
        ex.on_entry(pid)
        return inst

    def test_half_below_min_is_skipped_not_ordered(self):
        inst = self._run(True)
        row = inst.orders("entry")[0]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual([c for c in inst.ex.calls if c[0] == "order"], [])
        msgs = [e["message"] for e in inst.events()]
        self.assertTrue(any("低于交易所下限" in m for m in msgs), msgs)
        self.assertTrue(any("低于交易所下限" in m for m in inst.notifier.messages),
                        inst.notifier.messages)

    def test_same_signal_orders_normally_without_switch(self):
        """同一笔信号在开关关闭时名义够 10U → 正常下单（证明跳过是减半带来的）。"""
        inst = self._run(False)
        self.assertNotEqual(inst.orders("entry")[0]["status"], "skipped")


# ================================================================ 确认页风险体检
class TestReviewFillTarget(unittest.TestCase):
    """确认开仓后的「风险体检」目标值也必须按生效后口径算。

    否则一笔本来就该只有一半风险的空单会被判成「偏离目标 -50%」，还会建议
    「要正好 20U 就开双倍张数」—— 那正是这个开关要防的事。
    """

    def test_short_target_uses_effective_pct(self):
        c = cfg(short_risk_half=True)
        # 42 张 × 0.0001 × 2400U = 10.08U ≈ 1000U×1%
        rev = engine.review_fill(c, "BTC_USDT", 83_000.0, 42, 83_000.0, 85_400.0, 42,
                                 "contracts", side=-1)
        self.assertAlmostEqual(rev["target_risk_u"], 10.0, places=6)
        self.assertEqual(rev["notes"], [], "半额空单不该被判成偏离目标")

    def test_long_target_unchanged(self):
        c = cfg(short_risk_half=True)
        rev = engine.review_fill(c, "BTC_USDT", 83_000.0, 84, 83_000.0, 80_600.0, 84,
                                 "contracts", side=1)
        self.assertAlmostEqual(rev["target_risk_u"], 20.0, places=6)
        self.assertEqual(rev["notes"], [])

    def test_switch_off_matches_old_target(self):
        c = cfg(short_risk_half=False)
        rev = engine.review_fill(c, "BTC_USDT", 83_000.0, 84, 83_000.0, 85_400.0, 84,
                                 "contracts", side=-1)
        self.assertAlmostEqual(rev["target_risk_u"], 20.0, places=6)

    def test_no_side_keeps_pre_change_behaviour(self):
        """不传 side（老脚本/测试）⇒ 按多单口径 ⇒ 与改动前完全一致。"""
        c = cfg(short_risk_half=True)
        rev = engine.review_fill(c, "BTC_USDT", 83_000.0, 42, 83_000.0, 85_400.0, 42,
                                 "contracts")
        self.assertAlmostEqual(rev["target_risk_u"], 20.0, places=6)


# ================================================================ 2/面板
try:
    from tests.test_admin_ui import TOKEN as PANEL_TOKEN
    from tests.test_admin_ui import BaseCase as _PanelBase
except ImportError:                       # pragma: no cover - fastapi 未装
    _PanelBase = None


@unittest.skipIf(_PanelBase is None, "fastapi 未安装")
class TestPanelSwitch(_PanelBase):
    """开关页：渲染 + 独立 POST（写库 / 事件旧→新 / 缺字段不变 / '0' 是关）。"""

    def test_switch_rendered_next_to_short_toggle(self):
        html = self.body(tab="switch")
        self.assertIn("空单风险减半", html)
        self.assertIn("name='short_risk_half'", html)
        self.assertIn("<input type='hidden' name='short_risk_half' value='0'>", html)
        self.assertIn("0.5 倍", html)                    # 白话说明里写清倍率
        self.assertIn("多单不变", html)
        self.assertIn("只影响开仓", html)
        # 与「允许做空」相邻（同一张卡、紧跟其后），而不是跑到参数页
        self.assertLess(html.index("允许做空"), html.index("空单风险减半"))
        self.assertLess(html.index("空单风险减半"), html.index("到期前自动清仓"))
        # 默认关：不出现 checked
        self.assertNotRegex(html, r"name='short_risk_half' value='1' checked")

    def test_post_on_writes_db_and_event_with_old_new(self):
        self.assertFalse(self._cfg().short_risk_half)
        r = self.post("/admin/switch/strategy", short_risk_half="1")
        self.assertEqual(r.status_code, 303)
        self.assertIn("tab=switch", r.headers["location"])
        self.assertIn(f"k={PANEL_TOKEN}", r.headers["location"])
        self.assertEqual(str(self.db.get_config_str("short_risk_half")), "1")
        self.assertTrue(self._cfg().short_risk_half)
        msgs = [e["message"] for e in self.db.hl_recent_events(5, kind="decision")]
        self.assertTrue(any("空单风险减半=关→开" in m for m in msgs), msgs)
        # 页面回显：checkbox 变 checked
        html = self.body(tab="switch")
        self.assertRegex(html, r"name='short_risk_half' value='1' checked")
        # 再关一次：事件记「开→关」
        r = self.post("/admin/switch/strategy", short_risk_half="0")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("short_risk_half")), "0")
        self.assertFalse(self._cfg().short_risk_half, "'0' 必须是关")
        msgs = [e["message"] for e in self.db.hl_recent_events(5, kind="decision")]
        self.assertTrue(any("空单风险减半=开→关" in m for m in msgs), msgs)

    def test_partial_post_keeps_value(self):
        self.db.set_config("short_risk_half", 1)
        r = self.post("/admin/switch/strategy", enable_shorts="1")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("short_risk_half")), "1",
                         "没提交这个键时必须保持原值")

    def test_bare_checkbox_multivalue_reads_last(self):
        """浏览器语义：hidden 的 0 先发、勾选的 1 后发 → 取最后一个 = 开。"""
        r = self.client.post(
            "/admin/switch/strategy",
            content=f"k={PANEL_TOKEN}&short_risk_half=0&short_risk_half=1",
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self._cfg().short_risk_half)
        self.assertTrue(any("空单风险减半=关→开" in e["message"]
                            for e in self.db.hl_recent_events(5, kind="decision")))

    def test_params_page_cannot_toggle_it(self):
        """参数页批量表单不许碰这个键（否则「保存全部参数」会静默改风险政策）。"""
        self.db.set_config("short_risk_half", 1)
        form = self.params_form()
        form["short_risk_half"] = "0"
        r = self.post("/admin/params", **form)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(str(self.db.get_config_str("short_risk_half")), "1",
                         "参数 POST 不该改 short_risk_half")


if __name__ == "__main__":
    unittest.main(verbosity=2)
