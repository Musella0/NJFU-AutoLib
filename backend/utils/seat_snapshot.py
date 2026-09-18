# -*- coding: utf-8 -*-
"""每天抢座结束后拍一张「全馆占用快照」，作为座位竞争度的唯一事实来源。

为什么需要它
------------
7:00 抢座失败时，日志里只有我们自己试过的那两三张座位的成败，看不到
「整个二层B区几点被抢光」「隔壁哪张座位其实一直没人要」。没有这张全局图，
任何座位推荐都只能靠猜。

ic-web 的 `GET ic-web/reserve?roomIds={roomId}&resvDates=D,D&sysKind=8`
一次就返回该区域**所有**座位当天的预约区间（字段 resvInfo，含 startTime /
endTime / resvStatus，不含预约人身份）。12 个区域跑一遍约 14 秒、2700 多个
座位，代价可以忽略。

什么时候拍：一个早上三张，夹住 7:00
----------------------------------
    06:45  tag='pre'   抢座前，板子应该还是空的
    07:01  tag='rush'  7:00 那一波刚结束——**这张才是真信号**
    07:05  tag='post'  再加上几分钟慢慢约的人

**竞争度只存在于差里。** 07:00:00 整的那一刻明天还是一张白板（实测 2026-09-09
10:50 查 09-11，2749 个座位 0 条预约），「谁赢了这一秒」的信息在那个瞬间根本
不存在，所以没法「只拍 7:00 那一张」。

也正因为板子在开闸前完全是空的，1400 多条预约全挤在 07:00~07:05 这五分钟里，
分辨率必须做到分钟级：拍在 7 点前后几小时都是同一个数，没有任何信息量。

    rush − pre  = 7:00 那一波真抢走的座位 → 我们抢不过的
    post − rush = 07:01 之后慢慢约走的   → 我们本来拿得到的

只拍 post 会把这两类混成一个数，竞争度算高。见 rush_delta()。

pre 那张还回答一个单独的问题：有没有人比 7:00 更早下手。它要是哪天不为空，
就说明我们结构性地晚了，那是另一个层面的问题。

pre 定在 6:45（`SEAT_SNAPSHOT_PRE_MINUTES`）而不是 6:59，是为了让它的网络活动
在 6:50 的预登录开始前就彻底结束。rush 那张（`SEAT_SNAPSHOT_RUSH_SECONDS`）
复用 pre 建好的会话，抢座窗口里一次登录都不发生——宁可某天缺一张，
也不能跟抢座抢网关。

不做什么
--------
只读。绝不写 user_config_info，也绝不碰任何用户配置或座位配置。
登录用一个固定的观测账号（SEAT_SNAPSHOT_PID），不用其他人的凭据。

用法
----
    docker compose exec scheduler python -m utils.seat_snapshot           # 拍明天
    docker compose exec scheduler python -m utils.seat_snapshot 2026-09-12 --tag pre
    # 看 7:00 那一波到底抢走了什么：
    docker compose exec scheduler python -m utils.seat_snapshot --rush
    docker compose exec scheduler python -m utils.seat_snapshot --rush --window 08:00-22:00
"""

import logging
import os
import sys
import time
from datetime import date as date_cls, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pymongo import ASCENDING, MongoClient

from utils import config
from utils.crypto import decrypt

logger = logging.getLogger(__name__)

# 观测账号：只用来发 GET，不参与抢座逻辑，也不会被这里改动任何配置。
# 不设默认值——真实学号不进仓库，也不能在没配账号时拿别人的号去查。
SNAPSHOT_PID = os.getenv("SEAT_SNAPSHOT_PID", "").strip()
SNAPSHOT_COLLECTION = "seat_snapshots"

# 同一天可以拍多张快照，用 tag 区分（默认按拍摄时刻的 HHMM）。
# 估算竞争度时只取固定 tag，避免把定时拍的和手工补拍的混在一起算。
DEFAULT_TAG_FORMAT = "%H%M"

