"""API 钱包（agent）有效期监控 + 到期前自动清仓的测试。

覆盖：
- extraAgents 解析（多 agent / 无 agent / validUntil=null / name 不匹配）
- 剩余天数按自然日（北京时间）+ 动作档位边界 15/14/7/4/3/2/0
- 告警 24h 去重；开关关闭时 ≤3 天不清仓（仍告警）
- 清仓：有仓平仓 / 空仓 / 失败重试 2 次 / kill 先置位 / dry 不下真单 / 复查仍有仓算失败
- 清理状态落库 hl_agent_state + 面板只读它（渲染零网络调用）
- 新配置键四处同步（Config 默认值 / OVERRIDABLE / 面板表单 / 解析）
- 清仓 job 与 tick 互斥（持 _tick_lock）

只依赖相对路径与临时 DB；不 import app.main（模块级建 DB）。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB
from app.hl import MODE_DRY, MODE_LIVE, agent as A
from app.hl.executor import ExecParams

ROOT = Path(__file__).resolve().parent.parent

# 北京时间 2026-10-18 12:00（= 04:00 UTC）：取正午，避开「自然日」边界抖动
NOW_MS = 1_792_296_000_000
DAY_MS = 86_400_000


def until_in_days(n: int, now_ms: int = NOW_MS) -> int:
    return int(now_ms + n * DAY_MS)


def _agents(*rows):
    return list(rows)


def _agent(name="Hermes", address="0xc1ee4519ce893070c83cf90b324c6c8e5484be22", until=None):
    return {"name": name, "address": address, "validUntil": until}


class FakeInfo:
    """HlInfo 替身：只实现到期检查与清仓用到的那几个方法。"""

    def __init__(self, snap=None, agents=None, raise_agents=None, raise_snapshot=None,
                 specs=None):
        self._snap = snap or {"positions": [], "open_orders": []}
        self._agents = agents if agents is not None else []
        self.raise_agents = raise_agents
        self.raise_snapshot = raise_snapshot
        self._specs = specs or {"BTC": {"sz_decimals": 5, "max_leverage": 40},
                                "ETH": {"sz_decimals": 4, "max_leverage": 25}}
        self.snapshot_calls = 0
        self.extra_agents_calls = 0

    def extra_agents(self, address):
        self.extra_agents_calls += 1
        if self.raise_agents:
            from app.hl.info import HlInfoError
            raise HlInfoError(self.raise_agents)
        return list(self._agents)

    def snapshot(self, address, mids=None):
        self.snapshot_calls += 1
        if self.raise_snapshot:
            from app.hl.info import HlInfoError
            raise HlInfoError(self.raise_snapshot)
        return dict(self._snap)

    def specs(self, coins=None):
        return {k: v for k, v in self._specs.items()
                if coins is None or k in coins}


def _pos(coin="BTC", size=0.01, side=1, mark=83000.0):
    return {"coin": coin, "side": side, "size": size, "entry_px": mark - 100,
            "mark_px": mark, "position_value": size * mark, "liq_px": 0.0,
            "leverage": 5, "isolated": True, "margin_used": size * mark / 5,
            "unrealized_pnl": 0.0, "roe": 0.0, "funding_since_open": 0.0}


class FakeExchange:
    """写路径替身：记录调用，可按需让撤单/平仓失败。

    `info` 指到 FakeInfo 时，成功平仓会把这枚币从快照里摘掉 —— 用于验证「复查无仓」；
    不指（为 None）就表示「平了但交易所还有仓」（复查必须判失败）。
    """

    def __init__(self, cancel_ok=True, exit_results=None, info=None):
        self.calls = []
        self.cancel_ok = cancel_ok
        self.exit_results = list(exit_results or [])
        self.info = info

    def mid(self, coin):
        return 83000.0 if coin == "BTC" else 2500.0

    def slippage_limit(self, coin, is_buy, mid=None):
        px = mid if mid else self.mid(coin)
        return px * (1 + 0.003) if is_buy else px * (1 - 0.003)

    def cancel_all_orders(self):
        self.calls.append(("cancel_all",))
        if not self.cancel_ok:
            return {"ok": False, "error": "撤单全部失败：签名错误", "raw": {"oids": [], "failed": []}}
        return {"ok": True, "status": "resting", "raw": {"oids": [11, 12], "failed": []},
                "error": None}

    def place_order(self, coin, is_buy, sz, limit_px, reduce_only=False, tif="Ioc",
                    trigger_px=None, cloid=None):
        self.calls.append(("order", coin, is_buy, sz, limit_px, reduce_only))
        if self.exit_results:
            res = self.exit_results.pop(0)
        else:
            res = {"ok": True, "status": "filled", "oid": 7, "avg_px": limit_px,
                   "filled_sz": sz, "error": None, "raw": {}}
        if res.get("ok") and str(res.get("status")) == "filled" and self.info is not None:
            # 真实交易所平掉后仓位就没了：替身同步摘掉，供「复查」用
            self.info._snap["positions"] = [p for p in self.info._snap.get("positions", [])
                                            if p.get("coin") != coin]
        return res


class SilentNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


class Base(unittest.TestCase):
    mode = MODE_LIVE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = SilentNotifier()
        self.params = ExecParams(mode=self.mode, account_address="0xMAIN", private_key="0xkey",
                                 equity=1000.0, equity_min=20.0, risk_pct=0.02, leverage=5.0,
                                 code_rev="t")

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return A.read_state(self.db)


# ---------------------------------------------------------------- 解析
class TestAgentParsing(unittest.TestCase):
    def test_pick_by_exact_name_among_many(self):
        raw = _agents(_agent("gridbot", "0xaaa", until_in_days(3)),
                      _agent("Hermes", "0xbbb", until_in_days(100)),
                      _agent("other", "0xccc", until_in_days(1)))
        row = A.pick_agent(A.norm_agents(raw), "Hermes")
        self.assertIsNotNone(row)
        self.assertEqual(row["address"], "0xbbb")
        self.assertEqual(row["valid_until"], until_in_days(100))

    def test_pick_datai_no_agent_returns_none(self):
        self.assertIsNone(A.pick_agent(A.norm_agents([]), "Hermes"))
        self.assertIsNone(A.pick_agent(None, "Hermes"))

    def test_pick_name_mismatch_returns_none(self):
        """名字必须精确相等：Hermes2 / hermes 都不算（同名授权是硬要求）。"""
        for nm in ("Hermes2", "hermes", "Hermes ", ""):
            self.assertIsNone(A.pick_agent(A.norm_agents([_agent(nm, "0x1", 1)]), "Hermes"), nm)

    def test_valid_until_null_means_no_expiry(self):
        rows = A.norm_agents([_agent(until=None)])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["valid_until"])
        self.assertIsNone(A.days_left(rows[0]["valid_until"]))

    def test_zero_and_garbage_valid_until(self):
        rows = A.norm_agents([{"name": "Hermes", "address": "0x1", "validUntil": 0},
                              {"name": "H2", "address": "0x2", "validUntil": "abc"},
                              "not-a-dict", None])
        self.assertIsNone(rows[0]["valid_until"])
        self.assertIsNone(rows[1]["valid_until"])
        self.assertEqual(len(rows), 2, "非 dict 的成员必须被丢掉")

    def test_non_list_payload_is_empty(self):
        """接口返回错误对象（限流/结构变化）时当空列表，不能拿脏数据算天数。"""
        self.assertEqual(A.norm_agents({"error": "rate limited"}), [])
        self.assertEqual(A.norm_agents("nope"), [])


# ---------------------------------------------------------------- 天数 / 档位
class TestDaysAndLevels(unittest.TestCase):
    def test_natural_day_not_hours(self):
        """用北京时间口径的「日期差」而不是「小时数 ÷ 24」。"""
        now = 1_792_340_000_000
        valid = 1_792_400_000_000
        d = A.days_left(valid, now)
        import datetime as dt
        tz = dt.timezone(dt.timedelta(hours=8))
        want = (dt.datetime.fromtimestamp(valid / 1000, tz).date()
                - dt.datetime.fromtimestamp(now / 1000, tz).date()).days
        self.assertEqual(d, want)
        # 与「向上取整的小时数」不同：北京时间 23:00 到期、当天 01:00 看 = 0 天（今天到期）
        tz_now = int(dt.datetime(2026, 10, 18, 1, 0, tzinfo=tz).timestamp() * 1000)
        tz_exp = int(dt.datetime(2026, 10, 18, 23, 0, tzinfo=tz).timestamp() * 1000)
        self.assertEqual(A.days_left(tz_exp, tz_now), 0)
        self.assertEqual(A.level(A.days_left(tz_exp, tz_now)), "expired")

    def test_boundaries(self):
        cases = {15: "ok", 14: "near", 13: "near", 8: "near", 7: "urgent",
                 4: "urgent", 3: "cleanup", 2: "cleanup", 1: "cleanup",
                 0: "expired", -1: "expired"}
        for days, want in cases.items():
            self.assertEqual(A.level(days), want, f"{days} 天应为 {want}")
        self.assertEqual(A.level(None), "unknown")
    def test_days_left_from_ms(self):
        self.assertEqual(A.days_left(until_in_days(3), NOW_MS), 3)
        self.assertEqual(A.days_left(until_in_days(0), NOW_MS), 0)
        self.assertIsNone(A.days_left(None, NOW_MS))

    def test_level_text_map_covers_all(self):
        for lvl in ("unknown", "ok", "near", "urgent", "cleanup", "expired"):
            self.assertIn(lvl, A.LEVEL_TXT)
            self.assertTrue(A.LEVEL_TXT[lvl])

    def test_fmt_bj(self):
        # 2027-04-12 04:57 UTC == 2027-04-12 12:57 北京时间（与线上 agent 实况一致）
        self.assertEqual(A.fmt_bj(1_807_505_820_000), "2027-04-12 12:57")
        self.assertEqual(A.fmt_bj(None), "—")
        self.assertEqual(A.fmt_bj(0), "—")

    def test_mask_addr(self):
        self.assertEqual(A.mask_addr("0xc1ee4519ce893070c83cf90b324c6c8e5484be22"),
                         "0xc1ee…be22")
        self.assertEqual(A.mask_addr(""), "—")
        self.assertEqual(A.mask_addr("0x1234"), "0x1234")


# ---------------------------------------------------------------- 配置
class TestAgentConfig(unittest.TestCase):
    def test_defaults_and_overridable(self):
        from app.config import Config
        cfg = Config()
        self.assertTrue(cfg.agent_expiry_cleanup, "默认必须开")
        self.assertEqual(cfg.hl_agent_valid_until_ms, "")
        for key in ("agent_expiry_cleanup", "hl_agent_valid_until_ms"):
            self.assertIn(key, Config.OVERRIDABLE, f"{key} 必须在 OVERRIDABLE 里（四处同步）")

    def test_db_overrides_bool_and_str(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = DB(str(Path(tmp.name) / "c.db"))
            db.set_config("agent_expiry_cleanup", 0)
            db.set_config("hl_agent_valid_until_ms", "1830000000000")
            cfg = __import__("app.config", fromlist=["Config"]).Config().apply_db(db.get_config())
            self.assertFalse(cfg.agent_expiry_cleanup)
            self.assertEqual(cfg.hl_agent_valid_until_ms, "1830000000000")
            # 显式清空要能落下去（否则手工兜底值清不掉）
            db.set_config("hl_agent_valid_until_ms", "")
            cfg2 = __import__("app.config", fromlist=["Config"]).Config().apply_db(db.get_config())
            self.assertEqual(cfg2.hl_agent_valid_until_ms, "")
        finally:
            tmp.cleanup()

    def test_cleanup_switch_unrecognized_means_on(self):
        for v in ("", None, "weird", "on", "1", True, "yes"):
            self.assertTrue(A.cleanup_enabled(v), f"{v!r} 应判为「开」")
        for v in ("0", "false", "no", "off", False, 0):
            self.assertFalse(A.cleanup_enabled(v), f"{v!r} 应判为「关」")

    def test_manual_until_seconds_tolerated(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = DB(str(Path(tmp.name) / "m.db"))
            db.set_config("hl_agent_valid_until_ms", "1830000000")     # 秒
            self.assertEqual(A.manual_until(db), 1_830_000_000_000)
            db.set_config("hl_agent_valid_until_ms", "1830000000000")  # 毫秒
            self.assertEqual(A.manual_until(db), 1_830_000_000_000)
            db.set_config("hl_agent_valid_until_ms", "abc")
            self.assertIsNone(A.manual_until(db))
            db.set_config("hl_agent_valid_until_ms", "")
            self.assertIsNone(A.manual_until(db))
        finally:
            tmp.cleanup()


# ---------------------------------------------------------------- 检查 / 动作表
class TestCheckOnce(Base):
    def run_check(self, *, days=None, agents=None, raise_agents=None, cleanup=None,
                  cfg=None):
        info = FakeInfo(agents=agents if agents is not None else
                        ([_agent(until=until_in_days(days))] if days is not None else []),
                        raise_agents=raise_agents)
        from app.config import Config
        return A.check_once(self.db, cfg or Config(), self.notifier, info,
                            address="0xMAIN", now_ms=NOW_MS, cleanup=cleanup)

    def test_far_future_only_state_no_alert(self):
        st = self.run_check(days=30)
        self.assertEqual(st["level"], "ok")
        self.assertEqual(st["source"], "api")
        self.assertEqual(st["days_left"], 30)
        self.assertEqual(self.notifier.messages, [])
        self.assertEqual(self.state()["level"], "ok", "每轮都要写 state")
        self.assertEqual(self.state()["checked_ms"], NOW_MS)

    def test_near_alert_at_14_and_7(self):
        for days, want_lvl in ((14, "near"), (13, "near"), (8, "near"), (7, "urgent"), (4, "urgent")):
            self.notifier.messages.clear()
            self.db = DB(str(Path(self.tmp.name) / f"n{days}.db"))
            st = self.run_check(days=days)
            self.assertEqual(st["level"], want_lvl, f"{days} 天应为 {want_lvl}")
            self.assertEqual(len(self.notifier.messages), 1, f"{days} 天应告警一次")
            msg = self.notifier.messages[0]
            self.assertIn("API 有效期", msg)
            self.assertIn("北京时间", msg)
            kinds = [r["kind"] for r in self.db.recent_alerts(5)]
            self.assertIn("agent_expiry_alert", kinds)

    def test_alert_dedup_24h(self):
        self.run_check(days=5)
        self.assertEqual(len(self.notifier.messages), 1)
        self.run_check(days=5)          # 同一轮再跑：24h 内不重复推
        self.assertEqual(len(self.notifier.messages), 1, "24h 窗口内必须去重")

    def test_alert_dedup_not_marked_when_send_fails(self):
        class Broken:
            def send_message(self, text):
                return False
        info = FakeInfo(agents=[_agent(until=until_in_days(5))])
        from app.config import Config
        A.check_once(self.db, Config(), Broken(), info, address="0xMAIN", now_ms=NOW_MS)
        A.check_once(self.db, Config(), self.notifier, info, address="0xMAIN", now_ms=NOW_MS)
        self.assertEqual(len(self.notifier.messages), 1, "上次失败不该被当成已推送")

    def test_cleanup_at_3_days_sets_kill_then_flattens(self):
        seen = {}

        def cleanup(reason=""):
            seen["kill_at_call"] = self.db.hl_kill()
            seen["reason"] = reason
            return {"ok": True, "closed": [{"coin": "BTC", "sz": 0.01}], "dry": False}

        st = self.run_check(days=3, cleanup=cleanup)
        self.assertTrue(seen.get("kill_at_call"), "kill 必须在清仓之前就置位")
        self.assertIn("3", seen.get("reason", ""))
        self.assertEqual(st["cleanup"], "done")
        self.assertTrue(self.db.hl_kill())
        kinds = [r["kind"] for r in self.db.recent_alerts(5)]
        self.assertIn("agent_expiry_cleanup", kinds)
        self.assertTrue(any("已清仓" in m for m in self.notifier.messages))

    def test_cleanup_at_2_and_1_days(self):
        for days in (2, 1):
            self.db = DB(str(Path(self.tmp.name) / f"c{days}.db"))
            self.notifier.messages.clear()
            calls = []

            def cleanup(reason=""):
                calls.append(reason)
                return {"ok": True, "closed": [], "dry": False}

            st = self.run_check(days=days, cleanup=cleanup)
            self.assertEqual(st["level"], "cleanup")
            self.assertEqual(len(calls), 1, f"{days} 天应清仓")
            self.assertEqual(st["cleanup"], "done")

    def test_switch_off_no_cleanup_but_alert(self):
        self.db.set_config("agent_expiry_cleanup", 0)
        calls = []

        def cleanup(reason=""):
            calls.append(reason)
            return {"ok": True, "closed": [], "dry": False}

        st = self.run_check(days=2, cleanup=cleanup)
        self.assertEqual(calls, [], "开关关闭时绝不能清仓")
        self.assertFalse(self.db.hl_kill(), "开关关闭时也不该置 kill")
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("自动清仓", self.notifier.messages[0])
        self.assertIn("已关闭", self.notifier.messages[0])
        self.assertEqual(st["cleanup"], "none")

    def test_cleanup_failure_marks_failed_and_next_round_retries(self):
        results = [{"ok": False, "error": "撤单全部失败：签名错误", "closed": []},
                   {"ok": True, "closed": [{"coin": "ETH", "sz": 1.0}], "dry": False}]
        calls = []

        def cleanup(reason=""):
            calls.append(reason)
            return results.pop(0)

        st = self.run_check(days=3, cleanup=cleanup)
        self.assertEqual(st["cleanup"], "failed")
        self.assertTrue(any("自动清仓失败" in m for m in self.notifier.messages))
        # 下一轮（新的一天，绕开 24h 去重）重试并成功
        st2 = self.run_check_again(days=3, cleanup=cleanup, now_ms=NOW_MS + DAY_MS + 1)
        self.assertEqual(len(calls), 2, "失败后下一轮要重试")
        self.assertEqual(st2["cleanup"], "done")

    def run_check_again(self, **kw):
        now_ms = kw.pop("now_ms")
        info = FakeInfo(agents=[_agent(until=until_in_days(kw.pop("days"), now_ms))])
        from app.config import Config
        return A.check_once(self.db, Config(), self.notifier, info,
                            address="0xMAIN", now_ms=now_ms, cleanup=kw.get("cleanup"))

    def test_dry_mode_reports_but_not_done(self):
        st = self.run_check(days=2, cleanup=lambda reason="": {
            "ok": True, "closed": [{"coin": "BTC", "sz": 0.01, "dry": True}], "dry": True})
        self.assertEqual(st["cleanup"], "none", "dry 没真平仓就不能显示已清仓")
        self.assertTrue(any("影子模式" in m for m in self.notifier.messages))

    def test_cleanup_not_wired_shouts(self):
        st = self.run_check(days=2, cleanup=None)
        self.assertEqual(st["cleanup"], "failed")
        self.assertTrue(any("清仓" in m and "失败" in m for m in self.notifier.messages))

    def test_expired_sets_kill_and_alerts(self):
        st = self.run_check(days=0)
        self.assertEqual(st["level"], "expired")
        self.assertTrue(self.db.hl_kill())
        self.assertTrue(any("已失效" in m for m in self.notifier.messages))
        # 24h 内不重复
        self.run_check(days=0)
        self.assertEqual(len([m for m in self.notifier.messages if "已失效" in m]), 1)

    def test_expired_negative_days(self):
        st = self.run_check(days=-3)
        self.assertEqual(st["level"], "expired")
        self.assertTrue(self.db.hl_kill())

    def test_api_failure_falls_back_to_manual(self):
        self.db.set_config("hl_agent_valid_until_ms", str(until_in_days(5)))
        st = self.run_check(raise_agents="boom")
        self.assertEqual(st["source"], "manual")
        self.assertEqual(st["days_left"], 5)
        self.assertEqual(len(self.notifier.messages), 1, "手工兜底也要照常告警")

    def test_no_agent_falls_back_to_manual(self):
        self.db.set_config("hl_agent_valid_until_ms", str(until_in_days(2)))
        st = self.run_check(agents=[])
        self.assertEqual(st["source"], "manual")

    def test_unknown_when_nothing_available(self):
        st = self.run_check(agents=[])
        self.assertEqual(st["source"], "none")
        self.assertEqual(st["level"], "unknown")
        self.assertEqual(self.notifier.messages, [], "未知不告警")
        self.assertEqual(self.state()["source"], "none")

    def test_null_valid_until_is_unknown_level(self):
        st = self.run_check(agents=[_agent(until=None)])
        self.assertEqual(st["source"], "api")
        self.assertIsNone(st["days_left"])
        self.assertEqual(st["level"], "unknown")
        self.assertEqual(self.notifier.messages, [])

    def test_name_mismatch_falls_back(self):
        self.db.set_config("hl_agent_valid_until_ms", str(until_in_days(9)))
        st = self.run_check(agents=[_agent("gridbot", until=until_in_days(1))])
        self.assertEqual(st["source"], "manual", "名字不匹配不能拿别人的到期日")

    def test_state_keeps_address_and_no_key(self):
        st = self.run_check(days=30)
        self.assertEqual(st["address"], "0xc1ee4519ce893070c83cf90b324c6c8e5484be22")
        raw = self.db.get_config_str("hl_agent_state", "")
        self.assertNotIn("PRIVATE", raw.upper())
        self.assertNotIn("0xkey", raw)


# ---------------------------------------------------------------- 清仓
class TestFlattenAll(Base):
    def flatten(self, ex, info, params=None):
        return A.flatten_all(self.db, params or self.params, info, ex, self.notifier)

    def flatten_ok(self, positions, **kw):
        """常见场景：仓位平掉后快照同步清空（复查通过）。"""
        info = FakeInfo(snap={"positions": list(positions), "open_orders": []})
        ex = FakeExchange(info=info, **kw)
        return self.flatten(ex, info), ex, info

    def test_closes_each_position_reduce_only(self):
        res, ex, _info = self.flatten_ok([_pos("BTC", 0.01, 1), _pos("ETH", 2.0, -1)])
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(res["closed"]), 2)
        orders = [c for c in ex.calls if c[0] == "order"]
        self.assertEqual(len(orders), 2)
        # 平多 = 卖（is_buy False）；平空 = 买（is_buy True）
        self.assertFalse(orders[0][2])
        self.assertTrue(orders[1][2])
        self.assertTrue(all(c[5] for c in orders), "必须 reduce_only")
        self.assertTrue(any(c[0] == "cancel_all" for c in ex.calls), "先撤全部挂单")

    def test_cancel_before_order(self):
        res, ex, _info = self.flatten_ok([_pos()])
        self.assertTrue(res["ok"])
        kinds = [c[0] for c in ex.calls]
        self.assertEqual(kinds[0], "cancel_all")
        self.assertIn("order", kinds)

    def test_empty_positions_still_ok(self):
        res, ex, _info = self.flatten_ok([])
        self.assertTrue(res["ok"])
        self.assertEqual(res["closed"], [])
        self.assertEqual([c for c in ex.calls if c[0] == "order"], [])

    def test_cancel_failure_retries_twice_then_fails(self):
        info = FakeInfo(snap={"positions": [_pos()], "open_orders": []})
        ex = FakeExchange(cancel_ok=False)
        res = self.flatten(ex, info)
        self.assertFalse(res["ok"])
        self.assertEqual(res["attempts"], 3, "失败重试 2 次 → 共 3 轮")
        self.assertIn("撤单失败", res["error"])
        self.assertEqual([c for c in ex.calls if c[0] == "order"], [], "撤单没成功不许平仓")

    def test_order_failure_retries(self):
        info = FakeInfo(snap={"positions": [_pos()], "open_orders": []})
        fail = {"ok": False, "status": "error", "error": "reject", "oid": None,
                "avg_px": None, "filled_sz": None, "raw": None}
        ex = FakeExchange(exit_results=[fail, fail, fail], info=info)
        res = self.flatten(ex, info)
        self.assertFalse(res["ok"])
        self.assertEqual(res["attempts"], 3)
        self.assertIn("平仓失败", res["error"])

    def test_second_attempt_succeeds(self):
        fail = {"ok": False, "status": "error", "error": "reject", "oid": None,
                "avg_px": None, "filled_sz": None, "raw": None}
        res, ex, _info = self.flatten_ok([_pos()], exit_results=[fail])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(len(res["closed"]), 1)

    def test_post_check_still_open_is_failure(self):
        """平仓单回报 ok，但复查还有仓（成交没落/部分成交）→ 不能宣称已清仓。"""
        info = FakeInfo(snap={"positions": [_pos()], "open_orders": []})
        ex = FakeExchange()          # info=None：平了但交易所侧还有仓
        res = self.flatten(ex, info)
        self.assertFalse(res["ok"])
        self.assertIn("复查", res["error"])
        self.assertEqual(res["attempts"], 3)

    def test_snapshot_failure_is_failure(self):
        info = FakeInfo(raise_snapshot="down")
        res = self.flatten(FakeExchange(), info)
        self.assertFalse(res["ok"])
        self.assertIn("读取账户失败", res["error"])

    def test_dry_does_not_touch_exchange(self):
        params = ExecParams(mode=MODE_DRY, account_address="0xMAIN", private_key="0xkey")
        info = FakeInfo(snap={"positions": [_pos()], "open_orders": []})
        ex = FakeExchange()
        res = A.flatten_all(self.db, params, info, ex, self.notifier)
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry"])
        self.assertEqual(ex.calls, [], "dry 模式不许撤单/下单")
        self.assertTrue(res["closed"][0]["dry"])

    def test_no_position_side_handled(self):
        """side 缺失/字符串也要能算对平仓方向（不能抛异常）。"""
        p = _pos()
        p.pop("side")
        res, ex, _info = self.flatten_ok([p])
        self.assertTrue(res["ok"])
        self.assertFalse([c for c in ex.calls if c[0] == "order"][0][2])


# ---------------------------------------------------------------- 面板 / 装配
class TestPanelAndWiring(unittest.TestCase):
    def test_alert_kind_txt_has_new_kinds(self):
        from app.admin_ui import _ALERT_KIND_TXT
        for kind in ("agent_expiry_alert", "agent_expiry_cleanup", "agent_renew"):
            self.assertIn(kind, _ALERT_KIND_TXT, f"{kind} 必须进中文映射")

    def test_cleanup_job_holds_tick_lock(self):
        """清仓与 tick 互斥：清仓入口必须持进程内 _tick_lock（源码级断言，不 import main）。"""
        src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        m = re.search(r"def _run_agent_cleanup.*?(?=\ndef )", src, re.S)
        self.assertIsNotNone(m, "找不到 _run_agent_cleanup")
        body = m.group(0)
        self.assertIn("with _tick_lock", body)
        self.assertIn("flatten_for_expiry", body)

    def test_scheduler_registers_agent_expiry_job(self):
        src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('id="agent_expiry"', src)
        self.assertIn("CronTrigger(minute=11)", src)

    def test_hourly_job_calls_check(self):
        src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        m = re.search(r"def _job_agent_expiry.*?(?=\ndef )", src, re.S)
        self.assertIn("_agent_check()", m.group(0))

    def test_executor_flatten_uses_op_lock(self):
        import inspect
        from app.hl.executor import Executor
        body = inspect.getsource(Executor.flatten_for_expiry)
        self.assertIn("self._op_lock", body)
        self.assertIn("flatten_all", body)

    def test_params_page_has_cleanup_checkbox_and_manual_field(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:                                       # pragma: no cover
            self.skipTest("fastapi 未安装")
        from app.admin_ui import AdminDeps, build_admin_router
        from app.config import Config
        from app.hl.views import DashboardDeps

        tmp = tempfile.TemporaryDirectory()
        try:
            db = DB(str(Path(tmp.name) / "p.db"))
            cfg_fn = lambda: Config().apply_db(db.get_config())
            hl = DashboardDeps(db=db, config_fn=cfg_fn, info=None,
                               address_fn=lambda: "", mode_fn=lambda: "off",
                               base_url_fn=lambda: "https://api.hyperliquid.xyz")
            deps = AdminDeps(db=db, config_fn=cfg_fn, hl_deps=hl,
                             mode_fn=lambda: "off", token_fn=lambda: "tk")
            app = FastAPI()
            app.include_router(build_admin_router(deps))
            c = TestClient(app)
            html = c.get("/admin", params={"k": "tk", "tab": "params"}).text
            self.assertIn("agent_expiry_cleanup", html)
            self.assertIn("hl_agent_valid_until_ms", html)
            self.assertIn("到期前自动清仓", html)
            # 默认开 → 复选框必须有 checked
            self.assertRegex(html, r"name='agent_expiry_cleanup' value='1' checked")
            # 保存：不传 = 保持原值（老表单不会把它清成 0）
            r = c.post("/admin/params", data={
                "equity": "100", "risk_pct": "0.02", "leverage": "5",
                "stop_min": "0.015", "stop_max": "0.1", "short_regime_ma": "200",
                "equity_mode": "follow", "equity_min": "20", "k": "tk"},
                follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("1", str(db.get_config_str("agent_expiry_cleanup")))
            # 显式关
            r = c.post("/admin/params", data={
                "equity": "100", "risk_pct": "0.02", "leverage": "5",
                "stop_min": "0.015", "stop_max": "0.1", "short_regime_ma": "200",
                "equity_mode": "follow", "equity_min": "20",
                "agent_expiry_cleanup": "0", "k": "tk"}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("0", str(db.get_config_str("agent_expiry_cleanup")))
            html2 = c.get("/admin", params={"k": "tk", "tab": "params"}).text
            self.assertNotRegex(html2, r"name='agent_expiry_cleanup' value='1' checked")
        finally:
            tmp.cleanup()

    def test_params_rejects_bad_manual_until(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:                                       # pragma: no cover
            self.skipTest("fastapi 未安装")
        from app.admin_ui import AdminDeps, build_admin_router
        from app.config import Config
        from app.hl.views import DashboardDeps

        tmp = tempfile.TemporaryDirectory()
        try:
            db = DB(str(Path(tmp.name) / "q.db"))
            cfg_fn = lambda: Config().apply_db(db.get_config())
            hl = DashboardDeps(db=db, config_fn=cfg_fn, info=None,
                               address_fn=lambda: "", mode_fn=lambda: "off",
                               base_url_fn=lambda: "https://api.hyperliquid.xyz")
            deps = AdminDeps(db=db, config_fn=cfg_fn, hl_deps=hl,
                             mode_fn=lambda: "off", token_fn=lambda: "tk")
            app = FastAPI()
            app.include_router(build_admin_router(deps))
            c = TestClient(app)
            r = c.post("/admin/params", data={
                "equity": "100", "risk_pct": "0.02", "leverage": "5",
                "stop_min": "0.015", "stop_max": "0.1", "short_regime_ma": "200",
                "equity_mode": "follow", "equity_min": "20",
                "hl_agent_valid_until_ms": "not-a-number", "k": "tk"},
                follow_redirects=False)
            self.assertEqual(r.status_code, 400)
            self.assertIsNone(db.get_config_str("hl_agent_valid_until_ms") or None)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
