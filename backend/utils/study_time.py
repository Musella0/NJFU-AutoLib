# -*- coding: utf-8 -*-
"""真实在馆时长：用图书馆的操作流水把「预约了多久」换成「实际坐了多久」。

visit_logs 里每条到馆记录默认只有 planned_duration_minutes——预约时段的长度，
9:00 约到 22:00 就记 13 小时，中午出去吃饭、下午提前走都算在里面，是个模糊值。
ic-web 的 reserve/operate/rec 接口按 resvId 给出这条预约的全部操作：几点签到、
几点暂离、几点回来、几点结束，减一减就是真在座位上的时间。

这些流水是用户的行踪明细，只在用户明确勾选同意（user_config_info.study_time_consent）
之后才去拉，拉到的原始事件连同算出的时长一起存回 visit_logs 那条记录里，
只给本人看。关掉开关时用户自己选：整体删掉、原样保留、或者模糊保留
（只留按半小时取整的起止和总时长，中间的暂离细节删掉）。

kind 的位定义照图书馆网页「操作记录」里的操作类型一列对出来的
（见 library_system.OPERATE_KIND_NAMES）：
    1 预约成功   2 已生效   4 已签到   8 暂离   16 返回   32 已结束
在馆时间从 4「已签到」（闸机）起算，不是 2「已生效」——那只是系统到点把预约
置为生效，人还没进馆。8 当「离座开始」，16 当「回到座位」，32 当「结束」。
原始事件存着，以后位定义要是再对不上可以直接重算，不用再去打接口。

每条记录只写一次（actual_synced_at 一打上就不再碰），所以每晚定时跑和用户
白天手动点「立即同步」互不重复：只会把还没折算的记录补上，不会把时长累加。
没有「结束」事件的预约是个开区间——人可能还坐着——这一条先不动，等它
真正结束了再算；图书馆迟迟不写结束的（违约释放之类）超过一天就按计划结束时间收口。
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

CONSENT_FIELD = "study_time_consent"
CONSENT_AT_FIELD = "study_time_consent_at"

KIND_CHECKIN = 4
KIND_AWAY = 8
KIND_BACK = 16
KIND_END = 32

KIND_NAMES = {1: "预约成功", 2: "已生效", 4: "已签到", 8: "暂离", 16: "返回", 32: "已结束"}
CONSOLE_NAMES = {1: "系统", 8: "闸机", 16: "电脑端", 32: "现场预约台"}

# 用户选「删除」时从 visit_logs 里整体抹掉的字段——全部是从操作流水推出来的东西
DETAIL_FIELDS = (
    "events", "actual_minutes", "away_minutes", "actual_checkin_at",
    "actual_end_at", "end_source", "actual_synced_at", "blurred",
)
# 「模糊保留」只删中间细节，起止时间和总时长按这个粒度取整
BLUR_MINUTES = 30

# 预约结束后至少等这么久再拉：给图书馆那边写「结束」事件留点余地
SYNC_SETTLE_MINUTES = 10
# 拉失败 / 还没结束的记录最多往前找几天来补；再早的当作永久缺失，不再重试
SYNC_LOOKBACK_DAYS = 3
# 计划结束都过了这么久还没有「结束」事件，就不等了，按计划结束时间收口
SYNC_STALE_DAYS = 1


def summarize_events(
    events: Iterable[Dict[str, Any]],
    planned_end: Optional[datetime],
) -> Dict[str, Any]:
    """把一条预约的操作流水折成在馆时长。

    events 每项至少有 kind 和 create_time(datetime)，顺序无所谓。
    没有签到事件就算 0 分钟；没有结束事件就用 planned_end 兜底并标 end_source=planned。
    离座区间：8 开始，下一次 16 结束；结束时还在外面的，按结束时刻收口。
    """
    ordered = sorted(
        (e for e in events if e.get("create_time")),
        key=lambda e: e["create_time"],
    )
    checkin_at = next(
        (e["create_time"] for e in ordered if e.get("kind") == KIND_CHECKIN), None)
    if checkin_at is None:
        return {
            "actual_minutes": 0, "away_minutes": 0,
            "actual_checkin_at": None, "actual_end_at": None,
            "end_source": "no_checkin",
        }

    end_at = next(
        (e["create_time"] for e in ordered
         if e.get("kind") == KIND_END and e["create_time"] >= checkin_at),
        None,
    )
    end_source = "event"
    if end_at is None:
        end_at, end_source = planned_end, "planned"
    if end_at is None or end_at < checkin_at:
        # 既没结束事件也没计划结束时间，只能按签到那一刻算，时长记 0
        end_at, end_source = checkin_at, "unknown"

    away = timedelta()
    away_since: Optional[datetime] = None
    for e in ordered:
        t = e["create_time"]
        if t < checkin_at or t > end_at:
            continue
        kind = e.get("kind")
        if kind == KIND_AWAY and away_since is None:
            away_since = t
        elif kind == KIND_BACK and away_since is not None:
            away += t - away_since
            away_since = None
    if away_since is not None:
        away += end_at - away_since

    total = end_at - checkin_at
    away_minutes = int(away.total_seconds() // 60)
    actual_minutes = max(0, int(total.total_seconds() // 60) - away_minutes)
    return {
        "actual_minutes": actual_minutes,
        "away_minutes": away_minutes,
        "actual_checkin_at": checkin_at,
        "actual_end_at": end_at,
        "end_source": end_source,
    }


def is_closed(events: Iterable[Dict[str, Any]]) -> bool:
    """有「结束」事件就是闭区间，可以折算；否则人可能还在座位上。"""
    return any(e.get("kind") == KIND_END for e in events)


def pending_logs(db, now: datetime, pid: Optional[str] = None) -> List[Dict[str, Any]]:
    """还没同步过、预约已经结束、并且存了 resv_id 的到馆记录。

    老记录没有 resv_id（是 operate/rec 接口接进来之前写的），拉不到流水，跳过。
    """
    query: Dict[str, Any] = {
        "resv_id": {"$exists": True, "$ne": None},
        "actual_synced_at": {"$exists": False},
        "planned_end": {
            "$lte": now - timedelta(minutes=SYNC_SETTLE_MINUTES),
            "$gte": now - timedelta(days=SYNC_LOOKBACK_DAYS),
        },
    }
    if pid:
        query["pid"] = pid
    return list(db.visit_logs.find(
        query,
        {"_id": 0, "uuid": 1, "pid": 1, "resv_id": 1, "planned_end": 1, "seat_name": 1},
    ).sort("planned_end", 1))


def store_summary(db, uuid: str, records: List[Dict[str, Any]],
                  planned_end: Optional[datetime], now: datetime) -> Optional[Dict[str, Any]]:
    """把流水和折算结果写回 visit_logs；返回折算结果方便打日志。

    开区间（没有结束事件）不写、返回 None，留到下次；只有计划结束时间已经过去
    SYNC_STALE_DAYS 天还没等到结束事件，才按计划结束收口。
    """
    if not is_closed(records):
        stale = planned_end is not None and now - planned_end >= timedelta(days=SYNC_STALE_DAYS)
        if not stale:
            return None
    summary = summarize_events(records, planned_end)
    events = [
        {"kind": r.get("kind"), "console_kind": r.get("console_kind"), "at": r.get("create_time")}
        for r in records
    ]
    db.visit_logs.update_one(
        {"uuid": uuid},
        {"$set": {**summary, "events": events, "actual_synced_at": now},
         "$unset": {"blurred": ""}},
    )
    return summary


def clear_user_details(db, pid: str) -> int:
    """用户撤回同意并选「删除」：把已采集的流水和折算字段全部抹掉，只留预约时长。"""
    result = db.visit_logs.update_many(
        {"pid": pid, "actual_synced_at": {"$exists": True}},
        {"$unset": {field: "" for field in DETAIL_FIELDS}},
    )
    return result.modified_count


def _round_to(dt: datetime, minutes: int) -> datetime:
    """四舍五入到 minutes 的整数倍（09:14 → 09:00，09:16 → 09:30）。"""
    base = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    offset = (dt - base).total_seconds() / 60
    return base + timedelta(minutes=round(offset / minutes) * minutes)


def blur_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """「模糊保留」：只留签到到结束的跨度，按半小时取整；暂离细节不保留。"""
    checkin_at, end_at = summary.get("actual_checkin_at"), summary.get("actual_end_at")
    if not checkin_at or not end_at:
        return {"actual_minutes": 0, "actual_checkin_at": None, "actual_end_at": None}
    start = _round_to(checkin_at, BLUR_MINUTES)
    end = _round_to(end_at, BLUR_MINUTES)
    span = max(0, int((end - start).total_seconds() // 60))
    return {"actual_minutes": span, "actual_checkin_at": start, "actual_end_at": end}


def blur_user_details(db, pid: str) -> int:
    """用户撤回同意并选「模糊保留」：逐条把精确记录降成半小时粒度的起止 + 总时长。"""
    count = 0
    cursor = db.visit_logs.find(
        {"pid": pid, "actual_synced_at": {"$exists": True}, "blurred": {"$ne": True}},
        {"uuid": 1, "actual_checkin_at": 1, "actual_end_at": 1},
    )
    for log in cursor:
        blurred = blur_summary(log)
        db.visit_logs.update_one(
            {"uuid": log["uuid"]},
            {"$set": {**blurred, "blurred": True},
             "$unset": {"events": "", "away_minutes": ""}},
        )
        count += 1
    return count


def export_rows(db, pid: str) -> List[Dict[str, Any]]:
    """导出给用户自己留档的明细，一条到馆记录一行，事件流水压成一格文字。"""
    rows = []
    cursor = db.visit_logs.find(
        {"pid": pid, "actual_synced_at": {"$exists": True}},
        {"_id": 0},
    ).sort("planned_begin", 1)
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M") if isinstance(d, datetime) else ""
    for log in cursor:
        events = "; ".join(
            f"{fmt(e.get('at'))} {KIND_NAMES.get(e.get('kind'), e.get('kind'))}"
            + (f"({CONSOLE_NAMES[e['console_kind']]})" if e.get("console_kind") in CONSOLE_NAMES else "")
            for e in (log.get("events") or [])
        )
        rows.append({
            "日期": fmt(log.get("planned_begin"))[:10],
            "座位": log.get("seat_name", ""),
            "区域": log.get("location", ""),
            "预约开始": fmt(log.get("planned_begin")),
            "预约结束": fmt(log.get("planned_end")),
            "预约时长(分钟)": log.get("planned_duration_minutes", ""),
            "签到": fmt(log.get("actual_checkin_at")),
            "结束": fmt(log.get("actual_end_at")),
            "离座(分钟)": log.get("away_minutes", ""),
            "在馆(分钟)": log.get("actual_minutes", ""),
            "精度": "半小时" if log.get("blurred") else "精确",
            "操作流水": events,
        })
    return rows
