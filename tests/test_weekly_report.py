"""周报文案（engine.weekly_text）的回归测试。

周报是唯一一份「用户不看盘也能判断策略有没有失效」的推送，所以文案本身是契约：
数字必须来自库里、白话必须区分多空与离场原因、缺数据必须如实说缺而不是编。

这里全部用**真的 sqlite 库**（DB + tempfile）而不是 dict 假库：周报读的是
sqlite3.Row（没有 .get，缺列抛 IndexError），用 dict 假库测不到这类回归。
"""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine                                          # noqa: E402
from app.config import Config                                   # noqa: E402
from app.db import DB                                           # noqa: E402
from app.db import now as db_now                                # noqa: E402

DAY = 86400.0
MINUS = "−"          # 文案里的排版减号（U+2212），与 ASCII 横杠不同


def make_cfg(**kw) -> Config:
    c = Config()
    c.alert_prefix = "【双均线】"
    c.risk_pct = 0.0133          # 线上真实值：1.33%
    c.equity = 1000.0            # 配置本金 —— 周报**不许**拿它当账户净值
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def make_db() -> DB:
    tmp = tempfile.mkdtemp()
    return DB(os.path.join(tmp, "weekly.db"))


_NOW = db_now()          # 统一时间基准：夹具里所有相对秒数都从这一刻往回算
_SEQ = [0]               # signal_bar_ms 唯一化：positions 有 UNIQUE(symbol, signal_bar_ms)


def add_position(db: DB, *, symbol="BTC_USDT", side=-1, entry=100_000.0, stop=103_000.0,
                 status="open", created_days_ago=1.0, exit_price=None, exit_reason=None,
                 r_multiple=None, close_days_ago=0.5, opened_days_ago=None) -> int:
    """建一行仓位；closed 的按参数钉死 r_multiple / closed_at（展示层只读这几个字段）。"""
    now = _NOW
    _SEQ[0] += 1
    pid = db.create_position(symbol=symbol,
                             signal_bar_ms=int((now - created_days_ago * DAY) * 1000) + _SEQ[0],
                             side=side, entry_price=entry, stop_price=stop,
                             stop_dist=abs(entry - stop) / entry, contracts=1,
                             coin_qty=0.01, notional=entry * 0.01, risk_u=20.0,
                             equity_at_entry=1000.0)
    db.conn.execute("UPDATE positions SET created_at=?, status=? WHERE id=?",
                    (now - created_days_ago * DAY, status, pid))
    if opened_days_ago is not None:
        db.conn.execute("UPDATE positions SET opened_at=? WHERE id=?",
                        (now - opened_days_ago * DAY, pid))
    if status == "closed":
        db.conn.execute(
            "UPDATE positions SET exit_price=?, exit_reason=?, r_multiple=?, closed_at=?, "
            "actual_entry=? WHERE id=?",
            (exit_price if exit_price is not None else entry,
             exit_reason or "ma20",
             r_multiple if r_multiple is not None else 0.0,
             now - close_days_ago * DAY, entry, pid))
    db.conn.commit()
    return pid


def snap(db: DB, days_ago: float, equity: float) -> None:
    db.hl_add_snapshot({"ts": _NOW - days_ago * DAY, "equity": equity})


