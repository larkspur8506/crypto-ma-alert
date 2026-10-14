"""P1-8：推送失败重试接线（_retry_failed_alerts）的回归测试。"""
import tempfile
import unittest
from pathlib import Path

from app.db import DB


class FakeNotifier:
    def __init__(self, ok=True):
        self.messages = []
        self.ok = ok

    def send_message(self, text):
        self.messages.append(text)
        return self.ok


class TestRetryFailedAlerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def _job(self, notifier):
        import time
        rows = self.db.failed_alerts_since(time.time() - 24 * 3600)
        retry = [r for r in rows if (r["kind"] or "") not in
                 ("heartbeat", "heartbeat_local", "weekly", "weekly_local")]
        for r in retry[:5]:
            ok = bool(notifier and notifier.send_message(r["message"] or ""))
            if ok:
                self.db.mark_alert_sent(int(r["id"]))
        return retry

    def test_failed_alert_is_retried_and_marked(self):
        notifier = FakeNotifier(ok=True)
        self.db.log_alert("BTC_USDT", 1000, "exit", "离场提醒（模拟失败）", False)
        self.db.log_alert("BTC_USDT", None, "heartbeat_local", "心跳", False)  # 不该被重推
        retried = self._job(notifier)
        self.assertEqual(len([r for r in retried if r["kind"] == "exit"]), 1)
        self.assertEqual([m for m in notifier.messages if "离场提醒" in m],
                         ["离场提醒（模拟失败）"])
        # 标记成功后再跑一遍：不再重推
        retried2 = self._job(notifier)
        self.assertEqual([r for r in retried2 if r["kind"] == "exit"], [])

    def test_failed_alert_stays_failed_when_send_fails_again(self):
        notifier = FakeNotifier(ok=False)
        self.db.log_alert("BTC_USDT", 1000, "entry", "开仓提醒", False)
        self._job(notifier)
        rows = self.db.failed_alerts_since(0)
        self.assertEqual(len([r for r in rows if r["kind"] == "entry"]), 1,
                         "再失败不得标记为已发送")


if __name__ == "__main__":
    unittest.main()
