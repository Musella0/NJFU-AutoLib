"""
预登录会话池

7:00 抢座的时间几乎全花在登录链路上：webvpn 登录 + CAS SSO 跳转链要 4~8 秒，
真正的 reserve 请求往返只有 200~300 毫秒。图书馆 7:00 才开放**预约**，但**认证**
接口全天可用（日志里 20:00、22:00 的任务同样能登录成功），所以把登录挪到 6:50
提前跑完，7:00 直接发预约请求。

安全底线：这里任何一步失败，都只能让抢座退回「7:00 现场登录」的老路径，
绝不能让当天的预约失败。所以 take() 从不抛异常——拿不到就返回 None，
调用方照旧自己 new 一个 LibrarySystem。
"""

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 预登录总开关。关掉之后 take() 永远返回 None，行为与加这个功能之前完全一致。
ENABLED = os.getenv("PRELOGIN_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

# 会话最长存活时间。正常流程里 6:50 存、7:00 取，只有 10 分钟；
# 超过这个岁数说明调度出了意外（比如当天的抢座任务压根没跑），一律丢弃重登。
MAX_AGE_SECONDS = float(os.getenv("PRELOGIN_MAX_AGE_SECONDS", "1800"))

_pool: Dict[str, Tuple[Any, float]] = {}
_lock = threading.Lock()


def _log(level: str, user: str, operation: str, message: str) -> None:
    getattr(logger, level)(message, extra={'user': user, 'operation': operation})


def store(pid: str, library: Any) -> None:
    """存入一个已完成登录的 LibrarySystem。"""
    with _lock:
        _pool[pid] = (library, time.monotonic())


def discard(pid: str) -> None:
    """丢弃某个账号的预登录会话。"""
    with _lock:
        _pool.pop(pid, None)


def clear() -> None:
    """清空整个池子。抢座跑完必须调用，避免过期会话漏进后面的任务。"""
    with _lock:
        count = len(_pool)
        _pool.clear()
    if count:
        _log('info', '系统', '预登录', f"已清空预登录会话池（{count} 个）")


def pooled_pids() -> List[str]:
    """当前池中的账号列表（快照）。"""
    with _lock:
        return list(_pool)


def _pop_fresh(pid: str) -> Optional[Any]:
    """取出会话并做岁数检查；过期的直接丢掉。"""
    with _lock:
        entry = _pool.pop(pid, None)
    if entry is None:
        return None
    library, stored_at = entry
    age = time.monotonic() - stored_at
    if age > MAX_AGE_SECONDS:
        _log('warning', pid, '预登录',
             f"预登录会话已存放 {age:.0f} 秒，超过上限 {MAX_AGE_SECONDS:.0f} 秒，丢弃重登")
        return None
    return library


def is_alive(pid: str) -> bool:
    """
    校验池中会话是否仍然有效，失效的顺手清出池子。

    给 7:00 前 30 秒的复查任务用：这时候发现会话死了还来得及重登，
    比等到 7:00:00 才发现要从容得多。
    """
    library = _pop_fresh(pid)
    if library is None:
        return False
    if not _verify(pid, library):
        return False
    store(pid, library)
    return True


def take(pid: str) -> Optional[Any]:
    """
    取出可用的预登录会话；没有、过期或已失效都返回 None。

    这个函数在 7:00:00 的关键路径上，任何异常都必须被吞掉——
    宁可退回现场登录慢 8 秒，也不能让预约直接崩掉。
    """
    if not ENABLED:
        return None
    try:
        library = _pop_fresh(pid)
        if library is None:
            return None
        if not _verify(pid, library):
            return None
        return library
    except Exception as exc:  # pragma: no cover - 兜底，理论上 _verify 已吞完异常
        _log('warning', pid, '预登录', f"取用预登录会话异常，回退现场登录: {exc}")
        return None


def _verify(pid: str, library: Any) -> bool:
    """一次轻量的 userInfo 请求（~200ms），确认会话没被服务端踢掉。"""
    try:
        if library.verify_session():
            return True
        _log('warning', pid, '预登录', "预登录会话已失效，回退到现场登录")
        return False
    except Exception as exc:
        _log('warning', pid, '预登录', f"校验预登录会话异常，回退现场登录: {exc}")
        return False