class WeeklyFixture(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.cfg = make_cfg()

    def render(self) -> str:
        return engine.weekly_text(self.db, self.cfg)


class TestWithClosedPositions(WeeklyFixture):
    """有平仓：概览 + 净值为真实快照 + 逐笔白话。"""

    def setUp(self):
        super().setUp()
        snap(self.db, 6.9, 1000.0)              # 窗口首条（7 天窗口边界内）
        snap(self.db, 0.01, 1087.4)             # 最新一条
        add_position(self.db, side=-1, entry=82_909.0, stop=84_056.0,
                     status="closed", exit_price=83_930.0, exit_reason="ma20",
                     r_multiple=-0.89, close_days_ago=2.0)
        add_position(self.db, symbol="ETH_USDT", side=1, entry=2_500.0, stop=2_450.0,
                     status="closed", exit_price=2_650.0, exit_reason="ma20",
                     r_multiple=3.0, close_days_ago=1.0)
        add_position(self.db, side=-1, entry=100_000.0, status="open", opened_days_ago=0.3)
        self.text = self.render()

    def test_header_is_rolling_7_day_window(self):
        today = dt.datetime.now(engine.DISPLAY_TZ).date()
        start = today - dt.timedelta(days=7)
        self.assertIn(f"【双均线】周报 {start:%m-%d}～{today:%m-%d}（窗口=过去 7 天，北京时间）",
                      self.text)

    def test_overview_counts(self):
        self.assertIn("▍本周概览", self.text)
        self.assertIn("平仓 2 笔", self.text)
        self.assertIn("BTC 做空", self.text)
        self.assertIn("ETH 做多", self.text)
        self.assertIn("新开 3 笔", self.text)
        self.assertIn("当前持仓 1", self.text)

    def test_equity_uses_snapshots_not_config_equity(self):
        self.assertIn("账户净值：1,000.0 → 1,087.4 U（本周 +87.4 U，约 +8.7%）", self.text)
        # 配置本金 1000U 只在净值首端出现一次，不再被当作「本金」二次展示
        self.assertNotIn("本金", self.text)

    def test_equity_delta_appears_once(self):
        self.assertEqual(self.text.count("+87.4 U"), 1, "净值差只该在概览出现一次")

    def test_per_trade_lines(self):
        self.assertIn("▍本周逐笔（1R = 这笔交易计划的亏损额，即开仓时净值的 1.33%）", self.text)
        self.assertIn(f"① 10-17 BTC 做空 82,909 → 83,930　{MINUS}0.89R　价格收上均线，正常离场",
                      self.text)
        self.assertIn("② 10-18 ETH 做多 2,500 → 2,650　+3.00R　价格收下均线，正常离场", self.text)

    def test_no_black_box_or_compounding(self):
        for bad in ("复利", "永续口径", "抵押品", "均R", "总R", "本金"):
            self.assertNotIn(bad, self.text)

    def test_static_evaluation_rule_always_present(self):
        self.assertIn("按既定规则：实盘满 20 笔或满 12 个月才做第一次评估", self.text)

    def test_status_line(self):
        self.assertIn("▍当前状态", self.text)
        self.assertIn("持仓：BTC 做空（", self.text)
        self.assertIn(" 开仓）｜ 执行器：", self.text)


class TestNoClosedPositions(WeeklyFixture):
    """无平仓：概览走变体、逐笔段整体省略，但其余段落照旧。"""

    def setUp(self):
        super().setUp()
        snap(self.db, 6.9, 500.0)
        snap(self.db, 0.01, 512.5)
        add_position(self.db, side=-1, status="open", opened_days_ago=2.0)
        add_position(self.db, symbol="ETH_USDT", side=1, status="open", opened_days_ago=1.0)
        self.text = self.render()

    def test_variant_line(self):
        self.assertIn("本周无平仓 · 当前持仓 2", self.text)
        self.assertNotIn("平仓 0 笔", self.text)

    def test_per_trade_section_omitted(self):
        self.assertNotIn("▍本周逐笔", self.text)
        self.assertNotIn("1R =", self.text)

    def test_lifetime_section_still_present(self):
        self.assertIn("▍上线以来（10-12 起）", self.text)
        self.assertIn("还没有已平仓的交易", self.text)

    def test_equity_still_reported(self):
        self.assertIn("账户净值：500.0 → 512.5 U（本周 +12.5 U，约 +2.5%）", self.text)

    def test_open_positions_listed(self):
        self.assertIn("BTC 做空（", self.text)
        self.assertIn("ETH 做多（", self.text)


class TestPendingSignals(WeeklyFixture):
    """pending>0 才出提醒行。"""

    def test_reminder_when_pending(self):
        add_position(self.db, status="pending")
        add_position(self.db, symbol="ETH_USDT", side=1, status="pending")
        text = self.render()
        self.assertIn("⚠️ 有 2 笔信号待确认，本周统计暂按假设值", text)
        # 提醒行紧跟概览（在净值行之前）
        self.assertLess(text.index("⚠️"), text.index("账户净值："))

    def test_no_reminder_when_zero_pending(self):
        add_position(self.db, status="open")
        text = self.render()
        self.assertNotIn("⚠️", text)
        self.assertNotIn("待确认", text)

    def test_pending_is_not_counted_as_holding(self):
        # 待确认 = 还没下单的信号，不能混进「当前持仓 N」（否则和 ⚠️ 那行重复计数、
        # 且读者会以为已经进场了）。
        add_position(self.db, status="pending")
        text = self.render()
        self.assertIn("当前持仓 0", text)
        self.assertIn("⚠️ 有 1 笔信号待确认", text)
        self.assertIn("持仓：无", text)

    def test_pending_not_counted_in_new_entries(self):
        add_position(self.db, status="pending")
        self.assertIn("本周无平仓 · 当前持仓 0", self.render())
        add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=1.0)
        text = self.render()
        self.assertIn("新开 1 笔", text)     # 只有那笔真开出来的（pending 不算）


