import hashlib
import os
import secrets
import time
from functools import wraps
from threading import Lock
from flask import Flask, Response, g, render_template, jsonify, request, session
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from utils import config
from utils.account_config import (
    NOTIFY_MODES,
    RESERVATION_MODES,
    account_config_for_client,
    classify_result,
    CREDENTIAL_INVALID_RESULT,
    CREDENTIAL_REVERIFIED_RESULT,
    default_account_config,
    RESULT_FAILED,
    RESULT_PENDING,
    RESULT_SKIPPED,
    RESULT_SUCCESS,
    validate_time_config,
)
from utils.admin_credentials import (
    AdminCredentialError,
    load_credentials as load_admin_credentials,
    update_password as update_admin_password,
    validate_password as validate_admin_password,
    verify_login as verify_admin_login,
)
from utils.crypto import encrypt as _enc, decrypt as _dec
from utils.student_id import parse_student_id
from utils import study_time
from utils.notify import queue_email, send_email
from utils.reservation_blackout import (
    BEIJING_TZ,
    ReservationBlackoutError,
    find_any_reservation_conflict,
    find_reservation_conflict,
    parse_beijing_datetime,
    validate_pause_range,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())
app.permanent_session_lifetime = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# templates 是 bind mount 进来的，不打开这个开关 Jinja 会一直用首次加载的缓存，
# 改完 HTML 必须重启容器才生效。代价只是每次渲染多一次 mtime 检查。
app.config["TEMPLATES_AUTO_RELOAD"] = True
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

# 抢座优先级最多 3 个座位：再往下排也轮不到，列表长了反而看不清顺序
MAX_SEAT_LIST = 3

ADMIN_RESET_CODE_TTL = 10 * 60
ADMIN_RESET_MAX_ATTEMPTS = 5
_admin_reset_codes = {}
_admin_reset_lock = Lock()


def get_db():
    client = MongoClient(config.get_mongo_uri())
    return client, client.AutoLib


def _manual_reservation_blackout(segments):
    """Return a stable HTTP payload before any manual reservation mutation."""
    client, db = get_db()
    try:
        conflict = find_any_reservation_conflict(db.school_notice_reviews, segments)
    finally:
        client.close()
    return ReservationBlackoutError(conflict).to_payload() if conflict else None


# ==================== Bearer token ====================
# 登录态有两条等价的路：Cookie 会话，和 Authorization: Bearer <token>。
# 加第二条是因为 Cookie 这条路会被环境掐断——Safari 在明文 HTTP 页面上直接丢弃带
# Secure 标记的 Cookie，各家 WebView / 第三方客户端也未必带 Cookie 罐，用户看到的
# 现象是「验证成功但没登上」。令牌由验证接口签发，服务端只存 sha256，明文只在签发
# 时回一次。代价是它不像 Cookie 那样有 HttpOnly 保护，页面被 XSS 就能读走，所以
# 有效期与会话一致，退出登录时立即吊销。

AUTH_TOKEN_TTL = timedelta(days=30)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_auth_token(db, uid: str) -> str:
    """签发长效令牌，返回明文；库里只落哈希。"""
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    db.auth_tokens.insert_one({
        "token_hash": _hash_token(token),
        "uid": uid,
        "created_at": now,
        "expires_at": now + AUTH_TOKEN_TTL,
    })
    return token


def _bearer_token():
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def _uid_from_token():
    token = _bearer_token()
    if not token:
        return None
    client, db = get_db()
    try:
        row = db.auth_tokens.find_one({"token_hash": _hash_token(token)})
    finally:
        client.close()
    if not row:
        return None
    # TTL 索引最多能拖一分钟才删，这里自己再判一次过期。
    expires_at = row.get("expires_at")
    if expires_at and expires_at <= datetime.now():
        return None
    return row.get("uid")


def _revoke_auth_token() -> None:
    """退出登录时吊销本次请求带的令牌（如果有）。"""
    token = _bearer_token()
    if not token:
        return
    client, db = get_db()
    try:
        db.auth_tokens.delete_one({"token_hash": _hash_token(token)})
    finally:
        client.close()


def current_uid():
    """当前登录学号：Cookie 会话优先，其次 Bearer 令牌；都没有返回 None。

    结果挂在 g 上，一个请求里只查一次库。
    """
    if "autolib_uid" not in g:
        g.autolib_uid = session.get("web_uid") or _uid_from_token()
    return g.autolib_uid


def _ensure_uid():
    """确保有 uid：已登录（Cookie 或令牌）返回真实 uid，否则分配游客 uid。"""
    uid = current_uid()
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
        db.auth_tokens.create_index(
            "token_hash", unique=True, name="uniq_auth_token"
        )
        # 过期令牌交给 MongoDB 的 TTL 线程清，不需要额外的定时任务。
        db.auth_tokens.create_index(
            "expires_at", expireAfterSeconds=0, name="ttl_auth_token"
        )
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
        db.school_notice_reviews.create_index(
            [("source_id", 1), ("content_hash", 1)],
            unique=True,
            name="uniq_school_notice_revision",
        )
        # 同一学号的同一时段只排一次补约，重复点「立即预约」不会攒出多条。
        db.pending_segments.create_index(
            [("pid", 1), ("resv_begin_time", 1), ("resv_end_time", 1)],
            unique=True,
            name="uniq_pending_segment",
        )
        db.pending_segments.create_index(
            [("status", 1), ("open_at", 1)], name="pending_segment_due"
        )
    finally:
        client.close()


# ==================== Decorators ====================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_uid():
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
        uid = current_uid()
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


def _contacts():
    """「联系方式」卡片上的条目，全部来自环境变量。

    仓库里不写死任何一个具体地址或号码：这是部署者的私人联系方式，
    换个人部署就该是他自己的。没配的条目直接不渲染那一行。
    """
    items = []
    email = os.environ.get("CONTACT_EMAIL", "").strip()
    qq = os.environ.get("CONTACT_QQ", "").strip()
    github = os.environ.get("CONTACT_GITHUB", "").strip()
    if email:
        items.append({"kind": "email", "title": "反馈与建议", "value": email, "action": "复制"})
    if qq:
        items.append({"kind": "qq", "title": "QQ", "value": qq, "action": "复制"})
    if github:
        items.append({
            "kind": "github", "title": "GitHub", "action": "打开",
            # 显示时去掉协议头，点开时仍要用完整 URL
            "value": github.split("://", 1)[-1].rstrip("/"),
            "href": github if "://" in github else "https://" + github,
        })
    return items


