"""
通知工具模块

支持:
1. 邮件通知 (SMTP)
2. Server酱微信推送 (sct.ftqq.com)
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
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("邮件通知未配置 SMTP，跳过发送")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_FROM or SMTP_USER
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(content, "plain", "utf-8"))

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        logger.info(f"邮件已发送至 {to_addr}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def send_serverchan(key: str, title: str, content: str) -> bool:
    """
    Server酱微信推送

    Args:
        key: Server酱的 SendKey
        title: 消息标题
        content: 消息内容（支持 Markdown）

    Returns:
        bool: 是否发送成功
    """
    if not key:
        return False

    try:
        url = f"https://sctapi.ftqq.com/{key}.send"
        resp = requests.post(url, data={"title": title, "desp": content}, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            logger.info(f"Server酱推送成功")
            return True
        else:
            logger.error(f"Server酱推送失败: {result}")
            return False
    except Exception as e:
        logger.error(f"Server酱推送异常: {e}")
        return False


def notify_user(user_config: dict, title: str, content: str) -> None:
    """
    根据用户配置发送通知

    从用户配置中读取通知渠道并发送

    Args:
        user_config: 用户配置字典
        title: 通知标题
        content: 通知内容
    """
    sent = False

    # 邮件通知
    email = user_config.get("notify_email", "")
    if email:
        if send_email(email, title, content):
            sent = True

    # Server酱
    sc_key = user_config.get("notify_serverchan_key", "")
    if sc_key:
        if send_serverchan(sc_key, title, content):
            sent = True

    if not sent:
        logger.info(f"用户 {user_config.get('pid', '?')} 未配置通知渠道，跳过通知")
