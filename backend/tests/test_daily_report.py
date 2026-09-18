"""锁住抢座日报的口径：邮件里每个数怎么来的，以及模型挂了要怎么办。"""

import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

from utils import daily_report
from utils.daily_report import (
    ARRIVAL_COLLECTION,
    ATTEMPT_COLLECTION,
    OCCUPANCY_COLLECTION,
    PROTECT_EVENT_COLLECTION,
    SEGMENT_COLLECTION,
    USER_COLLECTION,
    VISIT_COLLECTION,
    build_subject,
    collect_facts,
    render_text,
)


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = list(documents or [])

    @staticmethod
    def _match(doc, query):
        for key, value in query.items():
            actual = doc.get(key)
            if isinstance(value, dict):
                if "$gte" in value and (actual is None or actual < value["$gte"]):
                    return False
                if "$lt" in value and (actual is None or actual >= value["$lt"]):
                    return False
                if "$regex" in value and not str(actual or "").startswith(
                        value["$regex"].lstrip("^")):
                    return False
            elif actual != value:
                return False
        return True

    def find(self, query=None, projection=None):
        return [d for d in self.documents if self._match(d, query or {})]

    def find_one(self, query=None, projection=None):
        found = self.find(query, projection)
        return found[0] if found else None

    def count_documents(self, query):
        return len(self.find(query))


class FakeDb:
    def __init__(self, cols):
        self.cols = {name: FakeCollection(docs) for name, docs in cols.items()}

    def __getitem__(self, name):
        return self.cols.setdefault(name, FakeCollection())


def _attempt(pid, outcome, offset_ms, latency_ms=200, begin="2026-09-18 09:30:00",
             end="2026-09-18 20:00:00", seat="2F-B070"):
    return {
        "pid": pid, "outcome": outcome, "offset_ms": offset_ms, "latency_ms": latency_ms,
        "begin": begin, "end": end, "seat_name": seat, "target_date": begin[:10],
        "phase": "rush", "message": "",
        "fired_at": datetime(2026, 9, 17, 7, 0, 0) + __import__("datetime").timedelta(
            milliseconds=offset_ms),
    }


def _db_with(**overrides):
    cols = {
        ATTEMPT_COLLECTION: [
            _attempt("a", "success", 119),
            _attempt("b", "taken", 157),
            _attempt("b", "success", 366, latency_ms=159),
            _attempt("c", "success", 515, latency_ms=365),
        ],
        OCCUPANCY_COLLECTION: [
            {"date": "2026-09-18", "seats_total": 2749,
             "booked": {"pre": 0, "rush": 356, "post": 477},
             "fill": [{"offset": 1, "booked": 74}, {"offset": 5, "booked": 146},
                      {"offset": 900, "booked": 601}, {"offset": 12345, "booked": 900}],
             "rooms": [{"name": "二层B区", "seats": 100, "booked": 80},
                       {"name": "五层A区", "seats": 100, "booked": 10},
                       {"name": "三层A区", "seats": 100, "booked": 40},
                       {"name": "七层南侧", "seats": 100, "booked": 20}]},
            {"date": "2026-09-17", "seats_total": 2749, "booked": {"post": 400}},
        ],
        SEGMENT_COLLECTION: [],
        PROTECT_EVENT_COLLECTION: [
            {"date": "2026-09-16", "action": "skipped_in_library", "mode": "shift",
             "in_library": True, "detail": "在馆探针", "seat_name": "2F-B013",
             "target_time": "2026-09-16 09:30:00-22:00:00"},
            {"date": "2026-09-16", "action": "shifted", "mode": "shift",
             "in_library": False, "detail": "", "seat_name": "5F-A073",
             "target_time": "2026-09-16 08:30:00-22:00:00",
             "new_time": "2026-09-16 09:30:00-22:00:00"},
            {"date": "2026-09-16", "action": "shift_failed", "mode": "shift",
             "in_library": None, "detail": "座位已被预约", "seat_name": "3F-A176",
             "target_time": "2026-09-16 10:00:00-22:00:00"},
        ],
        ARRIVAL_COLLECTION: [
            {"target_time": "2026-09-16 09:30:00-22:00:00", "status": "arrived"},
            {"target_time": "2026-09-16 09:30:00-22:00:00", "status": "arrived"},
            {"target_time": "2026-09-16 10:00:00-22:00:00", "status": "violated"},
            {"target_time": "2026-09-17 09:30:00-22:00:00", "status": "pending"},
        ],
        USER_COLLECTION: [
            {"late_protection": "True", "late_protection_mode": None,
             "late_protection_count": 2, "verified": True, "credential_failures": 0},
            {"late_protection": "True", "late_protection_mode": "cancel",
             "late_protection_count": 0, "verified": True, "credential_failures": 0},
            {"late_protection": "False", "verified": False, "credential_failures": 1},
        ],
        VISIT_COLLECTION: [],
    }
    cols.update(overrides)
    return FakeDb(cols)


