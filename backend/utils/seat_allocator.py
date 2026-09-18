# -*- coding: utf-8 -*-
"""座位竞争度建模 + 抢座队列分配。**目前只出报告，不写任何配置。**

状态：已写好，未接线。scheduler_runner 里没有它的 job，主流程也不 import 它。
      要用就手动跑 CLI 看报告。别以为它在跑。

背景
----
7:00 开闸是固定的，改不了。开闸那一秒能动的只有两件事：
  1. 谁排在第一波（RESERVE_CONCURRENCY 个名额，只有他们能在 07:00:00.2 发出请求，
     后面的人要等前面拿到响应，实测能晚 1~6 秒）——由 user_config_info.priority
     降序决定（scheduled_task.get_all_active_reservations）。
  2. 每个人把唯一那次「早期射击」打在哪张座位上——由 seat_list 的第一项决定。
     第二备选的请求要等第一发的响应回来才发得出去，2026-09-09 那天等了 5.2 秒。

现状是这两件事都没人管：所有账号的 priority 都是 0，排序退化成 updated_at 降序，
等于「谁最近在网页上改过配置谁先抢」。这个模块就是把这两件事变成有依据的决策。

算法
----
输入是 utils.seat_snapshot 攒下来的每日全馆占用快照。

  第一步 竞争度估计
      对每张座位、每个目标时段，统计历史上「抢座尘埃落定后这张座位还空着」
      的加权频率（用 post 那张快照）。权重 = 时间衰减(半衰期 HALF_LIFE_DAYS)
      × 同星期几加成。样本少的时候用**同区域的整体空闲率**作先验做 Beta 平滑，
      所以一张从没观测过的座位不会因为「没数据」就被判成必胜或必败。

      pre 那张快照单独参与：它给出「这张座位在 7:00 之前就已经没了」的概率。
      这个数高的话，排第几波、priority 给多高都没用，唯一出路是换座位——
      所以它在报告里单独占一列，不和 post 的存活率混成一个数。

  第二步 座位分配（二分图最大权匹配 / 匈牙利算法）
      左边是账号，右边是候选座位，边权 = 偏好度 × 存活概率。
      一张座位只能分给一个账号（时段重叠时），这条约束顺带根治了
      「我们自己几个账号抢同一张座位」——2026-09-09 就有三个账号把 2F-B036
      排在前两位。规模是 20 × 几百，纯 Python 的 O(n²m) 匈牙利毫秒级跑完。

  第三步 队列排序
      第一波名额是稀缺资源，给最可能抢不到的人：
          priority = 1000 × (1 - 分到的座位的存活概率) × 严重度
      严重度按「抢不到的代价」加权（开了迟到保护的、时段长的更吃亏）。
      存活概率接近 1 的冷门座位排最后——他们等到 07:00:06 照样约得到。

红线
----
禁止改动用户配置，seat_list 和 priority 都算在内。所以这里**只读**
user_config_info，结果写进独立的 seat_plans 集合，由人去看。
apply_plan() 是故意留的一颗雷，直接抛异常，防止以后手滑接上去。

已知短板（接线前必须先解决，别忘了）
------------------------------------
  * post 快照是 07:05 拍的，仍然区分不了「07:00:00.1 被秒杀」和「07:03 有人
    慢慢约走」，后者其实是我们本来抢得到的。要真正分开得靠 pre/post 做差
    （utils.seat_snapshot.rush_delta），但那个差目前只用于人看的诊断报告，
    还没喂进 survival() 的目标变量——接线前应该改成用差值当训练目标。
  * 冷门座位的「空着」有两种原因：没人要，或者那张座位有什么毛病
    （靠门、没插座、屏幕坏了）。快照分不出来。真按推荐去坐之前得实地确认。
  * 分配结果目前无法自动生效——按红线要求，只能出报告给人看。

用法
----
    docker compose exec scheduler python -m utils.seat_allocator            # 打印报告
    docker compose exec scheduler python -m utils.seat_allocator --save     # 顺便存进 seat_plans
    docker compose exec scheduler python -m utils.seat_allocator --days 30
"""

import logging
import math
import os
import sys
from datetime import date as date_cls, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

PLAN_COLLECTION = "seat_plans"

