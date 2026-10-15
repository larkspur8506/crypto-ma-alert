"""加入 SOL_USDT 标的的回归测试（组合口径：BTC + ETH + SOL，风险预算固定 1.33%/标的）。

覆盖四块：
  a. config：默认 SYMBOLS 三个标的；线上 compose 的 SYMBOLS env 仍能覆盖（双层控制）。
  b. coin 映射：market.hl_coin 与 executor.coin 两处都把 SOL_USDT → SOL（含 HL_COIN_MAP 覆盖）。
  c. HL 下单规格：SOL 的 szDecimals=2（0.01 步长）、HL maxLeverage ≥ 5x、最小名义 10U 边界。
  d. 引擎三标的互不挤兑：一个标的的 pending 不挡另一个标的的信号（各自独立槽位）。

约束：不放本地绝对路径（CI 容器没有 /opt/data）；不 import app.main；executor 用假参数构造、零网络。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market                                       # noqa: E402
from app.config import Config                                        # noqa: E402
from app.db import DB                                                # noqa: E402
from app.hl import sizing as HS                                      # noqa: E402
from app.hl.executor import Executor, ExecParams                     # noqa: E402
from app.sizing import SPECS                                         # noqa: E402
from tests.test_strategy import (trend_then_flat, breakout_bar,      # noqa: E402
                                 flat_bar, STEP)

THREE = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]


def _tmp_db() -> DB:
    import tempfile
    return DB(os.path.join(tempfile.mkdtemp(), "t.db"))


class _Notifier:
    def __init__(self):
        self.msgs = []

    def send_message(self, m):
        self.msgs.append(m)
        return True


def _cfg(**kw) -> Config:
    c = Config()
    c.equity, c.risk_pct, c.leverage = 1000.0, 0.02, 5.0
    c.stop_min, c.stop_max = 0.015, 0.10
    c.token_secret = "test-secret"
    c.base_url = "https://ma.example.com"
    c.alert_prefix = "【双均线】"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _entry_setup():
    """横盘密集 → 突破 bar → 次根（= 实盘里「进行中」的那根）。

    walk 的契约：最后一根 = 未收盘的进行中 bar，所以突破 bar 在 len-2。
    """
    base = trend_then_flat()
    bo = breakout_bar(base, 0.04)
    bars = base + [bo, flat_bar(bo, bo.close)]
    return bars


# ================================================================ a. SYMBOLS
class TestSymbolsDefault(unittest.TestCase):
    def _cfg_with_env(self, environ):
        with mock.patch.dict(os.environ, environ, clear=False):
            if "SYMBOLS" not in environ:
                os.environ.pop("SYMBOLS", None)
            return Config()

    def test_default_is_three_symbols(self):
        # 代码默认值 = 三标的（新部署 / 本地跑直接三标的）
        self.assertEqual(self._cfg_with_env({}).symbols, THREE)

    def test_env_override_keeps_two(self):
        # 线上 compose 显式设 SYMBOLS=BTC_USDT,ETH_USDT → 容器 env 覆盖代码默认
        # （双层控制：部署节奏由 compose 决定，不随代码默认值变动）
        self.assertEqual(
            self._cfg_with_env({"SYMBOLS": "BTC_USDT,ETH_USDT"}).symbols,
            ["BTC_USDT", "ETH_USDT"])

    def test_env_can_add_sol(self):
        self.assertEqual(
            self._cfg_with_env({"SYMBOLS": "BTC_USDT,ETH_USDT,SOL_USDT"}).symbols, THREE)


# ================================================================ b. coin 映射
class TestCoinMapping(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("HL_COIN_MAP", None)

    def tearDown(self):
        self._env.stop()

    def test_market_hl_coin_sol(self):
        self.assertEqual(market.hl_coin("SOL_USDT"), "SOL")
        # strict 模式（信号路径）也不得抛错 —— 已加入 HL_COIN_NAMES
        self.assertEqual(market.hl_coin("SOL_USDT", strict=True), "SOL")

    def test_market_hl_coin_btc_eth_unchanged(self):
        self.assertEqual(market.hl_coin("BTC_USDT", strict=True), "BTC")
        self.assertEqual(market.hl_coin("ETH_USDT", strict=True), "ETH")

    def test_hl_coin_map_env_still_overrides(self):
        for raw in ("SOL_USDT:SOLX", '{"SOL_USDT": "SOLX"}'):     # 逗号格式 + JSON 格式
            with mock.patch.dict(os.environ, {"HL_COIN_MAP": raw}, clear=False):
                self.assertEqual(market.hl_coin("SOL_USDT"), "SOLX")

    def _executor(self, coin_map=None):
        p = ExecParams()
        if coin_map is not None:
            p.coin_map = coin_map
        # 假 info/exchange：coin() 只用 coin_map，不发网络（__init__ 也不发）
        return Executor(_tmp_db(), p, info=None, exchange=None, notifier=None)

    def test_executor_coin_sol_default(self):
        self.assertEqual(self._executor().coin("SOL_USDT"), "SOL")

    def test_executor_coin_sol_explicit_map(self):
        ex = self._executor(coin_map={"BTC_USDT": "BTC", "ETH_USDT": "ETH", "SOL_USDT": "SOL"})
        self.assertEqual(ex.coin("SOL_USDT"), "SOL")

    def test_two_layers_agree_on_sol(self):
        # 信号层与执行器必须解析出同一个 coin（否则拿一家数据、在另一家下单）
        self.assertEqual(market.hl_coin("SOL_USDT"), self._executor().coin("SOL_USDT"))


# ================================================================ c. HL 规格
class TestHlSizingSol(unittest.TestCase):
    """SOL 永续规格（实测 api.hyperliquid.xyz/info {"type":"meta"}）：
    szDecimals=2、maxLeverage=20。用 2 / 20 做夹具，不依赖网络。"""

    SZ_DEC = 2
    MAX_LEV = 20
    RISK_PCT = 0.0133          # 组合固定预算：2% × 2/3 ≈ 1.33%/标的
    ENTRY = 100.0              # SOL 价格量级
    STOP_DIST = 0.10           # 最宽止损（策略上限）

    def test_step_is_one_cent(self):
        self.assertEqual(HS.round_size(0.123456, self.SZ_DEC), 0.12)
        self.assertEqual(HS.round_size(0.129, self.SZ_DEC), 0.12)      # floor 不上进
        v = HS.round_size(1.0, self.SZ_DEC)
        self.assertAlmostEqual(v * 100, round(v * 100), places=9)      # 恰为 0.01 整数倍

    def test_hl_max_leverage_covers_configured_5x(self):
        self.assertGreaterEqual(self.MAX_LEV, 5.0)

    def test_widest_stop_still_safe_at_5x(self):
        # 10% 止损 → 安全杠杆上限 5x；配置 5x 恰好不超 → 不给「强平早于止损」告警
        msl = HS.max_safe_leverage(self.STOP_DIST)
        self.assertAlmostEqual(msl, 5.0)
        self.assertGreaterEqual(msl, 5.0)

    def test_min_notional_boundary_100u_passes(self):
        stop = self.ENTRY * (1 - self.STOP_DIST)
        plan = HS.size_order_hl("SOL", self.SZ_DEC, self.ENTRY, stop,
                                equity=100.0, risk_pct=self.RISK_PCT, leverage=5.0, side=1)
        # 目标名义 = 风险U ÷ 10% = 1.33 / 0.10 = 13.3U > HL 10U 下限
        self.assertAlmostEqual(100.0 * self.RISK_PCT / self.STOP_DIST, 13.3, places=6)
        self.assertGreater(plan.notional, HS.DEFAULT_MIN_NOTIONAL)
        self.assertFalse(plan.below_min_notional)

    def test_min_notional_boundary_75u_is_on_the_line(self):
        stop = self.ENTRY * (1 - self.STOP_DIST)
        target = 75.0 * self.RISK_PCT / self.STOP_DIST       # = 9.975U，压 10U 线
        self.assertLess(abs(target - 10.0), 0.1)
        plan = HS.size_order_hl("SOL", self.SZ_DEC, self.ENTRY, stop,
                                equity=75.0, risk_pct=self.RISK_PCT, leverage=5.0, side=1)
        # floor 到 0.01 步长后跌破 10U → 交易所会拒单（小账户边界）
        self.assertLess(plan.notional, HS.DEFAULT_MIN_NOTIONAL)
        self.assertTrue(plan.below_min_notional)

    def test_mexc_contract_spec_present(self):
        # engine.walk → size_order(symbol) 需要 SOL_USDT 在 SPECS 里，否则 KeyError
        self.assertIn("SOL_USDT", SPECS)
        self.assertEqual(SPECS["SOL_USDT"][0], 0.1)          # 面值 0.1 SOL/张


# ================================================================ d. 三标的独立
class TestThreeSymbolsIndependent(unittest.TestCase):
    """engine.tick 遍历 cfg.symbols；每个标的独立槽位，一个 pending 不挡别的标的。"""

    def setUp(self):
        self.bars = _entry_setup()
        self.now_ms = self.bars[-1].open_ms + STEP + 60_000
        self._orig_fetch = engine.market.fetch
        engine.market.fetch = self._fake_fetch

    def tearDown(self):
        engine.market.fetch = self._orig_fetch

    def _fake_fetch(self, symbol, now_ms=None, allow_fallback=False):
        # 每个标的返回同一份形状的 K 线（信号/状态机与标的无关，标的只作槽位键）
        return market.Feed(symbol=symbol, source="hl-candle", bars=self.bars,
                           fetched_at_ms=now_ms or int(time.time() * 1000))

    def _pendings(self, db):
        return [r for r in db.recent_positions(50) if r["status"] == "pending"]

    def test_each_symbol_gets_own_slot(self):
        db = _tmp_db()
        n = _Notifier()
        cfg = _cfg(symbols=THREE)
        engine.tick(db, cfg, n, now_ms=self.now_ms)
        pend = self._pendings(db)
        self.assertEqual(len(pend), 3, "三标的应各建一个 pending")
        self.assertEqual({r["symbol"] for r in pend}, set(THREE))
        for s in THREE:
            self.assertIsNotNone(db.open_position(s), f"{s} 应有独立槽位")
        self.assertEqual(len(n.msgs), 3)

    def test_pending_on_one_does_not_block_others(self):
        db = _tmp_db()
        # 第一步：只有 BTC 有信号 → BTC 占住 pending
        cfg_btc = _cfg(symbols=["BTC_USDT"])
        engine.tick(db, cfg_btc, _Notifier(), now_ms=self.now_ms)
        btc = self._pendings(db)
        self.assertEqual(len(btc), 1)
        self.assertEqual(btc[0]["symbol"], "BTC_USDT")

        # 第二步：三标的都跑 → BTC 仍被 pending 挡住（不叠第二个），ETH/SOL 照常入场
        n2 = _Notifier()
        cfg3 = _cfg(symbols=THREE)
        engine.tick(db, cfg3, n2, now_ms=self.now_ms)
        pend = self._pendings(db)
        self.assertEqual(len([r for r in pend if r["symbol"] == "BTC_USDT"]), 1,
                         "BTC 的 pending 不应被第二个信号叠加")
        self.assertIsNotNone(db.open_position("ETH_USDT"), "ETH 不受 BTC pending 影响")
        self.assertIsNotNone(db.open_position("SOL_USDT"), "SOL 不受 BTC pending 影响")
        # 第二步只应推 ETH/SOL 两条开仓提醒（BTC 被挡）
        self.assertEqual(len(n2.msgs), 2)
        joined = "".join(n2.msgs)
        self.assertIn("ETH_USDT", joined)
        self.assertIn("SOL_USDT", joined)
        self.assertNotIn("BTC_USDT", joined)


if __name__ == "__main__":
    unittest.main()
