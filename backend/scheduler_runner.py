"""
定时任务调度入口

使用 APScheduler 在每天指定时间执行预约任务，
预约完成后自动启动迟到保护服务。

环境变量:
  SCHEDULE_HOUR   - 预约执行的小时 (默认 7)
  SCHEDULE_MINUTE - 预约执行的分钟 (默认 0)
"""

import os
import time
import logging
from datetime import datetime
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
    scheduler.add_job(
        run_auto_nap_check,
        'interval',
        minutes=1,
        id='auto_nap_check',
        replace_existing=True
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止")

if __name__ == "__main__":
    main()
