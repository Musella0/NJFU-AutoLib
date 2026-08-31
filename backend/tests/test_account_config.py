import unittest
from datetime import datetime, timedelta

from utils.account_config import (
    account_config_for_client,
    default_account_config,
    default_time_config,
    merge_account_documents,
    normalize_notify_mode,
)


class AccountConfigTests(unittest.TestCase):
    def test_defaults_match_the_web_configuration(self):
        defaults = default_account_config()
        self.assertEqual(defaults["seat_list"], [])
        self.assertEqual(defaults["mode"], "week_time")
        self.assertEqual(defaults["time"]["week_time"]["5"], ["08:00-20:00"])
        self.assertEqual(defaults["time"]["week_time"]["7"], ["08:00-22:00"])
        self.assertEqual(defaults["notify_mode"], "simple")

    def test_unknown_notify_modes_fall_back_to_the_quiet_default(self):
        self.assertEqual(normalize_notify_mode("full"), "full")
        self.assertEqual(normalize_notify_mode(" FULL "), "full")
        self.assertEqual(normalize_notify_mode("simple"), "simple")
        # 老账号没有这个字段，按精简处理，不再收每天的成功回执。
        self.assertEqual(normalize_notify_mode(None), "simple")
        self.assertEqual(normalize_notify_mode("verbose"), "simple")

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

    def test_string_timestamp_is_ordered_with_datetime_values(self):
        merged = merge_account_documents(
            [
                {
                    "is_reserved": "False",
                    "updated_at": datetime(2026, 1, 1, 8, 0),
                },
                {
                    "is_reserved": "True",
                    "updated_at": "2026-01-02 08:00:00",
                },
            ],
            web_uid="123",
            pid="123",
        )
        self.assertEqual(merged["is_reserved"], "True")

    def test_cross_owner_records_are_reowned_to_the_student(self):
        merged = merge_account_documents(
            [
                {
                    "web_uid": "another-student",
                    "pid": "123",
                    "owned_seat": {"2F-A001": [{"uuid": "existing"}]},
                    "updated_at": datetime(2026, 1, 1),
                },
                {
                    "web_uid": "guest_retry",
                    "pid": "123",
                    "verified": True,
                    "updated_at": datetime(2026, 1, 2),
                },
            ],
            web_uid="123",
            pid="123",
        )
        self.assertEqual(merged["web_uid"], "123")
        self.assertEqual(merged["pid"], "123")
        self.assertEqual(
            merged["owned_seat"]["2F-A001"][0]["uuid"],
            "existing",
        )

    def test_client_config_excludes_all_stored_credentials(self):
        stored = {
            "_id": "database-id",
            "pid": "123",
            "seat_list": ["二楼A区001"],
            "web_password": "web-secret",
            "vpn_password": "encrypted-vpn-secret",
            "lib_password": "encrypted-library-secret",
        }

        public = account_config_for_client(stored)

        self.assertEqual(public["pid"], "123")
        self.assertEqual(public["seat_list"], ["二楼A区001"])
        self.assertNotIn("_id", public)
        self.assertNotIn("web_password", public)
        self.assertNotIn("vpn_password", public)
        self.assertNotIn("lib_password", public)
        self.assertIn("vpn_password", stored)


if __name__ == "__main__":
    unittest.main()
