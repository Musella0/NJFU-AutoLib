"""锁住「每次预约尝试都要留下记录」这件事。

这是选座模型唯一的一手数据来源，而且它挂在抢座的关键路径上，所以两头都要钉：
记录的内容要对（开闸偏移、判定分类），以及记账本身绝不能把抢座搞挂。
"""

import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

from utils import attempt_log
from utils.attempt_log import build_record, classify


class ClassifyTests(unittest.TestCase):
    def test_the_outcome_we_actually_care_about(self):
        msg = ("座位 2F-B070(100455871) 期望预约时间2026-09-10 08:00:00-22:00:00 "
               "预约失败: 设备在该时间段内已被预约")
        self.assertEqual(classify(msg), "taken")

    def test_self_conflict_must_not_count_as_competition(self):
        """撞自己已有的单子时，图书馆压根不看座位空不空——这种样本必须能被剔掉。"""
        msg = ("座位 2F-B070(100455871) 期望预约时间2026-09-10 08:00:00-22:00:00 "
               "预约失败: 学工号为：2000102115的用户在当前时段有预约")
        self.assertEqual(classify(msg), "self_conflict")
        self.assertNotIn("self_conflict", attempt_log.CONTENTION_OUTCOMES)

    def test_busy_is_a_signal_but_not_a_verdict(self):
        """有人同一瞬间在下这张座位——座位归谁未定，不能算 taken，但也不能丢进 other。"""
        msg = ("座位 2F-B072(100455873) 期望预约时间2026-09-12 08:00:00-22:00:00 "
               "预约失败: 当前设备正在被预约，请稍后重试")
        self.assertEqual(classify(msg), "busy")
        self.assertNotIn("busy", attempt_log.CONTENTION_OUTCOMES)

    def test_account_lock_is_not_about_the_seat(self):
        msg = ("座位 2F-B070(100455871) 期望预约时间2026-09-12 08:00:00-22:00:00 "
               "预约失败: 您有预约操作正在进行，请稍后操作")
        self.assertEqual(classify(msg), "account_lock")
        self.assertNotIn("account_lock", attempt_log.CONTENTION_OUTCOMES)

    def test_only_success_and_taken_are_contention_signals(self):
        self.assertEqual(set(attempt_log.CONTENTION_OUTCOMES), {"success", "taken"})

    def test_other_outcomes(self):
        self.assertEqual(classify("✅ 09-10 · 08:00-22:00 · 2F-B070 · 预约成功"), "success")
        self.assertEqual(classify("预约失败: 不在提前预约时间范围内"), "out_of_window")
        self.assertEqual(classify("座位 X 网络请求异常: timeout"), "network")
        self.assertEqual(classify(""), "unknown")
        self.assertEqual(classify("某个没见过的错误"), "other")


class BuildRecordTests(unittest.TestCase):
    def test_offset_is_measured_from_the_day_the_shot_was_fired(self):
        """模型要的特征就是「开闸后第几毫秒这张座位还活着」。"""
        fired = datetime(2026, 9, 9, 7, 0, 0, 223000)
        doc = build_record(
            "2310104222", "2F-B070", "100455871",
            "2026-09-10 08:00:00", "2026-09-10 22:00:00",
            fired, datetime(2026, 9, 9, 7, 0, 5, 392000),
            "预约失败: 设备在该时间段内已被预约",
        )
        self.assertEqual(doc["offset_ms"], 223)
        self.assertEqual(doc["latency_ms"], 5169)
        self.assertEqual(doc["phase"], "rush")
        self.assertEqual(doc["outcome"], "taken")
        # 目标日和时段要能直接喂给模型
        self.assertEqual(doc["target_date"], "2026-09-10")
        self.assertEqual(doc["weekday"], 4)
        self.assertEqual(doc["begin_min"], 480)
        self.assertEqual(doc["end_min"], 1320)

    def test_second_shot_lands_five_seconds_late(self):
        """第二发要等第一发的响应，实测晚 5 秒多——这个代价必须留在数据里。"""
        doc = build_record(
            "2310104222", "2F-B033", "100455834",
            "2026-09-10 08:00:00", "2026-09-10 22:00:00",
            datetime(2026, 9, 9, 7, 0, 5, 394000),
            datetime(2026, 9, 9, 7, 0, 5, 828000),
            "预约失败: 设备在该时间段内已被预约",
        )
        self.assertEqual(doc["offset_ms"], 5394)

    def test_off_peak_attempts_are_tagged_apart(self):
        """迟到保护/补约那些尝试不能和 7:00 那一波混在一起算竞争度。"""
        doc = build_record(
            "2310104222", "2F-B070", "100455871",
            "2026-09-09 09:00:00", "2026-09-09 22:00:00",
            datetime(2026, 9, 9, 7, 53, 3, 304000),
            datetime(2026, 9, 9, 7, 53, 3, 632000),
            "✅ 09-09 · 09:00-22:00 · 2F-B070 · 预约成功",
        )
        self.assertEqual(doc["phase"], "off_peak")
        self.assertEqual(doc["outcome"], "success")

    def test_survives_garbage_timestamps(self):
        doc = build_record("p", "s", "1", "", "", datetime(2026, 9, 9, 7, 0),
                           datetime(2026, 9, 9, 7, 0), "")
        self.assertIsNone(doc["weekday"])
        self.assertIsNone(doc["begin_min"])


