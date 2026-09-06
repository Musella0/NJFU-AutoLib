"""本地数据一律以「登录学号」归属，不能用图书馆返回的内部人员 ID。

背景：图书馆登录响应里的 user_info['pid'] 是它自己的内部人员 ID，多数用户恰好
和学号相同，所以拿它当主键很久都没露馅。直到有个学号 202312981 的用户，图书馆
那边的 pid 是 2310306109——他的 owned_seat 和到馆复查全被写到一个不存在的账号下，
复查时找不到凭据直接判 failed，迟到保护和学习记录静默失效。
这组测试专门盯住「两者不相等」的情况。
"""
import sys
import types
import unittest
from unittest.mock import Mock, patch

# 与其他用例一致：不碰数据库、VPN 和真实加密库。
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

from utils.library_system import LibrarySystem  # noqa: E402

# 真实出过问题的那一对：左边是登录学号，右边是图书馆内部 pid。
LOGIN_ID = "202312981"
LIBRARY_PID = "2310306109"


def _build_system():
    """跳过登录，造一个 user_info['pid'] 与学号不同的实例。"""
    with patch.object(LibrarySystem, "_initialize_login"):
        system = LibrarySystem(username=LOGIN_ID, password="pw")
    system.user_info = {
        "pid": LIBRARY_PID,
        "accNo": "acc-1",
        "logonName": LOGIN_ID,
        "token": "t",
    }
    system.session = Mock()
    return system


class AccountKeyIsUsernameTest(unittest.TestCase):
    def test_clear_owned_seat_keys_by_login_id(self):
        system = _build_system()
        system.insert_or_update_mongo = Mock(return_value=True)

        system._clear_owned_seat()

        _collection, pid, data = system.insert_or_update_mongo.call_args[0][:3]
        self.assertEqual(pid, LOGIN_ID)
        self.assertEqual(data, {"owned_seat": {}})

    def test_reservation_writeback_keys_by_login_id(self):
        system = _build_system()
        system.insert_or_update_mongo = Mock(return_value=True)
        system._format_reservation_data = Mock(return_value=([], {"3F-C060": []}))
        response = Mock(status_code=200)
        response.json.return_value = {"code": 0, "data": [{"uuid": "u1"}]}
        system.session.get.return_value = response

        with patch("utils.library_system.user_config_info") as cfg:
            cfg.find_one.return_value = {"owned_seat": {}}
            system.get_reservation_info()

        # 读回本地元数据和回写都必须落在登录学号上
        self.assertEqual(cfg.find_one.call_args[0][0], {"pid": LOGIN_ID})
        self.assertEqual(system.insert_or_update_mongo.call_args[0][1], LOGIN_ID)

    def test_arrival_check_registered_under_login_id(self):
        system = _build_system()
        system.get_seat_name_by_id = Mock(return_value="3F-C060")
        response = Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {
                "uuid": "resv-uuid",
                "resvDevInfoList": [{"devName": "3F-C060"}],
            },
        }
        system.session.post.return_value = response

        with patch("utils.library_system.register_arrival_check") as register:
            result = system._reserve_single_seat(
                system.user_info,
                "100499626",
                "2026-09-07 10:00:00",
                "2026-09-07 21:00:00",
            )

        self.assertIn("预约成功", result)
        self.assertEqual(register.call_args[0][0], LOGIN_ID)


if __name__ == "__main__":
    unittest.main()
