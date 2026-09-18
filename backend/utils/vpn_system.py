"""
统一身份认证（CAS）登录模块

两条路，由 LIBRARY_ACCESS 决定（见 utils/phone_relay.py）：
- webvpn：登录 webvpn 网关，顺带在 CAS 上留下会话，图书馆随后走 CAS SSO 免密进入。
  2026-09-17 起 CAS 对 webvpn 要短信动态码，这条路在校方解除之前走不通。
- direct / phone：不经 webvpn，直接向 authserver 认证、拿 libseat 的 SSO 入口换票。
  phone 模式下这些请求由用户自己的手机在校园网上发出。

VPNSystem 这个名字保留：十几处调用方只关心「vpn_login() 之后 session 里有没有 CAS 会话」。
"""

import requests

from utils.base_system import BaseSystem
from utils.cas_login import (  # noqa: F401  —— 老调用方仍从这里 import
    CREDENTIAL_REJECTED_MARKERS,
    CAS_BASE_URL,
    LIBSEAT_BASE_URL,
    LIBSEAT_SSO_ADDRESS_PARAMS,
    LIBSEAT_SSO_ADDRESS_URL,
    WEBVPN_BASE_URL,
    build_cas_form_data,
    describe_cas_landing,
    extract_cas_form,
    is_cas_login_page,
    is_credentials_rejected_message,
)
from utils.phone_relay import uses_webvpn
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

class VPNSystem(BaseSystem):
    def __init__(self, username, password):
        self.via_webvpn = uses_webvpn()
        super().__init__(
            username=username,
            password=password,
            base_url=WEBVPN_BASE_URL if self.via_webvpn else CAS_BASE_URL,
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
        form = extract_cas_form(html_text)
        if not form:
            log_with_user('error', '系统', '表单解析', "未找到必要的表单元素")
            return None
        return form[0], form[1]

    def vpn_login(self):
        """
        建立 CAS 会话：webvpn 模式登录网关，直连模式直接向 authserver 认证。

        :return: 登录成功返回 True，失败返回 False
        """
        self.last_error = ""
        self.credentials_rejected = False
        if not self.via_webvpn:
            return self._direct_cas_login()
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

        # 加密密码并提交登录请求（webvpn 下 execution 一直是 e1s1，沿用）
        data = build_cas_form_data(self.username, self.password, salt, lt, 'e1s1')
        if not data:
            log_with_user('error', self.username, 'VPN登录', "密码加密失败")
            return False

        try:
            response = self.session.post(login_url, data=data)

            if response and "frontend/login/index.html" in response.url:
                return True
            log_with_user('error', self.username, 'VPN登录', "VPN登录失败：未重定向到成功页面")
            self._record_failed_landing(response, 'VPN登录')
            return False

        except Exception as e:
            log_with_user('error', self.username, 'VPN登录', f"提交VPN登录请求时发生异常: {str(e)}")
            return False

    def _record_failed_landing(self, response: requests.Response, operation: str) -> None:
        """登录没成功：把落地页记到日志，认出「密码错」就置 credentials_rejected。"""
        try:
            description, error_message = describe_cas_landing(response)
            log_with_user('warning', self.username, operation, description)
            if error_message:
                self.last_error = error_message
                self.credentials_rejected = is_credentials_rejected_message(error_message)
                log_with_user('error', self.username, operation, f"登录错误信息: {error_message}")
            else:
                log_with_user('error', self.username, operation, "响应内容中未找到明显的错误信息元素")
        except Exception as parse_error:
            log_with_user('error', self.username, operation,
                          f"解析响应内容查找错误信息时发生异常: {str(parse_error)}")

    def _direct_cas_login(self) -> bool:
        """
        不经 webvpn 的 CAS 认证。

        先问图书馆要它的 SSO 入口（带 service 参数的 authserver 地址），跟到 CAS 登录页，
        填表提交；CAS 认了会 302 回 libseat 换票。成功的判据是 CAS 那一跳给了 ticket——
        之后 session 里就有 CAS 会话（CASTGC），LibrarySystem 走同一条 SSO 链免密进入。
        """
        operation = 'CAS直连登录'
        try:
            addr_resp = self.session.get(LIBSEAT_SSO_ADDRESS_URL, params=LIBSEAT_SSO_ADDRESS_PARAMS)
            if addr_resp.status_code != 200:
                log_with_user('error', self.username, operation,
                              f"获取图书馆 SSO 入口失败，状态码: {addr_resp.status_code}")
                return False
            entry = (addr_resp.json() or {}).get("data")
            if not entry:
                log_with_user('error', self.username, operation, f"图书馆 SSO 入口为空: {addr_resp.text[:200]}")
                return False
        except Exception as e:
            log_with_user('error', self.username, operation, f"获取图书馆 SSO 入口时发生异常: {str(e)}")
            raise

        try:
            page = self.session.get(entry)
        except Exception as e:
            log_with_user('error', self.username, operation, f"打开 CAS 登录页时发生异常: {str(e)}")
            raise
        if page.status_code != 200:
            log_with_user('error', self.username, operation, f"CAS 登录页状态码: {page.status_code}")
            return False
        if not is_cas_login_page(page.text):
            if self._landed_in_library(page):
                # CAS 已经有会话（同一 session 重复登录），直接算成功
                return True
            log_with_user('error', self.username, operation, "SSO 入口没有落到 CAS 登录页")
            self._record_failed_landing(page, operation)
            return False

        form = extract_cas_form(page.text)
        if not form:
            log_with_user('error', self.username, operation, "提取表单元素失败，可能页面结构已改变")
            return False
        salt, lt, execution = form
        data = build_cas_form_data(self.username, self.password, salt, lt, execution)
        if not data:
            log_with_user('error', self.username, operation, "密码加密失败")
            return False

        try:
            # 表单提交回登录页自己的地址（带 service 参数），跟着跳转直到 libseat 收下票
            response = self.session.post(page.url, data=data)
        except Exception as e:
            log_with_user('error', self.username, operation, f"提交 CAS 登录请求时发生异常: {str(e)}")
            raise

        if self._cas_issued_ticket(response):
            log_with_user('info', self.username, operation, "CAS 认证通过，已换取图书馆会话")
            return True
        log_with_user('error', self.username, operation, "CAS 没有签发 ticket")
        self._record_failed_landing(response, operation)
        return False

    @staticmethod
    def _cas_issued_ticket(response: requests.Response) -> bool:
        return any("ticket=" in hop.headers.get("Location", "") for hop in response.history)

    @staticmethod
    def _landed_in_library(response: requests.Response) -> bool:
        return "libseat.njfu.edu.cn" in (response.url or "") and "authserver" not in (response.url or "")
