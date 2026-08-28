import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

# 预登录本身不碰数据库、VPN 和真实网络，这里把这几层换成桩件。
# 与 test_concurrent_reservation 保持同一套替换，避免同进程下互相污染。
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


fake_encryptor_module.PasswordEncryptor = FakePasswordEncryptor
sys.modules.setdefault("utils.password_encryptor", fake_encryptor_module)

os.environ.setdefault("ENCRYPTION_KEY", "00" * 32)

import scheduled_task  # noqa: E402
from utils import prelogin  # noqa: E402
from utils.library_system import LibrarySystem  # noqa: E402


def warm_library(alive=True):
    """造一个「已登录」的 LibrarySystem 替身。"""
    library = Mock()
    library.verify_session.return_value = alive
    return library


class PoolTests(unittest.TestCase):
    """会话池的取用规则。核心不变量：拿不到就返回 None，永远不抛异常。"""

    def setUp(self):
        prelogin.clear()
        self.addCleanup(prelogin.clear)

    def test_empty_pool_returns_none(self):
        self.assertIsNone(prelogin.take("2021123400"))

    def test_live_session_is_handed_out_once(self):
        library = warm_library()
        prelogin.store("2021123400", library)

        self.assertIs(prelogin.take("2021123400"), library)
        # 取走之后池子里就没有了，第二次只能回退现场登录。
        self.assertIsNone(prelogin.take("2021123400"))

    def test_dead_session_is_dropped(self):
        prelogin.store("2021123400", warm_library(alive=False))
        self.assertIsNone(prelogin.take("2021123400"))
        self.assertEqual(prelogin.pooled_pids(), [])

    def test_verify_blowing_up_falls_back_instead_of_raising(self):
        library = Mock()
        library.verify_session.side_effect = RuntimeError("网关炸了")
        prelogin.store("2021123400", library)

        # 这个函数在 7:00:00 的关键路径上，异常必须被吞掉。
        self.assertIsNone(prelogin.take("2021123400"))

    def test_stale_session_is_discarded_without_network_call(self):
        library = warm_library()
        prelogin.store("2021123400", library)

        with patch.object(prelogin, "MAX_AGE_SECONDS", -1):
            self.assertIsNone(prelogin.take("2021123400"))
        library.verify_session.assert_not_called()

    def test_disabled_switch_makes_take_a_no_op(self):
        library = warm_library()
        prelogin.store("2021123400", library)

        with patch.object(prelogin, "ENABLED", False):
            self.assertIsNone(prelogin.take("2021123400"))
        library.verify_session.assert_not_called()

    def test_is_alive_keeps_live_session_in_pool(self):
        prelogin.store("2021123400", warm_library())
        self.assertTrue(prelogin.is_alive("2021123400"))
        self.assertEqual(prelogin.pooled_pids(), ["2021123400"])

    def test_is_alive_evicts_dead_session(self):
        prelogin.store("2021123400", warm_library(alive=False))
        self.assertFalse(prelogin.is_alive("2021123400"))
        self.assertEqual(prelogin.pooled_pids(), [])


class VerifySessionTests(unittest.TestCase):
    """ensure_login 只看本地字段，判断不了服务端会话是否过期，所以要有这个。"""

    @staticmethod
    def make_library(response):
        library = LibrarySystem.__new__(LibrarySystem)
        library.username = "12345678"
        library.base_url = "https://example.test/"
        library.vpn_suffix = "?vpn"
        library.user_info = None
        library.session = Mock()
        library.session.get.return_value = response
        return library

    def test_valid_session_refreshes_user_info(self):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 0, "data": {"accNo": "42"}}
        library = self.make_library(response)

        with patch.object(LibrarySystem, "_set_user_cookie") as set_cookie:
            self.assertTrue(library.verify_session())
        self.assertEqual(library.user_info, {"accNo": "42"})
        set_cookie.assert_called_once_with({"accNo": "42"})

    def test_expired_session_reports_false(self):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 300, "message": "未登录"}
        self.assertFalse(self.make_library(response).verify_session())

    def test_http_error_reports_false(self):
        self.assertFalse(self.make_library(Mock(status_code=502)).verify_session())

    def test_network_error_reports_false(self):
        library = self.make_library(Mock(status_code=200))
        library.session.get.side_effect = OSError("connection reset")
        self.assertFalse(library.verify_session())

    def test_verify_uses_a_tight_timeout(self):
        # 超时给得比默认宽松值紧，是因为卡住比失败更糟：
        # 失败只是回退现场登录，卡住会把整个抢座窗口拖没。
        from utils.base_system import DEFAULT_TIMEOUT
        from utils.library_system import SESSION_VERIFY_TIMEOUT

        self.assertLess(SESSION_VERIFY_TIMEOUT[0], DEFAULT_TIMEOUT[0])
        self.assertLess(SESSION_VERIFY_TIMEOUT[1], DEFAULT_TIMEOUT[1])