class NeverBreaksBookingTests(unittest.TestCase):
    def test_a_dead_database_does_not_raise(self):
        """记账失败绝不能把一次成功的抢座变成失败。"""
        with patch.object(attempt_log, "_collection",
                          side_effect=RuntimeError("mongo 挂了")):
            attempt_log.record("p", "2F-B070", "1", "2026-09-10 08:00:00",
                               "2026-09-10 22:00:00", datetime.now(), "预约成功")

    def test_insert_failure_does_not_raise(self):
        col = Mock()
        col.insert_one.side_effect = RuntimeError("写超时")
        with patch.object(attempt_log, "_collection", return_value=col):
            attempt_log.record("p", "2F-B070", "1", "2026-09-10 08:00:00",
                               "2026-09-10 22:00:00", datetime.now(), "预约成功")
        col.insert_one.assert_called_once()


class ChronicSelfConflictTests(unittest.TestCase):
    """挑出「每天都在撞自己已有预约」的账号，给 7:00 的队列排序用。"""

    def _pids(self, rows, **kwargs):
        col = Mock()
        col.aggregate.return_value = iter(rows)
        with patch.object(attempt_log, "_collection", return_value=col):
            result = attempt_log.chronic_self_conflict_pids(
                today=date(2026, 9, 14), **kwargs)
        return result, col.aggregate.call_args.args[0]

    def test_returns_the_pids_the_pipeline_found(self):
        pids, _ = self._pids([{"_id": "2310104222", "days": 4}])
        self.assertEqual(pids, {"2310104222"})

    def test_only_looks_at_the_seven_oclock_wave(self):
        """补约、迟到保护换约那些非高峰尝试不该参与判定——它们本来就慢。"""
        _, pipeline = self._pids([])
        self.assertEqual(pipeline[0]["$match"]["phase"], "rush")

    def test_window_is_counted_back_from_today(self):
        _, pipeline = self._pids([], lookback_days=7)
        self.assertEqual(pipeline[0]["$match"]["target_date"], {"$gte": "2026-09-07"})

    def test_a_day_with_any_success_does_not_count(self):
        """那天还是抢到了，就说明备选里有活路，不该因为撞了一发就被降级。"""
        _, pipeline = self._pids([])
        day_filter = pipeline[2]["$match"]["$and"]
        self.assertIn({"outcomes": "self_conflict"}, day_filter)
        self.assertIn({"outcomes": {"$ne": "success"}}, day_filter)

    def test_threshold_is_the_number_of_such_days(self):
        _, pipeline = self._pids([], min_days=2)
        self.assertEqual(pipeline[-1]["$match"], {"days": {"$gte": 2}})

    def test_turning_it_off_skips_the_query_entirely(self):
        col = Mock()
        with patch.object(attempt_log, "_collection", return_value=col):
            self.assertEqual(
                attempt_log.chronic_self_conflict_pids(lookback_days=0), set())
        col.aggregate.assert_not_called()

    def test_a_dead_database_just_means_no_demotion(self):
        """这只是个排序优化，查不动就按原顺序排，绝不能把 7:00 搞挂。"""
        with patch.object(attempt_log, "_collection",
                          side_effect=RuntimeError("mongo 挂了")):
            self.assertEqual(attempt_log.chronic_self_conflict_pids(), set())


if __name__ == "__main__":
    unittest.main()
