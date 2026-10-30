"""PENDING_BARS_MAX 自动过期的 tick 级回归测试。

背景：PR #69 删掉人工确认页后，pending 行**唯一的归宿**只剩
`engine._tick_symbol` 里的自动过期分支（app/engine.py 的 `age_bars >= PENDING_BARS_MAX`
判断）。这条分支一旦回归，一次漏看的提醒就会把该标的的仓位槽永久堵死 —— 没人会
收到报错，只会「再也不来信号」。所以用 tick 级用例钉住它的四个面：

  ① 窗口内：pending 占住该标的唯一槽位 —— 行保持 pending、同 tick 的新信号不评估；
  ② 到期：`(now − signal_bar_ms) // INTERVAL_MS >= PENDING_BARS_MAX` →
     `db.ignore_position(id)` + 一条「已作废…槽位已释放」提醒；
  ③ 同轮释放：过期后**同一轮 walk 继续跑**，紧接的新信号能建出新 pending（槽位真释放）；
  ④ 边界：`age_bars == PENDING_BARS_MAX − 1` 仍保留（差一根不误杀）。

时间一律用写死的时间轴（`tests.test_strategy.STEP` = 4h = INTERVAL_MS）：`now_ms`
显式传给 `engine.tick`，不用 `time.time()`，跨日/跨机器都不 flaky。零网络 ——
`market.fetch` 全程打桩。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market                                    # noqa: E402
from app.db import DB                                             # noqa: E402
from tests.test_engine import cfg, entry_setup                     # noqa: E402
from tests.test_strategy import STEP, flat_bar                     # noqa: E402

SYMBOL = "BTC_USDT"
MAX = engine.PENDING_BARS_MAX          # 3（文案里的「约 8 小时」= (MAX-1)*4）
HOUR_MS = engine.INTERVAL_MS           # 4h；与 STEP 同一个值，tick 的 bar 间距


class _Notifier:
    def __init__(self):
        self.msgs = []

    def send_message(self, m):
        self.msgs.append(m)
        return True


class _Base(unittest.TestCase):
    """一套写死的时间轴：突破 bar 在 len-2，signal_bar_ms = 该 bar 的 open_ms。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.c = cfg()
        self.c.symbols = [SYMBOL]
        self.c.dry_run = False
        self.bars, self.i = entry_setup()
        self.signal_ms = self.bars[self.i].open_ms
        # 第二根 tick 的喂数：在突破 bar 之后补一根同价 bar（成为新的「进行中」bar），
        # 于是 len-2 变成突破 bar 的次根 —— 该根在「不被 pending 挡住」时本身就是
        # 一个合法入场信号（见 _assert_next_bar_has_signal）。用它来检验
        # 「跳过评估」与「同轮建新 pending」是不是真的发生了。
        self.bars_next = self.bars + [flat_bar(self.bars[-1], self.bars[-1].close)]
        self.feed = self.bars
        self.notifier = _Notifier()
        self._orig_fetch = market.fetch
        market.fetch = self._fetch

    def tearDown(self):
        market.fetch = self._orig_fetch
        self.tmp.cleanup()

    # ------------------------------------------------------------ 夹具
    def _fetch(self, symbol, now_ms=None, allow_fallback=False):
        """feed 桩：只回当前 self.feed，不做任何真实网络/校验。"""
        return market.Feed(symbol=symbol, source="hl-candle", bars=self.feed,
                           fetched_at_ms=now_ms if now_ms is not None else self.feed[-1].open_ms)

    def _tick(self, now_ms, bars=None):
        """跑一次 engine.tick（now_ms 必传：过期判定只看它，不看系统时钟）。"""
        if bars is not None:
            self.feed = bars
        sent = engine.tick(self.db, self.c, self.notifier, now_ms=now_ms)
        return sent

    def _pending(self):
        return [r for r in self.db.recent_positions(50) if r["status"] == "pending"]

    def _by_id(self, pid):
        for r in self.db.recent_positions(50):
            if int(r["id"]) == int(pid):
                return r
        return None

    def _create_pending(self):
        """第一根 tick：突破 bar 触发入场 → 建 pending 行（返回该行 id）。"""
        self._tick(self.signal_ms + 60_000, bars=self.bars)
        rows = self._pending()
        self.assertEqual(len(rows), 1, "前置：第一根 tick 应建出一个 pending")
        self.assertEqual(int(rows[0]["signal_bar_ms"]), self.signal_ms)
        self.notifier.msgs.clear()
        return int(rows[0]["id"])

    def _assert_next_bar_has_signal(self):
        """守卫：确认 bars_next 的 len-2 根本身是个合法入场信号。

        没有这一步，「不评估新入场」「槽位已释放」两条断言可能只是空转（那根根本
        就没信号）。纯 walk、不碰库。
        """
        ind = engine.strategy.compute_indicators(self.bars_next)
        sq = engine.strategy.squeeze_flags(ind["band"], self.c.band_window,
                                           self.c.band_quantile, self.c.min_squeeze_bars)
        latest = len(self.bars_next) - 2
        events, _ = engine.walk(self.c, SYMBOL, self.bars_next, ind, sq, None,
                                latest, latest, latest)
        self.assertIn("entry", [e.kind for e in events],
                      "前置：bars_next 的最新已收盘 bar 应当是个入场信号")


