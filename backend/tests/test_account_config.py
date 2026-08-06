import unittest
from datetime import datetime, timedelta

from utils.account_config import (
    default_account_config,
    default_time_config,
    merge_account_documents,
)


class AccountConfigTests(unittest.TestCase):
    def test_defaults_match_the_web_configuration(self):
        defaults = default_account_config()
        self.assertEqual(defaults["seat_list"], [])
        self.assertEqual(defaults["mode"], "week_time")
        self.assertEqual(defaults["time"]["week_time"]["5"], ["08:00-20:00"])
        self.assertEqual(defaults["time"]["week_time"]["7"], ["08:00-22:00"])

    def test_merge_retains_fields_missing_from_newer_document(self):
        old_time = datetime.now() - timedelta(days=1)
        new_time = datetime.now()
        merged = merge_account_documents(
            [
                {
                    "web_uid": "guest_old",
                    "pid": "123",
                    "seat_list": ["二楼A区001"],
                    "time": default_time_config(),
                    "updated_at": old_time,
                },
                {
                    "web_uid": "registered",
                    "pid": "123",
                    "mode": "week_time",
                    "verified": True,
                    "updated_at": new_time,
                },
            ],
            web_uid="registered",
            pid="123",
        )
        self.assertEqual(merged["seat_list"], ["二楼A区001"])
        self.assertEqual(merged["time"]["week_time"]["1"], ["08:00-22:00"])
        self.assertTrue(merged["verified"])
        self.assertEqual(merged["web_uid"], "registered")

    def test_newer_explicit_value_wins(self):
        merged = merge_account_documents(
            [
                {
                    "seat_list": ["二楼A区001"],
                    "updated_at": datetime(2026, 1, 1),
                },
                {
                    "seat_list": [],
                    "updated_at": datetime(2026, 1, 2),
                },
            ],
            web_uid="registered",
            pid="123",
        )
        self.assertEqual(merged["seat_list"], [])


if __name__ == "__main__":
    unittest.main()
