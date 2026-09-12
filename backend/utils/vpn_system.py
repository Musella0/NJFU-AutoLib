"""
VPN系统模块

本模块实现了与VPN系统的交互功能，包括：
1. VPN登录和认证
2. 会话管理
3. 错误处理

主要组件：
- VPNSystem: 核心类，处理所有与VPN系统的交互
- 日志系统：记录操作日志和错误信息
"""

from utils.base_system import BaseSystem
from bs4 import BeautifulSoup
from utils.password_encryptor import PasswordEncryptor
import logging

# 获取日志记录器
logger = logging.getLogger(__name__)

def log_with_user(level: str, user: str, operation: str, message: str) -> None:
    """
    统一的日志记录函数
    
    Args:
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


class VPNSystem(BaseSystem):
    def __init__(self, username, password):
        super().__init__(
            username=username,
            password=password,
            base_url="https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIxNC4xNTguMTk5LjEwMi4xNjIuMTU5LjIwMi4xNjguMTQ3LjE1MS4xNTYuMTczLjE0OC4xNTMuMTY1/",
            vpn_suffix=""
        )
        # 上一次 vpn_login() 失败时 CAS 给出的文案；明确是密码被拒时 credentials_rejected 为 True。
        # vpn_login() 的返回值仍然只是 True/False，调用方想区分「密码错」和「网络抽风」看这两个字段。
        self.last_error: str = ""
        self.credentials_rejected: bool = False

    def fetch_vpn_initial_page(self, login_url, params):
        """
        获取 VPN 初始页面。

        :param login_url: VPN 登录 URL
        :param params: 请求参数
        :return: (响应文本, 响应状态码) 或 None
        """
        try:
            response = self.session.get(login_url, params=params)
            if response.status_code != 200:
                log_with_user('error', self.username, 'VPN初始化', f"VPN初始页面响应状态码: {response.status_code}")
                return None
            return response.text
        except Exception as e:
            log_with_user('error', self.username, 'VPN初始化', f"获取VPN初始页面时发生异常: {str(e)}")
            return None

    @staticmethod
    def extract_form_elements(html_text):
        """
        从页面中提取必要的表单元素。

        :param html_text: HTML 文本
        :return: (salt, lt) 或 None
        """
        try:
            soup = BeautifulSoup(html_text, 'html.parser')
            salt_input = soup.find('input', {'id': 'pwdDefaultEncryptSalt'})
            lt_input = soup.find('input', {'name': 'lt'})
            if not salt_input or not lt_input:
                log_with_user('error', '系统', '表单解析', "未找到必要的表单元素")
                return None
            return salt_input['value'], lt_input['value']
        except Exception as e:
            log_with_user('error', '系统', '表单解析', f"提取表单元素时发生异常: {str(e)}")
            return None

    def vpn_login(self):
        """
        登录 VPN，获取有效的 VPN 会话。

        :return: 登录成功返回 True，失败返回 False
        """
        login_url = f"{self.base_url}authserver/login"
        params = {'service': 'https://webvpn.njfu.edu.cn/rump_frontend/loginFromCas/'}

        # 获取初始页面
        try:
            response = self.session.get(login_url, params=params)
            if response.status_code != 200:
                log_with_user('error', self.username, 'VPN登录', f"VPN初始页面获取失败，状态码: {response.status_code}")
                return False
            html_text = response.text
        except Exception as e:
            log_with_user('error', self.username, 'VPN登录', f"获取VPN初始页面时发生异常: {str(e)}")
            return False

        # 提取表单元素
        form_elements = self.extract_form_elements(html_text)
        if not form_elements:
            log_with_user('error', self.username, 'VPN登录', "提取表单元素失败，可能页面结构已改变或不是登录页面")
            return False

        salt, lt = form_elements

        # 加密密码
        encrypted_password = PasswordEncryptor.aes_encrypt_password(salt, self.password)
        if not encrypted_password:
            log_with_user('error', self.username, 'VPN登录', "密码加密失败")
            return False

        # 提交登录请求
        data = {
            'username': self.username,
            'password': encrypted_password,
            'lt': lt,
            'dllt': 'userNamePasswordLogin',
            'execution': 'e1s1', 
            '_eventId': 'submit',
            'rmShown': '1'
        }

        self.last_error = ""
        self.credentials_rejected = False
        try:
            response = self.session.post(login_url, data=data)

            if response and "frontend/login/index.html" in response.url:
                return True
            else:
                log_with_user('error', self.username, 'VPN登录', "VPN登录失败：未重定向到成功页面")
                log_with_user('debug', self.username, 'VPN登录', f"当前URL: {response.url}")
                # 尝试从响应内容中查找错误信息
                try:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    error_message = soup.find(class_='errortip') or soup.find(id='msg')
                    if error_message:
                        self.last_error = error_message.get_text(strip=True)
                        self.credentials_rejected = is_credentials_rejected_message(self.last_error)
                        log_with_user('error', self.username, 'VPN登录', f"登录错误信息: {self.last_error}")
                    else:
                        log_with_user('error', self.username, 'VPN登录', "响应内容中未找到明显的错误信息元素")
                except Exception as parse_error:
                    log_with_user('error', self.username, 'VPN登录', f"解析响应内容查找错误信息时发生异常: {str(parse_error)}")
                return False

        except Exception as e:
            log_with_user('error', self.username, 'VPN登录', f"提交VPN登录请求时发生异常: {str(e)}")
            return False
