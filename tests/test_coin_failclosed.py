"""coin 映射 fail-closed 回归测试（`feat/coin-failclosed`）。

背景（血泪）：行情层 `market.hl_coin` 与执行层 `Executor.coin` 过去都有
`symbol.split("_")[0]` 的**隐式兜底**。`1000PEPE_USDT` 这种标的会被静默拼成
`1000PEPE` —— 一个在这个系统里并不存在的 HL 合约名，于是「拿错标的的行情算信号」、
「在错标的上发单/补挂止损」。本批把两层都改成消费单一注册表
(`app/symbol_registry.py`) 且**未配置即拦/报错**：

- 行情层：env `HL_COIN_MAP` 覆盖 → 注册表 → 抛 `FeedError`（信息含
  「未配置 HL coin 映射：{symbol}」），永不拼前缀。
- 执行层：executor coin_map（env/DB 覆盖）→ 注册表 → `SymbolNotConfigured`；
  入场路径有显性闸门（`_entry_gates`）拒绝开仓 + 落 skipped 意图 + 告警，
  且 dry 模式也不抛异常崩掉整轮 tick。

约束：不放本地绝对路径（CI 容器没有 /opt/data）；不 import app.main；零网络
（`market._hl_post` 一律打桩成「被调用即失败」，用来证明根本没发出请求）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import market, symbol_registry as registry                  # noqa: E402
from app.db import DB                                                # noqa: E402
from app.hl import MODE_DRY, MODE_LIVE                               # noqa: E402
from app.hl import sizing as hl_sizing                               # noqa: E402
from app.hl.executor import DEFAULT_COIN_MAP, Executor, ExecParams    # noqa: E402
from app.market import FeedError                                      # noqa: E402
from app.symbol_registry import SymbolNotConfigured                    # noqa: E402

THREE = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]
COINS = {"BTC_USDT": "BTC", "ETH_USDT": "ETH", "SOL_USDT": "SOL"}
# 典型「split 会拼错」的标的：HL 上并不存在 1000PEPE 这个名字（HL 上是 kPEPE）
FAKE = "1000PEPE_USDT"


def _no_env_coin_map():
    """临时清掉 HL_COIN_MAP（否则线上/本机 env 会干扰断言）。"""
    return mock.patch.dict(os.environ, {}, clear=False)


def _tmp_db() -> DB:
    return DB(str(Path(tempfile.mkdtemp()) / "t.db"))


class _Notifier:
    def __init__(self):
        self.msgs = []

    def send_message(self, m):
        self.msgs.append(m)
        return True


class _FakeExchange:
    """只读/写通道的最小假桩：任何下单调用都会被记下来（断言「一次都没发」）。"""

    def __init__(self):
        self.calls = []

    @property
    def available(self):
        return True

    @property
    def init_error(self):
        return ""

    def mid(self, coin):
        return 83000.0

    def update_leverage(self, *a, **kw):
        self.calls.append(("leverage", a))
        return {"ok": True, "status": "resting", "error": None, "oid": None}

    def place_order(self, *a, **kw):
        self.calls.append(("order", a))
        return {"ok": True, "status": "filled", "error": None, "oid": 1,
                "avg_px": 1.0, "filled_sz": 1.0, "raw": {}}

    def cancel(self, *a, **kw):
        self.calls.append(("cancel", a))
        return {"ok": True, "status": "resting", "error": None, "oid": None}

    def _cloid(self, symbol, bar_ms, purpose):
        from app.hl.exchange import make_cloid
        return make_cloid(symbol, bar_ms, purpose)


class _FakeInfo:
    def all_mids(self):
        return {}

    def snapshot(self, address, mids=None):
        return {"ts": 1.0, "address": "0xabc", "equity": 1000.0, "withdrawable": 1000.0,
                "margin_used": 0.0, "notional": 0.0, "positions": [], "open_orders": [],
                "orders_trusted": True}

    def specs(self, coins=None):
        # 价格/数量精度给足（sz 向下取整到 sz_decimals；返回空 dict 会让数量变 0，
        # 走到「低于交易所下限」闸门，测不到想测的东西）
        return {c: {"sz_decimals": 5, "max_leverage": 40} for c in (coins or ["BTC"])}

    def open_orders(self, address):
        return []

    def user_fills(self, address):
        return []


def _executor(mode=MODE_LIVE, coin_map=None, db=None):
    p = ExecParams(mode=mode, account_address="0xabc", private_key="0xkey",
                   equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="testrev",
                   # 本文件不建模信号时效；时效闸由 tests/test_hl_signal_age.py 专测。
                   max_signal_age_min=0)
    if coin_map is not None:
        p.coin_map = coin_map
    return Executor(db or _tmp_db(), p, _FakeInfo(), _FakeExchange(),
                    notifier=_Notifier())


def _plan():
    """一份合法的计划（闸门只看计划字段，与 symbol 无关）。"""
    return hl_sizing.size_order_hl("BTC", 5, 83000.0, 80600.0, 1000.0, 0.02, 5.0, side=1)


# ================================================================ a. 行情层
class TestMarketCoinFailClosed(unittest.TestCase):
    def setUp(self):
        self._env = _no_env_coin_map()
        self._env.start()
        os.environ.pop("HL_COIN_MAP", None)

    def tearDown(self):
        self._env.stop()

    def test_registry_symbols_resolve(self):
        for s, c in COINS.items():
            with self.subTest(symbol=s):
                self.assertEqual(market.hl_coin(s), c)
                self.assertEqual(market.hl_coin(s, strict=True), c)

    def test_hl_coin_names_derived_from_registry(self):
        """硬编码 dict 已改成从注册表派生：注册表加标的自带映射，别处不再手写。"""
        self.assertEqual(market.HL_COIN_NAMES,
                         {s: registry.hl_coin(s) for s in registry.known_symbols()})
        for s in registry.known_symbols():
            self.assertEqual(market.HL_COIN_NAMES[s], registry.hl_coin(s))

    def test_env_override_still_wins(self):
        for raw in (f"{FAKE}:kPEPE", '{"%s": "kPEPE"}' % FAKE):     # 逗号格式 + JSON 格式
            with mock.patch.dict(os.environ, {"HL_COIN_MAP": raw}, clear=False):
                self.assertEqual(market.hl_coin(FAKE), "kPEPE")
                self.assertEqual(market.hl_coin("BTC_USDT"), "BTC")  # 未覆盖的仍走注册表

    def test_env_override_beats_registry(self):
        with mock.patch.dict(os.environ, {"HL_COIN_MAP": "BTC_USDT:kPEPE"}, clear=False):
            self.assertEqual(market.hl_coin("BTC_USDT"), "kPEPE")

    def test_unregistered_raises_instead_of_prefix(self):
        with self.assertRaises(FeedError) as ctx:
            market.hl_coin(FAKE)
        self.assertIn("未配置 HL coin 映射", str(ctx.exception))
        self.assertIn(FAKE, str(ctx.exception))
        # 旧行为是**无声返回** "1000PEPE"（split 兜底）；现在必须抛错而不是返回
        with self.assertRaises(FeedError):
            market.hl_coin(FAKE)

    def test_unregistered_raises_with_and_without_strict(self):
        """strict 参数保留兼容，但真假两种取值都必须 fail-closed。"""
        for strict in (False, True):
            with self.subTest(strict=strict):
                with self.assertRaises(FeedError):
                    market.hl_coin("DOGE_USDT", strict=strict)

    def test_fetch_path_never_requests_wrong_coin(self):
        """取数前就抛：连一次 HTTP 都不该发出（打桩函数被调用即崩）。"""
        def boom(*a, **kw):
            raise AssertionError("不该发出任何请求（coin 未配置）")
        saved = market._hl_post
        # 用打桩替换（**不要 del**：删掉模块属性会让别的测试文件 setUp 里
        # `market._hl_post` 取不到 → 跨文件连锁报错）
        market._hl_post = boom
        try:
            with self.assertRaises(FeedError) as ctx:
                market._hyperliquid(FAKE)
            self.assertIn("未配置 HL coin 映射", str(ctx.exception))
        finally:
            market._hl_post = saved


# ================================================================ b. 执行层
class TestExecutorCoinFailClosed(unittest.TestCase):
    def setUp(self):
        self._env = _no_env_coin_map()
        self._env.start()
        os.environ.pop("HL_COIN_MAP", None)

    def tearDown(self):
        self._env.stop()

    def test_default_coin_map_derives_from_registry(self):
        """DEFAULT_COIN_MAP 过去漏了 SOL（靠 split 兜底才没炸）——必须与注册表同源。"""
        self.assertEqual(DEFAULT_COIN_MAP,
                         {s: registry.hl_coin(s) for s in registry.known_symbols()})
        self.assertIn("SOL_USDT", DEFAULT_COIN_MAP)

    def test_coin_three_symbols(self):
        ex = _executor()
        for s, c in COINS.items():
            with self.subTest(symbol=s):
                self.assertEqual(ex.coin(s), c)

    def test_coin_map_override_wins(self):
        ex = _executor(coin_map={"BTC_USDT": "kBTC"})
        self.assertEqual(ex.coin("BTC_USDT"), "kBTC")
        self.assertEqual(ex.coin("ETH_USDT"), "ETH")      # 未覆盖的回落注册表

    def test_coin_unregistered_raises_not_prefix(self):
        ex = _executor()
        with self.assertRaises(SymbolNotConfigured) as ctx:
            ex.coin(FAKE)
        self.assertIn("未配置 HL coin 映射", str(ctx.exception))
        self.assertIn(FAKE, str(ctx.exception))
        # 旧行为会返回 "1000PEPE"（split 兜底）——现在必须是异常
        self.assertIn(FAKE, str(ctx.exception))

    def test_coin_or_none_does_not_raise(self):
        """容错入口（闸门/对账/离场）用不抛的版本，否则一个配置错误会崩掉整轮 tick。"""
        ex = _executor()
        self.assertIsNone(ex._coin_or_none(FAKE))
        self.assertEqual(ex._coin_or_none("BTC_USDT"), "BTC")

    def test_entry_gates_block_unregistered(self):
        ex = _executor()
        gates = ex._entry_gates(FAKE, _plan(), _FakeInfo().snapshot("0xabc"), equity=1000.0)
        self.assertTrue(gates, "未配置 coin 必须进闸门 bad 列表")
        self.assertTrue(any("未配置 HL coin 映射" in g for g in gates), gates)
        self.assertTrue(any(FAKE in g for g in gates), gates)
        # 注册表内的标的照旧不报这条
        ok = ex._entry_gates("BTC_USDT", _plan(), _FakeInfo().snapshot("0xabc"), equity=1000.0)
        self.assertEqual([g for g in ok if "未配置 HL coin 映射" in g], [])

    def _signal(self, db, symbol):
        return db.create_position(symbol=symbol, signal_bar_ms=1791912000000, side=1,
                                  entry_price=83000.0, stop_price=80600.0,
                                  stop_dist=abs(83000.0 - 80600.0) / 83000.0, contracts=1,
                                  coin_qty=0.001, notional=83.0, risk_u=20.0,
                                  equity_at_entry=1000.0)

    def test_on_entry_unregistered_blocks_and_alerts(self):
        for mode in (MODE_LIVE, MODE_DRY):
            with self.subTest(mode=mode):
                db = _tmp_db()
                ex = _executor(mode=mode, db=db)
                pid = self._signal(db, FAKE)
                self.assertTrue(ex.on_entry(pid), "已接管 → 调用方不要再走人工确认流")
                self.assertEqual(ex.ex.calls, [], "绝不能发出任何下单/杠杆请求")
                msgs = ex.notifier.msgs
                self.assertTrue(msgs, "必须告警")
                self.assertIn("未配置 HL coin 映射", msgs[0])
                self.assertIn(FAKE, msgs[0])
                # 留痕：skipped 意图（不进入下单流程）
                rows = [r for r in db.hl_recent_orders(20) if r["purpose"] == "entry"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "skipped")

    def test_on_entry_registered_still_runs(self):
        """回归护栏：三标的照旧能进入正常流程（dry 下落 planned 计划）。"""
        db = _tmp_db()
        ex = _executor(mode=MODE_DRY, db=db)
        pid = self._signal(db, "BTC_USDT")
        self.assertFalse(ex.on_entry(pid), "dry 必须返回 False，让人工提醒照常发出")
        rows = [r for r in db.hl_recent_orders(20) if r["purpose"] == "entry"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "planned")

    def test_reconcile_unregistered_is_a_problem_not_a_crash(self):
        db = _tmp_db()
        db.create_position(symbol=FAKE, signal_bar_ms=1791912000000, side=1,
                           entry_price=83000.0, stop_price=80600.0, stop_dist=0.03,
                           contracts=1, coin_qty=0.001, notional=83.0, risk_u=20.0,
                           equity_at_entry=1000.0)
        db.set_pos_hl(1, hl_status="open")
        db.hl_record_intent(FAKE, FAKE, 1791912000000, "entry", 1, 0.001, status="filled")
        ex = _executor(db=db)
        res = ex.reconcile()                               # 不抛
        self.assertFalse(res["ok"])
        self.assertTrue(any("未配置 HL coin 映射" in p for p in res["problems"]), res["problems"])

    def test_exit_does_not_take_over_when_coin_missing(self):
        db = _tmp_db()
        db.create_position(symbol=FAKE, signal_bar_ms=1791912000000, side=1,
                           entry_price=83000.0, stop_price=80600.0, stop_dist=0.03,
                           contracts=1, coin_qty=0.001, notional=83.0, risk_u=20.0,
                           equity_at_entry=1000.0)
        db.set_pos_hl(1, hl_status="open")
        ex = _executor(db=db)
        self.assertFalse(ex.on_exit(1, "ma20", 81000.0), "解析不出 coin 就让出接管权（人工流）")
        self.assertEqual(ex.ex.calls, [], "绝不能拿猜的名字去平仓/撤单")
        self.assertIn("未配置 HL coin 映射", ex.notifier.msgs[0])


if __name__ == "__main__":
    unittest.main()
