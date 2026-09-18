"""
CAS（统一身份认证）登录页的解析与填表，webvpn 与直连两条路共用。

单独成模块是为了让 library_system 不必依赖 vpn_system（测试里那个模块常被整个替换掉）。
"""

import re
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup

from utils.password_encryptor import PasswordEncryptor
import logging

logger = logging.getLogger(__name__)


def _log(level: str, user: str, operation: str, message: str) -> None:
    getattr(logger, level)(message, extra={'user': user, 'operation': operation})


# 统一身份认证明确拒绝密码时的提示语。只认这几种明说「密码」的文案，
# 账号锁定、需要验证码之类的都不算——那些不是用户改了密码，不能拿去翻 verified。
CREDENTIAL_REJECTED_MARKERS = (
    "密码有误",
    "密码错误",
    "密码不正确",
)


def is_credentials_rejected_message(message: str) -> bool:
    """CAS 返回的错误文案是否明确说了「用户名或密码不对」。"""
    normalized = (message or "").replace(" ", "")
    return any(marker in normalized for marker in CREDENTIAL_REJECTED_MARKERS)


WEBVPN_BASE_URL = "https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIxNC4xNTguMTk5LjEwMi4xNjIuMTU5LjIwMi4xNjguMTQ3LjE1MS4xNTYuMTczLjE0OC4xNTMuMTY1/"
CAS_BASE_URL = "https://authserver.njfu.edu.cn/"
LIBSEAT_BASE_URL = "https://libseat.njfu.edu.cn/"
# 图书馆自己吐出来的 CAS 入口（带它要的 service 参数），直连模式拿它当登录页，
# 不用猜 CAS 到底给 libseat 注册了哪个 service
LIBSEAT_SSO_ADDRESS_URL = f"{LIBSEAT_BASE_URL}ic-web/auth/address"
LIBSEAT_SSO_ADDRESS_PARAMS = {
    "finalAddress": "http://libseat.njfu.edu.cn/",
    "manager": "false",
    "consoleType": "16",
}


def is_cas_login_page(html_text: str) -> bool:
    return "pwdDefaultEncryptSalt" in (html_text or "")


def extract_cas_form(html_text: str) -> Optional[Tuple[str, str, str]]:
    """从 CAS 登录页取 (加密盐, lt, execution)；不是登录页返回 None。"""
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        salt_input = soup.find('input', {'id': 'pwdDefaultEncryptSalt'})
        lt_input = soup.find('input', {'name': 'lt'})
        if not salt_input or not lt_input:
            return None
        execution_input = soup.find('input', {'name': 'execution'})
        execution = execution_input['value'] if execution_input and execution_input.get('value') else 'e1s1'
        return salt_input['value'], lt_input['value'], execution
    except Exception as e:
        _log('error', '系统', '表单解析', f"提取表单元素时发生异常: {str(e)}")
        return None


def build_cas_form_data(username: str, password: str, salt: str, lt: str, execution: str) -> Optional[dict]:
    encrypted_password = PasswordEncryptor.aes_encrypt_password(salt, password)
    if not encrypted_password:
        return None
    return {
        'username': username,
        'password': encrypted_password,
        'lt': lt,
        'dllt': 'userNamePasswordLogin',
        'execution': execution,
        '_eventId': 'submit',
        'rmShown': '1',
    }


def describe_cas_landing(response: requests.Response) -> Tuple[str, str]:
    """
    登录没落到成功页时把落地页说清楚，返回 (给日志的描述, CAS 的错误文案或空串)。

    落在哪个页面必须打出来：密码错是一种；webvpn 拿 ticket 换会话失败（「访问出现异常」）
    是另一种，靠重试没用；2026-09-17 之后还有第三种——CAS 要短信动态码的二次认证页。
    """
    soup = BeautifulSoup(response.text, 'html.parser')
    title = soup.title.get_text(strip=True) if soup.title else ''
    hops = [f"{h.status_code}->{h.headers.get('Location', '')[:80]}" for h in response.history]
    for tag in soup(['script', 'style']):
        tag.decompose()
    # 二次认证页会带真实姓名，日志里抹掉
    body_text = re.sub(r'您好，\S+', '您好，[姓名]', ' '.join(soup.get_text(' ').split()))[:400]
    description = (
        f"落地页: {response.url[:300]} 标题: {title!r} 状态码: {response.status_code} "
        f"跳转链: {hops} 正文: {body_text!r}"
    )
    error_element = soup.find(class_='errortip') or soup.find(id='msg')
    error_message = error_element.get_text(strip=True) if error_element else ''
    return description, error_message