# 每天定时拍的三张，把 7:00 那一秒夹在中间。差值才是竞争信号。
PRE_TAG = "pre"      # 06:45 抢座前
RUSH_TAG = "rush"    # 07:01 抢座刚结束
POST_TAG = "post"    # 07:05 尘埃落定

# 区域之间歇一下，别把网关当压测目标——反正 07:05 没人跟我们抢时间。
ROOM_INTERVAL_SECONDS = float(os.getenv("SEAT_SNAPSHOT_ROOM_INTERVAL", "0.3"))

# ic-web 偶尔会在单个区域上超时，重试两次再放弃，缺一个区域也要把其余的存下来。
ROOM_MAX_ATTEMPTS = 3

DAY_END_MINUTE = 1440


def _to_minutes(ts_ms: Any, day: date_cls) -> Optional[int]:
    """epoch 毫秒 → 相对 day 零点的分钟数；不属于这一天的返回 None。

    闭馆时间是 22:00，正常不会跨天；但 22:00-次日00:00 这种写法真出现的话，
    把次日零点算作当天的 1440，别丢掉这条区间。
    """
    try:
        moment = datetime.fromtimestamp(int(ts_ms) / 1000)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    delta_days = (moment.date() - day).days
    if delta_days == 0:
        return moment.hour * 60 + moment.minute
    if delta_days == 1 and moment.hour == 0 and moment.minute == 0:
        return DAY_END_MINUTE
    return None


def _hhmm_to_minutes(value: Any) -> Optional[int]:
    """"07:30" → 450。openStart/openEnd 是这种字符串，不是时间戳。"""
    if not isinstance(value, str) or ":" not in value:
        return None
    hour, _, minute = value.partition(":")
    try:
        result = int(hour) * 60 + int(minute)
    except ValueError:
        return None
    return result if 0 <= result <= DAY_END_MINUTE else None


def _open_minutes(dev: Dict[str, Any]) -> Optional[List[int]]:
    """区域的开放时段，取该区域第一个座位的 openStart/openEnd。"""
    start = _hhmm_to_minutes(dev.get("openStart"))
    end = _hhmm_to_minutes(dev.get("openEnd"))
    if start is None or end is None:
        return None
    return [start, end]


def _safe_seat_name(name: Any) -> Optional[str]:
    """座位名要当 Mongo 的字段名用，带点或 $ 开头的一律跳过（现实中没有）。"""
    if not isinstance(name, str) or not name:
        return None
    if "." in name or name.startswith("$"):
        return None
    return name


def seat_is_free(intervals: Sequence[Sequence[int]], begin: int, end: int) -> bool:
    """快照里这张座位在 [begin, end) 内是否完全空着。

    半开区间：09:30 结束的单子不挡 09:30 开始的单子，和图书馆的行为一致。
    """
    for interval in intervals or ():
        if len(interval) < 2:
            continue
        if interval[0] < end and begin < interval[1]:
            return False
    return True


def ensure_indexes(db) -> None:
    """(date, tag, room_id) 唯一，重跑同一天同一 tag 是覆盖而不是攒重复。"""
    col = db[SNAPSHOT_COLLECTION]
    col.create_index(
        [("date", ASCENDING), ("tag", ASCENDING), ("room_id", ASCENDING)],
        name="date_tag_room",
        unique=True,
    )
    col.create_index([("date", ASCENDING)], name="date")


def _login(pid: str, db):
    """用观测账号登录。凭据从 user_config_info 读，但一个字段都不回写。"""
    doc = db.user_config_info.find_one({"pid": pid}, {"_id": 0, "vpn_password": 1})
    if not doc or not doc.get("vpn_password"):
        raise RuntimeError(f"观测账号 {pid} 不存在或没有保存凭据，无法拍快照")

    # 延迟导入：library_system 会拉起 vpn/网络栈，单测里不需要它。
    from utils.library_system import LibrarySystem

    password = decrypt(doc["vpn_password"])
    return LibrarySystem(pid, password, vpn_password=password)


