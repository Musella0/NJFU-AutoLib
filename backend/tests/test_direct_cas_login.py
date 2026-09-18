"""直连（不经 webvpn）的 CAS 登录：vpn_system 的直连路径 + library_system 在 SSO 链里填表。"""

import os
import sys
import types
import unittest
from unittest.mock import Mock, patch

os.environ["LIBRARY_ACCESS"] = "direct"

fake_pymongo = types.ModuleType("pymongo")
fake_pymongo.MongoClient = Mock(return_value=Mock())
fake_pymongo.ASCENDING = 1
fake_pymongo.DESCENDING = -1
sys.modules.setdefault("pymongo", fake_pymongo)

fake_config = types.ModuleType("utils.config")
fake_config.get_mongo_uri = Mock(return_value="mongodb://test")
fake_config.LOG_FILE = "/tmp/autolib-test.log"
sys.modules.setdefault("utils.config", fake_config)

fake_apscheduler = types.ModuleType("apscheduler")
fake_apscheduler_schedulers = types.ModuleType("apscheduler.schedulers")
fake_apscheduler_background = types.ModuleType("apscheduler.schedulers.background")
fake_apscheduler_background.BackgroundScheduler = Mock
sys.modules.setdefault("apscheduler", fake_apscheduler)
sys.modules.setdefault("apscheduler.schedulers", fake_apscheduler_schedulers)
sys.modules.setdefault("apscheduler.schedulers.background", fake_apscheduler_background)

import requests

from utils.phone_relay import build_response
from utils.vpn_system import VPNSystem
from utils.library_system import LibraryCredentialsError, LibrarySystem

CAS_PAGE = """<html><head><title>统一身份认证</title></head><body>
<form id="casLoginForm" method="post">
<input type="hidden" id="pwdDefaultEncryptSalt" value="0123456789abcdef">
<input type="hidden" name="lt" value="LT-42">
<input type="hidden" name="execution" value="e2s1">
</form></body></html>"""

CAS_PAGE_REJECTED = CAS_PAGE.replace("</form>", '<span id="msg" class="auth_error">用户名或密码错误</span></form>')
CAS_ENTRY = "https://authserver.njfu.edu.cn/authserver/login?service=http%3A%2F%2Flibseat.njfu.edu.cn%2Fic-web%2Fauth%2Ftoken"


def _resp(url, status=200, headers=(), body=b"", history=()):
    req = requests.Request("GET", url).prepare()
    resp = build_response(req, status, list(headers), body)
    resp.history = list(history)
    return resp


class _ScriptedSession:
    """按 (method, url 前缀) 回响应，记下每次 POST 的表单。"""

    def __init__(self, routes):
        self.routes = routes
        self.posts = []
        self.cookies = requests.cookies.RequestsCookieJar()

    def _dispatch(self, method, url, **kwargs):
        for (m, prefix), handler in self.routes:
            if m == method and url.startswith(prefix):
                return handler(url, kwargs) if callable(handler) else handler
        raise AssertionError(f"没有为 {method} {url} 准备响应")

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, data=None, **kwargs):
        self.posts.append((url, data, kwargs))
        return self._dispatch("POST", url, data=data, **kwargs)


def _address_ok(url, kwargs):
    return _resp(url, 200, [("Content-Type", "application/json")], ('{"code":0,"data":"%s"}' % CAS_ENTRY).encode())


