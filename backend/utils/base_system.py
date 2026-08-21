import os

import  requests

# (连接超时, 读取超时)，单位秒。requests 默认不设超时，一个不响应的上游
# 能把调用线程永久挂住；并发抢座时几条线程一起卡死，线程池 join 不完，
# 当天的定时任务就再也结束不了，后面几天的抢座也跟着停摆。
CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "30"))
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)


class TimeoutSession(requests.Session):
    """给每个请求兜一个默认超时；显式传了 timeout 的调用仍按自己的值走。"""

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return super().request(*args, **kwargs)


def log(*args):
    """
    统一打印日志函数。

    :param args: 打印的内容
    :return: None
    """
    # print(*args)
    pass

class BaseSystem:
    def __init__(self, username, password, base_url, vpn_suffix):
        """
        初始化基础系统类。

        :param username: 用户名
        :param password: 密码
        :param base_url: 基础 URL
        :param vpn_suffix: VPN 后缀
        """
        self.session = TimeoutSession()
        self.username = username
        self.password = password
        self.base_url = base_url
        self.vpn_suffix = vpn_suffix

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def get_response(self, url, params=None):
        """
        通用 GET 请求方法。

        :param url: 请求的 URL
        :param params: 请求参数
        :return: 响应对象
        """
        try:
            response = self.session.get(url, params=params)
            log(f"请求 {url} 的响应状态码:", response.status_code)
            return response
        except Exception as e:
            log(f"请求 {url} 时发生异常:", str(e))
            return None

    def post_request(self, url, data=None, json=None):
        """
        通用 POST 请求方法。

        :param url: 请求的 URL
        :param data: 表单数据
        :param json: JSON 数据
        :return: 响应对象
        """
        try:
            response = self.session.post(url, data=data, json=json)
            log(f"请求 {url} 的响应状态码:", response.status_code)
            return response
        except Exception as e:
            log(f"请求 {url} 时发生异常:", str(e))
            return None
