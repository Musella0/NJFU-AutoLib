"""从学号拆出学院号和班级号，供管理后台做两层分类。

本科学号是「入学年份(2) + 学院号(2) + 班级号(2) + 班内序号(2)…」，例如 2000102115
的末 6 位 102115 → 10 院 21 班 15 号。只有 2 打头的本科号段才按这条规则拆：
3/8 打头的号段（研究生等，如 3241700745、8241711859）同样是 10 位纯数字，但末 6 位
拆出来是 70 院、71 班这种根本不存在的学院。长度、字符或号段对不上的学号一律标为
未识别，让它落进「未分类」——按错误的规则硬拆会凭空造出一个不存在的学院，
比少分一档更糟。
"""

import re
from typing import Any, Dict

# 已知可拆的本科号段：2 打头的 10 位学号（20xx 级入学年份）。
UNDERGRAD_ID_PATTERN = re.compile(r"^2\d{9}$")

UNRECOGNIZED: Dict[str, Any] = {
    "college_code": "",
    "class_code": "",
    "class_key": "",
    "seq_code": "",
    "id_recognized": False,
}


def parse_student_id(pid: Any) -> Dict[str, Any]:
    """返回学号的分类字段；无法识别时各码为空字符串。"""
    text = str(pid or "").strip()
    if not UNDERGRAD_ID_PATTERN.match(text):
        return dict(UNRECOGNIZED)
    college_code, class_code, seq_code = text[-6:-4], text[-4:-2], text[-2:]
    return {
        "college_code": college_code,
        "class_code": class_code,
        # 班级号在不同学院之间会重号，单独拿 class_code 分组会把两个学院的
        # 21 班并成一堆，所以对外统一用带学院前缀的复合键。
        "class_key": f"{college_code}-{class_code}",
        "seq_code": seq_code,
        "id_recognized": True,
    }