def _require_pid(pid: Optional[str]) -> str:
    """观测账号没配就直接报错，别悄悄回退到某个写死的学号。"""
    resolved = (pid or SNAPSHOT_PID or "").strip()
    if not resolved:
        raise RuntimeError("未配置 SEAT_SNAPSHOT_PID，跳过座位快照")
    return resolved


def login(pid: Optional[str] = None, client: Optional[MongoClient] = None):
    """单独建一个观测会话，给「同一个早上拍好几张」的场景复用。

    抢座窗口里那张（07:01）必须复用 06:45 建好的会话：webvpn + CAS 要 4~8 秒，
    在 7:00 前后再登一次是往最要命的时刻上加网关负载。
    """
    owns_client = client is None
    client = client or MongoClient(config.get_mongo_uri())
    try:
        return _login(_require_pid(pid), client[config.DB_NAME])
    finally:
        if owns_client:
            client.close()


def _areas(api) -> List[Dict[str, str]]:
    """seatMenu → 所有可预约区域（楼层 → 区域两级）。"""
    areas: List[Dict[str, str]] = []
    for floor in api("ic-web/seatMenu").json().get("data", []) or []:
        for child in floor.get("children", []) or []:
            areas.append({
                "room_id": str(child.get("id")),
                "room_name": child.get("name") or "",
                "floor_name": floor.get("name") or "",
            })
    return areas


def _fetch_room(api, room_id: str, day_str: str) -> List[Dict[str, Any]]:
    """拉一个区域的座位列表，失败重试；连续失败就抛出去，由上层记账跳过。"""
    last_error: Optional[Exception] = None
    for attempt in range(1, ROOM_MAX_ATTEMPTS + 1):
        try:
            body = api("ic-web/reserve", {
                "roomIds": room_id,
                "resvDates": f"{day_str},{day_str}",
                "sysKind": 8,
            }).json()
            if body.get("code") not in (0, None):
                raise RuntimeError(f"接口返回 code={body.get('code')} {body.get('message')}")
            return body.get("data") or []
        except Exception as exc:  # noqa: BLE001 - 网络/解析异常都按同样方式重试
            last_error = exc
            logger.warning("区域 %s 第 %d 次拉取失败: %s", room_id, attempt, exc)
            if attempt < ROOM_MAX_ATTEMPTS:
                time.sleep(1.0 * attempt)
    raise RuntimeError(f"区域 {room_id} 拉取失败: {last_error}")


def _build_room_doc(
    area: Dict[str, str],
    devices: Iterable[Dict[str, Any]],
    day: date_cls,
    day_str: str,
    tag: str,
    captured_at: datetime,
    pid: str,
) -> Dict[str, Any]:
    """把一个区域的原始设备列表压成一条快照文档。"""
    seats: Dict[str, List[List[int]]] = {}
    unavailable: List[str] = []
    status_hist: Dict[str, int] = {}
    open_window: Optional[List[int]] = None
    intervals_total = 0

    for dev in devices:
        name = _safe_seat_name(dev.get("devName"))
        if name is None:
            continue
        if open_window is None:
            open_window = _open_minutes(dev)

        # devStatus 非 0 / openState 非 1 表示座位停用或维护，不该推荐给任何人。
        if dev.get("devStatus") not in (0, None) or dev.get("openState") not in (1, None):
            unavailable.append(name)

        booked: List[List[int]] = []
        for resv in dev.get("resvInfo") or []:
            begin = _to_minutes(resv.get("startTime"), day)
            end = _to_minutes(resv.get("endTime"), day)
            if begin is None or end is None or end <= begin:
                continue
            booked.append([begin, end])
            status = str(resv.get("resvStatus"))
            status_hist[status] = status_hist.get(status, 0) + 1
        booked.sort()
        seats[name] = booked
        intervals_total += len(booked)

    return {
        "date": day_str_iso(day),
        "weekday": day.isoweekday(),
        "tag": tag,
        "captured_at": captured_at,
        "room_id": area["room_id"],
        "room_name": area["room_name"],
        "floor_name": area["floor_name"],
        "open": open_window,
        "seats": seats,
        "unavailable": unavailable,
        "stats": {
            "seats": len(seats),
            "booked_seats": sum(1 for v in seats.values() if v),
            "intervals": intervals_total,
            "status_hist": status_hist,
        },
        "source_pid": pid,
        "source_query_date": day_str,
    }


