# -*- coding: utf-8 -*-
"""把每一次预约尝试的结果存下来，不再只写进会被滚掉的容器日志。

为什么这是最好的数据源
----------------------
抢座系统每天 7:00 才开放，7:00 之前板子一定是空的，所以唯一存在的信号是
「7:00 之后每张座位多快消失」。而**我们自己发出的每一个预约请求，本身就是
对那张座位的一次精确探测**：

    07:00:00.223  开枪 → 2F-B070
    07:00:05.392  回报 → 「设备在该时间段内已被预约」

这一行的含义就是「在开闸后 223 毫秒这一刻，B070 已经被人拿走了」。精确到毫秒，
零新增网络请求，而且覆盖的正好是用户真正想要的那些热门座位——最需要数据的那部分。

这个数据我们每天都在产生然后扔掉：以前只进 docker 日志，会被滚掉。
现在落进 `reserve_attempts` 集合留着。

写在什么时候
------------
在拿到图书馆的响应**之后**才写，所以它不会拖慢这一次下单；它唯一延后的是
循环里下一张座位的尝试，量级 1 毫秒——而第二发本来就要等第一发响应 5 秒多，
这 1 毫秒是噪声。任何异常都被吞掉：记日志失败绝不能影响抢座。

不做什么
--------
不碰 user_config_info，不改任何用户配置。只往自己的集合里 insert。
"""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional, Set

from pymongo import ASCENDING, MongoClient

from utils import config

logger = logging.getLogger(__name__)

ATTEMPT_COLLECTION = "reserve_attempts"

# 抢座开闸时刻，用来把每次尝试换算成「开闸后多少毫秒」——模型要的就是这个量。
# 和 scheduler_runner 读同一组环境变量，7:00 改了这里跟着走。
OPEN_HOUR = int(os.getenv("SCHEDULE_HOUR", "7"))
OPEN_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "0"))

# 距开闸这么久以内算「抢座那一波」，之外的是迟到保护/补约/手动那些非高峰尝试。
# 两类数据都有用，但绝不能混在一起算竞争度。
RUSH_WINDOW_MS = int(os.getenv("ATTEMPT_RUSH_WINDOW_MS", str(10 * 60 * 1000)))

# 「老是撞到自己已有预约」的判定窗口：往回看几天、其中要有几天是这样。
# 看 2 天而不是 1 天，是因为偶尔自己手动约一次不该被永久降级；而一旦停手，
# 窗口滑过去之后自己就恢复原来的位置，不需要任何人去改配置。
SELF_CONFLICT_LOOKBACK_DAYS = int(os.getenv("SELF_CONFLICT_LOOKBACK_DAYS", "7"))
SELF_CONFLICT_MIN_DAYS = int(os.getenv("SELF_CONFLICT_MIN_DAYS", "2"))

_client: Optional[MongoClient] = None
_indexes_ready = False


def _collection():
    global _client, _indexes_ready
    if _client is None:
        _client = MongoClient(config.get_mongo_uri())
    col = _client[config.DB_NAME][ATTEMPT_COLLECTION]
    if not _indexes_ready:
        # 建索引只做一次。这里不能用 unique：同一张座位一天可能被试多次
        # （补约重试、迟到保护换约），每一次都是一个独立的观测样本。
        col.create_index([("target_date", ASCENDING), ("pid", ASCENDING)],
                         name="date_pid")
        col.create_index([("seat_name", ASCENDING), ("target_date", ASCENDING)],
                         name="seat_date")
        _indexes_ready = True
    return col


def classify(message: str) -> str:
    """把图书馆回的那句中文归成几个可统计的类别。

    **建模时只有 `taken` 能当「被别人抢走」的证据。** 其余每一类失败都另有原因，
    跟座位热不热没关系，混进去会把竞争度算得一塌糊涂：

      success       我们拿到了 → 那一刻座位是活的
      taken         「设备在该时间段内已被预约」→ 那一刻座位已经没了。**唯一的竞争信号**
      self_conflict 「学工号为：xxx的用户在当前时段有预约」→ 撞的是自己已有的单子，
                    这种情况下无论座位空不空都会失败，**必须从竞争统计里剔掉**
      busy          「当前设备正在被预约，请稍后重试」→ 同一瞬间有别人也在下这张座位。
                    座位到底归谁还没定，不能当 taken；但它是最强的「有人盯着」证据，
                    建模时单独算一档，别丢进 other
      account_lock  「您有预约操作正在进行，请稍后操作」→ 账号级的锁，跟座位无关
                    （实测 2026-09-11：该用户自己也在 7:00 手动约，两边撞了），
                    和 self_conflict 一样剔掉
      out_of_window 超出提前预约窗口 → 我们自己发早了
      network       网络/状态码异常 → 没问到座位
    """
    text = message or ""
    if "预约成功" in text:
        return "success"
    if "设备在该时间段内已被预约" in text:
        return "taken"
    # 这条是实测撞出来的（2026-09-09 用观测账号打了一发）：账号自己在该时段
    # 已经有预约时，图书馆先报这个，根本不会去看座位空不空。
    if "的用户在当前时段有预约" in text:
        return "self_conflict"
    # 下面两句是 2026-09-11 07:00:00.47~0.74 落库时冒出来的，之前一直归在 other。
    if "当前设备正在被预约" in text:
        return "busy"
    if "您有预约操作正在进行" in text:
        return "account_lock"
    if "不在提前预约时间范围内" in text:
        return "out_of_window"
    if "网络请求异常" in text or "状态码" in text:
        return "network"
    if not text:
        return "unknown"
    return "other"


