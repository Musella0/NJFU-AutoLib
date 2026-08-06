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


if __name__ == "__main__":
    unittest.main()
