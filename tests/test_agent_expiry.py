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

    def test_pick_duplicate_names_prefers_live_max(self):
        """M6 回归：同名多行时不能取第一条 —— 取「有效行里 validUntil 最大」的。

        HL 允许同名 approve，历史行可能残留（旧行已过期 / 旧行有效期更短）。
        取第一条会把「已经续期成功」读成「快到期」，进而误清仓。
        """
        old = _agent("Hermes", "0xOLD", until_in_days(-30))     # 已过期
        new = _agent("Hermes", "0xNEW", until_in_days(170))     # 新授权的
        row = A.pick_agent(A.norm_agents([old, new]), "Hermes", NOW_MS)
        self.assertEqual(row["address"], "0xNEW")
        self.assertEqual(row["valid_until"], until_in_days(170))
        # 顺序反过来也一样
        row2 = A.pick_agent(A.norm_agents([new, old]), "Hermes", NOW_MS)
        self.assertEqual(row2["address"], "0xNEW")
        # 两行都有效 → 取更晚的那个
        a = _agent("Hermes", "0xA", until_in_days(20))
        b = _agent("Hermes", "0xB", until_in_days(100))
        self.assertEqual(A.pick_agent(A.norm_agents([a, b]), "Hermes", NOW_MS)["address"], "0xB")
        # 全都过期 → 取有效期最大的那条（交给调用方判 expired）
        p = _agent("Hermes", "0xP", until_in_days(-40))
        q = _agent("Hermes", "0xQ", until_in_days(-10))
        self.assertEqual(A.pick_agent(A.norm_agents([p, q]), "Hermes", NOW_MS)["address"], "0xQ")


