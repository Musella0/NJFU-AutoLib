"""Encrypted local storage for the administrator account."""

import json
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from werkzeug.security import check_password_hash, generate_password_hash

from utils.crypto import decrypt, encrypt


ADMIN_CREDENTIALS_FILE = os.environ.get(
    "ADMIN_CREDENTIALS_FILE",
    "/app/data/admin_credentials.enc",
)


class AdminCredentialError(Exception):
    """The encrypted administrator credential file is invalid or unreadable."""


def validate_username(username: str) -> Optional[str]:
    username = (username or "").strip()
    if not 3 <= len(username) <= 64:
        return "管理员账号长度须为 3–64 位"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        return "管理员账号只能包含字母、数字、点、下划线和连字符"
    return None


def validate_email(email: str) -> Optional[str]:
    email = (email or "").strip()
    if len(email) > 254 or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        email,
    ):
        return "请输入有效的管理员邮箱"
    return None


def validate_password(password: str) -> Optional[str]:
    if len(password or "") < 8:
        return "密码至少需要 8 位"
    if len(password) > 128:
        return "密码不能超过 128 位"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "密码必须同时包含字母和数字"
    return None


def _validate_record(record: Any) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise AdminCredentialError("管理员凭据文件格式无效")
    required = ("username", "email", "password_hash", "auth_version")
    if any(not isinstance(record.get(key), str) or not record[key] for key in required):
        raise AdminCredentialError("管理员凭据文件缺少必要字段")
    return record


def load_credentials() -> Optional[Dict[str, Any]]:
    """Load and decrypt the local credential record, or return None if absent."""
    if not os.path.isfile(ADMIN_CREDENTIALS_FILE):
        return None
    try:
        with open(ADMIN_CREDENTIALS_FILE, "r", encoding="utf-8") as handle:
            encrypted = handle.read().strip()
        if not encrypted:
            raise AdminCredentialError("管理员凭据文件为空")
        return _validate_record(json.loads(decrypt(encrypted)))
    except AdminCredentialError:
        raise
    except Exception as exc:
        raise AdminCredentialError("管理员凭据文件无法解密或已损坏") from exc


def _write_credentials(record: Dict[str, Any]) -> None:
    directory = os.path.dirname(ADMIN_CREDENTIALS_FILE) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass

    payload = encrypt(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    temp_path = (
        f"{ADMIN_CREDENTIALS_FILE}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    )
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, ADMIN_CREDENTIALS_FILE)
        os.chmod(ADMIN_CREDENTIALS_FILE, 0o600)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def save_credentials(
    username: str,
    email: str,
    password: str,
    *,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Create or replace the local administrator credential record."""
    username = (username or "").strip()
    email = (email or "").strip().lower()
    for error in (
        validate_username(username),
        validate_email(email),
        validate_password(password),
    ):
        if error:
            raise ValueError(error)
    if not overwrite and os.path.exists(ADMIN_CREDENTIALS_FILE):
        raise FileExistsError("管理员凭据已存在")

    now = datetime.now(timezone.utc).isoformat()
    existing = load_credentials() if overwrite else None
    record = {
        "version": 1,
        "username": username,
        "email": email,
        "password_hash": generate_password_hash(password),
        "auth_version": secrets.token_hex(16),
        "created_at": (existing or {}).get("created_at", now),
        "updated_at": now,
    }
    _write_credentials(record)
    return record


def update_password(record: Dict[str, Any], new_password: str) -> Dict[str, Any]:
    """Replace the password hash and rotate the session version."""
    error = validate_password(new_password)
    if error:
        raise ValueError(error)
    updated = dict(_validate_record(record))
    updated["password_hash"] = generate_password_hash(new_password)
    updated["auth_version"] = secrets.token_hex(16)
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_credentials(updated)
    return updated


def verify_login(username: str, password: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Verify an administrator login without exposing which field was wrong."""
    record = load_credentials()
    if not record:
        return False, None
    username_matches = secrets.compare_digest(
        (username or "").strip(),
        record["username"],
    )
    password_matches = check_password_hash(record["password_hash"], password or "")
    return username_matches and password_matches, record
