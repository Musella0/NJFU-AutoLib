"""
通知工具模块

邮件通知：优先走 Resend API，未配置时回退到 SMTP。
"""

import os
import logging
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

logger = logging.getLogger(__name__)

# 抢座改成并发之后，几十封结果邮件会在同一秒里发出去，而 Resend 免费额度是
# 每秒 2 封，超出的直接被丢掉。单线程队列既做串行化又做限流，抢座线程只投递
# 不等待，不会因为发信被拖慢——发信慢一点无所谓，抢座慢一秒座位就没了。
NOTIFY_MIN_INTERVAL = float(os.getenv("NOTIFY_MIN_INTERVAL", "0.6"))
_notify_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify")
_last_sent_at = 0.0

# SMTP 配置（从环境变量读取，管理员统一配置）
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")  # QQ邮箱授权码
SMTP_FROM = os.getenv("SMTP_FROM", "")  # 发件人地址，默认同 SMTP_USER

# Resend Email API（配置后优先于 SMTP）
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM = os.getenv("RESEND_FROM", "AutoLib <onboarding@resend.dev>")


def _send_email_via_resend(to_addr: str, subject: str, content: str) -> bool:
    """Send a plain-text email through the Resend HTTPS API."""
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": RESEND_FROM,
                "to": [to_addr],
                "subject": subject,
                "text": content,
            },
            timeout=15,
        )
        if response.status_code in (200, 201):
            logger.info("Resend 邮件发送成功")
            return True
        logger.error("Resend 邮件发送失败: HTTP %s", response.status_code)
        return False
    except Exception as exc:
        logger.error("Resend 邮件发送异常: %s", exc)
        return False


def send_email(to_addr: str, subject: str, content: str) -> bool:
    """
    发送邮件通知

    Args:
        to_addr: 收件人邮箱
        subject: 邮件主题
        content: 邮件内容

    Returns:
        bool: 是否发送成功
    """
    if RESEND_API_KEY:
        return _send_email_via_resend(to_addr, subject, content)

    if not SMTP_USER or not SMTP_PASS:
        logger.warning("邮件通知未配置 Resend 或 SMTP，跳过发送")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_FROM or SMTP_USER
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(content, "plain", "utf-8"))

        if SMTP_PORT == 465:
            context = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
        else:
            context = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
            context.starttls()
        with context as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        logger.info(f"邮件已发送至 {to_addr}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def _send_paced(to_addr: str, subject: str, content: str) -> None:
    """队列 worker：两封之间至少隔 NOTIFY_MIN_INTERVAL 秒。单线程独占，无需加锁。"""
    global _last_sent_at
    wait = NOTIFY_MIN_INTERVAL - (time.monotonic() - _last_sent_at)
    if wait > 0:
        time.sleep(wait)
    try:
        send_email(to_addr, subject, content)
    except Exception as exc:
        logger.error("通知发送异常: %s", exc)
    finally:
        _last_sent_at = time.monotonic()


def queue_email(to_addr: str, subject: str, content: str):
    """Queue a paced email and return the Future for optional observation."""
    return _notify_pool.submit(_send_paced, to_addr, subject, content)


def notify_user(user_config: dict, title: str, content: str) -> None:
    """
    根据用户配置发送通知（异步投递，不阻塞调用方）

    Args:
        user_config: 用户配置字典
        title: 通知标题
        content: 通知内容
    """
    email = user_config.get("notify_email", "")
    if not email:
        logger.info(f"用户 {user_config.get('pid', '?')} 未配置通知邮箱，跳过通知")
        return
    queue_email(email, title, content)
