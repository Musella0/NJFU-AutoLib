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


class SafeBookableAtTests(unittest.TestCase):
    """边界是逐秒判定的，下单时刻必须比规则再退一点。"""

    def test_margin_pushes_order_time_past_the_rule_boundary(self):
        rule = scheduled_task.bookable_at("2026-09-08 17:30:00")
        safe = scheduled_task.safe_bookable_at("2026-09-08 17:30:00")
        self.assertGreater(safe, rule, "踩着线发请求，本机时钟快几秒就白烧一个时段")
        self.assertEqual(
            (safe - rule).total_seconds(), scheduled_task.RESV_WINDOW_MARGIN_SECONDS
        )


class PlanHoldWindowTests(unittest.TestCase):
    """占位预约的时间窗：起点顶到窗口边界，终点跟着目标段走。"""

    SEVEN_AM = datetime(2026, 9, 7, 7, 0)

    def test_out_of_window_segment_is_held_from_the_window_edge(self):
        window = scheduled_task.plan_hold_window(
            self.SEVEN_AM, "2026-09-08 14:30:00", "2026-09-08 22:00:00",
            prev_segment_end="2026-09-08 12:00:00",
        )
        # 7:00 + 31h = 次日 14:00，退 2 分钟余量 → 13:58 起；终点就是本段终点
        self.assertEqual(window, ("2026-09-08 13:58:00", "2026-09-08 22:00:00"))
        self.assertLess(window[0], "2026-09-08 14:30:00", "占位必须真的盖住目标时段")

    def test_segment_already_in_window_needs_no_hold(self):
        self.assertIsNone(scheduled_task.plan_hold_window(
            self.SEVEN_AM, "2026-09-08 09:00:00", "2026-09-08 12:00:00"))

    def test_previous_segment_blocks_the_hold(self):
        """占位起点会压到自己前一段上时只能放弃——硬发是「用户在当前时段有预约」。"""
        self.assertIsNone(scheduled_task.plan_hold_window(
            self.SEVEN_AM, "2026-09-08 15:00:00", "2026-09-08 22:00:00",
            prev_segment_end="2026-09-08 14:30:00"))

    def test_day_after_tomorrow_cannot_be_held(self):
        """后天的段：占位起点要么超窗、要么超 900 分钟上限，占不了就老实排队。"""
        self.assertIsNone(scheduled_task.plan_hold_window(
            self.SEVEN_AM, "2026-09-09 09:00:00", "2026-09-09 12:00:00"))

    def test_hold_never_starts_before_opening(self):
        self.assertIsNone(
            scheduled_task.plan_hold_window(
                datetime(2026, 9, 7, 0, 30), "2026-09-08 09:00:00", "2026-09-08 12:00:00"),
            "07:30 开馆之前的起点图书馆不收",
        )


