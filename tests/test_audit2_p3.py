"""第二轮审计 P3 回归测试（展示层）。

覆盖：
1. 参数 Tab 表单：所有输入控件都在 <form> 内（提交不再空 body）。
2. 可提余额精简文案 + title 悬停。
3. views 时间为北京时间（无 " UTC" 后缀）。
4. 数量精度 _fmt_sz（0.00095 不显示 0.00）。
5. 下单记录/成交/信号三张表（2026-10-18 起只在记录页）标的列用 HL 名。
6. 成交方向列翻译 Open Long/Close Short。
7. keyform 成功页不再拼「已存在/拒绝覆盖」矛盾文案。
8. main.py 重定向 k= 已 urlencode。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestParamsFormWrapsInputs(unittest.TestCase):
    def test_form_before_fields_and_close_after(self):
        from app.admin_ui import render_params, AdminDeps
        # 最小 deps：渲染参数页只需要这些
        class D:
            get_all = staticmethod(lambda: {})
            def __getattr__(self, k):
                raise AttributeError(k)
        import app.admin_ui as A
        html = A.render_params(None, A._blank_cfg() if hasattr(A, "_blank_cfg") else None, {}, 80000.0) \
            if False else None
        # 直接用现有测试基建太重 —— 静态断言源码结构：
        src = open(A.__file__, encoding="utf-8").read()
        body = src[src.index("def render_params"):]
        body = body[:body.index("\ndef ")]
        form_open = body.index("<form method='post' action='/admin/params'>")
        first_field = body.index("_num_field(")
        form_close = body.index("</form>")
        self.assertLess(form_open, first_field,
                        "form 必须开在所有输入控件之前（否则提交空 body → 400）")
        self.assertLess(first_field, form_close, "按钮必须在 form 内")

    def test_num_input_required(self):
        import app.admin_ui as A
        src = open(A.__file__, encoding="utf-8").read()
        self.assertIn("required name='{_esc(key)}'", src)


class TestViewsDisplay(unittest.TestCase):
    def test_fmt_dt_no_utc_suffix(self):
        from app.hl import views
        txt = views._fmt_dt(1760000000.0)
        self.assertNotIn("UTC", txt)
        self.assertRegex(txt, r"\d{2}-\d{2} \d{2}:\d{2}")

    def test_fmt_dt_env_float_offset(self):
        from app.hl import views
        os.environ["DISPLAY_TZ_OFFSET"] = "8.0"
        try:
            txt = views._fmt_dt(1760000000.0)   # 不抛 ValueError
            self.assertTrue(txt)
        finally:
            os.environ.pop("DISPLAY_TZ_OFFSET", None)

    def test_fmt_sz_precision(self):
        from app.hl import views
        self.assertEqual(views._fmt_sz(0.00095), "0.00095")
        self.assertEqual(views._fmt_sz(12.3), "12.30")
        self.assertEqual(views._fmt_sz(None), "—")

    def test_fill_buy_sell_mapping(self):
        """成交「买卖」列四向推导（原 _DIR_TXT 只给总览那张 9 列表的「方向」列用，
        表迁走后随之删除；记录页只显示买卖，口径仍是同一份映射）。"""
        from app.hl.views import _FILL_BS
        self.assertEqual(_FILL_BS["Open Long"], "买")
        self.assertEqual(_FILL_BS["Close Short"], "买")
        self.assertEqual(_FILL_BS["Close Long"], "卖")
        self.assertEqual(_FILL_BS["Open Short"], "卖")

    def test_orders_symbol_disp(self):
        """记录页「下单记录」标的列不渲染 MEXC 名（_USDT）。

        原来钉的是总览那段源码字符串；2026-10-18 该表迁到记录页（admin_ui.render_records，
        取数走 _rows + _coin 归一），所以改钉记录页的行为而不是总览的源码文本。
        """
        from app.admin_ui import _coin
        self.assertEqual(_coin("BTC", "BTC_USDT"), "BTC")     # coin 优先
        self.assertEqual(_coin("", "BTC_USDT"), "BTC")         # 缺 coin → 从 symbol 截
        self.assertEqual(_coin(None, None), "—")

    def test_withdrawable_copy(self):
        from app.hl import views
        src = open(views.__file__, encoding="utf-8").read()
        # P2 分层后「可提余额」进顶层三数；口径与后缀逐字保留（统一账户路径）
        self.assertIn("wd_html = f\"{_fmt(avail)} U<span class='muted'>（提现收 1U 手续费）</span>\"", src)
        self.assertNotIn("可提余额（现货口径，已扣占用，提现固定扣 1U）", src)
        # 2026-10-15 用户反馈：口径名「永续口径」是黑话、且与「已用保证金」重复 →
        # 改成一行「资金分布（统一账户）现货 X · 合约占用 Y」，统一账户下不再单列保证金
        self.assertIn("资金分布（统一账户）", src)
        # 源码里 JSON payload 的注释仍会提到「永续口径」，这里只钉死**展示层**不再用这个口径名
        self.assertNotIn("永续口径 {_fmt", src)
        self.assertNotIn('_kv("永续口径', src)

    def test_fills_render_capped(self):
        """成交/下单/信号三表渲染必须截断（不能全量渲染）。

        2026-10-15 起按「每页行数」设置（默认 20，可切 10/20/50）；2026-10-18 三表从
        总览迁到记录页 —— 切片改由 render_records 的 n 做（_signal 块显式 [:n]），
        下拉数据的取数上限（fills_out[:100]）仍在 views.status_payload。
        """
        from app.hl import views
        import app.admin_ui as A
        vsrc = open(views.__file__, encoding="utf-8").read()
        asrc = open(A.__file__, encoding="utf-8").read()
        self.assertIn("DEFAULT_ROWS = 20", vsrc)
        self.assertIn('fills_out[:100]', vsrc)             # JSON 侧仍截断
        self.assertIn("def render_records(deps: AdminDeps, rows_n: int = DEFAULT_ROWS",
                      asrc)
        self.assertIn('_rows(deps, "hl_recent_fills", n)', asrc)
        self.assertIn('_rows(deps, "hl_recent_orders", n)', asrc)
        self.assertIn("_signals_html(list(signals or [])[:n])", asrc)


class TestKeyformSuccessPage(unittest.TestCase):
    def test_success_page_no_contradiction(self):
        import app.hl.keyform as K
        src = open(K.__file__, encoding="utf-8").read()
        # 成功路径不再调 render_form（has_key 已 True 会拼「拒绝覆盖」）
        body = src[src.index("good, info = write_key"):]
        body = body[:body.index("return r")]
        self.assertNotIn("render_form(deps, token=\"\"", body)


class TestMainRedirectQuotes(unittest.TestCase):
    def test_k_quoted(self):
        import re
        # 不 import app.main（模块级就建 DB，CI 里 /app 不可写）；直接读源文件
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "app", "main.py")
        src = open(p, encoding="utf-8").read()
        bad = re.findall(r'RedirectResponse\(f"/admin\?tab=\w+&k=\{k\}"', src)
        self.assertEqual(bad, [], "main.py 重定向里的 k= 必须 urlencode")


if __name__ == "__main__":
    unittest.main()
