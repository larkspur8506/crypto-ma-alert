"""PR-5 `/why` 只读诊断的单元测试（全部离线：假行情 + 临时库，不触网、不下单）。

被测的三件事：

1. **六步逐项**（`strategy.explain_entry`）：根数 / 密集 / 破线 / MA200 / 止损距离 / 结论，
   每一步各至少一例「被它拦下」，并断言 `blocked_by` 指向的就是那一步、`reason` 说的是那件事。
2. **闸门只读性**（`Executor.gate_state` / `gate_state_for`）：调用前后
   `_settle_until` / `_churn_hits` **逐字节不变**（值、条数、键都不许动），且不写事件、不告警。
3. **一致性**（本 PR 的核心风险）：诊断结论必须与 `evaluate_entry` 完全一致 ——
   逐条对账 `fired`/`side`，并且**用 AST 证明本仓没有第二份信号判定**（不是靠人工 review）。

为什么一致性要用结构断言而不是只对几个用例：诊断器最大的风险不是算错某个数，而是悄悄长出
第二套规则（今天一致、三个月后改了一处就不一致）。所以既做行为对账（随机/构造用例逐条比），
也做结构断言（`explain_entry` 必须调用 `evaluate_entry`，且不得自己比较 ma_max/ma_min/sma200）。
"""
from __future__ import annotations

import ast
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import strategy as S                                      # noqa: E402
from app import why as why_mod                                     # noqa: E402
from app.config import Config                                      # noqa: E402
from app.db import DB                                              # noqa: E402
from app.hl import MODE_LIVE                                       # noqa: E402
from app.hl.executor import (CHURN_MAX_DEFAULT, CHURN_WINDOW_S, GATE_CHURN, GATE_OK,  # noqa: E402
                             GATE_SETTLE, SETTLE_WINDOW_S, Executor, ExecParams)
from tests.test_hl_executor import FakeExchange, FakeInfo, FakeNotifier   # noqa: E402
from tests.test_strategy import breakout_bar, flat_then_trend, trend_then_flat  # noqa: E402

STEP = 14_400_000
T0 = 1_700_000_000_000


# ---------------------------------------------------------------- 夹具
def _bar(i: int, px: float, spread: float = 0.001) -> S.Bar:
    return S.Bar(open_ms=T0 + i * STEP, open=px, high=px * (1 + spread),
                 low=px * (1 - spread), close=px, volume=1.0)


def _flat(n: int, px: float = 100.0) -> list:
    return [_bar(i, px) for i in range(n)]


def _squeezed_breakout(jump: float = 0.04) -> tuple:
    """趋势后横盘（密集成立）+ 一根突破 bar → 天然做多信号。返回 (bars, ind, sq, i)。"""
    base = trend_then_flat()
    bars = base + [breakout_bar(base, jump)]
    ind = S.compute_indicators(bars)
    sq = S.squeeze_flags(ind["band"])
    return bars, ind, sq, len(bars) - 1


def _cx(bars, ind, sq, i, **kw):
    """explain_entry 的简写（阈值默认放到最宽，先隔离出想测的那一步）。"""
    args = dict(entry_price=bars[i].close * 1.01, stop_min=0.0, stop_max=1.0)
    args.update(kw)
    return S.explain_entry("BTC_USDT", bars, ind, sq, i, **args)


def _mixed_tape(cycles: int = 3, seed: int = 7) -> list:
    """混合行情：趋势段（均线发散）→ 横盘段（均线收敛、构成密集）→ 突破/跌破。

    每段长度都对得上真实预热需求（250 根分位窗口 + 120 根预热），所以第 3 个循环起
    才可能出信号 —— 与线上「新标的要跑一段时间才有第一个信号」的情形一致。
    """
    rnd = random.Random(seed)
    px = 100.0
    bars: list = []
    for c in range(cycles):
        for _ in range(70):
            px *= 1.0 + (0.004 if c % 2 == 0 else -0.004)
            bars.append(_bar(len(bars), px, spread=0.004))
        for _ in range(110):
            px *= 1 + rnd.uniform(-0.0004, 0.0004)
            bars.append(_bar(len(bars), px, spread=0.0005))
        px *= 1.0 + (0.012 if c % 2 == 0 else -0.012)
        bars.append(_bar(len(bars), px, spread=0.006))
        for _ in range(4):
            bars.append(_bar(len(bars), px, spread=0.002))
    return bars


