"""DB 覆盖参数的解析回归（`Config.apply_db`）。

历史 bug（2026-10-14 发现，直接影响实盘规模）：旧面板（`/admin/rules`，2026-10-30 随
参数页删除）把 equity / risk_pct / leverage / stop_min / stop_max 以**字符串**写库，`set_config` 走 `json.dumps` →
存成 `'"100"'`（带引号）；`apply_db` 里 `float('"100"')` 抛 ValueError，被
`except (TypeError, ValueError): continue` **静默吞掉** → 面板上改这些参数永远不生效。

2026-10-30 面板精简后，DB 里的这些键**只能靠容器内 sqlite 直改**写入（runbook），
所以「写进去的形态五花八门 → apply_db 必须都能认」这条比以前更要紧：直改 SQL 的人
不会替你把值 json.dumps 成数字。

之所以长期没被发现：默认 env（ACCOUNT_EQUITY=1000）与 DB 里的 "1000" 数值相同，
看起来「一切正常」；一旦两者不同（比如测试期把资金改成 100）就会静默按旧值跑，
单笔风险会按 1000×2%=20U 算 = 实际 100U 资金的 20%。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Config
from app.db import DB


class TestApplyDbTypes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_quoted_numbers_take_effect(self):
        """老库里已存的带引号值必须也能正确解析（否则历史行永远覆盖不了 env）。"""
        c = Config()
        c.apply_db({"equity": '"100"', "risk_pct": '"0.01"', "leverage": '"3"',
                    "stop_min": '"0.02"', "stop_max": '"0.08"'})
        self.assertEqual(c.equity, 100.0)
        self.assertEqual(c.risk_pct, 0.01)
        self.assertEqual(c.leverage, 3.0)
        self.assertEqual(c.stop_min, 0.02)
        self.assertEqual(c.stop_max, 0.08)

    def test_bare_json_numbers_take_effect(self):
        c = Config()
        c.apply_db({"equity": 100, "risk_pct": 0.01, "leverage": 3})
        self.assertEqual(c.equity, 100.0)
        self.assertEqual(c.risk_pct, 0.01)
        self.assertEqual(c.leverage, 3.0)

    def test_garbage_keeps_previous_value(self):
        c = Config()
        before = (c.equity, c.risk_pct)
        c.apply_db({"equity": "abc", "risk_pct": "不是数字"})
        self.assertEqual((c.equity, c.risk_pct), before)

    def test_empty_values_are_ignored(self):
        c = Config()
        before = c.equity
        c.apply_db({"equity": "", "risk_pct": None})
        self.assertEqual(c.equity, before)

    def test_config_write_then_read_roundtrip(self):
        """写库（模拟容器内 sqlite 存数字）→ 读回 → 生效值正确。"""
        for k, v in (("equity", 100.0), ("risk_pct", 0.02), ("leverage", 5.0),
                     ("stop_min", 0.015), ("stop_max", 0.10)):
            self.db.set_config(k, v)
        c = Config().apply_db(self.db.get_config())
        self.assertEqual(c.equity, 100.0)
        self.assertEqual(c.risk_pct, 0.02)
        self.assertEqual(c.leverage, 5.0)

    def test_legacy_quoted_rows_still_apply(self):
        """直接在库里塞带引号的老行（模拟升级前的数据），也必须生效。"""
        with self.db.conn:
            for k, v in (("equity", '"250"'), ("leverage", '"10"')):
                self.db.conn.execute(
                    "INSERT INTO configuration(key, value, updated_at) VALUES(?,?,0) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        c = Config().apply_db(self.db.get_config())
        self.assertEqual(c.equity, 250.0)
        self.assertEqual(c.leverage, 10.0)

    def test_booleans_still_strict(self):
        c = Config()
        c.apply_db({"enable_long": 0})
        self.assertFalse(c.enable_long)
        c.apply_db({"enable_long": 1})
        self.assertTrue(c.enable_long)
        c.apply_db({"enable_long": "abc"})          # 识别不了 → 保留原值，绝不静默关闭
        self.assertTrue(c.enable_long)


if __name__ == "__main__":
    unittest.main()
