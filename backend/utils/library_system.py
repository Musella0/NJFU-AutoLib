"""
图书馆座位预约系统模块

本模块实现了与图书馆座位预约系统的交互功能，包括：
1. 用户登录和认证
2. 座位预约管理
3. 预约信息查询
4. 数据库操作

主要组件：
- LibrarySystem: 核心类，处理所有与图书馆系统的交互
- 数据库操作：使用 MongoDB 存储用户信息和预约记录
- 日志系统：记录操作日志和错误信息

注意事项：
- 所有网络请求都需要通过 VPN
- 密码使用 RSA 公钥加密
- 预约操作需要先登录
- 所有操作都有详细的日志记录
"""

from datetime import datetime, timedelta
import html
import os
import re
from typing import Dict, List, Optional, Tuple, Any, Union
from urllib.parse import urljoin, urlparse

import requests
from pymongo import MongoClient, ASCENDING, DESCENDING
import time
import logging

from utils.base_system import BaseSystem, TimeoutSession
from utils.password_encryptor import PasswordEncryptor
from utils import config
from utils.vpn_system import VPNSystem
from utils import attempt_log

# 获取日志记录器
logger = logging.getLogger(__name__)

ARRIVAL_CHECK_DELAY_MINUTES = 32
LIBRARY_LOGIN_MAX_ATTEMPTS = max(
    1, int(os.getenv("LIBRARY_LOGIN_MAX_ATTEMPTS", "3"))
)
LIBRARY_LOGIN_RETRY_DELAY_SECONDS = max(
    0.0, float(os.getenv("LIBRARY_LOGIN_RETRY_DELAY_SECONDS", "2"))
)

# 会话有效性校验的超时。这个请求跑在 7:00:00 的关键路径上，卡住比失败更糟——
# 失败只是回退到现场登录，卡住会把整个抢座窗口拖没，所以给得比默认超时紧得多。
SESSION_VERIFY_TIMEOUT = (
    float(os.getenv("SESSION_VERIFY_CONNECT_TIMEOUT", "3")),
    float(os.getenv("SESSION_VERIFY_READ_TIMEOUT", "5")),
)

# 图书馆对同一账号同时只受理一个预约操作，上一发刚回完那零点几秒里再发会被
# 「您有预约操作正在进行」顶回。这时候不该跳下一张座位（2026-09-11 一个账号就是
# 这样把三张备选全烧掉的），而是等锁放开再打同一张。
BUSY_RETRY_DELAY_SECONDS = max(0.0, float(os.getenv("RESERVE_BUSY_RETRY_DELAY_SECONDS", "0.4")))
BUSY_RETRY_MAX = max(0, int(os.getenv("RESERVE_BUSY_RETRY_MAX", "3")))

# 下单响应里这些迹象说明会话已经被服务端踢掉了（预登录会话放久了会这样），
# 而不是座位有问题。认出来就现场重登、原座位再打一次。
AUTH_FAILURE_MARK = "会话失效"

# 下单请求根本没被图书馆受理——webvpn 网关限流/报错、连接被重置、超时、回的不是
# JSON——这些跟座位空不空毫无关系。认出来就别再往下打备选座位（网关在拒你，
# 多打只是多烧），把这一段整个交还给队列，等一小会儿从第一张座位重来。
REJECTED_MARK = "请求未被受理"
_REJECTED_MARKERS = (
    "网络请求异常",
    "预约过程异常",
    "请求失败: 状态码",
    "响应不是 JSON",
    # 图书馆自己的限流/繁忙文案
    "系统繁忙",
    "服务器繁忙",
    "操作频繁",
    "过于频繁",
    "请稍后再试",
    "请求超时",
)


def _is_account_busy(message: str) -> bool:
    """图书馆的账号级锁：「您有预约操作正在进行，请稍后操作」。"""
    return "预约操作正在进行" in (message or "")


def _is_auth_failure(message: str) -> bool:
    return AUTH_FAILURE_MARK in (message or "")


def _is_rejected(message: str) -> bool:
    """请求没被受理（网关/网络/限流），图书馆压根没看座位。

    「当前设备正在被预约，请稍后重试」和「您有预约操作正在进行」虽然也带「稍后」，
    但那是图书馆看过座位/账号之后的判定，不算被拒。
    """
    text = message or ""
    if not text or _is_account_busy(text) or "当前设备正在被预约" in text:
        return False
    if _is_auth_failure(text):
        return False
    return any(mark in text for mark in _REJECTED_MARKERS)


def _is_self_conflict(message: str) -> bool:
    """撞上自己已有的预约：「学工号为：xxx的用户在当前时段有预约」。

    这句话是**账号维度**的判定，图书馆压根没去看座位空不空——所以同一段里再打
    备选座位，只会一字不差地再收到同一句话。和 attempt_log.classify 认的是同一串。
    """
    return "的用户在当前时段有预约" in (message or "")


def _is_transient_login_error(message: str) -> bool:
    """判断图书馆登录失败是否适合在短时间内自动重试。"""
    normalized = message.lower().replace(" ", "")
    markers = (
        "系统繁忙",
        "稍后重试",
        "超时",
        "timeout",
        "timedout",
        "连接失败",
        "connection",
        "remotedisconnected",
        "nameresolution",
        "resolve",
        "dns",
        "temporarilyunavailable",
        "http429",
        "http500",
        "http502",
        "http503",
        "http504",
    )
    return any(marker in normalized for marker in markers)


class LibraryLoginError(Exception):
    """图书馆登录失败，并保留上游返回的错误码和原始消息。"""

    is_credentials_error = False

    def __init__(self, message: str, code: Optional[Union[int, str]] = None) -> None:
        self.code = code
        self.server_message = message
        detail = f"code {code}：{message}" if code is not None else message
        super().__init__(detail)


class LibraryCredentialsError(LibraryLoginError):
    """上游明确指出用户名或密码不正确。"""

    is_credentials_error = True


def _is_credentials_error(message: str) -> bool:
    """只识别明确提到用户名/账号与密码错误的响应。"""
    normalized = message.replace(" ", "")
    credential_markers = (
        "用户名或密码错误",
        "用户名或密码不正确",
        "账号或密码错误",
        "账号或密码不正确",
        "帐号或密码错误",
        "帐号或密码不正确",
        "登录名或密码错误",
        "登录名或密码不正确",
        "密码错误",
        "密码不正确",
        "密码有误",
    )
    return any(marker in normalized for marker in credential_markers)

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

