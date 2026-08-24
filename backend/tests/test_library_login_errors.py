import unittest
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

# 这些测试只覆盖登录响应的错误分类，不依赖数据库、VPN 或真实加密库。
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
fake_apscheduler_background.BackgroundScheduler = Mock
sys.modules.setdefault("apscheduler", fake_apscheduler)
sys.modules.setdefault("apscheduler.schedulers", fake_apscheduler_schedulers)
sys.modules.setdefault("apscheduler.schedulers.background", fake_apscheduler_background)

from utils.library_system import (
    LibraryCredentialsError,
    LibraryLoginError,
    LibrarySystem,
    _is_transient_login_error,
    register_arrival_check,
)
from scheduled_task import check_arrival_after_grace


class LibraryLoginErrorTests(unittest.TestCase):
    def make_library(self, response):
        library = LibrarySystem.__new__(LibrarySystem)
        library.username = "12345678"
        library.password = "secret"
        library.login_url = "https://example.test/login"
        library.session = Mock()
        library.session.post.return_value = response
        return library

    @patch("utils.library_system.PasswordEncryptor.set_public_key", return_value="key")
    @patch("utils.library_system.PasswordEncryptor.encrypt_with_public_key", return_value="encrypted")
    def test_code_301_keeps_server_authentication_error(self, _encrypt, _set_key):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 301, "message": "认证失败，请联系管理员"}
        library = self.make_library(response)

        with self.assertRaises(LibraryLoginError) as raised:
            library._perform_login("public-key", "nonce")

        self.assertNotIsInstance(raised.exception, LibraryCredentialsError)
        self.assertEqual(str(raised.exception), "code 301：认证失败，请联系管理员")
        self.assertFalse(raised.exception.is_credentials_error)

    @patch("utils.library_system.PasswordEncryptor.set_public_key", return_value="key")
    @patch("utils.library_system.PasswordEncryptor.encrypt_with_public_key", return_value="encrypted")
    def test_explicit_password_error_is_classified_as_credentials_error(self, _encrypt, _set_key):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 400, "message": "用户名或密码错误"}
        library = self.make_library(response)

        with self.assertRaises(LibraryCredentialsError) as raised:
            library._perform_login("public-key", "nonce")

        self.assertTrue(raised.exception.is_credentials_error)

    @patch("utils.library_system.PasswordEncryptor.set_public_key", return_value="key")
    @patch("utils.library_system.PasswordEncryptor.encrypt_with_public_key", return_value="encrypted")
    def test_http_error_is_not_classified_as_password_error(self, _encrypt, _set_key):
        library = self.make_library(Mock(status_code=503))

        with self.assertRaises(LibraryLoginError) as raised:
            library._perform_login("public-key", "nonce")

        self.assertFalse(raised.exception.is_credentials_error)
        self.assertEqual(str(raised.exception), "登录请求失败：HTTP 503")


