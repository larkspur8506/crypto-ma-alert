"""告警文本卫生：SDK 异常携带整页 HTML 时不得泄进推送与看守。

2026-11-01 Lighter 升级窗口的实测在另一系统（lighter-ma-bot）先暴露了这类泄漏；
本仓库同构路径（exchange 的 `_err(f"…异常：{e}")` → `error` 列 → 执行器告警 /
watchdog 推送）一并加卫生。
"""

import unittest

from app.hl.exchange import _clean_alert_text, _exc_text

MAINTENANCE_PAGE = (
    "<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n"
    "<body><center><h1>502 Bad Gateway</h1></center></body>\r\n</html>"
)


class ExcTextTests(unittest.TestCase):

    def test_html_page_collapses_to_title(self):
        text = _exc_text(RuntimeError(f"下单异常：(502)\n{MAINTENANCE_PAGE}"))
        self.assertEqual(text, "HTML 错误页：502 Bad Gateway")

    def test_plain_exception_is_collapsed_and_truncated(self):
        text = _exc_text(RuntimeError("line one\nline two\n" + "z" * 900))
        self.assertFalse("\n" in text)
        self.assertEqual(len(text), 301)

    def test_accepts_plain_strings(self):
        self.assertEqual(_exc_text("初始化 SDK 失败：boom"),
                         "初始化 SDK 失败：boom")


class CleanAlertTextTests(unittest.TestCase):

    def test_multiline_alert_text_is_untouched(self):
        text = "【HL执行】⚠️ SOL 止损单丢失\n已自动补挂"
        self.assertEqual(_clean_alert_text(text), text)

    def test_html_inside_alert_is_reduced(self):
        cleaned = _clean_alert_text(
            f"【HL执行】🛑 SOL 补挂失败（下单异常：{MAINTENANCE_PAGE}）")
        self.assertIn("HTML 错误页：502 Bad Gateway", cleaned)
        self.assertNotIn("<html", cleaned.lower())

    def test_overlong_text_is_truncated(self):
        self.assertEqual(len(_clean_alert_text("y" * 5000)), 801)


if __name__ == "__main__":
    unittest.main()
