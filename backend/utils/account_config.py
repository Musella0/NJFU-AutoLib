"""Helpers for durable account configuration and duplicate-safe merging."""

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_WEEK_SEGMENTS = {
    "1": ["08:00-22:00"],
    "2": ["08:00-22:00"],
    "3": ["08:00-22:00"],
    "4": ["08:00-22:00"],
    "5": ["08:00-20:00"],
    "6": ["08:00-22:00"],
    "7": ["08:00-22:00"],
}

# 通知范围：simple 只在异常时发信（预约失败、午休失败等），full 连每天的
# 预约成功回执一起发。闭馆类通知不受此开关影响，两种模式都会收到。
NOTIFY_MODES = ("simple", "full")
DEFAULT_NOTIFY_MODE = "simple"

# ---- 时间配置校验 ----
# 存进库的非法时段不会在保存时报错，而是等到第二天 07:00 抢座才炸（或者更糟：
# 悄悄少约一段）。所以格式、时长、重叠一律在写库前拦掉。
RESERVATION_MODES = ("week_time", "tomorrow", "after_tomorrow")
REST_VALUES = ("休息", "off")
# 图书馆的最低预约时长，和 scheduled_task.calculate_reservation_time 里那道
# 7200 秒的过滤是同一条规则——那边是静默丢弃，这里提前告诉用户。
MIN_SEGMENT_MINUTES = 120
MAX_SEGMENTS_PER_DAY = 6
WEEK_LABELS = ("一", "二", "三", "四", "五", "六", "日")
_SEGMENT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def _parse_segment(text: str) -> Optional[Tuple[int, int]]:
    """"HH:MM-HH:MM" → (起始分钟, 结束分钟)；格式不对返回 None。"""
    match = _SEGMENT_RE.match(text)
    if not match:
        return None
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    return h1 * 60 + m1, h2 * 60 + m2


def _validate_day_segments(raw: Any, label: str) -> Optional[str]:
    """校验某一天的时间配置，返回错误说明；None 表示合法。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw in REST_VALUES:
            return None
        items: List[Any] = [raw]
    elif isinstance(raw, list):
        if not raw:
            return f"{label}没有任何时段，不预约请把这天设为休息"
        if len(raw) > MAX_SEGMENTS_PER_DAY:
            return f"{label}最多只能设置 {MAX_SEGMENTS_PER_DAY} 个时段"
        items = raw
    else:
        return f"{label}的时间配置格式无效"

    parsed: List[Tuple[int, int, str]] = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, str):
            return f"{label}第 {index} 段的时间配置格式无效"
        # 列表里混进 "休息" 说明前端状态串了，这天到底约不约无法判断，直接拒掉
        if item in REST_VALUES:
            return f"{label}的时段列表里不能混入「{item}」"
        bounds = _parse_segment(item)
        if bounds is None:
            return f"{label}第 {index} 段「{item}」格式无效，应形如 09:30-12:00"
        begin, end = bounds
        if begin >= end:
            return f"{label}第 {index} 段「{item}」的结束时间必须晚于开始时间"
        if end - begin < MIN_SEGMENT_MINUTES:
            return (f"{label}第 {index} 段「{item}」不足 {MIN_SEGMENT_MINUTES} 分钟，"
                    f"图书馆不接受这么短的预约")
        parsed.append((begin, end, item))

    # 按开始时间排序后只需比相邻两段：前一段的结束越过后一段的开始就是重叠
    order = sorted(range(len(parsed)), key=lambda i: parsed[i][0])
    for prev, cur in zip(order, order[1:]):
        if parsed[cur][0] < parsed[prev][1]:
            return (f"{label}第 {cur + 1} 段「{parsed[cur][2]}」与"
                    f"第 {prev + 1} 段「{parsed[prev][2]}」时间重叠")
    return None


def validate_time_config(time_config: Any) -> Optional[str]:
    """
    校验整份时间配置，返回第一条错误说明；None 表示合法。

    只看结构和数值，不关心用户处于哪种模式——前端两种模式都会把 week_time
    一起提交，存进去的每一天早晚都会被用上。
    """
    if not isinstance(time_config, dict):
        return "时间配置格式无效"

    week_time = time_config.get("week_time")
    if week_time is not None:
        if not isinstance(week_time, dict):
            return "每周时间配置格式无效"
        for key, raw in week_time.items():
            if str(key) not in ("1", "2", "3", "4", "5", "6", "7"):
                return f"每周时间配置里有无效的星期「{key}」"
            error = _validate_day_segments(raw, f"周{WEEK_LABELS[int(key) - 1]}")
            if error:
                return error

    for key, label in (("tomorrow", "明天"), ("after_tomorrow", "后天")):
        if key in time_config:
            error = _validate_day_segments(time_config[key], label)
            if error:
                return error
    return None


CLIENT_SENSITIVE_FIELDS = {
    "_id",
    "web_password",
    "vpn_password",
    "lib_password",
}


def normalize_notify_mode(value: Any) -> str:
    """Map missing or unknown values onto the quiet default."""
    text = str(value or "").strip().lower()
    return text if text in NOTIFY_MODES else DEFAULT_NOTIFY_MODE


def default_time_config() -> Dict[str, Any]:
    """Return an independent copy of the UI's default weekly schedule."""
    return {"week_time": deepcopy(DEFAULT_WEEK_SEGMENTS)}


