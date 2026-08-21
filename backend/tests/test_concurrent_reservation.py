import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

# 抢座调度本身不碰数据库、VPN 和真实网络，这里把这几层换成桩件。
# 只替换 test_library_login_errors 同样会替换的那几个模块：
# utils.library_system / utils.notify 保持真实，否则同一进程里跑的其他测试
# 会拿到假模块，patch 不到真实实现。
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


class ConcurrentReservationTests(unittest.TestCase):
    """7:00 的抢座必须真的并行，否则排在后面的账号会错过黄金窗口。"""

    @staticmethod
    def _accounts(count):
        return [
            {"pid": f"20211234{index:02d}", "priority": count - index}
            for index in range(count)
        ]

    def test_accounts_run_in_parallel(self):
        accounts = self._accounts(12)
        per_account_seconds = 0.2
        processed = []
        lock = threading.Lock()

        def fake_reservation(res_item):
            time.sleep(per_account_seconds)
            with lock:
                processed.append(res_item["pid"])

        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "reservation", side_effect=fake_reservation), \
             patch.object(scheduled_task, "RESERVE_CONCURRENCY", 8):
            started = time.monotonic()
            scheduled_task.process_reservations()
            elapsed = time.monotonic() - started

        self.assertEqual(sorted(processed), sorted(a["pid"] for a in accounts))
        # 串行需要 12 × 0.2 = 2.4 秒；并发度 8 时两批就能跑完。
        self.assertLess(elapsed, 1.2, f"抢座没有并发执行，耗时 {elapsed:.2f}s")

    def test_one_failing_account_does_not_stop_the_others(self):
        accounts = self._accounts(4)
        processed = []
        lock = threading.Lock()

        def fake_reservation(res_item):
            if res_item["pid"] == accounts[1]["pid"]:
                raise RuntimeError("统一认证挂了")
            with lock:
                processed.append(res_item["pid"])

        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "reservation", side_effect=fake_reservation), \
             patch.object(scheduled_task, "RESERVE_CONCURRENCY", 4):
            scheduled_task.process_reservations()

        self.assertEqual(len(processed), 3)
        self.assertNotIn(accounts[1]["pid"], processed)

    def test_higher_priority_accounts_start_first(self):
        accounts = self._accounts(4)  # 已按 priority 降序
        order = []
        lock = threading.Lock()

        def fake_reservation(res_item):
            with lock:
                order.append(res_item["pid"])
            time.sleep(0.05)

        # 并发度 1 时线程池退化成串行，能直接验证提交顺序仍是优先级顺序。
        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "reservation", side_effect=fake_reservation), \
             patch.object(scheduled_task, "RESERVE_CONCURRENCY", 1):
            scheduled_task.process_reservations()

        self.assertEqual(order, [a["pid"] for a in accounts])

    def test_empty_queue_is_a_no_op(self):
        with patch.object(scheduled_task, "get_all_active_reservations", return_value=[]), \
             patch.object(scheduled_task, "reservation") as reservation:
            scheduled_task.process_reservations()
        reservation.assert_not_called()

    def test_daily_reservation_stops_before_school_login_when_closed(self):
        account = {"pid": "2021123400", "vpn_password": "encrypted", "seat_list": ["2F-A001"]}
        conflict = {"title": "临时闭馆", "pause_from": "2026-08-24T00:00+08:00", "pause_until": "2026-08-25T08:00+08:00"}
        with patch.object(scheduled_task, "_dec", return_value="secret"), \
             patch.object(scheduled_task, "calculate_reservation_time", return_value=[("2026-08-24 08:00:00", "2026-08-24 12:00:00")]), \
             patch.object(scheduled_task, "find_any_reservation_conflict", return_value=conflict), \
             patch.object(scheduled_task, "LibrarySystem") as library, \
             patch.object(scheduled_task, "update_user_config") as update:
            scheduled_task.reservation(account)
        library.assert_not_called()
        self.assertIn("学校闭馆，已跳过预约", update.call_args.args[1])

    def test_late_protection_stops_before_cancelling_when_closed(self):
        conflict = {"title": "临时闭馆", "pause_from": "2026-08-24T00:00+08:00", "pause_until": "2026-08-25T08:00+08:00"}
        user = {"pid": "2021123400", "vpn_password": "encrypted"}
        seat = {"uuid": "reservation-id", "target_time": "2026-08-24 08:00:00-12:00:00"}
        with patch.object(scheduled_task.user_config_info, "find_one", return_value={"protection_max_minutes": 60}), \
             patch.object(scheduled_task, "find_reservation_conflict", return_value=conflict), \
             patch.object(scheduled_task, "LibrarySystem") as library:
            scheduled_task.late_protect_action(user, "2F-A001", seat)
        library.assert_not_called()

    def test_auto_nap_stops_before_cancelling_when_closed(self):
        conflict = {"title": "临时闭馆", "pause_from": "2026-08-24T00:00+08:00", "pause_until": "2026-08-25T08:00+08:00"}
        cfg = {
            "pid": "2021123400",
            "vpn_password": "encrypted",
            "nap_config": {"start_time": "14:00", "end_time": "18:00"},
        }
        with patch.object(scheduled_task.user_config_info, "find_one", return_value=cfg), \
             patch.object(scheduled_task, "find_reservation_conflict", return_value=conflict), \
             patch.object(scheduled_task, "LibrarySystem") as library:
            scheduled_task.auto_nap_action("2021123400")
        library.assert_not_called()


if __name__ == "__main__":
    unittest.main()
