"""锁住占用快照的解析和抢座队列分配的算法行为。

这两个模块目前都没接进主流程（快照每天拍，分配器只出报告），但正因为没人
盯着它们跑，算法一旦悄悄错了不会有任何报警——所以该测的一条都不能少：
匈牙利指派要真的最优，同一张座位不能同时分给两个账号，
apply_plan 那颗雷必须还在。
"""

import itertools
import unittest
from datetime import date, datetime
from unittest.mock import patch

from utils import seat_allocator, seat_snapshot
from utils.seat_allocator import ContentionModel, allocate, hungarian
from utils.seat_snapshot import (
    _build_room_doc,
    _hhmm_to_minutes,
    _to_minutes,
    seat_is_free,
)


def _dev(name, resv=(), **extra):
    """造一个 ic-web 风格的设备记录；resv 传 ("HH:MM","HH:MM") 的列表。"""
    day = date(2026, 9, 10)
    info = []
    for begin, end in resv:
        bh, bm = (int(x) for x in begin.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        info.append({
            "startTime": int(datetime(day.year, day.month, day.day, bh, bm).timestamp() * 1000),
            "endTime": int(datetime(day.year, day.month, day.day, eh, em).timestamp() * 1000),
            "resvStatus": 1027,
        })
    record = {
        "devName": name,
        "openStart": "07:30",
        "openEnd": "22:00",
        "devStatus": 0,
        "openState": 1,
        "resvInfo": info,
    }
    record.update(extra)
    return record


class SnapshotParsingTests(unittest.TestCase):
    def test_hhmm_parsing(self):
        self.assertEqual(_hhmm_to_minutes("07:30"), 450)
        self.assertEqual(_hhmm_to_minutes("22:00"), 1320)
        self.assertIsNone(_hhmm_to_minutes("不开放"))
        self.assertIsNone(_hhmm_to_minutes(1789007400000))

    def test_timestamp_to_minutes(self):
        day = date(2026, 9, 10)
        noon = int(datetime(2026, 9, 10, 12, 30).timestamp() * 1000)
        self.assertEqual(_to_minutes(noon, day), 750)
        # 别的日子的单子不该混进这一天
        other = int(datetime(2026, 9, 11, 12, 30).timestamp() * 1000)
        self.assertIsNone(_to_minutes(other, day))
        # 次日零点当作当天收尾，不能丢
        midnight = int(datetime(2026, 9, 11, 0, 0).timestamp() * 1000)
        self.assertEqual(_to_minutes(midnight, day), 1440)
        self.assertIsNone(_to_minutes(None, day))

    def test_seat_is_free_uses_half_open_intervals(self):
        booked = [[570, 1320]]           # 09:30-22:00
        self.assertFalse(seat_is_free(booked, 600, 1320))
        self.assertTrue(seat_is_free(booked, 450, 570))   # 到 09:30 为止，不算冲突
        self.assertFalse(seat_is_free(booked, 450, 571))
        self.assertTrue(seat_is_free([], 450, 1320))

    def test_build_room_doc(self):
        area = {"room_id": "100455346", "room_name": "二层B区", "floor_name": "图书馆二层"}
        devices = [
            _dev("2F-B070", [("07:30", "22:00")]),
            _dev("2F-B068"),
            _dev("2F-B060", [("09:00", "12:00"), ("14:00", "18:00")]),
            _dev("2F-B050", devStatus=1),
            _dev("bad.name", [("09:00", "10:00")]),   # 带点的名字当不了 Mongo 字段名
        ]
        doc = _build_room_doc(area, devices, date(2026, 9, 10), "20260910",
                              "0705", datetime(2026, 9, 9, 7, 5), "2000102115")

        self.assertEqual(doc["date"], "2026-09-10")
        self.assertEqual(doc["weekday"], 4)
        self.assertEqual(doc["open"], [450, 1320])
        self.assertEqual(doc["seats"]["2F-B070"], [[450, 1320]])
        self.assertEqual(doc["seats"]["2F-B068"], [])
        self.assertEqual(doc["seats"]["2F-B060"], [[540, 720], [840, 1080]])
        self.assertNotIn("bad.name", doc["seats"])
        self.assertEqual(doc["unavailable"], ["2F-B050"])
        self.assertEqual(doc["stats"]["seats"], 4)
        self.assertEqual(doc["stats"]["booked_seats"], 2)
        self.assertEqual(doc["stats"]["intervals"], 3)
        self.assertEqual(doc["stats"]["status_hist"], {"1027": 3})


class HungarianTests(unittest.TestCase):
    def test_matches_brute_force_optimum(self):
        cost = [
            [4.0, 1.0, 3.0, 9.0, 2.0],
            [2.0, 0.0, 5.0, 3.0, 8.0],
            [3.0, 2.0, 2.0, 1.0, 7.0],
        ]
        rows, cols = len(cost), len(cost[0])
        best = min(
            sum(cost[r][c] for r, c in enumerate(pick))
            for pick in itertools.permutations(range(cols), rows)
        )
        assignment = hungarian(cost)
        self.assertEqual(len(set(assignment)), rows)   # 一列不能给两行
        self.assertAlmostEqual(sum(cost[r][c] for r, c in enumerate(assignment)), best)

    def test_square_matrix(self):
        cost = [[1.0, 2.0], [2.0, 1.0]]
        self.assertEqual(hungarian(cost), [0, 1])

    def test_rejects_more_rows_than_columns(self):
        with self.assertRaises(ValueError):
            hungarian([[1.0], [2.0]])

    def test_empty(self):
        self.assertEqual(hungarian([]), [])


def _snapshots(days, room="R", booked_by_day=None, seats=("S1", "S2", "S3", "S4", "S5")):
    """造若干天的快照。booked_by_day: {date_str: {seat: [[b,e]]}}"""
    booked_by_day = booked_by_day or {}
    docs = []
    for day in days:
        booked = booked_by_day.get(day, {})
        docs.append({
            "date": day,
            "weekday": datetime.strptime(day, "%Y-%m-%d").date().isoweekday(),
            "tag": "0705",
            "room_id": room,
            "room_name": "测试区",
            "floor_name": "测试楼",
            "seats": {seat: list(booked.get(seat, [])) for seat in seats},
            "unavailable": [],
        })
    return docs


class ContentionModelTests(unittest.TestCase):
    def setUp(self):
        days = ["2026-09-06", "2026-09-07", "2026-09-08"]
        # S1 天天被人从开馆占到闭馆，S2~S5 天天空着
        booked = {day: {"S1": [[450, 1320]]} for day in days}
        self.model = ContentionModel(
            _snapshots(days, booked_by_day=booked),
            reference_date=date(2026, 9, 9),
        )
        self.window = [(480, 1320)]      # 08:00-22:00

    def test_contested_seat_scores_below_free_seat(self):
        contested = self.model.survival("S1", self.window)
        free = self.model.survival("S2", self.window)
        self.assertLess(contested, free - 0.3)

    def test_estimate_converges_with_more_evidence(self):
        """三天数据时先验还压着，攒够两三周就该收敛到接近 0 / 接近 1。"""
        days = [f"2026-08-{d:02d}" for d in range(19, 32)] + \
               [f"2026-09-0{d}" for d in range(1, 9)]
        booked = {day: {"S1": [[450, 1320]]} for day in days}
        model = ContentionModel(
            _snapshots(days, booked_by_day=booked),
            reference_date=date(2026, 9, 9),
        )
        self.assertLess(model.survival("S1", self.window), 0.2)
        self.assertGreater(model.survival("S2", self.window), 0.85)

    def test_unseen_seat_falls_back_to_room_prior(self):
        # 没在快照里出现过的座位不该被判成必胜或必败
        self.assertAlmostEqual(self.model.survival("完全没见过的座位", self.window), 0.5)

    def test_window_matters(self):
        # S1 只在 07:30-22:00 被占，问 06:00-07:00 这段它是空的
        self.assertGreater(self.model.survival("S1", [(360, 420)]), 0.8)

    def test_sample_size(self):
        self.assertEqual(self.model.sample_size("S1"), 3)
        self.assertEqual(self.model.sample_size("没见过"), 0)


class AllocateTests(unittest.TestCase):
    def setUp(self):
        days = ["2026-09-06", "2026-09-07", "2026-09-08"]
        booked = {day: {"S1": [[450, 1320]]} for day in days}
        self.model = ContentionModel(
            _snapshots(days, booked_by_day=booked),
            reference_date=date(2026, 9, 9),
        )
        self.weekday = 4

    def test_no_two_accounts_get_the_same_seat(self):
        """2026-09-09 有三个账号把 2F-B036 排在前两位，这种自己人打自己人要被拆开。"""
        accounts = [
            {"pid": "A", "seat_list": ["S1"], "windows": [(480, 1320)],
             "late_protection": False, "priority": 0},
            {"pid": "B", "seat_list": ["S1"], "windows": [(480, 1320)],
             "late_protection": False, "priority": 0},
            {"pid": "C", "seat_list": ["S1"], "windows": [(480, 1320)],
             "late_protection": False, "priority": 0},
        ]
        rows = allocate(accounts, self.model, self.weekday)
        assigned = [row["assigned_seat"] for row in rows]
        self.assertEqual(len(assigned), 3)
        self.assertEqual(len(set(assigned)), 3, f"座位撞车了: {assigned}")

    def test_riskiest_account_goes_first(self):
        """只盯着热门座位的人应该排进第一波，冷门座位的人排后面。"""
        accounts = [
            {"pid": "cold", "seat_list": ["S2"], "windows": [(480, 1320)],
             "late_protection": False, "priority": 0},
            {"pid": "hot", "seat_list": ["S1"], "windows": [(480, 1320)],
             "late_protection": False, "priority": 0},
        ]
        rows = allocate(accounts, self.model, self.weekday, wave_size=1)
        self.assertEqual(rows[0]["pid"], "hot")
        self.assertEqual(rows[0]["wave"], 1)
        self.assertEqual(rows[1]["wave"], 2)
        self.assertGreater(rows[0]["suggested_priority"], rows[1]["suggested_priority"])

    def test_report_row_carries_own_seat_estimates(self):
        accounts = [
            {"pid": "A", "seat_list": ["S1", "S2"], "windows": [(480, 1320)],
             "late_protection": True, "priority": 0},
        ]
        row = allocate(accounts, self.model, self.weekday)[0]
        self.assertEqual([o["seat"] for o in row["own_seats"]], ["S1", "S2"])
        self.assertEqual(row["best_own_p"], max(o["p"] for o in row["own_seats"]))
        self.assertTrue(row["assigned_is_own"])
        self.assertEqual(row["assigned_seat"], "S2")   # 同样是自己的座位，挑存活率高的
        self.assertEqual(row["queue_position"], 1)

    def test_late_protection_raises_severity(self):
        plain = {"pid": "A", "seat_list": ["S1"], "windows": [(480, 1320)],
                 "late_protection": False, "priority": 0}
        protected = dict(plain, pid="B", late_protection=True)
        self.assertGreater(seat_allocator.severity(protected), seat_allocator.severity(plain))

    def test_accounts_without_windows_or_seats_are_skipped(self):
        accounts = [
            {"pid": "A", "seat_list": [], "windows": [(480, 1320)],
             "late_protection": False, "priority": 0},
            {"pid": "B", "seat_list": ["S1"], "windows": [],
             "late_protection": False, "priority": 0},
        ]
        self.assertEqual(allocate(accounts, self.model, self.weekday), [])


class FakeSnapshotCollection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query):
        return [d for d in self.documents
                if all(d.get(k) == v for k, v in query.items())]


