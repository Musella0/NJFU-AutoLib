"""
图书馆自动预约和迟到保护任务调度模块

本模块实现了两个主要功能：
1. 自动预约：根据用户配置自动预约图书馆座位
2. 迟到保护：对已预约的座位进行迟到保护，在用户可能迟到时自动调整预约时间

主要组件：
- 预约系统：处理用户的预约请求，包括时间计算、座位选择、预约执行等
- 迟到保护：监控已预约座位，在适当时间自动调整预约时间
- 调度系统：使用 APScheduler 管理定时任务
- 日志系统：记录所有操作和状态变化
- 数据库交互：使用 MongoDB 存储用户配置和预约信息

工作流程：
1. 自动预约：
   - 获取所有活动预约记录
   - 按优先级处理每个预约请求
   - 执行预约并更新状态
   - 记录预约结果

2. 迟到保护：
   - 扫描所有开启迟到保护的用户
   - 为每个需要保护的座位注册保护任务
   - 在指定时间执行保护动作
   - 更新预约状态

注意事项：
- 所有时间操作都基于服务器时间
- 预约时间需要提前计算
- 迟到保护在预约时间前5分钟触发
- 所有操作都有详细的日志记录
"""

import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Optional

import requests
from pymongo import MongoClient, DESCENDING

from utils.vpn_system import VPNSystem
from utils.library_system import LibrarySystem
from utils.notify import notify_user
from utils.crypto import decrypt as _dec
from utils import prelogin
from utils import config
from utils.reservation_blackout import find_any_reservation_conflict, find_reservation_conflict

# 日志配置
def setup_logging() -> logging.Logger:
    """
    配置日志系统

    设置日志格式、输出位置和日志级别。日志同时输出到文件和控制台。
    日志文件路径在 config.LOG_FILE 中配置。

    Returns:
        logging.Logger: 配置好的日志记录器
    """
    log_path = os.path.dirname(config.LOG_FILE)
    if not os.path.exists(log_path):
        os.makedirs(log_path)

    # 自定义日志格式
    log_format = (
        "[%(asctime)s] [%(levelname)s] "
        "[%(name)s] [用户:%(user)s] "
        "[%(operation)s] - %(message)s"
    )

    # 创建自定义过滤器
    class UserFilter(logging.Filter):
        def filter(self, record):
            if not hasattr(record, 'user'):
                record.user = '系统'
            if not hasattr(record, 'operation'):
                record.operation = '未知操作'
            return True

    # 创建处理器
    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    console_handler = logging.StreamHandler()

    # 设置处理器格式
    formatter = logging.Formatter(log_format)
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # 添加过滤器
    user_filter = UserFilter()
    file_handler.addFilter(user_filter)
    console_handler.addFilter(user_filter)

    # 配置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # 获取当前模块的日志记录器
    logger = logging.getLogger(__name__)
    return logger

logger = setup_logging()

def log_with_user(logger, level: str, user: str, operation: str, message: str) -> None:
    """
    统一的日志记录函数

    Args:
        logger: 日志记录器
        level: 日志级别
        user: 用户标识
        operation: 操作类型
        message: 日志消息
    """
    extra = {'user': user, 'operation': operation}
    if level == 'info':
        logger.info(message, extra=extra)
    elif level == 'error':
        logger.error(message, extra=extra)
    elif level == 'warning':
        logger.warning(message, extra=extra)
    elif level == 'debug':
        logger.debug(message, extra=extra)

# MongoDB 初始化
# 连接到MongoDB服务器，获取数据库和集合的引用
mongo_client = MongoClient(config.get_mongo_uri())
db = mongo_client.AutoLib
user_config_info = db.user_config_info  # 存储用户配置和预约记录
users_col = db.users  # 存储用户基本信息
pending_segments = db.pending_segments  # 超出提前预约窗口、等着补约的时段

IN_LIBRARY_STATUSES = {1093, 3141}  # 1093=使用中，3141=暂离，都表示已刷卡入馆
PENDING_CHECKIN_STATUS = 1027       # 待签到——只有这个状态的预约才谈得上「迟到」
_arrival_checks_backfilled = False

# 并发抢座：7:00 时所有账号并行执行，避免串行排队让靠后的用户错过黄金窗口。
# 上限不宜过高，webvpn 网关和图书馆接口都扛不住太猛的并发。
RESERVE_CONCURRENCY = int(os.getenv("RESERVE_CONCURRENCY", "8"))
# 午休功能总开关，见 main.py 的同名说明。关掉时每日自动午休整个不跑。
NAP_ENABLED = os.getenv("NAP_ENABLED", "0").strip().lower() not in ("0", "false", "no", "off")

# 图书馆服务端规则 resvRule.earliestResvTime（实测 1860 分钟 = 31 小时）：一个时段
# 最早只能提前这么久下单，而且是相对「预约开始时间」算的。所以 7:00 那批最远只够得到
# 次日 14:00 开始的时段，再往后的段发过去只会得到「不在提前预约时间范围内」。
# 超窗的段不能硬发，只能排进 pending_segments，等 开始时间-31h 到了再补约。
EARLIEST_RESV_MINUTES = int(os.getenv("EARLIEST_RESV_MINUTES", "1860"))
TIME_FMT = "%Y-%m-%d %H:%M:%S"

# 这条线是**逐秒**判定的（2026-09-07 实测：提前 1859.7 分钟成功、1860.7 分钟被拒），
# 所以下单时刻要离边界留点余量。踩着线发、本机时钟又比图书馆快几秒，
# 换来的就是一句「不在提前预约时间范围内」和一个白白烧掉的时段。
RESV_WINDOW_MARGIN_SECONDS = int(os.getenv("RESV_WINDOW_MARGIN_SECONDS", "120"))
# resvRule.maxResvTime：单次预约最长 900 分钟。占位预约把起点往前顶时会先撞到它。
MAX_RESV_MINUTES = int(os.getenv("MAX_RESV_MINUTES", "900"))
# resvRule.minResvTime：不足 120 分钟的单子图书馆不收。
MIN_RESV_MINUTES = int(os.getenv("MIN_RESV_MINUTES", "120"))
# 开馆时间（resvRule 里的 openStart），占位预约的起点不能早于它。
LIBRARY_OPEN_TIME = os.getenv("LIBRARY_OPEN_TIME", "07:30")
# 占位抢座总开关。关掉就退回「超窗只排队、不占位」的旧行为。
SEGMENT_HOLD_ENABLED = os.getenv("SEGMENT_HOLD_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off"
)
# 换约失败后一直重试，直到占位预约开始前这么多分钟；之后才放弃并报警。
SEGMENT_SWAP_DEADLINE_MINUTES = int(os.getenv("SEGMENT_SWAP_DEADLINE_MINUTES", "30"))
# 纯排队（没占上位）的段补约失败后最多再试这么多次，每分钟一次。
# 原先一次失败就判死，窗口边界、网络抖动、登录失败这种一次性问题会白白烧掉一个时段。
# 但也不能无限试：每次重试都要重走一遍 webvpn + CAS，二十几个账号一起转扛不住。
SEGMENT_RETRY_MAX_ATTEMPTS = int(os.getenv("SEGMENT_RETRY_MAX_ATTEMPTS", "10"))


def bookable_at(resv_begin_time: str) -> datetime:
    """图书馆规则本身：该时段最早可以下单的时刻（开始时间 − 31 小时）。"""
    return datetime.strptime(resv_begin_time, TIME_FMT) - timedelta(minutes=EARLIEST_RESV_MINUTES)


def safe_bookable_at(resv_begin_time: str) -> datetime:
    """我们实际去下单的时刻：在规则边界上再退一点，别踩线。"""
    return bookable_at(resv_begin_time) + timedelta(seconds=RESV_WINDOW_MARGIN_SECONDS)


def latest_bookable_start(now: datetime) -> datetime:
    """此刻能下单的、最晚的那个「预约开始时间」。"""
    moment = now + timedelta(minutes=EARLIEST_RESV_MINUTES) - timedelta(seconds=RESV_WINDOW_MARGIN_SECONDS)
    # 落到整分：面板上好看，也避免每次跑出来的占位起点都差几秒。
    return moment.replace(second=0, microsecond=0)


def plan_hold_window(
    now: datetime,
    resv_begin_time: str,
    resv_end_time: str,
    prev_segment_end: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """
    给超窗的时段算一张「占位预约」的时间窗，算不出来就返回 None（退回纯排队）。

    图书馆只拿 resvBeginTime 去比 31 小时窗口——实测起点 16:30、终点次日 22:00
    （终点提前 36 小时）的单子照样能约成。所以超窗的段不必干等：现在就下一张
    **[窗口边界, 本段终点]** 的单子，它完整盖住目标时段，别人插不进来；等目标时段
    自己进了窗口，再取消换成精确时段（见 _swap_hold_to_segment）。

    起点顶到能顶的最远处，但要同时躲开三条服务端规则，任一条不满足就没得占：
      * 起点不能晚于 now + 31h（earliestResvTime）——占位的意义就是顶到这条线；
      * 单次时长不能超过 900 分钟（maxResvTime）——终点太远时起点被迫往后挪；
      * 起点不能早于当天开馆，也不能压到同一天自己前一段的预约上，
        否则图书馆报「用户在当前时段有预约」，占位反而把自己挡了。
    """
    begin = datetime.strptime(resv_begin_time, TIME_FMT)
    end = datetime.strptime(resv_end_time, TIME_FMT)

    hold_start = latest_bookable_start(now)
    if hold_start >= begin:
        return None  # 这段本来就够得着，用不着占位

    open_hour, open_minute = (int(part) for part in LIBRARY_OPEN_TIME.split(":"))
    lower_bounds = [
        end - timedelta(minutes=MAX_RESV_MINUTES),
        begin.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0),
    ]
    if prev_segment_end:
        lower_bounds.append(datetime.strptime(prev_segment_end, TIME_FMT))
    if hold_start < max(lower_bounds):
        return None
    if end - hold_start < timedelta(minutes=MIN_RESV_MINUTES):
        return None
    return hold_start.strftime(TIME_FMT), resv_end_time


