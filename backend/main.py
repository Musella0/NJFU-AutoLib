import os
import secrets
import time
from functools import wraps
from threading import Lock
from flask import Flask, render_template, jsonify, request, session
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient, DESCENDING
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from utils import config
from utils.account_config import default_account_config
from utils.admin_credentials import (
    AdminCredentialError,
    load_credentials as load_admin_credentials,
    update_password as update_admin_password,
    validate_password as validate_admin_password,
    verify_login as verify_admin_login,
)
from utils.crypto import encrypt as _enc, decrypt as _dec
from utils.notify import send_email

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())
app.permanent_session_lifetime = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
CORS(app, origins=_cors_origins if _cors_origins else [])

# 本项目默认部署在 Caddy 反代之后，直连拿到的 remote_addr 是反代容器的 IP，
# 会让所有用户共用同一个限流桶。信任 X-Forwarded-* 才能按真实来源限流。
# 直接把 Flask 暴露到公网时必须设为 0，否则请求方可以伪造 X-Forwarded-For。
_trusted_proxies = int(os.environ.get("TRUSTED_PROXY_COUNT", "1"))
if _trusted_proxies > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=_trusted_proxies,
        x_proto=_trusted_proxies,
        x_host=_trusted_proxies,
    )

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

from blueprints.database_bp import database_bp
app.register_blueprint(database_bp, url_prefix="/db")

ADMIN_RESET_CODE_TTL = 10 * 60
ADMIN_RESET_MAX_ATTEMPTS = 5
_admin_reset_codes = {}
_admin_reset_lock = Lock()


def get_db():
    client = MongoClient(config.get_mongo_uri())
    return client, client.AutoLib


def _ensure_uid():
    """确保 session 中有 uid：已登录返回真实 uid，否则分配游客 uid。"""
    uid = session.get("web_uid")
    if uid:
        return uid
    guest_uid = session.get("guest_uid")
    if not guest_uid:
        import uuid as _uuid
        guest_uid = f"guest_{_uuid.uuid4().hex[:12]}"
        session["guest_uid"] = guest_uid
        session.permanent = True
    return guest_uid


def _ensure_database_indexes() -> None:
    """Create uniqueness constraints for the one-student-one-account model."""
    client, db = get_db()
    try:
        db.web_users.create_index("uid", unique=True, name="uniq_web_uid")
        # Create the new invariant before removing the legacy compound index.
        # If historical duplicates still exist this raises DuplicateKeyError,
        # leaving the old index intact until migrate_account_configs.py runs.
        db.user_config_info.create_index(
            [("pid", 1)],
            unique=True,
            name="uniq_pid",
            partialFilterExpression={
                "pid": {"$type": "string"},
            },
        )
        if "uniq_owner_pid" in db.user_config_info.index_information():
            db.user_config_info.drop_index("uniq_owner_pid")
    finally:
        client.close()


# ==================== Decorators ====================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("web_uid"):
            return jsonify({"error": "请先验证学号登录", "need_login": True}), 401
        return f(*args, **kwargs)
    return decorated