class FakeDb:
    def __init__(self, documents):
        self._col = FakeSnapshotCollection(documents)

    def __getitem__(self, _name):
        return self._col


class RushDeltaTests(unittest.TestCase):
    """7:00 前后做差，才分得开「抢不过」和「本来就没了」。"""

    def _db(self, pre_booked, post_booked):
        def doc(tag, booked):
            return {
                "date": "2026-09-10", "tag": tag, "room_id": "R",
                "room_name": "二层B区", "floor_name": "图书馆二层",
                "captured_at": datetime(2026, 9, 9, 6, 45),
                "seats": {s: list(booked.get(s, [])) for s in ("S1", "S2", "S3", "S4")},
            }
        return FakeDb([doc("pre", pre_booked), doc("post", post_booked)])

    def test_splits_pre_existing_from_rush(self):
        full = [[450, 1320]]
        db = self._db(
            pre_booked={"S1": full},                 # 7:00 之前就没了
            post_booked={"S1": full, "S2": full},    # S2 是 7:00 那一波被抢走的
        )
        delta = seat_snapshot.rush_delta(db, date(2026, 9, 10))
        totals = delta["totals"]
        self.assertEqual(totals["seats"], 4)
        self.assertEqual(totals["gone_before"], 1)
        self.assertEqual(totals["taken_in_rush"], 1)
        self.assertEqual(totals["free_after"], 2)
        self.assertEqual(delta["rooms"][0]["rush_seats"], ["S2"])

    def test_window_scopes_the_comparison(self):
        """只订了上午的座位，不该算成下午时段的竞争者。"""
        db = self._db(pre_booked={}, post_booked={"S1": [[450, 720]]})   # 07:30-12:00
        afternoon = seat_snapshot.rush_delta(
            db, date(2026, 9, 10), window=(840, 1320))                   # 14:00-22:00
        self.assertEqual(afternoon["totals"]["taken_in_rush"], 0)
        morning = seat_snapshot.rush_delta(db, date(2026, 9, 10), window=(480, 1320))
        self.assertEqual(morning["totals"]["taken_in_rush"], 1)

    def test_missing_pre_snapshot_yields_nothing_to_compare(self):
        db = FakeDb([{
            "date": "2026-09-10", "tag": "post", "room_id": "R",
            "room_name": "二层B区", "floor_name": "图书馆二层",
            "captured_at": datetime(2026, 9, 9, 7, 5), "seats": {"S1": []},
        }])
        delta = seat_snapshot.rush_delta(db, date(2026, 9, 10))
        self.assertEqual(delta["rooms"], [])
        self.assertIn("没有可对比", seat_snapshot.format_rush_delta(delta))

    def test_parse_window(self):
        self.assertEqual(seat_snapshot.parse_window("08:00-22:00"), (480, 1320))
        with self.assertRaises(ValueError):
            seat_snapshot.parse_window("22:00-08:00")


