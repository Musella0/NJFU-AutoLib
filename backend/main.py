import os
import io
import base64
from functools import wraps
from flask import Flask, render_template, jsonify, request, session
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from utils import config

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())
app.permanent_session_lifetime = timedelta(days=30)
CORS(app)

from blueprints.database_bp import database_bp
app.register_blueprint(database_bp, url_prefix="/db")

# Admin TOTP secret from env
ADMIN_TOTP_SECRET = os.environ.get("ADMIN_TOTP_SECRET", "")


def get_db():
    client = MongoClient(config.get_mongo_uri())
    return client, client.AutoLib


# ==================== Decorators ====================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "web_uid" not in session:
            return jsonify({"error": "请先登录", "need_login": True}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"error": "需要管理员权限", "need_admin_login": True}), 403
        return f(*args, **kwargs)
    return decorated


# ==================== Pages ====================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/admin")
def admin():
    return render_template("admin.html")


# ==================== User Auth ====================

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json()
    uid = data.get("username", "").strip()
    password = data.get("password", "")
    if not uid or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(password) < 4:
        return jsonify({"error": "密码至少4位"}), 400
    if len(uid) < 2:
        return jsonify({"error": "用户名至少2位"}), 400

    client, db = get_db()
    existing = db.web_users.find_one({"uid": uid})
    if existing:
        client.close()
        return jsonify({"error": "该用户名已注册，请直接登录"}), 409

    db.web_users.insert_one({
        "uid": uid,
        "password": generate_password_hash(password),
        "created_at": datetime.now()
    })
    client.close()

    session.permanent = True
    session["web_uid"] = uid
    return jsonify({"message": "注册成功", "uid": uid}), 200


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    uid = data.get("username", "").strip()
    password = data.get("password", "")
    if not uid or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    client, db = get_db()
    user = db.web_users.find_one({"uid": uid})
    client.close()

    if not user:
        return jsonify({"error": "用户不存在"}), 404
    if not check_password_hash(user["password"], password):
        return jsonify({"error": "密码错误"}), 401

    session.permanent = True
    session["web_uid"] = uid
    return jsonify({"message": "登录成功", "uid": uid}), 200


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "已退出"}), 200


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    if "web_uid" in session:
        return jsonify({"logged_in": True, "uid": session["web_uid"]}), 200
    return jsonify({"logged_in": False}), 200


# ==================== Admin Auth (2FA) ====================

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json()
    totp_code = data.get("totp_code", "")

    # TOTP verification (required)
    if not ADMIN_TOTP_SECRET:
        return jsonify({"error": "请先在 .env 中配置 ADMIN_TOTP_SECRET"}), 400

    import pyotp
    totp = pyotp.TOTP(ADMIN_TOTP_SECRET)
    if not totp.verify(totp_code, valid_window=1):
        return jsonify({"error": "动态验证码错误"}), 401

    session["is_admin"] = True
    return jsonify({"message": "管理员登录成功"}), 200


@app.route("/api/admin/totp_setup", methods=["GET"])
def admin_totp_setup():
    """Generate TOTP QR code for first-time setup"""
    if not ADMIN_TOTP_SECRET:
        return jsonify({"error": "请先在 .env 中配置 ADMIN_TOTP_SECRET"}), 400

    import pyotp
    import qrcode

    totp = pyotp.TOTP(ADMIN_TOTP_SECRET)
    uri = totp.provisioning_uri(name="admin", issuer_name="AutoLib Admin")

    # Generate QR code as base64 image
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode()

    return jsonify({
        "qr_image": f"data:image/png;base64,{b64}",
        "secret": ADMIN_TOTP_SECRET,
        "uri": uri
    }), 200


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return jsonify({"message": "已退出管理后台"}), 200


@app.route("/api/admin/me", methods=["GET"])
def admin_me():
    return jsonify({"is_admin": bool(session.get("is_admin"))}), 200


# ==================== User Multi-Account API ====================