def visitor_session_required(f):
    """Allow credential verification to start from a guest session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        _ensure_uid()
        return f(*args, **kwargs)
    return decorated


def own_account_required(f):
    """Require a real login whose student ID matches the route account."""
    @wraps(f)
    def decorated(pid, *args, **kwargs):
        uid = session.get("web_uid")
        if not uid:
            return jsonify({"error": "请先验证学号登录", "need_login": True}), 401
        if uid != pid:
            return jsonify({"error": "当前登录学号无权访问该账号"}), 403
        return f(pid, *args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _admin_session_valid():
            session.pop("is_admin", None)
            session.pop("admin_auth_version", None)
            session.pop("admin_username", None)
            return jsonify({"error": "需要管理员权限", "need_admin_login": True}), 403
        return f(*args, **kwargs)
    return decorated


def _admin_session_valid():
    if not session.get("is_admin") or not session.get("admin_auth_version"):
        return False
    try:
        credentials = load_admin_credentials()
    except AdminCredentialError:
        return False
    if not credentials:
        return False
    return secrets.compare_digest(
        session.get("admin_auth_version", ""),
        credentials["auth_version"],
    )


def _account_filter(pid: str):
    """Canonical filter while web_uid is retained for schema compatibility."""
    return {"pid": pid, "web_uid": pid}


def _get_decrypted_cfg(pid: str):
    """从 DB 读取用户配置并解密密码，返回 cfg 或 None。"""
    client, db = get_db()
    cfg = db.user_config_info.find_one(
        _account_filter(pid),
    )
    client.close()
    if cfg:
        if cfg.get("vpn_password"):
            cfg["vpn_password"] = _dec(cfg["vpn_password"])
    return cfg


# ==================== Cache headers ====================

@app.after_request
def add_cache_headers(response):
    path = request.path
    if path.startswith('/static/') and request.args.get('v'):
        # 带版本号的静态资源：永久缓存（版本号变则 URL 变）
        response.cache_control.public = True
        response.cache_control.max_age = 31536000
        response.headers['Expires'] = 'Thu, 31 Dec 2099 23:59:59 GMT'
    elif path in ('/', '/admin'):
        # HTML 页面：每次重新验证，但允许使用缓存版本（304 快速响应）
        response.cache_control.no_cache = True
    elif path.startswith('/api/'):
        response.cache_control.no_store = True
    return response

# ==================== Pages ====================

def _static_v():
    """以静态目录最新 mtime 作为版本号，防止浏览器缓存旧资源"""
    try:
        static_dir = os.path.join(app.root_path, "static")
        return int(max(
            os.path.getmtime(os.path.join(static_dir, f))
            for f in os.listdir(static_dir)
            if os.path.isfile(os.path.join(static_dir, f))
        ))
    except Exception:
        return 0


@app.route("/")
def index():
    return render_template("index.html", static_v=_static_v())

@app.route("/admin")
def admin():
    return render_template("admin.html", static_v=_static_v())


# ==================== User Auth ====================
# 没有独立的登录/注册流程：前端「添加学号」调用
# /api/my/accounts/<pid>/verify，验证统一身份认证密码通过后会话即被提升为该学号。
# 数据以学号（web_uid = pid）归属，换设备重新验证一次即可找回。

def _cache_identity(db, pid: str, password: str):
    """把统一身份认证密码哈希缓存到 web_users，供学校服务不可用时离线登录。"""
    db.web_users.update_one(
        {"uid": pid},
        {
            "$set": {"password": generate_password_hash(password)},
            "$setOnInsert": {"uid": pid, "created_at": datetime.now()},
        },
        upsert=True,
    )


def _login_as(db, pid: str):
    """建立以学号为唯一身份的会话。返回昵称。"""
    session.permanent = True
    session["web_uid"] = pid
    session.pop("guest_uid", None)
    user = db.web_users.find_one({"uid": pid}) or {}
    return (user.get("nickname") or "").strip()


def _upsert_verified_account(db, pid: str, password: str) -> None:
    """Atomically create/recover the canonical account after school verification."""
    db.user_config_info.update_one(
        {"pid": pid},
        {
            "$set": {
                "pid": pid,
                "web_uid": pid,
                "vpn_password": _enc(password),
                "verified": True,
                "updated_at": datetime.now(),
            },
            "$setOnInsert": default_account_config(),
            "$unset": {"lib_password": ""},
        },
        upsert=True,
    )


def _offline_login(pid: str, password: str) -> bool:
    """学校服务不可用时的回退：本地缓存的密码哈希对得上就放行进入登录态。

    只用于服务异常（不是密码错误）的场景，且不会把 verified 置为 True——
    学校侧恢复后仍需要重新验证一次。
    """
    client, db = get_db()
    try:
        user = db.web_users.find_one({"uid": pid})
        if not user or not check_password_hash(user.get("password", ""), password):
            return False
        _login_as(db, pid)
        return True
    finally:
        client.close()


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "已退出"}), 200


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    if "web_uid" in session:
        client, db = get_db()
        user = db.web_users.find_one({"uid": session["web_uid"]}) or {}
        nickname = (user.get("nickname") or "").strip()
        client.close()
        return jsonify({"logged_in": True, "uid": session["web_uid"], "nickname": nickname, "is_guest": False}), 200
    if "guest_uid" in session:
        return jsonify({"logged_in": False, "uid": session["guest_uid"], "nickname": "", "is_guest": True}), 200
    return jsonify({"logged_in": False, "nickname": "", "is_guest": True}), 200


@app.route("/api/auth/profile", methods=["POST"])
def update_profile():
    """登录用户更新昵称。密码即统一身份认证密码，由登录流程自动缓存，不提供修改。"""
    if "web_uid" not in session:
        return jsonify({"error": "请先登录"}), 401
    data = request.get_json() or {}
    uid = session["web_uid"]

    update = {}
    if "nickname" in data:
        nick = (data.get("nickname") or "").strip()
        if len(nick) > 30:
            return jsonify({"error": "昵称最多 30 个字符"}), 400
        update["nickname"] = nick

    if not update:
        return jsonify({"error": "没有要更新的内容"}), 400

    client, db = get_db()
    db.web_users.update_one({"uid": uid}, {"$set": update})
    client.close()
    return jsonify({
        "message": "已更新",
        "uid": uid,
        "nickname": update.get("nickname", "")
    }), 200


# ==================== Admin Auth ====================

@app.route("/api/admin/login", methods=["POST"])
@limiter.limit("5/minute")
def admin_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    try:
        valid, credentials = verify_admin_login(username, password)
    except AdminCredentialError:
        return jsonify({"error": "管理员凭据文件无法读取，请检查服务器配置"}), 503
    if credentials is None:
        return jsonify({"error": "管理员账号尚未初始化"}), 503
    if not valid:
        return jsonify({"error": "管理员账号或密码错误"}), 401

    session.clear()
    session["is_admin"] = True
    session["admin_auth_version"] = credentials["auth_version"]
    session["admin_username"] = credentials["username"]
    return jsonify({"message": "管理员登录成功"}), 200


@app.route("/api/admin/password-reset/request", methods=["POST"])
@limiter.limit("3/hour")
def admin_password_reset_request():
    """Send a short-lived recovery code to the configured administrator email."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    try:
        credentials = load_admin_credentials()
    except AdminCredentialError:
        return jsonify({"error": "管理员凭据文件无法读取，请检查服务器配置"}), 503
    if not credentials:
        return jsonify({"error": "管理员账号尚未初始化"}), 503
    if not secrets.compare_digest(username, credentials["username"]):
        return jsonify({"message": "如果账号存在，验证码将发送至绑定邮箱"}), 200

    code = f"{secrets.randbelow(1_000_000):06d}"
    now = time.time()
    with _admin_reset_lock:
        previous = _admin_reset_codes.get(username)
        if previous and now - previous.get("sent_at", 0) < 60:
            return jsonify({"error": "验证码发送过于频繁，请稍后再试"}), 429

    sent = send_email(
        credentials["email"],
        "AutoLib 管理员密码重置验证码",
        (
            f"你的 AutoLib 管理员密码重置验证码是：{code}\n\n"
            "验证码 10 分钟内有效，且只能使用一次。"
            "如果不是你本人操作，请忽略本邮件。"
        ),
    )
    if not sent:
        return jsonify({"error": "验证码邮件发送失败，请检查 SMTP 配置"}), 503

    with _admin_reset_lock:
        _admin_reset_codes[username] = {
            "code_hash": generate_password_hash(code),
            "expires_at": now + ADMIN_RESET_CODE_TTL,
            "sent_at": now,
            "attempts": 0,
        }
    return jsonify({"message": "验证码已发送至绑定邮箱"}), 200