def _as_status_code(raw: Any) -> Optional[int]:
    """
    把 resvStatus 统一成 int。上游返回 int，但 owned_seat 里存的是 str
    （见 library_system._format_reservation_data），两边比较必须先归一化，
    否则 '1027' in {1027} 永远是 False，判断会静默失效。
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _blackout_message(conflict: Dict[str, Any]) -> str:
    return (
        f"学校闭馆，已跳过预约：{conflict.get('title', '图书馆闭馆通知')}；"
        f"暂停 {conflict.get('pause_from', '')} 至 {conflict.get('pause_until', '')}；"
        f"原文 {conflict.get('source_url', '')}"
    )

def get_all_active_reservations() -> List[Dict[str, Any]]:
    """
    获取所有正在预约的记录

    从数据库中查询所有标记为活动的预约记录，并按优先级降序排序。
    活动记录条件：is_reserved == "True" 且 verified == True，
    避免对未通过凭据验证的账号重复尝试预约并产生失败通知。

    Returns:
        List[Dict[str, Any]]: 按优先级排序的预约记录列表，每条记录包含完整的预约配置
    """
    candidates = list(
        user_config_info.find({
            "is_reserved": "True",
            "verified": True,
            "seat_list": {"$type": "array", "$ne": []},
        }).sort([("priority", DESCENDING), ("updated_at", DESCENDING)])
    )
    # 同一学号即使残留于不同游客会话，也只能进入一次预约队列。
    active = []
    seen_pids = set()
    for candidate in candidates:
        pid = candidate.get("pid")
        if not pid or pid in seen_pids:
            continue
        seen_pids.add(pid)
        active.append(candidate)
    return active

def get_seat_ids(seat_list: List[str]) -> List[str]:
    """
    根据设备名称列表获取设备ID

    将用户配置中的座位名称转换为系统内部的座位ID。
    如果某个座位名称在数据库中不存在，会记录警告日志但继续处理其他座位。

    Args:
        seat_list: 座位名称列表，如 ["A区-101", "B区-202"]

    Returns:
        List[str]: 座位ID列表，如 ["100500174", "100500175"]
    """
    seat_ids = []
    for device_name in seat_list:
        device = db.devices.find_one({"devName": device_name}, {"_id": 0, "devId": 1})
        if device:
            seat_ids.append(device["devId"])
        else:
            log_with_user(logger, 'warning', '系统', '座位ID获取', f"设备号 {device_name} 不存在")
    return seat_ids

REST_VALUES = ('休息', 'off')
WEEK_LABELS = ('一', '二', '三', '四', '五', '六', '日')


def _to_segments(raw: Any) -> List[str]:
    """
    将时间配置统一规范为段列表。

    - 字符串 "HH:MM-HH:MM" → ["HH:MM-HH:MM"]
    - 列表 [..] → 过滤掉无效/休息项
    - "休息" / "off" / 空 → []
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, str) and s not in REST_VALUES and '-' in s]
    if isinstance(raw, str):
        if raw in REST_VALUES:
            return []
        if '-' in raw:
            return [raw]
    return []


def _resolve_day_config(res_item: Dict[str, Any]) -> Tuple[datetime, Any]:
    """
    按预约模式定位目标日期和它那天的原始时间配置。

    calculate_reservation_time 和 rest_day_label 共用这一份，
    否则「哪天算休息」会和「哪天出段」各算各的，迟早对不上。
    """
    mode = res_item["mode"]
    now = datetime.now()

    if mode == "week_time":
        target_date = now + timedelta(days=1)
        raw = res_item.get('time', {}).get('week_time', {}).get(str(target_date.isoweekday()))
    elif mode == "tomorrow":
        target_date = now + timedelta(days=1)
        raw = res_item.get("time", {}).get("tomorrow")
    elif mode == "after_tomorrow":
        target_date = now + timedelta(days=2)
        # 优先读 after_tomorrow 字段，缺省回退到 tomorrow（兼容旧配置）
        raw = (res_item.get("time", {}).get("after_tomorrow")
               or res_item.get("time", {}).get("tomorrow"))
    else:
        raise ValueError(f"不支持的预约模式: {mode}")

    return target_date, raw


def rest_day_label(res_item: Dict[str, Any]) -> Optional[str]:
    """
    目标日被用户显式设成休息时，返回「周二休息」这样的说法；否则返回 None。

    只认显式的 "休息"/"off"。整天没配（raw 为空）是配置缺失，不是休息，
    仍旧要按错误报出来，否则用户漏配一天会被悄悄咽掉。
    """
    target_date, raw = _resolve_day_config(res_item)
    if isinstance(raw, str) and raw in REST_VALUES:
        return f"周{WEEK_LABELS[target_date.isoweekday() - 1]}休息"
    return None

