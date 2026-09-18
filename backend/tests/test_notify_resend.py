import unittest
from unittest.mock import Mock, patch

from utils import notify


class ResendNotificationTests(unittest.TestCase):
    @patch("utils.notify.requests.post")
    def test_resend_is_preferred_when_api_key_is_configured(self, post):
        post.return_value = Mock(status_code=200)
        with patch.object(notify, "RESEND_API_KEY", "test-key"), patch.object(
            notify,
            "RESEND_FROM",
            "AutoLib <onboarding@resend.dev>",
        ):
            sent = notify.send_email(
                "owner@example.net",
                "Password reset",
                "123456",
            )

        self.assertTrue(sent)
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["to"], ["owner@example.net"])
        self.assertEqual(kwargs["json"]["text"], "123456")
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("Bearer "))

    @patch("utils.notify.requests.post")
    def test_resend_http_error_is_reported_as_failure(self, post):
        post.return_value = Mock(status_code=403)
        with patch.object(notify, "RESEND_API_KEY", "test-key"):
            self.assertFalse(
                notify.send_email("owner@example.net", "Subject", "Body")
            )


class NotifyModeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("utils.notify.queue_email")
        self.queue_email = patcher.start()
        self.addCleanup(patcher.stop)

    def test_simple_mode_drops_success_notifications(self):
        notify.notify_user(
            {"pid": "123", "notify_email": "owner@example.net", "notify_mode": "simple"},
            "✅ 预约完成",
            "座位与时间",
        )
        self.queue_email.assert_not_called()

    def test_simple_mode_still_sends_failures(self):
        notify.notify_user(
            {"pid": "123", "notify_email": "owner@example.net", "notify_mode": "simple"},
            "❌ 预约失败",
            "没抢到",
            always=True,
        )
        self.queue_email.assert_called_once_with(
            "owner@example.net", "❌ 预约失败", "没抢到"
        )

    def test_full_mode_sends_success_notifications(self):
        notify.notify_user(
            {"pid": "123", "notify_email": "owner@example.net", "notify_mode": "full"},
            "✅ 预约完成",
            "座位与时间",
        )
        self.queue_email.assert_called_once_with(
            "owner@example.net", "✅ 预约完成", "座位与时间"
        )

    def test_accounts_without_a_mode_only_get_failures(self):
        config = {"pid": "123", "notify_email": "owner@example.net"}
        notify.notify_user(config, "✅ 预约完成", "座位与时间")
        self.queue_email.assert_not_called()
        notify.notify_user(config, "❌ 预约失败", "没抢到", always=True)
        self.assertEqual(self.queue_email.call_count, 1)

    def test_missing_email_skips_every_mode(self):
        notify.notify_user(
            {"pid": "123", "notify_email": "", "notify_mode": "full"},
            "❌ 预约失败",
            "没抢到",
            always=True,
        )
        self.queue_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
