"""
定时任务调度入口

使用 APScheduler 在每天指定时间执行预约任务，
预约完成后自动启动迟到保护服务。

环境变量:
  SCHEDULE_HOUR         - 预约执行的小时 (默认 7)
  SCHEDULE_MINUTE       - 预约执行的分钟 (默认 0)
  RESERVE_CONCURRENCY   - 抢座并发度 (默认 8)
  PRELOGIN_ENABLED      - 是否提前登录 (默认 1，设 0 关闭)
  PRELOGIN_LEAD_MINUTES - 提前多少分钟预登录 (默认 10)
  PRELOGIN_REFRESH_LEAD_SECONDS - 提前多少秒复查会话 (默认 30)
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
    """执行一次完整的预约 + 迟到保护流程"""
    logger.info("========== 开始执行预约任务 ==========")
    try:
        # 每次执行时重新导入，确保拿到最新的数据库连接
        from scheduled_task import process_reservations, schedule_late_protection_jobs

        # 1. 执行所有预约
        process_reservations()
        logger.info("预约任务执行完毕")

        # 2. 启动迟到保护（会阻塞到22:00）
        logger.info("启动迟到保护服务...")
        schedule_late_protection_jobs()

    except Exception as e:
        logger.error(f"预约任务执行异常: {e}", exc_info=True)
    finally:
        logger.info("========== 预约任务结束 ==========")

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

    logger.info(f"定时预约调度器启动，每天 {hour:02d}:{minute:02d} 执行预约")

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
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
    scheduler.add_job(
        run_visit_check,
        'cron',
        hour='8,10,12,14,16,18,20,22',
        minute=0,
        id='visit_check',
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
