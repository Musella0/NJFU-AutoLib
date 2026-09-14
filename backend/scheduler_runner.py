"""
定时任务调度入口

使用 APScheduler 在每天指定时间执行预约任务。
迟到保护是独立的常驻 job（每 30 分钟重扫一次），与抢座解耦，
这样白天重启容器不会让当天剩下的保护全部失效。

环境变量:
  SCHEDULE_HOUR         - 预约执行的小时 (默认 7)
  SCHEDULE_MINUTE       - 预约执行的分钟 (默认 0)
  RESERVE_CONCURRENCY   - 抢座并发度 (默认 8)
  PRELOGIN_ENABLED      - 是否提前登录 (默认 1，设 0 关闭)
  PRELOGIN_LEAD_MINUTES - 提前多少分钟预登录 (默认 10)
  PRELOGIN_REFRESH_LEAD_SECONDS - 提前多少秒复查会话 (默认 30)
  SEAT_SNAPSHOT_ENABLED - 是否每天拍全馆占用快照 (默认 1，设 0 关闭)
  SEAT_SNAPSHOT_PRE_MINUTES - 抢座前多少分钟拍第一张 (默认 15，即 6:45)
  SEAT_SNAPSHOT_RUSH_SECONDS - 抢座后多少秒拍第二张 (默认 60，即 7:01)
  SEAT_SNAPSHOT_DELAY_MINUTES - 抢座后多少分钟拍第三张 (默认 5，即 7:05)
  SEAT_SNAPSHOT_PID - 拍快照用的观测账号 (见 utils/seat_snapshot.py)
  SEAT_OCCUPANCY_STATS_DELAY_MINUTES - 抢座后多少分钟汇总全馆预约概况 (默认 10，即 7:10)
  STUDY_TIME_SYNC_AT    - 每晚几点同步真实在馆时长 (默认 22:10，闭馆之后)
"""

import os
import time
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler

# 设置基础日志
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s"
)
logger = logging.getLogger("scheduler_runner")

# 主调度器。迟到保护的一次性任务要注册到它上面，所以 run_late_protection_scan
# 需要能拿到；在 main() 里赋值。
_scheduler = None

def run_auto_nap_check():
    """每分钟扫描一次，对到达触发时间的用户执行自动午休"""
    try:
        from scheduled_task import process_auto_naps
        process_auto_naps()
    except Exception as e:
        logger.error(f"自动午休检查异常: {e}", exc_info=True)

def run_visit_check():
    """定期扫描已签到用户，作为学习记录同步的兜底。"""
    try:
        from scheduled_task import scan_and_record_visits
        scan_and_record_visits()
    except Exception as e:
        logger.error(f"道馆签到检查异常: {e}", exc_info=True)


def run_arrival_check():
    """处理预约生效 32 分钟后的精确到馆复查任务。"""
    try:
        from scheduled_task import process_due_arrival_checks
        process_due_arrival_checks()
    except Exception as e:
        logger.error(f"到馆复查异常: {e}", exc_info=True)


def run_pending_segment_check():
    """补约 7:00 时还超出图书馆 31 小时预约窗口的时段。"""
    try:
        from scheduled_task import process_due_segments
        process_due_segments()
    except Exception as e:
        logger.error(f"排队补约异常: {e}", exc_info=True)


# 06:45 建好的观测会话，留给 07:01 那张复用。抢座窗口里不能再登一次。
_snapshot_session = None


def run_seat_snapshot_task(tag, keep_session=False, reuse_session=False):
    """拍一张明天的全馆占用快照，攒「哪张座位有人抢」的历史数据。

    一个早上拍三张，tag 分别是：
      pre  (06:45) 抢座前，板子应该还是空的——它同时是「有没有人比 7:00 更早下手」的对照
      rush (07:01) 7:00 那一波刚结束，这张才是「谁抢赢了」的真信号
      post (07:05) 加上几分钟慢慢约的人

    rush 减 pre = 真抢不过的座位；post 减 rush = 我们本来拿得到的座位。
    单看 post 这两类混在一起，竞争度会被算高。

    只读 ic-web，不碰任何用户配置。tag 用固定字面量而不是执行时刻，
    偶尔晚几分钟跑也还归到同一批数据里，不会把序列切碎。
    """
    global _snapshot_session
    try:
        from utils.seat_snapshot import capture, login

        library = None
        if reuse_session:
            library, _snapshot_session = _snapshot_session, None
            if library is None:
                logger.warning("没有可复用的观测会话，%s 这张跳过——"
                               "抢座窗口里不现场登录", tag)
                return
        elif keep_session:
            library = login()

        capture(tag=tag, library=library)
        if keep_session:
            _snapshot_session = library
    except Exception as e:
        _snapshot_session = None
        logger.error(f"座位占用快照异常: {e}", exc_info=True)


