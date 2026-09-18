import base64
import json
import os
import unittest
from unittest.mock import Mock, patch

import requests

from utils import phone_relay
from utils.phone_relay import (
    PhoneOfflineError,
    PhoneRelayAdapter,
    PhoneTimeoutError,
    RelayUnavailableError,
    build_response,
    library_access,
    mount_library_route,
)


def _prepared(method="GET", url="https://libseat.njfu.edu.cn/ic-web/x", **kwargs):
    return requests.Request(method, url, **kwargs).prepare()


class BuildResponseTests(unittest.TestCase):
    """手机传回的三件套要拼成 requests 自己那种 Response：cookie 进罐、逐跳头不带。"""

    def test_multiple_set_cookie_headers_all_land_in_jar(self):
        req = _prepared()
        resp = build_response(req, 200, [
            ("Set-Cookie", "JSESSIONID=abc; Path=/"),
            ("Set-Cookie", "ic-cookie=def; Path=/ic-web"),
            ("Content-Type", "application/json;charset=UTF-8"),
        ], b'{"code":0}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.cookies["JSESSIONID"], "abc")
        self.assertEqual(resp.cookies["ic-cookie"], "def")
        self.assertEqual(resp.json(), {"code": 0})
        self.assertEqual(resp.encoding, "UTF-8")

    def test_hop_by_hop_headers_are_dropped(self):
        resp = build_response(_prepared(), 200, [
            ("Content-Encoding", "gzip"),
            ("Content-Length", "5"),
            ("Transfer-Encoding", "chunked"),
            ("X-Keep", "1"),
        ], b"hello")
        self.assertNotIn("Content-Encoding", resp.headers)
        self.assertNotIn("Content-Length", resp.headers)
        self.assertNotIn("Transfer-Encoding", resp.headers)
        self.assertEqual(resp.headers["X-Keep"], "1")
        self.assertEqual(resp.text, "hello")

    def test_redirect_is_recognised(self):
        resp = build_response(_prepared(), 302, [("Location", "/ic-web/auth/token?ticket=ST-1")], b"")
        self.assertTrue(resp.is_redirect)
        self.assertEqual(resp.headers["Location"], "/ic-web/auth/token?ticket=ST-1")


class _FakeHub:
    """替身 relay：按 URL 决定回什么，顺手记下服务器实际推给手机的请求。"""

    def __init__(self, script):
        self.script = script
        self.seen = []
        self.headers = {}

    def post(self, url, json=None, timeout=None):
        self.seen.append(json)
        status, payload = self.script(json)
        resp = Mock()
        resp.status_code = status
        resp.json = lambda: payload
        resp.text = str(payload)
        return resp

    def close(self):
        pass


def _ok(status, headers, body=b"", reason="OK"):
    return 200, {
        "status": status,
        "reason": reason,
        "headers": [list(h) for h in headers],
        "body": base64.b64encode(body).decode("ascii"),
    }


class AdapterTests(unittest.TestCase):
    def make_session(self, script):
        hub = _FakeHub(script)
        adapter = PhoneRelayAdapter("2024001")
        adapter._hub = hub
        session = requests.Session()
        session.mount("https://libseat.njfu.edu.cn", adapter)
        session.mount("https://authserver.njfu.edu.cn", adapter)
        return session, hub

    def test_session_follows_redirect_and_carries_cookie_through_phone(self):
        def script(payload):
            url = payload["url"]
            if url.startswith("https://authserver.njfu.edu.cn/authserver/login"):
                return _ok(302, [
                    ("Location", "https://libseat.njfu.edu.cn/ic-web/auth/token?ticket=ST-9"),
                    ("Set-Cookie", "CASTGC=TGT-1; Path=/authserver"),
                ])
            if url.startswith("https://libseat.njfu.edu.cn/ic-web/auth/token"):
                return _ok(302, [
                    ("Location", "/ic-web/default/index"),
                    ("Set-Cookie", "JSESSIONID=S1; Path=/"),
                ])
            if url.startswith("https://libseat.njfu.edu.cn/ic-web/default/index"):
                return _ok(200, [("Content-Type", "text/html")], b"<html>home</html>")
            raise AssertionError(f"unexpected url {url}")

        session, hub = self.make_session(script)
        resp = session.get("https://authserver.njfu.edu.cn/authserver/login?service=x")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "<html>home</html>")
        self.assertEqual(len(resp.history), 2)
        self.assertEqual(session.cookies.get("JSESSIONID"), "S1")
        self.assertEqual(session.cookies.get("CASTGC"), "TGT-1")
        # 第三跳是手机发的，Cookie 头由服务器这边的 requests 拼好一起推过去
        last = hub.seen[-1]
        cookie_header = dict(last["headers"]).get("Cookie", "")
        self.assertIn("JSESSIONID=S1", cookie_header)
        self.assertEqual(last["pid"], "2024001")

    def test_post_body_is_base64_and_timeout_is_total(self):
        session, hub = self.make_session(lambda p: _ok(200, [], b"ok"))
        session.post("https://libseat.njfu.edu.cn/ic-web/reserve", json={"a": 1}, timeout=(3, 7))
        sent = hub.seen[0]
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(json.loads(base64.b64decode(sent["body"])), {"a": 1})
        self.assertEqual(sent["timeout"], 10.0)

    def test_offline_timeout_and_phone_side_failure_map_to_requests_exceptions(self):
        session, _ = self.make_session(lambda p: (503, {"error": "学号 2024001 的手机不在线"}))
        with self.assertRaises(PhoneOfflineError) as ctx:
            session.get("https://libseat.njfu.edu.cn/ic-web/x")
        self.assertIn("手机不在线", str(ctx.exception))
        self.assertIsInstance(ctx.exception, requests.exceptions.ConnectionError)

        session, _ = self.make_session(lambda p: (504, {"error": "30 秒内手机没有回响应"}))
        with self.assertRaises(PhoneTimeoutError):
            session.get("https://libseat.njfu.edu.cn/ic-web/x")

        session, _ = self.make_session(lambda p: (502, {"error": "ConnectionError: dns"}))
        with self.assertRaises(requests.exceptions.ConnectionError) as ctx:
            session.get("https://libseat.njfu.edu.cn/ic-web/x")
        self.assertNotIsInstance(ctx.exception, PhoneOfflineError)

        session, _ = self.make_session(lambda p: (500, {"error": "boom"}))
        with self.assertRaises(RelayUnavailableError):
            session.get("https://libseat.njfu.edu.cn/ic-web/x")

    def test_relay_session_override_is_forwarded(self):
        hub = _FakeHub(lambda p: _ok(200, [], b""))
        with phone_relay.relay_session_override("anon-1"):
            adapter = PhoneRelayAdapter("2024001", relay_session=phone_relay.current_relay_session())
        adapter._hub = hub
        adapter.send(_prepared())
        self.assertEqual(hub.seen[0]["relay_session"], "anon-1")
        self.assertIsNone(phone_relay.current_relay_session())


class ModeTests(unittest.TestCase):
    def test_default_mode_is_webvpn_and_mounts_nothing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIBRARY_ACCESS", None)
            self.assertEqual(library_access(), "webvpn")
            session = requests.Session()
            mount_library_route(session, "2024001")
            self.assertNotIsInstance(session.get_adapter("https://libseat.njfu.edu.cn/x"), PhoneRelayAdapter)

    def test_phone_mode_mounts_only_library_hosts(self):
        with patch.dict(os.environ, {"LIBRARY_ACCESS": "phone"}):
            session = requests.Session()
            mount_library_route(session, "2024001")
            self.assertIsInstance(session.get_adapter("https://libseat.njfu.edu.cn/ic-web/x"), PhoneRelayAdapter)
            self.assertIsInstance(session.get_adapter("http://authserver.njfu.edu.cn/authserver/login"), PhoneRelayAdapter)
            self.assertNotIsInstance(session.get_adapter("https://api.resend.com/emails"), PhoneRelayAdapter)

    def test_unknown_mode_is_rejected_loudly(self):
        with patch.dict(os.environ, {"LIBRARY_ACCESS": "carrier-pigeon"}):
            with self.assertRaises(ValueError):
                library_access()


if __name__ == "__main__":
    unittest.main()