def default_account_config() -> Dict[str, Any]:
    """Fields that must be persisted when a library account is first added."""
    return {
        "seat_list": [],
        "mode": "week_time",
        "time": default_time_config(),
        "is_reserved": "True",
        "late_protection": "False",
        "notify_mode": DEFAULT_NOTIFY_MODE,
        "priority": 0,
    }


# 定时任务连续几次被 CAS 拒绝密码后写进 result 的提示；main.py 在用户重新验证成功时
# 靠它认出「这条 result 只是密码失效提示」并替换掉，别让红字一直挂到下次抢座。
CREDENTIAL_INVALID_RESULT = (
    "🔑 学校统一身份认证密码已失效（可能你刚改过密码），自动预约已暂停。\n"
    "请到「配置」页重新填写密码并点「验证并保存」，验证通过后自动恢复。"
)
CREDENTIAL_REVERIFIED_RESULT = "✅ 新密码验证成功，自动预约已恢复，下个抢座窗口正常执行。"


# ---- result 文本的状态判定 ----
# 面板和后台都拿 result 那段文字判断「这个人今天到底怎么样」，以前两边各写各的：
# 后台是「不含"成功"就算异常」，面板是「不含"成功"且不含"已跳过"就标红」。
# 结果两种正常状态被当成了故障：
#   周二休息，已跳过预约                        → 用户自己设的休息，压根没打算约
#   ⏳ 第1段 17:30-22:00: 已用 3F-A176 占位…    → 超出图书馆 31 小时窗口，占着位等换约，
#                                                真正那一段还没到能约的时候
# 只有真正下单出错才算异常。判定收在这里一处，两边都用它，别再各写一遍。
RESULT_SUCCESS = "success"     # ✅ 约上了
RESULT_PENDING = "pending"     # ⏳ 占位/排队中，等补约任务接手，还没有结论
RESULT_SKIPPED = "skipped"     # 休息日、学校闭馆——按配置就是不约
RESULT_FAILED = "failed"       # ❌ 真的没约上，这才是异常
RESULT_NONE = "none"           # 还没跑过

FAILURE_MARKS = ("❌", "🔑")
SUCCESS_MARKS = ("✅", "预约成功")
SKIPPED_MARKS = ("已跳过",)
PENDING_MARKS = ("⏳",)


def classify_result(text: Any) -> str:
    """把 result 那段文字归成一个状态。

    先看有没有 ❌：多段预约里「第1段成功、第2段失败」这种，失败那半才是要盯的，
    不能因为另一行写着"成功"就当没事。剩下的按 成功 → 已跳过 → 排队中 排。
    认不出来的文字一律算失败——宁可多报一个，也别把真出事的那条藏起来。
    """
    content = str(text or "").strip()
    if not content:
        return RESULT_NONE
    if any(mark in content for mark in FAILURE_MARKS):
        return RESULT_FAILED
    if any(mark in content for mark in SUCCESS_MARKS):
        return RESULT_SUCCESS
    if any(mark in content for mark in SKIPPED_MARKS):
        return RESULT_SKIPPED
    if any(mark in content for mark in PENDING_MARKS):
        return RESULT_PENDING
    return RESULT_FAILED


def account_config_for_client(document: Dict[str, Any]) -> Dict[str, Any]:
    """Return an independent account document with all credentials removed.

    Top-level datetimes are rendered as local "YYYY-MM-DD HH:MM:SS" strings so the
    client never sees Flask's RFC 822 default.
    """
    public_document = deepcopy(document)
    for field in CLIENT_SENSITIVE_FIELDS:
        public_document.pop(field, None)
    for key, value in list(public_document.items()):
        if isinstance(value, datetime):
            public_document[key] = value.strftime("%Y-%m-%d %H:%M:%S")
    return public_document


def _updated_at(document: Dict[str, Any]) -> datetime:
    value = document.get("updated_at")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    object_id = document.get("_id")
    generation_time = getattr(object_id, "generation_time", None)
    if isinstance(generation_time, datetime):
        return generation_time
    return datetime.min.replace(tzinfo=timezone.utc)


def merge_account_documents(
    documents: Iterable[Dict[str, Any]],
    *,
    web_uid: str,
    pid: str,
) -> Dict[str, Any]:
    """
    Merge duplicate records in chronological order.

    Newer documents win for fields they explicitly contain, while fields that
    only exist in an older document are retained. This preserves saved seat and
    time configuration during guest-to-account migration.
    """
    ordered = sorted((dict(doc) for doc in documents), key=_updated_at)
    merged: Dict[str, Any] = {}
    for document in ordered:
        for key, value in document.items():
            if key in ("_id", "web_uid", "pid", "lib_password"):
                continue
            merged[key] = deepcopy(value)

    defaults = default_account_config()
    for key, value in defaults.items():
        merged.setdefault(key, deepcopy(value))
    merged["web_uid"] = web_uid
    merged["pid"] = pid
    merged["updated_at"] = max(
        (_updated_at(document) for document in ordered),
        default=datetime.now(timezone.utc),
    ).replace(tzinfo=None)
    return merged
