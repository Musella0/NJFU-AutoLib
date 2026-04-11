"""
对称加密工具 — 用于加密存储用户凭据（vpn_password / lib_password）

使用 AES-256-GCM，密钥从环境变量 ENCRYPTION_KEY 读取（32 字节 hex 字符串）。
若未配置则启动时自动生成并打印警告（仅适用于开发环境）。
"""

import os
import base64
import logging

logger = logging.getLogger(__name__)

_PREFIX = "enc:"  # 加密字段前缀，用于区分明文和密文


def _get_key() -> bytes:
    """获取加密密钥（32 字节），优先从环境变量读取。"""
    hex_key = os.environ.get("ENCRYPTION_KEY", "")
    if hex_key:
        key = bytes.fromhex(hex_key)
        if len(key) != 32:
            raise ValueError("ENCRYPTION_KEY 必须是 64 位 hex 字符串（32 字节）")
        return key
    # 未配置则生成一个并警告
    key = os.urandom(32)
    logger.warning(
        "ENCRYPTION_KEY 未配置，已自动生成临时密钥。重启后密钥变化会导致已加密数据无法解密！"
        "请在 .env 中设置 ENCRYPTION_KEY=%s",
        key.hex(),
    )
    return key


_KEY: bytes | None = None


def _key() -> bytes:
    global _KEY
    if _KEY is None:
        _KEY = _get_key()
    return _KEY


def encrypt(plaintext: str) -> str:
    """加密明文，返回带前缀的 base64 密文字符串。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    aesgcm = AESGCM(_key())
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """解密密文，返回明文字符串。若非加密格式（无前缀）则原样返回（兼容旧数据）。"""
    if not ciphertext.startswith(_PREFIX):
        return ciphertext

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(ciphertext[len(_PREFIX):])
    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(_key())
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


def is_encrypted(value: str) -> bool:
    """判断字段是否已加密。"""
    return isinstance(value, str) and value.startswith(_PREFIX)
