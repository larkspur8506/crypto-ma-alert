"""确认页 / 修改页（`app/main.py::_entry_form`）的渲染回归。

B1（2026-10-24 对抗审查）：双口径收敛那次提交（c950bdc）把 `_entry_form` 里
`_is_hl` 的赋值行连同注释一起删掉了，但五处引用还在 → **任何 pending 行**打开
`GET /c/<pid>` 直接 `NameError: name '_is_hl' is not defined`（500），HL 行与
MEXC 行一起挂。这类「只在渲染时才炸」的 NameError 全量单测原先没覆盖，所以
这里按「一行渲染一次」钉住三条路径：

1. pending 的 **HL 行**：按币量口径（actual_contracts）、禁用 MEXC 金额换算
2. pending 的 **MEXC 行**：张数口径、金额换算可用、目标风险显示生效后百分比
3. 已确认 MEXC 行的**修改页**（a=edit_open）：同一渲染函数，同样不得 500

刻意用真 HTTP 路由（TestClient）而不是直接调 `_entry_form`：500 是路由层的
表现，直接调函数会把「谁传了什么」这层漏掉。
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient
except ImportError:                                            # pragma: no cover
    TestClient = None

TOKEN_SECRET = "entry-secret"


@unittest.skipIf(TestClient is None, "fastapi 未安装")
class TestEntryFormRender(unittest.TestCase):
    """`_entry_form` 的三种入口都必须 200（B1 防再次存活）。"""

    def setUp(self):
        assert TestClient is not None, "fastapi 未安装"
        self.tmp = tempfile.mkdtemp(prefix="entry_form_")
        self._env = {k: os.environ.get(k) for k in
                     ("DB_PATH", "ENABLE_SCHEDULER", "TOKEN_SECRET", "BASE_URL",
                      "DRY_RUN", "SHORT_RISK_HALF", "RISK_PCT", "STOP_MAX")}
        os.environ.update({"DB_PATH": os.path.join(self.tmp, "entry.db"),
                           "ENABLE_SCHEDULER": "false",
                           "TOKEN_SECRET": TOKEN_SECRET,
                           "BASE_URL": "https://entry.example",
                           "DRY_RUN": "true",
                           "RISK_PCT": "0.02",
                           "STOP_MAX": "0.1"})
        os.environ.pop("SHORT_RISK_HALF", None)
        import app.main as main_mod
        self.mod = importlib.reload(main_mod)
        # 不依赖进程级 env 的读取时序（app.config 可能已被别的用例导入过）
        self.mod.config.token_secret = TOKEN_SECRET
        self.mod.config.base_url = "https://entry.example"
        self.db = self.mod.db
        # 渲染 pending 行会去取行情算 MA200：断网（抛异常 → 页面显示「MA200 检查失败」，
        # 不影响要断言的表单字段），避免单测触网。
        self._fetch = self.mod.market.fetch
        self.mod.market.fetch = lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("offline in test"))

    def tearDown(self):
        self.mod.market.fetch = self._fetch
        try:
            self.db.conn.close()
        except Exception:                                       # noqa: BLE001
            pass
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具

    def _pending(self, symbol="BTC_USDT", side=-1, bar_ms=1_792_000_000_000):
        return self.db.create_position(
            symbol=symbol, signal_bar_ms=bar_ms, side=side,
            entry_price=83_000.0, stop_price=85_400.0, stop_dist=0.0289,
            contracts=85, coin_qty=0.0085, notional=697.2, risk_u=20.16,
            equity_at_entry=1000.0)

    def _hl_pending(self):
        """HL 执行器管的 pending 行：contracts/notional/risk_u 是 MEXC 人工口径
        （无意义），真实量在 actual_contracts / hl_sz（**币量**）。"""
        pid = self._pending(bar_ms=1_792_000_400_000)
        self.db.set_pos_hl(pid, hl_status="pending", hl_sz=0.00084,
                           hl_entry_oid="0xlive0001")
        self.db.conn.execute(
            "UPDATE positions SET actual_entry=?, actual_stop=?, actual_contracts=? WHERE id=?",
            (83_120.0, 85_400.0, 0.00084, pid))
        self.db.conn.commit()
        return pid

    def _mexc_open(self):
        pid = self._pending(bar_ms=1_792_000_600_000)
        self.db.confirm_position(pid, 83_000.0, 85, 85_400.0, True)
        return pid

    def _get(self, pid, action="view"):
        tok = self.mod.engine.sign_token(TOKEN_SECRET, pid, action)
        with TestClient(self.mod.app) as c:
            return c.get(f"/c/{pid}", params={"a": action, "t": tok})

    # ---- ① pending 行（B1 的直接受害路径）

    def test_pending_mexc_row_renders(self):
        r = self._get(self._pending())
        self.assertEqual(r.status_code, 200)
        self.assertIn("确认开仓 #", r.text)
        self.assertIn("实际张数", r.text)
        self.assertIn("没填成交金额时用这个</span>", r.text)      # 无 HL 提示语
        self.assertIn("value='85.0'", r.text)                     # 张数口径回显（contracts_raw）
        self.assertIn("填了就按它换算张数", r.text)
        self.assertNotIn("disabled", r.text.split("成交金额 USDT")[1].split(">")[0])
        # 目标风险显示**生效后**比例（开关默认关 = 2%）
        self.assertIn("× 2%", r.text)

    def test_pending_hl_row_renders_by_coin_qty(self):
        r = self._get(self._hl_pending())
        self.assertEqual(r.status_code, 200)
        self.assertIn("确认开仓 #", r.text)
        self.assertIn("数量（币量）", r.text)                     # 标签跟着口径走
        self.assertIn("value='0.00084'", r.text)                 # actual_contracts（币量）
        self.assertIn("HL 仓按币量填写，忽略成交金额", r.text)
        self.assertIn("HL 仓不适用金额换算", r.text)
        self.assertIn("disabled", r.text)                        # 金额输入框被禁用
        self.assertIn("实际成交：入场 <b>83,120.00</b>", r.text)
        # 不能把 MEXC 人工口径的 85「张」当真实量展示
        self.assertNotIn("value='85.0'", r.text)
        self.assertNotIn("张数", r.text)

    # ---- ② 修改页（同一个渲染函数，editing=True 分支）

    def test_edit_form_renders(self):
        pid = self._mexc_open()
        r = self._get(pid, "edit_open")
        self.assertEqual(r.status_code, 200)
        self.assertIn("修改开仓数据 #", r.text)
        self.assertIn("这是修改页", r.text)
        self.assertIn("value='85.0'", r.text)                    # posfields.qty 回显
        self.assertIn("保存修改", r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
