import unittest
from datetime import datetime
from unittest.mock import MagicMock

from utils import study_time


def _ev(kind, hh, mm, day=14):
    return {"kind": kind, "console_kind": 8, "create_time": datetime(2026, 9, day, hh, mm)}


class SummarizeEventsTest(unittest.TestCase):
    def test_real_day_from_operate_rec(self):
        # 2026-09-14 一条真实流水，对照图书馆网页的操作记录：08:58 系统置生效，
        # 09:11 闸机签到，11:35 / 17:20 暂离，12:18 / 18:02 返回，21:04 现场预约台结束
        events = [
            _ev(1, 7, 0, day=13), _ev(2, 8, 58), _ev(4, 9, 11), _ev(8, 11, 35),
            _ev(16, 12, 18), _ev(8, 17, 20), _ev(16, 18, 2), _ev(32, 21, 4),
        ]
        s = study_time.summarize_events(events, datetime(2026, 9, 14, 22, 0))
        self.assertEqual(s["actual_checkin_at"], datetime(2026, 9, 14, 9, 11))
        self.assertEqual(s["actual_end_at"], datetime(2026, 9, 14, 21, 4))
        self.assertEqual(s["end_source"], "event")
        # 09:11→21:04 共 713 分钟，离座 43 + 42 = 85 分钟
        self.assertEqual(s["away_minutes"], 85)
        self.assertEqual(s["actual_minutes"], 713 - 85)

    def test_unordered_input_is_sorted(self):
        events = [_ev(32, 12, 0), _ev(16, 11, 0), _ev(4, 9, 0), _ev(8, 10, 0)]
        s = study_time.summarize_events(events, None)
        self.assertEqual(s["away_minutes"], 60)
        self.assertEqual(s["actual_minutes"], 120)

    def test_effective_without_gate_checkin_counts_zero(self):
        # 只有「已生效」没有闸机签到：人没进馆，不算时间
        s = study_time.summarize_events([_ev(1, 7, 0), _ev(2, 9, 0), _ev(32, 9, 30)],
                                        datetime(2026, 9, 14, 22, 0))
        self.assertEqual(s["actual_minutes"], 0)
        self.assertEqual(s["end_source"], "no_checkin")

    def test_no_checkin_counts_zero(self):
        s = study_time.summarize_events([_ev(1, 7, 0)], datetime(2026, 9, 14, 22, 0))
        self.assertEqual(s["actual_minutes"], 0)
        self.assertEqual(s["end_source"], "no_checkin")
        self.assertIsNone(s["actual_checkin_at"])

    def test_missing_end_falls_back_to_planned_end(self):
        events = [_ev(4, 9, 0), _ev(8, 20, 0)]
        s = study_time.summarize_events(events, datetime(2026, 9, 14, 22, 0))
        self.assertEqual(s["end_source"], "planned")
        # 20:00 出去一直没回来，离座算到 22:00 收口
        self.assertEqual(s["away_minutes"], 120)
        self.assertEqual(s["actual_minutes"], 11 * 60)

    def test_back_without_leave_is_ignored(self):
        events = [_ev(4, 9, 0), _ev(16, 10, 0), _ev(32, 12, 0)]
        s = study_time.summarize_events(events, None)
        self.assertEqual(s["away_minutes"], 0)
        self.assertEqual(s["actual_minutes"], 180)

    def test_no_end_and_no_planned_end(self):
        s = study_time.summarize_events([_ev(4, 9, 0)], None)
        self.assertEqual(s["end_source"], "unknown")
        self.assertEqual(s["actual_minutes"], 0)