class PreRushColumnTests(unittest.TestCase):
    def test_pre_model_reported_separately_from_post(self):
        """座位在 7:00 前就没了 vs 抢不过，是两件事，报告里不能混成一个数。"""
        days = ["2026-09-06", "2026-09-07", "2026-09-08"]
        full = {day: {"S1": [[450, 1320]]} for day in days}
        post_model = ContentionModel(_snapshots(days, booked_by_day=full),
                                     reference_date=date(2026, 9, 9))
        # pre 那张里 S1 一直是空的 —— 说明它是在 7:00 那一波才被抢走的
        pre_model = ContentionModel(_snapshots(days), reference_date=date(2026, 9, 9))
        accounts = [{"pid": "A", "seat_list": ["S1"], "windows": [(480, 1320)],
                     "late_protection": False, "priority": 0}]

        row = allocate(accounts, post_model, 4, pre_model=pre_model)[0]
        self.assertLess(row["best_own_p"], 0.4)
        self.assertGreater(row["best_own_p_before_rush"], 0.8)

    def test_pre_column_is_none_without_pre_snapshots(self):
        model = ContentionModel(_snapshots(["2026-09-08"]), reference_date=date(2026, 9, 9))
        accounts = [{"pid": "A", "seat_list": ["S1"], "windows": [(480, 1320)],
                     "late_protection": False, "priority": 0}]
        self.assertIsNone(allocate(accounts, model, 4)[0]["best_own_p_before_rush"])