def run_occupancy_stats_task():
    """07:10 把早上三张快照压成一条「全馆预约概况」，设置页的折线图读它。

    只读库不碰图书馆；顺手补齐历史上有快照但没汇总的日子，所以启动时也跑一次。
    """
    try:
        from utils.occupancy_stats import refresh
        refresh()
    except Exception as e:
        logger.error(f"预约概况汇总异常: {e}", exc_info=True)


def run_study_time_sync():
    """闭馆后把当天勾了同意的用户的到馆记录换成真实在馆时长。

    一个用户登一次图书馆，逐条拉操作流水，没有任何时间压力；
    失败的记录接下来几晚会自动补。
    """
    try:
        from scheduled_task import sync_actual_study_time
        sync_actual_study_time()
    except Exception as e:
        logger.error(f"在馆时长同步异常: {e}", exc_info=True)


def run_school_notice_check():
    """20:00 检查学校公告；20:05/20:15 只在前次失败时继续。"""
    try:
        from utils.school_notice_monitor import run_school_notice_scan
        run_school_notice_scan()
    except Exception as e:
        logger.error(f"学校公告检查异常: {e}", exc_info=True)

def run_prelogin_task():
    """抢座前提前完成 webvpn + CAS 登录，把 4~8 秒的登录挪出抢座窗口。"""
    try:
        from scheduled_task import process_prelogin
        process_prelogin()
    except Exception as e:
        # 预登录纯粹是加速，失败了 7:00 照旧现场登录，不能让异常影响调度器。
        logger.error(f"预登录异常（抢座将回退到现场登录）: {e}", exc_info=True)


def run_prelogin_refresh_task():
    """抢座前 30 秒复查预登录会话，失效的趁还来得及重登一遍。"""
    try:
        from scheduled_task import process_prelogin
        process_prelogin(refresh=True)
    except Exception as e:
        logger.error(f"预登录复查异常（抢座将回退到现场登录）: {e}", exc_info=True)


def run_reservation_task():
    """执行一次抢座。迟到保护是独立的常驻 job，不再挂在这里。"""
    logger.info("========== 开始执行预约任务 ==========")
    try:
        # 每次执行时重新导入，确保拿到最新的数据库连接
        from scheduled_task import process_reservations

        process_reservations()
        logger.info("预约任务执行完毕")
    except Exception as e:
        logger.error(f"预约任务执行异常: {e}", exc_info=True)
    finally:
        logger.info("========== 预约任务结束 ==========")


def run_late_protection_scan():
    """
    把今天待触发的迟到保护任务注册到主调度器上，每 30 分钟重扫一次。

    独立成常驻 job 而不是挂在 07:00 抢座后面，是因为后者一旦白天重启容器就
    再也不会被调用，当天剩下所有人的保护会静默消失。现在启动即扫一次，
    重启只会丢掉「已经过了触发点」的那些，其余照常补回来。
    """
    try:
        from scheduled_task import register_late_protection_jobs
        register_late_protection_jobs(_scheduler)
    except Exception as e:
        logger.error(f"迟到保护扫描异常: {e}", exc_info=True)

