"""
手机中继的参考客户端：在校园网里的任何一台机器上把自己变成某个学号的「出口」。

这是 relay_server.py 协议的 Python 实现，两个用途：
  1. Android 端写好之前，在 Termux / 笔记本上先把服务端整条链路（直连 CAS →
     libseat → 抢座）跑通；
  2. 给 Android 端当对照——手机侧要做的事全在 handle_request() 里，一共就四条：
     不跟随跳转、不存 cookie、响应体解压后原样回、去掉逐跳头。

用法（只依赖 requests 和 websockets）：
    pip install requests websockets
    python relay_client.py --server wss://你的域名/relay/ws --token <API 令牌>

令牌就是 /api/my/accounts/<pid>/verify 回的 token，也可以从 App 的存储里拿。
"""

import argparse
import asyncio
import base64
import json
import logging
import platform
import sys
import time
from typing import Any, Dict, List

import requests

try:
    import websockets
except ImportError:  # pragma: no cover
    sys.exit("缺 websockets：pip install websockets")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
logger = logging.getLogger("relay-client")

# 手机端不要回这些头：响应体已经解压成实体，长度也变了
STRIP_RESPONSE_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}
# requests 会自己算这些，不要照搬服务器给的
STRIP_REQUEST_HEADERS = {"content-length", "host"}


def handle_request(msg: Dict[str, Any]) -> Dict[str, Any]:
    """按服务器的要求发一次请求，把结果原样打包。Android 端照着这个写。"""
    headers = {k: v for k, v in msg.get("headers", []) if k.lower() not in STRIP_REQUEST_HEADERS}
    body = base64.b64decode(msg["body"]) if msg.get("body") else None
    timeout = max(1.0, msg.get("timeout_ms", 30000) / 1000.0)
    started = time.monotonic()
    try:
        # 每次新建 Session：不能让手机侧攒 cookie，那是服务器那边 requests 的活
        with requests.Session() as s:
            s.trust_env = False
            resp = s.request(
                msg["method"], msg["url"], headers=headers, data=body,
                allow_redirects=False, timeout=timeout,
            )
    except requests.exceptions.RequestException as exc:
        logger.warning("%s %s 失败: %s", msg["method"], _short(msg["url"]), exc)
        return {"type": "error", "id": msg["id"], "message": f"{type(exc).__name__}: {exc}"}

    # raw.headers 保留同名头（多条 Set-Cookie），resp.headers 会把它们拼成一条
    pairs: List[List[str]] = [
        [k, v] for k, v in resp.raw.headers.items() if k.lower() not in STRIP_RESPONSE_HEADERS
    ]
    logger.info("%s %s → %d (%.0f ms, %d B)", msg["method"], _short(msg["url"]), resp.status_code,
                (time.monotonic() - started) * 1000, len(resp.content))
    return {
        "type": "response",
        "id": msg["id"],
        "status": resp.status_code,
        "reason": resp.reason or "",
        "headers": pairs,
        "body": base64.b64encode(resp.content).decode("ascii"),
    }


def _short(url: str) -> str:
    # 日志里不带 query：CAS 的 ticket 之类不该落盘
    return url.split("?", 1)[0]


async def serve(server: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    backoff = 2
    while True:
        try:
            async with _connect(server, headers) as ws:
                backoff = 2
                await ws.send(json.dumps({
                    "type": "hello",
                    "app_version": "relay_client.py",
                    "network": "unknown",
                    "device": platform.platform(),
                }))
                loop = asyncio.get_running_loop()
                async for raw in ws:
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "welcome":
                        logger.info("已连上，学号 %s，匿名连接号 %s", msg.get("pid"), msg.get("relay_session"))
                    elif kind == "request":
                        # 放线程池里发，别让一个慢请求把心跳和别的请求都堵住
                        asyncio.ensure_future(_answer(ws, loop, msg))
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            logger.warning("连接断开: %s，%d 秒后重连", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def _connect(server: str, headers: Dict[str, str]):
    # websockets 13+ 叫 additional_headers，老版本叫 extra_headers
    try:
        return websockets.connect(server, additional_headers=headers, max_size=16 * 1024 * 1024)
    except TypeError:
        return websockets.connect(server, extra_headers=headers, max_size=16 * 1024 * 1024)


async def _answer(ws, loop, msg):
    reply = await loop.run_in_executor(None, handle_request, msg)
    try:
        await ws.send(json.dumps(reply))
    except websockets.exceptions.WebSocketException as exc:
        logger.warning("回响应时连接已断: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", required=True, help="wss://<域名>/relay/ws")
    parser.add_argument("--token", default="", help="API 令牌；不给就是匿名连接（只能配合 verify 用）")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.server, args.token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
