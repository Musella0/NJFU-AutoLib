"""图书馆只让提前 31 小时下单，超窗的段必须排队补约，不能硬发。

之前 7:00 把当天所有段一次性发出去，下午那段稳定收到「不在提前预约时间范围内」，
一个月里第 2 段没成功过一次。这里锁住新的分流行为。
"""

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

# 与 test_concurrent_reservation 相同的桩件：抢座调度本身不碰数据库和真实网络。
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


def _fmt(moment: datetime) -> str:
    return moment.strftime(scheduled_task.TIME_FMT)


class FakeCursor(list):
    def sort(self, *args, **kwargs):
        return self


class FakePendingSegments:
    """够用的 pending_segments 替身：只记下写了什么。"""

    def __init__(self, docs=None):
        self.docs = docs or []
        self.updates = []

    def find(self, query):
        return FakeCursor(self.docs)

    def update_one(self, flt, update, upsert=False):
        self.updates.append({"filter": flt, "update": update, "upsert": upsert})


class BookableAtTests(unittest.TestCase):
    def test_window_is_31_hours_before_start(self):
        opens = scheduled_task.bookable_at("2026-09-08 17:30:00")
        self.assertEqual(opens, datetime(2026, 9, 7, 10, 30))

    def test_seven_am_run_reaches_next_day_14_00(self):
        """7:00 那批最远只够得到次日 14:00 开始的段——这正是下午段一直失败的原因。"""
        run_at = datetime(2026, 9, 7, 7, 0)
        self.assertLessEqual(scheduled_task.bookable_at("2026-09-08 14:00:00"), run_at)
        self.assertGreater(scheduled_task.bookable_at("2026-09-08 14:30:00"), run_at)


class ReservationSegmentSplitTests(unittest.TestCase):
    """窗口内的段照常抢，窗口外的段只排队、不发请求。"""

    def setUp(self):
        now = datetime.now()
        self.due = (_fmt(now + timedelta(hours=2)), _fmt(now + timedelta(hours=5)))
        self.queued = (_fmt(now + timedelta(hours=40)), _fmt(now + timedelta(hours=44)))
        self.library = Mock()
        self.library.reserve_seat.return_value = ("✅ 预约成功", None)
        self.res_item = {"pid": "2310102110", "vpn_password": "pwd", "seat_list": ["2F-B013"]}

    def _run(self, segments):
        with patch.object(scheduled_task, "calculate_reservation_time", return_value=segments), \
             patch.object(scheduled_task, "find_any_reservation_conflict", return_value=None), \
             patch.object(scheduled_task, "get_seat_ids", return_value=["100455814"]), \
             patch.object(scheduled_task.prelogin, "take", return_value=self.library), \
             patch.object(scheduled_task, "queue_pending_segment") as queue, \
             patch.object(scheduled_task, "update_user_config") as update, \
             patch.object(scheduled_task, "notify_user") as notify:
            scheduled_task.reservation(self.res_item)
        return queue, update, notify

    def test_out_of_window_segment_is_queued_not_sent(self):
        queue, update, _ = self._run([self.due, self.queued])

        sent = [call.kwargs["resv_begin_time"] for call in self.library.reserve_seat.call_args_list]
        self.assertEqual(sent, [self.due[0]], "窗口外的段不能发给图书馆")

        queue.assert_called_once_with(
            "2310102110", self.queued[0], self.queued[1],
            scheduled_task.bookable_at(self.queued[0]),
        )
        combined = update.call_args.args[1]
        self.assertIn("✅ 预约成功", combined)
        self.assertIn("⏳", combined)

    def test_all_segments_out_of_window_skips_login_and_is_not_a_failure(self):
        queue, _, notify = self._run([self.queued])

        self.library.reserve_seat.assert_not_called()
        queue.assert_called_once()
        self.assertIn("排队", notify.call_args.args[1])
        self.assertNotIn("失败", notify.call_args.args[1])

    def test_segment_results_keep_configured_order(self):
        _, update, _ = self._run([self.queued, self.due])

        lines = update.call_args.args[1].splitlines()
        self.assertTrue(lines[0].startswith("⏳"), "结果顺序应跟着配置里的段序，而不是执行顺序")
        self.assertEqual(lines[1], "✅ 预约成功")


