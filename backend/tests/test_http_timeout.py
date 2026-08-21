import unittest
from unittest.mock import patch

from utils.base_system import DEFAULT_TIMEOUT, TimeoutSession


class TimeoutSessionTests(unittest.TestCase):
    """
    requests 默认不设超时。并发抢座时，一个不响应的上游会把线程永久挂住，
    线程池 join 不完，当天的定时任务就再也结束不了——后面几天的抢座也跟着停摆。
    """

    def test_default_timeout_is_injected(self):
        with patch("requests.Session.request") as request:
            TimeoutSession().get("https://example.test/seat")
        self.assertEqual(request.call_args[1]["timeout"], DEFAULT_TIMEOUT)

    def test_post_also_gets_a_timeout(self):
        with patch("requests.Session.request") as request:
            TimeoutSession().post("https://example.test/reserve", json={"seat": 1})
        self.assertEqual(request.call_args[1]["timeout"], DEFAULT_TIMEOUT)

    def test_explicit_timeout_wins(self):
        with patch("requests.Session.request") as request:
            TimeoutSession().get("https://example.test/seat", timeout=5)
        self.assertEqual(request.call_args[1]["timeout"], 5)

    def test_timeout_is_a_connect_read_pair(self):
        connect, read = DEFAULT_TIMEOUT
        self.assertGreater(connect, 0)
        self.assertGreater(read, 0)


if __name__ == "__main__":
    unittest.main()
