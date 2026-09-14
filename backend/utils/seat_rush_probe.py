# -*- coding: utf-8 -*-
"""7:00 开闸后按秒采样全馆占用，看座位到底是几秒钟内被抢光的。

seat_snapshot 每天拍的三张（06:45 / 07:01 / 07:05）只能回答「7:00 那一波抢走了
多少」，回答不了「那一波持续了几秒」「第 1 秒和第 10 秒差多少」。这个脚本就干
这一件事：在 07:00:01、07:00:05、07:00:10、07:01、07:05、07:15 各拍一张，
之后每半小时一张一直拍到闭馆（22:00，周五 20:00），把明天那块板子一整天
的填充过程记下来——31 小时窗口意味着明天下午/晚上的时段是今天白天陆续
放开的。观测日撞上已确认的闭馆通知就直接退出，不白拍。

两台机器分工
------------
开闸那一刻钟（-10s ~ +15m）跑在分机部署的 edge（境外那台，只有 Caddy，7:00
闲着）：每张 12 个区域**并发**拉，秒级分辨率才有意义；它和抢座的 backend 不是
同一台机器、不是同一条出口，采样再密也不会跟 7:00 的下单抢带宽。
07:30 起每半小时一张由 backend（国内）的 scheduler 用 fill_sample() 补——那时
不着急了，和每日三张一样一个区域一个区域慢慢拉，境外那边白天不再发请求。
两边写同一张表、同一套 tag，前端看到的是一条连续的曲线。

同一个观测账号在两边各登一个会话不会互踢：backend 上 06:45 快照的会话一直
撑到 07:01 复用成功，中间 06:50 预登录、07:00 下单都是同账号另开的会话
（scheduler 日志里从没出现过「观测会话已失效」）。

只读。只发 GET，绝不写 user_config_info，绝不碰任何用户配置或座位配置。
凭据只用观测账号 SEAT_SNAPSHOT_PID 的。

结果
----
每个采样点按 tag=`probe{±秒}s` 写进 seat_snapshots（和每日三张同一张表、同一
文档结构，date/tag/room_id 唯一，重跑覆盖）。跑完在 stdout 打一张表，
并可 --out 落一份 JSON 汇总。

每拍一张就顺手把当天的 seat_occupancy_daily 重算一遍（occupancy_stats），
设置页「图书馆预约情况」里的填充曲线就是这么一点点长出来的。

库在隧道对面（backend 的 mongo 绑在 WireGuard 地址上，见 docker-compose.backend.yml）。
隧道抖一下写不进去的区域先攒着，下一张拍完再补写，不丢。

部署：docker-compose.edge.yml 里的 probe 服务，`--daily --every 0` 常驻，每天
06:55 醒来登录、07:00 开拍、07:15 收工、睡到第二天。手动跑一次::

    docker compose -p autolib -f docker-compose.edge.yml run --rm probe \\
      python -m utils.seat_rush_probe --open 07:00:00 --every 0

    python -m utils.seat_rush_probe --open 07:00:00 --offsets=-10,1,5,10,60,300,900 --every 1800
    python -m utils.seat_rush_probe --until 20:00      # 手动指定收工时刻
    python -m utils.seat_rush_probe --open now+90s --offsets 1,5 --dry-run   # 自测
"""

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_cls, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from pymongo import MongoClient
from requests.adapters import HTTPAdapter

from utils import config
from utils.occupancy_stats import store_day
from utils.reservation_blackout import find_reservation_conflict
from utils.seat_snapshot import (
    SNAPSHOT_COLLECTION,
    _areas,
    _build_room_doc,
    ROOM_INTERVAL_SECONDS,
    capture,
    login,
    _fetch_room,
    _login,
    _require_pid,
    day_str_iso,
    ensure_indexes,
)

logger = logging.getLogger(__name__)

# 开闸后的密集采样点（秒）。seat_snapshot 的经验是 1400 多条预约全挤在
# 07:00~07:05，所以前一分钟按秒、前一刻钟按分钟；之后交给 --every 每半小时一张。
# -10s 那张既是开闸前的白板基线，也把 12 条 TLS 连接提前握好。
DEFAULT_OFFSETS = "-10,1,5,10,60,300,900"
DEFAULT_EVERY_SECONDS = 1800

# 收工时刻：图书馆 22:00 闭馆，周五 20:00。
CLOSE_HHMM = "22:00"
FRIDAY_CLOSE_HHMM = "20:00"

# 长会话：超过这个偏移的采样点开拍前先校验会话，失效就重登再拍。
VERIFY_AFTER_OFFSET_SECONDS = 600