class ProcessDueSegmentsTests(unittest.TestCase):
    """窗口一到，常驻 job 把队列里的段补约上。"""

    def setUp(self):
        self.now = datetime(2026, 9, 7, 10, 30, 20)
        self.doc = {
            "_id": "seg-1",
            "pid": "2310102110",
            "resv_begin_time": "2026-09-08 17:30:00",
            "resv_end_time": "2026-09-08 22:00:00",
            "open_at": datetime(2026, 9, 7, 10, 30),
            "status": "pending",
        }
        self.pending = FakePendingSegments([self.doc])
        self.library = Mock()
        self.library.reserve_seat.return_value = ("✅ 09-08 · 17:30-22:00 · 2F-B013 · 预约成功", None)
        self.cfg = {"pid": "2310102110", "vpn_password": "pwd", "seat_list": ["2F-B013"]}

    def _run(self):
        user_config = Mock()
        user_config.find_one.return_value = self.cfg
        with patch.object(scheduled_task, "pending_segments", self.pending), \
             patch.object(scheduled_task, "user_config_info", user_config), \
             patch.object(scheduled_task, "find_reservation_conflict", return_value=None), \
             patch.object(scheduled_task, "get_seat_ids", return_value=["100455814"]), \
             patch.object(scheduled_task, "LibrarySystem", return_value=self.library), \
             patch.object(scheduled_task, "update_user_config") as update, \
             patch.object(scheduled_task, "notify_user") as notify:
            scheduled_task.process_due_segments(now=self.now)
        return update, notify

    def test_due_segment_is_booked_and_marked_done(self):
        update, notify = self._run()

        self.library.reserve_seat.assert_called_once()
        self.assertEqual(
            self.library.reserve_seat.call_args.kwargs["resv_begin_time"],
            "2026-09-08 17:30:00",
        )
        self.assertEqual(self.pending.updates[0]["update"]["$set"]["status"], "done")
        self.assertIn("预约成功", update.call_args.args[1])
        self.assertIn("补约成功", notify.call_args.args[1])

    def test_segment_whose_start_already_passed_is_dropped(self):
        self.doc["resv_begin_time"] = "2026-09-07 09:00:00"
        self.doc["resv_end_time"] = "2026-09-07 12:00:00"

        self._run()

        self.library.reserve_seat.assert_not_called()
        self.assertEqual(self.pending.updates[0]["update"]["$set"]["status"], "expired")

    def test_disabled_account_is_skipped(self):
        self.cfg = None

        self._run()

        self.library.reserve_seat.assert_not_called()
        self.assertEqual(self.pending.updates[0]["update"]["$set"]["status"], "skipped")


class ReplaceQueuedResultTests(unittest.TestCase):
    def test_only_the_matching_placeholder_line_is_replaced(self):
        user_config = Mock()
        user_config.find_one.return_value = {
            "result": "✅ 09-08 · 09:00-12:00 · 2F-B013 · 预约成功\n"
                      "⏳ 第2段 17:30-22:00: 图书馆最多提前 31 小时预约，已排到 09-07 10:30 自动补约"
        }
        with patch.object(scheduled_task, "user_config_info", user_config), \
             patch.object(scheduled_task, "update_user_config") as update:
            scheduled_task._replace_queued_result(
                "2310102110", "2026-09-08 17:30:00", "2026-09-08 22:00:00", "✅ 补约成功"
            )

        self.assertEqual(
            update.call_args.args[1],
            "✅ 09-08 · 09:00-12:00 · 2F-B013 · 预约成功\n✅ 补约成功",
        )


if __name__ == "__main__":
    unittest.main()
