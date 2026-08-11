"""
通知工具模块

邮件通知：优先走 Resend API，未配置时回退到 SMTP。
"""

import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

logger = logging.getLogger(__name__)

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


def notify_user(user_config: dict, title: str, content: str) -> None:
    """
    根据用户配置发送通知

    Args:
        user_config: 用户配置字典
        title: 通知标题
        content: 通知内容
    """
    email = user_config.get("notify_email", "")
    if not email:
        logger.info(f"用户 {user_config.get('pid', '?')} 未配置通知邮箱，跳过通知")
        return
    send_email(email, title, content)