def day_str_iso(day: date_cls) -> str:
    """统一用 YYYY-MM-DD 存库；查询接口要的 YYYYMMDD 另算。"""
    return day.strftime("%Y-%m-%d")


def capture(
    target_date: Optional[date_cls] = None,
    tag: Optional[str] = None,
    pid: Optional[str] = None,
    client: Optional[MongoClient] = None,
    library: Optional[Any] = None,
) -> Dict[str, Any]:
    """拍一张快照并写库，返回本次的汇总信息。

    Args:
        target_date: 观测哪一天的占用，默认明天（07:00 刚抢完的那一天）。
        tag: 同一天内区分多次拍摄，默认取当前 HHMM。
        pid: 观测账号，默认 SEAT_SNAPSHOT_PID。
        client: 复用外部的 MongoClient；不传则自己建自己关。
        library: 复用已有的观测会话（见 login()）；不传则现场登录。
                 会话失效时自动退回现场登录，不会让这一张白丢。
    """
    day = target_date or (datetime.now().date() + timedelta(days=1))
    captured_at = datetime.now()
    tag = tag or captured_at.strftime(DEFAULT_TAG_FORMAT)
    pid = _require_pid(pid)
    day_str = day.strftime("%Y%m%d")

    owns_client = client is None
    client = client or MongoClient(config.get_mongo_uri())
    started = time.time()
    try:
        db = client[config.DB_NAME]
        ensure_indexes(db)

        if library is not None and not library.verify_session():
            logger.warning("传入的观测会话已失效，改为现场登录")
            library = None
        if library is None:
            library = _login(pid, db)

        def api(path: str, params: Optional[Dict[str, Any]] = None):
            url = f"{library.base_url}{path.lstrip('/')}{library.vpn_suffix}"
            return library.session.get(url, params=params, timeout=(10, 90))

        areas = _areas(api)
        if not areas:
            raise RuntimeError("seatMenu 没有返回任何区域，可能是会话失效")

        rooms_written = 0
        seats_total = 0
        booked_total = 0
        failed_rooms: List[str] = []

        for index, area in enumerate(areas):
            try:
                devices = _fetch_room(api, area["room_id"], day_str)
            except Exception as exc:  # noqa: BLE001 - 单个区域失败不该毁掉整张快照
                logger.error("区域 %s(%s) 跳过: %s", area["room_name"], area["room_id"], exc)
                failed_rooms.append(area["room_id"])
                continue

            doc = _build_room_doc(area, devices, day, day_str, tag, captured_at, pid)
            db[SNAPSHOT_COLLECTION].replace_one(
                {"date": doc["date"], "tag": tag, "room_id": doc["room_id"]},
                doc,
                upsert=True,
            )
            rooms_written += 1
            seats_total += doc["stats"]["seats"]
            booked_total += doc["stats"]["booked_seats"]
            logger.info(
                "快照 %s[%s] %s/%s 座位 %d 已约 %d",
                doc["date"], tag, doc["floor_name"], doc["room_name"],
                doc["stats"]["seats"], doc["stats"]["booked_seats"],
            )
            if index + 1 < len(areas) and ROOM_INTERVAL_SECONDS > 0:
                time.sleep(ROOM_INTERVAL_SECONDS)

        summary = {
            "date": day_str_iso(day),
            "tag": tag,
            "rooms": rooms_written,
            "rooms_failed": failed_rooms,
            "seats": seats_total,
            "booked_seats": booked_total,
            "elapsed": round(time.time() - started, 1),
        }
        logger.info(
            "占用快照完成 %s[%s]：%d 个区域 / %d 座位 / %d 已被占，耗时 %.1fs%s",
            summary["date"], tag, rooms_written, seats_total, booked_total,
            summary["elapsed"],
            f"，失败区域 {failed_rooms}" if failed_rooms else "",
        )
        return summary
    finally:
        if owns_client:
            client.close()