# ---------------------------------------------------------------- 天数 / 档位
class TestDaysAndLevels(unittest.TestCase):
    def test_natural_day_not_hours(self):
        """`days_left` 仍是自然日（北京时间口径），但**只用于展示**。"""
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

    def test_expiry_day_is_cleanup_not_expired(self):
        """H1 回归：到期当天（还剩 11 小时）**必须**是 cleanup 档，不是 expired。

        以前的判据是 `days <= 0 → expired`（自然日差），于是到期日当天 00:00 起
        就不清仓、不重试，还推「已于今晚 23:00 过期」这种自相矛盾的文案。
        """
        import datetime as dt
        tz = dt.timezone(dt.timedelta(hours=8))
        noon = int(dt.datetime(2026, 10, 18, 12, 0, tzinfo=tz).timestamp() * 1000)
        tonight = int(dt.datetime(2026, 10, 18, 23, 0, tzinfo=tz).timestamp() * 1000)
        self.assertEqual(A.level_ms(tonight, noon), "cleanup",
                         "到期日当天中午 → 还剩 11 小时，必须走 cleanup（清仓/重试）")
        self.assertEqual(A.days_left_display(tonight, noon), 1, "展示口径：不到 1 天向上取整为 1")
        # 真过期（过了一秒）才 expired
        self.assertEqual(A.level_ms(tonight, tonight), "expired")
        self.assertEqual(A.level_ms(tonight, tonight + 1), "expired")
        self.assertEqual(A.level_ms(tonight, tonight - 1), "cleanup")

    def test_level_ms_boundaries(self):
        base = NOW_MS
        cases = {
            15: "ok", 14: "near", 13: "near", 8: "near", 7: "urgent",
            4: "urgent", 3: "cleanup", 2: "cleanup", 1: "cleanup",
        }
        for days, want in cases.items():
            self.assertEqual(A.level_ms(base + days * DAY_MS, base), want,
                             f"{days} 天应为 {want}")
        # 0/负 → expired（毫秒精确）
        self.assertEqual(A.level_ms(base, base), "expired")
        self.assertEqual(A.level_ms(base - DAY_MS, base), "expired")
        self.assertEqual(A.level_ms(None, base), "unknown")
        # 精确到毫秒的边界：3 天整点仍是 cleanup，多 1ms 进 urgent
        self.assertEqual(A.level_ms(base + 3 * DAY_MS, base), "cleanup")
        self.assertEqual(A.level_ms(base + 3 * DAY_MS + 1, base), "urgent")

    def test_boundaries(self):
        """`level(days)` 只剩展示兼容语义：0 天 = 今天到期（cleanup），不是 expired。"""
        cases = {15: "ok", 14: "near", 13: "near", 8: "near", 7: "urgent",
                 4: "urgent", 3: "cleanup", 2: "cleanup", 1: "cleanup",
                 0: "cleanup", -1: "expired"}
        for days, want in cases.items():
            self.assertEqual(A.level(days), want, f"{days} 天应为 {want}")
        self.assertEqual(A.level(None), "unknown")
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
            # M3：空串 = 「没传」→ **保持当前值**（不再允许被空输入抹掉）。
            # 真正清空走「参数」页的「清除兜底」按钮（写空串进 DB，再由
            # manual_until 读成 None —— 那条路径不经过 apply_db 的空串语义）。
            db.set_config("hl_agent_valid_until_ms", "")
            cfg2 = __import__("app.config", fromlist=["Config"]).Config().apply_db(db.get_config())
            self.assertEqual(cfg2.hl_agent_valid_until_ms, "",
                             "空串时回到默认空值（未被\"\"覆盖成别的）")
            self.assertNotIn("hl_agent_valid_until_ms",
                             __import__("app.config", fromlist=["Config"]).Config._CLEARABLE_STR,
                             "该键不能在 _CLEARABLE_STR 里（否则空输入会抹掉兜底值）")
            # 有值时再套一层：带值 apply_db 仍生效
            db.set_config("hl_agent_valid_until_ms", "1830000000000")
            cfg3 = __import__("app.config", fromlist=["Config"]).Config().apply_db(db.get_config())
            self.assertEqual(cfg3.hl_agent_valid_until_ms, "1830000000000")
            cfg4 = cfg3.apply_db({"hl_agent_valid_until_ms": ""})
            self.assertEqual(cfg4.hl_agent_valid_until_ms, "1830000000000",
                             "空输入不许把已有的兜底值抹成空")
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
                  cfg=None, mode=""):
        info = FakeInfo(agents=agents if agents is not None else
                        ([_agent(until=until_in_days(days))] if days is not None else []),
                        raise_agents=raise_agents)
        from app.config import Config
        return A.check_once(self.db, cfg or Config(), self.notifier, info,
                            address="0xMAIN", now_ms=NOW_MS, cleanup=cleanup, mode=mode)

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
            want_kind = "agent_expiry_near" if want_lvl == "near" else "agent_expiry_urgent"
            self.assertIn(want_kind, kinds, f"{days} 天应用 {want_kind}")

    def test_upgrade_not_suppressed_by_near_dedup(self):
        """M4 回归：near → urgent 升级通知不能被 near 的 24h 去重吞掉。"""
        self.run_check(days=8)                       # near
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("有效期提醒", self.notifier.messages[0])
        # 1 小时后进入 7 天（urgent）：kind 不同 → 必须再推一条升级文案
        info = FakeInfo(agents=[_agent(until=until_in_days(7, NOW_MS + 10 * DAY_MS))])
        from app.config import Config
        st = A.check_once(self.db, Config(), self.notifier, info,
                          address="0xMAIN", now_ms=NOW_MS + 10 * DAY_MS)
        self.assertEqual(st["level"], "urgent")
        self.assertEqual(len(self.notifier.messages), 2, "升级文案被去重压住了")
        self.assertIn("紧急", self.notifier.messages[-1])
        kinds = [r["kind"] for r in self.db.recent_alerts(10)]
        self.assertIn("agent_expiry_near", kinds)
        self.assertIn("agent_expiry_urgent", kinds)

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
        self.assertIn("agent_expiry_cleanup_ok", kinds)
        self.assertTrue(any("已清仓" in m for m in self.notifier.messages))

    def test_cleanup_ok_edge_trigger_only_once(self):
        """M4/L2 回归：清仓成功后**不再**每 24h 重推「已清仓（此前已完成）」。"""
        calls = []

        def cleanup(reason=""):
            calls.append(reason)
            return {"ok": True, "closed": [{"coin": "BTC", "sz": 0.01}], "dry": False}

        self.run_check(days=2, cleanup=cleanup)
        n1 = len(self.notifier.messages)
        self.assertEqual(n1, 1)
        # 后续几轮（跨过 24h 去重窗口也算）：done 态不再推、不再调清仓器
        for i in range(1, 4):
            self.run_check(days=2, cleanup=cleanup)
        self.run_check_again(days=2, cleanup=cleanup, now_ms=NOW_MS + 2 * DAY_MS)
        self.assertEqual(len(calls), 1, "done 之后不许再清仓")
        self.assertEqual(len(self.notifier.messages), n1,
                         "「已清仓」只推一次（边沿触发），不许每轮/每天重推")

    def test_cleanup_success_after_failure_is_not_swallowed(self):
        """M4 回归：清仓失败后 2 小时成功 —— 成功通知不能被失败通知的 24h 去重吞掉。"""
        results = [{"ok": False, "error": "撤单全部失败：签名错误", "closed": []},
                   {"ok": True, "closed": [{"coin": "BTC", "sz": 0.01}], "dry": False}]

        def cleanup(reason=""):
            return results.pop(0)

        st = self.run_check(days=2, cleanup=cleanup)
        self.assertEqual(st["cleanup"], "failed")
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("自动清仓失败", self.notifier.messages[0])
        # 2 小时后重试成功
        info = FakeInfo(agents=[_agent(until=until_in_days(2, NOW_MS + 2 * 3600_000))])
        from app.config import Config
        st2 = A.check_once(self.db, Config(), self.notifier, info, address="0xMAIN",
                           now_ms=NOW_MS + 2 * 3600_000, cleanup=cleanup)
        self.assertEqual(st2["cleanup"], "done")
        self.assertEqual(len(self.notifier.messages), 2, "成功通知被失败的 24h 去重吞掉了")
        self.assertIn("已清仓", self.notifier.messages[-1])
        kinds = [r["kind"] for r in self.db.recent_alerts(10)]
        self.assertIn("agent_expiry_cleanup_fail", kinds)
        self.assertIn("agent_expiry_cleanup_ok", kinds)

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

    def test_mode_off_marks_skipped_and_does_not_retry(self):
        """M5 回归：执行器 off（用户主动）→ skipped + 一条提醒，不喊「失败」、不进重试循环。"""
        calls = []

        def cleanup(reason=""):
            calls.append(reason)
            return {"ok": False, "error": "执行器未启用或装配失败（模式=off？）"}

        st = self.run_check(days=2, cleanup=cleanup, mode="off")
        self.assertEqual(st["cleanup"], "skipped", "off 不是失败，state 必须标 skipped")
        self.assertEqual(len(self.notifier.messages), 1)
        msg = self.notifier.messages[0]
        self.assertIn("执行器未启用（off）", msg)
        self.assertIn("自行决定", msg)
        self.assertNotIn("自动清仓失败", msg, "用户是故意 off 的，不许喊「失败」")
        self.assertNotIn("请手动处理", msg)
        # 后续轮次：7 天节流（不再每小时刷） + 不再调清仓器
        for _ in range(5):
            self.run_check(days=2, cleanup=cleanup, mode="off")
        self.assertEqual(len(self.notifier.messages), 1, "off 提醒必须节流（7 天）")
        self.assertEqual(calls, [], "off 模式根本不走清仓器")
        # 7 天后允许再提醒一次（去重窗口按 alert_logs.created_at 的真实时间算，
        # 单测里把那一行的时间回拨来模拟）
        self.db.conn.execute("UPDATE alert_logs SET created_at = created_at - 8*86400")
        self.db.conn.commit()
        self.run_check(days=2, cleanup=cleanup, mode="off")
        self.assertEqual(len(self.notifier.messages), 2, "7 天后应再提醒一次")

    def test_dry_mode_wording_says_no_orders_sent(self):
        """M5 回归：dry 文案不许说「已撤单」（实际一张单都没发）。"""
        st = self.run_check(days=2, cleanup=lambda reason="": {
            "ok": True, "dry": True, "closed": [{"coin": "BTC", "sz": 0.01, "dry": True}]})
        msg = [m for m in self.notifier.messages if "影子模式" in m][0]
        self.assertIn("未向交易所发送任何指令", msg)
        self.assertIn("live", msg)
        self.assertNotIn("已撤单", msg)
        self.assertNotIn("已撤掉", msg)
        self.assertEqual(st["cleanup"], "none", "dry 不写 done")

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
        mode = kw.pop("mode", "")
        info = FakeInfo(agents=[_agent(until=until_in_days(kw.pop("days"), now_ms))])
        from app.config import Config
        return A.check_once(self.db, Config(), self.notifier, info,
                            address="0xMAIN", now_ms=now_ms, cleanup=kw.get("cleanup"),
                            mode=mode)

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

    def test_null_valid_until_is_no_expiry_and_ignores_stale_manual(self):
        """H4 回归：命中授权行但 validUntil 为空 = 不限期 → 不告警、不清仓、不读兜底。

        以前这一路会掉进「until is None → 回落地兜底」分支，拿几个月前填的陈旧
        兜底值算「≤3 天」，然后真的把仓位平掉。
        """
        # 手工兜底填了个「2 天后到期」的陈旧值 —— 必须完全不起作用
        self.db.set_config("hl_agent_valid_until_ms", str(until_in_days(2)))
        calls = []

        def cleanup(reason=""):
            calls.append(reason)
            return {"ok": True, "closed": [{"coin": "BTC", "sz": 0.01}], "dry": False}

        st = self.run_check(agents=[_agent(until=None)], cleanup=cleanup)
        self.assertEqual(st["source"], "api", "行命中了就是 api 来源")
        self.assertIsNone(st["valid_until_ms"])
        self.assertIsNone(st["days_left"])
        self.assertEqual(st["level"], "no_expiry", "不限期是独立档位，不是 unknown")
        self.assertEqual(self.notifier.messages, [], "不限期不告警")
        self.assertEqual(calls, [], "不限期绝不清仓（H4：陈旧兜底值误平仓）")
        self.assertFalse(self.db.hl_kill(), "不限期不置 kill")
        self.assertEqual(self.state()["source"], "api")
        self.assertEqual(self.state()["level"], "no_expiry")

    def test_prev_cleanup_not_done_when_no_expiry(self):
        """不限期时上一轮的 done 不该被沿用成「已清仓」以外的语义。"""
        A.write_state(self.db, {"cleanup": "done", "address": "0xold", "source": "api",
                                "valid_until_ms": None, "days_left": None, "level": "unknown"})
        st = self.run_check(agents=[_agent(until=None)])
        self.assertEqual(st["level"], "no_expiry")
        self.assertEqual(self.notifier.messages, [], "不限期不推任何东西")

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