@app.route("/")
def index():
    return render_template("index.html", static_v=_static_v(), contacts=_contacts())

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
    # 本次请求早先可能已经把「未登录」缓存进 g 了，登录后要让它重新解析。
    g.pop("autolib_uid", None)
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
                # 新密码验证通过，之前累计的「密码被拒」记录一并清掉。
                "credential_failures": 0,
                "updated_at": datetime.now(),
            },
            "$setOnInsert": default_account_config(),
            "$unset": {"lib_password": "", "credential_invalid_at": ""},
        },
        upsert=True,
    )


def _offline_login(pid: str, password: str):
    """学校服务不可用时的回退：本地缓存的密码哈希对得上就放行进入登录态。

    只用于服务异常（不是密码错误）的场景，且不会把 verified 置为 True——
    学校侧恢复后仍需要重新验证一次。
    成功返回新签发的令牌（非空字符串），失败返回 None。
    """
    client, db = get_db()
    try:
        user = db.web_users.find_one({"uid": pid})
        if not user or not check_password_hash(user.get("password", ""), password):
            return None
        _login_as(db, pid)
        return _issue_auth_token(db, pid)
    finally:
        client.close()


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    _revoke_auth_token()
    session.clear()
    return jsonify({"message": "已退出"}), 200


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    uid = current_uid()
    if uid:
        client, db = get_db()
        user = db.web_users.find_one({"uid": uid}) or {}
        nickname = (user.get("nickname") or "").strip()
        client.close()
        return jsonify({"logged_in": True, "uid": uid, "nickname": nickname, "is_guest": False}), 200
    if "guest_uid" in session:
        return jsonify({"logged_in": False, "uid": session["guest_uid"], "nickname": "", "is_guest": True}), 200
    return jsonify({"logged_in": False, "nickname": "", "is_guest": True}), 200


@app.route("/api/auth/profile", methods=["POST"])
def update_profile():
    """登录用户更新昵称。密码即统一身份认证密码，由登录流程自动缓存，不提供修改。"""
    uid = current_uid()
    if not uid:
        return jsonify({"error": "请先登录"}), 401
    data = request.get_json() or {}

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
    uid = current_uid()
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
    accounts = [account_config_for_client(a) for a in accounts]
    return jsonify(accounts), 200


@app.route("/api/my/accounts/<pid>", methods=["GET"])
@own_account_required
def get_my_account(pid):
    """Get a single account configuration without stored credentials."""
    client, db = get_db()
    cfg = db.user_config_info.find_one(
        _account_filter(pid),
        {"_id": 0, "web_password": 0, "vpn_password": 0, "lib_password": 0},
    )
    client.close()
    if not cfg:
        return jsonify({}), 200
    # Defense in depth: never serialize credentials even if the DB projection
    # is changed or bypassed in a future refactor.
    cfg = account_config_for_client(cfg)
    return jsonify(cfg), 200


