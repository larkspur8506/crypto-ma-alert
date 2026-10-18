"""「信号」表迁到记录页后的渲染回归（2026-10-18）。

背景：总览页的「下单记录 / 最近成交 / 信号」三张历史表与记录页重复 ——
前两张记录页本来就有，信号则是本次抽成 `hl/views._signals_html` 后**新增**到记录页
（放在「成交」之后，与总览原顺序一致）。JSON payload 字段（signals / fills /
orders_recent）一个都不能动：/api/hl/status 与看守脚本还在读。

验收点：
- `_signals_html` 正常行 / 空表 / non-list 兜底 / 多空方向 / 状态映射（open=pos, pending=warn）
- 数量走 _fmt_sz（HL 仓 0.00084 不能显示成 0.00）
- 信号 bar 时间是北京时间口径
- /api/hl/status 仍含 signals / fills / orders_recent（数据流未动）
- 记录页确实渲染了信号表（入口接线没断）
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_admin_ui import BaseCase, NOW                     # noqa: E402

BAR = 1_700_000_000_000


def _sig(**kw):
    """一条 payload["signals"] 行（字段与 hl/views.status_payload 一致）。"""
    row = {"symbol": "BTC_USDT", "coin": "BTC", "status": "open", "side": 1,
           "entry": 100_000.0, "stop": 97_000.0, "contracts": 6,
           "qty": 0.00084, "qty_unit": "币量", "bar_ms": BAR}
    row.update(kw)
    return row


def _seg(html: str) -> str:
    """取「信号」块正文（到下一个 <h2> 或卡片边界为止）。"""
    assert "<h2>信号</h2>" in html, "信号块不存在"
    return html.split("<h2>信号</h2>", 1)[1].split("<h2>", 1)[0]


class TestSignalsHtmlPure(unittest.TestCase):
    """`_signals_html` 纯渲染（不碰 DB / HTTP）。"""

    def setUp(self):
        from app.hl import views
        self.views = views

    def test_normal_row_cells(self):
        seg = _seg(self.views._signals_html([_sig()]))
        for head in ("<th>标的</th>", "<th>状态</th>", "<th>方向</th>",
                     "<th class='num'>入场</th>", "<th class='num'>止损</th>",
                     "<th class='num'>数量</th>", "<th>信号 bar</th>"):
            self.assertIn(head, seg, f"表头缺 {head}")
        self.assertIn(">BTC<", seg)
        self.assertIn(">持仓中<", seg)                 # status=open → _ST_TXT
        self.assertIn(">多<", seg)
        self.assertIn(">100,000.00<", seg)             # 入场
        self.assertIn(">97,000.00<", seg)              # 止损
        self.assertIn(">0.00084<", seg)                # 数量（_fmt_sz 精度）
        # 信号 bar：北京时间（UTC+8）—— BAR=1_700_000_000_000 即 11-15 06:13:20
        self.assertIn(">11-15 06:13:20<", seg)
        self.assertNotIn("暂无信号", seg)
        # 一行 7 格（表头 7 列，别再来 8 列表头配 7 格数据那种错位）
        cells = seg.split("<tbody>", 1)[1].count("<td")
        self.assertEqual(cells, 7, "数据行必须 7 格")

    def test_empty_renders_placeholder(self):
        seg = _seg(self.views._signals_html([]))
        self.assertIn("暂无信号", seg)
        self.assertIn("colspan='7'", seg)
        self.assertNotIn("<td>BTC", seg)

    def test_none_and_non_list_do_not_raise(self):
        for bad in (None, 0, "", "oops", {}):
            seg = _seg(self.views._signals_html(bad))
            self.assertIn("暂无信号", seg, f"{bad!r} 应降级成空表")

    def test_dirty_rows_do_not_raise(self):
        # 行不是 dict → _as_dict 兜底成 {}，渲染出「—」而不是抛异常
        seg = _seg(self.views._signals_html([None, 5, _sig()]))
        self.assertIn(">—<", seg)
        self.assertIn(">BTC<", seg)

    def test_side_direction(self):
        long_seg = _seg(self.views._signals_html([_sig(side=1)]))
        short_seg = _seg(self.views._signals_html([_sig(side=-1)]))
        self.assertIn(">多<", long_seg)
        self.assertNotIn(">空<", long_seg)
        self.assertIn(">空<", short_seg)
        self.assertNotIn(">多<", short_seg)
        # side 缺失/脏数据 → 默认按多处理（与 payload 侧 int(_num(side, 1)) 同口径）
        self.assertIn(">多<", _seg(self.views._signals_html([_sig(side=None)])))

    def test_status_classes(self):
        open_seg = _seg(self.views._signals_html([_sig(status="open")]))
        self.assertIn("<span class='pos'>持仓中</span>", open_seg)
        pending_seg = _seg(self.views._signals_html([_sig(status="pending")]))
        self.assertIn("<span class='warn'>待确认</span>", pending_seg)
        closed_seg = _seg(self.views._signals_html([_sig(status="closed")]))
        self.assertIn("<span class=''>已平仓</span>", closed_seg)   # 无着色
        # 未知状态：原样显示，不吞
        weird = _seg(self.views._signals_html([_sig(status="brand_new")]))
        self.assertIn("brand_new", weird)

    def test_coin_escaped_and_falls_back_to_symbol(self):
        seg = _seg(self.views._signals_html([_sig(coin="A&B", symbol="A&B_USDT")]))
        self.assertIn("A&amp;B", seg)
        self.assertNotIn("A&B<", seg)
        # coin 缺失 → 用 symbol 的展示名（去掉 _USDT）
        seg2 = _seg(self.views._signals_html([_sig(coin=None, symbol="ETH_USDT")]))
        self.assertIn(">ETH<", seg2)
        self.assertNotIn("ETH_USDT", seg2)


class TestSignalsJsonApi(BaseCase):
    """/api/hl/status 的字段回归：搬渲染不搬数据流。"""

    def _position(self):
        return self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=BAR, status="open", side=1,
            entry_price=100_000.0, stop_price=97_000.0, stop_dist=0.03, contracts=6,
            coin_qty=0.0006, notional=60.0, risk_u=18.0, equity_at_entry=1000.0)

    def test_status_json_keeps_signals_fills_orders_recent(self):
        self._position()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, "entry", 1, 0.005, px=100_000.0)
        self.db.hl_upsert_fill(tid="t1", symbol="BTC_USDT", coin="BTC", side=1, sz=0.005,
                               px=100_000.0, fee=0.05, fee_token="USDT", dir="Open Long",
                               closed_pnl=0.0, hash="t1", reduce_only=False,
                               ts=int(NOW * 1000), created_at=NOW)
        p = self.client.get("/api/hl/status").json()
        for k in ("signals", "fills", "orders_recent"):
            self.assertIn(k, p, f"/api/hl/status 缺字段 {k}")
            self.assertIsInstance(p[k], list, k)
        self.assertEqual(len(p["signals"]), 1)
        self.assertEqual(p["signals"][0]["coin"], "BTC")
        self.assertEqual(p["signals"][0]["symbol"], "BTC_USDT")   # 内部键保留
        self.assertEqual(len(p["fills"]), 1)
        self.assertGreaterEqual(len(p["orders_recent"]), 1)


class TestSignalsOnRecordsTab(BaseCase):
    """记录 tab 的接线：payload["signals"] 真的传进去了、块真的渲染了。"""

    def test_records_renders_payload_signals(self):
        # HL 仓（hl_status 非空）才读 actual_* 的币量口径：数量列 0.00084 而不是张数 6.00
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=BAR, status="open",
                                      side=1, entry_price=100_000.0, stop_price=97_000.0,
                                      stop_dist=0.03, contracts=6, coin_qty=0.0006,
                                      notional=60.0, risk_u=18.0, equity_at_entry=1000.0)
        self.db.set_pos_hl(pid, hl_status="open", hl_stop_oid=7, hl_sz=0.00084)
        self.db.confirm_position(pid, 100_000.0, 0.00084, 97_000.0, True)
        html = self.body(tab="records")
        seg = _seg(html)
        self.assertIn(">持仓中<", seg)
        self.assertIn(">0.00084<", seg)

    def test_overview_no_longer_renders_signals(self):
        self.db.create_position(symbol="BTC_USDT", signal_bar_ms=BAR, status="open", side=1,
                                entry_price=100_000.0, stop_price=97_000.0, stop_dist=0.03,
                                contracts=6, coin_qty=0.0006, notional=60.0,
                                risk_u=18.0, equity_at_entry=1000.0)
        html = self.body(tab="overview")
        self.assertNotIn("<h2>信号</h2>", html)
        self.assertNotIn(">持仓中<", html)          # 信号行数据也不该漏到总览


if __name__ == "__main__":
    unittest.main(verbosity=2)