# 权重衰减：两周前的观测只算一半。开学季座位偏好变得快，别让上个月的数据主导。
HALF_LIFE_DAYS = float(os.getenv("SEAT_MODEL_HALF_LIFE_DAYS", "14"))
# 同一星期几的观测更有参考价值（周末和工作日的占用完全是两回事）。
WEEKDAY_BONUS = float(os.getenv("SEAT_MODEL_WEEKDAY_BONUS", "2.0"))
# 先验强度：相当于「凭区域整体空闲率白送这么多次观测」。样本少时靠它兜底。
# 攒满一个月时加权样本量约 14.7，取 2 意味着先验占约 12%——够压住噪声，
# 又不至于在刚起步的头几天把「连续三天被占满」这种明确信号也抹平。
PRIOR_STRENGTH = float(os.getenv("SEAT_MODEL_PRIOR_STRENGTH", "2.0"))

# 每个账号除自己配置的座位外，再从同区域挑这么多张高存活率的座位当候选。
NEIGHBOR_CANDIDATES = int(os.getenv("SEAT_ALLOC_NEIGHBOR_CANDIDATES", "12"))
# 换到别人的座位终归是打扰，同区域邻座的偏好打个折。
NEIGHBOR_PREFERENCE = float(os.getenv("SEAT_ALLOC_NEIGHBOR_PREFERENCE", "0.6"))
# seat_list 里越靠后的越不受待见，每往后一位扣一点。
OWN_SEAT_DECAY = 0.05

# 第一波能容纳多少人，和抢座任务用的是同一个环境变量。
WAVE_SIZE = int(os.getenv("RESERVE_CONCURRENCY", "8"))


# --------------------------------------------------------------------------
# 竞争度模型
# --------------------------------------------------------------------------

def _overlaps(intervals: Sequence[Sequence[int]], begin: int, end: int) -> bool:
    for interval in intervals or ():
        if len(interval) >= 2 and interval[0] < end and begin < interval[1]:
            return True
    return False


def _free_for_windows(intervals: Sequence[Sequence[int]],
                      windows: Sequence[Tuple[int, int]]) -> bool:
    """一个账号要的是它**所有**时段都空着，缺一段这张座位就没用。"""
    return all(not _overlaps(intervals, begin, end) for begin, end in windows)


