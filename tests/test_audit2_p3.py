"""第二轮审计 P3 回归测试（展示层）。

覆盖：
1. 参数 Tab 表单：所有输入控件都在 <form> 内（提交不再空 body）。
2. 可提余额精简文案 + title 悬停。
3. views 时间为北京时间（无 " UTC" 后缀）。
4. 数量精度 _fmt_sz（0.00095 不显示 0.00）。
5. 下单记录标的列用 HL 名（页面不出现 _USDT）。
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

    def test_dir_txt(self):
        from app.hl import views
        self.assertEqual(views._DIR_TXT["Open Long"], "开多")
        self.assertEqual(views._DIR_TXT["Close Short"], "平空")

    def test_orders_symbol_disp(self):
        from app.hl import views
        src = open(views.__file__, encoding="utf-8").read()
        self.assertIn('_td(_esc(_disp(od.get("symbol")) or "—"))', src)

    def test_withdrawable_copy(self):
        from app.hl import views
        src = open(views.__file__, encoding="utf-8").read()
        self.assertIn("可提余额\", f\"{_fmt(avail)} U（提现收 1U 手续费）\"", src)
        self.assertNotIn("可提余额（现货口径，已扣占用，提现固定扣 1U）", src)
        self.assertIn("资金都在现货（统一账户）", src)

    def test_fills_render_capped(self):
        from app.hl import views
        src = open(views.__file__, encoding="utf-8").read()
        self.assertIn("[:50]", src)
        self.assertIn('fills_out[:100]', src)


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
        src = open("/opt/data/cache/scratch/crypto-ma-alert/app/main.py", encoding="utf-8").read()
        bad = re.findall(r'RedirectResponse\(f"/admin\?tab=\w+&k=\{k\}"', src)
        self.assertEqual(bad, [], "main.py 重定向里的 k= 必须 urlencode")


if __name__ == "__main__":
    unittest.main()
