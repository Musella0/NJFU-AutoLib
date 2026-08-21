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
        "priority": 0,
    }


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