def calculate_reservation_time(res_item: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    根据预约模式计算预约时间段列表（支持一天多段）。

    支持三种预约模式：
    1. week_time: 根据星期几选择对应的时间段
    2. tomorrow: 预约明天的时间段
    3. after_tomorrow: 预约后天的时间段

    时间格式为 "YYYY-MM-DD HH:MM:SS"

    Returns:
        List[Tuple[str, str]]: 每段 (开始时间, 结束时间)；可能为空列表
    """
    target_date, raw = _resolve_day_config(res_item)

    date_str = target_date.strftime("%Y-%m-%d")
    is_friday = target_date.isoweekday() == 5
    result: List[Tuple[str, str]] = []

    for seg in _to_segments(raw):
        try:
            begin_time, end_time = seg.split("-")
        except ValueError:
            continue
        # 周五 20:00 关闭
        if is_friday and end_time > "20:00":
            end_time = "20:00"
        if begin_time >= end_time:
            continue
        # 跳过不满 120 分钟的时段（图书馆最低预约时长限制）
        _b = datetime.strptime(begin_time, "%H:%M")
        _e = datetime.strptime(end_time, "%H:%M")
        if (_e - _b).total_seconds() < 7200:
            continue
        result.append((
            f"{date_str} {begin_time}:00",
            f"{date_str} {end_time}:00"
        ))
    return result

def update_user_config(pid: str, result: str) -> None:
    """
    更新用户配置信息

    直接更新数据库中的用户配置，记录预约结果和更新时间。
    使用 upsert 确保即使记录不存在也能创建新记录。

    Args:
        pid: 用户ID（学号）
        result: 预约结果信息
    """
    try:
        user_config_info.update_one(
            {"pid": pid},
            {
                "$set": {
                    "result": result,
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )
    except Exception as e:
        log_with_user(logger, 'error', pid, '用户配置更新', f"更新用户配置失败: {str(e)}")

def handle_reservation_error(pid: str, error_msg: str) -> None:
    """
    处理预约错误

    记录错误日志并更新用户配置，确保用户能看到错误信息。

    Args:
        pid: 用户ID
        error_msg: 错误信息
    """
    log_with_user(logger, 'error', pid, '预约异常', error_msg)
    update_user_config(pid, error_msg)


def _reserve_one_segment(
    library: LibrarySystem,
    pid: str,
    seat_ids: List[str],
    resv_begin_time: str,
    resv_end_time: str,
    seg_label: str
) -> Tuple[bool, str]:
    """
    预约一个时段。7:00 主流程和排队补约共用这一份，两边的日志和结果格式才不会跑偏。

    Returns:
        Tuple[bool, str]: (是否成功, 写进 result 给用户看的一行)
    """
    try:
        log_with_user(logger, 'info', pid, '预约执行', f"开始预约 {seg_label}")
        res_message, user_info = library.reserve_seat(
            seat_list=seat_ids,
            resv_begin_time=resv_begin_time,
            resv_end_time=resv_end_time
        )
    except Exception as e:
        err = f"{seg_label} 异常: {str(e)}"
        log_with_user(logger, 'error', pid, '预约异常', err)
        return False, f"❌ {err}"

    if "成功" in res_message or "预约成功" in res_message:
        log_with_user(logger, 'info', pid, '预约结果', f"{seg_label} 成功: {res_message}")
        if user_info:
            library.insert_or_update_mongo(
                collection_name="users",
                pid=user_info.get("pid"),
                data=user_info,
                upsert=True
            )
        # res_message 已是「✅ 08-08 · 08:30-22:00 · 3F-C109 · 预约成功」格式
        return True, res_message

    log_with_user(logger, 'error', pid, '预约失败', f"{seg_label} 失败: {res_message}")
    return False, f"❌ {seg_label}: {res_message}"


def queue_pending_segment(
    pid: str,
    resv_begin_time: str,
    resv_end_time: str,
    open_at: datetime,
    hold: Optional[Dict[str, Any]] = None,
) -> None:
    """
    把超窗的时段排进补约队列，占到位的话连占位预约一起记下来。

    按 (学号, 起止时间) upsert：同一段被重复排队——比如用户又点了一次「立即预约」——
    只会更新同一条记录，不会攒出一堆重复补约。

    没占到位（hold 为 None）时**不去动**已存的 hold 字段：那张占位预约在图书馆那边
    还占着座，记录一抹掉就再也没人去取消它，会一直把目标时段挡住。
    """
    now = datetime.now()
    changes: Dict[str, Any] = {
        "open_at": open_at, "status": "pending", "updated_at": now, "attempts": 0,
    }
    if hold:
        changes["hold"] = hold
    pending_segments.update_one(
        {"pid": pid, "resv_begin_time": resv_begin_time, "resv_end_time": resv_end_time},
        {
            "$set": changes,
            "$setOnInsert": {"created_at": now},
        },
        upsert=True
    )


def _pending_result_line(
    seg_label: str, open_at: datetime, hold: Optional[Dict[str, Any]] = None
) -> str:
    hours = EARLIEST_RESV_MINUTES // 60
    if hold:
        return (f"⏳ {seg_label}: 已用 {hold.get('dev_name', '座位')} 占位"
                f"（{hold.get('resv_begin_time', '')[11:16]} 起），"
                f"{open_at:%m-%d %H:%M} 自动换回本段时间")
    return (f"⏳ {seg_label}: 图书馆最多提前 {hours} 小时预约，"
            f"已排到 {open_at:%m-%d %H:%M} 自动补约")


def _replace_queued_result(pid: str, resv_begin_time: str, resv_end_time: str, line: str) -> None:
    """
    用补约结果替换掉 result 里那条「⏳ 已排队」占位。

    直接覆盖整个 result 会把同一天其他段的结果一起抹掉，所以按时间段匹配，只换自己那行。
    """
    cfg = user_config_info.find_one({"pid": pid}, {"result": 1}) or {}
    span = f"{resv_begin_time[11:16]}-{resv_end_time[11:16]}"
    kept = [
        item for item in (cfg.get("result") or "").splitlines()
        if not (item.startswith("⏳") and span in item)
    ]
    kept.append(line)
    update_user_config(pid, "\n".join(kept))


def _finish_pending_segment(doc: Dict[str, Any], status: str, message: str) -> None:
    """给队列里的一条打上终态，顺手把结果写回用户面板。"""
    pid = doc.get("pid", "?")
    pending_segments.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": status, "message": message, "updated_at": datetime.now()}}
    )
    log_with_user(logger, 'info' if status == "done" else 'warning', pid, '排队补约',
                  f"{doc.get('resv_begin_time')} ~ {doc.get('resv_end_time')} → {status}: {message}")
    _replace_queued_result(pid, doc.get("resv_begin_time", ""), doc.get("resv_end_time", ""), message)


def _live_reservations(library: LibrarySystem) -> Optional[List[Dict[str, Any]]]:
    """
    回图书馆拉一遍当前有效预约；查不到返回 None（**不是**空列表）。

    这一步顺手重建 owned_seat——迟到保护就是照着它扫的，占位/换约动过预约之后
    不刷一次，新的那条就进不了保护。
    """
    try:
        reservations, message = library.get_reservation_info()
    except Exception as exc:
        log_with_user(logger, 'warning', library.username, '预约核对', f"查询预约异常: {exc}")
        return None
    if reservations is None:
        log_with_user(logger, 'warning', library.username, '预约核对', f"查询预约失败: {message}")
        return None
    return reservations


def _find_reservation(
    reservations: List[Dict[str, Any]], resv_begin_time: str, resv_end_time: str
) -> Optional[Dict[str, Any]]:
    """在有效预约里找起止时间**完全相等**的那一条。"""
    for item in reservations:
        if (item.get("resvBeginTime") == resv_begin_time
                and item.get("resvEndTime") == resv_end_time):
            return item
    return None


def _place_hold(
    library: LibrarySystem,
    pid: str,
    seat_ids: List[str],
    hold_begin: str,
    hold_end: str,
    seg_label: str,
) -> Optional[Dict[str, Any]]:
    """
    先把座位占住：下一张 [窗口边界, 本段终点] 的预约，返回它的 uuid / 座位。

    占不到不算错——只是回到「纯排队」的老行为，真正的补约还在队列里等窗口，
    所以这里只记日志，不往用户面板写失败。
    """
    label = f"{seg_label} 占位 {hold_begin[11:16]}-{hold_end[11:16]}"
    ok, line = _reserve_one_segment(library, pid, seat_ids, hold_begin, hold_end, label)
    if not ok:
        log_with_user(logger, 'warning', pid, '占位预约', f"{label} 没占到，退回纯排队: {line}")
        return None

    hold = dict(library.last_reservation or {})
    if not hold.get("uuid"):
        # 没有 uuid 就取消不掉它，而它又正好盖住目标时段——等于自己把自己挡死。
        # 兜底回图书馆按时间把这条捞出来；再捞不到就只能报错等人工。
        found = _find_reservation(_live_reservations(library) or [], hold_begin, hold_end)
        hold.update({
            "uuid": (found or {}).get("uuid", ""),
            "dev_id": hold.get("dev_id", ""),
            "dev_name": hold.get("dev_name") or (found or {}).get("devInfo", {}).get("devName", ""),
            "resv_begin_time": hold_begin,
            "resv_end_time": hold_end,
        })
    if not hold.get("uuid"):
        log_with_user(logger, 'error', pid, '占位预约',
                      f"{label} 下单成功却拿不到 uuid，这张占位没人能取消，需要人工清理")
        return None

    hold["created_at"] = datetime.now()
    log_with_user(logger, 'info', pid, '占位预约',
                  f"{label} 已占住 {hold.get('dev_name')}（uuid {hold['uuid']}）")
    return hold


def _verify_swap(
    library: LibrarySystem, resv_begin_time: str, resv_end_time: str, hold_uuid: str
) -> bool:
    """
    换约后的检测：不认下单接口那句「预约成功」，回图书馆核对真实状态。

    必须**同时**满足两条才算换过来了：
      * 有一条起止时间正好等于目标时段的有效预约；
      * 当初那张占位预约已经不在有效列表里。
    差一条就返回 False，让这段留在队列里下一分钟接着试——宁可重试，
    也不能把「手上还占着比配置更早的时段」当成功报给用户。
    """
    reservations = _live_reservations(library)
    if reservations is None:
        return False
    if hold_uuid and any(item.get("uuid") == hold_uuid for item in reservations):
        return False
    return _find_reservation(reservations, resv_begin_time, resv_end_time) is not None


def _swap_hold_to_segment(
    library: LibrarySystem,
    pid: str,
    doc: Dict[str, Any],
    seat_ids: List[str],
    seg_label: str,
) -> Tuple[bool, str]:
    """
    窗口开了，把占位预约换成精确时段：先取消占位，紧接着重新下单，最后回查核对。

    顺序不能反：同一个人在重叠时段上再下一单会被「用户在当前时段有预约」直接顶掉，
    所以只能先取消。两次请求之间座位是裸的（约零点几秒），因此复用同一个已登录会话、
    中间不插任何多余 IO，并且优先约回刚放开的那张座位。
    """
    hold = doc.get("hold") or {}
    resv_begin_time = doc["resv_begin_time"]
    resv_end_time = doc["resv_end_time"]
    hold_uuid = hold.get("uuid", "")

    # 占位可能已经被用户自己或图书馆取消了。查不到状态就什么都别动：
    # 万一占位其实还在，硬发一单只会撞上自己，白白浪费一次机会。
    reservations = _live_reservations(library)
    if reservations is None:
        return False, f"❌ {seg_label}: 查不到当前预约状态，本轮不动占位，下一轮重试"

    hold_alive = any(item.get("uuid") == hold_uuid for item in reservations)
    existing = _find_reservation(reservations, resv_begin_time, resv_end_time)
    if existing:
        # 目标时段已经在手上，没什么可换的。2026-09-11 实测：用户在图书馆 App 里看到
        # 13:58 那张占位不对劲，自己取消后手动约了 14:00——这时再下单只会被
        # 「用户在当前时段有预约」顶回，而且占位段的重试不封顶，会每分钟登录一次
        # 直到期限，最后还给用户发一条错误的「换约失败请手动处理」。
        dev_name = (existing.get("devInfo") or {}).get("devName") or hold.get("dev_name") or "座位"
        if hold_alive:
            # 理论上图书馆不允许同一人重叠时段并存，但真出现了就把占位清掉，
            # 清不掉也不影响结论：目标时段确实已经拿到了。
            deleted, message = library.delete_seat(hold_uuid)
            log_with_user(logger, 'warning' if not deleted else 'info', pid, '占位换约',
                          f"{seg_label} 目标时段已在，占位 {hold.get('dev_name')} "
                          f"{'已一并取消' if deleted else '取消失败: ' + str(message)}")
        log_with_user(logger, 'info', pid, '占位换约',
                      f"{seg_label} 有效预约里已有本段（{dev_name}），无需换约")
        return True, (f"✅ {resv_begin_time[5:10]} · {resv_begin_time[11:16]}-{resv_end_time[11:16]}"
                      f" · {dev_name} · 已有本段预约，无需换约")

    if hold_alive:
        deleted, message = library.delete_seat(hold_uuid)
        if not deleted:
            # 没删掉就绝不能往下走：目标时段和占位重叠，发出去必被顶回来，
            # 还会让人以为「换约失败 = 座位没了」，其实座位一直在自己手里。
            return False, f"❌ {seg_label}: 占位预约取消失败（{message}），下一轮重试"
        log_with_user(logger, 'info', pid, '占位换约',
                      f"{seg_label} 已取消占位 {hold.get('dev_name')} "
                      f"{hold.get('resv_begin_time')}，立刻改约精确时段")
    else:
        log_with_user(logger, 'info', pid, '占位换约',
                      f"{seg_label} 占位预约已不在（可能被手动取消），直接按普通补约下单")

    ordered_seats = list(seat_ids)
    hold_dev = hold.get("dev_id")
    if hold_dev:
        # 刚放开的那张排最前面：这零点几秒里最可能还空着的就是它。
        ordered_seats = [hold_dev] + [seat for seat in ordered_seats if seat != hold_dev]

    ok, line = _reserve_one_segment(
        library, pid, ordered_seats, resv_begin_time, resv_end_time, seg_label
    )
    if not ok:
        return False, line
    if not _verify_swap(library, resv_begin_time, resv_end_time, hold_uuid):
        return False, f"❌ {seg_label}: 换约后回查未通过（占位可能还在），下一轮重试"
    return True, line


def _retry_or_finish_segment(
    doc: Dict[str, Any],
    cfg: Dict[str, Any],
    seg_label: str,
    line: str,
    now: datetime,
) -> None:
    """
    补约/换约没成时的收尾：先重试，别一次失败就把这个时段判死。

    两种段「重试到什么时候」不一样：
      * 占位过的段——**绝不能**判失败了事，那等于默认接受了那张开始时间比配置更早的
        占位预约，到点签不上到就是迟到。一直重试到占位快开始为止（还有二十多个小时），
        真放弃时必须吵醒用户，因为那时他手上这张预约的时间是错的。
      * 纯排队的段——手上什么都没有，重试只为吃掉窗口边界/网络/登录那几种一次性抖动，
        试满 SEGMENT_RETRY_MAX_ATTEMPTS 次就照旧判失败，不然重登会把网关拖垮。
    """
    pid = doc.get("pid", "?")
    hold = doc.get("hold") or {}
    attempts = int(doc.get("attempts") or 0) + 1

    if hold:
        hold_begin = hold.get("resv_begin_time") or ""
        deadline = (datetime.strptime(hold_begin, TIME_FMT)
                    - timedelta(minutes=SEGMENT_SWAP_DEADLINE_MINUTES)) if hold_begin else None
        give_up = deadline is not None and now >= deadline
        final_line = (f"❌ {seg_label}: 换约失败 {attempts} 次且已到最后期限，"
                      f"手上仍是占位预约（{hold.get('dev_name', '座位')} {hold_begin[11:16]} 起，"
                      f"比配置的早），请手动处理")
        give_up_title, retry_title = "❌ 占位换约失败", "⚠️ 占位换约未成功"
        retry_body = f"{line}\n座位还占着，正在每分钟重试"
    else:
        give_up = attempts >= SEGMENT_RETRY_MAX_ATTEMPTS
        final_line = f"{line}（已重试 {attempts} 次）"
        give_up_title, retry_title = "❌ 补约失败", "⚠️ 补约未成功"
        retry_body = f"{line}\n正在每分钟重试，最多 {SEGMENT_RETRY_MAX_ATTEMPTS} 次"

    if give_up:
        _finish_pending_segment(doc, "failed", final_line)
        notify_user(cfg, give_up_title, f"学号 {pid}\n{final_line}", always=True)
        return

    pending_segments.update_one(
        {"_id": doc["_id"]},
        {"$set": {"attempts": attempts, "message": line, "updated_at": datetime.now()}},
    )
    log_with_user(logger, 'warning', pid, '排队补约',
                  f"{seg_label} 第 {attempts} 次未成功，保持排队下一轮重试: {line}")
    if attempts == 1:
        notify_user(cfg, retry_title, f"学号 {pid}\n{retry_body}", always=True)


def process_due_segments(now: Optional[datetime] = None) -> None:
    """
    补约那些当初超出 31 小时窗口、现在刚够得着的时段。

    独立常驻 job 扫数据库，而不是 7:00 时挂一串一次性定时任务：后者只活在内存里，
    白天重启一次容器当天所有补约就静默消失了（迟到保护踩过同样的坑）。

    Args:
        now: 仅测试用，默认取当前时间
    """
    now = now or datetime.now()
    try:
        due = list(
            pending_segments.find({"status": "pending", "open_at": {"$lte": now}}).sort("open_at", 1)
        )
    except Exception as exc:
        log_with_user(logger, 'error', '系统', '排队补约', f"读取补约队列失败: {exc}")
        return

    for doc in due:
        pid = doc.get("pid", "?")
        resv_begin_time = doc.get("resv_begin_time", "")
        resv_end_time = doc.get("resv_end_time", "")
        seg_label = f"{resv_begin_time[5:10]} {resv_begin_time[11:16]}-{resv_end_time[11:16]}"
        cfg: Optional[Dict[str, Any]] = None
        try:
            if datetime.strptime(resv_begin_time, TIME_FMT) <= now:
                _finish_pending_segment(doc, "expired", f"⚠️ {seg_label}: 已过开始时间，放弃补约")
                continue

            cfg = user_config_info.find_one({"pid": pid, "is_reserved": "True", "verified": True})
            if not cfg or not cfg.get("seat_list"):
                _finish_pending_segment(doc, "skipped", f"⚠️ {seg_label}: 账号已停用自动预约，跳过补约")
                continue

            conflict = find_reservation_conflict(db.school_notice_reviews, resv_begin_time, resv_end_time)
            if conflict:
                _finish_pending_segment(doc, "skipped", _blackout_message(conflict))
                continue

            seat_ids = get_seat_ids(cfg["seat_list"])
            if not seat_ids:
                _finish_pending_segment(doc, "failed", f"❌ {seg_label}: 未找到有效的座位ID")
                continue

            vpn_password = _dec(cfg["vpn_password"])
            library = LibrarySystem(
                username=pid,
                password=vpn_password,
                vpn_password=vpn_password
            )
            hold = doc.get("hold") or {}
            if hold:
                # 7:00 已经拿一张更早开始的预约把座位占住了，这里做的是「换」不是「抢」：
                # 取消占位 → 立刻改约精确时段 → 回查核对，三步缺一不可。
                ok, line = _swap_hold_to_segment(library, pid, doc, seat_ids, seg_label)
            else:
                ok, line = _reserve_one_segment(
                    library, pid, seat_ids, resv_begin_time, resv_end_time, seg_label
                )
                if ok:
                    try:
                        # get_reservation_info 会重建 owned_seat，迟到保护是照着它扫的，
                        # 不刷一次这条补出来的预约就进不了保护。
                        library.get_reservation_info()
                    except Exception as exc:
                        log_with_user(logger, 'warning', pid, '排队补约', f"同步预约信息失败: {exc}")

            if ok:
                _finish_pending_segment(doc, "done", line)
                notify_user(cfg, "✅ 补约成功", f"学号 {pid}\n{line}")
            else:
                # 一次失败不收尾：占位过的段收尾等于接受更早的时间，
                # 纯排队的段收尾等于让一次抖动白吃掉一个时段。
                _retry_or_finish_segment(doc, cfg, seg_label, line, now)
        except Exception as exc:
            log_with_user(logger, 'error', pid, '排队补约', f"{seg_label} 补约异常: {exc}")
            # 异常同样走重试：占位过的段还占着更早的时段，纯排队的段也可能只是抖了一下。
            _retry_or_finish_segment(doc, cfg or {}, seg_label,
                                     f"❌ {seg_label}: 补约异常 {exc}", now)


def reservation(res_item: Dict[str, Any], now: Optional[datetime] = None) -> None:
    """
    处理单个预约请求

    完整的预约流程：
    1. 计算预约时间
    2. 获取座位ID
    3. 登录VPN和图书馆系统
    4. 执行预约（窗口内的段直接约，超窗的段先占位再排队补约）
    5. 更新用户信息
    6. 记录预约结果

    Args:
        res_item: 用户配置
        now: 仅测试用，默认取当前时间
    """
    # 加载账号信息
    pid = res_item["pid"]
    vpn_password = _dec(res_item["vpn_password"])
    seat_list = res_item["seat_list"]

    try:
        # 计算预约时间段列表（支持多段）
        segments = calculate_reservation_time(res_item)
        if not segments:
            # 用户自己关掉的那天是正常状态，不能走 handle_reservation_error——
            # 那条会被前端判成「预约失败」标红，休息日天天弹一次红。
            rest = rest_day_label(res_item)
            if rest:
                message = f"{rest}，已跳过预约"
                log_with_user(logger, 'info', pid, '休息日', message)
                update_user_config(pid, message)
                return
            log_with_user(logger, 'error', pid, '预约时间', "未找到有效的预约时间段")
            handle_reservation_error(pid, "未配置有效的预约时间段")
            return

        conflict = find_any_reservation_conflict(db.school_notice_reviews, segments)
        if conflict:
            message = _blackout_message(conflict)
            log_with_user(logger, 'info', pid, '闭馆保护', message)
            update_user_config(pid, message)
            return

        log_with_user(logger, 'info', pid, '预约时间',
                     f"共 {len(segments)} 段: " + "; ".join([f"{b}~{e}" for b, e in segments]))

        # 获取座位ID
        seat_ids = get_seat_ids(seat_list)
        if not seat_ids:
            log_with_user(logger, 'error', pid, '座位获取', "未找到有效的座位ID")
            handle_reservation_error(pid, "未找到有效的座位ID")
            return

        # 按提前预约窗口把段分成两拨：现在够得着的照常抢，够不着的排队等窗口打开。
        now = now or datetime.now()
        plan = [
            (idx, begin, end, safe_bookable_at(begin))
            for idx, (begin, end) in enumerate(segments, 1)
        ]
        due = [item for item in plan if item[3] <= now]
        queued = [item for item in plan if item[3] > now]

        # 超窗的段不是干等：先给它算一张起点顶到窗口边界、终点就是本段终点的占位预约，
        # 它完整盖住目标时段，别人抢不进来。算在登录之前——一张都占不了（比如后天的段）
        # 且没有窗口内的段时，照旧连 webvpn 都不用登。
        hold_plans: Dict[int, Tuple[str, str]] = {}
        if SEGMENT_HOLD_ENABLED:
            for idx, resv_begin_time, resv_end_time, _ in queued:
                window = plan_hold_window(
                    now, resv_begin_time, resv_end_time,
                    prev_segment_end=segments[idx - 2][1] if idx >= 2 else None,
                )
                # 占位比目标时段起得早，闭馆窗口要按占位这段重新对一次。
                if window and not find_reservation_conflict(
                        db.school_notice_reviews, window[0], window[1]):
                    hold_plans[idx] = window

        # 初始化图书馆系统（多段共享同一会话）
        # 优先复用预登录好的会话：webvpn + CAS 那 4~8 秒已经在 6:50 付过了，
        # 这里直接就能发预约请求。取不到（没预登录、会话过期、校验失败）
        # 就照旧现场登录，行为与加这个功能之前完全一致。
        library = None
        if due or hold_plans:
            library = prelogin.take(pid)
            if library is not None:
                log_with_user(logger, 'info', pid, '系统初始化', "复用预登录会话")
            else:
                log_with_user(logger, 'info', pid, '系统初始化', "开始初始化图书馆系统")
                library = LibrarySystem(
                    username=pid,
                    password=vpn_password,
                    vpn_password=vpn_password
                )

        # 逐段预约
        segment_results: Dict[int, str] = {}
        any_success = False
        any_failure = False
        for idx, resv_begin_time, resv_end_time, _ in due:
            seg_label = f"第{idx}段 {resv_begin_time[-8:-3]}-{resv_end_time[-8:-3]}"
            ok, line = _reserve_one_segment(
                library, pid, seat_ids, resv_begin_time, resv_end_time, seg_label
            )
            any_success = any_success or ok
            any_failure = any_failure or not ok
            segment_results[idx] = line

        # 占位下单排在窗口内的段之后：7:00 真正被人抢的是那几段，
        # 不能为了占位让它们多等一个来回；占位那张这会儿还没人跟你抢。
        placed_hold = False
        for idx, resv_begin_time, resv_end_time, open_at in queued:
            seg_label = f"第{idx}段 {resv_begin_time[-8:-3]}-{resv_end_time[-8:-3]}"
            hold = None
            if idx in hold_plans and library is not None:
                hold_begin, hold_end = hold_plans[idx]
                hold = _place_hold(library, pid, seat_ids, hold_begin, hold_end, seg_label)
                placed_hold = placed_hold or hold is not None
            queue_pending_segment(pid, resv_begin_time, resv_end_time, open_at, hold=hold)
            segment_results[idx] = _pending_result_line(seg_label, open_at, hold)
            log_with_user(logger, 'info', pid, '预约排队',
                          f"{seg_label} 超出提前预约窗口，"
                          f"{'已占位 ' + str(hold.get('dev_name')) + '，' if hold else ''}"
                          f"已排到 {open_at:%Y-%m-%d %H:%M} 换约")

        combined = "\n".join(segment_results[idx] for idx in sorted(segment_results))
        update_user_config(pid, combined)

        # 占位预约也要进 owned_seat：迟到保护和面板都照着它看，
        # 不刷一次的话，用户手上明明占着座却在系统里查无此约。
        if (any_success or placed_hold) and library is not None:
            try:
                reservations, message = library.get_reservation_info()
                if reservations:
                    log_with_user(logger, 'info', pid, '预约状态', f"当前预约状态: {message}")
                    for res in reservations:
                        log_with_user(logger, 'info', pid, '预约详情',
                                    f"座位 {res.get('devInfo', {}).get('devName', '未知')} "
                                    f"时间 {res.get('resvBeginTime')} - {res.get('resvEndTime')} "
                                    f"状态 {res.get('resvStatus')}")
            except Exception:
                pass

        if any_success:
            notify_user(res_item, "✅ 预约完成" if len(segments) == 1 else f"✅ 多段预约 ({len(segments)}段)",
                        f"学号 {pid}\n{combined}")
        elif queued and not any_failure:
            # 一段都没约，但也没失败——全都还没到窗口，等补约任务接手就行，不算异常。
            notify_user(res_item,
                        f"⏳ 已占位待换约 ({len(queued)}段)" if placed_hold
                        else f"⏳ 预约已排队 ({len(queued)}段)",
                        f"学号 {pid}\n{combined}")
        else:
            notify_user(res_item, "❌ 预约失败", f"学号 {pid}\n{combined}", always=True)

    except Exception as e:
        error_msg = f"预约过程发生异常: {str(e)}"
        log_with_user(logger, 'error', pid, '预约异常', error_msg)
        handle_reservation_error(pid, error_msg)

def _run_reservation_safely(res_item: Dict[str, Any]) -> None:
    """线程入口：包住 reservation()，任何逃逸异常都不能拖垮整个线程池。"""
    pid = res_item.get("pid", "?")
    try:
        reservation(res_item)
    except Exception as exc:
        log_with_user(logger, 'error', pid, '预约异常', f"预约线程异常: {exc}")



def _prelogin_one(res_item: Dict[str, Any]) -> None:
    """
    给单个账号提前建好登录会话。

    失败只写日志、不通知用户——预登录是纯粹的加速手段，失败的后果仅仅是
    这个账号 7:00 时退回现场登录，跟没有这个功能时一模一样。
    """
    pid = res_item["pid"]
    try:
        vpn_password = _dec(res_item["vpn_password"])
        library = LibrarySystem(
            username=pid,
            password=vpn_password,
            vpn_password=vpn_password
        )
        prelogin.store(pid, library)
        log_with_user(logger, 'info', pid, '预登录', "预登录成功，会话已就绪")
    except Exception as exc:
        prelogin.discard(pid)
        log_with_user(logger, 'warning', pid, '预登录',
                      f"预登录失败，抢座时将回退到现场登录: {exc}")


def _collect_dead_sessions(active_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    并发校验池中会话，返回需要重新登录的账号。

    必须并发：单次校验最坏要等满超时，串行校验 N 个账号可能一路拖到 7:00
    之后，反而把抢座窗口吃掉。
    """
    workers = max(1, min(RESERVE_CONCURRENCY, len(active_list)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prelogin-check") as pool:
        alive = list(pool.map(lambda item: prelogin.is_alive(item['pid']), active_list))
    return [item for item, ok in zip(active_list, alive) if not ok]


def process_prelogin(refresh: bool = False) -> None:
    """
    提前完成登录，把 webvpn + CAS 的 4~8 秒挪出抢座窗口。

    图书馆 7:00 才开放预约，但认证接口全天可用，所以登录可以提前跑。
    真正的 reserve 请求仍然等到 7:00 才发，这条没有变。

    分两次执行：
    - 抢座前 10 分钟：全量预登录
    - 抢座前 30 秒：只复查，把已经失效的会话重登一遍（这时候重登还来得及）

    Args:
        refresh: True 表示复查模式，只处理会话已失效的账号
    """
    if not prelogin.ENABLED:
        return

    stage = '预登录复查' if refresh else '预登录'
    try:
        active_list = get_all_active_reservations()
    except Exception as exc:
        log_with_user(logger, 'warning', '系统', stage, f"读取预约列表失败，跳过预登录: {exc}")
        return

    if not active_list:
        log_with_user(logger, 'info', '系统', stage, "没有正在预约中的记录，跳过预登录")
        return

    if refresh:
        pending = _collect_dead_sessions(active_list)
        if not pending:
            log_with_user(logger, 'info', '系统', stage,
                          f"{len(active_list)} 个预登录会话全部有效")
            return
        log_with_user(logger, 'info', '系统', stage,
                      f"{len(pending)}/{len(active_list)} 个会话已失效，重新登录")
    else:
        # 全量预登录前先清干净，避免上一轮的残留会话被当成新鲜的。
        prelogin.clear()
        pending = active_list
        log_with_user(logger, 'info', '系统', stage, f"开始预登录，共 {len(pending)} 个账号")

    started = time.time()
    workers = max(1, min(RESERVE_CONCURRENCY, len(pending)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prelogin") as pool:
        # _prelogin_one 自己吞掉所有异常，这里不会有 future 抛出来。
        list(pool.map(_prelogin_one, pending))

    elapsed = time.time() - started
    log_with_user(logger, 'info', '系统', stage,
                  f"预登录结束，{len(prelogin.pooled_pids())}/{len(active_list)} 个会话就绪，"
                  f"耗时 {elapsed:.1f} 秒")


def process_reservations() -> None:
    """
    并发处理所有预约请求

    工作流程：
    1. 获取所有活动预约记录（已按优先级降序排列）
    2. 用线程池并发执行，每个账号一条独立的 VPN + 图书馆会话
    3. 记录处理结果

    为什么要并发：单个账号跑完「VPN 登录 → CAS SSO → 逐段抢座」通常要好几秒，
    串行执行会让排在后面的用户错过 7:00 开抢的黄金窗口。线程池按提交顺序取任务，
    所以高优先级账号仍然先出发，只是不再需要等前一个人跑完。

    并发度由 RESERVE_CONCURRENCY 控制，别调太高——webvpn 网关和图书馆接口都有限流。
    每个账号仍然互相独立，一个失败不影响其他账号。
    """
    active_list = get_all_active_reservations()
    if not active_list:
        log_with_user(logger, 'info', '系统', '预约处理', "没有正在预约中的记录")
        return

    workers = max(1, min(RESERVE_CONCURRENCY, len(active_list)))
    log_with_user(logger, 'info', '系统', '预约处理',
                  f"开始处理预约列表，共 {len(active_list)} 条，并发度 {workers}")

    started = time.time()
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reserve") as pool:
            futures = {}
            for item in active_list:
                log_with_user(logger, 'info', item['pid'], '预约处理',
                              f"排入预约队列: {item['pid']}, 优先级: {item.get('priority', 0)}")
                futures[pool.submit(_run_reservation_safely, item)] = item['pid']
            for future in as_completed(futures):
                pid = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    log_with_user(logger, 'error', pid, '预约异常', f"预约任务未正常结束: {exc}")
    finally:
        # 没被取走的预登录会话到这里就作废了，留着只会被后面的午休/到馆复查
        # 任务当成有效会话误用。
        prelogin.clear()

    elapsed = time.time() - started
    log_with_user(logger, 'info', '系统', '预约处理', f"预约处理结束，耗时 {elapsed:.1f} 秒")


def _mark_seat_by_protection(pid: str, dev_name: str, target_date: str) -> None:
    """
    将指定用户/座位/日期的 owned_seat 条目标记为 by_protection=True，
    防止保护后重新预约的位置再次触发级联迟到保护。

    必须按日期过滤：同一座位名下往往还躺着明后天的预约（07:00 定时任务提前约的），
    整批打标会让那些预约在它们自己那天被 register_protection_jobs 当成
    “保护生成的预约”跳过，等于第二天迟到保护静默失效。
    """
    try:
        fresh_data = user_config_info.find_one({"pid": pid}, {"owned_seat": 1})
        if not fresh_data:
            return
        owned = fresh_data.get("owned_seat") or {}
        seats = owned.get(dev_name)
        if not seats:
            return
        updated = [
            {**s, "by_protection": True}
            if str(s.get("target_time", ""))[:10] == target_date else s
            for s in seats
        ]
        if updated == seats:
            log_with_user(logger, 'warning', pid, '迟到保护',
                         f"未找到 {dev_name} 在 {target_date} 的新预约，未打 by_protection 标记")
            return
        user_config_info.update_one(
            {"pid": pid},
            {"$set": {f"owned_seat.{dev_name}": updated}}
        )
        log_with_user(logger, 'info', pid, '迟到保护',
                     f"已标记 {dev_name} 在 {target_date} 的新预约为 by_protection，防止级联触发")
    except Exception as e:
        log_with_user(logger, 'warning', pid, '迟到保护', f"标记 by_protection 失败: {str(e)}")


def _record_visit_log(pid: str, uuid: str, target_time_str: str, seat_name: str) -> None:
    """记录一次道馆（按 uuid upsert，防重复写入）"""
    try:
        date_str, time_range = target_time_str.split(' ')
        begin_str, end_str = time_range.split('-', 1)
        begin_dt = datetime.strptime(f"{date_str} {begin_str}", "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M:%S")
        duration_minutes = int((end_dt - begin_dt).total_seconds() / 60)
        device = db.devices.find_one({"devName": seat_name}, {"_id": 0, "location": 1})
        location = device.get("location", "") if device else ""
        now = datetime.now()
        db.visit_logs.update_one(
            {"uuid": uuid},
            {
                "$set": {
                    "pid": pid,
                    "seat_name": seat_name,
                    "location": location,
                    "planned_begin": begin_dt,
                    "planned_end": end_dt,
                    "planned_duration_minutes": duration_minutes,
                },
                "$setOnInsert": {
                    "uuid": uuid,
                    "checkin_detected_at": now,
                    "created_at": now,
                },
            },
            upsert=True
        )
        # 服务端状态已确认到馆时，同步主页的“已到馆”状态。
        if date_str == now.strftime("%Y-%m-%d"):
            user_config_info.update_one(
                {"pid": pid}, {"$set": {"arrived_date": date_str}}
            )
        log_with_user(logger, 'info', pid, '道馆统计',
                      f"记录道馆 {seat_name} {date_str} 共{duration_minutes}分钟")
    except Exception as e:
        log_with_user(logger, 'warning', pid, '道馆统计', f"记录道馆失败: {str(e)}")


def _reservation_target_time(reservation: Dict[str, Any], fallback: str) -> str:
    """优先使用图书馆实时返回的时段，缺失时回退到任务快照。"""
    begin_time = reservation.get("resvBeginTime", "")
    end_time = reservation.get("resvEndTime", "")
    if begin_time and end_time:
        return f"{begin_time}-{end_time[-8:]}"
    return fallback


def _finish_arrival_check(
    uuid: str,
    status: str,
    message: str,
    checked_at: Optional[datetime] = None,
) -> None:
    db.arrival_checks.update_one(
        {"uuid": uuid},
        {
            "$set": {
                "status": status,
                "message": message,
                "checked_at": checked_at or datetime.now(),
            },
            "$unset": {"next_attempt_at": ""},
        },
    )


def check_arrival_after_grace(
    check: Dict[str, Any], now: Optional[datetime] = None
) -> None:
    """在预约生效 32 分钟后核验到馆状态，并同步学习记录。"""
    now = now or datetime.now()
    pid = str(check.get("pid") or "")
    uuid = str(check.get("uuid") or "")
    seat_name = str(check.get("seat_name") or "")
    target_time = str(check.get("target_time") or "")
    if not pid or not uuid or not target_time:
        _finish_arrival_check(uuid, "failed", "复查任务数据不完整", now)
        return

    user = user_config_info.find_one(
        {"pid": pid}, {"vpn_password": 1, "verified": 1}
    )
    if not user or not user.get("vpn_password"):
        db.visit_logs.delete_one({"uuid": uuid})
        _finish_arrival_check(uuid, "failed", "用户配置或凭据不存在", now)
        log_with_user(logger, 'warning', pid, '到馆复查', "用户配置或凭据不存在")
        return

    try:
        library = LibrarySystem(
            username=pid,
            password=_dec(user["vpn_password"]),
            vpn_password=_dec(user["vpn_password"]),
        )
        reservations, message = library.get_reservation_info()
        if reservations is None:
            raise RuntimeError(message or "图书馆预约查询失败")

        matched = next(
            (reservation for reservation in reservations
             if str(reservation.get("uuid") or "") == uuid),
            None,
        )
        try:
            status_code = int(matched.get("resvStatus")) if matched else None
        except (TypeError, ValueError):
            status_code = None

        if matched and status_code in IN_LIBRARY_STATUSES:
            actual_seat = (
                (matched.get("devInfo") or {}).get("devName") or seat_name
            )
            _record_visit_log(
                pid,
                uuid,
                _reservation_target_time(matched, target_time),
                actual_seat,
            )
            result_message = f"已到馆（状态码: {status_code}），学习时间已同步"
            _finish_arrival_check(uuid, "arrived", result_message, now)
            log_with_user(logger, 'info', pid, '到馆复查', result_message)
            return

        # 32 分钟时仍未签到，或记录已被图书馆服务器释放，均不得计入学习时间。
        db.visit_logs.delete_one({"uuid": uuid})
        if matched:
            result_message = f"未到馆（状态码: {status_code}），已违约，不计学习时间"
        else:
            result_message = "预约记录已由图书馆服务器释放，已违约，不计学习时间"
        _finish_arrival_check(uuid, "violated", result_message, now)
        log_with_user(logger, 'warning', pid, '到馆复查', result_message)
    except Exception as exc:
        # 网络或上游临时故障不应被误判为违约；5 分钟后继续重试。
        db.arrival_checks.update_one(
            {"uuid": uuid, "status": "pending"},
            {
                "$inc": {"attempts": 1},
                "$set": {
                    "next_attempt_at": now + timedelta(minutes=5),
                    "message": f"复查失败，稍后重试: {exc}",
                    "updated_at": now,
                },
            },
        )
        log_with_user(logger, 'warning', pid, '到馆复查',
                      f"查询失败，5分钟后重试: {exc}")


def _backfill_arrival_checks(now: datetime) -> None:
    """为部署前已存在、但尚无复查任务的预约补建任务。"""
    users = user_config_info.find(
        {"owned_seat": {"$exists": True, "$ne": {}}},
        {"pid": 1, "owned_seat": 1},
    )
    earliest_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for user in users:
        pid = str(user.get("pid") or "")
        for seat_name, seats in (user.get("owned_seat") or {}).items():
            for seat in seats:
                uuid = str(seat.get("uuid") or "")
                target_time = str(seat.get("target_time") or "")
                if not pid or not uuid or target_time[:10] < earliest_date:
                    continue
                try:
                    begin_time = datetime.strptime(
                        target_time[:19], "%Y-%m-%d %H:%M:%S"
                    )
                except (TypeError, ValueError):
                    continue
                db.arrival_checks.update_one(
                    {"uuid": uuid},
                    {"$setOnInsert": {
                        "pid": pid,
                        "uuid": uuid,
                        "seat_name": seat_name,
                        "target_time": target_time,
                        "check_at": begin_time + timedelta(minutes=32),
                        "status": "pending",
                        "attempts": 0,
                        "created_at": now,
                        "updated_at": now,
                    }},
                    upsert=True,
                )


def process_due_arrival_checks(now: Optional[datetime] = None) -> None:
    """处理所有到达预约生效后 32 分钟的持久化复查任务。"""
    global _arrival_checks_backfilled
    now = now or datetime.now()
    if not _arrival_checks_backfilled:
        try:
            _backfill_arrival_checks(now)
            _arrival_checks_backfilled = True
        except Exception as exc:
            # 补建失败不能阻断已经持久化的到期任务；下分钟会再次尝试。
            log_with_user(logger, 'warning', '系统', '到馆复查',
                          f"补建历史复查任务失败: {exc}")
    due_checks = list(db.arrival_checks.find({
        "status": "pending",
        "check_at": {"$lte": now},
        "$or": [
            {"next_attempt_at": {"$exists": False}},
            {"next_attempt_at": {"$lte": now}},
        ],
    }).sort("check_at", 1).limit(100))
    if due_checks:
        log_with_user(logger, 'info', '系统', '到馆复查',
                      f"发现 {len(due_checks)} 条到期任务")
    for check in due_checks:
        check_arrival_after_grace(check, now=now)


def late_protect_action(user: Dict[str, Any], dev_name: str, seat_dict: Dict[str, Any]) -> None:
    """
    执行迟到保护动作

    迟到保护流程：
    0. 检查"我已到馆"标志 / 用户是否已在馆（已签到则跳过）
    1. 取消原预约
    2. 根据 protection_max_minutes 决定行为：
       - 0 / 黑名单：仅取消，不重新预约
       - 正数 N：延后 N 分钟后重新预约，并标记 by_protection 防止级联
       - -1（永久）：延后60分钟重新预约，允许继续保护
    3. 重新预约座位（最多重试3次）
    4. 累计触发计数 late_protection_count
    """
    pid = user["pid"]
    try:
        # 实时读取最新用户配置，避免缓存
        today_str = datetime.now().strftime("%Y-%m-%d")
        fresh = user_config_info.find_one({"pid": pid}, {
            "arrived_date": 1,
            "protection_max_minutes": 1,
            "late_protection_blacklisted": 1
        })

        # 优先检查"我已到馆"手动标志（当天有效）
        if fresh and fresh.get("arrived_date") == today_str:
            log_with_user(logger, 'info', pid, '迟到保护', "用户已标记到馆，跳过迟到保护")
            return

        # 读取保护配置
        blacklisted = bool(fresh.get("late_protection_blacklisted")) if fresh else False
        protection_minutes = fresh.get("protection_max_minutes", 60) if fresh else 60
        if protection_minutes is None:
            protection_minutes = 60

        log_with_user(logger, 'info', pid, '迟到保护',
                     f"开始处理座位 {dev_name} 的迟到保护（配置: {protection_minutes}min, 黑名单: {blacklisted}）")

        # 闭馆判断必须早于登录和取消。即使配置为“仅取消”，闭馆日也不应让
        # 自动保护流程改动已有预约；用户仍可通过单纯取消接口自行处理。
        target_time = seat_dict['target_time']
        date_str, time_range = target_time.split(' ')
        begin_time_str, end_time_str = time_range.split('-')
        begin_time = datetime.strptime(f"{date_str} {begin_time_str}", "%Y-%m-%d %H:%M:%S")
        end_time = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M:%S")
        shift_minutes = 60 if protection_minutes == -1 else max(0, protection_minutes)
        new_begin = begin_time + timedelta(minutes=shift_minutes)
        new_end = max(end_time, new_begin + timedelta(hours=2))
        new_begin_str = f"{date_str} {new_begin.strftime('%H:%M:%S')}"
        new_end_str = f"{date_str} {new_end.strftime('%H:%M:%S')}"
        conflict = find_reservation_conflict(
            db.school_notice_reviews,
            new_begin_str if not (blacklisted or protection_minutes == 0) else begin_time,
            new_end_str if not (blacklisted or protection_minutes == 0) else end_time,
        )
        if conflict:
            log_with_user(logger, 'info', pid, '闭馆保护', _blackout_message(conflict))
            return

        library = LibrarySystem(
            username=user["pid"],
            password=_dec(user["vpn_password"]),
            vpn_password=_dec(user["vpn_password"])
        )

        # 检查这条预约的实时状态，决定要不要保护。
        # 只有 1027（待签到）才继续往下取消重约；其余一律保守跳过：
        # 1093/3141 说明人已经进馆了；1169/1217/3265 之类是已违约/已取消/已结束，
        # 拿这种陈旧 uuid 去 delete_seat 只会换回「预约在当前状态下不能删除」。
        # uuid 在实时列表里找不到同理——那条预约已经不存在了，不能替它做决定。
        try:
            res_list, _ = library.get_reservation_info()
            if res_list is None:
                log_with_user(logger, 'warning', pid, '迟到保护',
                    "查询实时预约失败，保守跳过本次保护")
                return

            matched = next(
                (r for r in (res_list or []) if r.get('uuid') == seat_dict['uuid']),
                None,
            )
            if matched is None:
                log_with_user(logger, 'warning', pid, '迟到保护',
                    f"实时预约列表中找不到 uuid {seat_dict['uuid']}（{dev_name} "
                    f"{seat_dict['target_time']}），可能已取消或过期，保守跳过本次保护")
                return

            current_status = _as_status_code(matched.get('resvStatus'))
            if current_status in IN_LIBRARY_STATUSES:
                log_with_user(logger, 'info', pid, '迟到保护',
                    f"用户已在馆内（状态码: {current_status}），跳过迟到保护")
                _record_visit_log(pid, seat_dict['uuid'], seat_dict['target_time'], dev_name)
                return
            if current_status != PENDING_CHECKIN_STATUS:
                log_with_user(logger, 'warning', pid, '迟到保护',
                    f"预约状态为 {current_status}，非待签到，保守跳过本次保护")
                return
            log_with_user(logger, 'info', pid, '迟到保护',
                f"用户未签到（状态码: {current_status}），继续执行迟到保护")
        except Exception as e:
            log_with_user(logger, 'warning', pid, '迟到保护',
                f"检查签到状态失败，继续执行迟到保护: {str(e)}")

        # 取消原预约
        try:
            success, message = library.delete_seat(seat_dict["uuid"])
            if not success:
                log_with_user(logger, 'error', pid, '迟到保护', f"取消原预约失败: {message}")
                return
            log_with_user(logger, 'info', pid, '迟到保护', f"成功取消原预约: {seat_dict['uuid']}")
        except Exception as e:
            log_with_user(logger, 'error', pid, '迟到保护', f"取消原预约异常: {str(e)}")
            return

        # 累计触发计数
        user_config_info.update_one({"pid": pid}, {"$inc": {"late_protection_count": 1}})

        # 黑名单或保护时长为0：仅取消，不重新预约
        if blacklisted or protection_minutes == 0:
            log_with_user(logger, 'info', pid, '迟到保护',
                         "已列入黑名单或保护时长为0，预约已取消，不重新预约")
            return

        log_with_user(logger, 'info', pid, '迟到保护',
                     f"调整后的预约时间: {new_begin_str} - {new_end_str}")

        # 获取座位ID
        device = db.devices.find_one({"devName": dev_name}, {"_id": 0, "devId": 1})
        if not device:
            log_with_user(logger, 'error', pid, '迟到保护', f"未找到座位ID: {dev_name}")
            return

        seat_id = device["devId"]
        log_with_user(logger, 'info', pid, '迟到保护', f"座位 {dev_name} 的ID为: {seat_id}")

        # 重新预约（带重试机制）
        max_retries = 3
        retry_count = 0
        last_error = None

        while retry_count < max_retries:
            try:
                res_msg, _ = library.reserve_seat(
                    seat_list=[seat_id],
                    resv_begin_time=new_begin_str,
                    resv_end_time=new_end_str
                )

                if "成功" in res_msg or "预约成功" in res_msg:
                    log_with_user(logger, 'info', pid, '迟到保护',
                                f"重新预约成功 (第{retry_count + 1}次尝试): {res_msg}")
                    try:
                        library.get_reservation_info()
                        log_with_user(logger, 'info', pid, '迟到保护', "已同步最新预约信息到数据库")
                        # 非永久保护：标记新预约，防止级联触发
                        if protection_minutes != -1:
                            _mark_seat_by_protection(pid, dev_name, date_str)
                    except Exception as e:
                        log_with_user(logger, 'warning', pid, '迟到保护', f"同步预约信息失败: {str(e)}")
                    return
                else:
                    last_error = res_msg
                    log_with_user(logger, 'warning', pid, '迟到保护',
                                f"预约返回非成功状态 (第{retry_count + 1}次尝试): {res_msg}")

            except Exception as e:
                last_error = str(e)
                log_with_user(logger, 'error', pid, '迟到保护',
                            f"重新预约异常 (第{retry_count + 1}次尝试): {str(e)}")

            retry_count += 1
            if retry_count < max_retries:
                log_with_user(logger, 'info', pid, '迟到保护',
                            f"等待5秒后进行第{retry_count + 1}次重试...")
                time.sleep(5)

        log_with_user(logger, 'error', pid, '迟到保护',
                     f"重新预约失败，已重试{max_retries}次，最后一次错误: {last_error}")

    except Exception as e:
        log_with_user(logger, 'error', pid, '迟到保护', f"执行失败: {str(e)}")

def register_late_protection_jobs(scheduler) -> None:
    """
    把今天还没到点的迟到保护任务注册到传入的调度器上（在预约开始前 7 分钟触发）。

    这个函数是**幂等**的：job_id 固定 + replace_existing，重复调用只会覆盖同一批任务。
    调度器由调用方（scheduler_runner）持有并常驻，所以本函数不启动、不关闭、不阻塞。

    历史坑：早先这里自己 new 一个 BackgroundScheduler 并阻塞到 22:00，整段挂在
    07:00 的抢座任务里。结果白天任何一次重启容器，当天剩下所有人的迟到保护就
    静默消失了（07:00 那一发已经过去，不会再有人调它），日志上完全看不出异常。
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    try:
        users = list(user_config_info.find({"late_protection": "True"}))
    except Exception as e:
        log_with_user(logger, 'error', '系统', '迟到保护', f"查询用户列表异常: {str(e)}")
        return

    registered = 0
    try:
        for user in users:
            pid = user.get("pid")
            owned_seat = user.get("owned_seat", {})

            for dev_name, seat_list in owned_seat.items():
                for seat_dict in seat_list:
                    if seat_dict['target_time'][:10] != today_str:
                        continue
                    # owned_seat 是图书馆整份预约列表的镜像，已违约(1169)/已取消(1217)/
                    # 已结束(3265) 的陈旧条目都留在里面，同一座位同一天能有好几条。
                    # 只有「待签到」才谈得上迟到，其余全是僵尸——照旧注册的话，它们会
                    # 在同一时刻起一堆任务，各自拿着陈旧 uuid 去取消，然后齐刷刷失败。
                    status = _as_status_code(seat_dict.get('resvStatus'))
                    if status != PENDING_CHECKIN_STATUS:
                        log_with_user(logger, 'debug', pid, '迟到保护',
                                     f"跳过非待签到预约 {dev_name} "
                                     f"{seat_dict['target_time']}（状态码: {status}）")
                        continue
                    # 跳过已由保护机制创建的预约，防止级联触发。
                    # 日志带上时段：这是唯一一条“今天不给你保护”的记录，
                    # 不打出来的话事后完全查不到保护为什么没触发。
                    if seat_dict.get('by_protection'):
                        log_with_user(logger, 'debug', pid, '迟到保护',
                                     f"跳过已受保护的预约 {dev_name} "
                                     f"{seat_dict['target_time']}（防止级联）")
                        continue

                    begin_str = seat_dict['target_time'][:19]
                    begin_time = datetime.strptime(begin_str, "%Y-%m-%d %H:%M:%S")
                    exec_time = begin_time - timedelta(minutes=7)

                    # 只注册未来的任务
                    if exec_time > now:
                        # job_id 不带 uuid：带了的话同一时段的重复条目 uuid 不同，
                        # replace_existing 去重不掉，会并行起好几个保护任务。
                        job_id = f"{pid}_{dev_name}_{begin_str}"
                        scheduler.add_job(
                            late_protect_action,
                            'date',
                            run_date=exec_time,
                            args=[user, dev_name, seat_dict],
                            id=job_id,
                            replace_existing=True,
                            # 调度器卡几十秒不该让保护整个失效；但也不能补跑到预约
                            # 开始之后——那时进了宽限期，原预约已经删不掉了。
                            # 4 分钟的窗口保证最晚也在 begin-3min 触发。
                            misfire_grace_time=240,
                        )
                        registered += 1
                        log_with_user(logger, 'info', pid, '迟到保护',
                                     f"注册任务 用户:{pid} 座位:{dev_name} 执行时间:{exec_time.strftime('%H:%M:%S')}")
                    else:
                        log_with_user(logger, 'debug', pid, '迟到保护',
                                      f"跳过过期任务 用户:{pid} 座位:{dev_name} 原执行时间:{exec_time.strftime('%H:%M:%S')}")
    except Exception as e:
        log_with_user(logger, 'error', '系统', '迟到保护', f"注册保护任务时发生异常: {str(e)}")

    log_with_user(logger, 'info', '系统', '迟到保护',
                  f"扫描完成：{len(users)} 个用户开启保护，本轮待触发任务 {registered} 个")


def auto_nap_action(pid: str) -> None:
    """对单个用户执行自动午休：取消今日当前预约，立即重新预约下午时段。"""
    try:
        cfg = user_config_info.find_one({"pid": pid})
        if not cfg:
            log_with_user(logger, 'warning', pid, '自动午休', "未找到用户配置")
            return

        nap_cfg = cfg.get("nap_config") or {}
        nap_start = nap_cfg.get("start_time") or "14:00"
        nap_end = nap_cfg.get("end_time") or ""
        nap_seat_name = (nap_cfg.get("seat") or "").strip()

        today_str = datetime.now().strftime("%Y-%m-%d")
        preflight_end = nap_end
        if not preflight_end:
            for seats in (cfg.get("owned_seat") or {}).values():
                for seat in seats or []:
                    target_time = str(seat.get("target_time") or "")
                    if target_time.startswith(today_str) and "-" in target_time:
                        preflight_end = target_time.rsplit("-", 1)[-1][:5]
                        break
                if preflight_end:
                    break
        preflight_end = preflight_end or "23:59"
        conflict = find_reservation_conflict(
            db.school_notice_reviews,
            f"{today_str} {nap_start}:00",
            f"{today_str} {preflight_end}:00",
        )
        if conflict:
            log_with_user(logger, 'info', pid, '闭馆保护', _blackout_message(conflict))
            return

        vpn_password = _dec(cfg["vpn_password"])

        library = LibrarySystem(
            username=pid,
            password=vpn_password,
            vpn_password=vpn_password,
        )

        reservations, _ = library.get_reservation_info()
        if not reservations:
            log_with_user(logger, 'info', pid, '自动午休', "今日无预约，跳过")
            return

        active_statuses = {1027, 1093, 3141}
        target = None
        for r in reservations:
            if (r.get("resvBeginTime", "")[:10] == today_str
                    and r.get("resvStatus") in active_statuses):
                target = r
                break

        if not target:
            log_with_user(logger, 'info', pid, '自动午休', "今日无可取消的活跃预约，跳过")
            return

        uuid = target.get("uuid") or target.get("resvId", "")
        current_seat = (target.get("devInfo") or {}).get("devName", "")
        seat_name = nap_seat_name or current_seat
        if not nap_end:
            nap_end = (target.get("resvEndTime") or "")[-8:-3] or "18:00"

        conflict = find_reservation_conflict(
            db.school_notice_reviews,
            f"{today_str} {nap_start}:00",
            f"{today_str} {nap_end}:00",
        )
        if conflict:
            log_with_user(logger, 'info', pid, '闭馆保护', _blackout_message(conflict))
            return

        log_with_user(logger, 'info', pid, '自动午休', f"取消预约 {uuid}，将重约 {seat_name} {nap_start}-{nap_end}")

        cancel_ok, cancel_msg = library.delete_seat(uuid)
        if not cancel_ok:
            log_with_user(logger, 'error', pid, '自动午休', f"取消失败：{cancel_msg}")
            notify_user(cfg, "❌ 自动午休失败", f"学号 {pid}\n取消原预约失败：{cancel_msg}",
                        always=True)
            return

        time.sleep(0.5)

        seat_ids = get_seat_ids([seat_name])
        if not seat_ids:
            msg = f"取消成功，但未找到座位「{seat_name}」，请手动预约下午时段"
            log_with_user(logger, 'error', pid, '自动午休', msg)
            notify_user(cfg, "⚠ 自动午休部分失败", f"学号 {pid}\n{msg}", always=True)
            return

        resv_msg, _ = library.reserve_seat(
            seat_list=seat_ids,
            resv_begin_time=f"{today_str} {nap_start}:00",
            resv_end_time=f"{today_str} {nap_end}:00",
        )
        if "成功" in resv_msg:
            log_with_user(logger, 'info', pid, '自动午休', f"重新预约成功：{resv_msg}")
            notify_user(cfg, "✅ 自动午休完成", f"学号 {pid}\n已重新预约 {seat_name} {nap_start}-{nap_end}")
        else:
            log_with_user(logger, 'error', pid, '自动午休', f"重新预约失败：{resv_msg}")
            notify_user(cfg, "⚠ 自动午休部分失败",
                        f"学号 {pid}\n取消成功，但重新预约失败：{resv_msg}\n请手动预约下午时段",
                        always=True)

    except Exception as e:
        log_with_user(logger, 'error', pid, '自动午休', f"异常：{str(e)}")


def process_auto_naps() -> None:
    """遍历所有开启每日自动午休的用户，按其配置的触发时间调度执行。"""
    if not NAP_ENABLED:
        return
    now = datetime.now()
    users = list(user_config_info.find({"nap_config.auto_daily": True}))
    if not users:
        return
    log_with_user(logger, 'info', '系统', '自动午休', f"共 {len(users)} 个用户开启自动午休")
    for u in users:
        pid = u["pid"]
        trigger = (u.get("nap_config") or {}).get("trigger_time") or "12:00"
        try:
            h, m = map(int, trigger.split(":"))
        except Exception:
            h, m = 12, 5
        trigger_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if abs((now - trigger_dt).total_seconds()) <= 90:
            log_with_user(logger, 'info', pid, '自动午休', f"触发时间 {trigger}，开始执行")
            auto_nap_action(pid)
        else:
            log_with_user(logger, 'debug', pid, '自动午休', f"当前时间 {now.strftime('%H:%M')} 不匹配触发时间 {trigger}，跳过")

def scan_and_record_visits() -> None:
    """扫描所有用户今日预约，若检测到签到状态则记录道馆日志（每15分钟由调度器调用）"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        users = list(user_config_info.find(
            {"owned_seat": {"$exists": True, "$ne": {}}, "verified": True},
            {"pid": 1, "vpn_password": 1, "owned_seat": 1}
        ))
    except Exception as e:
        log_with_user(logger, 'error', '系统', '道馆统计', f"查询用户列表异常: {str(e)}")
        return

    for user in users:
        pid = user.get("pid", "")
        owned = user.get("owned_seat") or {}
        if not any(
            sd.get("target_time", "")[:10] == today_str
            for seats in owned.values()
            for sd in seats
        ):
            continue
        try:
            library = LibrarySystem(
                username=pid,
                password=_dec(user["vpn_password"]),
                vpn_password=_dec(user["vpn_password"])
            )
            res_list, _ = library.get_reservation_info()
            if not res_list:
                continue
            for res in res_list:
                if _as_status_code(res.get("resvStatus")) in IN_LIBRARY_STATUSES:
                    uuid = res.get("uuid", "")
                    begin_time = res.get("resvBeginTime", "")
                    end_time = res.get("resvEndTime", "")
                    dev_info = res.get("devInfo") or {}
                    seat_name = dev_info.get("devName", "")
                    if uuid and seat_name and begin_time and end_time:
                        target_time_str = f"{begin_time}-{end_time[-8:]}"
                        _record_visit_log(pid, uuid, target_time_str, seat_name)
        except Exception as e:
            log_with_user(logger, 'warning', pid, '道馆统计', f"扫描签到状态失败: {str(e)}")


if __name__ == "__main__":
    """
    主程序入口：手动跑一轮预约用。

    迟到保护不在这里启动——它是 scheduler_runner 里的常驻 job
    （register_late_protection_jobs），需要一个活着的调度器持有任务。

    异常处理：
    - 捕获所有异常并记录日志
    - 确保程序正常退出
    """
    try:
        process_reservations()
    except KeyboardInterrupt:
        log_with_user(logger, 'info', '系统', '程序中断', "程序被用户中断")
    except Exception as e:
        log_with_user(logger, 'error', '系统', '程序异常', f"程序运行出错: {str(e)}")
    finally:
        log_with_user(logger, 'info', '系统', '程序退出', "程序已退出")