# 每天常驻模式：提前多久醒来准备（登录 + 拉区域列表）。
DAILY_OPEN_HHMM = "07:00:00"

# 隧道对面的库：服务器选择别等默认的 30s，隧道抖动时快速失败、攒着下次补写。
MONGO_TIMEOUT_MS = 8000

# 12 路并发只在开闸后那一刻钟用——那时秒级分辨率才有意义。之后每半小时一张
# 没人跟我们抢时间，就按 seat_snapshot 那样一个区域一个区域慢慢拉，别让观测
# 账号一整天每半小时都对网关打一梭子 12 发并发，那是最像机器的特征。
CONCURRENT_UNTIL_OFFSET_SECONDS = 900

# requests 默认每主机只留 10 条连接，12 个区域并发会有两路被丢掉重新握手。
POOL_SIZE = 32

# 提前多久登录。webvpn + CAS 从境外要 4~8 秒，留够余量，也别太早让会话闲到过期。
DEFAULT_LOGIN_LEAD_SECONDS = 300
# 开闸前再校验一次会话，失效就趁还没到点重登，不把登录留到 7:00 之后。
VERIFY_BEFORE_OPEN_SECONDS = 45

TAG_PREFIX = "probe"


def _parse_open(text: str, today: date_cls) -> datetime:
    """`HH:MM[:SS]`（今天这个时刻；已过则明天）或 `now+90s`（自测用）。"""
    text = text.strip()
    if text.startswith("now+") and text.endswith("s"):
        return datetime.now() + timedelta(seconds=float(text[4:-1]))
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.append(0)
    when = datetime.combine(today, datetime.min.time()).replace(
        hour=parts[0], minute=parts[1], second=parts[2])
    if when <= datetime.now():
        when += timedelta(days=1)
    return when


def _sleep_until(when: datetime) -> None:
    """分段睡到指定时刻，最后一段用短睡把误差压到毫秒级。"""
    while True:
        remaining = (when - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.5) if remaining > 1 else remaining)


