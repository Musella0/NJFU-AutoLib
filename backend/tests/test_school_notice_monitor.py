import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

try:
    import pymongo  # noqa: F401
except ModuleNotFoundError:
    fake_pymongo = types.ModuleType("pymongo")
    fake_pymongo.ASCENDING = 1
    fake_pymongo.MongoClient = Mock()
    sys.modules["pymongo"] = fake_pymongo

try:
    import werkzeug  # noqa: F401
except ModuleNotFoundError:
    fake_admin_credentials = types.ModuleType("utils.admin_credentials")

    class FakeAdminCredentialError(RuntimeError):
        pass

    fake_admin_credentials.AdminCredentialError = FakeAdminCredentialError
    fake_admin_credentials.load_credentials = Mock(return_value=None)
    sys.modules["utils.admin_credentials"] = fake_admin_credentials

from utils.school_notice_monitor import (
    SchoolNoticeMonitor,
    _definitively_irrelevant,
    analyze_notice,
    normalize_notice_url,
    parse_notice_content,
    parse_notice_list,
)


class FakeCursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


class MemoryCollection:
    def __init__(self):
        self.documents = []

    def create_index(self, *args, **kwargs):
        return kwargs.get("name", "index")

    def find_one(self, query):
        return next((doc.copy() for doc in self.documents if self._matches(doc, query)), None)

    def find(self, query):
        return FakeCursor(doc.copy() for doc in self.documents if self._matches(doc, query))

    def update_one(self, query, update, upsert=False):
        target = next((doc for doc in self.documents if self._matches(doc, query)), None)
        if target is None and upsert:
            target = dict(query)
            self.documents.append(target)
        if target is not None:
            target.update(update.get("$set", {}))

    @staticmethod
    def _matches(document, query):
        for key, expected in query.items():
            if isinstance(expected, dict):
                # These tests only need an empty retry cursor for operator queries.
                return False
            if document.get(key) != expected:
                return False
        return True


class MemoryDb:
    def __init__(self):
        self.school_notice_seen = MemoryCollection()
        self.school_notice_reviews = MemoryCollection()
        self.school_notice_monitor_state = MemoryCollection()


class FakeAI:
    configured = True

    def __init__(self, mismatch=False):
        self.mismatch = mismatch
        self.extract_calls = []
        self.review_calls = 0

    def extract(self, title, content, published, run_name):
        self.extract_calls.append(run_name)
        end = "2026-08-25T09:00:00+08:00" if self.mismatch and run_name == "B" else "2026-08-25T08:00:00+08:00"
        return {
            "effect_type": "venue_closed",
            "summary": "8月24日闭馆，次日恢复。",
            "proposed_pause_from": "2026-08-24T00:00:00+08:00",
            "proposed_pause_until": end,
            "affected_scope": "all_seats",
            "confidence": 0.98,
            "evidence": ["8月24日闭馆，8月25日8:00恢复开放。"],
            "ambiguities": [],
        }

    def review(self, content, first, second):
        self.review_calls += 1
        return {"supported": True, "evidence_valid": True, "conflicts": []}


class SchoolNoticeParsingTests(unittest.TestCase):
    def test_extracts_datalist_and_sorts_newest_first(self):
        html = '''<script>var dataList = [{"infolist":[
          {"iid":"1","url":"/info/1.htm","title":"旧","releasetime":100},
          {"iid":"2","url":"/info/2.htm","title":"新","releasetime":200}
        ]}]; window.after = true;</script>'''
        items = parse_notice_list(html)
        self.assertEqual([item["iid"] for item in items], ["2", "1"])

    def test_extracts_only_article_content(self):
        html = '''<div>导航</div><div class="article-content"><p>8月24日闭馆</p>
          <div><p>8月25日恢复</p></div></div><footer>页脚</footer>'''
        self.assertEqual(parse_notice_content(html), "8月24日闭馆\n8月25日恢复")

    def test_rejects_non_school_detail_url(self):
        with self.assertRaises(ValueError):
            normalize_notice_url("https://example.com/fake-notice")

    def test_three_ai_passes_must_agree_on_canonical_fields(self):
        ai = FakeAI()
        result = analyze_notice(
            ai,
            "临时闭馆通知",
            "8月24日闭馆，8月25日8:00恢复开放。",
            "2026-08-20T00:00:00+08:00",
        )
        self.assertEqual(ai.extract_calls, ["A", "B"])
        self.assertEqual(ai.review_calls, 1)
        self.assertEqual(result["ai_checks"], {
            "extractor_a": "passed",
            "extractor_b": "passed",
            "reviewer_c": "passed",
            "canonical_fingerprint_match": True,
        })

    def test_disagreement_is_exposed_for_human_review(self):
        result = analyze_notice(
            FakeAI(mismatch=True),
            "临时闭馆通知",
            "8月24日闭馆，8月25日8:00恢复开放。",
            None,
        )
        self.assertFalse(result["ai_checks"]["canonical_fingerprint_match"])
        self.assertIn("两次独立提取的关键字段不一致", result["ai_ambiguities"])

    def test_irrelevant_notice_is_suppressed_only_after_all_three_checks_pass(self):
        analysis = {
            "effect_type": "irrelevant",
            "ai_checks": {
                "extractor_a": "passed",
                "extractor_b": "passed",
                "reviewer_c": "passed",
                "canonical_fingerprint_match": True,
            },
        }
        self.assertTrue(_definitively_irrelevant(analysis))
        analysis["ai_checks"]["reviewer_c"] = "failed"
        self.assertFalse(_definitively_irrelevant(analysis))

    def test_first_run_only_builds_watermark_then_revision_queues_review(self):
        db = MemoryDb()
        monitor = SchoolNoticeMonitor(db, ai=FakeAI())
        original = {
            "source_id": "100",
            "source_url": "https://lib.njfu.edu.cn/info/1008/100.htm",
            "source_title": "通知",
            "source_published_at": None,
            "source_content": "原正文",
            "content_hash": "hash-v1",
        }
        revised = {**original, "source_content": "修订正文", "content_hash": "hash-v2"}
        monitor._load_current = Mock(side_effect=[[original], [revised]])
        monitor._create_review = Mock()
        utc8 = timezone(timedelta(hours=8))

        first = monitor.run(datetime(2026, 8, 21, 20, 0, tzinfo=utc8))
        second = monitor.run(datetime(2026, 8, 22, 20, 0, tzinfo=utc8))

        self.assertEqual(first["status"], "baseline")
        self.assertEqual(second["status"], "checked")
        self.assertEqual(monitor._create_review.call_count, 1)
        self.assertEqual(monitor._create_review.call_args.args[1], 2)


if __name__ == "__main__":
    unittest.main()