class TestPendingExpiry(_Base):
    # ① 窗口内：占住槽位、不评估新入场
    def test_within_window_holds_slot_and_skips_new_signal(self):
        pid = self._create_pending()
        self._assert_next_bar_has_signal()          # 新 bar 确实有信号
        last_bar_before = self.db.get_last_bar(SYMBOL)

        # age_bars = 2（< MAX）；喂进来的数据里明摆着一个新信号
        self._tick(self.signal_ms + 2 * HOUR_MS, bars=self.bars_next)

        row = self._by_id(pid)
        self.assertEqual(row["status"], "pending", "窗口内必须保持 pending（不可提前作废）")
        pend = self._pending()
        self.assertEqual(len(pend), 1, "同一标的只允许一个 pending，不得叠加")
        self.assertEqual(int(pend[0]["id"]), pid, "占槽的仍是原行，不是新建的行")
        self.assertEqual(self.notifier.msgs, [],
                         "被 pending 占槽时，新信号不得评估/推送（否则叠出幽灵行）")
        self.assertEqual(self.db.get_last_bar(SYMBOL), last_bar_before,
                         "窗口内不得推进 bar 进度（晚确认后要能从入场根重扫）")

    # ② 到期：作废 + 提醒文案
    def test_expired_pending_is_ignored_with_release_alert(self):
        self.assertEqual(MAX, 3, "文案按 PENDING_BARS_MAX=3 写「约 8 小时」，改常量要同步文案")
        pid = self._create_pending()

        self._tick(self.signal_ms + MAX * HOUR_MS, bars=self.bars_next)

        row = self._by_id(pid)
        self.assertEqual(row["status"], "ignored", "到期必须落 ignored（PR B 后无人工确认页兜底）")
        self.assertIsNotNone(row["closed_at"], "作废要写 closed_at，否则面板/统计口径会漏")

        hit = [m for m in self.notifier.msgs if "作废" in m]
        self.assertEqual(len(hit), 1, "作废提醒恰好一条")
        text = hit[0]
        self.assertIn(SYMBOL, text)
        self.assertIn("待确认仓位已作废", text)
        self.assertIn("槽位已释放", text)
        self.assertIn("8 小时", text, "用户体感窗口 = (PENDING_BARS_MAX-1) 根 4h")
        self.assertIn("若你实际开了仓，请自行管理离场", text)

        # 落了 alert_logs 且标记为发送成功（幂等键 = 信号 bar 时间戳）
        logs = [a for a in self.db.recent_alerts(200) if a["kind"] == "exit"]
        self.assertEqual(len([a for a in logs if "作废" in str(a["message"])]), 1)
        self.assertEqual([a["sent_successfully"] for a in logs if "作废" in str(a["message"])], [1])

    # ③ 同轮槽位真释放：过期那一根 tick 里就能为新信号建 pending
    def test_same_tick_slot_is_released_for_new_pending(self):
        old_pid = self._create_pending()
        self._assert_next_bar_has_signal()
        new_signal_ms = self.bars_next[-2].open_ms

        self._tick(self.signal_ms + MAX * HOUR_MS, bars=self.bars_next)

        pend = self._pending()
        self.assertEqual(len(pend), 1, "旧行作废后，同一轮 tick 就应建出新 pending")
        new_row = pend[0]
        self.assertNotEqual(int(new_row["id"]), old_pid, "必须是新行（旧行已 ignored）")
        self.assertEqual(int(new_row["signal_bar_ms"]), new_signal_ms,
                         "新 pending 挂在紧随其后的新信号 bar 上")
        self.assertEqual(self._by_id(old_pid)["status"], "ignored")
        self.assertEqual(int(self.db.open_position(SYMBOL)["id"]), int(new_row["id"]),
                         "open_position 读到的槽位应当是新的那行")
        self.assertTrue(any("做多信号" in m for m in self.notifier.msgs),
                        "新信号要照常推入场提醒（槽位释放不是静默释放）")

    # ④ 边界：age_bars == MAX-1 仍保留
    def test_age_bars_max_minus_one_still_kept(self):
        pid = self._create_pending()
        now_ms = self.signal_ms + (MAX - 1) * HOUR_MS
        self.assertEqual((now_ms - self.signal_ms) // HOUR_MS, MAX - 1,
                         "构造的时间点必须正好落在 MAX-1（边界用例不能飘）")

        self._tick(now_ms, bars=self.bars_next)

        self.assertEqual(self._by_id(pid)["status"], "pending", "差一根不误杀")
        self.assertEqual([r for r in self.db.recent_positions(50)
                          if r["status"] == "ignored"], [], "边界内不得写 ignored")
        self.assertEqual(self.notifier.msgs, [], "边界内不发作废提醒")


if __name__ == "__main__":
    unittest.main()