# ---------------------------------------------------------------- H2：清仓后本地关账
class TestFlattenClosesLocalPositions(Base):
    """H2 回归：清仓成功后本地仓位必须关账，否则对账永久 mismatch → kill 死循环。"""

    def _open_local(self, symbol="BTC_USDT", bar_ms=1_792_296_000_000, side=1,
                    entry=83000.0, stop=80600.0, hl_status="open"):
        pid = self.db.create_position(
            symbol=symbol, signal_bar_ms=bar_ms, side=side, entry_price=entry,
            stop_price=stop, stop_dist=abs(entry - stop) / entry, contracts=1,
            coin_qty=0.01, notional=entry * 0.01, risk_u=20.0, equity_at_entry=1000.0)
        self.db.confirm_position(pid, entry, 0.01, stop, True)
        if hl_status:
            self.db.set_pos_hl(pid, hl_status=hl_status, hl_sz=0.01)
        return pid

    def test_flatten_closes_local_row_with_fill_price(self):
        pid = self._open_local()
        info = FakeInfo(snap={"positions": [_pos("BTC", 0.01, 1, 83000.0)], "open_orders": []})
        ex = FakeExchange(info=info)
        res = A.flatten_all(self.db, self.params, info, ex, self.notifier)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["closed_local"], 1)
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "closed", "本地仓位必须关账（H2）")
        self.assertEqual(row["exit_reason"], A.EXIT_REASON_EXPIRY)
        self.assertEqual(row["auto_closed"], 1, "引擎自动关账 → 用户可在确认页覆盖真实价")
        # exit_price 用实际成交均价（FakeExchange 回 limit_px）
        fill = [c for c in ex.calls if c[0] == "order"][0][4]
        self.assertAlmostEqual(float(row["exit_price"]), float(fill), places=6)
        self.assertEqual(row["hl_status"], "closed")

    def test_flatten_marks_local_orders_cancelled(self):
        self._open_local()
        self.db.hl_record_intent("BTC_USDT", "BTC", 1_792_296_000_000, "stop", 1, 0.01,
                                 px=80600.0, reduce_only=True, status="resting")
        info = FakeInfo(snap={"positions": [_pos("BTC", 0.01, 1, 83000.0)], "open_orders": []})
        A.flatten_all(self.db, self.params, info, FakeExchange(info=info), self.notifier)
        rows = [r for r in self.db.hl_recent_orders(10) if r["purpose"] == "stop"]
        self.assertEqual(rows[0]["status"], "cancelled", "清仓撤掉的挂单本地状态要同步")

    def test_flatten_does_not_touch_mexc_only_rows(self):
        """MEXC 人工行（hl_status 为空、没发过 HL 入场单）不许被清仓关账。"""
        pid = self.db.create_position(
            symbol="BTC_USDT", signal_bar_ms=42, side=1, entry_price=83000.0,
            stop_price=80600.0, stop_dist=0.03, contracts=85, coin_qty=0.01,
            notional=830.0, risk_u=20.0, equity_at_entry=1000.0)
        info = FakeInfo(snap={"positions": [], "open_orders": []})
        res = A.flatten_all(self.db, self.params, info, FakeExchange(info=info), self.notifier)
        self.assertTrue(res["ok"])
        self.assertEqual(res["closed_local"], 0)
        self.assertEqual(self.db.position_by_id(pid)["status"], "pending")

    def test_reconcile_clean_after_cleanup_and_kill_can_be_cleared(self):
        """清仓 → 对账不再报 mismatch → 解 kill 后 run() 不重置 kill（H2 全链路）。"""
        from app.hl.executor import Executor, ExecParams
        pid = self._open_local()
        info = FakeInfo(snap={"positions": [_pos("BTC", 0.01, 1, 83000.0)],
                              "open_orders": [],
                              "notional": 830.0, "equity": 1000.0,
                              "equity_total": 1000.0, "withdrawable": 1000.0,
                              "margin_used": 166.0, "orders_trusted": True,
                              "ts": time.time()})
        ex = FakeExchange(info=info)
        params = ExecParams(mode=MODE_LIVE, account_address="0xMAIN", private_key="0xkey",
                            equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="t")
        exe = Executor(self.db, params, info, ex, notifier=self.notifier)
        # 清仓前先 kill（check_once 的顺序），再清仓
        self.db.hl_set_kill(True, "agent 到期保护")
        res = A.flatten_all(self.db, params, info, ex, self.notifier)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.db.position_by_id(pid)["status"], "closed")
        # 交易所已空仓 + 本地已关账 → 对账必须干净
        rec = exe.reconcile(info.snapshot("0xMAIN"))
        self.assertTrue(rec["ok"], f"清仓后仍然对账异常：{rec['problems']}")
        # 续期后解 kill → 下一轮 run() 不许再把它拉回去
        self.db.hl_set_kill(False, "")
        alerts = exe.run(force_snapshot=True)
        self.assertFalse(self.db.hl_kill(), f"run() 又把 kill 拉回来了：{alerts}")
        self.assertFalse(any("对账异常" in a for a in alerts))

    def test_flatten_ok_with_no_local_rows_still_ok(self):
        """交易所空仓、本地也没仓：清仓照常成功，不报错。"""
        info = FakeInfo(snap={"positions": [], "open_orders": []})
        res = A.flatten_all(self.db, self.params, info, FakeExchange(info=info), self.notifier)
        self.assertTrue(res["ok"])
        self.assertEqual(res["closed_local"], 0)

    def test_stale_local_row_closed_even_without_fresh_close(self):
        """上一轮平了仓但关账失败/进程被杀：这轮交易所已空仓、closed 为空 → 仍要补关账。"""
        pid = self._open_local()
        info = FakeInfo(snap={"positions": [], "open_orders": []})
        res = A.flatten_all(self.db, self.params, info, FakeExchange(info=info), self.notifier)
        self.assertTrue(res["ok"])
        self.assertEqual(res["closed_local"], 1)
        self.assertEqual(self.db.position_by_id(pid)["status"], "closed")


