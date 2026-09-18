"""Read-only reservation blocking for administrator-confirmed closure notices."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional


BEIJING_TZ = timezone(timedelta(hours=8))
BLOCKING_EFFECT_TYPES = {"venue_closed", "partial_closure"}


class ReservationBlackoutError(RuntimeError):
    """Raised before a reservation mutation when a confirmed closure overlaps."""

    def __init__(self, conflict: Dict[str, Any]):
        self.conflict = conflict
        super().__init__("学校闭馆期间暂停预约")

    def to_payload(self) -> Dict[str, Any]:
        return {
            "error": str(self),
            "code": "RESERVATION_BLACKOUT",
            "blackout": self.conflict,
        }


def parse_beijing_datetime(value: Any) -> datetime:
    """Parse stored ISO/local reservation timestamps into aware UTC+8 datetimes."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        if not text:
            raise ValueError("时间不能为空")
        parsed = datetime.fromisoformat(text)
    else:
        raise ValueError("时间格式无效")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def validate_pause_range(pause_from: Any, pause_until: Any) -> tuple[datetime, datetime]:
    begin = parse_beijing_datetime(pause_from)
    end = parse_beijing_datetime(pause_until)
    if end <= begin:
        raise ValueError("恢复时间必须晚于暂停开始时间")
    return begin, end


def _iter_confirmed(collection: Any) -> Iterable[Dict[str, Any]]:
    return collection.find({
        "status": "confirmed",
        "effect_type": {"$in": sorted(BLOCKING_EFFECT_TYPES)},
    })


def find_reservation_conflict(
    collection: Any,
    reservation_begin: Any,
    reservation_end: Any,
) -> Optional[Dict[str, Any]]:
    """Return the first confirmed closure overlapping ``[begin, end)``."""
    begin, end = validate_pause_range(reservation_begin, reservation_end)
    for review in _iter_confirmed(collection):
        try:
            pause_from, pause_until = validate_pause_range(
                review.get("approved_pause_from"),
                review.get("approved_pause_until"),
            )
        except (TypeError, ValueError):
            continue
        if begin < pause_until and end > pause_from:
            return {
                "review_id": str(review.get("_id", "")),
                "title": review.get("source_title", "图书馆闭馆通知"),
                "pause_from": pause_from.isoformat(timespec="minutes"),
                "pause_until": pause_until.isoformat(timespec="minutes"),
                "source_url": review.get("source_url", ""),
                "revision": int(review.get("revision", 1) or 1),
            }
    return None


def find_any_reservation_conflict(
    collection: Any,
    segments: Iterable[tuple[Any, Any]],
) -> Optional[Dict[str, Any]]:
    """Block an entire multi-segment operation if any segment overlaps."""
    for begin, end in segments:
        conflict = find_reservation_conflict(collection, begin, end)
        if conflict:
            return conflict
    return None


def require_reservation_allowed(
    collection: Any,
    reservation_begin: Any,
    reservation_end: Any,
) -> None:
    conflict = find_reservation_conflict(collection, reservation_begin, reservation_end)
    if conflict:
        raise ReservationBlackoutError(conflict)