@app.route("/api/admin/password-reset/confirm", methods=["POST"])
@limiter.limit("5 per 10 minutes")
def admin_password_reset_confirm():
    """Verify the email code and replace the local administrator password."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    code = (data.get("code") or "").strip()
    new_password = data.get("new_password") or ""
    password_error = validate_admin_password(new_password)
    if password_error:
        return jsonify({"error": password_error}), 400

    try:
        credentials = load_admin_credentials()
    except AdminCredentialError:
        return jsonify({"error": "管理员凭据文件无法读取，请检查服务器配置"}), 503
    if not credentials or not secrets.compare_digest(
        username,
        credentials["username"],
    ):
        return jsonify({"error": "验证码无效或已过期"}), 400

    now = time.time()
    with _admin_reset_lock:
        reset = _admin_reset_codes.get(username)
        if not reset or now > reset["expires_at"]:
            _admin_reset_codes.pop(username, None)
            return jsonify({"error": "验证码无效或已过期"}), 400
        reset["attempts"] += 1
        if reset["attempts"] > ADMIN_RESET_MAX_ATTEMPTS:
            _admin_reset_codes.pop(username, None)
            return jsonify({"error": "验证码尝试次数过多，请重新获取"}), 400
        code_valid = check_password_hash(reset["code_hash"], code)
        if not code_valid:
            return jsonify({"error": "验证码无效或已过期"}), 400

    try:
        update_admin_password(credentials, new_password)
    except (AdminCredentialError, OSError):
        return jsonify({"error": "管理员凭据文件更新失败"}), 500
    finally:
        with _admin_reset_lock:
            _admin_reset_codes.pop(username, None)

    session.clear()
    return jsonify({"message": "密码已重置，请使用新密码登录"}), 200


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    session.pop("admin_auth_version", None)
    session.pop("admin_username", None)
    return jsonify({"message": "已退出管理后台"}), 200


@app.route("/api/admin/me", methods=["GET"])
def admin_me():
    valid = _admin_session_valid()
    if not valid:
        session.pop("is_admin", None)
        session.pop("admin_auth_version", None)
        session.pop("admin_username", None)
    return jsonify({
        "is_admin": valid,
        "username": session.get("admin_username") if valid else None,
    }), 200


# ==================== Student account API ====================

@app.route("/api/my/accounts", methods=["GET"])
@login_required
def get_my_accounts():
    """Return the canonical account for the logged-in student."""
    uid = session["web_uid"]
    client, db = get_db()
    raw_accounts = list(db.user_config_info.find(
        _account_filter(uid),
        {"_id": 0, "web_password": 0, "vpn_password": 0, "lib_password": 0}
    ))
    client.close()
    accounts = []
    seen_pids = set()
    for account in raw_accounts:
        pid = account.get("pid")
        if not pid or pid in seen_pids:
            continue
        seen_pids.add(pid)
        accounts.append(account)
    for a in accounts:
        if isinstance(a.get("updated_at"), datetime):
            a["updated_at"] = a["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(accounts), 200


@app.route("/api/my/accounts/<pid>", methods=["GET"])
@own_account_required
def get_my_account(pid):
    """Get single account config (with passwords, for editing)"""
    client, db = get_db()
    cfg = db.user_config_info.find_one(
        _account_filter(pid),
        {"_id": 0, "web_password": 0, "lib_password": 0},
    )
    client.close()
    if not cfg:
        return jsonify({}), 200
    # 解密敏感字段返回前端
    if cfg.get("vpn_password"):
        cfg["vpn_password"] = _dec(cfg["vpn_password"])
    if isinstance(cfg.get("updated_at"), datetime):
        cfg["updated_at"] = cfg["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(cfg), 200


@app.route("/api/my/accounts/<pid>", methods=["POST"])
@own_account_required
def save_my_account(pid):
    """Save settings for an already verified canonical account."""
    data = request.get_json(silent=True) or {}
    # Credentials and verified status may only be changed by verify_account.
    allowed = ["seat_list", "mode", "time", "is_reserved",
               "late_protection", "notify_email"]
    update = {k: v for k, v in data.items() if k in allowed and v is not None}

    if "seat_list" in update:
        seats = update["seat_list"]
        if not isinstance(seats, list) or any(
            not isinstance(seat, str) or not seat.strip() for seat in seats
        ):
            return jsonify({"error": "座位配置格式无效"}), 400
        update["seat_list"] = list(dict.fromkeys(seat.strip() for seat in seats))
    if "time" in update and not isinstance(update["time"], dict):
        return jsonify({"error": "时间配置格式无效"}), 400

    update["pid"] = pid
    update["web_uid"] = pid
    update["updated_at"] = datetime.now()

    client, db = get_db()
    existing = db.user_config_info.find_one(_account_filter(pid), {"_id": 1})
    if not existing:
        client.close()
        return jsonify({"error": "账号尚未通过验证，请重新验证学号"}), 409
    db.user_config_info.update_one(_account_filter(pid), {"$set": update})
    client.close()
    return jsonify({"message": "配置已保存"}), 200


@app.route("/api/my/accounts/<pid>", methods=["DELETE"])
@own_account_required
def delete_my_account(pid):
    """Delete a library account from current web user"""
    client, db = get_db()
    result = db.user_config_info.delete_one(_account_filter(pid))
    client.close()
    if result.deleted_count:
        return jsonify({"message": f"学号 {pid} 已删除"}), 200
    return jsonify({"error": "未找到该配置"}), 404


@app.route("/api/my/accounts/<pid>/reservations", methods=["GET"])
@own_account_required
def get_account_reservations(pid):
    """Live query reservations for a specific library account"""
    cfg = _get_decrypted_cfg(pid)

    if not cfg or not cfg.get("vpn_password"):
        return jsonify({"error": "请先保存统一身份认证密码"}), 400

    try:
        from utils.library_system import LibrarySystem
        library = LibrarySystem(
            username=pid,
            password=cfg["vpn_password"],
            vpn_password=cfg["vpn_password"]
        )
        reservations, message = library.get_reservation_info()
        return jsonify({"message": message, "reservations": reservations or []}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/my/accounts/<pid>/cancel", methods=["POST"])
@own_account_required
def cancel_account_reservation(pid):
    """Cancel a reservation for a specific library account"""
    data = request.get_json()
    uuid = data.get("uuid")
    if not uuid:
        return jsonify({"error": "缺少 uuid"}), 400

    cfg = _get_decrypted_cfg(pid)

    if not cfg or not cfg.get("vpn_password"):
        return jsonify({"error": "请先保存统一身份认证密码"}), 400

    try:
        from utils.library_system import LibrarySystem
        library = LibrarySystem(
            username=pid,
            password=cfg["vpn_password"],
            vpn_password=cfg["vpn_password"]
        )
        success, message = library.delete_seat(uuid)
        return jsonify({"success": success, "message": message}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/my/accounts/<pid>/nap_config", methods=["GET", "POST"])
@own_account_required
def nap_config(pid):
    client, db = get_db()
    cfg = db.user_config_info.find_one(_account_filter(pid), {"nap_config": 1})
    if not cfg:
        client.close()
        return jsonify({"error": "账号不存在"}), 404

    if request.method == "GET":
        defaults = {"start_time": "14:00", "end_time": "", "seat": "", "auto_daily": False, "trigger_time": "12:00"}
        result = {**defaults, **(cfg.get("nap_config") or {})}
        client.close()
        return jsonify(result), 200

    body = request.get_json(silent=True) or {}
    allowed = {"start_time", "end_time", "seat", "auto_daily", "trigger_time"}
    update = {k: v for k, v in body.items() if k in allowed}
    db.user_config_info.update_one(
        _account_filter(pid),
        {"$set": {"nap_config": update}}
    )
    client.close()
    return jsonify({"ok": True}), 200


@app.route("/api/my/accounts/<pid>/nap", methods=["POST"])
@own_account_required
def do_nap(pid):
    """取消当前预约并立即重新预约下午时段（一键午休）"""
    cfg = _get_decrypted_cfg(pid)
    if not cfg:
        return jsonify({"error": "未找到该账号配置"}), 404
    if not cfg.get("vpn_password"):
        return jsonify({"error": "请先保存统一身份认证密码"}), 400

    body = request.get_json(silent=True) or {}
    uuid = (body.get("uuid") or "").strip()
    seat_name = (body.get("seat") or "").strip()
    start_time = (body.get("start_time") or "").strip()
    end_time = (body.get("end_time") or "").strip()

    if not uuid or not seat_name or not start_time or not end_time:
        return jsonify({"error": "缺少必要参数 uuid / seat / start_time / end_time"}), 400
    if start_time >= end_time:
        return jsonify({"error": "结束时间必须晚于开始时间"}), 400

    try:
        from scheduled_task import get_seat_ids
        from utils.library_system import LibrarySystem
        import time as _time

        library = LibrarySystem(
            username=pid,
            password=cfg["vpn_password"],
            vpn_password=cfg["vpn_password"],
        )

        cancel_ok, cancel_msg = library.delete_seat(uuid)
        if not cancel_ok:
            return jsonify({"error": f"取消失败：{cancel_msg}"}), 200

        _time.sleep(0.5)

        seat_ids = get_seat_ids([seat_name])
        if not seat_ids:
            return jsonify({
                "cancel_success": True,
                "success": False,
                "result": f"取消成功，但未找到座位「{seat_name}」，请手动预约"
            }), 200

        today = __import__("datetime").date.today().strftime("%Y-%m-%d")
        msg, _ = library.reserve_seat(
            seat_list=seat_ids,
            resv_begin_time=f"{today} {start_time}:00",
            resv_end_time=f"{today} {end_time}:00",
        )
        success = "成功" in msg
        return jsonify({
            "cancel_success": True,
            "success": success,
            "result": msg
        }), 200
    except Exception as e:
        return jsonify({"error": f"午休操作失败: {str(e)}"}), 500


@app.route("/api/my/accounts/<pid>/arrived", methods=["POST"])
@own_account_required
def toggle_arrived(pid):
    """Toggle the 'arrived at library today' flag for late-protection bypass."""
    today = datetime.now().strftime("%Y-%m-%d")
    client, db = get_db()
    cfg = db.user_config_info.find_one(_account_filter(pid), {"arrived_date": 1})
    if not cfg:
        client.close()
        return jsonify({"error": "账号不存在"}), 404
    already = cfg.get("arrived_date") == today
    new_val = "" if already else today
    db.user_config_info.update_one(
        _account_filter(pid),
        {"$set": {"arrived_date": new_val}}
    )
    client.close()
    return jsonify({"arrived": not already}), 200


@app.route("/api/my/accounts/<pid>/verify", methods=["POST"])
@limiter.limit("5/minute")
@visitor_session_required
def verify_account(pid):
    """Verify the unified identity credential and library CAS session.

    这是唯一的凭据入口（验证通过即登录），所以必须限流防止撞库。

    Accepts the password either from the request body (for pre-save verification
    in addAccount flow) or falls back to the saved account config in DB.
    Returns `failed_at` to tell the frontend which step failed.
    """
    data = request.get_json(silent=True) or {}

    vpn_password = data.get("vpn_password")

    # Fall back to DB if the password is not provided in the request body.
    if not vpn_password:
        cfg = _get_decrypted_cfg(pid) if session.get("web_uid") == pid else None
        if cfg:
            vpn_password = vpn_password or cfg.get("vpn_password")

    if not vpn_password:
        return jsonify({"error": "请填写统一身份认证密码", "verified": False}), 400

    def mark_verified(value: bool):
        """Only invalidate the account that is currently authenticated."""
        if session.get("web_uid") != pid:
            return
        c, d = get_db()
        d.user_config_info.update_one(
            _account_filter(pid),
            {"$set": {"verified": value, "updated_at": datetime.now()}}
        )
        c.close()

    # Step 1: 登录 webvpn，建立 CAS 会话。
    try:
        from utils.vpn_system import VPNSystem
        vpn = VPNSystem(pid, vpn_password)
        vpn_ok = vpn.vpn_login()
    except Exception as e:
        # 服务异常而非密码错误：本地缓存对得上就先放行，但保持未验证状态。
        mark_verified(False)
        response = {
            "verified": False,
            "failed_at": "vpn",
            "error": f"VPN 登录异常：{str(e)}",
        }
        if _offline_login(pid, vpn_password):
            response["logged_in"] = True
            response["uid"] = pid
            response["offline"] = True
            response["error"] = (
                f"统一身份认证服务暂时不可用（{str(e)}），"
                "已使用本地缓存登录，服务恢复后请重新验证"
            )
        return jsonify(response), 200

    if not vpn_ok:
        mark_verified(False)
        return jsonify({
            "verified": False,
            "failed_at": "vpn",
            "error": "统一身份认证失败：请检查网上办事大厅密码是否正确"
        }), 200

    # Step 2: 复用 webvpn 会话，通过 CAS SSO 建立图书馆登录态。
    try:
        from utils.library_system import LibrarySystem
        LibrarySystem(
            username=pid,
            password=vpn_password,
            session=vpn.session
        )
    except Exception as e:
        mark_verified(False)
        return jsonify({
            "verified": False,
            "failed_at": "library",
            "error": f"图书馆统一身份认证失败：{str(e)}"
        }), 200

    # webvpn 与图书馆 CAS SSO 均通过。
    # Verification is the login and credential-save transaction. Always switch
    # to the verified student, even if another student was previously logged in.
    client2, db2 = get_db()
    try:
        _cache_identity(db2, pid, vpn_password)
        _upsert_verified_account(db2, pid, vpn_password)
        _login_as(db2, pid)
    finally:
        client2.close()
    return jsonify({
        "verified": True,
        "logged_in": True,
        "uid": pid,
        "message": "统一身份认证及图书馆登录验证成功",
    }), 200


@app.route("/api/my/accounts/<pid>/result", methods=["GET"])
@own_account_required
def get_account_result(pid):
    client, db = get_db()
    cfg = db.user_config_info.find_one(
        _account_filter(pid), {"result": 1, "_id": 0}
    )
    client.close()
    return jsonify({"result": cfg.get("result", "") if cfg else ""}), 200


@app.route("/api/my/accounts/<pid>/reserve_now", methods=["POST"])
@own_account_required
def reserve_now(pid):
    """立即执行预约（非定时任务）"""
    cfg = _get_decrypted_cfg(pid)

    if not cfg:
        return jsonify({"error": "未找到该账号配置"}), 404

    if not cfg.get("vpn_password"):
        return jsonify({"error": "请先保存统一身份认证密码"}), 400

    if not cfg.get("seat_list"):
        return jsonify({"error": "请先配置座位列表"}), 400

    # 导入预约逻辑
    try:
        from scheduled_task import reservation, update_user_config
        
        # 执行预约
        reservation(cfg)
        
        # 获取最新结果
        client, db = get_db()
        updated_cfg = db.user_config_info.find_one(
            _account_filter(pid), {"result": 1, "_id": 0}
        )
        client.close()
        
        result_text = updated_cfg.get("result", "") if updated_cfg else ""
        success = "成功" in result_text or "预约成功" in result_text
        
        return jsonify({
            "success": success,
            "result": result_text
        }), 200
    except Exception as e:
        return jsonify({"error": f"预约执行失败: {str(e)}"}), 500


@app.route("/api/my/accounts/<pid>/reserve_custom", methods=["POST"])
@own_account_required
def reserve_custom(pid):
    """用指定座位和时间段预约今天或明天

    `day` 只接受 today / tomorrow：图书馆系统本身也只开放这两天，
    限定取值顺便挡掉了任意日期的越权预约。
    """
    cfg = _get_decrypted_cfg(pid)
    if not cfg:
        return jsonify({"error": "未找到该账号配置"}), 404
    if not cfg.get("vpn_password"):
        return jsonify({"error": "请先保存统一身份认证密码"}), 400

    body = request.get_json(silent=True) or {}
    seat_name = (body.get("seat") or "").strip()
    start_time = (body.get("start_time") or "").strip()  # "HH:MM"
    end_time = (body.get("end_time") or "").strip()      # "HH:MM"
    day = (body.get("day") or "today").strip()

    if day not in ("today", "tomorrow"):
        return jsonify({"error": "day 只能是 today 或 tomorrow"}), 400
    if not seat_name or not start_time or not end_time:
        return jsonify({"error": "缺少 seat / start_time / end_time"}), 400
    if start_time >= end_time:
        return jsonify({"error": "结束时间必须晚于开始时间"}), 400
    from datetime import datetime as _dt
    _dur_min = (_dt.strptime(end_time, "%H:%M") - _dt.strptime(start_time, "%H:%M")).total_seconds() / 60
    if _dur_min < 120:
        return jsonify({"error": "预约时长至少 2 小时（120 分钟）"}), 400

    try:
        from scheduled_task import get_seat_ids
        from utils.library_system import LibrarySystem

        seat_ids = get_seat_ids([seat_name])
        if not seat_ids:
            return jsonify({"error": f"未找到座位「{seat_name}」"}), 400

        import datetime as _datetime
        target = _datetime.date.today()
        if day == "tomorrow":
            target += _datetime.timedelta(days=1)
        target_str = target.strftime("%Y-%m-%d")
        resv_begin = f"{target_str} {start_time}:00"
        resv_end   = f"{target_str} {end_time}:00"

        library = LibrarySystem(
            username=pid,
            password=cfg["vpn_password"],
            vpn_password=cfg["vpn_password"],
        )
        msg, _ = library.reserve_seat(
            seat_list=seat_ids,
            resv_begin_time=resv_begin,
            resv_end_time=resv_end,
        )
        success = "成功" in msg
        return jsonify({"success": success, "result": msg}), 200
    except Exception as e:
        return jsonify({"error": f"预约失败: {str(e)}"}), 500


# ==================== Seats ====================

@app.route("/api/seats", methods=["GET"])
@app.route("/api/public/seats", methods=["GET"])  # 兼容旧版客户端
def get_all_seats():
    """按楼层/区域分组的座位列表。座位表本身不含用户数据，无需登录。"""
    try:
        client, db = get_db()
        devices = list(db.devices.find({}, {"_id": 0, "devId": 1, "devName": 1, "location": 1}))
        client.close()
        grouped = {}
        for d in devices:
            loc = d.get("location", "未知")
            if loc not in grouped:
                grouped[loc] = []
            grouped[loc].append(d["devName"])
        for loc in grouped:
            grouped[loc].sort()
        return jsonify({"seats": grouped}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== Admin API (password-filtered) ====================


@app.route("/api/users", methods=["GET"])
@admin_required
def get_all_users():
    try:
        client, db = get_db()
        users = list(db.user_config_info.find())
        client.close()
        result = []
        for u in users:
            u["_id"] = str(u["_id"])
            # Hide all passwords from admin view
            u.pop("web_password", None)
            u.pop("vpn_password", None)
            u.pop("lib_password", None)
            if "updated_at" in u and isinstance(u["updated_at"], datetime):
                u["updated_at"] = u["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
            result.append(u)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/<pid>", methods=["DELETE"])
@admin_required
def delete_user(pid):
    try:
        client, db = get_db()
        result = db.user_config_info.delete_one({"pid": pid})
        client.close()
        if result.deleted_count:
            return jsonify({"message": f"用户 {pid} 已删除"}), 200
        return jsonify({"error": "未找到该用户"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/<pid>/toggle", methods=["POST"])
@admin_required
def toggle_user(pid):
    try:
        data = request.get_json()
        field = data.get("field")
        value = data.get("value")
        if field not in ("is_reserved", "late_protection"):
            return jsonify({"error": "无效字段"}), 400
        # 统一存储为字符串 "True"/"False"，与前端和 scheduled_task 保持一致
        if isinstance(value, bool):
            value = "True" if value else "False"
        elif not isinstance(value, str) or value not in ("True", "False"):
            return jsonify({"error": "无效值"}), 400
        client, db = get_db()
        db.user_config_info.update_one({"pid": pid}, {"$set": {field: value}})
        client.close()
        return jsonify({"message": "已更新"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/<pid>/protection_settings", methods=["POST"])
@admin_required
def update_protection_settings(pid):
    """Admin: update late-protection duration and blacklist status for a user."""
    try:
        data = request.get_json() or {}
        update = {}
        if "protection_max_minutes" in data:
            val = data["protection_max_minutes"]
            if not isinstance(val, int) or val < -1:
                return jsonify({"error": "保护时间无效（需为 -1 或 >= 0 的整数）"}), 400
            update["protection_max_minutes"] = val
        if "late_protection_blacklisted" in data:
            update["late_protection_blacklisted"] = bool(data["late_protection_blacklisted"])
        if not update:
            return jsonify({"error": "无有效字段"}), 400
        update["updated_at"] = datetime.now()
        client, db = get_db()
        db.user_config_info.update_one({"pid": pid}, {"$set": update})
        client.close()
        return jsonify({"message": "已更新"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/<pid>/update", methods=["POST"])
@admin_required
def update_user(pid):
    try:
        data = request.get_json()
        allowed = ["seat_list", "mode", "time", "priority",
                    "is_reserved", "late_protection",
                    "protection_max_minutes", "late_protection_blacklisted"]
        update = {k: v for k, v in data.items() if k in allowed}
        update["updated_at"] = datetime.now()
        client, db = get_db()
        db.user_config_info.update_one({"pid": pid}, {"$set": update})
        client.close()
        return jsonify({"message": "已更新"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== Announcements ====================

_ANN_LEVELS = {"info", "success", "warning", "danger"}


def _serialize_announcement(doc):
    return {
        "id": str(doc["_id"]),
        "title": doc.get("title", ""),
        "content": doc.get("content", ""),
        "level": doc.get("level", "info"),
        "pinned": bool(doc.get("pinned", False)),
        "active": bool(doc.get("active", True)),
        "created_at": doc["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(doc.get("created_at"), datetime) else doc.get("created_at", ""),
        "updated_at": doc["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(doc.get("updated_at"), datetime) else doc.get("updated_at", ""),
    }


# ==================== App version ====================
# 客户端升级检查。版本信息存单条文档，管理员在后台改，不用每次发版动代码。

_APP_VERSION_DOC = "android_latest"


def _serialize_app_version(doc):
    doc = doc or {}
    return {
        "version_code": int(doc.get("version_code", 0) or 0),
        "version_name": doc.get("version_name", ""),
        "download_url": doc.get("download_url", ""),
        "notes": doc.get("notes", ""),
        "updated_at": doc["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(doc.get("updated_at"), datetime) else doc.get("updated_at", ""),
    }


@app.route("/api/app/version", methods=["GET"])
def get_app_version():
    """Public: latest Android build info, used by the in-app update check."""
    client, db = get_db()
    doc = db.app_versions.find_one({"_id": _APP_VERSION_DOC})
    client.close()
    return jsonify(_serialize_app_version(doc)), 200


@app.route("/api/admin/app_version", methods=["POST"])
@admin_required
def admin_set_app_version():
    data = request.get_json(silent=True) or {}
    try:
        version_code = int(data.get("version_code", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "version_code 必须是整数"}), 400
    if version_code <= 0:
        return jsonify({"error": "version_code 必须大于 0"}), 400

    version_name = (data.get("version_name") or "").strip()
    download_url = (data.get("download_url") or "").strip()
    if not version_name:
        return jsonify({"error": "version_name 不能为空"}), 400
    # 客户端会用它拉起浏览器，限制协议避免存进 javascript: 之类的东西
    if not download_url.startswith(("http://", "https://")):
        return jsonify({"error": "download_url 必须是 http(s) 链接"}), 400

    doc = {
        "version_code": version_code,
        "version_name": version_name,
        "download_url": download_url,
        "notes": (data.get("notes") or "").strip(),
        "updated_at": datetime.now(),
    }
    client, db = get_db()
    db.app_versions.update_one({"_id": _APP_VERSION_DOC}, {"$set": doc}, upsert=True)
    client.close()
    return jsonify(_serialize_app_version(doc)), 200


@app.route("/api/announcements", methods=["GET"])
def list_announcements():
    """Public list of active announcements — visible to guests and logged-in users."""
    client, db = get_db()
    docs = list(
        db.announcements.find({"active": True})
        .sort([("pinned", DESCENDING), ("created_at", DESCENDING)])
    )
    client.close()
    return jsonify([_serialize_announcement(d) for d in docs]), 200


@app.route("/api/admin/announcements", methods=["GET"])
@admin_required
def admin_list_announcements():
    client, db = get_db()
    docs = list(
        db.announcements.find({})
        .sort([("pinned", DESCENDING), ("created_at", DESCENDING)])
    )
    client.close()
    return jsonify([_serialize_announcement(d) for d in docs]), 200


@app.route("/api/admin/announcements", methods=["POST"])
@admin_required
def admin_create_announcement():
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title or not content:
        return jsonify({"error": "标题和内容不能为空"}), 400
    level = data.get("level", "info")
    if level not in _ANN_LEVELS:
        level = "info"
    now = datetime.now()
    doc = {
        "title": title,
        "content": content,
        "level": level,
        "pinned": bool(data.get("pinned", False)),
        "active": bool(data.get("active", True)),
        "created_at": now,
        "updated_at": now,
    }
    client, db = get_db()
    res = db.announcements.insert_one(doc)
    doc["_id"] = res.inserted_id
    client.close()
    return jsonify(_serialize_announcement(doc)), 200


@app.route("/api/admin/announcements/<ann_id>", methods=["PUT"])
@admin_required
def admin_update_announcement(ann_id):
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "无效的公告 ID"}), 400
    data = request.get_json() or {}
    update = {}
    for k in ("title", "content"):
        if k in data:
            v = (data.get(k) or "").strip()
            if not v:
                return jsonify({"error": f"{k} 不能为空"}), 400
            update[k] = v
    if "level" in data:
        lv = data.get("level")
        if lv not in _ANN_LEVELS:
            return jsonify({"error": "无效的级别"}), 400
        update["level"] = lv
    if "pinned" in data:
        update["pinned"] = bool(data.get("pinned"))
    if "active" in data:
        update["active"] = bool(data.get("active"))
    if not update:
        return jsonify({"error": "没有要更新的字段"}), 400
    update["updated_at"] = datetime.now()

    client, db = get_db()
    result = db.announcements.update_one({"_id": oid}, {"$set": update})
    if result.matched_count == 0:
        client.close()
        return jsonify({"error": "公告不存在"}), 404
    doc = db.announcements.find_one({"_id": oid})
    client.close()
    return jsonify(_serialize_announcement(doc)), 200


@app.route("/api/admin/announcements/<ann_id>", methods=["DELETE"])
@admin_required
def admin_delete_announcement(ann_id):
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "无效的公告 ID"}), 400
    client, db = get_db()
    result = db.announcements.delete_one({"_id": oid})
    client.close()
    if result.deleted_count:
        return jsonify({"message": "已删除"}), 200
    return jsonify({"error": "公告不存在"}), 404


HEATMAP_DAYS = 371  # 53 周，正好铺满一整年的热力图


def _visit_totals(db, match):
    """聚合出次数和分钟数。放在数据库端算，避免把全部日志拉进内存。"""
    rows = list(db.visit_logs.aggregate([
        {"$match": match},
        {"$group": {
            "_id": None,
            "visits": {"$sum": 1},
            "minutes": {"$sum": {"$ifNull": ["$planned_duration_minutes", 0]}},
        }},
    ]))
    if not rows:
        return 0, 0
    return rows[0].get("visits", 0), rows[0].get("minutes", 0)


@app.route("/api/my/visit_stats", methods=["GET"])
@login_required
def my_visit_stats():
    uid = session["web_uid"]
    client, db = get_db()
    if not db.user_config_info.find_one(_account_filter(uid), {"_id": 1}):
        client.close()
        return jsonify({"total_visits": 0, "total_minutes": 0,
                        "this_week_visits": 0, "this_week_minutes": 0,
                        "recent": [], "daily": [], "heatmap_days": HEATMAP_DAYS}), 200

    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    heatmap_start = (now - timedelta(days=HEATMAP_DAYS - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    owned = {"pid": uid}

    # 累计值必须走聚合：早先的实现只取最近 200 条来求和，
    # 自习超过 200 次后「累计」就永远停在 200 了。
    total_visits, total_minutes = _visit_totals(db, owned)
    this_week_visits, this_week_minutes = _visit_totals(
        db, {**owned, "planned_begin": {"$gte": week_start}})

    # 热力图按天聚合。planned_begin 存的是本地时间的 naive datetime，
    # $dateToString 不带 timezone 正好原样取出当初写入的那一天。
    daily = [
        {
            "date": row["_id"],
            "visits": row.get("visits", 0),
            "minutes": row.get("minutes", 0),
        }
        for row in db.visit_logs.aggregate([
            {"$match": {
                **owned,
                "planned_begin": {"$gte": heatmap_start, "$type": "date"},
            }},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$planned_begin"}},
                "visits": {"$sum": 1},
                "minutes": {"$sum": {"$ifNull": ["$planned_duration_minutes", 0]}},
            }},
            {"$sort": {"_id": 1}},
        ])
    ]

    recent = []
    for l in db.visit_logs.find(
        owned,
        {"_id": 0, "seat_name": 1, "location": 1,
         "planned_begin": 1, "planned_duration_minutes": 1},
    ).sort("planned_begin", DESCENDING).limit(10):
        pb = l.get("planned_begin")
        recent.append({
            "date": pb.strftime("%Y-%m-%d") if isinstance(pb, datetime) else str(pb)[:10],
            "seat_name": l.get("seat_name", ""),
            "location": l.get("location", ""),
            "duration_minutes": l.get("planned_duration_minutes", 0),
        })

    client.close()
    return jsonify({
        "total_visits": total_visits,
        "total_minutes": total_minutes,
        "this_week_visits": this_week_visits,
        "this_week_minutes": this_week_minutes,
        "recent": recent,
        "daily": daily,
        "heatmap_days": HEATMAP_DAYS,
    }), 200


@app.route("/api/my/reservation_results", methods=["GET"])
@login_required
def my_reservation_results():
    """Return the latest reservation result for the logged-in student."""
    uid = session["web_uid"]
    client, db = get_db()
    rows = list(db.user_config_info.find(
        {**_account_filter(uid), "result": {"$exists": True, "$ne": ""}},
        {"_id": 0, "pid": 1, "result": 1, "updated_at": 1}
    ))
    client.close()
    out = []
    for r in rows:
        upd = r.get("updated_at")
        if isinstance(upd, datetime):
            upd = upd.strftime("%Y-%m-%d %H:%M:%S")
        result_text = r.get("result", "")
        success = ("成功" in result_text)
        out.append({
            "pid": r.get("pid", ""),
            "result": result_text,
            "success": success,
            "updated_at": upd or "",
        })
    out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return jsonify(out), 200


try:
    _ensure_database_indexes()
except DuplicateKeyError:
    app.logger.warning(
        "账号配置存在重复记录，唯一索引将在运行迁移脚本后创建"
    )
except Exception as index_error:
    app.logger.warning("数据库索引初始化失败: %s", index_error)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5004)