class TestSixSteps(unittest.TestCase):
    """六步逐项：每一步都要能单独成为「拦截原因」。"""

    # ---- ① 根数（bars） ----
    def test_step1_bars_insufficient_blocks_before_anything(self):
        bars = _flat(120)                       # 远少于 MIN_BARS_REQUIRED
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        why = _cx(bars, ind, sq, len(bars) - 1)
        self.assertEqual(why["blocked_by"], "bars")
        self.assertFalse(why["fired"])
        self.assertIn("不足", why["reason"])
        self.assertEqual(why["steps"][0]["key"], "bars")
        self.assertFalse(why["steps"][0]["ok"])
        # 与 evaluate_entry 一致：根数不足时它也不可能给出信号
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, len(bars) - 1))

    def test_step1_bars_ok_reports_count(self):
        bars, ind, sq, i = _squeezed_breakout()
        why = _cx(bars, ind, sq, i)
        st = next(s for s in why["steps"] if s["key"] == "bars")
        self.assertTrue(st["ok"])
        self.assertIn(str(len(bars)), st["detail"])
        self.assertIn(str(S.MIN_BARS_REQUIRED), st["detail"])

    def test_step1_index_out_of_range(self):
        bars, ind, sq, _ = _squeezed_breakout()
        why = S.explain_entry("BTC_USDT", bars, ind, sq, 0)      # i=0 没有前一根
        self.assertEqual(why["blocked_by"], "bars")
        self.assertIn("越界", why["reason"])

    # ---- ② 密集（squeeze） ----
    def test_step2_squeeze_false_blocks(self):
        base = flat_then_trend()                                  # 末端均线仍在发散 → 不密集
        bars = base + [breakout_bar(base, 0.04)]
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        i = len(bars) - 1
        self.assertFalse(sq[i - 1], "夹具前提：这根不密集")
        why = _cx(bars, ind, sq, i)
        self.assertEqual(why["blocked_by"], "squeeze")
        self.assertIn("不密集", why["reason"])
        # 阈值与带宽都摊开给人看了（能自我核对「差多少」）
        self.assertIsNotNone(why["values"]["band_prev"])
        self.assertIsNotNone(why["values"]["band_thr"])

    def test_step2_squeeze_short_run_not_enough(self):
        """密集要连续 ≥ min_squeeze_bars 根：把门槛抬到超过实际连续根数 → 拦在密集这一步。"""
        bars, ind, sq, i = _squeezed_breakout()
        seq = [True if j % 2 == 0 else False for j in range(len(bars))]   # 人为打断连续性
        why = _cx(bars, ind, seq, i)
        self.assertEqual(why["blocked_by"], "squeeze")

    def test_step2_squeeze_ok_when_true(self):
        bars, ind, sq, i = _squeezed_breakout()
        why = _cx(bars, ind, sq, i)
        st = next(s for s in why["steps"] if s["key"] == "squeeze")
        self.assertTrue(st["ok"], f"夹具应密集：{st}")
        self.assertNotEqual(why["blocked_by"], "squeeze")

    # ---- ③ 破线（breakout） ----
    def test_step3_no_breakout_blocks(self):
        bars, ind, sq, i = _squeezed_breakout()
        # 把收盘压到上轨之下（仍密集）→ 破线这一步拦下
        close = bars[i].close
        mx = ind["ma_max"][i - 1]
        px = (mx + close) / 2 if close < mx else mx * 0.999
        bars2 = list(bars[:-1]) + [S.Bar(open_ms=bars[i].open_ms, open=px, high=px, low=px,
                                        close=px, volume=1.0)]
        ind2 = S.compute_indicators(bars2)
        sq2 = S.squeeze_flags(ind2["band"])
        why = _cx(bars2, ind2, sq2, i)
        self.assertEqual(why["blocked_by"], "breakout")
        self.assertIn("没破线", why["reason"])

    def test_step3_breakout_but_direction_switch_off(self):
        bars, ind, sq, i = _squeezed_breakout()
        why = _cx(bars, ind, sq, i, allow_long=False)
        self.assertEqual(why["blocked_by"], "breakout")
        self.assertIn("做多", why["reason"])
        self.assertIn("关闭", why["reason"])

    def test_step3_long_breakout_ok(self):
        bars, ind, sq, i = _squeezed_breakout()
        why = _cx(bars, ind, sq, i)
        st = next(s for s in why["steps"] if s["key"] == "breakout")
        self.assertTrue(st["ok"])
        self.assertIn("上轨", st["detail"])

    # ---- ④ MA200 趋势过滤（regime） ----
    def _short_setup(self, below_ma200: bool) -> tuple:
        """构造下破 + 密集的场景，再用 SMA200 的取值决定过滤成立与否。

        做法：在密集横盘段之后挂一根「下破」bar，然后把 ind 里的 sma200 改成
        「收盘上方/下方各一档」—— 判定仍然走 evaluate_entry（不是我们自己比大小）。
        """
        base = trend_then_flat()
        last = base[-1]
        px = last.close * 0.96                                  # 下破
        bars = base + [S.Bar(open_ms=last.open_ms + STEP, open=px, high=px, low=px,
                             close=px, volume=1.0)]
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        i = len(bars) - 1
        self.assertTrue(sq[i - 1], "夹具前提：密集")
        self.assertLess(px, ind["ma_min"][i - 1], "夹具前提：收盘跌破下轨")
        seq = list(ind["sma200"])
        # below_ma200=True → 收盘在 MA200 下方（过滤成立）；False → 收盘在 MA200 上方（被拦）
        seq[i] = px * 1.05 if below_ma200 else px * 0.95
        ind = dict(ind)
        ind["sma200"] = seq
        return bars, ind, sq, i

    def test_step4_regime_blocks_short_above_ma200(self):
        bars, ind, sq, i = self._short_setup(below_ma200=False)
        why = _cx(bars, ind, sq, i, allow_long=False, short_regime_ma=200)
        self.assertEqual(why["blocked_by"], "regime")
        self.assertIn("MA200", why["reason"])
        st = next(s for s in why["steps"] if s["key"] == "regime")
        self.assertFalse(st["ok"])

    def test_step4_regime_ok_short_below_ma200(self):
        bars, ind, sq, i = self._short_setup(below_ma200=True)
        why = _cx(bars, ind, sq, i, allow_long=False, short_regime_ma=200)
        self.assertNotEqual(why["blocked_by"], "regime")
        st = next(s for s in why["steps"] if s["key"] == "regime")
        self.assertTrue(st["ok"])
        self.assertIn("SMA200", st["detail"])

    def test_step4_regime_missing_ma_fails_closed(self):
        """SMA200 算不出来 → 不做空（fail-closed），原因必须说明是「均线缺失」。"""
        bars, ind, sq, i = self._short_setup(below_ma200=True)
        seq = list(ind["sma200"])
        seq[i] = None
        ind = dict(ind)
        ind["sma200"] = seq
        why = _cx(bars, ind, sq, i, allow_long=False, short_regime_ma=200)
        self.assertEqual(why["blocked_by"], "regime")
        self.assertIn("SMA200", why["reason"])
        self.assertIn("还没算出来", why["reason"])

    def test_step4_regime_off_means_no_filter(self):
        bars, ind, sq, i = self._short_setup(below_ma200=False)
        why = _cx(bars, ind, sq, i, allow_long=False, short_regime_ma=0)
        self.assertNotEqual(why["blocked_by"], "regime",
                            "short_regime_ma=0 = 不做趋势过滤，不得拦下")
        st = next(s for s in why["steps"] if s["key"] == "regime")
        self.assertTrue(st["ok"])
        self.assertIn("关闭", st["detail"])

    # ---- ⑤ 止损距离（stop） ----
    def test_step5_stop_too_wide_blocks(self):
        bars, ind, sq, i = _squeezed_breakout(jump=0.40)         # 远离均线 → 止损距离过宽
        why = _cx(bars, ind, sq, i, stop_min=0.015, stop_max=0.10)
        self.assertEqual(why["blocked_by"], "stop")
        self.assertIn("不在", why["reason"])
        self.assertGreater(why["values"]["stop_dist"], 0.10)
        # 与 evaluate_entry 对齐：同一入参它也不给信号
        self.assertIsNone(S.evaluate_entry("BTC_USDT", bars, ind, sq, i,
                                          entry_price=bars[i].close * 1.01,
                                          stop_min=0.015, stop_max=0.10))

    def test_step5_stop_too_narrow_blocks(self):
        bars, ind, sq, i = _squeezed_breakout()
        why = _cx(bars, ind, sq, i, stop_min=0.5, stop_max=0.9)  # 门槛抬到实际距离之上
        self.assertEqual(why["blocked_by"], "stop")
        self.assertLess(why["values"]["stop_dist"], 0.5)

    def test_step5_stop_ok_reports_distance_and_range(self):
        bars, ind, sq, i = _squeezed_breakout()
        why = _cx(bars, ind, sq, i, stop_min=0.015, stop_max=0.10)
        st = next(s for s in why["steps"] if s["key"] == "stop")
        self.assertTrue(st["ok"], st)
        self.assertIn("%", st["detail"])
        self.assertAlmostEqual(why["values"]["stop_ref"], float(ind["ma_min"][i]))
        self.assertAlmostEqual(why["values"]["stop_dist"],
                               abs(why["values"]["entry_px"] - why["values"]["stop_ref"])
                               / why["values"]["entry_px"])

    # ---- ⑥ 结论（decision） ----
    def test_step6_decision_fired_long(self):
        bars, ind, sq, i = _squeezed_breakout()
        why = _cx(bars, ind, sq, i, stop_min=0.015, stop_max=0.10)
        self.assertTrue(why["fired"])
        self.assertEqual(why["side"], 1)
        self.assertEqual(why["blocked_by"], "ok")
        self.assertIn("做多", why["reason"])
        self.assertEqual(why["steps"][-1]["key"], "decision")
        self.assertTrue(why["steps"][-1]["ok"])
        self.assertFalse(why["drift"])

    def test_step6_decision_fired_short(self):
        bars, ind, sq, i = self._short_setup(below_ma200=True)
        why = _cx(bars, ind, sq, i, allow_long=False, short_regime_ma=200,
                  stop_min=0.0, stop_max=1.0)
        self.assertTrue(why["fired"], why["reason"])
        self.assertEqual(why["side"], -1)
        self.assertIn("做空", why["reason"])

    def test_step6_decision_blocked_says_why(self):
        bars = _flat(400)
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        why = _cx(bars, ind, sq, len(bars) - 1)
        self.assertFalse(why["fired"])
        self.assertEqual(why["side"], 0)
        self.assertTrue(why["reason"])
        self.assertNotEqual(why["blocked_by"], "ok")
        self.assertTrue(why["steps"][-1]["key"] == "decision")
        self.assertFalse(why["steps"][-1]["ok"])

    # ---- 结构性：六步键齐全、顺序固定 ----
    def test_steps_always_six_in_order(self):
        bars, ind, sq, i = _squeezed_breakout()
        for why in (_cx(bars, ind, sq, i),
                    S.explain_entry("BTC_USDT", bars, ind, sq, 0),
                    _cx(bars, ind, sq, i, allow_long=False, allow_short=False)):
            keys = [s["key"] for s in why["steps"]]
            self.assertEqual(len(keys), 6, keys)
            self.assertEqual(tuple(keys), S.WHY_STEPS)
            for st in why["steps"]:
                self.assertIn("detail", st, "每步都要有可读说明（面板要用）")
                self.assertIsInstance(st["ok"], bool)