def _sample(
    library: Any,
    areas: List[Dict[str, str]],
    day: date_cls,
    offset: int,
    open_at: datetime,
    collection: Optional[Any],
    pid: str,
    results: List[Dict[str, Any]],
    lock: threading.Lock,
    concurrent: bool = True,
    pending: Optional[List[Dict[str, Any]]] = None,
    db: Optional[Any] = None,
) -> None:
    """拍一张：所有区域并发（或串行）拉，逐区域落库，汇总进 results。

    pending 是还没写进库的文档（隧道断时攒下的），每张拍完先补它们再写自己的。
    """
    tag = f"{TAG_PREFIX}{offset:+d}s"
    day_str = day.strftime("%Y%m%d")
    captured_at = datetime.now()
    started = time.time()

    def api(path: str, params: Optional[Dict[str, Any]] = None):
        url = f"{library.base_url}{path.lstrip('/')}{library.vpn_suffix}"
        return library.session.get(url, params=params, timeout=(10, 40))

    def one(area: Dict[str, str]) -> Dict[str, Any]:
        t0 = time.time()
        try:
            devices = _fetch_room(api, area["room_id"], day_str)
        except Exception as exc:  # noqa: BLE001 - 单个区域失败不该毁掉整张
            logger.error("[%s] 区域 %s 跳过: %s", tag, area["room_name"], exc)
            return {"area": area, "doc": None, "elapsed": time.time() - t0}
        doc = _build_room_doc(area, devices, day, day_str, tag, captured_at, pid)
        # 单个区域从境外拉要 0.2~5s 且完全随机（ic-web 那头的事，跟连接冷热无关），
        # 一张「快照」其实摊在好几秒里。每个区域记自己真正拿到数据的时刻，
        # 看曲线时以它为准，captured_at 只是这一张的开拍时刻。
        doc["fetched_at"] = datetime.now()
        return {"area": area, "doc": doc, "elapsed": time.time() - t0}

    if concurrent:
        with ThreadPoolExecutor(max_workers=len(areas)) as pool:
            rooms = list(pool.map(one, areas))
    else:
        rooms = []
        for index, area in enumerate(areas):
            rooms.append(one(area))
            if index + 1 < len(areas) and ROOM_INTERVAL_SECONDS > 0:
                time.sleep(ROOM_INTERVAL_SECONDS)

    booked = 0
    seats = 0
    failed: List[str] = []
    per_room: Dict[str, int] = {}
    to_write: List[Dict[str, Any]] = []
    for item in rooms:
        doc = item["doc"]
        if doc is None:
            failed.append(item["area"]["room_name"])
            continue
        to_write.append(doc)
        booked += doc["stats"]["booked_seats"]
        seats += doc["stats"]["seats"]
        per_room[f"{doc['floor_name']}/{doc['room_name']}"] = doc["stats"]["booked_seats"]

    summary = {
        "offset": offset,
        "tag": tag,
        "planned_at": (open_at + timedelta(seconds=offset)).strftime("%H:%M:%S"),
        "started_at": captured_at.strftime("%H:%M:%S.%f")[:-3],
        "late_ms": int((captured_at - open_at - timedelta(seconds=offset)).total_seconds() * 1000),
        "elapsed": round(time.time() - started, 2),
        "slowest_room": round(max(i["elapsed"] for i in rooms), 2) if rooms else None,
        "room_fetched_at": {
            f"{i['doc']['floor_name']}/{i['doc']['room_name']}": i["doc"]["fetched_at"].strftime("%H:%M:%S.%f")[:-3]
            for i in rooms if i["doc"] is not None
        },
        "seats": seats,
        "booked_seats": booked,
        "rooms_failed": failed,
        "per_room": per_room,
    }
    with lock:
        results.append(summary)
    logger.info(
        "采样 %+ds：%s 开拍（晚 %dms），%.2fs 拍完，%d/%d 座位已约%s",
        offset, summary["started_at"], summary["late_ms"], summary["elapsed"],
        booked, seats, f"，失败区域 {failed}" if failed else "",
    )

    if collection is None:
        return
    # 落库放在采样之后、拿着锁做：写库走隧道，慢或断都不能影响下一张的开拍时刻，
    # 也不能让两张并发地互相插队把 pending 写乱。
    with lock:
        if pending is not None:
            to_write = pending + to_write
            pending.clear()
        written = 0
        for doc in to_write:
            try:
                collection.replace_one(
                    {"date": doc["date"], "tag": doc["tag"], "room_id": doc["room_id"]},
                    doc, upsert=True,
                )
                written += 1
            except Exception as exc:  # noqa: BLE001 - 隧道抖动，攒着下张补
                if pending is not None:
                    pending.extend(to_write[written:])
                logger.warning("[%s] 写库失败，%d 条攒到下一张再写: %s",
                               tag, len(to_write) - written, exc)
                return
        if db is not None and offset >= CONCURRENT_UNTIL_OFFSET_SECONDS:
            # 07:05 那张 post 拍完之后才算得出概况，太早调只会拿到 None。
            try:
                store_day(db, day)
            except Exception as exc:  # noqa: BLE001 - 概况算不出来不影响采样
                logger.warning("[%s] 重算预约概况失败: %s", tag, exc)


def expand_offsets(offsets: Sequence[int], every: int, open_at: datetime, until: datetime) -> List[int]:
    """显式采样点之后，按 every 的整倍数补到 until 为止。"""
    result = set(int(o) for o in offsets)
    if every > 0:
        k = max(result) // every + 1 if result else 0
        while open_at + timedelta(seconds=k * every) <= until:
            result.add(k * every)
            k += 1
    return sorted(result)


def default_until(open_at: datetime) -> datetime:
    hhmm = FRIDAY_CLOSE_HHMM if open_at.weekday() == 4 else CLOSE_HHMM
    hour, minute = (int(x) for x in hhmm.split(":"))
    return open_at.replace(hour=hour, minute=minute, second=0, microsecond=0)


def is_closed_day(db, target_date: date_cls) -> Optional[Dict[str, Any]]:
    """观测日撞上已确认的闭馆通知（school_notice_reviews）就不拍。"""
    day_begin = datetime.combine(target_date, datetime.min.time())
    return find_reservation_conflict(
        db.school_notice_reviews, day_begin + timedelta(hours=8), day_begin + timedelta(hours=22))


# backend 上每半小时那张复用的观测会话（同 scheduler_runner 里 06:45 那张的做法）。
_fill_session = None