# MongoDB 初始化
mongo_client = MongoClient(config.get_mongo_uri())
db = mongo_client.AutoLib
user_config_info = db.user_config_info  # 存储用户配置和预约记录
users_col = db.users  # 存储用户基本信息
devices_col = db.devices  # 存储设备信息


def register_arrival_check(
    pid: str,
    uuid: str,
    seat_name: str,
    resv_begin_time: str,
    resv_end_time: str,
) -> None:
    """持久化预约生效 32 分钟后的到馆复查任务。"""
    if not pid or not uuid:
        log_with_user('warning', pid or '未知用户', '到馆复查',
                      '预约成功响应缺少 uuid，无法注册到馆复查')
        return

    begin_time = datetime.strptime(resv_begin_time, "%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    db.arrival_checks.update_one(
        {"uuid": uuid},
        {
            "$set": {
                "pid": pid,
                "seat_name": seat_name,
                "target_time": (
                    f"{resv_begin_time[:10]} {resv_begin_time[11:]}-"
                    f"{resv_end_time[11:]}"
                ),
                "check_at": begin_time + timedelta(minutes=ARRIVAL_CHECK_DELAY_MINUTES),
                "status": "pending",
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now, "attempts": 0},
            "$unset": {"next_attempt_at": "", "checked_at": "", "message": ""},
        },
        upsert=True,
    )
    log_with_user(
        'info', pid, '到馆复查',
        f"已注册 {seat_name} 的到馆复查，执行时间 "
        f"{(begin_time + timedelta(minutes=ARRIVAL_CHECK_DELAY_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')}",
    )