class TestConsistencyWithEvaluateEntry(unittest.TestCase):
    """一致性验证：`explain_entry` 不得改变、也不得另写一套信号判定。"""

    def test_fired_and_side_match_on_mixed_tape(self):
        """混合行情（趋势/横盘/突破循环）逐根对账：fired/side 必须与 evaluate_entry 完全一致。

        为什么不用纯随机游走：均匀随机游走的均线带几乎不压缩，`squeeze` 恒 False，
        样本里一个信号都没有 —— 那样的「一致」是空断言（两边都是 None）。
        这里用「趋势段 → 横盘段 → 突破」循环，既制造密集也制造破线，信号与非信号都有。
        """
        bars = _mixed_tape(cycles=3, seed=7)
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        checked = fired = 0
        for i in range(1, len(bars) - 1, 3):
            for allow_long, allow_short, sreg in ((True, True, 200), (True, False, 200),
                                                  (False, True, 200), (True, True, 0)):
                kw = dict(entry_price=bars[i + 1].open, stop_min=0.015, stop_max=0.10,
                          allow_long=allow_long, allow_short=allow_short,
                          short_regime_ma=sreg)
                ref = S.evaluate_entry("BTC_USDT", bars, ind, sq, i, **kw)
                why = S.explain_entry("BTC_USDT", bars, ind, sq, i, **kw)
                checked += 1
                self.assertEqual(why["fired"], ref is not None,
                                 f"第 {i} 根 fired 不一致（{kw}）")
                self.assertEqual(why["side"], (int(ref.side) if ref is not None else 0),
                                 f"第 {i} 根 side 不一致")
                self.assertFalse(why["drift"], f"第 {i} 根出现口径漂移：{why['reason']}")
                if ref is not None:
                    fired += 1
                    self.assertAlmostEqual(why["signal"]["stop_price"], ref.stop_price)
                    self.assertAlmostEqual(why["signal"]["entry_price"], ref.entry_price)
        self.assertGreater(checked, 500)
        self.assertGreater(fired, 0, "样本里必须真有信号，否则一致性断言是空的")

    def test_blocked_by_reason_identifies_first_failing_step(self):
        """拦截原因必须指向**第一个**不成立的步骤（不是随便挑一个失败的）。"""
        bars = _mixed_tape(cycles=3, seed=7)
        ind = S.compute_indicators(bars)
        sq = S.squeeze_flags(ind["band"])
        seen = set()
        for i in range(1, len(bars) - 1, 3):
            why = S.explain_entry("BTC_USDT", bars, ind, sq, i,
                                  entry_price=bars[i + 1].open, stop_min=0.015, stop_max=0.10)
            seen.add(why["blocked_by"])
            flags = [st["ok"] for st in why["steps"] if st["key"] != "decision"]
            if why["fired"]:
                self.assertTrue(all(flags), f"有信号时六步必须全过：{why['steps']}")
                self.assertEqual(why["blocked_by"], "ok")
            else:
                # 第一个 False 的 key 必须等于 blocked_by（decision 步不参与，它就是结论本身）
                first_fail = next((st["key"] for st in why["steps"] if not st["ok"]), None)
                self.assertEqual(first_fail, why["blocked_by"],
                                 f"第 {i} 根：blocked_by={why['blocked_by']} 但首个失败步是 {first_fail}")
        self.assertTrue(seen & {"squeeze", "breakout"}, f"样本应覆盖密集/破线两类拦截：{seen}")

    def test_does_not_mutate_inputs(self):
        """纯只读：不得改调用方的 ind/squeeze（evaluate_entry 会补算 sma 序列）。"""
        bars, ind, sq, i = _squeezed_breakout()
        ind_before = {k: list(v) for k, v in ind.items()}
        sq_before = list(sq)
        S.explain_entry("BTC_USDT", bars, ind, sq, i, entry_price=bars[i].close * 1.01,
                        allow_long=False, allow_short=True, short_regime_ma=150)
        self.assertEqual(list(sq), sq_before)
        self.assertEqual(set(ind), set(ind_before), "不得往调用方的 ind 里加键")
        for k, v in ind_before.items():
            self.assertEqual(list(ind[k]), v, f"ind[{k}] 被改写")

    def test_explain_entry_calls_evaluate_entry_ast(self):
        """**结构证明没重写**：源码里必须真的调用 evaluate_entry，且不得自己比均线。"""
        src = Path(S.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "explain_entry")
        calls = {n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn("evaluate_entry", calls,
                      "explain_entry 必须复用 evaluate_entry 出结论，不能另写一套判定")
        # 复用现成的分位/密集实现，而不是自己再写一遍阈值比较
        self.assertIn("quantile", calls)
        self.assertIn("dense_run", calls)

    def test_no_duplicate_signal_predicate_in_repo(self):
        """全仓只允许 strategy.evaluate_entry 一处做「收盘 vs 均线带」的入场判定。

        允许出现的比较：strategy.py 自己的 evaluate_entry / explain_entry（解释用），
        以及 exit_triggered（离场口径，与入场无关）。
        """
        app_dir = Path(S.__file__).parent
        offenders = []
        for path in sorted(app_dir.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "ma_max[" in text and path.name not in ("strategy.py",):
                offenders.append(str(path.relative_to(app_dir)))
        self.assertEqual(offenders, [], "均线带比较只应存在于 strategy.py（信号口径唯一来源）")

    def test_why_steps_constant_matches_doc(self):
        self.assertEqual(len(S.WHY_STEPS), 6)
        self.assertEqual(S.WHY_STEPS[0], "bars")
        self.assertEqual(S.WHY_STEPS[-1], "decision")


class GateBase(unittest.TestCase):
    """闸门状态用例的公共夹具（复用 test_hl_executor 的假件，不触网）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, code_rev="testrev")
        self.ex = Executor(self.db, self.p, FakeInfo(), FakeExchange(), notifier=FakeNotifier())
        self._reset()

    def tearDown(self):
        self._reset()
        self.tmp.cleanup()

    @staticmethod
    def _reset():
        Executor._settle_until.clear()
        Executor._churn_hits.clear()
        Executor._churn_alerted.clear()
        Executor._gate_ev_round.clear()


class TestGateStateReadOnly(GateBase):
    """闸门状态：读得到、读得准，**且读不出副作用**。"""

    def test_no_gate_is_ok(self):
        st = self.ex.gate_state("BTC", 1)
        self.assertEqual(st["verdict"], GATE_OK)
        self.assertEqual(st["settle_remaining_s"], 0.0)
        self.assertEqual(st["churn_count"], 0)
        self.assertFalse(st["churn_tripped"])
        self.assertEqual(st["churn_limit"], CHURN_MAX_DEFAULT)
        self.assertEqual(st["settle_window_s"], SETTLE_WINDOW_S)
        self.assertEqual(st["churn_window_s"], CHURN_WINDOW_S)

    def test_settle_reported(self):
        self.ex._settle_arm("BTC", 1)
        st = self.ex.gate_state("BTC", 1)
        self.assertEqual(st["verdict"], GATE_SETTLE)
        self.assertGreater(st["settle_remaining_s"], 0)
        self.assertLessEqual(st["settle_remaining_s"], SETTLE_WINDOW_S + 0.5)
        # 空侧不受影响（闸门按 coin-side 分开）
        self.assertEqual(self.ex.gate_state("BTC", -1)["verdict"], GATE_OK)

    def test_churn_tripped_reported(self):
        for _ in range(CHURN_MAX_DEFAULT):
            self.ex._churn_record("BTC", 1)
        st = self.ex.gate_state("BTC", 1)
        self.assertEqual(st["verdict"], GATE_CHURN)
        self.assertTrue(st["churn_tripped"])
        self.assertEqual(st["churn_count"], CHURN_MAX_DEFAULT)

    def test_churn_zero_limit_means_disabled(self):
        self.p.churn_max = 0
        for _ in range(CHURN_MAX_DEFAULT + 2):
            self.ex._churn_record("BTC", 1)
        st = self.ex.gate_state("BTC", 1)
        self.assertFalse(st["churn_tripped"], "阈值 0 = 关闭熔断，不得报熔断")
        self.assertEqual(st["churn_limit"], 0)
        self.assertEqual(st["verdict"], GATE_OK if st["settle_remaining_s"] <= 0 else GATE_SETTLE)

    def test_settle_read_is_pure(self):
        """调用后 `_settle_until` 逐字节不变（值不变 = 没被续期）。"""
        self.ex._settle_arm("BTC", 1)
        key = self.ex._gate_key("BTC", 1)
        before = dict(Executor._settle_until)
        self.assertIn(key, before)
        for _ in range(3):
            self.ex.gate_state("BTC", 1)
        self.assertEqual(dict(Executor._settle_until), before, "读闸门状态不得改写 deadline")
        self.assertEqual(Executor._settle_until[key], before[key])

    def test_churn_read_is_pure(self):
        """调用后 `_churn_hits` 逐字节不变（窗口内记录不得被删或加）。"""
        for _ in range(2):
            self.ex._churn_record("BTC", 1)
        key = self.ex._gate_key("BTC", 1)
        before = {k: list(v) for k, v in Executor._churn_hits.items()}
        for _ in range(3):
            self.ex.gate_state("BTC", 1)
        after = {k: list(v) for k, v in Executor._churn_hits.items()}
        self.assertEqual(after, before, "读闸门状态不得改写 churn 窗口内的记录")
        self.assertEqual(Executor._churn_hits[key], before[key])

    def test_read_does_not_write_events_or_alerts_or_kill(self):
        """诊断是只读：不落事件、不推送、不置 kill（否则面板刷新会自己造噪音）。"""
        self.ex._settle_arm("BTC", 1)
        for _ in range(CHURN_MAX_DEFAULT):
            self.ex._churn_record("BTC", 1)
        events_before = self.db.hl_recent_events(50)
        alerts_before = self.db.recent_alerts(limit=50)
        kill_before = self.db.hl_kill()
        notifier_before = len(self.ex.notifier.sent) if hasattr(self.ex.notifier, "sent") else 0
        for _ in range(3):
            st = self.ex.gate_state("BTC", 1)
            self.assertEqual(st["verdict"], GATE_CHURN)
        self.assertEqual(self.db.hl_recent_events(50), events_before)
        self.assertEqual(self.db.recent_alerts(limit=50), alerts_before)
        self.assertEqual(self.db.hl_kill(), kill_before)
        if hasattr(self.ex.notifier, "sent"):
            self.assertEqual(len(self.ex.notifier.sent), notifier_before)

    def test_expired_settle_reports_zero(self):
        self.ex._settle_arm("BTC", 1)
        key = self.ex._gate_key("BTC", 1)
        Executor._settle_until[key] = time.time() - 0.01          # 人为过期
        st = self.ex.gate_state("BTC", 1)
        self.assertEqual(st["settle_remaining_s"], 0.0)
        self.assertEqual(st["verdict"], GATE_OK)

    def test_gate_state_for_without_instance(self):
        """面板没装配执行器（mode=off）时也要能读：走类方法，不建实例。"""
        self.ex._settle_arm("BTC", 1)
        st = Executor.gate_state_for(self.db, "BTC", 1)
        self.assertEqual(st["verdict"], GATE_SETTLE)
        self.assertGreater(st["settle_remaining_s"], 0)
        # 作用域必须与生产执行器一致（= db.path），否则同一个库里两套键互不相干
        self.assertEqual(self.ex._gate_key("BTC", 1), (str(self.db.path), "BTC", 1))
        # 阈值可注入
        self.assertEqual(Executor.gate_state_for(self.db, "BTC", 1, churn_max=0)["churn_limit"], 0)

    def test_gate_state_for_is_pure(self):
        for _ in range(2):
            self.ex._churn_record("BTC", 1)
        before_s = dict(Executor._settle_until)
        before_c = {k: list(v) for k, v in Executor._churn_hits.items()}
        Executor.gate_state_for(self.db, "BTC", 1)
        self.assertEqual(dict(Executor._settle_until), before_s)
        self.assertEqual({k: list(v) for k, v in Executor._churn_hits.items()}, before_c)


class WhyRowsBase(unittest.TestCase):
    """`app.why.build_rows` 的夹具：临时库 + 固定配置 + 假行情（绝不触网）。"""

    def setUp(self):
        why_mod.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.cfg = Config()
        self.cfg.symbols = ["BTC_USDT"]
        GateBase._reset()

    def tearDown(self):
        why_mod.clear_cache()
        GateBase._reset()
        self.tmp.cleanup()

    @staticmethod
    def _feed(bars):
        class F:
            source = "hl-candle"

            def __init__(self, b):
                self.bars = b
        return F(bars)

    def rows(self, bars, symbols=None):
        return why_mod.build_rows(self.db, self.cfg, symbols=symbols,
                                  fetch=lambda sym, now=None: self._feed(bars))


class TestWhyRows(WhyRowsBase):
    """装配层：每标的一行、信号态/闸门齐全，且只在只读路径上工作。"""

    def test_row_has_all_fields(self):
        rows = self.rows(_squeezed_breakout()[0])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        for key in ("symbol", "coin", "price", "bars", "why", "position", "note",
                    "error", "gates", "gate_side", "gate"):
            self.assertIn(key, r)
        self.assertIsNone(r["error"])
        self.assertEqual(r["coin"], "BTC")
        self.assertIsNotNone(r["price"])
        self.assertEqual(set(r["gates"]), {"long", "short"})
        self.assertEqual(r["gate"]["verdict"], GATE_OK)

    def test_fired_row_carries_signal(self):
        bars, ind, sq, i = _squeezed_breakout()
        r = self.rows(bars)[0]
        # 用同一份夹具独立算一遍参照值：结论必须与 evaluate_entry 一致
        ref = S.evaluate_entry("BTC_USDT", bars, ind, sq, len(bars) - 2,
                               entry_price=bars[-1].open, stop_min=self.cfg.stop_min,
                               stop_max=self.cfg.stop_max,
                               allow_long=self.cfg.enable_long,
                               allow_short=self.cfg.enable_shorts,
                               short_regime_ma=self.cfg.short_regime_ma)
        self.assertEqual(r["why"]["fired"], ref is not None)
        self.assertEqual(r["gate_side"], 0 if ref is None else int(ref.side))
        self.assertFalse(r["why"]["drift"])

    def test_gate_side_follows_open_position(self):
        bars = _squeezed_breakout()[0]
        self.db.create_position(symbol="BTC_USDT", signal_bar_ms=bars[-5].open_ms,
                                entry_price=100.0, stop_price=98.0, stop_dist=0.02,
                                contracts=1, coin_qty=0.01, notional=100.0, risk_u=2.0,
                                status="open", side=-1)
        r = self.rows(bars)[0]
        self.assertEqual(r["gate_side"], -1, "持仓方向决定看哪一侧的闸门")
        self.assertEqual(r["position"]["status"], "open")
        self.assertIn("持仓中", r["note"])

    def test_pending_note_mentions_slot(self):
        bars = _squeezed_breakout()[0]
        self.db.create_position(symbol="BTC_USDT", signal_bar_ms=bars[-5].open_ms,
                                entry_price=100.0, stop_price=98.0, stop_dist=0.02,
                                contracts=1, coin_qty=0.01, notional=100.0, risk_u=2.0,
                                status="pending", side=1)
        r = self.rows(bars)[0]
        self.assertEqual(r["position"]["status"], "pending")
        self.assertIn("待确认", r["note"])
        self.assertEqual(r["gate_side"], 0, "pending 不是仓位，方向看信号")

    def test_feed_error_degrades_one_symbol_only(self):
        bars = _squeezed_breakout()[0]

        def fetch(sym, now=None):
            if sym == "ETH_USDT":
                raise RuntimeError("boom: network down")
            return self._feed(bars)
        rows = why_mod.build_rows(self.db, self.cfg, symbols=["BTC_USDT", "ETH_USDT"], fetch=fetch)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["error"])
        self.assertIn("boom", rows[1]["error"])
        self.assertIsNone(rows[1]["why"])
        self.assertEqual(rows[1]["gates"]["long"]["verdict"], GATE_OK)

    def test_no_position_no_signal_row(self):
        bars = flat_then_trend()                                  # 均线发散 → 不密集、无信号
        r = self.rows(bars)[0]
        self.assertIsNone(r["position"])
        self.assertFalse(r["why"]["fired"])
        self.assertEqual(r["why"]["blocked_by"], "squeeze")

    def test_reads_gate_state_without_writing(self):
        self.ex_arm = Executor(self.db, ExecParams(mode=MODE_LIVE, account_address="0x",
                                                   private_key="0x", equity=1000.0),
                               FakeInfo(), FakeExchange(), notifier=FakeNotifier())
        self.ex_arm._settle_arm("BTC", 1)
        before = dict(Executor._settle_until)
        r = self.rows(_squeezed_breakout()[0])[0]
        self.assertEqual(r["gates"]["long"]["verdict"], GATE_SETTLE)
        self.assertEqual(dict(Executor._settle_until), before, "面板读闸门不得改写状态")

    def test_text_format_mentions_steps_and_gates(self):
        txt = why_mod.format_text(self.rows(_squeezed_breakout()[0]))
        self.assertIn("信号", txt)
        self.assertIn("闸门", txt)
        self.assertIn("BTC", txt)

    def test_readonly_db_adapter_rejects_writes(self):
        """命令行用的 `_ro_db` 必须物理只读（任何写被 SQLite 拒绝），且适配器语义与 DB 一致。"""
        p = str(Path(self.tmp.name) / "ro.db")
        from app.db import DB as RealDB
        real = RealDB(p)
        real.set_config("stop_max", 0.05)
        real.conn.close()
        ro = why_mod._ro_db(p)
        try:
            self.assertEqual(ro.path, p, "path 必须与生产执行器同值（闸门作用域）")
            self.assertEqual(ro.get_config().get("stop_max"), "0.05")
            self.assertIsNone(ro.open_position("BTC_USDT"))
            with self.assertRaises(Exception):
                ro.conn.execute("INSERT INTO positions(symbol, signal_bar_ms, status) "
                                "VALUES('X', 1, 'open')")
        finally:
            ro.conn.close()

    def test_feed_cached_within_ttl(self):
        """K 线缓存：TTL 内重复调用不再打行情接口（面板连刷不该每次拉 5000 根）。"""
        calls = []

        def fetch(sym, now=None):
            calls.append(sym)
            return self._feed(_squeezed_breakout()[0])
        for _ in range(3):
            why_mod.build_rows(self.db, self.cfg, fetch=fetch)
        self.assertEqual(len(calls), 1, "TTL 内只应取一次行情")

    def test_param_change_invalidates_six_step_cache(self):
        """参数热改必须让六步缓存失效，否则面板会按旧门槛解释新配置。"""
        why_mod.build_rows(self.db, self.cfg, fetch=lambda s, now=None: self._feed(_squeezed_breakout()[0]))
        before = dict(why_mod._WHY_CACHE)
        self.assertTrue(before)
        self.cfg.stop_max = 0.05                     # 改阈值 → 指纹变
        why_mod.build_rows(self.db, self.cfg, fetch=lambda s, now=None: self._feed(_squeezed_breakout()[0]))
        keys = list(why_mod._WHY_CACHE)
        self.assertTrue(any(k[2] != list(before)[0][2] for k in keys), "阈值指纹变化应产生新缓存键")

    def test_position_not_stale_from_six_step_cache(self):
        """仓位变化必须立刻反映（六步缓存命中时也要现读仓位）——否则会讲错的「为什么」。"""
        bars = _squeezed_breakout()[0]
        first = self.rows(bars)[0]
        self.assertIsNone(first["position"])
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=bars[-5].open_ms,
                                     entry_price=100.0, stop_price=98.0, stop_dist=0.02,
                                     contracts=1, coin_qty=0.01, notional=100.0, risk_u=2.0,
                                     status="open", side=1)
        self.assertTrue(pid)
        second = self.rows(bars)[0]
        self.assertIsNotNone(second["position"], "缓存命中路径必须现读仓位")
        self.assertEqual(second["position"]["status"], "open")
        self.assertEqual(second["gate_side"], 1)


class TestPanels(WhyRowsBase):
    """面板渲染：/why 的数据现在长在「三标的动态」卡片里（原「为什么没开仓」表格已合并掉）。"""

    @classmethod
    def setUpClass(cls):
        try:
            import fastapi  # noqa: F401
        except ImportError:                                       # pragma: no cover
            raise unittest.SkipTest("fastapi 未安装")

    def _deps(self, rows=None):
        from app.hl.views import DashboardDeps
        return DashboardDeps(db=self.db, config_fn=lambda: self.cfg, info=None,
                             address_fn=lambda: "", mode_fn=lambda: "off",
                             now_fn=lambda: 1_760_000_000.0)

    def test_merged_block_renders_each_symbol(self):
        """每标的一张卡，且只有一个区块标题（旧的两块不能同时出现）。"""
        from app.hl.views import _symbol_dynamics
        bars = _squeezed_breakout()[0]
        rows = why_mod.build_rows(self.db, self.cfg, symbols=["BTC_USDT", "ETH_USDT"],
                                  fetch=lambda sym, now=None: self._feed(bars))
        html = _symbol_dynamics({"why": rows})
        self.assertIn("三标的动态", html)
        self.assertNotIn("为什么没开仓", html)
        self.assertIn('class="nm">BTC', html)
        self.assertIn('class="nm">ETH', html)
        self.assertEqual(html.count('class="sym"'), 2)

    def test_block_reason_comes_from_strategy_and_ok_gate_is_silent(self):
        """不密集：原因行用人话讲拦截原因（词面与状态词统一为「未密集」）；
        闸门全正常 → 连「放行」字样都不出现。"""
        from app.hl.views import _symbol_dynamics
        html = _symbol_dynamics({"why": self.rows(flat_then_trend())})
        self.assertIn("均线未密集", html)
        self.assertNotIn("均线不密集", html)
        self.assertNotIn("闸门放行", html)
        self.assertNotIn("静置剩余", html)

    def test_fired_signal_reason_has_entry_and_stop(self):
        from app.hl.views import _symbol_dynamics
        row = {"symbol": "BTC_USDT", "coin": "BTC", "price": 100.0, "sma20": 96.0,
               "why": {"fired": True, "side": 1, "blocked_by": "ok", "reason": "信号成立：做多",
                       "signal": {"side": 1, "entry_price": 100.0, "stop_price": 96.0,
                                  "stop_dist": 0.04},
                       "values": {}},
               "position": None, "error": None, "gates": {}, "gate_side": 1, "gate": None}
        html = _symbol_dynamics({"why": [row]})
        self.assertIn("信号成立（多）", html)
        self.assertIn("信号成立：入场 100.00、止损 96.00（4.00%）", html)

    def test_json_endpoint_returns_rows(self):
        """`/api/hl/why`：每标的一行的 JSON（运维 curl 用；与会话面板同一份数据）。"""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.hl.views import build_router
        app = FastAPI()
        app.include_router(build_router(self._deps()))
        rows = [{"symbol": "BTC_USDT", "coin": "BTC",
                 "why": {"fired": False, "blocked_by": "squeeze", "reason": "不密集"},
                 "gates": {}, "gate": {}}]
        orig = why_mod.build_rows
        why_mod.build_rows = lambda *a, **kw: rows
        try:
            with TestClient(app) as c:
                r = c.get("/api/hl/why")
                self.assertEqual(r.status_code, 200)
                j = r.json()
                self.assertIn("ts", j)
                self.assertEqual(j["symbols"][0]["symbol"], "BTC_USDT")
                self.assertEqual(j["symbols"][0]["why"]["blocked_by"], "squeeze")
        finally:
            why_mod.build_rows = orig

    def test_merged_block_empty_returns_empty(self):
        from app.hl.views import _symbol_dynamics
        self.assertEqual(_symbol_dynamics({}), "")
        self.assertEqual(_symbol_dynamics({"why": [], "symbol_state": []}), "")

    def test_merged_block_fail_soft_on_dirty_rows(self):
        """脏行（缺字段/None/非 dict）不得抛异常 —— 诊断不能弄挂总览。"""
        from app.hl.views import _symbol_dynamics
        html = _symbol_dynamics({
            "why": [{}, {"symbol": None}, {"error": "boom", "coin": "ETH"},
                    {"coin": "BTC", "why": {"blocked_by": "regime", "reason": "MA200 过滤拦下"},
                     "gates": {"long": {}}, "gate": {}}],
            "symbol_state": [{"symbol": "SOL_USDT", "coin": "SOL", "price": 150.0,
                              "dense_cnt": None, "sma20": None, "sma200": None,
                              "long_trigger": None, "short_trigger": None,
                              "updated_at": None, "position_side": None}],
        })
        self.assertIn("三标的动态", html)
        self.assertIn("行情读取失败", html)      # error 行退了缓存/或标红，但整块照常
        self.assertIn("SOL", html)               # symbol_state 补上的标的也在
        self.assertIn("MA200 过滤拦下", html)

    def test_render_body_has_single_dynamics_section(self):
        """总览只留一个区块：旧的「为什么没开仓」标题与表格结构都不该再出现。"""
        from app.hl.views import _render_body
        payload = {"mode": "off", "ts": 1_760_000_000.0, "position": [], "symbol_state": [],
                   "why": [{"symbol": "BTC_USDT", "coin": "BTC", "price": 100.0,
                            "why": {"fired": False, "blocked_by": "breakout",
                                    "reason": "收盘价没破线：100.00 落在上轨 105.00 / 下轨 95.00 之间",
                                    "steps": []},
                            "gates": {}, "gate": {}}]}
        html = _render_body(payload)
        self.assertIn("<h2>三标的动态", html)
        self.assertNotIn("为什么没开仓", html)
        self.assertNotIn("闸门（动作前置）", html)
        self.assertIn("收盘价没破线", html)

    def test_admin_overview_renders_why_rows(self):
        """合并面板总览：注入的 why_rows_fn 必须被真正渲染（不是只传给 payload）。"""
        from app.admin_ui import AdminDeps, _overview_body
        rows = [{"symbol": "BTC_USDT", "coin": "BTC", "price": 100.0,
                 "why": {"fired": False, "blocked_by": "stop",
                         "reason": "止损距离 12.00% 不在 1.5%~10.0% 之间",
                         "steps": [], "drift": False},
                 "gates": {"long": {"verdict": "settle", "settle_remaining_s": 12.0,
                                    "churn_count": 0, "churn_limit": 5,
                                    "churn_tripped": False},
                           "short": {"verdict": "ok", "settle_remaining_s": 0.0,
                                     "churn_count": 0, "churn_limit": 5,
                                     "churn_tripped": False}},
                 "gate_side": 0, "gate": {"verdict": "settle", "settle_remaining_s": 12.0,
                                          "churn_count": 0, "churn_limit": 5,
                                          "churn_tripped": False}}]
        deps = AdminDeps(db=self.db, config_fn=lambda: self.cfg, hl_deps=self._deps(),
                         mode_fn=lambda: "off", token_fn=lambda: "t",
                         why_rows_fn=lambda: rows)
        html = _overview_body(deps, {"mode": "off", "ts": 1_760_000_000.0})
        self.assertIn("三标的动态", html)
        self.assertNotIn("为什么没开仓", html)
        self.assertIn("止损距离 12.00% 不在 1.5%~10.0% 之间", html)
        self.assertIn("⚠️ 静置剩余 12s", html)

    def test_admin_overview_survives_why_source_failure(self):
        """数据源抛异常 → 诊断行整体略去（有 symbol_state 就只少了原因行），页面照常渲染。"""
        from app.admin_ui import AdminDeps, _overview_body

        def boom():
            raise RuntimeError("行情接口挂了")
        deps = AdminDeps(db=self.db, config_fn=lambda: self.cfg, hl_deps=self._deps(),
                         mode_fn=lambda: "off", token_fn=lambda: "t", why_rows_fn=boom)
        html = _overview_body(deps, {"mode": "off", "ts": 1_760_000_000.0})
        self.assertNotIn("三标的动态", html)        # 没有任何标的读数 → 整块略去
        self.assertIn("账户", html, "总览其余部分照常渲染")

    def test_churn_line_says_how_it_trips(self):
        from app.hl.views import _symbol_dynamics
        html = _symbol_dynamics({"why": [{"symbol": "BTC_USDT", "coin": "BTC", "price": 100.0,
                                          "why": {"fired": False, "blocked_by": "squeeze",
                                                  "reason": "不密集"},
                                          "gates": {}, "gate_side": 1,
                                          "gate": {"verdict": "churn", "settle_remaining_s": 0,
                                                   "churn_count": 5, "churn_limit": 5,
                                                   "churn_tripped": True}}]})
        self.assertIn("⚠️ churn 5/5", html)
        self.assertIn("10 分钟内 5 次撤挂即熔断", html)


if __name__ == "__main__":
    unittest.main()