def main():
    hour = int(os.getenv("SCHEDULE_HOUR", "7"))
    minute = int(os.getenv("SCHEDULE_MINUTE", "0"))

    prelogin_enabled = os.getenv("PRELOGIN_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off"
    )
    lead_minutes = max(1, int(os.getenv("PRELOGIN_LEAD_MINUTES", "10")))
    refresh_lead = max(5, int(os.getenv("PRELOGIN_REFRESH_LEAD_SECONDS", "30")))

    # 相对抢座时间倒推，SCHEDULE_HOUR/MINUTE 改了这两个点会跟着走。
    # 日期部分只是个占位，实际只取 hour/minute/second。
    reserve_at = datetime(2000, 1, 2, hour, minute)
    warm_at = reserve_at - timedelta(minutes=lead_minutes)
    refresh_at = reserve_at - timedelta(seconds=refresh_lead)
    # 占用快照一个早上拍三张，把 7:00 那一秒夹在中间——竞争度只存在于差里。
    #   pre  (06:45) 抢座前：板子应该还是空的，同时是「有没有人比 7:00 更早下手」的对照
    #   rush (07:01) 抢座刚结束：这张才是「谁抢赢了」
    #   post (07:05) 再加上几分钟慢慢约的人
    # rush-pre = 真抢不过的；post-rush = 我们本来拿得到的。只拍 post 这两类混在
    # 一起，竞争度会被算高。实测 09-11 的板子在前一天 10:50 还是 0/2749，
    # 所以分辨率必须做到分钟级，拍在 7 点前后几小时都是同一个数。
    snapshot_pre_at = reserve_at - timedelta(
        minutes=max(1, int(os.getenv("SEAT_SNAPSHOT_PRE_MINUTES", "15")))
    )
    # rush 那张卡在抢座刚结束、慢悠悠约座的人还没进来之前。实测抢座本身
    # 6~15 秒就跑完了（最慢的一天 07:00:15），60 秒留足了余量；而且它复用
    # 6:45 的会话，窗口里一次登录都不会发生。
    snapshot_rush_at = reserve_at + timedelta(
        seconds=max(20, int(os.getenv("SEAT_SNAPSHOT_RUSH_SECONDS", "60")))
    )
    snapshot_at = reserve_at + timedelta(
        minutes=max(1, int(os.getenv("SEAT_SNAPSHOT_DELAY_MINUTES", "5")))
    )
    # 07:10 汇总：跟在 post 后面，只读库，给网页的折线图用
    stats_at = reserve_at + timedelta(
        minutes=max(1, int(os.getenv("SEAT_OCCUPANCY_STATS_DELAY_MINUTES", "10")))
    )
    snapshot_enabled = os.getenv("SEAT_SNAPSHOT_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off"
    )
    # 闭馆 22:00 之后再拉操作流水，「结束」那条事件才写全
    study_sync_at = datetime.strptime(
        os.getenv("STUDY_TIME_SYNC_AT", "22:10").strip() or "22:10", "%H:%M")

    logger.info(f"定时预约调度器启动，每天 {hour:02d}:{minute:02d} 执行预约")

    global _scheduler
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    _scheduler = scheduler
    scheduler.add_job(
        run_reservation_task,
        'cron',
        hour=hour,
        minute=minute,
        id='daily_reservation',
        replace_existing=True
    )
    if prelogin_enabled:
        logger.info(
            f"预登录已启用：{warm_at:%H:%M:%S} 全量预登录，"
            f"{refresh_at:%H:%M:%S} 复查会话"
        )
        scheduler.add_job(
            run_prelogin_task,
            'cron',
            hour=warm_at.hour,
            minute=warm_at.minute,
            second=warm_at.second,
            id='reservation_prelogin',
            coalesce=True,
            max_instances=1,
            # 迟到超过 1 分钟就别跑了，否则可能和 7:00 的抢座撞在一起抢网关。
            misfire_grace_time=60,
            replace_existing=True
        )
        scheduler.add_job(
            run_prelogin_refresh_task,
            'cron',
            hour=refresh_at.hour,
            minute=refresh_at.minute,
            second=refresh_at.second,
            id='reservation_prelogin_refresh',
            coalesce=True,
            max_instances=1,
            # 复查只在抢座前那几十秒有意义，错过了就跳过。
            misfire_grace_time=max(5, refresh_lead - 5),
            replace_existing=True
        )
    else:
        logger.info("预登录已关闭（PRELOGIN_ENABLED=0），抢座时现场登录")
    scheduler.add_job(
        run_auto_nap_check,
        'interval',
        minutes=1,
        id='auto_nap_check',
        replace_existing=True
    )
    scheduler.add_job(
        run_arrival_check,
        'interval',
        minutes=1,
        id='arrival_check',
        replace_existing=True
    )
    # 每分钟扫一次补约队列：窗口一开就下单，晚几十秒无所谓——能约到的时段这时候
    # 几乎没人在抢，但一定要扫得比窗口密，否则等于错过。
    scheduler.add_job(
        run_pending_segment_check,
        'interval',
        minutes=1,
        id='pending_segment_check',
        coalesce=True,
        max_instances=1,
        replace_existing=True
    )
    scheduler.add_job(
        run_visit_check,
        'cron',
        hour='8,10,12,14,16,18,20,22',
        minute=0,
        id='visit_check',
        replace_existing=True
    )
    # 启动即扫一次（next_run_time=now），之后每 30 分钟一轮，好让当天新产生的
    # 预约——比如保护自己重约出来的、或用户手动约的——也能进保护。
    scheduler.add_job(
        run_late_protection_scan,
        'interval',
        minutes=30,
        id='late_protection_scan',
        next_run_time=datetime.now(),
        coalesce=True,
        max_instances=1,
        replace_existing=True
    )
    if snapshot_enabled:
        logger.info(
            f"座位占用快照已启用：{snapshot_pre_at:%H:%M} 抢座前 / "
            f"{snapshot_rush_at:%H:%M:%S} 抢座刚结束 / {snapshot_at:%H:%M} 尘埃落定，"
            f"三张的差分出「抢不过」和「本来拿得到」；{stats_at:%H:%M} 汇总预约概况"
        )
        scheduler.add_job(
            run_seat_snapshot_task,
            'cron',
            hour=snapshot_pre_at.hour,
            minute=snapshot_pre_at.minute,
            id='seat_snapshot_pre',
            # keep_session：这次登录留给 07:01 那张复用
            args=['pre', True, False],
            coalesce=True,
            max_instances=1,
            # 抢座前这张迟到就没意义了，而且绝不能拖进 6:50 的预登录和 7:00 的抢座——
            # 宁可这天缺一张 pre，也不能跟抢座抢网关。
            misfire_grace_time=120,
            replace_existing=True
        )
        scheduler.add_job(
            run_seat_snapshot_task,
            'cron',
            hour=snapshot_rush_at.hour,
            minute=snapshot_rush_at.minute,
            second=snapshot_rush_at.second,
            id='seat_snapshot_rush',
            # reuse_session：只复用 6:45 那个会话，取不到就干脆不拍
            args=['rush', False, True],
            coalesce=True,
            max_instances=1,
            # 这张的价值全在「卡在抢座刚结束那一刻」，迟到 30 秒以上就没意义了
            misfire_grace_time=30,
            replace_existing=True
        )
        scheduler.add_job(
            run_seat_snapshot_task,
            'cron',
            hour=snapshot_at.hour,
            minute=snapshot_at.minute,
            id='seat_snapshot_post',
            args=['post', False, False],
            coalesce=True,
            max_instances=1,
            # 晚半小时拍也还有用（抢座早结束了），再晚就没必要补了。
            misfire_grace_time=1800,
            replace_existing=True
        )
        scheduler.add_job(
            run_occupancy_stats_task,
            'cron',
            hour=stats_at.hour,
            minute=stats_at.minute,
            id='seat_occupancy_stats',
            # 启动先跑一次：把有快照没汇总的日子补上，第一次部署不用手工回填
            next_run_time=datetime.now(),
            coalesce=True,
            max_instances=1,
            # 只是读库算几个数，什么时候补都行
            misfire_grace_time=3600,
            replace_existing=True
        )
    scheduler.add_job(
        run_study_time_sync,
        'cron',
        hour=study_sync_at.hour,
        minute=study_sync_at.minute,
        id='study_time_sync',
        coalesce=True,
        max_instances=1,
        # 晚几个小时补也没关系，流水一直在图书馆那边
        misfire_grace_time=3600 * 3,
        replace_existing=True
    )
    scheduler.add_job(
        run_school_notice_check,
        'cron',
        hour=20,
        minute='0,5,15',
        id='school_notice_check',
        coalesce=True,
        max_instances=1,
        replace_existing=True
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止")

if __name__ == "__main__":
    main()
