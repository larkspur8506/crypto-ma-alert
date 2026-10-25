"""数据源告警「持续未恢复才推」（feat/feed-alert-defer）回归测试。

行为（规格 2026-10-25）：
- 首次失败：落库 kind=data_error_pending、**不推送**（message 带观察后缀）；
- 恢复：pending 行就地标记「已于 HH:MM 恢复（未推送）」，永不推送；
- 持续未恢复 ≥ feed_alert_delay_s（默认 1800，0=旧行为立即推）：该行转正为
  data_error 并**恰好推送一次**（同一故障事件只推一次）。

集成点回归：24h 补推（_retry_failed_alerts）必须排除 pending 行——pending 是
「故意不发」，不是发送失败。参数页「执行与推送」组新增可调字段（非法值拒收、
保存回读生效），/admin 与 /hl 双入口的记录页用同一份 _ALERT_KIND_TXT 映射。

测试断言看渲染 HTML 词面 / alert_logs 行字段；零网络（fetch 一律打桩）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market                                        # noqa: E402
from app.config import Config                                         # noqa: E402
from app.db import DB                                                 # noqa: E402
from app.market import FeedError                                      # noqa: E402
from tests.test_admin_ui import BaseCase, PARAMS_OK, TOKEN            # noqa: E402
from tests.test_strategy import (trend_then_flat, flat_bar)           # noqa: E402

SYM = "BTC_USDT"


class _Notifier:
    def __init__(self, ok=True):
        self.msgs = []
        self.ok = ok

    def send_message(self, m):
        self.msgs.append(m)
        return self.ok


def _cfg(delay: float = 1800.0) -> Config:
    c = Config()
    c.dry_run = False
    c.alert_prefix = ""
    c.symbols = [SYM]
    c.feed_alert_delay_s = delay
    return c


def _boom(symbol, now_ms=None, allow_fallback=False):
    """数据源失败桩：任何取数调用都抛 FeedError（fail-closed 主路径）。"""
    raise FeedError("模拟主源失败")


def _ok_feed(symbol, now_ms=None, allow_fallback=False):
    """恢复桩：横盘收尾的合法 bar 序列（不触发入场信号，避免污染推送断言）。"""
    base = trend_then_flat()
    bars = base + [flat_bar(base[-1], base[-1].close)]
    return market.Feed(symbol=symbol, source="hl-candle", bars=bars,
                       fetched_at_ms=now_ms or int(time.time() * 1000))


def _pending_rows(db):
    """当前「观察中」的 pending 行（已恢复/已转正的不算——恢复行 sent=1 已了结）。"""
    return [r for r in db.recent_alerts(200)
            if r["kind"] == "data_error_pending" and not r["sent_successfully"]]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def _tick(self, fetch, notifier, cfg=None):
        cfg = cfg or _cfg()
        with mock.patch.object(market, "fetch", fetch):
            return engine.tick(self.db, cfg, notifier, now_ms=int(time.time() * 1000))


class TestPendingWindow(_Base):
    """清单 1–4：观察期生命周期（落库不推 → 转正恰好一次 / 恢复标记）。"""

    def test_1_first_failure_pends_without_push(self):
        n = _Notifier()
        self._tick(_boom, n)
        self.assertEqual(n.msgs, [], "首次失败只观察，不推送")
        rows = _pending_rows(self.db)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["symbol"], SYM)
        self.assertEqual(r["sent_successfully"], 0)
        self.assertIn("数据源异常", r["message"])
        self.assertIn("模拟主源失败", r["message"])
        self.assertIn("持续未恢复超过 30 分钟将提醒", r["message"])
        self.assertIn("若恢复则本条不推送", r["message"])
        # 没有任何已推送的 data_error
        self.assertEqual([x for x in self.db.recent_alerts(200)
                          if x["kind"] == "data_error" and x["sent_successfully"]], [])

    def test_2_within_window_still_no_push_no_duplicate(self):
        n1 = _Notifier()
        self._tick(_boom, n1)
        time.sleep(0.02)
        n2 = _Notifier()
        self._tick(_boom, n2)                    # ~观察期内再失败（模拟 5 分钟后）
        self.assertEqual(n1.msgs + n2.msgs, [], "观察期内不推送")
        rows = _pending_rows(self.db)
        self.assertEqual(len(rows), 1, "同一故障不重复落 pending 行")
        self.assertEqual(len(self.db.recent_alerts(200)), 1)

    def test_3_threshold_promotes_exactly_one_push(self):
        self._tick(_boom, _Notifier())
        # 把 pending 行的 created_at 回拨到阈值之前（等效「30 分钟仍失败」）
        self.db.conn.execute(
            "UPDATE alert_logs SET created_at=? WHERE kind='data_error_pending'",
            (time.time() - 1801,))
        self.db.conn.commit()
        n = _Notifier()
        sent = self._tick(_boom, n)
        self.assertEqual(len(n.msgs), 1, "到点转正，恰好推送一次")
        self.assertEqual(len(sent), 1)
        rows = self.db.recent_alerts(200)
        self.assertEqual([r["kind"] for r in rows], ["data_error"], "kind 已转正")
        self.assertEqual(rows[0]["sent_successfully"], 1)
        self.assertNotIn("若恢复则本条不推送", rows[0]["message"],
                         "转正推送的文案剥掉观察后缀，与旧 data_error 一致")
        self.assertIn("数据源异常", rows[0]["message"])
        # 故障仍在继续：不再另开 pending、不再推送（同一故障事件只推一次）
        n2 = _Notifier()
        self._tick(_boom, n2)
        self.assertEqual(n2.msgs, [])
        self.assertEqual(_pending_rows(self.db), [])

    def test_4_recovery_marks_pending_new_window_on_refail(self):
        self._tick(_boom, _Notifier())
        old = _pending_rows(self.db)[0]
        # feed 恢复成功（真实成功路径：tick → mark_feed ok → 就地标记）
        n = _Notifier()
        self._tick(_ok_feed, n)
        self.assertEqual(n.msgs, [], "恢复不推送")
        self.db.conn.commit()
        cur = self.db.conn.execute(
            "SELECT * FROM alert_logs WHERE id=?", (old["id"],)).fetchone()
        self.assertEqual(cur["kind"], "data_error_pending")
        self.assertEqual(cur["sent_successfully"], 1, "恢复行已了结，不再参与任何推送通道")
        self.assertIn("已于", cur["message"])
        self.assertIn("恢复（未推送）", cur["message"])
        # 恢复后再失败 = 新 pending 行（旧行不被复用）
        self._tick(_boom, _Notifier())
        rows = _pending_rows(self.db)
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["id"], old["id"], "新窗口开新行")
        self.assertIn("已于", self.db.conn.execute(
            "SELECT message FROM alert_logs WHERE id=?", (old["id"],)).fetchone()[0],
            "旧行的恢复标记保持原样")


class TestZeroDelayOldBehavior(_Base):
    """清单 5：feed_alert_delay_s=0 = 完全旧行为（首次失败立即推）。"""

    def test_zero_delay_pushes_immediately(self):
        n = _Notifier()
        self._tick(_boom, n, cfg=_cfg(delay=0))
        self.assertEqual(len(n.msgs), 1)
        kinds = [r["kind"] for r in self.db.recent_alerts(200)]
        self.assertEqual(kinds, ["data_error"], "不落 pending 行，直接走 data_error")
        self.assertNotIn("若恢复则本条不推送", n.msgs[0])


class TestThresholdValidation(BaseCase):
    """清单 6：阈值非法值拒收；面板参数页保存回读生效（走真实 POST 路由）。"""

    def test_apply_db_rejects_out_of_range(self):
        cfg = _cfg()
        cfg.apply_db({"feed_alert_delay_s": "7201"})
        self.assertEqual(cfg.feed_alert_delay_s, 1800.0, "超出 0–7200 拒收，保留原值")
        cfg.apply_db({"feed_alert_delay_s": "abc"})
        self.assertEqual(cfg.feed_alert_delay_s, 1800.0)
        cfg.apply_db({"feed_alert_delay_s": "600"})
        self.assertEqual(cfg.feed_alert_delay_s, 600.0, "合法值生效")
        cfg.apply_db({"feed_alert_delay_s": "0"})
        self.assertEqual(cfg.feed_alert_delay_s, 0.0, "显式 0（旧行为）合法")

    def test_panel_save_and_roundtrip(self):
        # 非法值 → 400 且不写库
        r = self.post("/admin/params", **self.params_form(feed_alert_delay_s="7201"))
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(self.db.conn.execute(
            "SELECT value FROM configuration WHERE key='feed_alert_delay_s'"
        ).fetchone())
        # 合法值 → 303 重定向，DB 落键，Config 装配读回生效
        r = self.post("/admin/params", **self.params_form(feed_alert_delay_s="600"))
        self.assertEqual(r.status_code, 303)
        cfg = Config().apply_db(self.db.get_config())
        self.assertEqual(cfg.feed_alert_delay_s, 600.0)
        # 显式 0 = 立即推，同样可保存
        r = self.post("/admin/params", **self.params_form(feed_alert_delay_s="0"))
        self.assertEqual(r.status_code, 303)
        cfg = Config().apply_db(self.db.get_config())
        self.assertEqual(cfg.feed_alert_delay_s, 0.0)


class TestRetryExcludesPending(_Base):
    """清单 7：24h 补推排除 pending 行（故意不发 ≠ 发送失败）。

    走生产函数 app.main._retry_failed_alerts，替换模块级 db/notifier；
    用一条真正发送失败的 data_error 行做对照，防止断言「 vacuously 通过」。
    """

    def test_retry_skips_pending_but_retries_data_error(self):
        from app import main as cma_main
        self.db.log_alert(SYM, None, "data_error_pending", "观察中的行（不该补推）", False)
        self.db.log_alert(SYM, 1000, "data_error", "真的发送失败了（该补推）", False)
        n = _Notifier()
        with mock.patch.object(cma_main, "db", self.db), \
                mock.patch.object(cma_main, "notifier", n):
            cma_main._retry_failed_alerts()
        self.assertEqual([m for m in n.msgs if "观察中的行" in m], [],
                         "pending 行不得被补推")
        self.assertEqual([m for m in n.msgs if "真的发送失败" in m],
                         ["真的发送失败了（该补推）"], "对照组：发送失败行照常补推")
        pend = self.db.conn.execute(
            "SELECT sent_successfully FROM alert_logs WHERE kind='data_error_pending'"
        ).fetchone()[0]
        self.assertEqual(pend, 0, "pending 行保持未推送状态")


class TestParamsPageRendering(BaseCase):
    """清单 8 + 记录页映射：新字段词面/白话说明可见；kind 中文映射双入口生效。"""

    def test_params_tab_renders_new_field(self):
        html = self.body("/admin", tab="params")
        self.assertIn("数据源异常提醒延迟（秒，0=立即）", html)
        self.assertIn("持续未恢复超过这么久才推送", html)
        self.assertIn("滤掉交易所例行维护这类分钟级抖动", html)
        # /hl 旧地址兼容入口仍在（302 → /admin），双入口同一份面板
        r = self.client.get("/hl", params={"k": TOKEN}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["location"].startswith("/admin"))

    def test_records_show_pending_kind_in_chinese(self):
        # 主记录页 + /admin/list/alerts 子页都用 _ALERT_KIND_TXT（同一份映射）
        self.db.log_alert(SYM, None, "data_error_pending",
                          "BTC_USDT: 模拟主源失败\n（持续未恢复超过 30 分钟将提醒）", False)
        rec = self.body("/admin", tab="records")
        self.assertIn("数据源异常（观察中）", rec)
        sub = self.body("/admin/list/alerts")
        self.assertIn("数据源异常（观察中）", sub)
        # 旧行为兜底照旧：未知 kind 回落英文原文
        self.db.log_alert(SYM, None, "some_new_kind", "x", False)
        self.assertIn("some_new_kind", self.body("/admin/list/alerts"))


if __name__ == "__main__":
    unittest.main()
