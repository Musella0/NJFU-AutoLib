"""
经用户自己的手机访问图书馆。

2026-09-17 起 CAS 对 webvpn 要短信动态码，服务器再也登不上；校园网直连 libseat 走 CAS
却不用短信码。让每个用户的 App 在校园 WiFi 上挂一条 WebSocket 到 relay 服务，服务器
把这个用户对图书馆的每一个 HTTP 请求原样推给手机发出、把响应原样收回来——一行业务
逻辑不动，只是「从哪个网口出去」变了。每个人的流量从自己的手机、自己的学号出去，
互相之间不牵连。

三种出口，由环境变量 LIBRARY_ACCESS 选：
    webvpn  经 webvpn 网关（默认，2026-09-17 之前的行为，自己部署的人什么都不用改）
    direct  服务器直连 libseat / authserver（只有校园网里的机器能用；测试用）
    phone   经该用户手机转发，本模块的主角

phone 模式下 BaseSystem 建会话时按学号挂上 PhoneRelayAdapter，只接管 libseat 和
authserver 两个域名，其余请求（邮件、LLM）照常直连。适配器把 requests 准备好的请求
POST 到 relay 的内部接口，relay 找到该学号的手机连接推过去，拿回状态码/响应头/响应体
后再拼回 requests.Response——cookie、跳转都还是 requests 自己在管，手机端不跟随跳转、
不存 cookie，只是一根透明的网线。
"""

import base64
import io
import os
import threading
from contextlib import contextmanager
from http.client import HTTPMessage
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from requests.adapters import BaseAdapter
from requests.cookies import extract_cookies_to_jar
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers

LIBRARY_ACCESS_MODES = ("webvpn", "direct", "phone")

# 图书馆这两个域名的请求才经手机；其余照常直连
LIBRARY_HOSTS = ("libseat.njfu.edu.cn", "authserver.njfu.edu.cn")

RELAY_INTERNAL_URL = os.getenv("RELAY_INTERNAL_URL", "http://relay:5005").rstrip("/")
RELAY_INTERNAL_TOKEN = os.getenv("RELAY_INTERNAL_TOKEN", "")
# 手机那头没回时 relay 自己会按请求超时放弃；这里再多等几秒是给 relay 那一跳的余量
RELAY_HOP_GRACE_SECONDS = float(os.getenv("RELAY_HOP_GRACE_SECONDS", "5"))
DEFAULT_PHONE_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "30"))

# 逐跳头：手机端已经把响应体解压成实体，这些头再带回来只会误导 requests
_HOP_BY_HOP_RESPONSE_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive",
}

PHONE_OFFLINE_MESSAGE = "手机不在线：AutoLib App 没有连上服务器，无法经手机访问图书馆"


def library_access() -> str:
    mode = os.getenv("LIBRARY_ACCESS", "webvpn").strip().lower() or "webvpn"
    if mode not in LIBRARY_ACCESS_MODES:
        raise ValueError(f"LIBRARY_ACCESS 只能是 {LIBRARY_ACCESS_MODES} 之一，现在是 {mode!r}")
    return mode


def uses_webvpn() -> bool:
    return library_access() == "webvpn"


class PhoneOfflineError(requests.exceptions.ConnectionError):
    """该学号的手机没有连着 relay。文案直接给用户看，别再包一层。"""

    def __init__(self, pid: str, detail: str = "") -> None:
        self.pid = pid
        message = PHONE_OFFLINE_MESSAGE
        if detail:
            message = f"{message}（{detail}）"
        super().__init__(message)


class PhoneTimeoutError(requests.exceptions.ReadTimeout):
    """手机收到了请求但没在期限内回响应（校园网卡、图书馆不响应、App 被冻结）。"""


class RelayUnavailableError(requests.exceptions.ConnectionError):
    """relay 服务本身没起来或回了不认识的东西——部署问题，不是用户问题。"""


# verify 接口用：手机还没拿到令牌时先匿名连上 relay，把「匿名连接号」带在请求头里，
# 本线程随后建的会话就经这条匿名连接转发。见 relay_server.py 的 bind。
_route_override = threading.local()


@contextmanager
def relay_session_override(relay_session: Optional[str]):
    previous = getattr(_route_override, "relay_session", None)
    _route_override.relay_session = relay_session
    try:
        yield
    finally:
        _route_override.relay_session = previous


def current_relay_session() -> Optional[str]:
    return getattr(_route_override, "relay_session", None)


def mount_library_route(session: requests.Session, pid: str) -> None:
    """phone 模式下给会话挂上经手机转发的适配器；其他模式什么都不做。"""
    if library_access() != "phone":
        return
    adapter = PhoneRelayAdapter(pid, relay_session=current_relay_session())
    for host in LIBRARY_HOSTS:
        session.mount(f"https://{host}", adapter)
        session.mount(f"http://{host}", adapter)


def _total_timeout(timeout: Any) -> float:
    if timeout is None:
        return DEFAULT_PHONE_TIMEOUT
    if isinstance(timeout, (tuple, list)):
        return float(sum(float(part) for part in timeout if part is not None))
    return float(timeout)


def _request_body_b64(body: Any) -> Optional[str]:
    if body is None:
        return None
    if isinstance(body, str):
        body = body.encode("utf-8")
    elif not isinstance(body, (bytes, bytearray)):
        # 文件/生成器：一次性读完。图书馆接口没有大上传，这里不会撑爆内存。
        body = b"".join(
            chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
            for chunk in body
        )
    return base64.b64encode(bytes(body)).decode("ascii")