class ReservationSegmentSplitTests(unittest.TestCase):
    """关掉占位开关时的老行为：窗口内的段照常抢，窗口外的段只排队、不发请求。"""

    HOLD_ENABLED = False

    def setUp(self):
        scheduled_task.prelogin.clear()
        self.addCleanup(scheduled_task.prelogin.clear)
        self.now = datetime(2026, 9, 7, 7, 0)
        self.due = ("2026-09-08 09:00:00", "2026-09-08 12:00:00")
        self.queued = ("2026-09-08 14:30:00", "2026-09-08 22:00:00")
        self.library = Mock()
        self.library.reserve_seat.return_value = ("✅ 预约成功", None)
        self.res_item = {"pid": "2310102110", "vpn_password": "pwd", "seat_list": ["2F-B013"]}

    def _run(self, segments):
        with patch.object(scheduled_task, "calculate_reservation_time", return_value=segments), \
             patch.object(scheduled_task, "find_any_reservation_conflict", return_value=None), \
             patch.object(scheduled_task, "find_reservation_conflict", return_value=None), \
             patch.object(scheduled_task, "SEGMENT_HOLD_ENABLED", self.HOLD_ENABLED), \
             patch.object(scheduled_task, "get_seat_ids", return_value=["100455814"]), \
             patch.object(scheduled_task.prelogin, "take", return_value=self.library), \
             patch.object(scheduled_task, "queue_pending_segment") as queue, \
             patch.object(scheduled_task, "update_user_config") as update, \
             patch.object(scheduled_task, "notify_user") as notify:
            scheduled_task.reservation(self.res_item, now=self.now)
        return queue, update, notify

    def test_out_of_window_segment_is_queued_not_sent(self):
        queue, update, _ = self._run([self.due, self.queued])

        sent = [call.kwargs["resv_begin_time"] for call in self.library.reserve_seat.call_args_list]
        self.assertEqual(sent, [self.due[0]], "窗口外的段不能发给图书馆")

        queue.assert_called_once_with(
            "2310102110", self.queued[0], self.queued[1],
            scheduled_task.safe_bookable_at(self.queued[0]), hold=None,
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


class SegmentHoldTests(unittest.TestCase):
    """开着占位开关：超窗的段先下一张更早开始的单子，把座位先占住。"""

    def setUp(self):
        scheduled_task.prelogin.clear()
        self.addCleanup(scheduled_task.prelogin.clear)
        self.now = datetime(2026, 9, 7, 7, 0)
        self.due = ("2026-09-08 09:00:00", "2026-09-08 12:00:00")
        self.queued = ("2026-09-08 14:30:00", "2026-09-08 22:00:00")
        self.res_item = {"pid": "2310102110", "vpn_password": "pwd", "seat_list": ["3F-A176"]}
        self.library = Mock()
        self.library.get_reservation_info.return_value = ([], "无预约记录")

        def _reserve(seat_list, resv_begin_time, resv_end_time):
            self.library.last_reservation = {
                "uuid": "hold-uuid",
                "dev_id": seat_list[0],
                "dev_name": "3F-A176",
                "resv_begin_time": resv_begin_time,
                "resv_end_time": resv_end_time,
            }
            return f"✅ {resv_begin_time} 预约成功", None

        self.library.reserve_seat.side_effect = _reserve

    def _run(self, segments):
        with patch.object(scheduled_task, "calculate_reservation_time", return_value=segments), \
             patch.object(scheduled_task, "find_any_reservation_conflict", return_value=None), \
             patch.object(scheduled_task, "find_reservation_conflict", return_value=None), \
             patch.object(scheduled_task, "SEGMENT_HOLD_ENABLED", True), \
             patch.object(scheduled_task, "get_seat_ids", return_value=["100456431"]), \
             patch.object(scheduled_task.prelogin, "take", return_value=self.library), \
             patch.object(scheduled_task, "queue_pending_segment") as queue, \
             patch.object(scheduled_task, "update_user_config") as update, \
             patch.object(scheduled_task, "notify_user") as notify:
            scheduled_task.reservation(self.res_item, now=self.now)
        return queue, update, notify

    def test_out_of_window_segment_is_held_at_the_window_edge(self):
        queue, update, _ = self._run([self.due, self.queued])

        sent = [call.kwargs["resv_begin_time"] for call in self.library.reserve_seat.call_args_list]
        self.assertEqual(sent, [self.due[0], "2026-09-08 13:58:00"],
                         "超窗的段要用一张 13:58 起的占位单把座位占住")

        hold = queue.call_args.kwargs["hold"]
        self.assertEqual(hold["uuid"], "hold-uuid")
        self.assertEqual(hold["dev_id"], "100456431")
        self.assertEqual(hold["resv_begin_time"], "2026-09-08 13:58:00")
        self.assertEqual(hold["resv_end_time"], self.queued[1], "占位终点必须盖到本段终点")
        self.assertIn("占位", update.call_args.args[1])

    def test_hold_fully_covers_the_target_segment(self):
        self._run([self.due, self.queued])
        kwargs = self.library.reserve_seat.call_args.kwargs
        self.assertLessEqual(kwargs["resv_begin_time"], self.queued[0])
        self.assertGreaterEqual(kwargs["resv_end_time"], self.queued[1])

    def test_failed_hold_falls_back_to_plain_queue(self):
        self.library.reserve_seat.side_effect = None
        self.library.reserve_seat.return_value = ("❌ 设备在该时间段内已被预约", None)

        queue, _, _ = self._run([self.queued])

        self.assertIsNone(queue.call_args.kwargs["hold"], "占不到位就退回纯排队，不能瞎记一条")

    def test_unholdable_segment_still_skips_login(self):
        """后天的段占不了位，行为保持原样：连 webvpn 都不用登。"""
        self._run([("2026-09-09 09:00:00", "2026-09-09 12:00:00")])
        self.library.reserve_seat.assert_not_called()


class ProcessDueSegmentsTests(unittest.TestCase):
    """窗口一到，常驻 job 把队列里的段补约上。"""

    def setUp(self):
        scheduled_task.prelogin.clear()
        self.addCleanup(scheduled_task.prelogin.clear)
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

    def test_pooled_session_is_reused_instead_of_logging_in_again(self):
        """7:00 留下的会话拿来补约，省掉一次完整的 webvpn + CAS。"""
        pooled = Mock()
        pooled.reserve_seat.return_value = ("✅ 09-08 · 17:30-22:00 · 2F-B013 · 预约成功", None)
        pooled.get_reservation_info.return_value = ([], "无预约记录")
        scheduled_task.prelogin.store("2310102110", pooled)

        self._run()

        pooled.reserve_seat.assert_called_once()
        self.library.reserve_seat.assert_not_called()

    def test_failed_attempt_keeps_the_session_for_the_next_round(self):
        self.library.reserve_seat.return_value = ("❌ 网络请求异常", None)

        self._run()

        self.assertEqual(scheduled_task.prelogin.pooled_pids(), ["2310102110"])

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

    def test_first_failure_retries_instead_of_burning_the_segment(self):
        """窗口边界/网络/登录抖一下就判死，一个时段就这么白没了。"""
        self.library.reserve_seat.return_value = ("❌ 网络请求异常", None)

        self._run()

        written = self.pending.updates[0]["update"]["$set"]
        self.assertNotIn("status", written, "第一次失败不能收尾，要留在队列里重试")
        self.assertEqual(written["attempts"], 1)

    def test_transient_failures_do_not_notify_the_user(self):
        """VPN 网关抽风一秒下一分钟就好了，为它发邮件只会让人虚惊一场。"""
        self.doc["attempts"] = scheduled_task.SEGMENT_RETRY_MAX_ATTEMPTS - 2
        self.library.reserve_seat.return_value = ("❌ 网络请求异常", None)

        _, notify = self._run()

        notify.assert_not_called()
        self.assertNotIn("status", self.pending.updates[0]["update"]["$set"])

    def test_retries_are_capped_so_the_gateway_is_not_hammered(self):
        """连续失败满上限才判失败，也只在这时通知一次。"""
        self.doc["attempts"] = scheduled_task.SEGMENT_RETRY_MAX_ATTEMPTS - 1
        self.library.reserve_seat.return_value = ("❌ 所有座位预约失败", None)

        _, notify = self._run()

        self.assertEqual(self.pending.updates[0]["update"]["$set"]["status"], "failed")
        notify.assert_called_once()
        self.assertTrue(notify.call_args.kwargs.get("always"))


class HoldSwapTests(unittest.TestCase):
    """窗口一开：取消占位 → 立刻改约精确时段 → 回图书馆核对，一步都不能省。"""

    HOLD = {
        "uuid": "hold-uuid",
        "dev_id": "100456431",
        "dev_name": "3F-A176",
        "resv_begin_time": "2026-09-08 13:58:00",
        "resv_end_time": "2026-09-08 22:00:00",
    }
    HOLD_RECORD = {
        "uuid": "hold-uuid",
        "resvBeginTime": "2026-09-08 13:58:00",
        "resvEndTime": "2026-09-08 22:00:00",
    }
    TARGET_RECORD = {
        "uuid": "new-uuid",
        "resvBeginTime": "2026-09-08 14:30:00",
        "resvEndTime": "2026-09-08 22:00:00",
    }

    def setUp(self):
        scheduled_task.prelogin.clear()
        self.addCleanup(scheduled_task.prelogin.clear)
        self.now = datetime(2026, 9, 7, 7, 33)
        self.doc = {
            "_id": "seg-1",
            "pid": "2310102110",
            "resv_begin_time": "2026-09-08 14:30:00",
            "resv_end_time": "2026-09-08 22:00:00",
            "open_at": datetime(2026, 9, 7, 7, 32),
            "status": "pending",
            "hold": dict(self.HOLD),
        }
        self.pending = FakePendingSegments([self.doc])
        self.library = Mock()
        self.library.delete_seat.return_value = (True, "删除座位成功")
        self.library.reserve_seat.return_value = (
            "✅ 09-08 · 14:30-22:00 · 3F-A176 · 预约成功", None)
        # 换约前后各查一次：先确认占位还在，再核对换过来了没有
        self.library.get_reservation_info.side_effect = [
            ([self.HOLD_RECORD], "查询成功"),
            ([self.TARGET_RECORD], "查询成功"),
        ]
        self.cfg = {"pid": "2310102110", "vpn_password": "pwd", "seat_list": ["3F-A176", "3F-A172"]}

    def _run(self):
        user_config = Mock()
        user_config.find_one.return_value = self.cfg
        with patch.object(scheduled_task, "pending_segments", self.pending), \
             patch.object(scheduled_task, "user_config_info", user_config), \
             patch.object(scheduled_task, "find_reservation_conflict", return_value=None), \
             patch.object(scheduled_task, "get_seat_ids", return_value=["100456427", "100456431"]), \
             patch.object(scheduled_task, "LibrarySystem", return_value=self.library), \
             patch.object(scheduled_task, "update_user_config"), \
             patch.object(scheduled_task, "notify_user") as notify:
            scheduled_task.process_due_segments(now=self.now)
        return notify

    def _last_status(self):
        for update in reversed(self.pending.updates):
            status = update["update"].get("$set", {}).get("status")
            if status:
                return status
        return None

    def test_hold_is_cancelled_before_the_new_order_goes_out(self):
        self._run()

        self.library.delete_seat.assert_called_once_with("hold-uuid")
        kwargs = self.library.reserve_seat.call_args.kwargs
        self.assertEqual(kwargs["resv_begin_time"], "2026-09-08 14:30:00",
                         "换约必须发精确时段，不能继续用占位的时间")
        self.assertEqual(kwargs["seat_list"][0], "100456431",
                         "优先约回刚放开的那张座位")
        self.assertEqual(self._last_status(), "done")

    def test_failed_cancel_never_sends_the_new_order(self):
        """占位没删掉还硬发，只会被「用户在当前时段有预约」顶回来，白费一次机会。"""
        self.library.delete_seat.return_value = (False, "删除座位失败: 预约不存在")

        self._run()

        self.library.reserve_seat.assert_not_called()
        self.assertNotEqual(self._last_status(), "done")
        self.assertEqual(self.pending.updates[-1]["update"]["$set"]["attempts"], 1)

    def test_verification_failure_keeps_the_segment_pending(self):
        """回查发现占位还在——哪怕下单接口回了「预约成功」，也绝不能判 done。"""
        self.library.get_reservation_info.side_effect = [
            ([self.HOLD_RECORD], "查询成功"),
            ([self.HOLD_RECORD, self.TARGET_RECORD], "查询成功"),
        ]

        self._run()

        self.assertNotEqual(self._last_status(), "done")
        self.assertEqual(self.pending.updates[-1]["update"]["$set"]["attempts"], 1)

    def test_missing_target_after_swap_keeps_the_segment_pending(self):
        self.library.get_reservation_info.side_effect = [
            ([self.HOLD_RECORD], "查询成功"),
            ([], "无预约记录"),
        ]

        self._run()

        self.assertNotEqual(self._last_status(), "done")

    def test_swap_gives_up_at_the_deadline_whatever_the_attempt_count(self):
        """占位快开始时不管试了几次都不能再动它；放弃时必须吵醒用户，因为手上的时间是错的。"""
        self.now = datetime(2026, 9, 8, 13, 40)
        self.library.delete_seat.return_value = (False, "删除座位失败")

        notify = self._run()

        self.assertEqual(self._last_status(), "failed")
        self.assertTrue(notify.call_args.kwargs.get("always"))
        self.assertIn("占位", notify.call_args.args[2])

    def test_early_swap_failures_retry_next_minute_without_notifying(self):
        """2026-09-14 10:33 VPN 502 一次就发了封邮件，下一分钟其实就换成了。"""
        self.library.delete_seat.return_value = (False, "删除座位失败")

        notify = self._run()

        changes = self.pending.updates[-1]["update"]["$set"]
        self.assertEqual(changes["attempts"], 1)
        self.assertNotIn("status", changes)
        notify.assert_not_called()

    def test_swap_fails_after_consecutive_failures_and_notifies_once(self):
        self.doc["attempts"] = scheduled_task.SEGMENT_RETRY_MAX_ATTEMPTS - 1
        self.library.delete_seat.return_value = (False, "删除座位失败")

        notify = self._run()

        self.assertEqual(self._last_status(), "failed")
        notify.assert_called_once()
        self.assertTrue(notify.call_args.kwargs.get("always"))
        self.assertIn("占位", notify.call_args.args[2])

    def test_hold_already_gone_falls_back_to_plain_rebooking(self):
        self.library.get_reservation_info.side_effect = [
            ([], "无预约记录"),
            ([self.TARGET_RECORD], "查询成功"),
        ]

        self._run()

        self.library.delete_seat.assert_not_called()
        self.library.reserve_seat.assert_called_once()
        self.assertEqual(self._last_status(), "done")

    def test_target_already_held_by_user_is_done_without_a_new_order(self):
        """2026-09-11 实测：用户自己取消占位、手动约了精确时段。再下单只会撞自己，
        而占位段的重试不封顶——会每分钟登录一次直到期限，最后还发错误通知。"""
        self.library.get_reservation_info.side_effect = [
            ([self.TARGET_RECORD], "查询成功"),
        ]

        notify = self._run()

        self.library.reserve_seat.assert_not_called()
        self.library.delete_seat.assert_not_called()
        self.assertEqual(self._last_status(), "done")
        self.assertIn("无需换约", notify.call_args.args[2])

    def test_target_present_alongside_hold_cancels_the_hold_and_is_done(self):
        self.library.get_reservation_info.side_effect = [
            ([self.HOLD_RECORD, self.TARGET_RECORD], "查询成功"),
        ]

        self._run()

        self.library.delete_seat.assert_called_once_with("hold-uuid")
        self.library.reserve_seat.assert_not_called()
        self.assertEqual(self._last_status(), "done")


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
