# -*- coding: utf-8 -*-
"""每天 07:10 把早上三张快照压成一条「全馆预约概况」，给设置页的折线图用。

seat_snapshots 里一天一个 tag 存 12 条区域文档、2700 多张座位的逐条区间，
是给座位推荐做差用的原料，前端每次打开都去扫一遍太重，也没必要把逐座位
的数据吐到浏览器。这里只保留回答「整体约了多少」需要的几个数：

    booked   pre / rush / post 三张各自已约的座位数（配上 seats_total 就是百分比）
    curve    post 那张按半小时切片，每个时段有多少座位被约——一天从早到晚的形状
    rooms    各区域座位数和 rush / post 已约数

一天一条，按 date 唯一。重跑同一天是覆盖，不会攒重复；旧的日子永远不动，
所以折线往前能一直翻。

跑的时机是 07:10（SEAT_OCCUPANCY_STATS_DELAY_MINUTES），跟在 07:05 那张 post
后面——它不再访问图书馆，只读库，晚几分钟跑也没关系。调度器启动时也会先跑
一次，把库里有快照但还没汇总的日子补齐，第一次部署不用手工回填。

用法
----
    docker compose exec scheduler python -m utils.occupancy_stats          # 汇总明天 + 补齐缺的
    docker compose exec scheduler python -m utils.occupancy_stats 2026-09-12
"""

import logging
import sys
from datetime import date as date_cls, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from pymongo import ASCENDING, MongoClient

from utils import config
from utils.seat_snapshot import (
    POST_TAG,
    PRE_TAG,
    RUSH_TAG,
    SNAPSHOT_COLLECTION,
    day_str_iso,
)

logger = logging.getLogger(__name__)

STATS_COLLECTION = "seat_occupancy_daily"

# 曲线从 07:00 到 22:00，每半小时一个点（最后一个点是 21:30-22:00 这一格）。
# 早上 7:30 / 8:30 / 9:30 是三个开始高峰，整点分辨率会把它们抹平。
CURVE_FROM = 7 * 60
CURVE_TO = 22 * 60
CURVE_STEP = 30

TAGS = (PRE_TAG, RUSH_TAG, POST_TAG)


def ensure_indexes(db) -> None:
    db[STATS_COLLECTION].create_index([("date", ASCENDING)], name="date", unique=True)


def _booked_count(seats: Dict[str, Sequence[Sequence[int]]]) -> int:
    return sum(1 for intervals in seats.values() if intervals)


def _slot_occupied(intervals: Sequence[Sequence[int]], begin: int, end: int) -> bool:
    """这张座位在 [begin, end) 这一格里有没有任何预约（哪怕只沾到一部分）。"""
    for interval in intervals or ():
        if len(interval) >= 2 and interval[0] < end and begin < interval[1]:
            return True
    return False


def _curve(docs: Sequence[Dict[str, Any]]) -> List[int]:
    slots = list(range(CURVE_FROM, CURVE_TO, CURVE_STEP))
    counts = [0] * len(slots)
    for doc in docs:
        for intervals in (doc.get("seats") or {}).values():
            if not intervals:
                continue
            for index, begin in enumerate(slots):
                if _slot_occupied(intervals, begin, begin + CURVE_STEP):
                    counts[index] += 1
    return counts


def summarize_day(db, day: date_cls) -> Optional[Dict[str, Any]]:
    """把某一天的快照压成一条概况；没有 post 那张就返回 None（没法算）。"""
    day_str = day_str_iso(day)
    by_tag: Dict[str, List[Dict[str, Any]]] = {}
    for doc in db[SNAPSHOT_COLLECTION].find({"date": day_str, "tag": {"$in": list(TAGS)}}):
        by_tag.setdefault(doc["tag"], []).append(doc)

    post_docs = sorted(by_tag.get(POST_TAG) or [], key=lambda d: str(d.get("room_id")))
    if not post_docs:
        return None

    booked: Dict[str, Optional[int]] = {}
    for tag in TAGS:
        docs = by_tag.get(tag)
        booked[tag] = (sum(_booked_count(d.get("seats") or {}) for d in docs)
                       if docs else None)

    rush_by_room = {d.get("room_id"): _booked_count(d.get("seats") or {})
                    for d in by_tag.get(RUSH_TAG) or []}
    rooms = []
    for doc in post_docs:
        seats = doc.get("seats") or {}
        rooms.append({
            "room_id": doc.get("room_id"),
            # room_name 本身就带楼层（「二层B区」「七层南侧」），不再拼 floor_name
            "name": doc.get("room_name") or "",
            "seats": len(seats),
            "booked": _booked_count(seats),
            "booked_rush": rush_by_room.get(doc.get("room_id")),
        })

    return {
        "date": day_str,
        "weekday": day.isoweekday(),
        "seats_total": sum(r["seats"] for r in rooms),
        "booked": booked,
        "curve": {"from": CURVE_FROM, "step": CURVE_STEP, "occupied": _curve(post_docs)},
        "rooms": rooms,
        "captured_at": post_docs[0].get("captured_at"),
        "generated_at": datetime.now(),
    }


def store_day(db, day: date_cls) -> Optional[Dict[str, Any]]:
    summary = summarize_day(db, day)
    if summary is None:
        logger.warning("%s 没有 post 快照，跳过预约概况汇总", day_str_iso(day))
        return None
    db[STATS_COLLECTION].replace_one({"date": summary["date"]}, summary, upsert=True)
    logger.info(
        "预约概况 %s：%s/%s 已约（rush %s）",
        summary["date"], summary["booked"].get(POST_TAG), summary["seats_total"],
        summary["booked"].get(RUSH_TAG),
    )
    return summary


def missing_dates(db) -> List[str]:
    """库里拍过 post 快照、但还没汇总过的日子。"""
    have = set(db[STATS_COLLECTION].distinct("date"))
    shot = set(db[SNAPSHOT_COLLECTION].distinct("date", {"tag": POST_TAG}))
    return sorted(shot - have)


def refresh(target: Optional[date_cls] = None, client: Optional[MongoClient] = None) -> List[str]:
    """汇总 target（默认明天，即 07:05 刚拍的那一天），顺手补齐历史上漏掉的。

    返回这次写了哪些日子。
    """
    target = target or (datetime.now().date() + timedelta(days=1))
    owns_client = client is None
    client = client or MongoClient(config.get_mongo_uri())
    try:
        db = client[config.DB_NAME]
        ensure_indexes(db)
        days = missing_dates(db)
        target_str = day_str_iso(target)
        if target_str not in days:
            days.append(target_str)
        written = []
        for day_str in days:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
            if store_day(db, day) is not None:
                written.append(day_str)
        return written
    finally:
        if owns_client:
            client.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
    args = list(argv if argv is not None else sys.argv[1:])
    target = datetime.strptime(args[0], "%Y-%m-%d").date() if args else None
    written = refresh(target)
    print("已汇总：" + (", ".join(written) if written else "（没有可汇总的日子）"))
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