class SnapshotSessionHandoffTests(unittest.TestCase):
    """06:45 建的会话必须传给 07:01 那张——抢座窗口里绝不能再登一次。

    这段逻辑挂在一个模块级全局上，错了不会报错，只会「rush 那张再也没拍成」，
    等发现时数据已经缺了一片，所以必须钉住。
    """

    def setUp(self):
        import scheduler_runner
        self.runner = scheduler_runner
        self.runner._snapshot_session = None
        self.captured = []
        self.logins = []

        sentinel = object()

        def fake_login(*_a, **_kw):
            self.logins.append(sentinel)
            return sentinel

        def fake_capture(tag=None, library=None, **_kw):
            self.captured.append((tag, library))

        self.sentinel = sentinel
        self.patches = [
            patch("utils.seat_snapshot.login", fake_login),
            patch("utils.seat_snapshot.capture", fake_capture),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.runner._snapshot_session = None

    def test_pre_logs_in_once_and_rush_reuses_it(self):
        self.runner.run_seat_snapshot_task('pre', True, False)
        self.runner.run_seat_snapshot_task('rush', False, True)
        self.runner.run_seat_snapshot_task('post', False, False)

        self.assertEqual(len(self.logins), 1, "整个早上只该登录一次")
        self.assertEqual([tag for tag, _ in self.captured], ['pre', 'rush', 'post'])
        self.assertIs(self.captured[0][1], self.sentinel)
        self.assertIs(self.captured[1][1], self.sentinel, "rush 没复用 pre 的会话")
        self.assertIsNone(self.captured[2][1], "post 该自己现场登录")

    def test_rush_skips_when_no_session_was_handed_over(self):
        """pre 那张失败时，rush 宁可不拍，也不在抢座窗口里现场登录。"""
        self.runner.run_seat_snapshot_task('rush', False, True)
        self.assertEqual(self.captured, [])
        self.assertEqual(self.logins, [])

    def test_failed_pre_does_not_leave_a_stale_session(self):
        with patch("utils.seat_snapshot.capture", side_effect=RuntimeError("网关炸了")):
            self.runner.run_seat_snapshot_task('pre', True, False)
        self.assertIsNone(self.runner._snapshot_session)
        self.runner.run_seat_snapshot_task('rush', False, True)
        self.assertEqual(self.captured, [])


class GuardrailTests(unittest.TestCase):
    def test_apply_plan_still_refuses(self):
        """禁止改用户配置。这颗雷被拆掉的那天，这条测试必须先红。"""
        with self.assertRaises(NotImplementedError):
            seat_allocator.apply_plan({"rows": []})


if __name__ == "__main__":
    unittest.main()
