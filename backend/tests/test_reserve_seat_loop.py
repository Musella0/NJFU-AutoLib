"""reserve_seat 逐座循环里的两个补丁。

2026-09-11 早上一个账号三张备选座位全烧掉：第一张撞上「当前设备正在被预约」，
0.1 秒后发第二张，图书馆的账号级锁还没放开，第二、三张都被「您有预约操作正在进行」
顶回去了。另外 7:00 为了省一个来回不再校验预登录会话，会话真死了得能现场重登。
"""

import sys
import types
import unittest
from unittest.mock import Mock, patch

fake_pymongo = types.ModuleType("pymongo")
fake_pymongo.MongoClient = Mock(return_value=Mock())
fake_pymongo.ASCENDING = 1
fake_pymongo.DESCENDING = -1
sys.modules.setdefault("pymongo", fake_pymongo)

fake_config = types.ModuleType("utils.config")
fake_config.get_mongo_uri = Mock(return_value="mongodb://test")
fake_config.LOG_FILE = "/tmp/autolib-test.log"
sys.modules.setdefault("utils.config", fake_config)

fake_encryptor_module = types.ModuleType("utils.password_encryptor")


class FakePasswordEncryptor:
    set_public_key = Mock(return_value="key")
    encrypt_with_public_key = Mock(return_value="encrypted")


fake_encryptor_module.PasswordEncryptor = FakePasswordEncryptor
sys.modules.setdefault("utils.password_encryptor", fake_encryptor_module)

fake_vpn_module = types.ModuleType("utils.vpn_system")
fake_vpn_module.VPNSystem = Mock()
sys.modules.setdefault("utils.vpn_system", fake_vpn_module)

fake_apscheduler = types.ModuleType("apscheduler")
fake_apscheduler_schedulers = types.ModuleType("apscheduler.schedulers")
fake_apscheduler_background = types.ModuleType("apscheduler.schedulers.background")
fake_apscheduler_background.BackgroundScheduler = Mock()
sys.modules.setdefault("apscheduler", fake_apscheduler)
sys.modules.setdefault("apscheduler.schedulers", fake_apscheduler_schedulers)
sys.modules.setdefault("apscheduler.schedulers.background", fake_apscheduler_background)

from utils import library_system  # noqa: E402
from utils.library_system import LibrarySystem  # noqa: E402

BUSY = "座位 2F-B070(100455871) 期望预约时间x 预约失败: 您有预约操作正在进行，请稍后操作"
TAKEN = "座位 2F-B070(100455871) 期望预约时间x 预约失败: 设备在该时间段内已被预约"
OK = "✅ 09-12 · 08:00-22:00 · 2F-B070 · 预约成功"
AUTH = f"座位 2F-B070(100455871) 请求失败: {library_system.AUTH_FAILURE_MARK}（返回登录页）"


class ReserveSeatLoopTests(unittest.TestCase):
    def make_library(self, responses):
        library = LibrarySystem.__new__(LibrarySystem)
        library.username = "12345678"
        library.user_info = {"accNo": "1"}
        library.last_reservation = None
        library.get_seat_name_by_id = lambda seat_id: f"seat-{seat_id}"
        library.ensure_login = lambda: library.user_info
        library._reserve_single_seat = Mock(side_effect=list(responses))
        return library

    def _reserve(self, library, seats=("100455871", "100455873")):
        with patch.object(library_system.time, "sleep") as sleep:
            message, _ = library.reserve_seat(
                seat_list=list(seats),
                resv_begin_time="2026-09-12 08:00:00",
                resv_end_time="2026-09-12 22:00:00",
            )
        return message, sleep

    def test_account_busy_retries_the_same_seat_after_a_pause(self):
        library = self.make_library([BUSY, OK])

        message, sleep = self._reserve(library)

        self.assertIn("预约成功", message)
        tried = [call.args[1] for call in library._reserve_single_seat.call_args_list]
        self.assertEqual(tried, ["100455871", "100455871"], "锁没放开时要打同一张，不能跳下一张")
        sleep.assert_called_once_with(library_system.BUSY_RETRY_DELAY_SECONDS)

    def test_account_busy_gives_up_on_that_seat_after_max_retries(self):
        library = self.make_library([BUSY] * (library_system.BUSY_RETRY_MAX + 1) + [OK])

        message, _ = self._reserve(library)

        self.assertIn("预约成功", message)
        tried = [call.args[1] for call in library._reserve_single_seat.call_args_list]
        self.assertEqual(tried[-1], "100455873", "重试用完才换下一张")

    def test_seat_taken_moves_on_immediately(self):
        library = self.make_library([TAKEN, OK])

        message, sleep = self._reserve(library)

        self.assertIn("预约成功", message)
        sleep.assert_not_called()
        tried = [call.args[1] for call in library._reserve_single_seat.call_args_list]
        self.assertEqual(tried, ["100455871", "100455873"])

    def test_dead_session_relogins_once_and_retries_the_same_seat(self):
        library = self.make_library([AUTH, OK])

        def relogin():
            library.user_info = {"accNo": "2"}

        library._initialize_login = Mock(side_effect=relogin)

        message, _ = self._reserve(library)

        self.assertIn("预约成功", message)
        library._initialize_login.assert_called_once()
        tried = [call.args[1] for call in library._reserve_single_seat.call_args_list]
        self.assertEqual(tried, ["100455871", "100455871"])
        # 重登后的 user_info 要用在重试那一发上
        self.assertEqual(library._reserve_single_seat.call_args_list[1].args[0], {"accNo": "2"})

    def test_second_auth_failure_is_not_retried_forever(self):
        library = self.make_library([AUTH, AUTH, TAKEN])
        library._initialize_login = Mock(
            side_effect=lambda: setattr(library, "user_info", {"accNo": "2"}))

        message, _ = self._reserve(library)

        self.assertIn("所有座位预约失败", message)
        library._initialize_login.assert_called_once()


class AuthFailureDetectionTests(unittest.TestCase):
    """_post_reservation 得把「会话没了」跟「座位没了」区分开。"""

    def make_library(self, response):
        library = LibrarySystem.__new__(LibrarySystem)
        library.username = "12345678"
        library.reserve_url = "https://x/reserve"
        library.session = Mock()
        library.session.post.return_value = response
        return library

    def _post(self, library):
        return library._post_reservation(
            {"accNo": "1"}, "100455871", "2F-B070", "2026-09-12 08:00:00", "2026-09-12 22:00:00"
        )

    def test_login_page_html_counts_as_auth_failure(self):
        response = Mock(status_code=200, text="<html><title>统一身份认证</title>")
        response.json.side_effect = ValueError("no json")

        self.assertTrue(library_system._is_auth_failure(self._post(self.make_library(response))))

    def test_unauthorized_status_counts_as_auth_failure(self):
        response = Mock(status_code=401, text="")
        self.assertTrue(library_system._is_auth_failure(self._post(self.make_library(response))))

    def test_seat_taken_is_not_an_auth_failure(self):
        response = Mock(status_code=200, text="{}")
        response.json.return_value = {"code": 1, "message": "设备在该时间段内已被预约"}

        message = self._post(self.make_library(response))
        self.assertFalse(library_system._is_auth_failure(message))
        self.assertIn("已被预约", message)


if __name__ == "__main__":
    unittest.main()