# 只有这两类说明「开枪那一刻座位到底活着没」，建模时只能用它们。
CONTENTION_OUTCOMES = ("success", "taken")


def _minutes(stamp: str) -> Optional[int]:
    """"2026-09-10 08:00:00" → 480。"""
    try:
        moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return moment.hour * 60 + moment.minute


def _offset_ms(fired_at: datetime) -> Optional[int]:
    """开枪时刻距**当天**开闸时刻多少毫秒；负数表示开闸之前。"""
    try:
        opened = fired_at.replace(hour=OPEN_HOUR, minute=OPEN_MINUTE,
                                  second=0, microsecond=0)
    except ValueError:
        return None
    return int((fired_at - opened).total_seconds() * 1000)


def build_record(
    pid: str,
    seat_name: str,
    dev_id: str,
    resv_begin_time: str,
    resv_end_time: str,
    fired_at: datetime,
    responded_at: datetime,
    message: str,
) -> Dict[str, Any]:
    """拼出要落库的那条记录。纯函数，方便单测。"""
    offset = _offset_ms(fired_at)
    target_date = (resv_begin_time or "")[:10]
    weekday = None
    try:
        weekday = datetime.strptime(target_date, "%Y-%m-%d").date().isoweekday()
    except (TypeError, ValueError):
        pass

    return {
        "target_date": target_date,
        "weekday": weekday,
        "pid": pid,
        "seat_name": seat_name,
        "dev_id": str(dev_id),
        "begin": resv_begin_time,
        "end": resv_end_time,
        "begin_min": _minutes(resv_begin_time),
        "end_min": _minutes(resv_end_time),
        "fired_at": fired_at,
        "responded_at": responded_at,
        "latency_ms": int((responded_at - fired_at).total_seconds() * 1000),
        # 模型真正要的那个特征：开闸后多少毫秒，这张座位还活着 / 已经没了。
        "offset_ms": offset,
        "phase": ("rush" if offset is not None and abs(offset) <= RUSH_WINDOW_MS
                  else "off_peak"),
        "outcome": classify(message),
        "message": message,
    }


def record(
    pid: str,
    seat_name: str,
    dev_id: str,
    resv_begin_time: str,
    resv_end_time: str,
    fired_at: datetime,
    message: str,
) -> None:
    """落一条尝试记录。**任何异常都吞掉**——记账失败不能影响抢座。"""
    try:
        doc = build_record(pid, seat_name, dev_id, resv_begin_time, resv_end_time,
                           fired_at, datetime.now(), message)
        _collection().insert_one(doc)
    except Exception as exc:  # noqa: BLE001 - 绝不让记账炸到调用方
        logger.warning("预约尝试记录写入失败（已忽略）: %s", exc)


def chronic_self_conflict_pids(
    today: Optional[date] = None,
    lookback_days: Optional[int] = None,
    min_days: Optional[int] = None,
) -> Set[str]:
    """挑出「最近老是撞到自己已有预约」的账号。

    为什么要挑出来
    --------------
    图书馆回「学工号为：xxx的用户在当前时段有预约」时，它根本没去看座位空不空——
    这个账号这一段无论打哪张座位都会失败。实测有账号连着四天都是这样（自己在
    图书馆那边手动约了同一段），每天 7:00 都要在开闸后最值钱的那几百毫秒里
    白打一轮，排在它后面能真抢到的人就得多等这一轮。

    怎么判定
    --------
    只看 7:00 那一波（phase=rush），按「哪一天的座位」分组：这一天里出现过
    self_conflict、而且一次都没成功过，才算一天。这样的天数够 `min_days` 就算。
    用 target_date 而不是 fired_at 做窗口，一来 rush 一天只有一批、正好一天一格，
    二来能吃上 (target_date, pid) 那条索引，7:00 开枪前多查这一次只是毫秒级。

    不做什么
    --------
    不写任何东西，尤其不碰 user_config_info：用户设的 priority 是用户的，这里
    只影响这一次排队的先后。查不到、查失败都返回空集合——这只是个排序优化，
    绝不能让它把抢座搞挂。
    """
    lookback = SELF_CONFLICT_LOOKBACK_DAYS if lookback_days is None else lookback_days
    threshold = SELF_CONFLICT_MIN_DAYS if min_days is None else min_days
    if lookback <= 0 or threshold <= 0:
        return set()
    cutoff = (today or date.today()) - timedelta(days=lookback)
    pipeline = [
        {"$match": {"target_date": {"$gte": cutoff.strftime("%Y-%m-%d")},
                    "phase": "rush"}},
        {"$group": {"_id": {"pid": "$pid", "target_date": "$target_date"},
                    "outcomes": {"$addToSet": "$outcome"}}},
        # 那一天撞了自己、而且一次都没约成——只失败一半不算，那说明备选里还有活路。
        {"$match": {"$and": [{"outcomes": "self_conflict"},
                             {"outcomes": {"$ne": "success"}}]}},
        {"$group": {"_id": "$_id.pid", "days": {"$sum": 1}}},
        {"$match": {"days": {"$gte": threshold}}},
    ]
    try:
        return {str(row["_id"]) for row in _collection().aggregate(pipeline)}
    except Exception as exc:  # noqa: BLE001 - 排序优化失败不能影响抢座
        logger.warning("查询自撞账号失败（已忽略）: %s", exc)
        return set()
