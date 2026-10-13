"""引擎状态机 / 幂等 / 签名链接单测。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, strategy as S                                # noqa: E402
from app.config import Config                                        # noqa: E402
from app.db import DB                                                # noqa: E402
from tests.test_strategy import (STEP, trend_then_flat, breakout_bar,  # noqa: E402
                                 flat_bar)


def cfg(**kw) -> Config:
    c = Config()
    c.equity, c.risk_pct, c.leverage = 1000.0, 0.02, 5.0
    c.stop_min, c.stop_max = 0.015, 0.10
    c.token_secret = "test-secret"
    c.base_url = "https://ma.example.com"
    c.alert_prefix = "【双均线】"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def prep(bars):
    ind = S.compute_indicators(bars)
    return ind, S.squeeze_flags(ind["band"])


def entry_setup():
    """构造：横盘密集 → 突破 bar → 次根（= 实盘里「进行中」的那根）。

    返回 (bars, 突破 bar 下标)。walk 的契约是「最后一根 = 未收盘的进行中 bar」，
    所以突破 bar 必须在 len-2，次根的 open 就是入场价。
    """
    base = trend_then_flat()
    bo = breakout_bar(base, 0.04)
    bars = base + [bo, flat_bar(bo, bo.close)]
    return bars, len(bars) - 2


class TestWalk(unittest.TestCase):
    def test_entry_created_at_latest_bar(self):
        bars, i = entry_setup()
        ind, sq = prep(bars)
        c = cfg()
        events, pos = engine.walk(c, "BTC_USDT", bars, ind, sq, None, i, i, i)
        self.assertEqual([e.kind for e in events], ["entry"])
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.entry_price, bars[i].close)     # 回放时 entry 用信号 bar 收盘
        self.assertAlmostEqual(pos.stop_price, ind["ma_min"][i])
        self.assertIn("做多信号", engine.format_entry(c, events[0].sig, events[0].order, 1))
        self.assertIn("逐仓 5x", engine.format_entry(c, events[0].sig, events[0].order, 1))
        self.assertIn("/c/1?a=view&t=", engine.format_entry(c, events[0].sig, events[0].order, 1))

    def test_old_signal_is_missed_not_tradeable(self):
        bars, i = entry_setup()
        bars = bars + [flat_bar(bars[-1], bars[-1].close)]   # 补一根，信号 bar 不再是最后一根已收盘 bar
        ind, sq = prep(bars)
        c = cfg()
        latest = len(bars) - 2
        events, pos = engine.walk(c, "BTC_USDT", bars, ind, sq, None, i, latest, latest)
        kinds = [e.kind for e in events]
        self.assertIn("entry_missed", kinds)
        missed = [e for e in events if e.kind == "entry_missed"][0]
        self.assertEqual(missed.bar_ms, bars[i].open_ms, "补报必须标注原信号 bar")
        # 过期那笔不建仓；但价格仍在 band 上方时，**下一根会重新给出一个合法信号**（策略本身如此）
        self.assertNotIn(i, [e.sig.bar_index for e in events if e.kind == "entry"])
        if pos is not None:
            self.assertEqual(pos.signal_bar_ms, bars[latest].open_ms)

    def test_stop_exit_takes_priority(self):
        bars, i = entry_setup()
        ind, sq = prep(bars)
        c = cfg()
        _, pos = engine.walk(c, "BTC_USDT", bars, ind, sq, None, i, i, i)
        stop = pos.stop_price
        prev = bars[-1]
        bars2 = bars + [S.Bar(open_ms=prev.open_ms + STEP, open=prev.close, high=prev.close,
                              low=stop * 0.99, close=prev.close),
                        flat_bar(prev, prev.close)]
        ind2, sq2 = prep(bars2)
        latest = len(bars2) - 2
        events, pos2 = engine.walk(c, "BTC_USDT", bars2, ind2, sq2, pos, latest, latest, latest)
        self.assertEqual([e.kind for e in events], ["exit"])
        self.assertEqual(events[0].reason, "stop")
        self.assertAlmostEqual(events[0].exit_price, stop)
        self.assertIsNone(pos2)

    def test_ma20_exit_branch(self):
        """收盘跌破 SMA20 但低点未触及止损 → reason=ma20。"""
        bars = trend_then_flat()                                   # 末端横盘在 130
        ind, sq = prep(bars)
        c = cfg()
        pos = engine.PositionState(symbol="BTC_USDT", entry_price=130.0, stop_price=120.0,
                                   contracts=1, signal_bar_ms=bars[-1].open_ms - 10 * STEP)
        last = bars[-1]
        drop = S.Bar(open_ms=last.open_ms + STEP, open=130.0, high=130.0, low=128.5, close=129.0)
        bars2 = bars + [drop, flat_bar(drop, 129.0)]
        ind2, sq2 = prep(bars2)
        latest = len(bars2) - 2
        events, pos2 = engine.walk(c, "BTC_USDT", bars2, ind2, sq2, pos, latest, latest, latest)
        self.assertEqual([e.kind for e in events], ["exit"])
        self.assertEqual(events[0].reason, "ma20")
        self.assertAlmostEqual(events[0].exit_price, 129.0)
        self.assertIsNone(pos2)

    def test_actual_stop_used_for_exit(self):
        """用户确认过实际止损价时，离场判定用实际值。"""
        bars, i = entry_setup()
        ind, sq = prep(bars)
        c = cfg()
        _, pos = engine.walk(c, "BTC_USDT", bars, ind, sq, None, i, i, i)
        pos.actual_stop = pos.stop_price * 1.02                     # 实际挂得更近
        prev = bars[-1]
        bars2 = bars + [S.Bar(open_ms=prev.open_ms + STEP, open=prev.close, high=prev.close,
                              low=pos.actual_stop * 0.999, close=prev.close),
                        flat_bar(prev, prev.close)]
        ind2, sq2 = prep(bars2)
        latest = len(bars2) - 2
        events, _ = engine.walk(c, "BTC_USDT", bars2, ind2, sq2, pos, latest, latest, latest)
        self.assertEqual(events[0].reason, "stop")
        self.assertAlmostEqual(events[0].exit_price, pos.actual_stop)


class TestTokens(unittest.TestCase):
    def test_sign_verify_roundtrip(self):
        tok = engine.sign_token("s3cret", 42, "open")
        self.assertTrue(engine.verify_token("s3cret", 42, "open", tok))
        self.assertFalse(engine.verify_token("s3cret", 42, "ignore", tok))
        self.assertFalse(engine.verify_token("other", 42, "open", tok))

    def test_confirm_url(self):
        c = cfg()
        url = engine.confirm_url(c, 7, "open")
        self.assertTrue(url.startswith("https://ma.example.com/c/7?a=open&t="))
        self.assertTrue(engine.verify_token(c.token_secret, 7, "open", url.split("t=")[1]))


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.path = "/tmp/cma_test.db"
        if os.path.exists(self.path):
            os.remove(self.path)
        self.db = DB(self.path)

    def test_failed_send_is_not_deduped(self):
        self.assertFalse(self.db.was_alerted("BTC_USDT", 1, "entry"))
        self.db.log_alert("BTC_USDT", 1, "entry", "text", success=False)
        self.assertFalse(self.db.was_alerted("BTC_USDT", 1, "entry"),
                         "发送失败不能标记已推送，否则永久丢提醒")
        self.db.log_alert("BTC_USDT", 1, "entry", "text", success=True)
        self.assertTrue(self.db.was_alerted("BTC_USDT", 1, "entry"))

    def test_position_lifecycle(self):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1, entry_price=100.0,
                                      stop_price=95.0, stop_dist=0.05, contracts=10, coin_qty=0.001,
                                      notional=100.0, risk_u=5.0)
        self.assertEqual(self.db.open_position("BTC_USDT")["status"], "pending")
        self.db.confirm_position(pid, 100.5, 10, 95.0, True)
        self.assertEqual(self.db.open_position("BTC_USDT")["status"], "open")
        self.db.close_position(pid, 110.0, "ma20")
        self.assertIsNone(self.db.open_position("BTC_USDT"))
        closed = self.db.closed_positions(0.0)
        self.assertAlmostEqual(closed[0]["r_multiple"], (110.0 - 100.5) / (100.5 - 95.0), places=6)

    def test_ignore_position(self):
        pid = self.db.create_position(symbol="ETH_USDT", signal_bar_ms=2, entry_price=10.0,
                                      stop_price=9.0, stop_dist=0.1, contracts=1, coin_qty=0.1,
                                      notional=1.0, risk_u=0.1)
        self.db.ignore_position(pid)
        self.assertIsNone(self.db.open_position("ETH_USDT"))

    def test_bar_state_roundtrip(self):
        self.assertIsNone(self.db.get_last_bar("BTC_USDT"))
        self.db.set_last_bar("BTC_USDT", 123)
        self.assertEqual(self.db.get_last_bar("BTC_USDT"), 123)

    def test_config_override(self):
        self.db.set_config("equity", 5000)
        self.assertEqual(self.db.get_config()["equity"], "5000")
        c = Config().apply_db(self.db.get_config())
        self.assertEqual(c.equity, 5000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDisplayTime(unittest.TestCase):
    """提醒里的时间必须是北京时间（用户在国内看），且 bar 报收盘时刻。"""

    def test_bar_close_is_beijing_time(self):
        # 10-12 20:00 UTC 的 bar（= 北京 10-13 04:00）→ 收盘 10-13 00:00 UTC = 北京 08:00
        ms = 1791835200000
        self.assertEqual(engine.fmt_ms(ms), "10-13 04:00")
        self.assertEqual(engine.fmt_bar_close(ms), "10-13 08:00")
        self.assertEqual(engine.TZ_LABEL, "北京时间")

    def test_alert_texts_label_beijing(self):
        c = cfg()
        sig = S.Signal(symbol="BTC_USDT", bar_index=0, bar_open_ms=1791835200000,
                       entry_price=81812.8, stop_price=84059.96, stop_dist=0.0275,
                       ma_max=84059.96, ma_min=81812.8, band=0.02, side=-1)
        order = type("O", (), {"notional": 728.0, "contracts": 89, "risk_u": 20.0,
                               "margin": 146.0})()
        first = engine.format_entry(c, sig, order, 1).splitlines()[0]
        self.assertIn("10-13 08:00 北京时间", first)
        pos = type("P", (), {"symbol": "BTC_USDT", "side": -1, "action_label": "空单",
                             "close_side": "买入平仓", "eff_entry": 82909.3,
                             "eff_stop": 84060.0})()
        first = engine.format_exit(c, pos, 83829.3, "ma20", 1791864000000, 1).splitlines()[0]
        self.assertIn("10-13 16:00 北京时间收盘", first)
        self.assertNotIn("UTC", first)