class ContentionModel:
    """按历史快照估「某张座位在某个时段还空着」的概率。

    只吃 seat_snapshot 存下来的文档结构，不碰数据库，方便单测直接喂假数据。
    """

    def __init__(
        self,
        snapshots: Sequence[Dict[str, Any]],
        reference_date: Optional[date_cls] = None,
        half_life_days: float = HALF_LIFE_DAYS,
        weekday_bonus: float = WEEKDAY_BONUS,
        prior_strength: float = PRIOR_STRENGTH,
    ) -> None:
        self.half_life_days = max(0.5, half_life_days)
        self.weekday_bonus = max(1.0, weekday_bonus)
        self.prior_strength = max(0.0, prior_strength)
        self.reference_date = reference_date or datetime.now().date()

        # (date, room_id) → {seat: intervals}
        self.days: Dict[Tuple[str, str], Dict[str, List[List[int]]]] = {}
        self.day_weekday: Dict[str, int] = {}
        self.seat_room: Dict[str, str] = {}
        self.room_seats: Dict[str, List[str]] = {}
        self.room_name: Dict[str, str] = {}
        self.unavailable: Dict[str, set] = {}

        for doc in snapshots:
            day = doc.get("date")
            room = doc.get("room_id")
            seats = doc.get("seats") or {}
            if not day or not room:
                continue
            # 同一天同一区域若有多个 tag，后读到的覆盖前面的——调用方想只用某个
            # tag 的话，在 load_snapshots 那层就该过滤掉。
            self.days[(day, room)] = seats
            self.day_weekday[day] = int(doc.get("weekday") or 0)
            self.room_name[room] = f"{doc.get('floor_name', '')}{doc.get('room_name', '')}"
            known = self.room_seats.setdefault(room, [])
            for seat in seats:
                if seat not in self.seat_room:
                    self.seat_room[seat] = room
                    known.append(seat)
            for seat in doc.get("unavailable") or []:
                self.unavailable.setdefault(room, set()).add(seat)

        for seats in self.room_seats.values():
            seats.sort()
        self.dates: List[str] = sorted({day for day, _ in self.days})

    # -- 权重 ---------------------------------------------------------------

    def _weight(self, day: str, weekday: Optional[int]) -> float:
        try:
            observed = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return 0.0
        age = (self.reference_date - observed).days
        if age < 0:
            return 0.0
        weight = math.pow(0.5, age / self.half_life_days)
        if weekday and self.day_weekday.get(day) == weekday:
            weight *= self.weekday_bonus
        return weight

    # -- 估计 ---------------------------------------------------------------

    def room_survival(
        self,
        room_id: str,
        windows: Sequence[Tuple[int, int]],
        weekday: Optional[int] = None,
    ) -> float:
        """整个区域在该时段的平均空闲率，当作单张座位的先验。"""
        free_weight = 0.0
        total_weight = 0.0
        for day in self.dates:
            seats = self.days.get((day, room_id))
            if not seats:
                continue
            weight = self._weight(day, weekday)
            if weight <= 0:
                continue
            for intervals in seats.values():
                total_weight += weight
                if _free_for_windows(intervals, windows):
                    free_weight += weight
        # 拉普拉斯平滑：一个区域一条记录都没有时给 0.5，不偏向任何一边。
        return (free_weight + 1.0) / (total_weight + 2.0)

    def survival(
        self,
        seat: str,
        windows: Sequence[Tuple[int, int]],
        weekday: Optional[int] = None,
    ) -> float:
        """这张座位在所有给定时段都空着的概率估计。"""
        room_id = self.seat_room.get(seat)
        if room_id is None:
            # 没观测过的座位（快照还没覆盖到）：保守地给个中间值。
            return 0.5
        prior = self.room_survival(room_id, windows, weekday)

        free_weight = 0.0
        total_weight = 0.0
        for day in self.dates:
            seats = self.days.get((day, room_id))
            if not seats or seat not in seats:
                continue
            weight = self._weight(day, weekday)
            if weight <= 0:
                continue
            total_weight += weight
            if _free_for_windows(seats[seat], windows):
                free_weight += weight

        alpha = self.prior_strength
        return (free_weight + alpha * prior) / (total_weight + alpha)

    def sample_size(self, seat: str) -> int:
        """这张座位实际被观测过多少天——报告里要标出来，样本少的结论别当真。"""
        room_id = self.seat_room.get(seat)
        if room_id is None:
            return 0
        return sum(1 for day in self.dates
                   if seat in (self.days.get((day, room_id)) or {}))

    def is_unavailable(self, seat: str) -> bool:
        room_id = self.seat_room.get(seat)
        return bool(room_id and seat in self.unavailable.get(room_id, set()))


# --------------------------------------------------------------------------
# 匈牙利算法（矩形，最小化总代价）
# --------------------------------------------------------------------------