class ReservationReuseTests(unittest.TestCase):
    """抢座路径：有可用的预登录会话就复用，没有就完全照旧现场登录。"""

    def setUp(self):
        prelogin.clear()
        self.addCleanup(prelogin.clear)
        self.account = {
            "pid": "2021123400",
            "vpn_password": "encrypted",
            "seat_list": ["2F-A001"],
        }

    @staticmethod
    def _reservable(alive=True):
        library = Mock()
        library.verify_session.return_value = alive
        library.reserve_seat.return_value = ("✅ 08-28 · 09:00-20:00 · 2F-A001 · 预约成功", None)
        library.get_reservation_info.return_value = ([], "查询成功")
        return library

    def _run(self):
        """跑一次 reservation()，返回「现场登录」这条老路径的桩件。"""
        with patch.object(scheduled_task, "_dec", return_value="secret"), \
             patch.object(scheduled_task, "calculate_reservation_time",
                          return_value=[("2026-08-28 09:00:00", "2026-08-28 20:00:00")]), \
             patch.object(scheduled_task, "find_any_reservation_conflict", return_value=None), \
             patch.object(scheduled_task, "get_seat_ids", return_value=["100456693"]), \
             patch.object(scheduled_task, "update_user_config"), \
             patch.object(scheduled_task, "notify_user"), \
             patch.object(scheduled_task, "LibrarySystem",
                          side_effect=lambda **kw: self._reservable()) as fresh_login:
            scheduled_task.reservation(self.account)
        return fresh_login

    def test_pooled_session_skips_the_login_chain(self):
        library = self._reservable()
        prelogin.store(self.account["pid"], library)

        fresh_login = self._run()

        fresh_login.assert_not_called()
        library.reserve_seat.assert_called_once()

    def test_empty_pool_falls_back_to_fresh_login(self):
        self._run().assert_called_once()

    def test_dead_pooled_session_falls_back_to_fresh_login(self):
        dead = self._reservable(alive=False)
        prelogin.store(self.account["pid"], dead)

        self._run().assert_called_once()
        dead.reserve_seat.assert_not_called()


class ProcessPreloginTests(unittest.TestCase):
    def setUp(self):
        prelogin.clear()
        self.addCleanup(prelogin.clear)

    @staticmethod
    def _accounts(count):
        return [
            {"pid": f"20211234{i:02d}", "vpn_password": "encrypted", "seat_list": ["2F-A001"]}
            for i in range(count)
        ]

    def test_all_accounts_get_a_warm_session(self):
        accounts = self._accounts(3)
        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "_dec", return_value="secret"), \
             patch.object(scheduled_task, "LibrarySystem", side_effect=lambda **kw: Mock()):
            scheduled_task.process_prelogin()

        self.assertEqual(sorted(prelogin.pooled_pids()), sorted(a["pid"] for a in accounts))

    def test_one_failing_account_does_not_stop_the_others(self):
        accounts = self._accounts(3)
        bad = accounts[1]["pid"]

        def build(**kwargs):
            if kwargs["username"] == bad:
                raise RuntimeError("统一认证挂了")
            return Mock()

        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "_dec", return_value="secret"), \
             patch.object(scheduled_task, "LibrarySystem", side_effect=build):
            scheduled_task.process_prelogin()

        self.assertNotIn(bad, prelogin.pooled_pids())
        self.assertEqual(len(prelogin.pooled_pids()), 2)

    def test_disabled_switch_skips_everything(self):
        with patch.object(prelogin, "ENABLED", False), \
             patch.object(scheduled_task, "get_all_active_reservations") as active:
            scheduled_task.process_prelogin()
        active.assert_not_called()

    def test_empty_queue_is_a_no_op(self):
        with patch.object(scheduled_task, "get_all_active_reservations", return_value=[]), \
             patch.object(scheduled_task, "LibrarySystem") as build:
            scheduled_task.process_prelogin()
        build.assert_not_called()

    def test_refresh_only_relogins_dead_sessions(self):
        accounts = self._accounts(3)
        prelogin.store(accounts[0]["pid"], warm_library(alive=True))
        prelogin.store(accounts[1]["pid"], warm_library(alive=False))
        # accounts[2] 压根没预登录成功，也该被复查捞回来。

        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "_dec", return_value="secret"), \
             patch.object(scheduled_task, "LibrarySystem", side_effect=lambda **kw: Mock()) as build:
            scheduled_task.process_prelogin(refresh=True)

        relogged = sorted(call.kwargs["username"] for call in build.call_args_list)
        self.assertEqual(relogged, sorted([accounts[1]["pid"], accounts[2]["pid"]]))
        self.assertEqual(len(prelogin.pooled_pids()), 3)

    def test_refresh_with_all_sessions_alive_does_nothing(self):
        accounts = self._accounts(2)
        for account in accounts:
            prelogin.store(account["pid"], warm_library(alive=True))

        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "LibrarySystem") as build:
            scheduled_task.process_prelogin(refresh=True)
        build.assert_not_called()

    def test_reservation_run_empties_the_pool(self):
        accounts = self._accounts(2)
        for account in accounts:
            prelogin.store(account["pid"], warm_library())

        # 留在池子里的过期会话会被后面的午休/到馆复查任务误用，必须清干净。
        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "reservation"):
            scheduled_task.process_reservations()

        self.assertEqual(prelogin.pooled_pids(), [])

    def test_pool_is_emptied_even_when_reservation_blows_up(self):
        accounts = self._accounts(1)
        prelogin.store(accounts[0]["pid"], warm_library())

        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "reservation", side_effect=RuntimeError("炸了")):
            scheduled_task.process_reservations()

        self.assertEqual(prelogin.pooled_pids(), [])


if __name__ == "__main__":
    unittest.main()