class _RawBody(io.BytesIO):
    """requests 从 Response.raw._original_response.msg 里抽 Set-Cookie，喂它一个像样的。"""

    def __init__(self, body: bytes, headers: Iterable[Tuple[str, str]]) -> None:
        super().__init__(body)
        msg = HTTPMessage()
        for name, value in headers:
            msg.add_header(name, value)
        self._original_response = SimpleNamespace(msg=msg)

    def release_conn(self) -> None:  # requests 关闭响应时会找这个
        pass


def build_response(
    request: requests.PreparedRequest,
    status: int,
    headers: Iterable[Tuple[str, str]],
    body: bytes,
    reason: str = "",
) -> requests.Response:
    """把手机传回来的三件套拼成 requests.Response，跟 HTTPAdapter.build_response 同款。"""
    header_pairs = [
        (name, value) for name, value in headers
        if name.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
    ]
    response = requests.Response()
    response.status_code = int(status)
    response.reason = reason
    merged: CaseInsensitiveDict = CaseInsensitiveDict()
    for name, value in header_pairs:
        # 同名头（典型是多条 Set-Cookie）按 urllib3 的做法用逗号拼起来；
        # cookie 真正的解析走下面 _RawBody 里那份逐条的 HTTPMessage。
        merged[name] = f"{merged[name]}, {value}" if name in merged else value
    response.headers = merged
    response.encoding = get_encoding_from_headers(response.headers)
    response.raw = _RawBody(body, header_pairs)
    response._content = body
    response._content_consumed = True
    response.url = request.url
    response.request = request
    extract_cookies_to_jar(response.cookies, request, response.raw)
    return response


class PhoneRelayAdapter(BaseAdapter):
    """把请求交给 relay，由该学号的手机在校园网上发出。"""

    def __init__(self, pid: str, relay_session: Optional[str] = None) -> None:
        super().__init__()
        self.pid = pid
        self.relay_session = relay_session
        self._hub = requests.Session()
        if RELAY_INTERNAL_TOKEN:
            self._hub.headers["X-Relay-Token"] = RELAY_INTERNAL_TOKEN

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        total = _total_timeout(timeout)
        payload: Dict[str, Any] = {
            "pid": self.pid,
            "relay_session": self.relay_session,
            "method": request.method,
            "url": request.url,
            "headers": [[name, value] for name, value in request.headers.items()],
            "body": _request_body_b64(request.body),
            "timeout": total,
        }
        try:
            hub_resp = self._hub.post(
                f"{RELAY_INTERNAL_URL}/internal/request",
                json=payload,
                timeout=total + RELAY_HOP_GRACE_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            raise RelayUnavailableError(f"relay 服务不可达: {exc}") from exc

        if hub_resp.status_code == 503:
            raise PhoneOfflineError(self.pid, _error_detail(hub_resp))
        if hub_resp.status_code == 504:
            raise PhoneTimeoutError(
                f"手机转发超时（{total:g} 秒内没有回响应）: {_error_detail(hub_resp)}"
            )
        if hub_resp.status_code == 502:
            # 手机那头发请求就失败了（校园网断了、DNS 不通、证书问题），
            # 对上层来说等同于普通的连接失败，让重试逻辑照常处理。
            raise requests.exceptions.ConnectionError(
                f"手机访问图书馆失败: {_error_detail(hub_resp)}"
            )
        if hub_resp.status_code != 200:
            raise RelayUnavailableError(
                f"relay 返回 HTTP {hub_resp.status_code}: {hub_resp.text[:200]}"
            )

        try:
            data = hub_resp.json()
            status = int(data["status"])
            headers = [(str(k), str(v)) for k, v in data.get("headers", [])]
            body = base64.b64decode(data.get("body") or "")
        except (ValueError, KeyError, TypeError) as exc:
            raise RelayUnavailableError(f"relay 响应格式不对: {exc}") from exc
        return build_response(request, status, headers, body, reason=str(data.get("reason", "")))

    def close(self) -> None:
        self._hub.close()


def _error_detail(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("error", "")) or resp.text[:200]
    except ValueError:
        return resp.text[:200]


def phone_status(pid: str) -> Dict[str, Any]:
    """问 relay 这个学号的手机在不在线。relay 不可达时也回一个「离线」，附带原因。"""
    headers = {"X-Relay-Token": RELAY_INTERNAL_TOKEN} if RELAY_INTERNAL_TOKEN else {}
    try:
        resp = requests.get(
            f"{RELAY_INTERNAL_URL}/internal/online/{pid}", headers=headers, timeout=3,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"online": False, "error": f"relay HTTP {resp.status_code}"}
    except requests.exceptions.RequestException as exc:
        return {"online": False, "error": f"relay 不可达: {exc}"}


def phones_online() -> List[Dict[str, Any]]:
    headers = {"X-Relay-Token": RELAY_INTERNAL_TOKEN} if RELAY_INTERNAL_TOKEN else {}
    resp = requests.get(f"{RELAY_INTERNAL_URL}/internal/online", headers=headers, timeout=3)
    resp.raise_for_status()
    return resp.json().get("phones", [])


def bind_relay_session(relay_session: str, pid: str) -> bool:
    """验证通过后把匿名连接认领给该学号，App 拿到令牌重连之前就能继续用。"""
    headers = {"X-Relay-Token": RELAY_INTERNAL_TOKEN} if RELAY_INTERNAL_TOKEN else {}
    try:
        resp = requests.post(
            f"{RELAY_INTERNAL_URL}/internal/bind",
            json={"relay_session": relay_session, "pid": pid},
            headers=headers,
            timeout=3,
        )
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False