class CollectFactsTests(unittest.TestCase):
    def setUp(self):
        self.facts = collect_facts(_db_with(), date(2026, 9, 17))

    def test_target_date_comes_from_the_attempts_not_the_calendar(self):
        self.assertEqual(self.facts["target_date"], "2026-09-18")
        self.assertEqual(self.facts["target_weekday"], "五")

    def test_an_account_that_retries_into_a_seat_counts_as_won_once(self):
        rush = self.facts["rush"]
        self.assertEqual((rush["accounts"], rush["won"], rush["lost"]), (3, 3, 0))
        self.assertEqual(rush["attempts"], 4)
        self.assertEqual(rush["retry_accounts"], 1)
        self.assertEqual(rush["outcomes"], {"success": 3, "taken": 1})

    def test_timing_is_reported_from_the_gate_not_from_wall_clock(self):
        rush = self.facts["rush"]
        self.assertEqual((rush["first_offset_ms"], rush["last_offset_ms"]), (119, 515))
        self.assertEqual(rush["latency_ms"]["max"], 365)

    def test_only_the_listed_fill_points_reach_the_mail(self):
        offsets = [f["offset"] for f in self.facts["occupancy"]["fill"]]
        self.assertEqual(offsets, [1, 5, 900])

    def test_rooms_are_ranked_by_occupancy_both_ends(self):
        occ = self.facts["occupancy"]
        self.assertEqual(occ["rooms_hot"][0]["name"], "二层B区")
        self.assertEqual(occ["rooms_cold"][0]["name"], "五层A区")
        self.assertEqual(occ["pct_post"], 17.4)

    def test_protection_counts_come_from_events_of_the_previous_day(self):
        prot = self.facts["late_protect"]
        self.assertEqual(prot["date"], "2026-09-16")
        self.assertEqual((prot["probe_veto"], prot["shifted"], prot["shift_failed"]), (1, 1, 1))
        # 探针没给结论的那次必须单独报出来：白名单文案失效就是这么冒头的
        self.assertEqual(prot["probe_unknown"], 1)
        self.assertEqual(prot["enabled_users"], 2)
        self.assertEqual(prot["arrival"], {"arrived": 2, "violated": 1,
                                           "cancelled": 0, "pending": 0})
        self.assertEqual(prot["violation_rate"], 33.3)

    def test_falls_back_to_visit_log_source_when_there_are_no_events(self):
        db = _db_with(**{
            PROTECT_EVENT_COLLECTION: [],
            VISIT_COLLECTION: [{"planned_begin": datetime(2026, 9, 16, 9, 30),
                                "source": "late_protect_probe"}],
        })
        prot = collect_facts(db, date(2026, 9, 17))["late_protect"]
        self.assertFalse(prot["has_events"])
        self.assertEqual(prot["probe_veto"], 1)

    def test_account_health_is_counted_across_all_configs(self):
        self.assertEqual(self.facts["health"],
                         {"accounts": 3, "unverified": 1, "credential_failures": 1})

    def test_a_day_without_any_attempt_still_produces_a_report(self):
        facts = collect_facts(_db_with(**{ATTEMPT_COLLECTION: []}), date(2026, 9, 17))
        self.assertEqual(facts["rush"], {"attempts": 0})
        self.assertEqual(facts["target_date"], "2026-09-18")
        self.assertIn("今天没有任何抢座记录", render_text(facts, None))