# ---------------------------------------------------------------- M1：挂单读不到不许当没有
class TestOrdersUntrusted(Base):
    def test_cancel_all_orders_untrusted_is_failure(self):
        from app.hl.exchange import HlExchange
        class Info:
            def open_orders_checked(self, address):
                return [], False          # 接口异常/回落老接口
            def open_orders(self, address):
                return []
        ex = HlExchange(account_address="0xMAIN", dry_run=False, info=Info(), sdk=object())
        res = ex.cancel_all_orders()
        self.assertFalse(res["ok"], "挂单读不到不能当「没有挂单」")
        self.assertEqual(res.get("reason"), "orders_untrusted")

    def test_cancel_all_orders_uses_trusted_list(self):
        """可信列表照常逐张撤；dry 模式只报「本该撤哪些」。"""
        from app.hl.exchange import HlExchange
        class Info:
            def __init__(self):
                self.calls = []
            def open_orders_checked(self, address):
                return ([{"coin": "BTC", "oid": 11}, {"coin": "ETH", "oid": 12}], True)
        info = Info()
        ex = HlExchange(account_address="0xMAIN", dry_run=True, info=info, sdk=object())
        res = ex.cancel_all_orders()
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "dry")
        self.assertEqual(res["raw"]["oids"], [11, 12])

    def test_flatten_does_not_close_when_orders_untrusted(self):
        """M1：撤单读不到 → 整轮失败重试，绝不贸然平仓留孤儿单。"""
        class UntrustedExchange(FakeExchange):
            def cancel_all_orders(self):
                self.calls.append(("cancel_all",))
                return {"ok": False, "error": "读不到可信的挂单列表", "reason": "orders_untrusted",
                        "raw": {"oids": [], "failed": []}}
        info = FakeInfo(snap={"positions": [_pos()], "open_orders": []})
        ex = UntrustedExchange(info=info)
        res = A.flatten_all(self.db, self.params, info, ex, self.notifier)
        self.assertFalse(res["ok"])
        self.assertIn("撤单失败", res["error"])
        self.assertEqual([c for c in ex.calls if c[0] == "order"], [],
                         "挂单状态未知时不许平仓（会留孤儿 reduce-only 单）")

    def test_cancel_failed_detail_goes_into_alert(self):
        """L5：撤单部分失败时，失败明细要进清仓失败文案。"""
        class PartialFail(FakeExchange):
            def cancel_all_orders(self):
                return {"ok": False, "error": "撤单全部失败：签名错误",
                        "raw": {"oids": [], "failed": [
                            {"oid": 11, "coin": "BTC", "error": "签名错误"},
                            {"oid": 12, "coin": "ETH", "error": "签名错误"}]}}
        info = FakeInfo(snap={"positions": [_pos()], "open_orders": []})
        res = A.flatten_all(self.db, self.params, info, PartialFail(info=info), self.notifier)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["cancel_failed"]), 2)
        self.db.set_config("hl_agent_valid_until_ms", str(until_in_days(2)))
        info2 = FakeInfo(agents=[])
        from app.config import Config
        A.check_once(self.db, Config(), self.notifier, info2, address="0xMAIN",
                     now_ms=NOW_MS,
                     cleanup=lambda reason="": res)
        msg = [m for m in self.notifier.messages if "自动清仓失败" in m][-1]
        self.assertIn("未撤掉的挂单", msg)
        self.assertIn("BTC#11", msg)
        self.assertIn("签名错误", msg)