class LibrarySystem(BaseSystem):
    """
    图书馆座位预约系统类

    处理所有与图书馆座位预约系统相关的操作，包括登录、预约、查询等。
    继承自 BaseSystem，使用共享的会话管理。

    Attributes:
        base_url (str): 图书馆系统基础URL
        vpn_suffix (str): VPN访问后缀
        user_info (Optional[Dict]): 用户信息
        session (requests.Session): HTTP会话对象
        vpn (Optional[VPNSystem]): VPN系统实例
    """

    # 系统URL配置
    BASE_URL = "https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIwNS4xNTguMjAwLjE3MS4xNTMuMTUwLjIxNi45Ny4yMTEuMTU2LjE1OC4xNzMuMTQ4LjE1NS4xNTUuMjE3LjEwMC4xNTAuMTY1/"
    VPN_SUFFIX = "?vpn-12-libseat.njfu.edu.cn"

    def __init__(
        self,
        username: str,
        password: str,
        vpn_password: Optional[str] = None,
        session: Optional[requests.Session] = None
    ) -> None:
        """
        初始化图书馆系统对象

        Args:
            username: 用户名（学号）
            password: 旧版图书馆密码（仅在 CAS SSO 失败回退时使用）
            vpn_password: VPN密码，如果提供则自动登录VPN
            session: 可选的共享会话对象
        """
        super().__init__(
            username=username,
            password=password,
            base_url=self.BASE_URL,
            vpn_suffix=self.VPN_SUFFIX
        )

        # 系统URL
        self.public_key_url = f"{self.base_url}ic-web/login/publicKey{self.vpn_suffix}"
        self.login_url = f"{self.base_url}ic-web/login/user{self.vpn_suffix}"
        self.reserve_url = f"{self.base_url}ic-web/reserve{self.vpn_suffix}"

        # 图书馆返回的用户信息。注意里面的 'pid' 是图书馆自己的内部人员 ID，
        # 和登录学号不一定相等，绝对不能拿它当本地数据库的主键——
        # user_config_info / arrival_checks / web_users 全都以 self.username（登录学号）归属。
        self.user_info: Optional[Dict[str, Any]] = None
        # 最近一次成功下单的结构化结果（uuid / 座位 / 时段）。预约接口只回一句给用户看的
        # 中文消息，而占位换约必须拿到 uuid 才能取消、拿到 devId 才能约回同一张座位。
        self.last_reservation: Optional[Dict[str, Any]] = None
        # 上一次 reserve_seat 是不是因为请求没被受理（网关/网络）而整段收手
        self.last_segment_rejected = False
        self.vpn: Optional[VPNSystem] = None

        # 使用共享会话或创建新会话（新建的同样要带默认超时）
        if session:
            self.session = session
        else:
            self.session = TimeoutSession()

        # 如果提供了VPN密码，先登录VPN
        if vpn_password:
            self._initialize_vpn(vpn_password)

        # 初始化登录
        self._initialize_login()

    def get_seat_name_by_id(self, seat_id: str) -> str:
        """
        根据座位ID获取座位名称

        Args:
            seat_id: 座位ID

        Returns:
            str: 座位名称，如果未找到则返回座位ID
        """
        try:
            device = devices_col.find_one({"devId": seat_id}, {"_id": 0, "devName": 1})
            if device:
                return device["devName"]
            else:
                return seat_id
        except Exception as e:
            log_with_user('warning', self.username, '座位名称获取', f"获取座位 {seat_id} 名称失败: {str(e)}")
            return seat_id

    def _initialize_vpn(self, vpn_password: str) -> None:
        """
        初始化并登录VPN

        Args:
            vpn_password: VPN密码

        Raises:
            LibraryCredentialsError: 统一身份认证明确返回密码错误
            Exception: 其他原因导致 VPN 登录失败
        """
        try:
            self.vpn = VPNSystem(self.username, vpn_password)
            self.vpn.session = self.session

            if not self.vpn.vpn_login():
                if self.vpn.credentials_rejected:
                    # CAS 明确说密码不对：抛带 is_credentials_error 的异常，
                    # 定时任务据此判断用户是不是改了学校密码，而不是当成网络抽风重试。
                    raise LibraryCredentialsError(f"VPN登录失败：{self.vpn.last_error}")
                raise Exception("VPN登录失败")

            # 等待VPN连接稳定（0.5秒）
            time.sleep(0.1)

        except Exception as e:
            print(f"VPN登录失败: {str(e)}")
            raise

    def _wrap_internal_url(self, url: str) -> str:
        """
        将原始 libseat 地址包装为 webvpn 代理地址。

        输出格式与项目现有请求一致：
        {base_url}{path}{vpn_suffix}&{query}
        """
        parsed = urlparse(url)
        wrapped = f"{self.base_url}{parsed.path.lstrip('/')}{self.vpn_suffix}"
        if parsed.query:
            wrapped += f"&{parsed.query}"
        return wrapped

    def _resolve_sso_url(
        self,
        next_url: str,
        current_url: Optional[str] = None,
    ) -> str:
        """将 SSO 跳转目标规整为可以直接请求的绝对地址。"""
        next_url = html.unescape(next_url.strip())

        if next_url.startswith("//"):
            next_url = "https:" + next_url
        elif not urlparse(next_url).scheme:
            if current_url:
                next_url = urljoin(current_url, next_url)
            elif next_url.startswith("/"):
                next_url = "https://webvpn.njfu.edu.cn" + next_url

        parsed = urlparse(next_url)
        if (parsed.hostname or "").lower() == "libseat.njfu.edu.cn":
            return self._wrap_internal_url(next_url)
        return next_url

    def _follow_sso_redirects(self, url: str, max_hops: int = 15) -> bool:
        """
        跟随 CAS SSO 跳转链直到落地。

        支持普通 HTTP 3xx 跳转，以及 webvpn 返回的
        window.location.href JS 垫片页。
        """
        for hop in range(max_hops):
            resp = self.session.get(
                url,
                allow_redirects=False,
                timeout=30,
            )

            if resp.status_code >= 400:
                self._sso_error = (
                    f"SSO跳转第{hop + 1}跳返回HTTP {resp.status_code}"
                )
                log_with_user(
                    'warning', self.username, 'CAS登录', self._sso_error
                )
                return False

            next_url = None
            if resp.status_code in (301, 302, 303, 307, 308):
                next_url = resp.headers.get("Location")
                if not next_url:
                    self._sso_error = (
                        f"SSO跳转第{hop + 1}跳缺少Location响应头"
                    )
                    log_with_user(
                        'warning', self.username, 'CAS登录', self._sso_error
                    )
                    return False

            if not next_url and resp.status_code == 200:
                match = re.search(
                    r"""window\.location\.href\s*=\s*['"]([^'"]+)['"]""",
                    resp.text,
                )
                if match:
                    next_url = match.group(1)

            if not next_url:
                log_with_user(
                    'debug',
                    self.username,
                    'CAS登录',
                    f"SSO跳转链结束于第{hop + 1}跳，状态码{resp.status_code}",
                )
                return True

            url = self._resolve_sso_url(next_url, current_url=url)

        self._sso_error = f"SSO跳转超过最大次数（{max_hops}跳）"
        log_with_user('warning', self.username, 'CAS登录', self._sso_error)
        return False

    def _login_via_cas_sso(self) -> bool:
        """
        通过统一身份认证（CAS SSO）登录图书馆系统。

        前提是 self.session 已完成 webvpn 登录并持有 CAS 会话。
        失败原因写入 self._sso_error，供上层错误信息使用。
        """
        self._sso_error = "未知错误"

        try:
            # 现有逆向记录中出现过两套附加参数；逐套尝试，
            # 只要接口成功返回 data 就停止，不手工拼接 URL 编码。
            param_sets = [
                {
                    "finalAddress": "http://libseat.njfu.edu.cn/",
                    "manager": "false",
                    "consoleType": "16",
                },
                {
                    "finalAddress": "http://libseat.njfu.edu.cn/",
                    "errPageUrl": "",
                    "manScSpaceReserv": "",
                },
            ]

            entry = None
            attempt_errors = []
            address_url = (
                f"{self.base_url}ic-web/auth/address{self.vpn_suffix}"
            )

            for param_index, params in enumerate(param_sets, start=1):
                try:
                    addr_resp = self.session.get(
                        address_url,
                        params=params,
                        timeout=30,
                    )
                    if addr_resp.status_code != 200:
                        attempt_errors.append(
                            f"HTTP {addr_resp.status_code}"
                        )
                        continue

                    addr_result = addr_resp.json()
                    entry = addr_result.get("data")
                    if entry:
                        self._sso_param_set = param_index
                        break

                    attempt_errors.append(
                        addr_result.get("message", "响应data为空")
                    )
                except Exception as e:
                    attempt_errors.append(str(e))

            if not entry:
                detail = "；".join(attempt_errors) or "无有效响应"
                self._sso_error = f"获取SSO入口地址失败: {detail}"
                log_with_user(
                    'warning', self.username, 'CAS登录', self._sso_error
                )
                return False

            entry = self._resolve_sso_url(entry)
            if not self._follow_sso_redirects(entry):
                return False

            info_resp = self.session.get(
                f"{self.base_url}ic-web/auth/userInfo{self.vpn_suffix}",
                timeout=30,
            )
            if info_resp.status_code != 200:
                self._sso_error = (
                    f"获取用户信息返回HTTP {info_resp.status_code}"
                )
                log_with_user(
                    'warning', self.username, 'CAS登录', self._sso_error
                )
                return False

            result = info_resp.json()
            if result.get("code") != 0 or not result.get("data"):
                self._sso_error = (
                    "SSO后仍未登录: "
                    f"{result.get('message', '未知错误')}"
                )
                log_with_user(
                    'warning', self.username, 'CAS登录', self._sso_error
                )
                return False

            user_info = result["data"]
            self._set_user_cookie(user_info)
            self.user_info = user_info
            log_with_user(
                'info',
                self.username,
                'CAS登录',
                "通过统一身份认证（CAS SSO）登录成功",
            )
            return True

        except Exception as e:
            self._sso_error = f"CAS单点登录异常: {str(e)}"
            log_with_user(
                'warning', self.username, 'CAS登录', self._sso_error
            )
            return False

    def _initialize_login(self) -> None:
        """
        登录图书馆系统。

        优先复用 webvpn 会话走 CAS SSO；SSO 主路径完全不使用
        图书馆密码。若 SSO 失败，则保留旧版 RSA 密码登录作为
        兼容回退，并在最终异常中同时报告两条路径的失败原因。
        """
        sso_error = "未知错误"
        for attempt in range(1, LIBRARY_LOGIN_MAX_ATTEMPTS + 1):
            if self._login_via_cas_sso():
                return

            sso_error = getattr(self, "_sso_error", "未知错误")
            should_retry = (
                attempt < LIBRARY_LOGIN_MAX_ATTEMPTS
                and _is_transient_login_error(sso_error)
            )
            if not should_retry:
                break

            delay = LIBRARY_LOGIN_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
            log_with_user(
                'warning',
                self.username,
                'CAS登录重试',
                f"第{attempt}次登录失败（{sso_error}），{delay:g}秒后重试",
            )
            time.sleep(delay)

        try:
            if not self._get_initial_cookie():
                raise Exception("获取初始Cookie失败")

            public_key, nonce = self._get_public_key()
            if not public_key or not nonce:
                raise Exception("获取公钥失败")

            user_info = self._perform_login(public_key, nonce)
            if not user_info:
                raise Exception("用户名或密码错误，或旧登录接口已停用")

            self._set_user_cookie(user_info)
            self.user_info = user_info

        except Exception as old_login_error:
            raise Exception(
                f"登录失败: CAS SSO未成功（{sso_error}）；"
                f"旧版密码登录回退同样失败（{old_login_error}）"
            ) from old_login_error

    def ensure_login(self) -> Optional[Dict[str, Any]]:
        """
        确保登录状态

        Checks the current login status and re-logs in if not logged in

        Returns:
            Optional[Dict[str, Any]]: 用户信息字典，登录失败返回None
        """
        if not self.user_info:
            self._initialize_login()
        return self.user_info

    def verify_session(self) -> bool:
        """
        轻量校验当前会话在服务端是否仍然有效。

        ensure_login() 只看本地的 self.user_info 有没有值，判断不了服务端那边
        会话是不是已经过期。预登录会话要放上十分钟，用前必须真的问一次。

        Returns:
            bool: 会话有效返回 True；顺带刷新 user_info 和 cookie。
        """
        try:
            resp = self.session.get(
                f"{self.base_url}ic-web/auth/userInfo{self.vpn_suffix}",
                timeout=SESSION_VERIFY_TIMEOUT,
            )
            if resp.status_code != 200:
                log_with_user('warning', self.username, '会话校验',
                              f"userInfo 返回 HTTP {resp.status_code}")
                return False

            result = resp.json()
            if result.get("code") != 0 or not result.get("data"):
                log_with_user('warning', self.username, '会话校验',
                              f"会话已失效: {result.get('message', '未知错误')}")
                return False

            user_info = result["data"]
            self._set_user_cookie(user_info)
            self.user_info = user_info
            return True

        except Exception as e:
            log_with_user('warning', self.username, '会话校验', f"校验会话异常: {str(e)}")
            return False

    def _get_initial_cookie(self) -> bool:
        """
        获取初始Cookie

        Returns:
            bool: 是否成功获取Cookie
        """
        try:
            init_resp = self.session.get(f"{self.base_url}ic-web/default/index{self.vpn_suffix}")
            if init_resp.status_code != 200:
                log_with_user('error', self.username, 'Cookie获取', f"获取初始Cookie失败: 状态码 {init_resp.status_code}")
                return False
            return True
        except Exception as e:
            log_with_user('error', self.username, 'Cookie获取', f"获取初始Cookie时发生异常: {str(e)}")
            return False

    def _get_public_key(self) -> Tuple[Optional[str], Optional[str]]:
        """
        获取登录所需的公钥和随机字符串

        Returns:
            Tuple[Optional[str], Optional[str]]: (公钥, 随机字符串)，获取失败返回(None, None)
        """
        try:
            key_resp = self.session.get(self.public_key_url)
            if key_resp.status_code != 200:
                log_with_user('error', self.username, '公钥获取', f"获取公钥失败: 状态码 {key_resp.status_code}")
                return None, None

            key_data = key_resp.json()
            if key_data.get('code') != 0:
                log_with_user('error', self.username, '公钥获取', f"获取公钥失败: {key_data.get('message', '未知错误')}")
                return None, None

            return key_data['data']['publicKey'], key_data['data']['nonceStr']
        except Exception as e:
            log_with_user('error', self.username, '公钥获取', f"获取公钥时发生异常: {str(e)}")
            return None, None

    def _perform_login(self, public_key: str, nonce: str) -> Dict[str, Any]:
        """
        执行登录请求

        Args:
            public_key: RSA公钥
            nonce: 随机字符串

        Returns:
            Dict[str, Any]: 登录成功后的用户信息

        Raises:
            LibraryCredentialsError: 上游明确返回用户名或密码错误
            LibraryLoginError: HTTP、响应格式或其他业务认证错误
        """
        try:
            # 加密密码
            encrypted_password = PasswordEncryptor.encrypt_with_public_key(
                PasswordEncryptor.set_public_key(public_key),
                f"{self.password};{nonce}"
            )

            # 发送登录请求
            login_data = {
                "logonName": self.username,
                "password": encrypted_password,
                "captcha": "",
                "privacy": True,
            }
            login_resp = self.session.post(self.login_url, json=login_data)

            if login_resp.status_code != 200:
                error_msg = f"登录请求失败：HTTP {login_resp.status_code}"
                log_with_user('error', self.username, '登录', error_msg)
                raise LibraryLoginError(error_msg)

            login_result = login_resp.json()
            if login_result.get('code') != 0:
                code = login_result.get('code')
                message = str(login_result.get('message') or '未知错误')
                log_with_user('error', self.username, '登录', f"登录失败: code {code}: {message}")
                error_type = LibraryCredentialsError if _is_credentials_error(message) else LibraryLoginError
                raise error_type(message, code=code)

            user_info = login_result.get('data')
            if not isinstance(user_info, dict) or not user_info:
                error_msg = "登录响应缺少用户信息"
                log_with_user('error', self.username, '登录', error_msg)
                raise LibraryLoginError(error_msg)

            return user_info
        except LibraryLoginError:
            raise
        except Exception as e:
            error_msg = f"登录请求异常：{str(e)}"
            log_with_user('error', self.username, '登录', error_msg)
            raise LibraryLoginError(error_msg) from e

    def _set_user_cookie(self, user_info: Dict[str, Any]) -> None:
        """
        设置用户Cookie

        Args:
            user_info: 用户信息字典
        """
        try:
            cookie_value = (
                f"userid={user_info['accNo']};"
                f"username={user_info['logonName']};"
                f"usernumber={user_info['cardNo']};"
                f"token={user_info['token']}"
            )
            self.session.cookies.set(
                'ic-cookie',
                cookie_value,
                domain='njfu.edu.cn',
                path='/'
            )
        except Exception as e:
            print(f"设置用户Cookie时发生异常: {str(e)}")

    def get_user_info(self) -> Optional[Dict[str, Any]]:
        """
        获取用户信息

        Returns:
            Optional[Dict[str, Any]]: 用户信息字典，包含必要的用户字段
        """
        try:
            self.ensure_login()
            if not self.user_info:
                return None

            return {
                'uuid': self.user_info['uuid'],
                'accNo': self.user_info['accNo'],
                'pid': self.user_info['pid'],
                'logonName': self.user_info['logonName'],
                'trueName': self.user_info['trueName'],
                'className': self.user_info['className'],
                'sex': self.user_info['sex'],
                'deptName': self.user_info['deptName'],
                'token': self.user_info['token']
            }
        except Exception as e:
            print(f"获取用户信息失败: {str(e)}")
            return None

    @staticmethod
    def get_reservation_time(begin_time: str = "10:30", end_time: str = "22:00") -> Tuple[str, str]:
        """
        生成预约时间

        Args:
            begin_time: 开始时间，格式 HH:MM
            end_time: 结束时间，格式 HH:MM

        Returns:
            Tuple[str, str]: (开始时间, 结束时间)，格式 YYYY-MM-DD HH:MM:SS
        """
        tomorrow = datetime.now() + timedelta(days=1)
        date_str = tomorrow.strftime("%Y-%m-%d")
        return (
            f"{date_str} {begin_time}:00",
            f"{date_str} {end_time}:00"
        )

    def _reserve_single_seat(
        self,
        user_info: Dict[str, Any],
        seat_id: str,
        resv_begin_time: str,
        resv_end_time: str
    ) -> str:
        """
        预约单个座位，并把这一次尝试的结果记进 reserve_attempts。

        每一次下单都是对那张座位的一次精确探测：开枪时刻 + 图书馆的判定，
        合起来就是「开闸后第 N 毫秒，这张座位还活着吗」。这是选座模型唯一的
        一手数据来源，以前只进容器日志、会被滚掉，现在留下来。

        记账固定走 finally，所以成功、失败、抛异常三条路都不会漏；而且记在拿到
        响应之后，不会拖慢这一次下单。

        Args:
            user_info: 用户信息
            seat_id: 座位ID
            resv_begin_time: 预约开始时间
            resv_end_time: 预约结束时间

        Returns:
            str: 预约结果消息
        """
        seat_name = self.get_seat_name_by_id(seat_id)
        fired_at = datetime.now()
        message = ""
        try:
            message = self._post_reservation(
                user_info, seat_id, seat_name, resv_begin_time, resv_end_time
            )
            return message
        finally:
            attempt_log.record(
                pid=self.username,
                seat_name=seat_name,
                dev_id=seat_id,
                resv_begin_time=resv_begin_time,
                resv_end_time=resv_end_time,
                fired_at=fired_at,
                message=message,
            )

    def _post_reservation(
        self,
        user_info: Dict[str, Any],
        seat_id: str,
        seat_name: str,
        resv_begin_time: str,
        resv_end_time: str
    ) -> str:
        """真正发那一个下单请求。调用方负责记账，这里只管下单和返回那句话。"""
        # 准备预约数据
        resv_data = {
            "testName": "",
            "appAccNo": user_info['accNo'],
            "memberKind": 1,
            "resvDev": [seat_id],
            "resvMember": [user_info['accNo']],
            "resvProperty": 0,
            "sysKind": 8,
            "resvBeginTime": resv_begin_time,
            "resvEndTime": resv_end_time
        }

        # 发送预约请求
        try:
            response = self.session.post(self.reserve_url, json=resv_data)
            log_with_user('info', self.username, '预约请求',
                         f"座位 {seat_name}({seat_id}) 响应状态码: {response.status_code}")

            # 记录响应内容用于调试
            log_with_user('debug', self.username, '预约响应',
                         f"座位 {seat_name}({seat_id}) 响应内容: {response.text}")

            if response.status_code in (401, 403) or (
                    response.status_code in (301, 302, 303, 307, 308)):
                error_msg = (f"座位 {seat_name}({seat_id}) 请求失败: {AUTH_FAILURE_MARK}"
                             f"（HTTP {response.status_code}）")
                log_with_user('error', self.username, '预约失败', error_msg)
                return error_msg
            if response.status_code != 200:
                error_msg = f"座位 {seat_name}({seat_id}) 请求失败: 状态码 {response.status_code}"
                log_with_user('error', self.username, '预约失败', error_msg)
                return error_msg

            # 处理响应结果
            try:
                result = response.json()
            except ValueError:
                # 会话没了的时候 webvpn/CAS 会把 POST 重定向到登录页，回来的是 HTML。
                body = (response.text or "")[:200].lower()
                if "<html" in body or "login" in body or "cas" in body:
                    error_msg = f"座位 {seat_name}({seat_id}) 请求失败: {AUTH_FAILURE_MARK}（返回登录页）"
                else:
                    error_msg = f"座位 {seat_name}({seat_id}) 请求失败: 响应不是 JSON"
                log_with_user('error', self.username, '预约失败', error_msg)
                return error_msg
            target_time = f"{resv_begin_time[:10]} {resv_begin_time[11:]}-{resv_end_time[11:]}"

            if result.get('code') == 0:
                # 预约成功
                success_info = result['data']
                dev_info = (success_info.get('resvDevInfoList') or [{}])[0]
                actual_seat_name = dev_info.get('devName') or seat_name
                success_msg = (
                    f"✅ {resv_begin_time[5:10]} · "
                    f"{resv_begin_time[11:16]}-{resv_end_time[11:16]} · "
                    f"{actual_seat_name} · 预约成功"
                )
                log_with_user('info', self.username, '预约成功',
                             f"座位 {seat_name}({seat_id}) 预约成功: {success_msg}")
                self.last_reservation = {
                    "uuid": str(success_info.get('uuid') or ''),
                    # devId 用我们发出去的那个，不用响应里的：要约回“同一张座位”，
                    # 以请求为准最可靠，响应里的字段名还随版本变过。
                    "dev_id": str(seat_id),
                    "dev_name": actual_seat_name,
                    "resv_begin_time": resv_begin_time,
                    "resv_end_time": resv_end_time,
                }
                try:
                    # 归属一律用登录学号：user_info['pid'] 是图书馆自己的内部人员 ID，
                    # 多数人恰好和学号相同，不同的那几个会被注册到一个不存在的账号上，
                    # 复查时找不到凭据直接判 failed，迟到保护静默失效。
                    register_arrival_check(
                        self.username,
                        str(success_info.get('uuid') or ''),
                        actual_seat_name,
                        resv_begin_time,
                        resv_end_time,
                    )
                except Exception as exc:
                    # 复查任务注册失败不能把已经成功的图书馆预约报告成失败。
                    log_with_user('error', self.username, '到馆复查',
                                  f"注册到馆复查失败: {exc}")
                return success_msg
            else:
                # 预约失败
                reason = str(result.get('message', '未知错误'))
                if any(mark in reason for mark in ("未登录", "请先登录", "登录失效", "登录过期", "token")):
                    reason = f"{AUTH_FAILURE_MARK}（{reason}）"
                error_msg = f"座位 {seat_name}({seat_id}) 期望预约时间{target_time} 预约失败: {reason}"
                log_with_user('error', self.username, '预约失败', error_msg)
                return error_msg

        except requests.exceptions.RequestException as e:
            error_msg = f"座位 {seat_name}({seat_id}) 网络请求异常: {str(e)}"
            log_with_user('error', self.username, '预约异常', error_msg)
            return error_msg
        except Exception as e:
            error_msg = f"座位 {seat_name}({seat_id}) 预约过程异常: {str(e)}"
            log_with_user('error', self.username, '预约异常', error_msg)
            return error_msg

    def reserve_seat(
        self,
        seat_list: List[str],
        resv_begin_time: str,
        resv_end_time: str
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        预约指定座位列表

        Args:
            seat_list: 座位ID列表
            resv_begin_time: 预约开始时间
            resv_end_time: 预约结束时间

        Returns:
            Tuple[str, Optional[Dict[str, Any]]]: (预约结果消息, 用户信息)
        """
        # 上一段的结果必须先清掉：调用方靠它判断“这一单落在哪张座位、uuid 是多少”，
        # 留着残留会让失败的一单被当成成功的那一单去取消。
        self.last_reservation = None
        # 调用方靠它决定要不要把这一段整个放回队列稍后重打。
        self.last_segment_rejected = False
        try:
            # 确保登录状态
            self.ensure_login()
            if not self.user_info:
                error_msg = "用户信息获取失败，无法进行预约"
                log_with_user('error', self.username, '预约', error_msg)
                return error_msg, None

            # 尝试预约每个座位
            failed_seats = []
            relogged = False
            self_conflict = False
            target_time = (f"{resv_begin_time[:10]} "
                           f"{resv_begin_time[11:]}-{resv_end_time[11:]}")
            for index, seat_id in enumerate(seat_list):
                seat_name = self.get_seat_name_by_id(seat_id)
                log_with_user('info', self.username, '预约', f"尝试预约座位: {seat_name}({seat_id})")

                busy_retries = 0
                while True:
                    res_message = self._reserve_single_seat(
                        self.user_info,
                        seat_id,
                        resv_begin_time,
                        resv_end_time
                    )
                    if _is_account_busy(res_message) and busy_retries < BUSY_RETRY_MAX:
                        # 账号级锁还没放开，跳下一张只会再被顶一次；等一下打同一张。
                        busy_retries += 1
                        log_with_user('warning', self.username, '预约',
                                      f"座位 {seat_name}({seat_id}) 账号有预约操作进行中，"
                                      f"{BUSY_RETRY_DELAY_SECONDS:g}s 后重试同一张（第 {busy_retries} 次）")
                        time.sleep(BUSY_RETRY_DELAY_SECONDS)
                        continue
                    if _is_auth_failure(res_message) and not relogged:
                        # 预登录会话被服务端踢了（7:00 为省一个来回没校验就直接开枪）。
                        # 现场重登一次，原座位再打；再失败就按普通失败处理。
                        relogged = True
                        log_with_user('warning', self.username, '预约',
                                      f"座位 {seat_name}({seat_id}) 会话已失效，现场重新登录后重试")
                        self.user_info = None
                        self._initialize_login()
                        if not self.user_info:
                            return "会话失效且重新登录失败，无法进行预约", None
                        continue
                    break

                if "预约成功" in res_message:
                    log_with_user('info', self.username, '预约', f"座位 {seat_name}({seat_id}) 预约成功")
                    return res_message, self.user_info
                else:
                    failed_seats.append(f"{seat_name}({seat_id}): {res_message}")
                    log_with_user('warning', self.username, '预约',
                                 f"座位 {seat_name}({seat_id}) 预约失败: {res_message}")
                    if _is_self_conflict(res_message):
                        # 账号在这一段已经有预约了（多半是用户自己在图书馆那边约的）。
                        # 这是账号级判定，剩下的备选座位一张张打过去只会收到同一句话，
                        # 白烧请求，还白烧掉 7:00 开闸后最值钱的那几百毫秒。
                        self_conflict = True
                        skipped = len(seat_list) - index - 1
                        if skipped:
                            log_with_user('warning', self.username, '预约',
                                          f"该账号在 {target_time} 已有预约，"
                                          f"跳过其余 {skipped} 张备选座位")
                        break
                    if _is_rejected(res_message):
                        # 请求没被受理，这张座位到底空不空根本没问到。网关正在拒我们，
                        # 接着打备选只会继续被拒，还把首选的优先级丢了：就地收手，
                        # 让调用方稍后把整段从第一张座位重来。
                        self.last_segment_rejected = True
                        skipped = len(seat_list) - index - 1
                        log_with_user('warning', self.username, '预约',
                                      f"{target_time} 这一段的请求未被受理"
                                      f"{'，跳过其余 ' + str(skipped) + ' 张备选座位' if skipped else ''}"
                                      f"，交回队列稍后重打")
                        break

            # 一张都没约上
            if failed_seats:
                if self_conflict:
                    error_msg = (f"该账号在 {target_time} 时段已有预约，无需也无法再约"
                                 f"（若不是本系统约的，多半是你自己在图书馆系统里约过）。"
                                 f"详细原因:\n" + "\n".join(failed_seats))
                elif self.last_segment_rejected:
                    error_msg = (f"{REJECTED_MARK}（网关/网络拒绝，未能问到座位）。"
                                 f"详细原因:\n" + "\n".join(failed_seats))
                else:
                    error_msg = f"所有座位预约失败，详细原因:\n" + "\n".join(failed_seats)
                log_with_user('error', self.username, '预约', error_msg)
                return error_msg, self.user_info
            else:
                error_msg = "没有可用的座位进行预约"
                log_with_user('error', self.username, '预约', error_msg)
                return error_msg, self.user_info

        except Exception as e:
            # 登录/网络层直接抛上来的，同样算没受理，让调用方稍后整段重来。
            self.last_segment_rejected = isinstance(e, requests.exceptions.RequestException)
            error_msg = f"预约过程出现异常: {str(e)}"
            log_with_user('error', self.username, '预约', error_msg)
            return error_msg, None

    def delete_seat(self, uuid: str) -> Tuple[bool, str]:
        """
        删除预约座位
        
        Args:
            uuid: 预约记录的UUID
            
        Returns:
            Tuple[bool, str]: (是否成功, 结果消息)
        """
        try:
            delete_url = f"{self.base_url}ic-web/reserve/delete{self.vpn_suffix}"
            response = self.session.post(
                delete_url,
                json={"uuid": uuid},
                headers={"Content-Type": "application/json;charset=UTF-8"}
            )

            print(f"删除座位 {uuid} 响应状态码: {response.status_code}")
            print(f"删除座位 {uuid} 响应内容: {response.text}")

            if response.status_code != 200:
                return False, f"删除座位请求失败: 状态码 {response.status_code}"

            result = response.json()
            if result.get('code') == 0:
                try:
                    db.arrival_checks.update_one(
                        {"uuid": uuid, "status": "pending"},
                        {"$set": {
                            "status": "cancelled",
                            "checked_at": datetime.now(),
                            "message": "预约已主动取消",
                        }},
                    )
                except Exception as exc:
                    log_with_user('warning', self.username, '到馆复查',
                                  f"取消复查任务失败: {exc}")
                return True, "删除座位成功"
            return False, f"删除座位失败: {result.get('message')}"

        except Exception as e:
            error_msg = f"删除座位时发生异常: {str(e)}"
            print(error_msg)
            return False, error_msg

    # reserve/operate/rec 的 kind 位，文字照图书馆网页「操作记录」的操作类型一列；
    # consoleKind 是操作端：1 系统、8 闸机、16 电脑端、32 现场预约台。
    # 2「已生效」是系统到点把预约置为生效，人还没进馆；真正的签到是 4（闸机）。
    OPERATE_KIND_NAMES = {
        1: "预约成功",
        2: "已生效",
        4: "已签到",
        8: "暂离",
        16: "返回",
        32: "已结束",
    }

    # creditRec/getOwn 的 status，文字照图书馆网页上显示的
    CREDIT_STATUS_NAMES = {1: "已记录", 2: "已取消，已违约"}

    def get_operate_records(
        self,
        resv_id: int,
        page_num: int = 1,
        page_size: int = 50
    ) -> Tuple[Optional[List[Dict[str, Any]]], str]:
        """
        查询一条预约的操作流水（个人预约页「操作记录」按钮背后的接口）。

        只读；比 resvInfo 的 resvStatus 多出精确的签到/暂离/返回/结束时间。

        Args:
            resv_id: 预约的 resvId（不是 uuid）
            page_num: 页码
            page_size: 每页条数

        Returns:
            Tuple[Optional[List[Dict[str, Any]]], str]: (按时间升序的流水, 结果消息)
            每条含 kind / kind_name / console_kind / create_time(datetime) / uuid / sid / dev_name
        """
        try:
            self.ensure_login()
            if not self.user_info:
                return None, "图书馆登录失败，无法查询操作记录"

            url = f"{self.base_url}ic-web/reserve/operate/rec{self.vpn_suffix}"
            params = {
                "resvId": resv_id,
                "pageNum": page_num,
                "pageSize": page_size,
                "orderKey": "createTime",
                "orderModel": "desc",
            }
            try:
                response = self.session.get(url, params=params)
                result = response.json()
            except Exception as e:
                return None, f"查询请求失败: {str(e)}"

            if response.status_code != 200:
                return None, f"查询请求失败: 状态码 {response.status_code}"
            if result.get('code') != 0:
                return None, f"查询失败: {result.get('message', '未知错误')}"

            records = []
            for item in result.get('data') or []:
                kind = item.get('kind')
                create_ms = item.get('createTime')
                records.append({
                    "uuid": item.get('uuid'),
                    "sid": item.get('sid'),
                    "resvId": item.get('resvId'),
                    "kind": kind,
                    "kind_name": self.OPERATE_KIND_NAMES.get(kind, f"未知({kind})"),
                    "console_kind": item.get('consoleKind'),
                    "dev_name": item.get('devName'),
                    "create_time": (datetime.fromtimestamp(create_ms / 1000)
                                    if create_ms else None),
                })
            records.sort(key=lambda r: r["create_time"] or datetime.min)
            return records, "查询成功" if records else "无操作记录"

        except Exception as e:
            return None, f"查询操作记录时发生异常: {str(e)}"

    CREDIT_SEAT_KEY = "8"   # classKind 8 = 座位；surPlus 按 classKind 分组给分数

    def get_credit_summary(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        查询座位信用分：ic-web/creditPunishRec/surPlus。

        只读，用户在设置页主动点才查，定时任务不碰它。

        Returns:
            Tuple[Optional[Dict[str, Any]], str]: ({"score": 当前分, "total": 满分}, 结果消息)
        """
        try:
            self.ensure_login()
            if not self.user_info:
                return None, "图书馆登录失败，无法查询信用分"

            url = f"{self.base_url}ic-web/creditPunishRec/surPlus{self.vpn_suffix}"
            try:
                response = self.session.get(url)
                result = response.json()
            except Exception as e:
                return None, f"查询请求失败: {str(e)}"

            if response.status_code != 200:
                return None, f"查询请求失败: 状态码 {response.status_code}"
            if result.get('code') != 0:
                return None, f"查询失败: {result.get('message', '未知错误')}"

            data = result.get('data') or {}
            key = self.CREDIT_SEAT_KEY
            return {
                "score": data.get(key),
                "total": data.get(f"total{key}"),
            }, "查询成功"

        except Exception as e:
            return None, f"查询信用分时发生异常: {str(e)}"

    def get_credit_records(
        self,
        page: int = 1,
        page_num: int = 10
    ) -> Tuple[Optional[List[Dict[str, Any]]], str, int]:
        """
        查询扣分记录：ic-web/creditRec/getOwn，按时间倒序。

        Returns:
            Tuple[Optional[List[Dict[str, Any]]], str, int]: (记录列表, 结果消息, 总条数)
            每条含 kind_name / dev_name / score / status / created_at(datetime) / memo
        """
        try:
            self.ensure_login()
            if not self.user_info:
                return None, "图书馆登录失败，无法查询扣分记录", 0

            url = f"{self.base_url}ic-web/creditRec/getOwn{self.vpn_suffix}"
            try:
                response = self.session.get(url, params={"page": page, "pageNum": page_num})
                result = response.json()
            except Exception as e:
                return None, f"查询请求失败: {str(e)}", 0

            if response.status_code != 200:
                return None, f"查询请求失败: 状态码 {response.status_code}", 0
            if result.get('code') != 0:
                return None, f"查询失败: {result.get('message', '未知错误')}", 0

            records = []
            for item in result.get('data') or []:
                created_ms = item.get('gmtCreate')
                records.append({
                    "kind_id": item.get('creditKindId'),
                    "kind_name": item.get('creditKindName') or "",
                    "dev_name": item.get('devName') or "",
                    "score": item.get('thisUseScore'),
                    "status": item.get('status'),
                    "status_name": self.CREDIT_STATUS_NAMES.get(item.get('status'), ""),
                    "created_at": (datetime.fromtimestamp(created_ms / 1000)
                                   if created_ms else None),
                    "memo": item.get('memo') or "",
                })
            return records, "查询成功", int(result.get('count') or len(records))

        except Exception as e:
            return None, f"查询扣分记录时发生异常: {str(e)}", 0

    def insert_or_update_mongo(
        self,
        collection_name: str,
        pid: str,
        data: Dict[str, Any],
        upsert: bool = True
    ) -> bool:
        """
        更新或插入MongoDB数据
        
        Args:
            collection_name: 集合名称 ('user_config_info' 或 'users')
            pid: 用户ID
            data: 要更新的数据
            upsert: 是否在记录不存在时插入
            
        Returns:
            bool: 操作是否成功
            
        Raises:
            ValueError: 当集合名称无效时抛出
        """
        # 选择集合
        collection = None
        if collection_name == 'user_config_info':
            collection = user_config_info
        elif collection_name == 'users':
            collection = users_col
        else:
            raise ValueError(f"无效的集合名称: {collection_name}")
            
        if collection is None:
            raise ValueError(f"无法获取集合: {collection_name}")
            
        try:
            # 添加更新时间
            data['updated_at'] = datetime.now()
            
            # 执行更新
            result = collection.update_one(
                {"pid": pid},
                {"$set": data},
                upsert=upsert
            )
            return bool(result.modified_count > 0 or result.upserted_id is not None)
        except Exception as e:
            print(f"MongoDB操作失败: {str(e)}")
            return False

    def get_reservation_info(
        self,
        begin_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        page_num: int = 10
    ) -> Tuple[Optional[List[Dict[str, Any]]], str]:
        """
        查询预约信息
        
        Args:
            begin_date: 开始日期，默认今天
            end_date: 结束日期，默认3天后
            page: 页码
            page_num: 每页记录数
            
        Returns:
            Tuple[Optional[List[Dict[str, Any]]], str]: (预约信息列表, 结果消息)
        """
        try:
            # 确保登录状态
            self.ensure_login()
            if not self.user_info:
                return None, "图书馆登录失败，无法查询预约信息"

            # 设置查询时间范围
            if not begin_date:
                begin_date = datetime.now().strftime("%Y-%m-%d")
            if not end_date:
                end_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

            # 发送查询请求
            query_url = f"{self.base_url}ic-web/reserve/resvInfo{self.vpn_suffix}"
            params = {
                "beginDate": begin_date,
                "endDate": end_date,
                "needStatus": 7,  # 待签到(1)+使用中(2)+暂离(4)
                "page": page,
                "pageNum": page_num,
                "orderKey": "gmt_create",
                "orderModel": "desc"
            }
            
            try:
                response = self.session.get(query_url, params=params)
                result = response.json()
            except Exception as e:
                self._clear_owned_seat()
                return None, f"查询请求失败: {str(e)}"

            # 处理查询失败
            if response.status_code != 200:
                self._clear_owned_seat()
                return None, f"查询请求失败: 状态码 {response.status_code}"

            if result.get('code') != 0:
                self._clear_owned_seat()
                return None, f"查询失败: {result.get('message', '未知错误')}"

            # 处理查询结果
            data_list = result.get('data', [])
            if not data_list:
                self._clear_owned_seat()
                return [], "无预约记录"

            try:
                # 格式化数据
                formatted_data, owned_seat = self._format_reservation_data(data_list)

                # get_reservation_info 会重建 owned_seat；保留本地流程元数据，
                # 否则迟到保护生成的新预约会在下一次查询后丢失标记并被二次保护。
                existing = user_config_info.find_one(
                    {"pid": self.username}, {"owned_seat": 1}
                ) or {}
                local_metadata = {
                    seat.get("uuid"): {"by_protection": True}
                    for seats in (existing.get("owned_seat") or {}).values()
                    for seat in seats
                    if seat.get("uuid") and seat.get("by_protection")
                }
                for seats in owned_seat.values():
                    for seat in seats:
                        seat.update(local_metadata.get(seat.get("uuid"), {}))
                
                # 更新数据库
                if not self.insert_or_update_mongo(
                    'user_config_info',
                    self.username,
                    {"owned_seat": owned_seat},
                    upsert=True
                ):
                    print("更新预约信息到数据库失败")
                
                return formatted_data, "查询成功"
            except Exception as e:
                self._clear_owned_seat()
                return None, f"处理预约数据失败: {str(e)}"

        except Exception as e:
            self._clear_owned_seat()
            error_msg = f"查询预约信息时发生异常: {str(e)}"
            print(error_msg)
            return None, error_msg

    def _clear_owned_seat(self) -> None:
        """
        清空用户的座位信息
        
        在查询失败或发生异常时调用，确保用户配置被正确清空。
        """
        try:
            if self.username:
                self.insert_or_update_mongo(
                    'user_config_info',
                    self.username,
                    {"owned_seat": {}},
                    upsert=True
                )
        except Exception as e:
            print(f"清空座位信息失败: {str(e)}")

    def _format_reservation_data(
        self,
        data_list: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
        """
        格式化预约数据
        
        Args:
            data_list: 原始预约数据列表
            
        Returns:
            Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]: 
                (格式化的预约列表, 座位信息字典)
        """
        formatted_data = []
        owned_seat = {}
        
        for item in data_list:
            # 处理时间
            begin_time = datetime.fromtimestamp(int(item.get('resvBeginTime', 0)) / 1000)
            end_time = datetime.fromtimestamp(int(item.get('resvEndTime', 0)) / 1000)
            target_time = (
                f"{begin_time.strftime('%Y-%m-%d %H:%M:%S')}-"
                f"{end_time.strftime('%H:%M:%S')}"
            )

            # 处理设备信息
            dev_info_list = item.get('resvDevInfoList', [])
            dev_info = dev_info_list[0] if dev_info_list else {}
            dev_name = dev_info.get('devName', '') if dev_info_list else ''

            # 构建座位信息
            seat_dict = {
                "uuid": item.get('uuid', ''),
                "target_time": target_time,
                "resvStatus": str(item.get('resvStatus', ''))
            }

            # 更新座位字典
            if dev_name:
                if dev_name not in owned_seat:
                    owned_seat[dev_name] = []
                owned_seat[dev_name].append(seat_dict)

            # 构建格式化数据
            formatted_item = {
                "uuid": item.get('uuid', ''),
                "resvId": item.get('resvId'),
                "resvBeginTime": begin_time.strftime("%Y-%m-%d %H:%M:%S"),
                "resvEndTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "resvStatus": item.get('resvStatus', ''),
                "resvName": item.get('resvName', ''),
                "devInfo": dev_info,
            }
            formatted_data.append(formatted_item)

        return formatted_data, owned_seat
