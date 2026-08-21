import unittest

from utils.reservation_blackout import (
    ReservationBlackoutError,
    find_any_reservation_conflict,
    find_reservation_conflict,
    require_reservation_allowed,
    validate_pause_range,
)


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query):
        effects = set(query["effect_type"]["$in"])
        return [
            item for item in self.documents
            if item.get("status") == query["status"] and item.get("effect_type") in effects
        ]


def review(status="confirmed", effect="venue_closed"):
    return {
        "_id": "review-1",
        "status": status,
        "effect_type": effect,
        "source_title": "临时闭馆通知",
        "source_url": "https://lib.njfu.edu.cn/info/1008/1234.htm",
        "approved_pause_from": "2026-08-24T00:00:00+08:00",
        "approved_pause_until": "2026-08-25T08:00:00+08:00",
        "revision": 2,
    }


class ReservationBlackoutTests(unittest.TestCase):
    def test_confirmed_overlap_is_blocked(self):
        conflict = find_reservation_conflict(
            FakeCollection([review()]),
            "2026-08-24 08:00:00",
            "2026-08-24 12:00:00",
        )
        self.assertEqual(conflict["review_id"], "review-1")
        self.assertEqual(conflict["revision"], 2)

    def test_pending_and_irrelevant_records_do_not_block(self):
        collection = FakeCollection([
            review(status="pending_review"),
            review(effect="irrelevant"),
        ])
        self.assertIsNone(find_reservation_conflict(
            collection, "2026-08-24 08:00:00", "2026-08-24 12:00:00"
        ))

    def test_half_open_boundaries_do_not_overlap(self):
        collection = FakeCollection([review()])
        self.assertIsNone(find_reservation_conflict(
            collection, "2026-08-23 20:00:00", "2026-08-24 00:00:00"
        ))
        self.assertIsNone(find_reservation_conflict(
            collection, "2026-08-25 08:00:00", "2026-08-25 10:00:00"
        ))

    def test_multi_segment_operation_is_blocked_if_one_segment_overlaps(self):
        conflict = find_any_reservation_conflict(FakeCollection([review()]), [
            ("2026-08-23 08:00:00", "2026-08-23 10:00:00"),
            ("2026-08-24 10:00:00", "2026-08-24 12:00:00"),
        ])
        self.assertIsNotNone(conflict)

    def test_exception_has_stable_api_payload(self):
        with self.assertRaises(ReservationBlackoutError) as captured:
            require_reservation_allowed(
                FakeCollection([review()]),
                "2026-08-24 08:00:00",
                "2026-08-24 12:00:00",
            )
        self.assertEqual(captured.exception.to_payload()["code"], "RESERVATION_BLACKOUT")

    def test_invalid_range_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_pause_range("2026-08-25 08:00", "2026-08-24 08:00")


if __name__ == "__main__":
    unittest.main()
