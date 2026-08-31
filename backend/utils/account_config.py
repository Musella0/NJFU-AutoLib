"""Helpers for durable account configuration and duplicate-safe merging."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable


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


def account_config_for_client(document: Dict[str, Any]) -> Dict[str, Any]:
    """Return an independent account document with all credentials removed."""
    public_document = deepcopy(document)
    for field in CLIENT_SENSITIVE_FIELDS:
        public_document.pop(field, None)
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