def fill_sample(open_at: datetime, now: Optional[datetime] = None,
                client: Optional[MongoClient] = None) -> Optional[Dict[str, Any]]:
    """国内那台每半小时补的一张：串行拉，tag 与 edge 的密集采样同一套。

    调度器整点/半点调一次；不在 07:30 ~ 闭馆之间、或观测日闭馆就返回 None。
    偏移按半小时取整，晚个把分钟跑也归到同一个采样点。
    """
    global _fill_session
    now = now or datetime.now()
    offset = int(round((now - open_at).total_seconds() / DEFAULT_EVERY_SECONDS)) * DEFAULT_EVERY_SECONDS
    if offset < DEFAULT_EVERY_SECONDS or now > default_until(open_at) + timedelta(minutes=5):
        return None
    target_date = open_at.date() + timedelta(days=1)

    owns_client = client is None
    client = client or MongoClient(config.get_mongo_uri())
    try:
        db = client[config.DB_NAME]
        conflict = is_closed_day(db, target_date)
        if conflict:
            logger.info("观测日 %s 闭馆（%s），不采样", day_str_iso(target_date), conflict.get("title"))
            return None
        if _fill_session is None or not _fill_session.verify_session():
            _fill_session = login(client=client)
        summary = capture(target_date=target_date, tag=f"{TAG_PREFIX}{offset:+d}s",
                          client=client, library=_fill_session)
        store_day(db, target_date)
        return summary
    except Exception:
        _fill_session = None
        raise
    finally:
        if owns_client:
            client.close()


def _fresh_session(pid: str, db):
    library = _login(pid, db)
    if not library.ensure_login():
        raise RuntimeError("观测账号登录失败")
    library.session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=POOL_SIZE))
    return library