@app.route("/api/my/accounts/<pid>", methods=["POST"])
@own_account_required
def save_my_account(pid):
    """Save settings for an already verified canonical account."""
    data = request.get_json(silent=True) or {}
    # Credentials and verified status may only be changed by verify_account.
    allowed = ["seat_list", "mode", "time", "is_reserved",
               "late_protection", "late_protection_mode", "notify_email", "notify_mode"]
    update = {k: v for k, v in data.items() if k in allowed and v is not None}

    # shift：推一次就不管了（默认）；cancel：推过一次之后再探一次，还没到就取消
    if "late_protection_mode" in update and update["late_protection_mode"] not in ("shift", "cancel"):
        return jsonify({"error": "迟到保护模式无效"}), 400

    if "seat_list" in update:
        seats = update["seat_list"]
        if not isinstance(seats, list) or any(
            not isinstance(seat, str) or not seat.strip() for seat in seats
        ):
            return jsonify({"error": "座位配置格式无效"}), 400
        update["seat_list"] = list(dict.fromkeys(seat.strip() for seat in seats))
        if len(update["seat_list"]) > MAX_SEAT_LIST:
            return jsonify({"error": f"最多只能设置 {MAX_SEAT_LIST} 个座位"}), 400
    if "mode" in update and update["mode"] not in RESERVATION_MODES:
        return jsonify({"error": "预约模式无效"}), 400
    if "time" in update:
        time_error = validate_time_config(update["time"])
        if time_error:
            return jsonify({"error": time_error}), 400
    if "notify_mode" in update and update["notify_mode"] not in NOTIFY_MODES:
        return jsonify({"error": "通知范围配置无效"}), 400
    # 开关一律落成 "True"/"False"，避免任意 JSON 值被原样写进配置文档。
    for flag in ("is_reserved", "late_protection"):
        if flag in update:
            update[flag] = "True" if str(update[flag]).strip().lower() == "true" else "False"

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
    """删除账号：配置和这个学号名下的所有记录一起真删，删完当场登出。

    以前只删 user_config_info，在馆流水、抢座尝试、到馆核验这些还留在库里；
    客户端说的是「删除所有配置和所有记录，无法撤回」，就得做到。
    """
    client, db = get_db()
    try:
        removed = db.user_config_info.delete_one(_account_filter(pid)).deleted_count
        purged = {
            "visit_logs": db.visit_logs.delete_many({"pid": pid}).deleted_count,
            "arrival_checks": db.arrival_checks.delete_many({"pid": pid}).deleted_count,
            "pending_segments": db.pending_segments.delete_many({"pid": pid}).deleted_count,
            "reserve_attempts": db.reserve_attempts.delete_many({"pid": pid}).deleted_count,
            "reaction_users": db.reaction_users.delete_many({"uid": pid}).deleted_count,
            # 离线登录用的密码哈希缓存和长期令牌，留着等于账号还能登
            "web_users": db.web_users.delete_many({"uid": pid}).deleted_count,
            "auth_tokens": db.auth_tokens.delete_many({"uid": pid}).deleted_count,
        }
    finally:
        client.close()
    if not removed:
        return jsonify({"error": "未找到该配置"}), 404
    logger.warning("账号 %s 已删除，连带清理 %s", pid, purged)
    session.clear()
    return jsonify({"message": f"学号 {pid} 及全部记录已删除", "logged_out": True, "purged": purged}), 200


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
    # 已入座 / 暂离的预约 delete 会被拒，要走「提前结束」；老客户端不传就两个都试
    try:
        resv_status = int(data.get("resv_status"))
    except (TypeError, ValueError):
        resv_status = None

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
        success, message = library.release_seat(uuid, resv_status)
        return jsonify({"success": success, "message": message}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/my/accounts/<pid>/credit", methods=["GET"])
@own_account_required
def get_account_credit(pid):
    """用户在设置页主动点「查信用分」才查，没有任何定时任务会碰这两个接口。"""
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
        summary, message = library.get_credit_summary()
        if summary is None:
            return jsonify({"error": message}), 502
        records, _, count = library.get_credit_records(page=1, page_num=20)
        return jsonify({
            "score": summary.get("score"),
            "total": summary.get("total"),
            "records": [
                {**r, "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M")
                 if r.get("created_at") else ""}
                for r in (records or [])
            ],
            "count": count,
            "queried_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/my/accounts/<pid>/study_time_consent", methods=["POST"])
@own_account_required
def set_study_time_consent(pid):
    """开/关「真实在馆时长」。

    开：之后每晚会拉这个用户的签到/暂离/返回/结束流水，存进 visit_logs，只有本人可见。
    关：已采集的记录按 keep 处理——
        delete  全部删掉（不可恢复，前端在这之前给过导出的机会）
        blur    只留半小时粒度的起止和总时长，暂离细节删掉
        keep    原样保留，只是不再采集新的
    """
    body = request.get_json(silent=True) or {}
    enabled = str(body.get("enabled", "")).strip().lower() == "true"
    keep = str(body.get("keep") or "keep").strip().lower()
    if not enabled and keep not in ("delete", "blur", "keep"):
        return jsonify({"error": "keep 只能是 delete / blur / keep"}), 400

    client, db = get_db()
    try:
        if not db.user_config_info.find_one(_account_filter(pid), {"_id": 1}):
            return jsonify({"error": "账号不存在"}), 404
        if enabled:
            db.user_config_info.update_one(
                _account_filter(pid),
                {"$set": {study_time.CONSENT_FIELD: True,
                          study_time.CONSENT_AT_FIELD: datetime.now()}},
            )
            return jsonify({"enabled": True, "message": "已开启，今晚起按真实在馆时长统计"}), 200

        db.user_config_info.update_one(
            _account_filter(pid),
            {"$set": {study_time.CONSENT_FIELD: False},
             "$unset": {study_time.CONSENT_AT_FIELD: ""}},
        )
        if keep == "delete":
            n = study_time.clear_user_details(db, pid)
            message = f"已关闭，删除了 {n} 条采集明细"
        elif keep == "blur":
            n = study_time.blur_user_details(db, pid)
            message = f"已关闭，{n} 条记录已降为半小时粒度"
        else:
            n = 0
            message = "已关闭，已有记录原样保留"
        return jsonify({"enabled": False, "keep": keep, "affected": n, "message": message}), 200
    finally:
        client.close()


@app.route("/api/my/accounts/<pid>/study_time_sync", methods=["POST"])
@limiter.limit("3 per 10 minutes")
@own_account_required
def manual_study_time_sync(pid):
    """用户手动触发一次同步：只补自己还没折算的记录，已经算过的不会重算或累加。"""
    client, db = get_db()
    try:
        account = db.user_config_info.find_one(
            _account_filter(pid), {study_time.CONSENT_FIELD: 1})
    finally:
        client.close()
    if not account or not account.get(study_time.CONSENT_FIELD):
        return jsonify({"error": "请先开启「真实在馆时长」"}), 400
    try:
        from scheduled_task import sync_actual_study_time
        result = sync_actual_study_time(pid=pid)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if result["pending"] == 0:
        message = "没有待同步的记录"
    else:
        parts = [f"新写入 {result['synced']} 条"]
        if result["open"]:
            parts.append(f"{result['open']} 条还没结束，等结束后再算")
        if result["failed"]:
            parts.append(f"{result['failed']} 条查询失败，稍后会自动重试")
        message = "，".join(parts)
    return jsonify({**result, "message": message}), 200


@app.route("/api/my/study_time/export", methods=["GET"])
@login_required
def export_study_time():
    """把自己的在馆明细导成 CSV（带 BOM，Excel / WPS 直接双击能打开，中文不乱码）。"""
    import csv
    import io

    uid = current_uid()
    client, db = get_db()
    try:
        rows = study_time.export_rows(db, uid)
    finally:
        client.close()
    buf = io.StringIO()
    fields = ["日期", "座位", "区域", "预约开始", "预约结束", "预约时长(分钟)",
              "签到", "结束", "离座(分钟)", "在馆(分钟)", "精度", "操作流水"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    filename = f"autolib-study-time-{datetime.now():%Y%m%d}.csv"
    return Response(
        "\ufeff" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# 午休功能总开关。已入座的预约现在走 reserve/endAhaed「提前结束」释放，默认开；
# 上游接口再变可以 NAP_ENABLED=0 整套关掉，前端 NAP_DISABLED 也要一起改。
NAP_ENABLED = os.getenv("NAP_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
NAP_DISABLED_MESSAGE = "午休功能维护中 🚧 —— 已入座的预约暂时无法释放，修好后会发公告"


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

    if not NAP_ENABLED:
        client.close()
        return jsonify({"error": NAP_DISABLED_MESSAGE}), 503

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
    if not NAP_ENABLED:
        return jsonify({"error": NAP_DISABLED_MESSAGE}), 503
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
    # 已入座 / 暂离的预约 delete 会被拒，要走「提前结束」；老客户端不传就两个都试
    try:
        resv_status = int(body.get("resv_status"))
    except (TypeError, ValueError):
        resv_status = None

    if not uuid or not seat_name or not start_time or not end_time:
        return jsonify({"error": "缺少必要参数 uuid / seat / start_time / end_time"}), 400
    if start_time >= end_time:
        return jsonify({"error": "结束时间必须晚于开始时间"}), 400

    today = datetime.now().strftime("%Y-%m-%d")
    blocked = _manual_reservation_blackout([
        (f"{today} {start_time}:00", f"{today} {end_time}:00")
    ])
    if blocked:
        return jsonify(blocked), 409

    try:
        from scheduled_task import get_seat_ids
        from utils.library_system import LibrarySystem
        import time as _time

        library = LibrarySystem(
            username=pid,
            password=cfg["vpn_password"],
            vpn_password=cfg["vpn_password"],
        )

        cancel_ok, cancel_msg = library.release_seat(uuid, resv_status)
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
    """已废弃：到馆与否由服务端在馆探针判断，不再需要手动标记。

    保留路由只是为了不让旧版安卓小组件上的「我已到馆」按钮报 404；
    什么都不写，直接告诉客户端不用点。
    """
    return jsonify({
        "arrived": False,
        "deprecated": True,
        "message": "已由服务器自动判断是否到馆，无需手动标记",
    }), 200


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
        cfg = _get_decrypted_cfg(pid) if current_uid() == pid else None
        if cfg:
            vpn_password = vpn_password or cfg.get("vpn_password")

    if not vpn_password:
        return jsonify({"error": "请填写统一身份认证密码", "verified": False}), 400

    def mark_verified(value: bool):
        """Only invalidate the account that is currently authenticated."""
        if current_uid() != pid:
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
        offline_token = _offline_login(pid, vpn_password)
        if offline_token:
            response["logged_in"] = True
            response["uid"] = pid
            response["token"] = offline_token
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
        # 之前因为密码失效挂在面板上的红字，验证通过就换成恢复提示。
        db2.user_config_info.update_one(
            {"pid": pid, "result": CREDENTIAL_INVALID_RESULT},
            {"$set": {"result": CREDENTIAL_REVERIFIED_RESULT}},
        )
        _login_as(db2, pid)
        # Cookie 会话之外再回一个令牌：浏览器存不下 Cookie（Safari + 明文 HTTP、
        # 无痕模式、WebView）时，客户端拿它走 Authorization 头照样是登录态。
        token = _issue_auth_token(db2, pid)
    finally:
        client2.close()
    return jsonify({
        "verified": True,
        "logged_in": True,
        "uid": pid,
        "token": token,
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
        from scheduled_task import calculate_reservation_time, reservation, update_user_config

        segments = calculate_reservation_time(cfg)
        blocked = _manual_reservation_blackout(segments)
        if blocked:
            return jsonify(blocked), 409
        
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

        blocked = _manual_reservation_blackout([(resv_begin, resv_end)])
        if blocked:
            return jsonify(blocked), 409

        # 图书馆只让提前 31 小时下单，超了服务端只回一句「不在提前预约时间范围内」，
        # 这里先算一遍，好告诉用户到底什么时候能约。
        # 用 safe_bookable_at 而不是规则本身：边界是逐秒判定的，按规则值放行的那一刻
        # 发出去照样可能被拒，页面上说「能约了」结果约不上比直接说清楚更糟。
        from scheduled_task import safe_bookable_at
        open_at = safe_bookable_at(resv_begin)
        if open_at > _dt.now():
            return jsonify({
                "error": f"图书馆最多提前 31 小时预约，该时段要到 {open_at:%m-%d %H:%M} 之后才能下单"
            }), 400

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


@app.route("/api/seat_popularity", methods=["GET"])
@limiter.limit("60/minute")
@login_required
def get_seat_popularity():
    """单个座位被多少人放进了抢座优先级。

    只回一个数字，不带学号，自己的账号不计入——读作「除我以外还有几个人」。
    刻意只按座位号单查：早先这里一次吐出全库计数给整张平面图上色，座位重合率本就
    接近 0，那层颜色没挡下过任何冲突，却等于把每个人的意愿清单摊开。现在只有用户
    真的点到某个座位才查那一个，限流是拦住拿脚本逐个刷座位、把全图拼回来的做法。
    """
    seat = (request.args.get("seat") or "").strip()
    if not seat:
        return jsonify({"error": "缺少座位号"}), 400
    try:
        uid = current_uid()
        client, db = get_db()
        count = db.user_config_info.count_documents(
            {"pid": {"$ne": uid}, "seat_list": seat}
        )
        client.close()
        return jsonify({"seat": seat, "count": count}), 200
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
            # 学院/班级由学号推导，不落库：学号改了分类要跟着改，存一份副本只会对不上。
            u.update(parse_student_id(u.get("pid")))
            # 后台那排「成功 N / 异常 N」照着它数，别再在模板里拿 result 文本自己判。
            u["result_state"] = classify_result(u.get("result"))
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
        if "mode" in update and update["mode"] not in RESERVATION_MODES:
            return jsonify({"error": "预约模式无效"}), 400
        if "time" in update:
            time_error = validate_time_config(update["time"])
            if time_error:
                return jsonify({"error": time_error}), 400
        update["updated_at"] = datetime.now()
        client, db = get_db()
        db.user_config_info.update_one({"pid": pid}, {"$set": update})
        client.close()
        return jsonify({"message": "已更新"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== Announcements ====================

_ANN_LEVELS = {"info", "success", "warning", "danger"}

# 一条公告最多挂几个表情按钮；单个表情最长几个字符（复合 emoji 会占好几个码点）
_ANN_REACTIONS_MAX = 8
_ANN_EMOJI_MAX_CHARS = 8
_ANN_CHEER_LABEL_MAX = 12
_ANN_CHEER_EMOJIS_MAX = 24


def _clean_reactions(raw):
    """公告上的表情按钮列表：去空、去重、限长，不合法的整体当没有。"""
    if not isinstance(raw, list):
        return []
    seen = []
    for item in raw:
        if not isinstance(item, str):
            continue
        emoji = item.strip()
        if not emoji or len(emoji) > _ANN_EMOJI_MAX_CHARS or emoji in seen:
            continue
        seen.append(emoji)
        if len(seen) >= _ANN_REACTIONS_MAX:
            break
    return seen


def _clean_cheer(raw):
    """应援按钮：{label: 按钮文字, emojis: 点击后飘出来的表情}，缺一样就当没有。"""
    if not isinstance(raw, dict):
        return None
    label = str(raw.get("label") or "").strip()[:_ANN_CHEER_LABEL_MAX]
    emojis = str(raw.get("emojis") or "").replace(" ", "").strip()[:_ANN_CHEER_EMOJIS_MAX]
    if not label or not emojis:
        return None
    return {"label": label, "emojis": emojis}


def _serialize_announcement(doc):
    return {
        "id": str(doc["_id"]),
        "title": doc.get("title", ""),
        "content": doc.get("content", ""),
        "level": doc.get("level", "info"),
        "pinned": bool(doc.get("pinned", False)),
        "active": bool(doc.get("active", True)),
        "source_review_id": doc.get("source_review_id", ""),
        "source_url": doc.get("source_url", ""),
        "popup_required": bool(doc.get("popup_required", False)),
        "revision": int(doc.get("revision", 1) or 1),
        # 公告栏小互动：这条公告下面挂哪些表情按钮、有没有应援按钮（见 reactions 一节）
        "reactions": _clean_reactions(doc.get("reactions")),
        "cheer": _clean_cheer(doc.get("cheer")),
        "display_from": doc["display_from"].strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(doc.get("display_from"), datetime) else doc.get("display_from", ""),
        "display_until": doc["display_until"].strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(doc.get("display_until"), datetime) else doc.get("display_until", ""),
        "created_at": doc["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(doc.get("created_at"), datetime) else doc.get("created_at", ""),
        "updated_at": doc["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(doc.get("updated_at"), datetime) else doc.get("updated_at", ""),
    }


def _announcement_visible(doc, now=None):
    """Manual announcements remain visible; scheduled ones use [from, until)."""
    current = now or datetime.now()
    start = doc.get("display_from")
    end = doc.get("display_until")
    try:
        if isinstance(start, str) and start:
            start = parse_beijing_datetime(start).replace(tzinfo=None)
        elif isinstance(start, datetime) and start.tzinfo is not None:
            start = start.astimezone(BEIJING_TZ).replace(tzinfo=None)
        if isinstance(end, str) and end:
            end = parse_beijing_datetime(end).replace(tzinfo=None)
        elif isinstance(end, datetime) and end.tzinfo is not None:
            end = end.astimezone(BEIJING_TZ).replace(tzinfo=None)
    except ValueError:
        return False
    if current.tzinfo is not None:
        current = current.astimezone(BEIJING_TZ).replace(tzinfo=None)
    return (not start or current >= start) and (not end or current < end)


_SCHOOL_NOTICE_EFFECTS = {
    "venue_closed", "partial_closure", "reservation_system_unavailable",
    "irrelevant", "uncertain",
}
_SCHOOL_NOTICE_STATUSES = {"pending_review", "confirmed", "cancelled"}


def _review_effective_status(doc, now=None):
    status = doc.get("status", "pending_review")
    if status != "confirmed":
        return status
    try:
        start, end = validate_pause_range(
            doc.get("approved_pause_from"), doc.get("approved_pause_until")
        )
    except ValueError:
        return "invalid"
    current = now or datetime.now(BEIJING_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    current = current.astimezone(BEIJING_TZ)
    if current < start:
        return "confirmed_upcoming"
    if current >= end:
        return "expired"
    return "active"


def _serialize_school_notice_review(doc):
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["id"] = str(doc.get("_id", ""))
    result["status"] = doc.get("status", "pending_review")
    result["effective_status"] = _review_effective_status(doc)
    result["revision"] = int(doc.get("revision", 1) or 1)
    return result


def _review_oid(review_id):
    try:
        return ObjectId(review_id)
    except (InvalidId, TypeError):
        return None


def _normalize_review_range(data):
    start_raw = data.get("pause_from") or data.get("approved_pause_from")
    end_raw = data.get("pause_until") or data.get("approved_pause_until")
    start, end = validate_pause_range(start_raw, end_raw)
    return start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")


def _school_announcement_content(review):
    start = review.get("approved_pause_from", "")
    end = review.get("approved_pause_until", "")
    return "\n".join([
        review.get("ai_summary") or "学校发布闭馆相关通知。",
        f"暂停预约：{start}",
        f"恢复预约：{end}",
        "AutoLib 已拦截与该时段冲突的自动和手动预约。",
        f"学校原文：{review.get('source_url', '')}",
    ])


def _publish_school_announcement(db, review):
    now = datetime.now()
    display_until = parse_beijing_datetime(review["approved_pause_until"]).replace(tzinfo=None)
    ann_doc = {
        "title": f"⚠ {review.get('source_title', '图书馆闭馆通知')}",
        "content": _school_announcement_content(review),
        "level": "danger",
        "pinned": True,
        "active": True,
        "source_review_id": str(review["_id"]),
        "source_url": review.get("source_url", ""),
        "display_from": now,
        "display_until": display_until,
        "popup_required": True,
        "revision": int(review.get("revision", 1) or 1),
        "updated_at": now,
    }
    existing = db.announcements.find_one({"source_review_id": str(review["_id"])})
    if existing:
        db.announcements.update_one({"_id": existing["_id"]}, {"$set": ann_doc})
        return existing["_id"]
    ann_doc["created_at"] = now
    return db.announcements.insert_one(ann_doc).inserted_id


def _deactivate_school_announcement(db, review):
    query = {"source_review_id": str(review["_id"])}
    db.announcements.update_many(query, {"$set": {"active": False, "updated_at": datetime.now()}})


def _queue_admin_status_email(subject, content):
    try:
        credentials = load_admin_credentials()
        if credentials and credentials.get("email"):
            queue_email(credentials["email"], subject, content)
    except AdminCredentialError:
        app.logger.error("管理员凭据损坏，无法发送学校公告状态邮件")


def _queue_user_notice_emails(db, review):
    emails = db.user_config_info.distinct("notify_email", {
        "notify_email": {"$type": "string", "$ne": ""},
    })
    subject = f"[AutoLib] {review.get('source_title', '图书馆闭馆通知')}"
    content = _school_announcement_content(review)
    for email in sorted({str(value).strip() for value in emails if str(value).strip()}):
        queue_email(email, subject, content)


def _owned_seat_interval(target_time):
    try:
        date_part, time_part = str(target_time).split(" ", 1)
        start_part, end_part = time_part.split("-", 1)
        return f"{date_part} {start_part}", f"{date_part} {end_part}"
    except ValueError:
        return None


def _find_local_reservation_conflicts(db, review):
    start, end = validate_pause_range(
        review.get("approved_pause_from"), review.get("approved_pause_until")
    )
    conflicts = []
    cursor = db.user_config_info.find(
        {"owned_seat": {"$exists": True, "$ne": {}}},
        {"pid": 1, "notify_email": 1, "owned_seat": 1},
    )
    for cfg in cursor:
        for seat_name, seats in (cfg.get("owned_seat") or {}).items():
            for seat in seats or []:
                interval = _owned_seat_interval(seat.get("target_time"))
                if not interval:
                    continue
                try:
                    begin = parse_beijing_datetime(interval[0])
                    finish = parse_beijing_datetime(interval[1])
                except ValueError:
                    continue
                if begin < end and finish > start:
                    conflicts.append({
                        "pid": cfg.get("pid", ""),
                        "seat": seat_name,
                        "target_time": seat.get("target_time", ""),
                    })
                    email = str(cfg.get("notify_email") or "").strip()
                    if email:
                        queue_email(
                            email,
                            "[紧急] 已有预约与学校闭馆时间冲突",
                            "\n".join([
                                f"学号：{cfg.get('pid', '')}",
                                f"预约：{seat_name} {seat.get('target_time', '')}",
                                f"闭馆：{review.get('approved_pause_from')} 至 {review.get('approved_pause_until')}",
                                f"学校原文：{review.get('source_url', '')}",
                                "AutoLib 不会自动取消已有预约，请人工处理。",
                            ]),
                        )
    return conflicts


def _apply_school_notice_confirmation(db, review_id, data, require_confirmed=False):
    oid = _review_oid(review_id)
    if not oid:
        return None, ({"error": "无效的审核记录 ID"}, 400)
    try:
        expected_revision = int(data.get("expected_revision"))
    except (TypeError, ValueError):
        return None, ({"error": "expected_revision 必须是整数"}, 400)
    effect_type = data.get("effect_type")
    if effect_type not in {"venue_closed", "partial_closure"}:
        return None, ({"error": "只有闭馆或部分闭馆规则可以启用预约拦截"}, 400)
    try:
        pause_from, pause_until = _normalize_review_range(data)
    except (TypeError, ValueError) as exc:
        return None, ({"error": str(exc)}, 400)
    query = {"_id": oid, "revision": expected_revision}
    if require_confirmed:
        query["status"] = "confirmed"
    updated = db.school_notice_reviews.find_one_and_update(
        query,
        {"$set": {
            "effect_type": effect_type,
            "affected_scope": (data.get("affected_scope") or "all_seats").strip(),
            "approved_pause_from": pause_from,
            "approved_pause_until": pause_until,
            "status": "confirmed",
            "reviewed_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
            "updated_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        }, "$inc": {"revision": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        return None, ({"error": "记录已被修改，请刷新后重试", "code": "REVISION_CONFLICT"}, 409)

    # 新修订确认后，旧修订不应继续拦截或展示。
    old_reviews = list(db.school_notice_reviews.find({
        "source_id": updated.get("source_id"),
        "_id": {"$ne": updated["_id"]},
        "status": "confirmed",
    }))
    for old in old_reviews:
        db.school_notice_reviews.update_one(
            {"_id": old["_id"]},
            {"$set": {"status": "cancelled", "cancel_reason": "superseded", "updated_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds")}},
        )
        _deactivate_school_announcement(db, old)

    announcement_id = _publish_school_announcement(db, updated)
    conflicts = _find_local_reservation_conflicts(db, updated)
    db.school_notice_reviews.update_one(
        {"_id": updated["_id"]},
        {"$set": {
            "announcement_id": str(announcement_id),
            "existing_reservation_conflicts": conflicts,
        }},
    )
    updated["announcement_id"] = str(announcement_id)
    updated["existing_reservation_conflicts"] = conflicts
    _queue_user_notice_emails(db, updated)
    _queue_admin_status_email(
        f"[已启用] {updated.get('source_title', '图书馆闭馆通知')}",
        _school_announcement_content(updated)
        + (f"\n\n发现 {len(conflicts)} 条已有预约冲突，请人工处理。" if conflicts else ""),
    )
    return updated, None


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


@app.route("/api/contacts", methods=["GET"])
def list_contacts():
    """给原生客户端用的「联系方式」条目。

    网页端是渲染 index.html 时把 _contacts() 塞进模板的，安卓端不走模板，
    只能单开一个接口拿同一份数据——否则号码又得写死进 APK 里。
    和网页端一样对游客可见。
    """
    return jsonify({"contacts": _contacts()}), 200


@app.route("/api/library/occupancy", methods=["GET"])
@limiter.limit("30/minute")
def library_occupancy():
    """全馆预约概况：每天 07:10 由调度器汇总一条（utils/occupancy_stats.py）。

    只有区域级的座位数和已约数，不带座位号也不带任何人，所以游客也能看。
    默认回最近 90 天，?days= 最多拉到一年；旧数据永远留在库里，只是不一次全吐。
    """
    try:
        days = int(request.args.get("days", 90))
    except (TypeError, ValueError):
        return jsonify({"error": "days 必须是整数"}), 400
    days = max(1, min(days, 366))

    client, db = get_db()
    docs = list(
        db.seat_occupancy_daily.find({}, {"_id": 0})
        .sort("date", DESCENDING)
        .limit(days)
    )
    client.close()
    docs.reverse()
    for doc in docs:
        for key in ("captured_at", "generated_at"):
            value = doc.get(key)
            doc[key] = value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else None
    return jsonify({"days": docs}), 200


@app.route("/api/announcements", methods=["GET"])
def list_announcements():
    """Public list of active announcements — visible to guests and logged-in users."""
    client, db = get_db()
    docs = [doc for doc in
        db.announcements.find({"active": True})
        .sort([("pinned", DESCENDING), ("created_at", DESCENDING)])
        if _announcement_visible(doc)
    ]
    client.close()
    return jsonify([_serialize_announcement(d) for d in docs]), 200


# ==================== 公告栏小互动 ====================
# 每条公告可以在后台配一排表情按钮（💩 / 👊 / 🌹 之类）和一个应援按钮（「会赢的」），
# 计数按「公告 + 表情」分开记：reactions 一行一个 (ann_id, kind) 的总数，
# reaction_users 额外按 uid 记一行，后台才知道「有多少个学号在玩、各扔了多少」。
# 应援按钮的 kind 固定叫 "cheer"，其余 kind 就是表情本身。

REACTION_CHEER_KIND = "cheer"
# 连点会在前端攒着一起发，一次最多认 20 下，多的丢掉——防的是改包刷榜，不是手速
REACTION_MAX_PER_POST = 20


def _reaction_kinds(ann):
    """这条公告允许的 kind：配置里的每个表情 + 有应援按钮时的 cheer。"""
    kinds = list(_clean_reactions(ann.get("reactions")))
    if _clean_cheer(ann.get("cheer")):
        kinds.append(REACTION_CHEER_KIND)
    return kinds


def _reaction_totals(db, ann_ids):
    """{ann_id: {kind: count}}，只含查询到的公告；没人点过的表情不出现，前端按 0 显示。"""
    totals = {ann_id: {} for ann_id in ann_ids}
    for doc in db.reactions.find({"ann_id": {"$in": list(ann_ids)}}):
        totals.setdefault(doc["ann_id"], {})[doc.get("kind", "")] = int(doc.get("count", 0) or 0)
    return totals


def _visible_announcement_ids(db):
    return [
        str(doc["_id"]) for doc in db.announcements.find({"active": True}, {"display_from": 1, "display_until": 1})
        if _announcement_visible(doc)
    ]


@app.route("/api/reactions", methods=["GET"])
@limiter.limit("60/minute")
def list_reactions():
    """公开总数：每条可见公告下每个表情的数字，不含任何学号。"""
    client, db = get_db()
    try:
        return jsonify({"totals": _reaction_totals(db, _visible_announcement_ids(db))}), 200
    finally:
        client.close()


@app.route("/api/reactions", methods=["POST"])
@limiter.limit("30/minute")
def add_reaction():
    """收的是增量 n 而不是固定 +1：连点在前端攒 700ms 才发一包，省往返。"""
    body = request.get_json(silent=True) or {}
    ann_id = str(body.get("ann_id") or "").strip()
    kind = str(body.get("kind") or "").strip()
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "无效的公告"}), 400
    try:
        n = int(body.get("n", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "无效的数量"}), 400
    n = max(1, min(n, REACTION_MAX_PER_POST))

    uid = _ensure_uid()
    now = datetime.now()
    client, db = get_db()
    try:
        ann = db.announcements.find_one({"_id": oid, "active": True})
        if not ann or not _announcement_visible(ann) or kind not in _reaction_kinds(ann):
            return jsonify({"error": "无效的类型"}), 400
        db.reactions.update_one(
            {"_id": f"{ann_id}:{kind}"},
            {
                "$inc": {"count": n},
                "$set": {"ann_id": ann_id, "kind": kind, "updated_at": now},
            },
            upsert=True,
        )
        db.reaction_users.update_one(
            {"_id": f"{ann_id}:{kind}:{uid}"},
            {
                "$inc": {"count": n},
                "$set": {
                    "ann_id": ann_id,
                    "kind": kind,
                    "uid": uid,
                    # 没验证学号的访客也能点，但统计时要和真学号分开数
                    "is_student": not uid.startswith("guest_"),
                    "last_at": now,
                },
                "$setOnInsert": {"first_at": now},
            },
            upsert=True,
        )
        totals = _reaction_totals(db, [ann_id])
    finally:
        client.close()
    return jsonify({"totals": totals}), 200


@app.route("/api/admin/reactions", methods=["GET"])
@admin_required
def admin_list_reactions():
    """后台看板：每条公告下每种表情的总数 + 参与人数（学号和游客分开数）。"""
    client, db = get_db()
    try:
        by_announcement = {}
        for doc in db.reactions.find({}):
            ann_id, kind = doc.get("ann_id", ""), doc.get("kind", "")
            by_announcement.setdefault(ann_id, {})[kind] = {
                "count": int(doc.get("count", 0) or 0),
                "students": len(db.reaction_users.distinct(
                    "uid", {"ann_id": ann_id, "kind": kind, "is_student": True})),
                "guests": len(db.reaction_users.distinct(
                    "uid", {"ann_id": ann_id, "kind": kind, "is_student": False})),
            }
        payload = {
            "by_announcement": by_announcement,
            "active_students": len(db.reaction_users.distinct("uid", {"is_student": True})),
            "active_guests": len(db.reaction_users.distinct("uid", {"is_student": False})),
        }
    finally:
        client.close()
    return jsonify(payload), 200


@app.route("/api/admin/school_notice_reviews", methods=["GET"])
@admin_required
def admin_list_school_notice_reviews():
    client, db = get_db()
    docs = list(db.school_notice_reviews.find({}).sort("created_at", DESCENDING))
    client.close()
    return jsonify([_serialize_school_notice_review(doc) for doc in docs]), 200


@app.route("/api/admin/school_notice_reviews/<review_id>", methods=["GET"])
@admin_required
def admin_get_school_notice_review(review_id):
    oid = _review_oid(review_id)
    if not oid:
        return jsonify({"error": "无效的审核记录 ID"}), 400
    client, db = get_db()
    doc = db.school_notice_reviews.find_one({"_id": oid})
    client.close()
    if not doc:
        return jsonify({"error": "审核记录不存在"}), 404
    return jsonify(_serialize_school_notice_review(doc)), 200


@app.route("/api/admin/school_notice_reviews/<review_id>/confirm", methods=["POST"])
@admin_required
def admin_confirm_school_notice_review(review_id):
    data = request.get_json(silent=True) or {}
    client, db = get_db()
    try:
        updated, error = _apply_school_notice_confirmation(db, review_id, data)
        if error:
            payload, status = error
            return jsonify(payload), status
        return jsonify(_serialize_school_notice_review(updated)), 200
    finally:
        client.close()


@app.route("/api/admin/school_notice_reviews/<review_id>", methods=["PUT"])
@admin_required
def admin_update_school_notice_review(review_id):
    data = request.get_json(silent=True) or {}
    client, db = get_db()
    try:
        updated, error = _apply_school_notice_confirmation(
            db, review_id, data, require_confirmed=True
        )
        if error:
            payload, status = error
            return jsonify(payload), status
        return jsonify(_serialize_school_notice_review(updated)), 200
    finally:
        client.close()


@app.route("/api/admin/school_notice_reviews/<review_id>/cancel", methods=["POST"])
@admin_required
def admin_cancel_school_notice_review(review_id):
    oid = _review_oid(review_id)
    if not oid:
        return jsonify({"error": "无效的审核记录 ID"}), 400
    data = request.get_json(silent=True) or {}
    try:
        expected_revision = int(data.get("expected_revision"))
    except (TypeError, ValueError):
        return jsonify({"error": "expected_revision 必须是整数"}), 400
    client, db = get_db()
    try:
        updated = db.school_notice_reviews.find_one_and_update(
            {"_id": oid, "revision": expected_revision},
            {"$set": {
                "status": "cancelled",
                "cancel_reason": (data.get("reason") or "管理员取消").strip(),
                "updated_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
            }, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            return jsonify({"error": "记录已被修改，请刷新后重试", "code": "REVISION_CONFLICT"}), 409
        _deactivate_school_announcement(db, updated)
        _queue_admin_status_email(
            f"[已取消] {updated.get('source_title', '图书馆闭馆通知')}",
            f"预约暂停规则已取消。\n学校原文：{updated.get('source_url', '')}",
        )
        return jsonify(_serialize_school_notice_review(updated)), 200
    finally:
        client.close()


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
        "reactions": _clean_reactions(data.get("reactions")),
        "cheer": _clean_cheer(data.get("cheer")),
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
    if "reactions" in data:
        update["reactions"] = _clean_reactions(data.get("reactions"))
    if "cheer" in data:
        update["cheer"] = _clean_cheer(data.get("cheer"))
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


def _purge_reactions(db, ann_id):
    """公告没了，它下面的表情计数也跟着走。

    不清的话 reactions / reaction_users 会留下一堆认不出是谁的孤儿行：前台看不见
    （_visible_announcement_ids 会把它们滤掉），但后台看板的 by_announcement 里会
    一直挂着一条没有标题的记录，而且再没有任何入口能删掉它——公告都不在了，
    删除接口也就够不着这些行了。所以只能在删公告的同一步里清掉。

    Returns:
        Tuple[int, int]: (清掉的计数行, 清掉的参与人行)
    """
    totals = db.reactions.delete_many({"ann_id": ann_id}).deleted_count
    users = db.reaction_users.delete_many({"ann_id": ann_id}).deleted_count
    return totals, users


@app.route("/api/admin/announcements/<ann_id>", methods=["DELETE"])
@admin_required
def admin_delete_announcement(ann_id):
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "无效的公告 ID"}), 400
    client, db = get_db()
    try:
        result = db.announcements.delete_one({"_id": oid})
        if not result.deleted_count:
            return jsonify({"error": "公告不存在"}), 404
        # 用 str(oid) 而不是 URL 里那串原文：ObjectId() 连大写十六进制也认，
        # 而 reactions 里存的是前端拿到的标准小写形式，照原文删会一行都匹配不上。
        totals, users = _purge_reactions(db, str(oid))
    finally:
        client.close()
    return jsonify({
        "message": "已删除",
        "removed_reactions": totals,
        "removed_reaction_users": users,
    }), 200


HEATMAP_DAYS = 371  # 53 周，正好铺满一整年的热力图


# 分钟数取哪个字段：有真实在馆时长就用它（只有用户同意采集过才会有，关掉开关时
# 选「保留 / 模糊保留」留下的也算数），没有的（当天、老记录没 resv_id、被删掉的）退回预约时长
MINUTES_EXPR = {"$ifNull": ["$actual_minutes", {"$ifNull": ["$planned_duration_minutes", 0]}]}


def _visit_totals(db, match):
    """聚合出次数和分钟数。放在数据库端算，避免把全部日志拉进内存。"""
    rows = list(db.visit_logs.aggregate([
        {"$match": match},
        {"$group": {
            "_id": None,
            "visits": {"$sum": 1},
            "minutes": {"$sum": MINUTES_EXPR},
        }},
    ]))
    if not rows:
        return 0, 0
    return rows[0].get("visits", 0), rows[0].get("minutes", 0)


@app.route("/api/my/visit_stats", methods=["GET"])
@login_required
def my_visit_stats():
    uid = current_uid()
    client, db = get_db()
    account = db.user_config_info.find_one(
        _account_filter(uid), {"_id": 1, study_time.CONSENT_FIELD: 1})
    if not account:
        client.close()
        return jsonify({"total_visits": 0, "total_minutes": 0,
                        "this_week_visits": 0, "this_week_minutes": 0,
                        "recent": [], "daily": [], "heatmap_days": HEATMAP_DAYS,
                        "real_time": False}), 200
    real_time = bool(account.get(study_time.CONSENT_FIELD))

    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    owned = {"pid": uid}

    # 累计值必须走聚合：早先的实现只取最近 200 条来求和，
    # 自习超过 200 次后「累计」就永远停在 200 了。
    total_visits, total_minutes = _visit_totals(db, owned)
    this_week_visits, this_week_minutes = _visit_totals(
        db, {**owned, "planned_begin": {"$gte": week_start}})

    # 热力图按天聚合，从这个人有记录的第一天起全部给（App 从第一天画到今天）。
    # 一天一条，几年也就一千来条。planned_begin 存的是本地时间的 naive datetime，
    # $dateToString 不带 timezone 正好原样取出当初写入的那一天。
    daily = [
        {
            "date": row["_id"],
            "visits": row.get("visits", 0),
            "minutes": row.get("minutes", 0),
        }
        for row in db.visit_logs.aggregate([
            {"$match": {**owned, "planned_begin": {"$type": "date"}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$planned_begin"}},
                "visits": {"$sum": 1},
                "minutes": {"$sum": MINUTES_EXPR},
            }},
            {"$sort": {"_id": 1}},
        ])
    ]

    recent = []
    for l in db.visit_logs.find(
        owned,
        {"_id": 0, "seat_name": 1, "location": 1,
         "planned_begin": 1, "planned_duration_minutes": 1, "actual_minutes": 1},
    ).sort("planned_begin", DESCENDING).limit(10):
        pb = l.get("planned_begin")
        is_actual = l.get("actual_minutes") is not None
        recent.append({
            "date": pb.strftime("%Y-%m-%d") if isinstance(pb, datetime) else str(pb)[:10],
            "seat_name": l.get("seat_name", ""),
            "location": l.get("location", ""),
            "duration_minutes": (l.get("actual_minutes") if is_actual
                                 else l.get("planned_duration_minutes", 0)),
            "actual": is_actual,
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
        "real_time": real_time,
    }), 200


@app.route("/api/my/reservation_results", methods=["GET"])
@login_required
def my_reservation_results():
    """Return the latest reservation result for the logged-in student."""
    uid = current_uid()
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
        state = classify_result(result_text)
        out.append({
            "pid": r.get("pid", ""),
            "result": result_text,
            "state": state,
            "success": state == RESULT_SUCCESS,
            # 休息日和闭馆都是「按配置没约」，占位待换约是「还没到能约的时候」，
            # 两种都不是失败，前端不该标红。
            "skipped": state == RESULT_SKIPPED,
            "pending": state == RESULT_PENDING,
            "failed": state == RESULT_FAILED,
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
