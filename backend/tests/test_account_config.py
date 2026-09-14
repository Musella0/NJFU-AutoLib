import unittest
from datetime import datetime, timedelta

from utils.account_config import (
    account_config_for_client,
    classify_result,
    CREDENTIAL_INVALID_RESULT,
    CREDENTIAL_REVERIFIED_RESULT,
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


class ResultStateTests(unittest.TestCase):
    """面板和后台都拿这个状态判断「今天到底怎么样」，正常状态不能被涂成故障。"""

    def test_a_rest_day_is_not_a_failure(self):
        """用户自己把周二设成休息，压根没打算约——后台以前把它数进「异常」。"""
        self.assertEqual(classify_result("周二休息，已跳过预约"), "skipped")

    def test_a_closed_library_is_not_a_failure(self):
        self.assertEqual(
            classify_result("学校闭馆，已跳过预约：图书馆闭馆通知；暂停 09-20 至 09-22"),
            "skipped")

    def test_a_1358_hold_waiting_to_be_swapped_is_not_a_failure(self):
        """超出图书馆 31 小时窗口的段：先占 13:58，等换约任务接手，还没有结论。"""
        self.assertEqual(
            classify_result("⏳ 第1段 17:30-22:00: 已用 3F-A176 占位（13:58 起），"
                            "09-14 10:32 自动换回本段时间"),
            "pending")
        self.assertEqual(
            classify_result("⏳ 第2段 18:30-22:00: 图书馆最多提前 31 小时预约，"
                            "已排到 09-14 11:32 自动补约"),
            "pending")

    def test_the_real_segment_failing_is_the_anomaly(self):
        """换约真的没约上，那一段才算异常——这是唯一该报红的情况。"""
        self.assertEqual(
            classify_result("❌ 09-15 17:30-22:00: 所有座位预约失败，详细原因:\n"
                            "3F-A176(100456431): …设备在该时间段内已被预约"),
            "failed")

    def test_a_hold_next_to_a_booked_segment_stays_green(self):
        """第1段约上了、第2段占位待换约：整体不该算异常。"""
        self.assertEqual(
            classify_result("✅ 09-15 · 08:30-12:00 · 2F-A348 · 预约成功\n"
                            "⏳ 第2段 18:30-22:00: 已用 2F-A348 占位（13:58 起），"
                            "09-14 11:32 自动换回本段时间"),
            "success")

    def test_one_failed_segment_is_not_hidden_by_another_that_worked(self):
        """多段里失败那半才是要盯的，不能因为另一行写着"成功"就当没事。"""
        self.assertEqual(
            classify_result("✅ 09-15 · 08:30-12:00 · 2F-A348 · 预约成功\n"
                            "❌ 第2段 18:30-22:00: 所有座位预约失败"),
            "failed")

    def test_an_expired_password_still_needs_attention(self):
        self.assertEqual(classify_result(CREDENTIAL_INVALID_RESULT), "failed")
        self.assertEqual(classify_result(CREDENTIAL_REVERIFIED_RESULT), "success")

    def test_nothing_run_yet_is_its_own_state(self):
        self.assertEqual(classify_result(""), "none")
        self.assertEqual(classify_result(None), "none")
        self.assertEqual(classify_result("   "), "none")

    def test_text_we_do_not_recognise_errs_towards_showing_it(self):
        """宁可多报一个，也别把真出事的那条藏起来。"""
        self.assertEqual(classify_result("没有可用的座位进行预约"), "failed")


if __name__ == "__main__":
    unittest.main()