class TestMissingEquity(WeeklyFixture):
    """净值取不到：只说明，不编数（尤其不许拿 cfg.equity 顶上）。"""

    def test_no_snapshots_at_all(self):
        add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=1.0)
        text = self.render()
        self.assertIn("账户净值：（本轮取不到净值，下轮补）", text)
        self.assertNotIn("1,000.0", text)
        self.assertNotIn("U（本周", text)

    def test_only_start_snapshot(self):
        snap(self.db, 6.9, 1000.0)              # 只有一条快照 → 算不出周变化
        text = self.render()
        self.assertIn("（本轮取不到净值，下轮补）", text)
        self.assertNotIn("→", text.split("账户净值：")[1].split("\n")[0])

    def test_zero_equity_treated_as_missing(self):
        snap(self.db, 6.9, 1000.0)
        snap(self.db, 0.01, 0.0)                # 统一账户下 equity 可能是 0，不是「亏光」
        text = self.render()
        self.assertIn("（本轮取不到净值，下轮补）", text)


class TestLosingStreakNote(WeeklyFixture):
    """连亏说明：仅当本周全部亏损且 ≥2 笔。"""

    def test_shown_for_all_losses(self):
        for d in (3.0, 2.0, 1.0):
            add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=d)
        text = self.render()
        self.assertIn("本策略历史胜率约 35%", text)
        # 0.65^3 = 27.46% → 27.5%（保留一位）
        self.assertIn(f"起步连亏的概率约 27.5%（3 笔）、17.9%（4 笔）、11.6%（5 笔）", text)
        self.assertIn("属正常波动，不是策略失效", text)

    def test_hidden_when_one_win_exists(self):
        add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=3.0)
        add_position(self.db, status="closed", r_multiple=2.0, close_days_ago=2.0)
        self.assertNotIn("历史胜率约 35%", self.render())

    def test_hidden_for_single_loss(self):
        add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=2.0)
        self.assertNotIn("历史胜率约 35%", self.render())

    def test_hidden_when_no_trades(self):
        self.assertNotIn("历史胜率约 35%", self.render())


class TestRiskDefinitionLine(WeeklyFixture):
    """R 定义行的百分比必须保留两位（旧 :.0f 把 1.33% 写成 1%）。"""

    def test_two_decimals(self):
        add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=1.0)
        text = self.render()
        self.assertIn("即开仓时净值的 1.33%）", text)
        self.assertNotIn("净值的 1%）", text)

    def test_follows_cfg(self):
        self.cfg.risk_pct = 0.02
        add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=1.0)
        self.assertIn("即开仓时净值的 2.00%）", self.render())


class TestExitReasonPlain(WeeklyFixture):
    """离场原因映射成白话；未知原因原样保留，绝不编。"""

    def _line(self, reason, side=-1):
        add_position(self.db, side=side, status="closed", r_multiple=-1.0,
                     exit_reason=reason, close_days_ago=1.0)
        return [ln for ln in self.render().split("\n") if ln.startswith("①")][0]

    def test_ma20_short(self):
        self.assertIn("价格收上均线，正常离场", self._line("ma20", side=-1))

    def test_ma20_long(self):
        self.assertIn("价格收下均线，正常离场", self._line("ma20", side=1))

    def test_stop(self):
        self.assertIn("触发止损", self._line("stop"))

    def test_agent_expiry_cleanup(self):
        self.assertIn("API 到期保护清仓", self._line("agent_expiry_cleanup"))

    def test_unknown_reason_kept_verbatim(self):
        line = self._line("manual_close_v2")
        self.assertIn("manual_close_v2", line)
        self.assertNotIn("正常离场", line)