# ---------------------------------------------------------------- 面板 / 装配
class TestPanelAndWiring(unittest.TestCase):
    def test_alert_kind_txt_has_new_kinds(self):
        from app.admin_ui import _ALERT_KIND_TXT
        for kind in ("agent_expiry_near", "agent_expiry_urgent", "agent_expiry_expired",
                     "agent_expiry_cleanup_ok", "agent_expiry_cleanup_fail", "agent_renew",
                     # 历史行仍要有中文
                     "agent_expiry_alert", "agent_expiry_cleanup"):
            self.assertIn(kind, _ALERT_KIND_TXT, f"{kind} 必须进中文映射")

    def test_all_agent_kinds_are_mapped(self):
        """任何 agent.* 里定义的 kind 都必须有中文映射（漏了会显示英文原文）。"""
        from app.admin_ui import _ALERT_KIND_TXT
        kinds = [getattr(A, n) for n in dir(A) if n.startswith("KIND_")]
        self.assertTrue(kinds)
        for k in kinds:
            self.assertIn(k, _ALERT_KIND_TXT, f"{k} 漏了中文映射")

    def test_cleanup_job_holds_tick_lock(self):
        """清仓与 tick 互斥：清仓入口必须持进程内 _tick_lock（源码级断言，不 import main）。"""
        src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        m = re.search(r"def _run_agent_cleanup.*?(?=\ndef )", src, re.S)
        self.assertIsNotNone(m, "找不到 _run_agent_cleanup")
        body = m.group(0)
        self.assertIn("with _tick_lock", body)
        self.assertIn("flatten_for_expiry", body)

    def test_hl_monitor_holds_tick_lock(self):
        """M2：hl_monitor 必须与清仓串行（同一把 _tick_lock）。"""
        src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        m = re.search(r"def _job_hl_monitor.*?(?=\ndef )", src, re.S)
        self.assertIsNotNone(m, "找不到 _job_hl_monitor")
        body = m.group(0)
        self.assertIn("with _tick_lock", body)
        self.assertIn("ex.run(", body)

    def test_op_lock_is_process_wide(self):
        """M2：_op_lock 必须是**类属性**（进程级）—— 实例级锁跨 Executor 无效。"""
        import inspect
        from app.hl.executor import Executor
        self.assertIn("_op_lock", vars(Executor),
                      "_op_lock 必须定义在类上（build_executor 每轮新建实例）")
        init_src = inspect.getsource(Executor.__init__)
        self.assertNotIn("self._op_lock =", init_src, "不许再建实例级锁")
        e1 = Executor(db=None, params=None, info=None, exchange=None)
        e2 = Executor(db=None, params=None, info=None, exchange=None)
        self.assertIs(e1._op_lock, e2._op_lock, "两个 Executor 必须是同一把锁")

    def test_concurrent_cleanup_blocks_monitor(self):
        """M2 行为测试：清仓持锁期间，另一个 Executor 的 run() 不会动交易所。"""
        import threading
        import time as _t
        from app.hl.executor import Executor, ExecParams
        tmp = tempfile.TemporaryDirectory()
        try:
            db = DB(str(Path(tmp.name) / "m2.db"))
            snap = {"positions": [], "open_orders": [], "orders_trusted": True,
                    "ts": _t.time(), "equity": 1000.0, "equity_total": 1000.0,
                    "withdrawable": 1000.0, "margin_used": 0.0, "notional": 0.0}
            info = FakeInfo(snap=snap)
            params = ExecParams(mode=MODE_LIVE, account_address="0xMAIN",
                                private_key="0xkey", code_rev="t")
            held = threading.Event()
            release = threading.Event()

            class SlowExchange(FakeExchange):
                def __init__(self, info=None):
                    super().__init__(info=info)
                    self.monitor_calls = []
                def cancel_all_orders(self):
                    held.set()                     # 已经在清仓临界区里了
                    release.wait(5.0)
                    return super().cancel_all_orders()

            slow = SlowExchange(info=info)
            cleaner = Executor(db, params, info, slow, notifier=None)
            other = Executor(db, params, info, slow, notifier=None)
            t = threading.Thread(target=lambda: cleaner.flatten_for_expiry("test"), daemon=True)
            t.start()
            self.assertTrue(held.wait(5.0), "清仓没进入临界区")
            # 此时 monitor 想跑：必须被同一把锁挡住
            done = threading.Event()

            def _monitor():
                other.run(force_snapshot=True)
                done.set()

            m = threading.Thread(target=_monitor, daemon=True)
            m.start()
            self.assertFalse(done.wait(0.3), "清仓持锁期间 monitor 竟然跑完了（互斥不成立）")
            release.set()
            self.assertTrue(done.wait(10.0), "清仓结束后 monitor 必须能继续")
            t.join(5.0)
            m.join(5.0)
        finally:
            tmp.cleanup()

    def test_cleanup_session_flag_blocks_stop_repair(self):
        """M2：清仓标记期间 _repair_missing_stop 不动交易所。"""
        from app.hl.executor import Executor, ExecParams
        tmp = tempfile.TemporaryDirectory()
        try:
            db = DB(str(Path(tmp.name) / "m2b.db"))
            ex_ch = FakeExchange()
            info = FakeInfo(snap={"positions": [_pos()], "open_orders": []})
            params = ExecParams(mode=MODE_LIVE, account_address="0xMAIN",
                                private_key="0xkey", code_rev="t")
            exe = Executor(db, params, info, ex_ch, notifier=SilentNotifier())
            row = {"symbol": "BTC_USDT", "signal_bar_ms": 1, "side": 1, "id": 1,
                   "stop_price": 80600.0, "hl_sz": 0.01}
            with A.cleanup_session():
                self.assertTrue(A.cleanup_in_progress())
                exe._repair_missing_stop(row, info.snapshot("0xMAIN"))
            self.assertFalse(A.cleanup_in_progress(), "退出后标记必须清掉")
            self.assertEqual(ex_ch.calls, [], "清仓期间不许补挂/平仓")
            # 标记解除后，同样的调用会真的补挂（证明不是永久的）
            exe._repair_missing_stop(row, info.snapshot("0xMAIN"))
            self.assertTrue(ex_ch.calls, "标记解除后应恢复补挂")
        finally:
            tmp.cleanup()

    def test_stop_repair_skipped_when_kill_already_on(self):
        """M2：kill 已置（进来时就是真）→ 不自动补挂，避免与人工操作打架。"""
        from app.hl.executor import Executor, ExecParams
        tmp = tempfile.TemporaryDirectory()
        try:
            db = DB(str(Path(tmp.name) / "m2c.db"))
            db.hl_set_kill(True, "人工")
            ex_ch = FakeExchange()
            info = FakeInfo(snap={"positions": [_pos()], "open_orders": []})
            params = ExecParams(mode=MODE_LIVE, account_address="0xMAIN",
                                private_key="0xkey", code_rev="t")
            exe = Executor(db, params, info, ex_ch, notifier=SilentNotifier())
            row = {"symbol": "BTC_USDT", "signal_bar_ms": 1, "side": 1, "id": 1,
                   "stop_price": 80600.0, "hl_sz": 0.01}
            exe._repair_missing_stop(row, info.snapshot("0xMAIN"), kill_before=True)
            self.assertEqual(ex_ch.calls, [], "kill 态下不许补挂")
        finally:
            tmp.cleanup()

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
            # H3：复选框前必须有同名 hidden 兜底（未勾选时提交 0）
            self.assertRegex(
                html, r"<input type='hidden' name='agent_expiry_cleanup' value='0'>"
                      r"<label class='chk'><input type='checkbox' name='agent_expiry_cleanup'")
            base = {"equity": "100", "risk_pct": "0.02", "leverage": "5",
                    "stop_min": "0.015", "stop_max": "0.1", "short_regime_ma": "200",
                    "equity_mode": "follow", "equity_min": "20", "k": "tk"}
            # 1) 老表单不带这个字段（字段缺失）→ 保持原值，绝不静默改动
            r = c.post("/admin/params", data=dict(base), follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIsNone(db.get_config_str("agent_expiry_cleanup") or None,
                              "字段缺失（老书签）不该写库")
            # 2) 未勾选（浏览器只提交 hidden=0）→ DB=0、生效=关
            r = c.post("/admin/params",
                       data=dict(base, agent_expiry_cleanup="0"), follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("0", str(db.get_config_str("agent_expiry_cleanup")))
            self.assertFalse(A.cleanup_switch(db, Config()),
                             "取消勾选后必须真的关掉（H3：以前永远关不掉）")
            html2 = c.get("/admin", params={"k": "tk", "tab": "params"}).text
            self.assertNotRegex(html2, r"name='agent_expiry_cleanup' value='1' checked")
            self.assertIn("当前 关", html2)
            # 3) 勾选（浏览器提交 hidden=0 + checkbox=1，顺序即文档顺序）→ DB=1
            r = c.post("/admin/params", content=(
                "&".join(f"{k}={v}" for k, v in base.items())
                + "&agent_expiry_cleanup=0&agent_expiry_cleanup=1"),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("1", str(db.get_config_str("agent_expiry_cleanup")))
            self.assertTrue(A.cleanup_switch(db, Config()))
            # 4) 识别不了的值按「开」（fail-open）
            r = c.post("/admin/params",
                       data=dict(base, agent_expiry_cleanup="weird"), follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("1", str(db.get_config_str("agent_expiry_cleanup")))
        finally:
            tmp.cleanup()

    def test_params_save_keeps_manual_fallback(self):
        """M3 回归：参数页留空保存不会抹掉手工兜底有效期（要清空走显式按钮）。"""
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
            db = DB(str(Path(tmp.name) / "m3.db"))
            db.set_config("hl_agent_valid_until_ms", "1830000000000")
            cfg_fn = lambda: Config().apply_db(db.get_config())
            hl = DashboardDeps(db=db, config_fn=cfg_fn, info=None,
                               address_fn=lambda: "", mode_fn=lambda: "off",
                               base_url_fn=lambda: "https://api.hyperliquid.xyz")
            deps = AdminDeps(db=db, config_fn=cfg_fn, hl_deps=hl,
                             mode_fn=lambda: "off", token_fn=lambda: "tk")
            app = FastAPI()
            app.include_router(build_admin_router(deps))
            c = TestClient(app)
            base = {"equity": "100", "risk_pct": "0.02", "leverage": "5",
                    "stop_min": "0.015", "stop_max": "0.1", "short_regime_ma": "200",
                    "equity_mode": "follow", "equity_min": "20",
                    "hl_agent_valid_until_ms": "", "k": "tk"}
            r = c.post("/admin/params", data=dict(base), follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(str(db.get_config_str("hl_agent_valid_until_ms")),
                             "1830000000000",
                             "留空保存把手工兜底值抹掉了（M3）")
            # 填新值仍然生效
            r = c.post("/admin/params",
                       data=dict(base, hl_agent_valid_until_ms="1840000000000"),
                       follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(str(db.get_config_str("hl_agent_valid_until_ms")), "1840000000000")
            # 显式清除按钮才清空
            r = c.post("/admin/hl/agent/fallback/clear", data={"k": "tk"},
                       follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(db.get_config_str("hl_agent_valid_until_ms", ""), "")
            self.assertIsNone(A.manual_until(db), "清除后兜底值真的没了")
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