@patch("utils.cas_login.PasswordEncryptor.aes_encrypt_password", return_value="ENC")
class DirectCasLoginTests(unittest.TestCase):
    def make_vpn(self, routes):
        vpn = VPNSystem("2024001", "pw")
        self.assertFalse(vpn.via_webvpn)
        vpn.session = _ScriptedSession(routes)
        return vpn

    def test_success_when_cas_issues_ticket(self, _enc):
        ticket_hop = _resp(CAS_ENTRY, 302, [("Location", "http://libseat.njfu.edu.cn/ic-web/auth/token?ticket=ST-1")])
        landed = _resp("https://libseat.njfu.edu.cn/ic-web/default/index", 200, [], b"<html>ok</html>", history=[ticket_hop])
        vpn = self.make_vpn([
            (("GET", "https://libseat.njfu.edu.cn/ic-web/auth/address"), _address_ok),
            (("GET", "https://authserver.njfu.edu.cn/authserver/login"), _resp(CAS_ENTRY, 200, [], CAS_PAGE.encode())),
            (("POST", "https://authserver.njfu.edu.cn/authserver/login"), landed),
        ])
        self.assertTrue(vpn.vpn_login())
        self.assertFalse(vpn.credentials_rejected)
        url, data, _ = vpn.session.posts[0]
        self.assertEqual(url, CAS_ENTRY)  # 表单交回带 service 的登录页地址
        self.assertEqual(data["username"], "2024001")
        self.assertEqual(data["password"], "ENC")
        self.assertEqual(data["lt"], "LT-42")
        self.assertEqual(data["execution"], "e2s1")  # 从页面读，不再写死 e1s1

    def test_rejected_password_is_flagged(self, _enc):
        vpn = self.make_vpn([
            (("GET", "https://libseat.njfu.edu.cn/ic-web/auth/address"), _address_ok),
            (("GET", "https://authserver.njfu.edu.cn/authserver/login"), _resp(CAS_ENTRY, 200, [], CAS_PAGE.encode())),
            (("POST", "https://authserver.njfu.edu.cn/authserver/login"), _resp(CAS_ENTRY, 200, [], CAS_PAGE_REJECTED.encode())),
        ])
        self.assertFalse(vpn.vpn_login())
        self.assertTrue(vpn.credentials_rejected)
        self.assertIn("密码错误", vpn.last_error)

    def test_existing_cas_session_lands_in_library_directly(self, _enc):
        vpn = self.make_vpn([
            (("GET", "https://libseat.njfu.edu.cn/ic-web/auth/address"), _address_ok),
            (("GET", "https://authserver.njfu.edu.cn/authserver/login"),
             _resp("https://libseat.njfu.edu.cn/ic-web/default/index", 200, [], b"<html>home</html>")),
        ])
        self.assertTrue(vpn.vpn_login())
        self.assertEqual(vpn.session.posts, [])

    def test_network_errors_propagate_instead_of_being_swallowed(self, _enc):
        def boom(url, kwargs):
            raise requests.exceptions.ConnectionError("手机不在线")
        vpn = self.make_vpn([(("GET", "https://libseat.njfu.edu.cn/ic-web/auth/address"), boom)])
        with self.assertRaises(requests.exceptions.ConnectionError):
            vpn.vpn_login()


@patch("utils.cas_login.PasswordEncryptor.aes_encrypt_password", return_value="ENC")
class SsoChainFillsCasFormTests(unittest.TestCase):
    def make_library(self, routes):
        library = LibrarySystem.__new__(LibrarySystem)
        library.username = "2024001"
        library.password = "pw"
        library.base_url = LibrarySystem.BASE_URL
        library.vpn_suffix = LibrarySystem.VPN_SUFFIX
        library.session = _ScriptedSession(routes)
        library._sso_error = ""
        library._sso_credentials_rejected = False
        return library

    def test_direct_mode_urls_have_no_webvpn_wrapping(self, _enc):
        self.assertEqual(LibrarySystem.BASE_URL, "https://libseat.njfu.edu.cn/")
        self.assertEqual(LibrarySystem.VPN_SUFFIX, "")
        library = self.make_library([])
        self.assertEqual(
            library._wrap_internal_url("http://libseat.njfu.edu.cn/ic-web/auth/token?ticket=ST-1"),
            "https://libseat.njfu.edu.cn/ic-web/auth/token?ticket=ST-1",
        )
        self.assertEqual(
            library._resolve_sso_url("/ic-web/default/index"),
            "https://libseat.njfu.edu.cn/ic-web/default/index",
        )

    def test_follower_submits_form_and_continues(self, _enc):
        library = self.make_library([
            (("GET", CAS_ENTRY), _resp(CAS_ENTRY, 200, [], CAS_PAGE.encode())),
            (("POST", CAS_ENTRY), _resp(CAS_ENTRY, 302, [("Location", "http://libseat.njfu.edu.cn/ic-web/auth/token?ticket=ST-1")])),
            (("GET", "https://libseat.njfu.edu.cn/ic-web/auth/token"),
             _resp("https://libseat.njfu.edu.cn/ic-web/auth/token", 302, [("Location", "/ic-web/default/index")])),
            (("GET", "https://libseat.njfu.edu.cn/ic-web/default/index"),
             _resp("https://libseat.njfu.edu.cn/ic-web/default/index", 200, [], b"<html>home</html>")),
        ])
        self.assertTrue(library._follow_sso_redirects(CAS_ENTRY))
        url, data, kwargs = library.session.posts[0]
        self.assertEqual(url, CAS_ENTRY)
        self.assertEqual(data["execution"], "e2s1")
        self.assertFalse(kwargs["allow_redirects"])

    def test_follower_reports_rejected_password_and_login_raises_credentials_error(self, _enc):
        library = self.make_library([
            (("GET", CAS_ENTRY), _resp(CAS_ENTRY, 200, [], CAS_PAGE.encode())),
            (("POST", CAS_ENTRY), _resp(CAS_ENTRY, 200, [], CAS_PAGE_REJECTED.encode())),
        ])
        self.assertFalse(library._follow_sso_redirects(CAS_ENTRY))
        self.assertTrue(library._sso_credentials_rejected)
        self.assertIn("拒绝密码", library._sso_error)

        with patch.object(library, "_login_via_cas_sso", side_effect=lambda: False):
            library._sso_credentials_rejected = True
            library._sso_error = "统一身份认证拒绝密码：用户名或密码错误"
            with self.assertRaises(LibraryCredentialsError):
                library._initialize_login()


if __name__ == "__main__":
    unittest.main()