def load_snapshots(
    db,
    days: int = 30,
    tag: Optional[str] = None,
    until: Optional[date_cls] = None,
) -> List[Dict[str, Any]]:
    """取最近 days 天的快照，给竞争度估算用。

    注意 date 字段是**被观测的那一天**，不是拍摄那一天——07:05 拍的是明天的
    占用，所以库里的 date 总是比拍摄时间晚一天。默认上界因此取明天，
    按「今天」去截会把刚拍的那张整个漏掉。

    同一天同一区域若有多个 tag，指定 tag 只取那一个；不指定则全取，
    由调用方自己决定怎么合并（见 seat_allocator）。
    """
    until = until or (datetime.now().date() + timedelta(days=1))
    since = until - timedelta(days=days)
    query: Dict[str, Any] = {
        "date": {"$gte": day_str_iso(since), "$lte": day_str_iso(until)},
    }
    if tag:
        query["tag"] = tag
    return list(db[SNAPSHOT_COLLECTION].find(query).sort("date", ASCENDING))


def rush_delta(
    db,
    day: date_cls,
    window: Optional[Tuple[int, int]] = None,
    pre_tag: str = PRE_TAG,
    post_tag: str = POST_TAG,
) -> Dict[str, Any]:
    """把 7:00 前后两张快照做差，分出「抢座前就没了」和「7:00 那一波被抢走」。

    这是整套采集真正想要的那个数字。单看 post 分不清「07:00:00.1 被秒杀」和
    「07:03 有人慢慢约走」，两者对我们的意义完全不同：前者是抢不过，
    后者其实是我们本来能拿到的。

    Args:
        window: 只关心某个时段 (begin_min, end_min) 的可用性；不给则按
                「这张座位有没有任何预约」统计。
    """
    day_str = day_str_iso(day)
    pre_docs = {d["room_id"]: d for d in
                db[SNAPSHOT_COLLECTION].find({"date": day_str, "tag": pre_tag})}
    post_docs = {d["room_id"]: d for d in
                 db[SNAPSHOT_COLLECTION].find({"date": day_str, "tag": post_tag})}

    def occupied(intervals) -> bool:
        if window is None:
            return bool(intervals)
        return not seat_is_free(intervals, window[0], window[1])

    rooms: List[Dict[str, Any]] = []
    totals = {"seats": 0, "gone_before": 0, "taken_in_rush": 0, "free_after": 0}
    for room_id, post in sorted(post_docs.items()):
        pre = pre_docs.get(room_id)
        if not pre:
            continue
        pre_seats = pre.get("seats") or {}
        post_seats = post.get("seats") or {}
        gone_before = taken_in_rush = free_after = 0
        rush_seats: List[str] = []
        for seat, post_intervals in post_seats.items():
            if seat not in pre_seats:
                continue
            was = occupied(pre_seats[seat])
            now = occupied(post_intervals)
            if was:
                gone_before += 1
            elif now:
                taken_in_rush += 1
                rush_seats.append(seat)
            else:
                free_after += 1
        rooms.append({
            "room_id": room_id,
            "room_name": f"{post.get('floor_name', '')}{post.get('room_name', '')}",
            "seats": gone_before + taken_in_rush + free_after,
            "gone_before": gone_before,
            "taken_in_rush": taken_in_rush,
            "free_after": free_after,
            "rush_seats": sorted(rush_seats),
        })
        totals["seats"] += rooms[-1]["seats"]
        totals["gone_before"] += gone_before
        totals["taken_in_rush"] += taken_in_rush
        totals["free_after"] += free_after

    return {
        "date": day_str,
        "window": list(window) if window else None,
        "pre_tag": pre_tag,
        "post_tag": post_tag,
        "pre_captured_at": next((d.get("captured_at") for d in pre_docs.values()), None),
        "post_captured_at": next((d.get("captured_at") for d in post_docs.values()), None),
        "rooms": rooms,
        "totals": totals,
    }