class TestDirectionWords(WeeklyFixture):
    """多空方向词必须跟着 side 走（做空写成做多是最伤信任的错）。"""

    def test_short_and_long(self):
        add_position(self.db, side=-1, status="closed", exit_price=103_000.0,
                     exit_reason="stop", r_multiple=-1.0, close_days_ago=2.0)
        add_position(self.db, symbol="ETH_USDT", side=1, status="closed", entry=2_000.0,
                     exit_price=2_200.0, exit_reason="ma20", r_multiple=2.0, close_days_ago=1.0)
        text = self.render()
        lines = [ln for ln in text.split("\n") if ln.startswith(("①", "②"))]
        self.assertIn("BTC 做空 100,000 → 103,000", lines[0])
        self.assertIn("ETH 做多 2,000 → 2,200", lines[1])
        self.assertNotIn("BTC 做多", text)
        self.assertNotIn("ETH 做空", text)

    def test_overview_direction_counts(self):
        add_position(self.db, side=-1, status="closed", r_multiple=-1.0, close_days_ago=2.0)
        add_position(self.db, side=-1, status="closed", r_multiple=-1.0, close_days_ago=1.0)
        self.assertIn("BTC 做空×2", self.render())


class TestStatusSources(WeeklyFixture):
    """执行器 / 信号源：读不到写「未知」，绝不把 off 当默认答案。"""

    def test_exec_mode_from_db(self):
        self.db.set_config("hl_exec_mode", "live")
        self.assertIn("执行器：live", self.render())

    def test_exec_mode_unknown_when_unset(self):
        self.assertNotIn("执行器：off", self.render())
        self.assertIn("执行器：未知", self.render())

    def test_feed_ok(self):
        self.db.mark_feed("BTC_USDT", "mexc", True)
        self.assertIn("信号源：正常", self.render())

    def test_feed_error_after_ok(self):
        self.db.mark_feed("BTC_USDT", "mexc", True)
        self.db.mark_feed("ETH_USDT", "mexc", False, "timeout")
        self.assertIn("信号源：异常（ETH）", self.render())

    def test_feed_unknown_without_rows(self):
        self.assertIn("信号源：未知", self.render())


class TestLifetimeSection(WeeklyFixture):
    def test_wins_losses_total_includes_old_weeks(self):
        add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=30.0)
        add_position(self.db, status="closed", r_multiple=3.5, close_days_ago=20.0)
        add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=1.0)
        text = self.render()
        self.assertIn("共 3 笔：胜 1 · 负 2，合计 +1.50R", text)
        # 本周只算窗口内的那一笔
        self.assertIn("平仓 1 笔", text)

    def test_negative_total_uses_typographic_minus(self):
        add_position(self.db, status="closed", r_multiple=-2.0, close_days_ago=1.0)
        self.assertIn(f"合计 {MINUS}2.00R", self.render())

    def test_launch_date_is_pinned(self):
        self.assertEqual(engine.WEEKLY_LAUNCH_DATE, dt.date(2026, 10, 12))
        self.assertIn("（10-12 起）", self.render())


class TestSymbolDisplay(WeeklyFixture):
    def test_usdt_suffix_stripped(self):
        add_position(self.db, symbol="SOL_USDT", side=1, status="closed",
                     r_multiple=1.0, close_days_ago=1.0)
        text = self.render()
        self.assertIn("SOL 做多", text)
        self.assertNotIn("SOL_USDT", text)

    def test_hundreds_beyond_twenty(self):
        for i in range(21):
            add_position(self.db, status="closed", r_multiple=-1.0, close_days_ago=1.0 + i * 0.01)
        text = self.render()
        self.assertIn("⑳", text)
        self.assertIn("21. ", text)


if __name__ == "__main__":
    unittest.main()
