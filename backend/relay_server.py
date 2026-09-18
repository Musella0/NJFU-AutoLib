"""
手机中继服务：收着每个用户 App 的 WebSocket，替 api / scheduler 把图书馆请求送到手机上发。

为什么单独一个进程：api 是 gunicorn 单 worker 多线程，scheduler 是 APScheduler 阻塞循环，
两边都要用手机连接，而 WebSocket 连接只能活在一个进程里。让它俩都通过内部 HTTP 找这里，
容器之间一跳不到 1ms。

对外（经 Caddy，路径 /relay/*）：
    GET /relay/ws      App 连上来。Authorization: Bearer <令牌>（与 API 同一套令牌）→ 绑定学号；
                       不带令牌 → 匿名连接，只能被同一客户端随后的 verify 请求使用（见 bind）。
对内（只在 compose 网络里，Caddy 不转发 /internal/*，带 X-Forwarded-For 的请求一律拒）：
    POST /internal/request   {pid | relay_session, method, url, headers, body, timeout}
                             → {status, reason, headers, body}   手机不在线 503 / 超时 504 / 手机发不出去 502
    POST /internal/bind      {relay_session, pid}   把匿名连接认领给学号
    GET  /internal/online    在线手机列表
    GET  /internal/online/{pid}

WebSocket 帧（JSON 文本）：
    服务器 → 手机  {"type":"welcome","pid":..,"relay_session":..,"heartbeat_seconds":30}
                   {"type":"request","id":..,"method":..,"url":..,"headers":[[k,v]..],"body":b64|null,"timeout_ms":..}
    手机 → 服务器  {"type":"hello","app_version":..,"network":..}            可选，只是记日志
                   {"type":"response","id":..,"status":..,"reason":..,"headers":[[k,v]..],"body":b64}
                   {"type":"error","id":..,"message":..}
手机端要求：不跟随跳转、不存 cookie、把响应体解压后再回、去掉 Content-Encoding /
Content-Length / Transfer-Encoding。参考实现见 relay_client.py。
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from aiohttp import WSMsgType, web
from pymongo import MongoClient

from utils.config import get_mongo_uri

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger("relay")

LISTEN_PORT = int(os.getenv("RELAY_PORT", "5005"))
INTERNAL_TOKEN = os.getenv("RELAY_INTERNAL_TOKEN", "")
# Cloudflare 大约 100 秒没数据就掐 WebSocket，心跳要明显短于它
HEARTBEAT_SECONDS = float(os.getenv("RELAY_HEARTBEAT_SECONDS", "30"))
# 平面图之类的响应有几百 KB，给足
MAX_MESSAGE_BYTES = int(os.getenv("RELAY_MAX_MESSAGE_BYTES", str(16 * 1024 * 1024)))

_mongo = MongoClient(get_mongo_uri(), serverSelectionTimeoutMS=5000)
_db = _mongo["autolib"]


def _uid_for_token(token: str) -> Optional[str]:
    """和 main.py 的 _uid_from_token 同一套：库里只有 sha256，过期自己再判一次。"""
    row = _db.auth_tokens.find_one({"token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest()})
    if not row:
        return None
    expires_at = row.get("expires_at")
    if expires_at and expires_at <= datetime.now():
        return None
    return row.get("uid")


class Phone:
    def __init__(self, ws: web.WebSocketResponse, pid: Optional[str], relay_session: str, remote: str) -> None:
        self.ws = ws
        self.pid = pid
        self.relay_session = relay_session
        self.remote = remote
        self.connected_at = datetime.now()
        self.info: Dict[str, Any] = {}
        self.pending: Dict[str, asyncio.Future] = {}
        self.requests_done = 0

    def label(self) -> str:
        return self.pid or f"匿名:{self.relay_session[:8]}"

    def describe(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "online": True,
            "connected_at": self.connected_at.isoformat(timespec="seconds"),
            "app_version": self.info.get("app_version"),
            "network": self.info.get("network"),
            "requests_done": self.requests_done,
            "in_flight": len(self.pending),
        }

    def fail_pending(self, message: str) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError(message))
        self.pending.clear()


class Hub:
    def __init__(self) -> None:
        self.by_pid: Dict[str, Phone] = {}
        self.by_session: Dict[str, Phone] = {}

    async def register(self, phone: Phone) -> None:
        self.by_session[phone.relay_session] = phone
        if phone.pid:
            await self._claim_pid(phone)

    async def _claim_pid(self, phone: Phone) -> None:
        old = self.by_pid.get(phone.pid)
        self.by_pid[phone.pid] = phone
        if old is not None and old is not phone:
            # 同一学号新连接顶掉旧的：App 重连时旧连接往往还没被判死
            logger.info("学号 %s 有新连接，关闭旧连接 %s", phone.pid, old.remote)
            old.fail_pending("手机重新连接，请求作废")
            try:
                await old.ws.close(code=4001, message=b"replaced by a newer connection")
            except Exception:
                pass

    async def bind(self, relay_session: str, pid: str) -> bool:
        phone = self.by_session.get(relay_session)
        if phone is None:
            return False
        phone.pid = pid
        await self._claim_pid(phone)
        return True

    def unregister(self, phone: Phone) -> None:
        if self.by_session.get(phone.relay_session) is phone:
            del self.by_session[phone.relay_session]
        if phone.pid and self.by_pid.get(phone.pid) is phone:
            del self.by_pid[phone.pid]

    def find(self, pid: Optional[str], relay_session: Optional[str]) -> Optional[Phone]:
        # 匿名连接号优先：verify 时手机还没绑学号，只有它能定位
        if relay_session and relay_session in self.by_session:
            return self.by_session[relay_session]
        if pid:
            return self.by_pid.get(pid)
        return None


hub = Hub()


# ==================== 手机侧 ====================

async def ws_handler(request: web.Request) -> web.StreamResponse:
    token = _bearer(request) or request.query.get("token", "").strip()
    pid: Optional[str] = None
    if token:
        loop = asyncio.get_running_loop()
        pid = await loop.run_in_executor(None, _uid_for_token, token)
        if not pid:
            raise web.HTTPUnauthorized(text="令牌无效或已过期")

    ws = web.WebSocketResponse(heartbeat=HEARTBEAT_SECONDS, max_msg_size=MAX_MESSAGE_BYTES)
    await ws.prepare(request)

    remote = request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0].strip()
    phone = Phone(ws, pid, secrets.token_urlsafe(16), remote)
    await hub.register(phone)
    logger.info("手机上线 %s 来自 %s", phone.label(), remote)
    await ws.send_json({
        "type": "welcome",
        "pid": pid,
        "relay_session": phone.relay_session,
        "heartbeat_seconds": HEARTBEAT_SECONDS,
    })

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    logger.warning("%s 发来非 JSON 帧，忽略", phone.label())
                    continue
                _on_phone_message(phone, data)
            elif msg.type == WSMsgType.ERROR:
                logger.warning("%s 连接异常: %s", phone.label(), ws.exception())
    finally:
        hub.unregister(phone)
        phone.fail_pending("手机连接已断开")
        logger.info("手机下线 %s（转发 %d 次）", phone.label(), phone.requests_done)
    return ws


def _on_phone_message(phone: Phone, data: Dict[str, Any]) -> None:
    kind = data.get("type")
    if kind == "hello":
        phone.info = {k: data.get(k) for k in ("app_version", "network", "device")}
        logger.info("%s 自报: %s", phone.label(), phone.info)
        return
    if kind in ("response", "error"):
        future = phone.pending.pop(str(data.get("id")), None)
        if future is None or future.done():
            return  # 已超时放弃的请求迟到了
        if kind == "response":
            future.set_result(data)
        else:
            future.set_exception(RuntimeError(str(data.get("message") or "手机端未说明原因")))
        return
    logger.debug("%s 发来未知帧类型 %r", phone.label(), kind)


def _bearer(request: web.Request) -> Optional[str]:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


# ==================== 内部接口 ====================

@web.middleware
async def internal_guard(request: web.Request, handler):
    if request.path.startswith("/internal/"):
        # Caddy 转发的请求一定带 X-Forwarded-For；内部接口只许 compose 网络里直连
        if "X-Forwarded-For" in request.headers:
            raise web.HTTPForbidden(text="internal only")
        if INTERNAL_TOKEN and request.headers.get("X-Relay-Token") != INTERNAL_TOKEN:
            raise web.HTTPForbidden(text="bad relay token")
    return await handler(request)


async def internal_request(request: web.Request) -> web.Response:
    payload = await request.json()
    pid = payload.get("pid")
    phone = hub.find(pid, payload.get("relay_session"))
    if phone is None or phone.ws.closed:
        return web.json_response({"error": f"学号 {pid} 的手机不在线"}, status=503)

    timeout = float(payload.get("timeout") or 30)
    req_id = uuid.uuid4().hex
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    phone.pending[req_id] = future
    try:
        await phone.ws.send_json({
            "type": "request",
            "id": req_id,
            "method": payload["method"],
            "url": payload["url"],
            "headers": payload.get("headers") or [],
            "body": payload.get("body"),
            "timeout_ms": int(timeout * 1000),
        })
        data = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        phone.pending.pop(req_id, None)
        return web.json_response({"error": f"{timeout:g} 秒内手机没有回响应"}, status=504)
    except ConnectionError as exc:
        return web.json_response({"error": str(exc)}, status=503)
    except RuntimeError as exc:
        # 手机端发请求失败（校园网断了 / DNS / 证书）
        return web.json_response({"error": str(exc)}, status=502)
    except Exception as exc:
        phone.pending.pop(req_id, None)
        logger.exception("转发给 %s 时出错", phone.label())
        return web.json_response({"error": str(exc)}, status=500)

    phone.requests_done += 1
    return web.json_response({
        "status": data.get("status"),
        "reason": data.get("reason", ""),
        "headers": data.get("headers") or [],
        "body": data.get("body") or "",
    })


async def internal_bind(request: web.Request) -> web.Response:
    payload = await request.json()
    ok = await hub.bind(str(payload.get("relay_session", "")), str(payload.get("pid", "")))
    if not ok:
        return web.json_response({"error": "没有这个匿名连接"}, status=404)
    logger.info("匿名连接认领为学号 %s", payload.get("pid"))
    return web.json_response({"ok": True})


async def internal_online(request: web.Request) -> web.Response:
    return web.json_response({"phones": [p.describe() for p in hub.by_pid.values()]})


async def internal_online_one(request: web.Request) -> web.Response:
    phone = hub.by_pid.get(request.match_info["pid"])
    if phone is None or phone.ws.closed:
        return web.json_response({"pid": request.match_info["pid"], "online": False})
    return web.json_response(phone.describe())


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "phones": len(hub.by_pid)})


def make_app() -> web.Application:
    app = web.Application(middlewares=[internal_guard], client_max_size=MAX_MESSAGE_BYTES)
    app.router.add_get("/relay/ws", ws_handler)
    app.router.add_get("/relay/healthz", healthz)
    app.router.add_post("/internal/request", internal_request)
    app.router.add_post("/internal/bind", internal_bind)
    app.router.add_get("/internal/online", internal_online)
    app.router.add_get("/internal/online/{pid}", internal_online_one)
    return app


if __name__ == "__main__":
    logger.info("relay 监听 :%d，心跳 %gs", LISTEN_PORT, HEARTBEAT_SECONDS)
    web.run_app(make_app(), port=LISTEN_PORT, access_log=None)
