import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

# 和 test_concurrent_reservation 同一套桩件：不碰数据库、VPN 和真实网络。
fake_pymongo = types.ModuleType("pymongo")
fake_pymongo.MongoClient = Mock(return_value=Mock())
fake_pymongo.ASCENDING = 1
fake_pymongo.DESCENDING = -1
sys.modules.setdefault("pymongo", fake_pymongo)

fake_apscheduler = types.ModuleType("apscheduler")
fake_schedulers = types.ModuleType("apscheduler.schedulers")
fake_background = types.ModuleType("apscheduler.schedulers.background")
fake_background.BackgroundScheduler = Mock()
sys.modules.setdefault("apscheduler", fake_apscheduler)
sys.modules.setdefault("apscheduler.schedulers", fake_schedulers)
sys.modules.setdefault("apscheduler.schedulers.background", fake_background)

fake_config = types.ModuleType("utils.config")
fake_config.get_mongo_uri = Mock(return_value="mongodb://test")
fake_config.LOG_FILE = os.path.join(tempfile.gettempdir(), "autolib-test", "auto_lib.log")
sys.modules.setdefault("utils.config", fake_config)

fake_vpn_module = types.ModuleType("utils.vpn_system")
fake_vpn_module.VPNSystem = Mock()
sys.modules.setdefault("utils.vpn_system", fake_vpn_module)

fake_encryptor_module = types.ModuleType("utils.password_encryptor")


class FakePasswordEncryptor:
    set_public_key = Mock(return_value="key")
    encrypt_with_public_key = Mock(return_value="encrypted")
    aes_encrypt_password = Mock(return_value="encrypted")


fake_encryptor_module.PasswordEncryptor = FakePasswordEncryptor
sys.modules.setdefault("utils.password_encryptor", fake_encryptor_module)

os.environ.setdefault("ENCRYPTION_KEY", "00" * 32)

import scheduled_task  # noqa: E402
from utils.account_config import CREDENTIAL_INVALID_RESULT  # noqa: E402
from utils.library_system import LibraryCredentialsError, LibrarySystem  # noqa: E402

# utils.vpn_system 在 sys.modules 里被换成了桩，真实模块单独加载来测分类函数。
# 它顶层 import bs4，测试机不一定装了，塞个空桩进去（分类函数本身不用它）。
if "bs4" not in sys.modules:
    fake_bs4 = types.ModuleType("bs4")
    fake_bs4.BeautifulSoup = Mock()
    sys.modules["bs4"] = fake_bs4
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_vpn_system",
    os.path.join(os.path.dirname(__file__), "..", "utils", "vpn_system.py"),
)
real_vpn_system = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_vpn_system)


class VpnRejectionClassifierTests(unittest.TestCase):
    """只有 CAS 明说「密码不对」才算密码被拒，别把锁定、验证码当成改密码。"""

    def test_cas_wording_counts_as_rejection(self):
        self.assertTrue(real_vpn_system.is_credentials_rejected_message("您提供的用户名或者密码有误"))
        self.assertTrue(real_vpn_system.is_credentials_rejected_message("用户名或密码错误"))
        self.assertTrue(real_vpn_system.is_credentials_rejected_message("密码 不正确"))

    def test_other_failures_do_not_count(self):
        self.assertFalse(real_vpn_system.is_credentials_rejected_message("账号已被锁定，请 30 分钟后再试"))
        self.assertFalse(real_vpn_system.is_credentials_rejected_message("请输入验证码"))
        self.assertFalse(real_vpn_system.is_credentials_rejected_message(""))
        self.assertFalse(real_vpn_system.is_credentials_rejected_message(None))


class InitializeVpnTests(unittest.TestCase):
    def make_library(self):
        library = LibrarySystem.__new__(LibrarySystem)
        library.username = "12345678"
        library.session = Mock()
        return library

    def test_rejected_password_raises_credentials_error(self):
        vpn = Mock()
        vpn.vpn_login.return_value = False
        vpn.credentials_rejected = True
        vpn.last_error = "您提供的用户名或者密码有误"
        library = self.make_library()
        with patch("utils.library_system.VPNSystem", return_value=vpn):
            with self.assertRaises(LibraryCredentialsError) as raised:
                library._initialize_vpn("pwd")
        self.assertTrue(raised.exception.is_credentials_error)
        self.assertIn("密码有误", str(raised.exception))

    def test_other_vpn_failure_is_plain_exception(self):
        vpn = Mock()
        vpn.vpn_login.return_value = False
        vpn.credentials_rejected = False
        library = self.make_library()
        with patch("utils.library_system.VPNSystem", return_value=vpn):
            with self.assertRaises(Exception) as raised:
                library._initialize_vpn("pwd")
        self.assertFalse(getattr(raised.exception, "is_credentials_error", False))