def hungarian(cost: Sequence[Sequence[float]]) -> List[int]:
    """O(n²m) 的 JV 版匈牙利，返回每一行匹配到的列下标（-1 表示没匹配上）。

    要求行数 ≤ 列数；账号数远小于座位数，这个前提天然成立。
    自己实现是为了不给这个项目引入 scipy —— 它现在一个数值库都不依赖。
    """
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    if m < n:
        raise ValueError(f"列数 {m} 少于行数 {n}，无法完成指派")

    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    match = [0] * (m + 1)   # 列 → 行（1-based，0 表示空）
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        match[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = match[j0]
            delta = inf
            j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            if j1 == 0:
                raise ValueError("指派失败：代价矩阵里存在无穷大的整行")
            for j in range(m + 1):
                if used[j]:
                    u[match[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if match[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            match[j0] = match[j1]
            j0 = j1

    result = [-1] * n
    for j in range(1, m + 1):
        if match[j]:
            result[match[j] - 1] = j - 1
    return result


# --------------------------------------------------------------------------
# 分配
# --------------------------------------------------------------------------

def _candidates(
    model: ContentionModel,
    own_seats: Sequence[str],
    windows: Sequence[Tuple[int, int]],
    weekday: int,
) -> Dict[str, float]:
    """这个账号能接受的座位 → 偏好度（0~1）。"""
    prefs: Dict[str, float] = {}
    for index, seat in enumerate(own_seats):
        prefs[seat] = max(0.1, 1.0 - OWN_SEAT_DECAY * index)

    # 再从「第一志愿所在区域」补一批高存活率的邻座当备胎。
    anchor_room = model.seat_room.get(own_seats[0]) if own_seats else None
    if anchor_room:
        pool = [
            seat for seat in model.room_seats.get(anchor_room, [])
            if seat not in prefs and not model.is_unavailable(seat)
        ]
        pool.sort(key=lambda s: model.survival(s, windows, weekday), reverse=True)
        for seat in pool[:NEIGHBOR_CANDIDATES]:
            prefs[seat] = NEIGHBOR_PREFERENCE
    return prefs


def severity(account: Dict[str, Any]) -> float:
    """抢不到的代价：开了迟到保护的、要坐一整天的，更输不起。"""
    hours = sum(end - begin for begin, end in account["windows"]) / 60.0
    score = 1.0
    if account.get("late_protection"):
        score += 0.5
    score += 0.3 * min(1.0, hours / 14.0)
    return score


def allocate(
    accounts: Sequence[Dict[str, Any]],
    model: ContentionModel,
    weekday: int,
    wave_size: int = WAVE_SIZE,
    pre_model: Optional[ContentionModel] = None,
) -> List[Dict[str, Any]]:
    """给每个账号定一张座位和一个 priority，返回报告行。

    accounts 每项需要: pid, windows [(begin_min, end_min)], seat_list,
                       late_protection, priority(现值)
    """
    accounts = [a for a in accounts if a.get("windows") and a.get("seat_list")]
    if not accounts:
        return []

    prefs = [
        _candidates(model, account["seat_list"], account["windows"], weekday)
        for account in accounts
    ]
    columns = sorted({seat for pref in prefs for seat in pref})
    if len(columns) < len(accounts):
        # 候选池比账号还少（历史数据太薄）时补足虚拟列，让指派仍能完成；
        # 落到虚拟列上的账号会被标成「无可分配座位」。
        columns += [f"__dummy_{i}" for i in range(len(accounts) - len(columns))]

    col_index = {seat: i for i, seat in enumerate(columns)}
    survival_cache: Dict[Tuple[int, str], float] = {}
    big = 1e6
    cost: List[List[float]] = []
    for row, (account, pref) in enumerate(zip(accounts, prefs)):
        line = [big] * len(columns)
        for seat, weight in pref.items():
            p = model.survival(seat, account["windows"], weekday)
            survival_cache[(row, seat)] = p
            # 最大化 偏好×存活 等价于最小化其相反数。
            line[col_index[seat]] = -weight * p
        cost.append(line)

    assignment = hungarian(cost)

    rows: List[Dict[str, Any]] = []
    for row, account in enumerate(accounts):
        col = assignment[row]
        seat = columns[col] if 0 <= col < len(columns) else None
        if seat is not None and (seat.startswith("__dummy_") or cost[row][col] >= big):
            seat = None
        p = survival_cache.get((row, seat), 0.0) if seat else 0.0

        own = [
            {
                "seat": s,
                "p": round(model.survival(s, account["windows"], weekday), 3),
                "n": model.sample_size(s),
            }
            for s in account["seat_list"]
        ]
        best_own = max((o["p"] for o in own), default=0.0)

        # 抢座前那张快照里，这几张座位还剩多少活路。这个数低说明座位在 7:00
        # 之前就已经没了——那不管排第几波、priority 给多高都抢不到，
        # 唯一的出路是换座位。把它和 best_own_p 分开列，别混成一个数。
        pre_p = None
        if pre_model is not None:
            pre_p = round(max(
                (pre_model.survival(s, account["windows"], weekday)
                 for s in account["seat_list"]),
                default=0.0,
            ), 3)

        rows.append({
            "pid": account["pid"],
            "windows": [f"{b // 60:02d}:{b % 60:02d}-{e // 60:02d}:{e % 60:02d}"
                        for b, e in account["windows"]],
            "own_seats": own,
            "best_own_p": round(best_own, 3),
            "best_own_p_before_rush": pre_p,
            "assigned_seat": seat,
            "assigned_p": round(p, 3),
            "assigned_is_own": bool(seat and seat in account["seat_list"]),
            "late_protection": bool(account.get("late_protection")),
            "current_priority": account.get("priority", 0),
            # 风险按**账号自己配置的**座位算，不能按 assigned_seat 算：座位配置
            # 改不了，7:00 真正会被发出去的还是 seat_list 里那几张。拿建议座位
            # 算风险，等于假装人已经换了座位，热门座位账号会被排到最后一波去。
            #
            # 用 1-max(p) 而不是 ∏(1-p)：同一区域的座位是一起被抢光的，
            # 各张座位的成败强相关，连乘会把风险算得离谱地低。
            "_score": (1.0 - best_own) * severity(account),
        })

    # 风险高的排前面，进第一波。
    rows.sort(key=lambda r: r["_score"], reverse=True)
    for index, row in enumerate(rows):
        row["suggested_priority"] = int(round(1000 * row.pop("_score")))
        row["wave"] = index // max(1, wave_size) + 1
        row["queue_position"] = index + 1
    return rows


# --------------------------------------------------------------------------
# 数据加载 / 报告
# --------------------------------------------------------------------------

def _windows_for(cfg: Dict[str, Any], target: date_cls) -> List[Tuple[int, int]]:
    """复用抢座主流程的时段计算，别在这里重写一份「哪天休息」的规则。"""
    from scheduled_task import calculate_reservation_time

    windows: List[Tuple[int, int]] = []
    for begin, end in calculate_reservation_time(cfg) or []:
        try:
            b = datetime.strptime(begin, "%Y-%m-%d %H:%M:%S")
            e = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if b.date() != target:
            continue
        windows.append((b.hour * 60 + b.minute, e.hour * 60 + e.minute))
    return windows


def load_accounts(db, target: date_cls) -> List[Dict[str, Any]]:
    """只读 user_config_info。这里不写、也不该写任何字段。"""
    accounts = []
    cursor = db.user_config_info.find({
        "is_reserved": "True",
        "verified": True,
        "seat_list": {"$type": "array", "$ne": []},
    })
    for cfg in cursor:
        try:
            windows = _windows_for(cfg, target)
        except Exception as exc:  # noqa: BLE001 - 单个账号配置坏了不该毁掉整份报告
            logger.warning("账号 %s 时段解析失败: %s", cfg.get("pid"), exc)
            continue
        if not windows:
            continue
        accounts.append({
            "pid": cfg.get("pid"),
            "seat_list": list(cfg.get("seat_list") or []),
            "windows": windows,
            "late_protection": str(cfg.get("late_protection")) == "True",
            "priority": cfg.get("priority", 0),
        })
    return accounts


def build_report(
    days: int = 30,
    target: Optional[date_cls] = None,
    tag: Optional[str] = None,
    client=None,
) -> Dict[str, Any]:
    from pymongo import MongoClient

    from utils import config
    from utils.seat_snapshot import PRE_TAG, POST_TAG, RUSH_TAG, load_snapshots

    target = target or (datetime.now().date() + timedelta(days=1))
    owns_client = client is None
    client = client or MongoClient(config.get_mongo_uri())
    try:
        db = client[config.DB_NAME]
        # 目标日自己那张快照要剔掉：真正决策的时刻（目标日前一天的 07:00 之前）
        # 它还不存在，留着等于拿事后诸葛亮冒充预测，会把模型的真实水平掩盖掉。
        target_str = target.strftime("%Y-%m-%d")

        def _load(which_tag):
            # 快照的 date 是被观测的那一天，所以上界要取到目标日本身。
            docs = load_snapshots(db, days=days, tag=which_tag, until=target)
            return [doc for doc in docs if doc.get("date") != target_str]

        # 默认吃 rush：要预测的是「7:00 那一波我抢不抢得到」，rush 那张正好定格在
        # 那一波刚结束。post 里混了 07:01 之后慢慢约走的座位，拿它当目标会把
        # 本来抢得到的座位也算成抢不过。没有 rush 数据时才退回 post。
        # 固定 tag 也是为了确定性——同一天同一区域多个 tag 会互相覆盖，
        # 不指定的话结果取决于读取顺序。
        snapshots = _load(tag or RUSH_TAG)
        used_tag = tag or RUSH_TAG
        if not snapshots and not tag:
            snapshots = _load(POST_TAG)
            used_tag = POST_TAG
        pre_snapshots = _load(PRE_TAG)
        # 权重按「离目标日多远」算，基准日因此是目标日而不是今天。
        model = ContentionModel(snapshots, reference_date=target)
        pre_model = (ContentionModel(pre_snapshots, reference_date=target)
                     if pre_snapshots else None)
        accounts = load_accounts(db, target)
        rows = allocate(accounts, model, target.isoweekday(), pre_model=pre_model)
        return {
            "generated_at": datetime.now(),
            "target_date": target.strftime("%Y-%m-%d"),
            "weekday": target.isoweekday(),
            "wave_size": WAVE_SIZE,
            "snapshot_tag": used_tag,
            "snapshot_days": len(model.dates),
            "snapshot_dates": model.dates,
            "pre_snapshot_days": len(pre_model.dates) if pre_model else 0,
            "rows": rows,
        }
    finally:
        if owns_client:
            client.close()


def save_report(report: Dict[str, Any], client=None) -> None:
    """写进独立的 seat_plans 集合。**不碰 user_config_info。**"""
    from pymongo import MongoClient

    from utils import config

    owns_client = client is None
    client = client or MongoClient(config.get_mongo_uri())
    try:
        client[config.DB_NAME][PLAN_COLLECTION].replace_one(
            {"target_date": report["target_date"]}, report, upsert=True
        )
    finally:
        if owns_client:
            client.close()


def apply_plan(*_args, **_kwargs):
    """故意留的雷：现在禁止把分配结果写回用户配置。

    seat_list 和 priority 都属于用户配置，改动需要用户点头。要放开这条，
    先确认约束变了，再把这里换成真实实现，并且务必先跑一次 dry-run 对账。
    """
    raise NotImplementedError(
        "禁止修改用户配置（seat_list / priority 均在内）。"
        "seat_allocator 只出报告；如需生效，请人工确认后由用户自己改。"
    )


def format_report(report: Dict[str, Any]) -> str:
    lines = [
        f"目标日 {report['target_date']}（周{report['weekday']}）  "
        f"快照 {report['snapshot_days']} 天(tag={report.get('snapshot_tag', '?')})  "
        f"第一波 {report['wave_size']} 人",
        "",
        f"{'序':>2} {'波':>2} {'学号':<12} {'时段':<14} "
        f"{'现备选(存活率)':<34} {'赛前':>5} {'建议座位':<10} {'存活':>5} {'建议pri':>7}",
    ]
    for row in report["rows"]:
        own = " ".join(f"{o['seat']}:{o['p']:.2f}" for o in row["own_seats"])
        mark = "" if row["assigned_is_own"] else " *"
        pre = row.get("best_own_p_before_rush")
        lines.append(
            f"{row['queue_position']:>2} {row['wave']:>2} {row['pid']:<12} "
            f"{','.join(row['windows']):<14} {own[:34]:<34} "
            f"{('-' if pre is None else f'{pre:.2f}'):>5} "
            f"{(row['assigned_seat'] or '无')+mark:<10} {row['assigned_p']:>5.2f} "
            f"{row['suggested_priority']:>7}"
        )
    lines.append("")
    lines.append("* = 建议的座位不在该账号现有备选里；按当前约定不会自动改，仅供参考。")
    lines.append("赛前 = 抢座前那张快照里备选还空着的概率。这个数低说明座位在 7:00 之前"
                 "就没了，调队列救不了，只能换座位。")
    if not report.get("pre_snapshot_days"):
        lines.append("⚠ 还没有 pre 快照，「赛前」一列全是 -，"
                     "暂时分不清「抢不过」和「本来就没了」。")
    days = report["snapshot_days"]
    if days == 0:
        lines.append("⚠ 一天可用快照都没有，上面每个 0.50 都是先验默认值，"
                     "整张表没有任何信息量。先让 utils.seat_snapshot 攒几天。")
    elif days < 7:
        lines.append(f"⚠ 只有 {days} 天快照，先验仍占主导，存活率的绝对值别当真，"
                     f"只看相对高低。")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] [%(levelname)s] %(message)s")
    args = list(argv if argv is not None else sys.argv[1:])
    days = 30
    tag = None
    save = False
    while args:
        item = args.pop(0)
        if item == "--days" and args:
            days = int(args.pop(0))
        elif item == "--tag" and args:
            tag = args.pop(0)
        elif item == "--save":
            save = True
        else:
            print(__doc__)
            return 1

    report = build_report(days=days, tag=tag)
    if not report["rows"]:
        print("没有可分配的账号，或者快照数据还是空的（先跑 utils.seat_snapshot）")
        return 1
    print(format_report(report))
    if save:
        save_report(report)
        print(f"\n已写入 {PLAN_COLLECTION}（未改动任何用户配置）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
