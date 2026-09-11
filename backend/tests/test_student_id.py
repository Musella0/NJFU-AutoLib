import unittest

from utils.student_id import parse_student_id


class StudentIdTests(unittest.TestCase):
    def test_last_six_digits_split_into_college_class_and_sequence(self):
        parsed = parse_student_id("2000102115")
        self.assertTrue(parsed["id_recognized"])
        self.assertEqual(parsed["college_code"], "10")
        self.assertEqual(parsed["class_code"], "21")
        self.assertEqual(parsed["seq_code"], "15")

    def test_class_key_carries_the_college_so_reused_class_numbers_stay_apart(self):
        # 10 院和 30 院各有一个 21 班，只按班级号分组会把两拨人并成一堆。
        first = parse_student_id("2000102115")
        second = parse_student_id("2330302115")
        self.assertEqual(first["class_code"], second["class_code"])
        self.assertNotEqual(first["class_key"], second["class_key"])
        self.assertEqual(first["class_key"], "10-21")

    def test_ids_that_do_not_match_the_rule_stay_unclassified(self):
        # 旧编号只有 9 位，硬按末 6 位拆会拆出一个不存在的 31 院。
        for pid in ("202312981", "", None, "20001021150", "2310a02115", "  "):
            parsed = parse_student_id(pid)
            self.assertFalse(parsed["id_recognized"], pid)
            self.assertEqual(parsed["college_code"], "")
            self.assertEqual(parsed["class_code"], "")
            self.assertEqual(parsed["class_key"], "")

    def test_non_undergrad_id_segments_stay_unclassified(self):
        # 3/8 打头的号段（研究生等）也是 10 位纯数字，硬拆末 6 位会造出 70 院、71 院。
        for pid in ("3241700745", "3241700760", "8241711859"):
            parsed = parse_student_id(pid)
            self.assertFalse(parsed["id_recognized"], pid)
            self.assertEqual(parsed["college_code"], "")
            self.assertEqual(parsed["class_key"], "")

    def test_surrounding_whitespace_and_non_string_ids_are_tolerated(self):
        self.assertTrue(parse_student_id(" 2000102115 ")["id_recognized"])
        self.assertEqual(parse_student_id(2000102115)["college_code"], "10")


if __name__ == "__main__":
    unittest.main()