class FakeCollection:
    """够 login_library 用的最小 user_config_info：单文档、支持 $inc/$set。"""

    def __init__(self, doc):
        self.doc = doc

    def _matches(self, flt):
        for key, cond in flt.items():
            value = self.doc.get(key)
            if isinstance(cond, dict) and "$gt" in cond:
                if not (value is not None and value > cond["$gt"]):
                    return False
            elif value != cond:
                return False
        return True

    def update_one(self, flt, update):
        result = Mock()
        if not self._matches(flt):
            result.matched_count = 0
            return result
        for key, delta in update.get("$inc", {}).items():
            self.doc[key] = self.doc.get(key, 0) + delta
        self.doc.update(update.get("$set", {}))
        result.matched_count = 1
        return result

    def find_one(self, flt, projection=None):
        return dict(self.doc) if self._matches(flt) else None


def credentials_error():
    return LibraryCredentialsError("VPN登录失败：您提供的用户名或者密码有误")


class LoginLibraryTests(unittest.TestCase):
    def setUp(self):
        self.doc = {"pid": "2310104113", "vpn_password": "enc", "verified": True,
                    "notify_email": "x@example.com"}
        self.col = FakeCollection(self.doc)
        patches = [
            patch.object(scheduled_task, "user_config_info", self.col),
            patch.object(scheduled_task, "_dec", return_value="pwd"),
            patch.object(scheduled_task, "notify_user"),
            patch.object(scheduled_task.prelogin, "discard"),
            patch.object(scheduled_task, "CREDENTIAL_FAILURE_THRESHOLD", 2),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_first_rejection_only_counts(self):
        with patch.object(scheduled_task, "LibrarySystem", side_effect=credentials_error()):
            with self.assertRaises(LibraryCredentialsError):
                scheduled_task.login_library(self.doc)
        self.assertEqual(self.doc["credential_failures"], 1)
        self.assertTrue(self.doc["verified"])
        scheduled_task.notify_user.assert_not_called()

    def test_second_rejection_flips_verified_and_notifies_once(self):
        with patch.object(scheduled_task, "LibrarySystem", side_effect=credentials_error()):
            for _ in range(3):
                with self.assertRaises(LibraryCredentialsError):
                    scheduled_task.login_library(self.doc)
        self.assertEqual(self.doc["credential_failures"], 3)
        self.assertFalse(self.doc["verified"])
        self.assertEqual(self.doc["result"], CREDENTIAL_INVALID_RESULT)
        self.assertIn("credential_invalid_at", self.doc)
        # 第 2 次翻 verified 并通知；第 3 次已经是 False，不再重复骚扰。
        self.assertEqual(scheduled_task.notify_user.call_count, 1)
        self.assertTrue(scheduled_task.notify_user.call_args.kwargs.get("always"))
        scheduled_task.prelogin.discard.assert_called_once_with("2310104113")

    def test_success_resets_counter(self):
        self.doc["credential_failures"] = 1
        with patch.object(scheduled_task, "LibrarySystem", return_value=Mock()):
            scheduled_task.login_library(self.doc)
        self.assertEqual(self.doc["credential_failures"], 0)
        self.assertTrue(self.doc["verified"])

    def test_transient_failure_does_not_count(self):
        with patch.object(scheduled_task, "LibrarySystem", side_effect=RuntimeError("VPN登录失败")):
            with self.assertRaises(RuntimeError):
                scheduled_task.login_library(self.doc)
        self.assertNotIn("credential_failures", self.doc)
        self.assertTrue(self.doc["verified"])

    def test_seat_config_is_never_touched(self):
        """红线：座位、优先级、时段这些用户配置系统不许写。"""
        self.doc.update({"seat_list": ["2F-B001"], "priority": 3, "time": {"k": "v"}})
        with patch.object(scheduled_task, "LibrarySystem", side_effect=credentials_error()):
            for _ in range(2):
                with self.assertRaises(LibraryCredentialsError):
                    scheduled_task.login_library(self.doc)
        self.assertEqual(self.doc["seat_list"], ["2F-B001"])
        self.assertEqual(self.doc["priority"], 3)
        self.assertEqual(self.doc["time"], {"k": "v"})
        self.assertFalse(self.doc["verified"])


if __name__ == "__main__":
    unittest.main()