class ReadOnlyGuardTests(unittest.TestCase):
    """日报不许写库——这条是靠句柄本身保证的，不是靠代码评审看出来的。"""

    def test_write_methods_are_not_reachable_through_the_handle(self):
        db = daily_report.ReadOnlyDb(_db_with())
        for method in ("insert_one", "update_one", "delete_many", "drop", "bulk_write"):
            with self.assertRaises(AttributeError):
                getattr(db[ATTEMPT_COLLECTION], method)

    def test_attribute_style_collection_access_is_blocked(self):
        db = daily_report.ReadOnlyDb(_db_with())
        with self.assertRaises(AttributeError):
            db.user_config_info

    def test_reads_still_work(self):
        db = daily_report.ReadOnlyDb(_db_with())
        self.assertEqual(len(db[ATTEMPT_COLLECTION].find({})), 4)

    def test_collect_facts_wraps_a_raw_handle_before_touching_it(self):
        """传进来的是裸库也照样只读：读得到数，写方法取不到。"""
        raw = _db_with()
        seen = []
        raw.cols[ATTEMPT_COLLECTION].insert_one = lambda *a, **k: seen.append(a)

        facts = collect_facts(raw, date(2026, 9, 17))

        self.assertEqual(facts["rush"]["accounts"], 3)
        self.assertEqual(seen, [])
        with self.assertRaises(AttributeError):
            daily_report.ReadOnlyDb(raw)[ATTEMPT_COLLECTION].insert_one


class ModelOutputSanitizerTests(unittest.TestCase):
    """模型写的字只当文本用，而且是被截断、去控制字符、限条数之后的文本。"""

    def test_non_string_items_are_dropped_not_rendered(self):
        cleaned = daily_report._clean_summary(
            {"headline": "标题", "highlights": ["好", {"evil": 1}, None, "也好"],
             "concerns": [], "verdict": "完"})
        self.assertEqual(cleaned["highlights"], ["好", "也好"])

    def test_control_characters_and_line_breaks_are_stripped(self):
        cleaned = daily_report._clean_summary(
            {"headline": "标题\n伪造的第二行\u2028第三行", "highlights": [],
             "concerns": [], "verdict": ""})
        self.assertEqual(cleaned["headline"], "标题伪造的第二行第三行")

    def test_long_lines_and_long_lists_are_capped(self):
        cleaned = daily_report._clean_summary(
            {"headline": "标题", "highlights": ["x" * 500] * 20,
             "concerns": [], "verdict": "v"})
        self.assertEqual(len(cleaned["highlights"]), daily_report.MAX_LINES)
        self.assertEqual(len(cleaned["highlights"][0]), daily_report.MAX_LINE_CHARS)

    def test_a_missing_headline_throws_the_whole_answer_away(self):
        self.assertIsNone(daily_report._clean_summary(
            {"headline": "", "highlights": ["a"], "concerns": [], "verdict": ""}))
        self.assertIsNone(daily_report._clean_summary({"highlights": "不是数组"}))

    def test_subject_never_contains_model_text(self):
        facts = collect_facts(_db_with(), date(2026, 9, 17))
        evil = {"headline": "退订链接 http://evil.example", "highlights": [],
                "concerns": [], "verdict": ""}
        self.assertNotIn("evil", build_subject(facts))
        self.assertIn("evil", render_text(facts, evil))  # 正文里有，标题里没有

    def test_model_text_is_escaped_in_the_html_body(self):
        facts = collect_facts(_db_with(), date(2026, 9, 17))
        html = daily_report.render_html(facts, {
            "headline": "<img src=x onerror=alert(1)>", "highlights": [],
            "concerns": [], "verdict": ""})
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.facts = collect_facts(_db_with(), date(2026, 9, 17))

    def test_subject_carries_the_two_numbers_worth_seeing_on_a_lock_screen(self):
        self.assertEqual(build_subject(self.facts),
                         "[AutoLib] 09-17 抢座日报 · 3/3 抢到 · 全馆 17.4%")

    def test_data_block_is_included_even_when_the_model_answered(self):
        text = render_text(self.facts, {"headline": "今天很顺", "highlights": ["全员抢到"],
                                        "concerns": [], "verdict": "没事"})
        self.assertIn("今天很顺", text)
        self.assertIn("【7:00 抢座", text)
        self.assertIn("开闸后 119ms 打出第一发", text)
        self.assertIn("响应中位 200ms", text)

    def test_model_failure_degrades_to_data_only(self):
        text = render_text(self.facts, None)
        self.assertIn("AI 总结没出来", text)
        self.assertIn("【迟到保护", text)


class LlmSummaryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {
            "LLM_API_KEY": "k", "LLM_BASE_URL": "https://api.deepseek.com",
            "REPORT_LLM_MODEL": "deepseek-v4-flash"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _session(self, *responses):
        session = Mock()
        session.post.side_effect = responses
        return session

    def test_json_schema_rejection_retries_as_plain_json_object(self):
        ok = Mock(status_code=200)
        ok.json.return_value = {"choices": [{"message": {"content": __import__("json").dumps(
            {"headline": "h", "highlights": ["a"], "concerns": [], "verdict": "v"})}}]}
        session = self._session(Mock(status_code=400), ok)
        result = daily_report._llm_summary({"x": 1}, session=session)

        self.assertEqual(result["headline"], "h")
        self.assertEqual(session.post.call_count, 2)
        second = session.post.call_args_list[1][1]["json"]
        self.assertEqual(second["response_format"], {"type": "json_object"})

    def test_trailing_noise_after_the_json_object_is_ignored(self):
        """2026-09-17 第一封日报的真实死法：JSON 后面跟了第二段内容。"""
        ok = Mock(status_code=200)
        ok.json.return_value = {"choices": [{"message": {"content":
            '{"headline": "h", "highlights": ["a"], "concerns": [], "verdict": "v"}\n'
            '这是模型多说的一段话{"又一个": "对象"}'}}]}
        result = daily_report._llm_summary({"x": 1}, session=self._session(ok))
        self.assertEqual(result["headline"], "h")

    def test_prose_before_the_json_is_ignored(self):
        ok = Mock(status_code=200)
        ok.json.return_value = {"choices": [{"message": {"content":
            '好的，这是你要的日报：\n{"headline": "h", "highlights": [], '
            '"concerns": [], "verdict": ""}'}}]}
        self.assertEqual(daily_report._llm_summary(
            {"x": 1}, session=self._session(ok))["headline"], "h")

    def test_a_reply_without_any_json_is_rejected(self):
        bad = Mock(status_code=200)
        bad.json.return_value = {"choices": [{"message": {"content": "我拒绝回答"}}]}
        self.assertIsNone(daily_report._llm_summary({"x": 1}, session=self._session(bad)))

    def test_network_error_is_swallowed(self):
        session = Mock()
        session.post.side_effect = RuntimeError("boom")
        self.assertIsNone(daily_report._llm_summary({"x": 1}, session=session))

    def test_malformed_model_output_is_rejected(self):
        bad = Mock(status_code=200)
        bad.json.return_value = {"choices": [{"message": {"content": '{"headline": 12}'}}]}
        self.assertIsNone(daily_report._llm_summary({"x": 1}, session=self._session(bad)))

    def test_missing_credentials_skip_the_model_entirely(self):
        with patch.dict("os.environ", {"LLM_API_KEY": "", "LLM_MODEL": "",
                                       "REPORT_LLM_MODEL": ""}):
            self.assertIsNone(daily_report._llm_summary({"x": 1}, session=Mock()))


class SendTests(unittest.TestCase):
    def test_dry_run_never_touches_the_mailer(self):
        with patch("utils.notify.send_email") as send:
            ok = daily_report.send_daily_report(date(2026, 9, 17), dry_run=True,
                                                use_ai=False, db=_db_with())
        self.assertTrue(ok)
        send.assert_not_called()

    def test_report_is_sent_as_text_plus_html(self):
        with patch("utils.notify.send_email", return_value=True) as send, \
                patch.object(daily_report, "DEFAULT_TO", "me@example.net"):
            ok = daily_report.send_daily_report(date(2026, 9, 17), use_ai=False,
                                                db=_db_with())
        self.assertTrue(ok)
        args, kwargs = send.call_args
        self.assertEqual(args[0], "me@example.net")
        self.assertIn("抢座日报", args[1])
        self.assertIn("<pre", kwargs["html"])

    def test_a_failed_delivery_is_reported_as_failure(self):
        with patch("utils.notify.send_email", return_value=False), \
                patch.object(daily_report, "DEFAULT_TO", "me@example.net"):
            self.assertFalse(daily_report.send_daily_report(
                date(2026, 9, 17), use_ai=False, db=_db_with()))


if __name__ == "__main__":
    unittest.main()