@app.route("/api/my/accounts", methods=["GET"])
@login_required
def get_my_accounts():
    """Get all library accounts under current web user"""
    uid = session["web_uid"]
    client, db = get_db()
    accounts = list(db.user_config_info.find(
        {"web_uid": uid},
        {"_id": 0, "web_password": 0, "vpn_password": 0, "lib_password": 0}
    ))
    client.close()
    for a in accounts:
        if isinstance(a.get("updated_at"), datetime):
            a["updated_at"] = a["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(accounts), 200


@app.route("/api/my/accounts/<pid>", methods=["GET"])
@login_required
def get_my_account(pid):
    """Get single account config (with passwords, for editing)"""
    uid = session["web_uid"]
    client, db = get_db()
    cfg = db.user_config_info.find_one(
        {"web_uid": uid, "pid": pid},
        {"_id": 0, "web_password": 0}
    )
    client.close()
    if not cfg:
        return jsonify({}), 200
    if isinstance(cfg.get("updated_at"), datetime):
        cfg["updated_at"] = cfg["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(cfg), 200


@app.route("/api/my/accounts/<pid>", methods=["POST"])
@login_required
def save_my_account(pid):
    """Save/update a library account config"""
    uid = session["web_uid"]
    data = request.get_json()
    allowed = ["vpn_password", "lib_password", "seat_list", "mode", "time",
               "is_reserved", "late_protection",
               "notify_email", "notify_serverchan_key", "verified"]
    update = {k: v for k, v in data.items() if k in allowed and v is not None}
    update["pid"] = pid
    update["web_uid"] = uid
    update["updated_at"] = datetime.now()

    # Check existing record for defaults and password change detection
    client_tmp, db_tmp = get_db()
    existing = db_tmp.user_config_info.find_one({"pid": pid, "web_uid": uid})
    client_tmp.close()

    # If frontend explicitly passes "verified" (e.g., right after a successful
    # verify-before-save), respect it and skip the auto-reset below.
    explicit_verified = "verified" in update

    if not existing:
        if "priority" not in update:
            update["priority"] = 0
        if not explicit_verified:
            update["verified"] = False
    else:
        # Reset verified if passwords actually changed (unless explicitly set)
        if not explicit_verified:
            if ("vpn_password" in update and update["vpn_password"] != existing.get("vpn_password")) or \
               ("lib_password" in update and update["lib_password"] != existing.get("lib_password")):
                update["verified"] = False

    client, db = get_db()
    db.user_config_info.update_one(
        {"pid": pid, "web_uid": uid},
        {"$set": update},
        upsert=True
    )
    client.close()
    return jsonify({"message": "配置已保存"}), 200


@app.route("/api/my/accounts/<pid>", methods=["DELETE"])
@login_required
def delete_my_account(pid):
    """Delete a library account from current web user"""
    uid = session["web_uid"]
    client, db = get_db()
    result = db.user_config_info.delete_one({"pid": pid, "web_uid": uid})
    client.close()
    if result.deleted_count:
        return jsonify({"message": f"学号 {pid} 已删除"}), 200
    return jsonify({"error": "未找到该配置"}), 404


@app.route("/api/my/accounts/<pid>/reservations", methods=["GET"])
@login_required
def get_account_reservations(pid):
    """Live query reservations for a specific library account"""
    uid = session["web_uid"]
    client, db = get_db()
    cfg = db.user_config_info.find_one({"pid": pid, "web_uid": uid})
    client.close()

    if not cfg or not cfg.get("vpn_password") or not cfg.get("lib_password"):
        return jsonify({"error": "请先保存 VPN 和图书馆密码"}), 400

    try:
        from utils.library_system import LibrarySystem
        library = LibrarySystem(
            username=pid,
            password=cfg["lib_password"],
            vpn_password=cfg["vpn_password"]
        )
        reservations, message = library.get_reservation_info()
        return jsonify({"message": message, "reservations": reservations or []}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/my/accounts/<pid>/cancel", methods=["POST"])
@login_required
def cancel_account_reservation(pid):
    """Cancel a reservation for a specific library account"""
    uid = session["web_uid"]
    data = request.get_json()
    uuid = data.get("uuid")
    if not uuid:
        return jsonify({"error": "缺少 uuid"}), 400

    client, db = get_db()
    cfg = db.user_config_info.find_one({"pid": pid, "web_uid": uid})
    client.close()

    if not cfg or not cfg.get("vpn_password") or not cfg.get("lib_password"):
        return jsonify({"error": "请先保存密码配置"}), 400

    try:
        from utils.library_system import LibrarySystem
        library = LibrarySystem(
            username=pid,
            password=cfg["lib_password"],
            vpn_password=cfg["vpn_password"]
        )
        success, message = library.delete_seat(uuid)
        return jsonify({"success": success, "message": message}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/my/accounts/<pid>/verify", methods=["POST"])
@login_required
def verify_account(pid):
    """Verify VPN and library credentials in two distinct steps.

    Accepts passwords either from the request body (for pre-save verification
    in addAccount flow) or falls back to the saved account config in DB.
    Returns `failed_at` to tell the frontend which step failed.
    """
    uid = session["web_uid"]
    data = request.get_json(silent=True) or {}

    vpn_password = data.get("vpn_password")
    lib_password = data.get("lib_password")

    # Fall back to DB if passwords not provided in body
    if not vpn_password or not lib_password:
        client, db = get_db()
        cfg = db.user_config_info.find_one({"pid": pid, "web_uid": uid})
        client.close()
        if cfg:
            vpn_password = vpn_password or cfg.get("vpn_password")
            lib_password = lib_password or cfg.get("lib_password")

    if not vpn_password or not lib_password:
        return jsonify({"error": "请填写 VPN 和图书馆密码", "verified": False}), 400

    def mark_verified(value: bool):
        """Update verified flag in DB. No-op if account doesn't exist yet."""
        c, d = get_db()
        d.user_config_info.update_one(
            {"pid": pid, "web_uid": uid},
            {"$set": {"verified": value}}
        )
        c.close()

    # Step 1: VPN login (独立验证 VPN 密码)
    try:
        from utils.vpn_system import VPNSystem
        vpn = VPNSystem(pid, vpn_password)
        if not vpn.vpn_login():
            mark_verified(False)
            return jsonify({
                "verified": False,
                "failed_at": "vpn",
                "error": "VPN 密码错误：请检查统一身份认证（webvpn）密码是否正确"
            }), 200
    except Exception as e:
        mark_verified(False)
        return jsonify({
            "verified": False,
            "failed_at": "vpn",
            "error": f"VPN 登录异常：{str(e)}"
        }), 200

    # Step 2: Library login (复用已登录的 VPN 会话，只验证图书馆密码)
    try:
        from utils.library_system import LibrarySystem
        LibrarySystem(
            username=pid,
            password=lib_password,
            session=vpn.session
        )
    except Exception as e:
        mark_verified(False)
        error_msg = str(e)
        if "用户名或密码错误" in error_msg:
            user_msg = f"图书馆密码错误：请检查 IC 空间系统密码（默认密码为 njfu{pid}!）"
        else:
            user_msg = f"图书馆登录失败：{error_msg}"
        return jsonify({
            "verified": False,
            "failed_at": "library",
            "error": user_msg
        }), 200

    # 两步都通过
    mark_verified(True)
    return jsonify({
        "verified": True,
        "message": "验证成功，VPN 和图书馆密码均正确"
    }), 200


@app.route("/api/my/accounts/<pid>/result", methods=["GET"])
@login_required
def get_account_result(pid):
    uid = session["web_uid"]
    client, db = get_db()
    cfg = db.user_config_info.find_one(
        {"pid": pid, "web_uid": uid}, {"result": 1, "_id": 0}
    )
    client.close()
    return jsonify({"result": cfg.get("result", "") if cfg else ""}), 200


@app.route("/api/my/accounts/<pid>/reserve_now", methods=["POST"])
@login_required
def reserve_now(pid):
    """立即执行预约（非定时任务）"""
    uid = session["web_uid"]
    client, db = get_db()
    cfg = db.user_config_info.find_one({"pid": pid, "web_uid": uid})
    client.close()

    if not cfg:
        return jsonify({"error": "未找到该账号配置"}), 404

    if not cfg.get("vpn_password") or not cfg.get("lib_password"):
        return jsonify({"error": "请先保存 VPN 和图书馆密码"}), 400

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
            {"pid": pid, "web_uid": uid}, {"result": 1, "_id": 0}
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


# ==================== Public API (游客模式) ====================

@app.route("/api/public/seats", methods=["GET"])
def get_public_seats():
    """游客可访问的座位列表，无需登录"""
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

@app.route("/api/seats", methods=["GET"])
@login_required
def get_all_seats():
    """已登录用户访问座位列表"""
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
        client, db = get_db()
        db.user_config_info.update_one({"pid": pid}, {"$set": {field: value}})
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
                    "is_reserved", "late_protection"]
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


@app.route("/api/my/reservation_results", methods=["GET"])
@login_required
def my_reservation_results():
    """Aggregate latest reservation results across all the user's library accounts.
    
    仅在用户有 web_uid session 时返回数据。
    """
    if "web_uid" not in session:
        return jsonify({"error": "请先登录", "need_login": True}), 401
    
    uid = session["web_uid"]
    client, db = get_db()
    rows = list(db.user_config_info.find(
        {"web_uid": uid, "result": {"$exists": True, "$ne": ""}},
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


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5003)