class LibraryLoginRetryTests(unittest.TestCase):
    def make_library(self):
        library = LibrarySystem.__new__(LibrarySystem)
        library.username = "12345678"
        library.user_info = None
        return library

    def test_system_busy_is_retried_with_exponential_delay(self):
        library = self.make_library()
        attempts = iter([False, False, True])

        def login():
            result = next(attempts)
            if not result:
                library._sso_error = "SSO后仍未登录: 系统繁忙，请稍后重试"
            return result

        library._login_via_cas_sso = Mock(side_effect=login)

        with patch("utils.library_system.LIBRARY_LOGIN_MAX_ATTEMPTS", 3), \
             patch("utils.library_system.LIBRARY_LOGIN_RETRY_DELAY_SECONDS", 2), \
             patch("utils.library_system.time.sleep") as sleep:
            library._initialize_login()

        self.assertEqual(library._login_via_cas_sso.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_permanent_sso_error_falls_back_without_retry(self):
        library = self.make_library()
        library._login_via_cas_sso = Mock(return_value=False)
        library._sso_error = "SSO入口参数无效"
        library._get_initial_cookie = Mock(return_value=False)

        with patch("utils.library_system.LIBRARY_LOGIN_MAX_ATTEMPTS", 3), \
             patch("utils.library_system.time.sleep") as sleep:
            with self.assertRaisesRegex(Exception, "SSO入口参数无效"):
                library._initialize_login()

        library._login_via_cas_sso.assert_called_once_with()
        sleep.assert_not_called()

    def test_transient_error_classifier(self):
        self.assertTrue(_is_transient_login_error("系统繁忙，请稍后重试"))
        self.assertTrue(_is_transient_login_error("SSO跳转返回HTTP 503"))
        self.assertFalse(_is_transient_login_error("用户名或密码错误"))


class ArrivalCheckRegistrationTests(unittest.TestCase):
    @patch("utils.library_system.register_arrival_check")
    def test_success_response_registers_arrival_check(self, register):
        response = Mock(status_code=200, text="ok")
        response.json.return_value = {
            "code": 0,
            "data": {
                "uuid": "reservation-uuid",
                "resvName": "测试用户",
                "resvDevInfoList": [{"devName": "2F-A001"}],
            },
        }
        library = LibrarySystem.__new__(LibrarySystem)
        library.username = "12345678"
        library.reserve_url = "https://example.test/reserve"
        library.session = Mock()
        library.session.post.return_value = response
        library.get_seat_name_by_id = Mock(return_value="2F-A001")

        result = library._reserve_single_seat(
            {"pid": "12345678", "accNo": 1},
            "seat-id",
            "2026-08-17 08:00:00",
            "2026-08-17 22:00:00",
        )

        self.assertEqual(result, "✅ 08-17 · 08:00-22:00 · 2F-A001 · 预约成功")
        self.assertNotIn("测试用户", result)
        register.assert_called_once_with(
            "12345678",
            "reservation-uuid",
            "2F-A001",
            "2026-08-17 08:00:00",
            "2026-08-17 22:00:00",
        )

    @patch("utils.library_system.db")
    def test_successful_reservation_is_checked_32_minutes_after_begin(self, db):
        register_arrival_check(
            "12345678",
            "reservation-uuid",
            "2F-A001",
            "2026-08-17 08:00:00",
            "2026-08-17 22:00:00",
        )

        query, update = db.arrival_checks.update_one.call_args.args[:2]
        self.assertEqual(query, {"uuid": "reservation-uuid"})
        self.assertEqual(update["$set"]["status"], "pending")
        self.assertEqual(
            update["$set"]["check_at"],
            datetime(2026, 8, 17, 8, 0) + timedelta(minutes=32),
        )
        self.assertEqual(
            update["$set"]["target_time"],
            "2026-08-17 08:00:00-22:00:00",
        )

    @patch("scheduled_task._record_visit_log")
    @patch("scheduled_task.LibrarySystem")
    @patch("scheduled_task.user_config_info")
    @patch("scheduled_task.db")
    def test_arrived_user_adds_learning_time(
        self, db, user_config, library_system, record_visit
    ):
        user_config.find_one.return_value = {
            "vpn_password": "encrypted-password",
            "verified": True,
        }
        library_system.return_value.get_reservation_info.return_value = ([{
            "uuid": "reservation-uuid",
            "resvStatus": 1093,
            "resvBeginTime": "2026-08-17 08:00:00",
            "resvEndTime": "2026-08-17 22:00:00",
            "devInfo": {"devName": "2F-A001"},
        }], "查询成功")
        check = {
            "pid": "12345678",
            "uuid": "reservation-uuid",
            "seat_name": "2F-A001",
            "target_time": "2026-08-17 08:00:00-22:00:00",
        }

        check_arrival_after_grace(check, now=datetime(2026, 8, 17, 8, 32))

        record_visit.assert_called_once_with(
            "12345678",
            "reservation-uuid",
            "2026-08-17 08:00:00-22:00:00",
            "2F-A001",
        )
        finish_update = db.arrival_checks.update_one.call_args.args[1]
        self.assertEqual(finish_update["$set"]["status"], "arrived")

    @patch("scheduled_task.LibrarySystem")
    @patch("scheduled_task.user_config_info")
    @patch("scheduled_task.db")
    def test_missing_reservation_removes_learning_time(
        self, db, user_config, library_system
    ):
        user_config.find_one.return_value = {
            "vpn_password": "encrypted-password",
            "verified": True,
        }
        library_system.return_value.get_reservation_info.return_value = (
            [], "无预约记录"
        )
        check = {
            "pid": "12345678",
            "uuid": "reservation-uuid",
            "seat_name": "2F-A001",
            "target_time": "2026-08-17 08:00:00-22:00:00",
        }

        check_arrival_after_grace(check, now=datetime(2026, 8, 17, 8, 32))

        db.visit_logs.delete_one.assert_called_once_with(
            {"uuid": "reservation-uuid"}
        )
        finish_update = db.arrival_checks.update_one.call_args.args[1]
        self.assertEqual(finish_update["$set"]["status"], "violated")
        self.assertIn("不计学习时间", finish_update["$set"]["message"])


if __name__ == "__main__":
    unittest.main()
