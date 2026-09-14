import os
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
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


def fake_plan(account, segments=1):
    """造一个已经算好的账号计划：segments 段，全部当作窗口内直接抢。"""
    plan = scheduled_task._UserPlan(account, [("b", "e")] * segments, ["100455814"], "pwd")
    for idx in range(1, segments + 1):
        plan.items.append(scheduled_task._WorkItem(
            plan, idx, f"2026-09-08 0{idx}:00:00", f"2026-09-08 1{idx}:00:00",
            datetime(2026, 9, 7, 7, 0), True, None,
        ))
    plan.remaining = len(plan.items)
    return plan


class ConcurrentReservationTests(unittest.TestCase):
    """7:00 的抢座必须真的并行，否则排在后面的账号会错过黄金窗口。"""

    @staticmethod
    def _accounts(count):
        return [
            {"pid": f"20211234{index:02d}", "priority": count - index}
            for index in range(count)
        ]

    def _run(self, accounts, run_item, concurrency, segments=1):
        with patch.object(scheduled_task, "get_all_active_reservations", return_value=accounts), \
             patch.object(scheduled_task, "_plan_reservation_safely",
                          side_effect=lambda item, now=None: fake_plan(item, segments)), \
             patch.object(scheduled_task, "_run_item", side_effect=run_item), \
             patch.object(scheduled_task, "RESERVE_CONCURRENCY", concurrency):
            scheduled_task.process_reservations()

    def test_accounts_run_in_parallel(self):
        accounts = self._accounts(12)
        per_account_seconds = 0.2
        processed = []
        lock = threading.Lock()

        def fake_run(item):
            time.sleep(per_account_seconds)
            with lock:
                processed.append(item.plan.pid)

        started = time.monotonic()
        self._run(accounts, fake_run, concurrency=8)
        elapsed = time.monotonic() - started

        self.assertEqual(sorted(processed), sorted(a["pid"] for a in accounts))
        # 串行需要 12 × 0.2 = 2.4 秒；并发度 8 时两批就能跑完。
        self.assertLess(elapsed, 1.2, f"抢座没有并发执行，耗时 {elapsed:.2f}s")

    def test_one_failing_account_does_not_stop_the_others(self):
        accounts = self._accounts(4)
        processed = []
        lock = threading.Lock()

        def fake_run(item):
            if item.plan.pid == accounts[1]["pid"]:
                raise RuntimeError("统一认证挂了")
            with lock:
                processed.append(item.plan.pid)

        self._run(accounts, fake_run, concurrency=4)

        self.assertEqual(len(processed), 3)
        self.assertNotIn(accounts[1]["pid"], processed)

    def test_higher_priority_accounts_start_first(self):
        accounts = self._accounts(4)  # 已按 priority 降序
        order = []
        lock = threading.Lock()

        def fake_run(item):
            with lock:
                order.append(item.plan.pid)
            time.sleep(0.05)

        # 并发度 1 时退化成串行，能直接验证取活顺序仍是优先级顺序。
        self._run(accounts, fake_run, concurrency=1)

        self.assertEqual(order, [a["pid"] for a in accounts])

    def test_accounts_that_keep_hitting_their_own_booking_go_last(self):
        """老是撞到自己已有预约的账号，不该占着开闸后最前面那几百毫秒。

        它在图书馆那边已经有同段的预约了，打哪张座位都会被同一句话顶回来；
        排在前面等于拿真能抢到的人的窗口去烧一轮必然失败的请求。
        """
        accounts = self._accounts(4)
        order = []

        def fake_run(item):
            order.append(item.plan.pid)

        with patch.object(scheduled_task.attempt_log, "chronic_self_conflict_pids",
                          return_value={accounts[0]["pid"]}):
            self._run(accounts, fake_run, concurrency=1)

        pids = [a["pid"] for a in accounts]
        self.assertEqual(order, pids[1:] + pids[:1], "该降级的排到队尾，其余保持优先级顺序")

    def test_demoted_accounts_are_still_attempted(self):
        """降级不是跳过：万一今天他没手动约，照样该给他抢。"""
        accounts = self._accounts(2)
        order = []

        with patch.object(scheduled_task.attempt_log, "chronic_self_conflict_pids",
                          return_value={a["pid"] for a in accounts}):
            self._run(accounts, lambda item: order.append(item.plan.pid), concurrency=1)

        self.assertEqual(sorted(order), sorted(a["pid"] for a in accounts))

    def test_second_segments_wait_behind_everyones_first_segment(self):
        """大家偏好从早坐到晚：第 1 段才是真正被抢的，第 2 段统一排到队尾。"""
        accounts = self._accounts(3)
        order = []

        def fake_run(item):
            order.append((item.plan.pid, item.idx))

        self._run(accounts, fake_run, concurrency=1, segments=2)

        pids = [a["pid"] for a in accounts]
        self.assertEqual(order, [(pid, 1) for pid in pids] + [(pid, 2) for pid in pids])

    def test_same_account_never_runs_two_segments_at_once(self):
        """图书馆对同一账号同时只受理一个预约操作，第二个会被直接顶掉。"""
        accounts = self._accounts(2)
        active = {}
        overlaps = []
        order = []
        lock = threading.Lock()

        def fake_run(item):
            pid = item.plan.pid
            with lock:
                if pid in active:
                    overlaps.append(pid)
                active[pid] = item.idx
                order.append((pid, item.idx))
            time.sleep(0.1)
            with lock:
                active.pop(pid, None)

        # 并发度远大于账号数，第 2 段本来有机会跟自己的第 1 段同时跑。
        self._run(accounts, fake_run, concurrency=4, segments=2)

        self.assertEqual(overlaps, [], "同一账号的两段同时在跑")
        for account in accounts:
            mine = [idx for pid, idx in order if pid == account["pid"]]
            self.assertEqual(mine, [1, 2], "同一账号的段必须按配置顺序执行")

    def test_empty_queue_is_a_no_op(self):
        with patch.object(scheduled_task, "get_all_active_reservations", return_value=[]), \
             patch.object(scheduled_task, "_plan_reservation_safely") as plan:
            scheduled_task.process_reservations()
        plan.assert_not_called()

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