def run(
    open_at: datetime,
    offsets: Sequence[int],
    target_date: date_cls,
    pid: Optional[str] = None,
    login_lead: int = DEFAULT_LOGIN_LEAD_SECONDS,
    dry_run: bool = False,
    out_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    pid = _require_pid(pid)
    offsets = sorted(set(int(o) for o in offsets))
    logger.info(
        "采样计划：开闸 %s，观测 %s，采样点 %s，%s",
        open_at.strftime("%Y-%m-%d %H:%M:%S"), day_str_iso(target_date),
        ",".join(f"{o:+d}s" for o in offsets),
        "不落库（dry-run）" if dry_run else "写入 seat_snapshots",
    )

    client = MongoClient(config.get_mongo_uri(), serverSelectionTimeoutMS=MONGO_TIMEOUT_MS)
    try:
        db = client[config.DB_NAME]
        collection = None
        pending: List[Dict[str, Any]] = []
        if not dry_run:
            ensure_indexes(db)
            collection = db[SNAPSHOT_COLLECTION]

        conflict = is_closed_day(db, target_date)
        if conflict:
            logger.info("观测日 %s 闭馆（%s），不采样", day_str_iso(target_date), conflict.get("title"))
            return []

        _sleep_until(open_at - timedelta(seconds=login_lead))
        library = _fresh_session(pid, db)
        logger.info("观测账号已登录，会话建好")

        def api(path: str, params: Optional[Dict[str, Any]] = None):
            url = f"{library.base_url}{path.lstrip('/')}{library.vpn_suffix}"
            return library.session.get(url, params=params, timeout=(10, 40))

        areas = _areas(api)
        if not areas:
            raise RuntimeError("seatMenu 没有返回任何区域")
        logger.info("%d 个区域待采样", len(areas))

        verify_at = open_at - timedelta(seconds=VERIFY_BEFORE_OPEN_SECONDS)
        if datetime.now() < verify_at:
            _sleep_until(verify_at)
            if not library.verify_session():
                logger.warning("会话在开闸前失效，重新登录")
                library = _fresh_session(pid, db)

        results: List[Dict[str, Any]] = []
        lock = threading.Lock()
        threads: List[threading.Thread] = []
        for offset in offsets:
            if offset >= VERIFY_AFTER_OFFSET_SECONDS:
                # 会话要撑一整天，晚上那几张开拍前先探一下，失效就趁没到点重登。
                _sleep_until(open_at + timedelta(seconds=offset - 20))
                if not library.verify_session():
                    logger.warning("会话失效，重新登录后再拍 %+ds", offset)
                    library = _fresh_session(pid, db)
            _sleep_until(open_at + timedelta(seconds=offset))
            # 每个采样点自己一个线程：上一张还没拍完也不能拖住下一张的开拍时刻。
            t = threading.Thread(
                target=_sample,
                args=(library, areas, target_date, offset, open_at, collection, pid, results, lock,
                      offset <= CONCURRENT_UNTIL_OFFSET_SECONDS, pending, db if not dry_run else None),
                name=f"probe+{offset}s", daemon=True,
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        if pending:
            logger.error("收工时还有 %d 条区域文档没写进库，丢弃", len(pending))
    finally:
        client.close()

    results.sort(key=lambda r: r["offset"])
    print(format_results(results, open_at, target_date))
    if out_path:
        payload = {
            "date": day_str_iso(target_date),
            "open_at": open_at.strftime("%Y-%m-%d %H:%M:%S"),
            "samples": results,
        }
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    return results


def format_results(results: Sequence[Dict[str, Any]], open_at: datetime, day: date_cls) -> str:
    lines = [
        f"开闸 {open_at.strftime('%Y-%m-%d %H:%M:%S')}，观测 {day_str_iso(day)} 的占用",
        f"{'采样点':>8}  {'开拍':>12}  {'晚(ms)':>6}  {'拍完':>5}  {'已约':>5}  {'增量':>5}  失败区域",
        "（「拍完」= 最慢区域到手的秒数；每个区域各自的到手时刻在 JSON 的 room_fetched_at）",
    ]
    prev = 0
    for r in results:
        delta = r["booked_seats"] - prev
        prev = r["booked_seats"]
        lines.append(
            f"{format(r['offset'], '+d') + 's':>8}  {r['started_at']:>12}  {r['late_ms']:>6}  "
            f"{r['elapsed']:>5.2f}  {r['booked_seats']:>5}  {delta:>+5}  "
            f"{','.join(r['rooms_failed']) or '-'}"
        )
    if results:
        lines.append("")
        lines.append("各区域已约（最后一张）：")
        for name, n in sorted(results[-1]["per_room"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>4}  {name}")
    return "\n".join(lines)


def run_daily(args: argparse.Namespace) -> None:
    """常驻：每天到点跑一轮，出错记日志、睡到下一天再来，进程不退。"""
    hour, minute, second = (int(x) for x in DAILY_OPEN_HHMM.split(":"))
    while True:
        now = datetime.now()
        open_at = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if open_at - timedelta(seconds=args.login_lead) <= now:
            open_at += timedelta(days=1)
        logger.info("下一轮 %s 开闸，先睡到 %s", open_at.strftime("%m-%d %H:%M:%S"),
                    (open_at - timedelta(seconds=args.login_lead)).strftime("%m-%d %H:%M:%S"))
        try:
            _run_once(args, open_at)
        except Exception:  # noqa: BLE001 - 一天失败不能把常驻进程带走
            logger.exception("本轮采样异常结束")
        # 至少睡过开闸时刻，免得异常退出后立刻又把同一天算成「下一轮」
        _sleep_until(open_at + timedelta(seconds=60))


def _run_once(args: argparse.Namespace, open_at: datetime) -> List[Dict[str, Any]]:
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = open_at.date() + timedelta(days=1)
    if args.until:
        hour, minute = (int(x) for x in args.until.split(":"))
        until = open_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        until = default_until(open_at)
    offsets = expand_offsets(
        [int(x) for x in args.offsets.split(",") if x.strip()], args.every, open_at, until)
    lead = min(args.login_lead, max(0, int((open_at - datetime.now()).total_seconds()) - 5))
    return run(open_at, offsets, target, pid=args.pid, login_lead=lead,
               dry_run=args.dry_run, out_path=args.out)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--open", default="07:00:00",
                        help="开闸时刻 HH:MM[:SS]（默认 07:00:00，已过则取明天）或 now+90s")
    parser.add_argument("--offsets", default=DEFAULT_OFFSETS,
                        help=f"开闸后多少秒采样，逗号分隔，含负数时写成 --offsets=-10,1（默认 {DEFAULT_OFFSETS}）")
    parser.add_argument("--every", type=int, default=DEFAULT_EVERY_SECONDS,
                        help=f"显式采样点之后每隔多少秒再拍一张，0 关闭（默认 {DEFAULT_EVERY_SECONDS}）")
    parser.add_argument("--until", default=None,
                        help=f"最后一张不晚于 HH:MM（默认 {CLOSE_HHMM}，周五 {FRIDAY_CLOSE_HHMM}）")
    parser.add_argument("--date", default=None,
                        help="观测哪一天的占用 YYYY-MM-DD，默认开闸日的次日")
    parser.add_argument("--pid", default=None, help="观测账号，默认 SEAT_SNAPSHOT_PID")
    parser.add_argument("--login-lead", type=int, default=DEFAULT_LOGIN_LEAD_SECONDS,
                        help="提前多少秒登录")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    parser.add_argument("--daily", action="store_true",
                        help=f"常驻，每天 {DAILY_OPEN_HHMM} 跑一轮（忽略 --open / --date）")
    parser.add_argument("--out", default=None, help="JSON 汇总写到这个路径")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    args = _parse_args(argv)
    if args.daily:
        run_daily(args)
        return 0
    _run_once(args, _parse_open(args.open, datetime.now().date()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
