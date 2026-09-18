"""锁住「全馆预约概况」的汇总口径：设置页折线图的每个数都从这里来。"""

import unittest
from datetime import date, datetime
from unittest.mock import patch

from utils import occupancy_stats
from utils.occupancy_stats import CURVE_FROM, CURVE_STEP, STATS_COLLECTION, summarize_day
from utils.seat_snapshot import SNAPSHOT_COLLECTION


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = list(documents or [])

    @staticmethod
    def _match(doc, query):
        for key, value in query.items():
            if isinstance(value, dict) and "$in" in value:
                if doc.get(key) not in value["$in"]:
                    return False
            elif doc.get(key) != value:
                return False
        return True

    def find(self, query=None, projection=None):
        return [d for d in self.documents if self._match(d, query or {})]

    def distinct(self, key, query=None):
        return sorted({d[key] for d in self.find(query)})

    def replace_one(self, query, doc, upsert=False):
        self.documents = [d for d in self.documents if not self._match(d, query)]
        self.documents.append(doc)

    def create_index(self, *args, **kwargs):
        pass


class FakeDb:
    def __init__(self):
        self.cols = {SNAPSHOT_COLLECTION: FakeCollection(), STATS_COLLECTION: FakeCollection()}

    def __getitem__(self, name):
        return self.cols[name]


def _room(tag, room_id, name, seats, day="2026-09-15"):
    return {
        "date": day, "tag": tag, "room_id": room_id,
        "room_name": name, "floor_name": "图书馆",
        "captured_at": datetime(2026, 9, 14, 7, 5),
        "seats": seats,
    }


class SummarizeDayTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        snap = self.db[SNAPSHOT_COLLECTION]
        # 两个区域：A 区 3 张（2 张有预约），B 区 2 张（1 张有预约）
        snap.documents += [
            _room("pre", "1", "A区", {"S1": [], "S2": [], "S3": []}),
            _room("pre", "2", "B区", {"T1": [], "T2": []}),
            _room("rush", "1", "A区", {"S1": [[510, 1320]], "S2": [], "S3": []}),
            _room("rush", "2", "B区", {"T1": [], "T2": []}),
            _room("post", "1", "A区", {"S1": [[510, 1320]], "S2": [[450, 720]], "S3": []}),
            _room("post", "2", "B区", {"T1": [], "T2": [[840, 1320]]}),
        ]

    def test_totals_and_rooms(self):
        s = summarize_day(self.db, date(2026, 9, 15))
        self.assertEqual(s["seats_total"], 5)
        self.assertEqual(s["booked"], {"pre": 0, "rush": 1, "post": 3})
        self.assertEqual(s["weekday"], 2)
        self.assertEqual([r["name"] for r in s["rooms"]], ["A区", "B区"])
        self.assertEqual(s["rooms"][0]["booked"], 2)
        self.assertEqual(s["rooms"][0]["booked_rush"], 1)
        self.assertEqual(s["rooms"][1]["seats"], 2)

    def test_curve_counts_seats_touching_each_half_hour(self):
        s = summarize_day(self.db, date(2026, 9, 15))
        occ = s["curve"]["occupied"]
        slot = lambda m: (m - CURVE_FROM) // CURVE_STEP
        self.assertEqual(occ[slot(420)], 0)       # 7:00 还没人
        self.assertEqual(occ[slot(450)], 1)       # 7:30 只有 S2
        self.assertEqual(occ[slot(510)], 2)       # 8:30 S1+S2
        self.assertEqual(occ[slot(690)], 2)       # 11:30-12:00 S2 还没结束
        self.assertEqual(occ[slot(720)], 1)       # 12:00 S2 结束了
        self.assertEqual(occ[slot(840)], 2)       # 14:00 T2 进来
        self.assertEqual(occ[slot(1290)], 2)      # 最后一格 21:30
        self.assertEqual(len(occ), (1320 - 420) // 30)

    def test_missing_rush_is_none_not_zero(self):
        snap = self.db[SNAPSHOT_COLLECTION]
        snap.documents = [d for d in snap.documents if d["tag"] != "rush"]
        s = summarize_day(self.db, date(2026, 9, 15))
        self.assertIsNone(s["booked"]["rush"])
        self.assertIsNone(s["rooms"][0]["booked_rush"])

    def test_no_post_snapshot_means_nothing(self):
        self.assertIsNone(summarize_day(self.db, date(2026, 9, 16)))


# 别的测试文件会把 utils.config 整个换成假模块（没有 DB_NAME），
# 一起跑时这里别被它牵连
@patch.object(occupancy_stats.config, "DB_NAME", "AutoLib", create=True)
class RefreshTests(unittest.TestCase):
    def test_backfills_missing_days_and_keeps_old_ones(self):
        db = FakeDb()
        snap = db[SNAPSHOT_COLLECTION]
        for day in ("2026-09-13", "2026-09-14", "2026-09-15"):
            snap.documents.append(_room("post", "1", "A区", {"S1": [[450, 1320]]}, day=day))
        # 09-13 早就汇总过了，而且被人改过：补齐不该动它
        db[STATS_COLLECTION].documents.append({"date": "2026-09-13", "marker": "old"})

        class Client:
            def __getitem__(self, _): return db
            def close(self): pass

        written = occupancy_stats.refresh(target=date(2026, 9, 15), client=Client())
        self.assertEqual(written, ["2026-09-14", "2026-09-15"])
        dates = {d["date"]: d for d in db[STATS_COLLECTION].documents}
        self.assertEqual(dates["2026-09-13"].get("marker"), "old")
        self.assertEqual(dates["2026-09-15"]["booked"]["post"], 1)

    def test_target_is_recomputed_even_if_present(self):
        db = FakeDb()
        db[SNAPSHOT_COLLECTION].documents.append(_room("post", "1", "A区", {"S1": [[450, 1320]]}))
        db[STATS_COLLECTION].documents.append({"date": "2026-09-15", "marker": "stale"})

        class Client:
            def __getitem__(self, _): return db
            def close(self): pass

        written = occupancy_stats.refresh(target=date(2026, 9, 15), client=Client())
        self.assertEqual(written, ["2026-09-15"])
        doc = db[STATS_COLLECTION].documents[0]
        self.assertNotIn("marker", doc)
        self.assertEqual(doc["seats_total"], 1)


if __name__ == "__main__":
    unittest.main()
