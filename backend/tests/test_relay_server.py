import asyncio
import base64
import json
import logging
import sys
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

# relay_server 一导入就建 MongoClient；测试里不要数据库，令牌校验直接打桩
fake_pymongo = types.ModuleType("pymongo")
fake_pymongo.MongoClient = Mock(return_value=MagicMock())
sys.modules.setdefault("pymongo", fake_pymongo)
fake_config = types.ModuleType("utils.config")
fake_config.get_mongo_uri = Mock(return_value="mongodb://test")
sys.modules.setdefault("utils.config", fake_config)

try:
    from aiohttp import ClientSession, WSMsgType
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("aiohttp 未安装")

import relay_server

logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("relay").setLevel(logging.WARNING)


class _PhoneScript:
    """一部假手机：收到 request 帧后按脚本回 response / error 帧，或者干脆不回。"""

    def __init__(self, ws, behaviour):
        self.ws = ws
        self.behaviour = behaviour
        self.welcome = None
        self.requests_seen = []

    async def run_once(self):
        msg = await self.ws.receive()
        assert msg.type == WSMsgType.TEXT, msg
        data = json.loads(msg.data)
        if data["type"] == "welcome":
            self.welcome = data
            return data
        if data["type"] == "request":
            self.requests_seen.append(data)
            reply = self.behaviour(data)
            if reply is not None:
                await self.ws.send_json(reply)
            return data
        return data


def _response_frame(req, status=200, headers=None, body=b""):
    return {
        "type": "response", "id": req["id"], "status": status, "reason": "OK",
        "headers": headers or [], "body": base64.b64encode(body).decode("ascii"),
    }


class RelayServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        relay_server.hub = relay_server.Hub()
        relay_server.INTERNAL_TOKEN = ""
        self.client = TestClient(TestServer(relay_server.make_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def _connect(self, token=None, behaviour=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        ws = await self.client.ws_connect("/relay/ws", headers=headers)
        phone = _PhoneScript(ws, behaviour or (lambda req: _response_frame(req)))
        await phone.run_once()  # welcome
        return phone

    async def test_request_round_trip_through_authenticated_phone(self):
        with patch.object(relay_server, "_uid_for_token", return_value="2024001"):
            phone = await self._connect(
                token="tok",
                behaviour=lambda req: _response_frame(
                    req, 302, [["Location", "/next"], ["Set-Cookie", "a=1"], ["Set-Cookie", "b=2"]], b"x"),
            )
        self.assertEqual(phone.welcome["pid"], "2024001")

        async def ask():
            resp = await self.client.post("/internal/request", json={
                "pid": "2024001", "method": "GET", "url": "https://libseat.njfu.edu.cn/ic-web/x",
                "headers": [["Cookie", "s=1"]], "body": None, "timeout": 5,
            })
            return resp.status, await resp.json()

        (status, data), _ = await asyncio.gather(ask(), phone.run_once())
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], 302)
        self.assertEqual(data["headers"], [["Location", "/next"], ["Set-Cookie", "a=1"], ["Set-Cookie", "b=2"]])
        self.assertEqual(base64.b64decode(data["body"]), b"x")
        sent = phone.requests_seen[0]
        self.assertEqual(sent["url"], "https://libseat.njfu.edu.cn/ic-web/x")
        self.assertEqual(sent["headers"], [["Cookie", "s=1"]])
        self.assertEqual(sent["timeout_ms"], 5000)

        online = await (await self.client.get("/internal/online/2024001")).json()
        self.assertTrue(online["online"])
        self.assertEqual(online["requests_done"], 1)

    async def test_offline_phone_is_503(self):
        resp = await self.client.post("/internal/request", json={
            "pid": "nobody", "method": "GET", "url": "https://libseat.njfu.edu.cn/", "timeout": 1,
        })
        self.assertEqual(resp.status, 503)
        self.assertIn("不在线", (await resp.json())["error"])

    async def test_phone_error_frame_is_502_and_silence_is_504(self):
        with patch.object(relay_server, "_uid_for_token", return_value="2024001"):
            phone = await self._connect(
                token="tok",
                behaviour=lambda req: {"type": "error", "id": req["id"], "message": "ConnectionError: dns"},
            )

        async def ask(timeout):
            resp = await self.client.post("/internal/request", json={
                "pid": "2024001", "method": "GET", "url": "https://libseat.njfu.edu.cn/", "timeout": timeout,
            })
            return resp.status, await resp.json()

        (status, data), _ = await asyncio.gather(ask(5), phone.run_once())
        self.assertEqual(status, 502)
        self.assertIn("dns", data["error"])

        phone.behaviour = lambda req: None  # 手机收到了但不回
        (status, data), _ = await asyncio.gather(ask(0.2), phone.run_once())
        self.assertEqual(status, 504)

    async def test_anonymous_connection_then_bind(self):
        phone = await self._connect(token=None)
        self.assertIsNone(phone.welcome["pid"])
        relay_session = phone.welcome["relay_session"]

        async def ask(payload):
            resp = await self.client.post("/internal/request", json={
                "method": "GET", "url": "https://authserver.njfu.edu.cn/authserver/login", "timeout": 5, **payload,
            })
            return resp.status

        # 只报学号找不到它；带匿名连接号才能用
        self.assertEqual(await ask({"pid": "2024001"}), 503)
        (status, _) = await asyncio.gather(ask({"pid": "2024001", "relay_session": relay_session}), phone.run_once())
        self.assertEqual(status, 200)

        bind = await self.client.post("/internal/bind", json={"relay_session": relay_session, "pid": "2024001"})
        self.assertEqual(bind.status, 200)
        (status, _) = await asyncio.gather(ask({"pid": "2024001"}), phone.run_once())
        self.assertEqual(status, 200)

    async def test_newer_connection_replaces_older_for_same_pid(self):
        with patch.object(relay_server, "_uid_for_token", return_value="2024001"):
            old = await self._connect(token="tok")
            new = await self._connect(token="tok")
        closed = await old.ws.receive()
        self.assertEqual(closed.type, WSMsgType.CLOSE)
        self.assertEqual(relay_server.hub.by_pid["2024001"].relay_session, new.welcome["relay_session"])

    async def test_bad_token_is_rejected(self):
        with patch.object(relay_server, "_uid_for_token", return_value=None):
            with self.assertRaises(Exception):
                await self.client.ws_connect("/relay/ws", headers={"Authorization": "Bearer nope"})

    async def test_internal_endpoints_refuse_proxied_requests(self):
        resp = await self.client.get("/internal/online", headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(resp.status, 403)
        relay_server.INTERNAL_TOKEN = "secret"
        self.assertEqual((await self.client.get("/internal/online")).status, 403)
        self.assertEqual((await self.client.get("/internal/online", headers={"X-Relay-Token": "secret"})).status, 200)


if __name__ == "__main__":
    unittest.main()