def format_rush_delta(delta: Dict[str, Any]) -> str:
    totals = delta["totals"]
    if not delta["rooms"]:
        return (f"{delta['date']} 没有可对比的前后两张快照"
                f"（需要 tag={delta['pre_tag']} 和 tag={delta['post_tag']} 各一张）")
    lines = [
        f"{delta['date']} 抢座前后对比"
        + (f"  时段 {delta['window'][0] // 60:02d}:{delta['window'][0] % 60:02d}-"
           f"{delta['window'][1] // 60:02d}:{delta['window'][1] % 60:02d}"
           if delta["window"] else "  （任意预约都算占用）"),
        f"  pre  拍于 {delta['pre_captured_at']}",
        f"  post 拍于 {delta['post_captured_at']}",
        "",
        f"{'区域':<14} {'座位':>5} {'抢座前就没了':>12} {'7:00那波抢走':>12} {'之后仍空':>8}",
    ]
    for room in delta["rooms"]:
        lines.append(
            f"{room['room_name']:<14} {room['seats']:>5} "
            f"{room['gone_before']:>12} {room['taken_in_rush']:>12} {room['free_after']:>8}"
        )
    lines.append(
        f"{'合计':<14} {totals['seats']:>5} "
        f"{totals['gone_before']:>12} {totals['taken_in_rush']:>12} {totals['free_after']:>8}"
    )
    return "\n".join(lines)


def parse_window(text: str) -> Tuple[int, int]:
    """"08:00-22:00" → (480, 1320)。"""
    begin, _, end = text.partition("-")
    start = _hhmm_to_minutes(begin.strip())
    stop = _hhmm_to_minutes(end.strip())
    if start is None or stop is None or stop <= start:
        raise ValueError(f"时段写法不对: {text}（应形如 08:00-22:00）")
    return start, stop


def _parse_args(argv: Sequence[str]) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {"day": None, "tag": None, "pid": None,
                              "rush": False, "window": None}
    rest = list(argv)
    while rest:
        item = rest.pop(0)
        if item == "--tag" and rest:
            parsed["tag"] = rest.pop(0)
        elif item == "--pid" and rest:
            parsed["pid"] = rest.pop(0)
        elif item == "--rush":
            parsed["rush"] = True
        elif item == "--window" and rest:
            parsed["window"] = parse_window(rest.pop(0))
        elif item.startswith("-"):
            raise SystemExit(f"未知参数: {item}\n{__doc__}")
        else:
            parsed["day"] = datetime.strptime(item, "%Y-%m-%d").date()
    return parsed


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
    )
    args = _parse_args(list(argv if argv is not None else sys.argv[1:]))

    if args["rush"]:
        client = MongoClient(config.get_mongo_uri())
        try:
            day = args["day"] or (datetime.now().date() + timedelta(days=1))
            db = client[config.DB_NAME]
            # 两段分别看：7:00 那一波抢走的（抢不过），和之后慢慢约走的（本来拿得到）
            race = rush_delta(db, day, window=args["window"],
                              pre_tag=PRE_TAG, post_tag=RUSH_TAG)
            late = rush_delta(db, day, window=args["window"],
                              pre_tag=RUSH_TAG, post_tag=POST_TAG)
        finally:
            client.close()
        print("【7:00 那一波】pre → rush：这些是真抢不过的")
        print(format_rush_delta(race))
        print()
        print("【7:01 之后】rush → post：这些是慢慢约走的，我们本来拿得到")
        print(format_rush_delta(late))
        return 0 if (race["rooms"] or late["rooms"]) else 1

    try:
        summary = capture(target_date=args["day"], tag=args["tag"], pid=args["pid"])
    except Exception as exc:  # noqa: BLE001 - CLI 只要一句人话
        logger.error("拍摄占用快照失败: %s", exc, exc_info=True)
        return 1
    print(
        f"{summary['date']}[{summary['tag']}] "
        f"{summary['rooms']} 区域 / {summary['seats']} 座位 / "
        f"{summary['booked_seats']} 已被占，耗时 {summary['elapsed']}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