class StoreAndClearTest(unittest.TestCase):
    def test_store_summary_writes_events_and_marker(self):
        db = MagicMock()
        now = datetime(2026, 9, 14, 22, 10)
        records = [_ev(4, 9, 0), _ev(32, 12, 0)]
        s = study_time.store_summary(db, "u1", records, None, now)
        self.assertEqual(s["actual_minutes"], 180)
        args, _ = db.visit_logs.update_one.call_args
        self.assertEqual(args[0], {"uuid": "u1"})
        payload = args[1]["$set"]
        self.assertEqual(payload["actual_synced_at"], now)
        self.assertEqual([e["kind"] for e in payload["events"]], [4, 32])
        self.assertEqual(payload["events"][0]["at"], datetime(2026, 9, 14, 9, 0))

    def test_open_interval_is_left_for_later(self):
        # 没有「结束」事件：人可能还坐着，不写、不打同步标记，等下次
        db = MagicMock()
        now = datetime(2026, 9, 14, 12, 30)
        s = study_time.store_summary(
            db, "u1", [_ev(4, 9, 0), _ev(8, 12, 0)], datetime(2026, 9, 14, 12, 0), now)
        self.assertIsNone(s)
        db.visit_logs.update_one.assert_not_called()

    def test_open_interval_closes_by_planned_end_when_stale(self):
        # 计划结束过去一天多还没有结束事件，按计划结束收口，不再等
        db = MagicMock()
        now = datetime(2026, 9, 15, 22, 10)
        s = study_time.store_summary(
            db, "u1", [_ev(4, 9, 0)], datetime(2026, 9, 14, 12, 0), now)
        self.assertEqual(s["end_source"], "planned")
        self.assertEqual(s["actual_minutes"], 180)
        db.visit_logs.update_one.assert_called_once()

    def test_blur_rounds_to_half_hour_and_ignores_away(self):
        b = study_time.blur_summary({
            "actual_checkin_at": datetime(2026, 9, 14, 8, 58),
            "actual_end_at": datetime(2026, 9, 14, 21, 4),
            "away_minutes": 628,
        })
        self.assertEqual(b["actual_checkin_at"], datetime(2026, 9, 14, 9, 0))
        self.assertEqual(b["actual_end_at"], datetime(2026, 9, 14, 21, 0))
        self.assertEqual(b["actual_minutes"], 12 * 60)

    def test_blur_user_details_strips_events(self):
        db = MagicMock()
        db.visit_logs.find.return_value = [{
            "uuid": "u1",
            "actual_checkin_at": datetime(2026, 9, 14, 9, 16),
            "actual_end_at": datetime(2026, 9, 14, 11, 40),
        }]
        self.assertEqual(study_time.blur_user_details(db, "p1"), 1)
        args, _ = db.visit_logs.update_one.call_args
        self.assertEqual(args[0], {"uuid": "u1"})
        self.assertTrue(args[1]["$set"]["blurred"])
        self.assertEqual(args[1]["$set"]["actual_minutes"], 120)
        self.assertEqual(set(args[1]["$unset"]), {"events", "away_minutes"})

    def test_export_rows_render_events_as_text(self):
        db = MagicMock()
        db.visit_logs.find.return_value.sort.return_value = [{
            "planned_begin": datetime(2026, 9, 14, 9, 0),
            "planned_end": datetime(2026, 9, 14, 22, 0),
            "planned_duration_minutes": 780,
            "seat_name": "3FA-018", "location": "三楼夹层",
            "actual_checkin_at": datetime(2026, 9, 14, 8, 58),
            "actual_end_at": datetime(2026, 9, 14, 21, 4),
            "away_minutes": 628, "actual_minutes": 98,
            "events": [{"kind": 4, "console_kind": 8, "at": datetime(2026, 9, 14, 9, 11)},
                       {"kind": 32, "console_kind": 32, "at": datetime(2026, 9, 14, 21, 4)}],
        }]
        rows = study_time.export_rows(db, "p1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["日期"], "2026-09-14")
        self.assertEqual(rows[0]["在馆(分钟)"], 98)
        self.assertEqual(rows[0]["精度"], "精确")
        self.assertEqual(rows[0]["操作流水"],
                         "2026-09-14 09:11 已签到(闸机); 2026-09-14 21:04 已结束(现场预约台)")

    def test_clear_user_details_unsets_every_detail_field(self):
        db = MagicMock()
        db.visit_logs.update_many.return_value.modified_count = 3
        self.assertEqual(study_time.clear_user_details(db, "p1"), 3)
        args, _ = db.visit_logs.update_many.call_args
        self.assertEqual(args[0]["pid"], "p1")
        self.assertEqual(set(args[1]["$unset"]), set(study_time.DETAIL_FIELDS))
        self.assertIn("events", args[1]["$unset"])
        self.assertIn("actual_synced_at", args[1]["$unset"])


if __name__ == "__main__":
    unittest.main()
