from __future__ import annotations

import calendar
import json
import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import NoMatchFound
from werkzeug.security import check_password_hash, generate_password_hash


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]

        os.environ.setdefault(key, value)


BASE_DIR = Path(__file__).resolve().parent
load_env_file(BASE_DIR / ".env")

DEFAULT_DATABASE = BASE_DIR / "data" / "mood_barometer.sqlite3"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
AUTH_BACKGROUND_DIR = STATIC_DIR / "login-backgrounds"
AUTH_BACKGROUND_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

MOODS = [
    {"emoji": "😄", "label": "开心"},
    {"emoji": "🙂", "label": "平静"},
    {"emoji": "😌", "label": "放松"},
    {"emoji": "😟", "label": "担心"},
    {"emoji": "😢", "label": "难过"},
    {"emoji": "😡", "label": "生气"},
]

GRADES = ("2024", "2025", "2026")
PROGRAMS = ("AP", "IB")
REGISTRATION_IP_LIMIT_SETTING = "registration_ip_limit_per_24h"
DEFAULT_REGISTRATION_IP_LIMIT = 5
REGISTRATION_IP_LIMIT_MAX = 100
REGISTRATION_WINDOW_HOURS = 24
AUDIT_RETENTION_DAYS = 30
TRUSTED_PROXY_HOSTS = {"127.0.0.1", "::1", "localhost"}

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
_USER_NOT_LOADED = object()


def create_app(test_config: dict[str, Any] | None = None) -> FastAPI:
    app = FastAPI()
    config = {
        "SECRET_KEY": os.environ.get("MOOD_SECRET_KEY", "dev-secret-key-change-me"),
        "DATABASE": os.environ.get("MOOD_DB_PATH", str(DEFAULT_DATABASE)),
        "ADMIN_NICKNAME": os.environ.get("MOOD_ADMIN_NICKNAME", "").strip(),
    }

    if test_config:
        config.update(test_config)

    app.state.config = config
    app.add_middleware(
        SessionMiddleware,
        secret_key=config["SECRET_KEY"],
        same_site="lax",
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def close_database_after_request(request: Request, call_next):
        response = None
        try:
            response = await call_next(request)
            if should_log_access(request):
                record_activity(
                    request,
                    "access",
                    f"{request.method} {request.url.path}",
                    status_code=response.status_code,
                )
            maybe_prune_audit_rows(request)
            return response
        finally:
            close_db(request)

    register_template_helpers()
    register_routes(app)
    init_db(app)

    return app


def database_path(app: FastAPI) -> Path:
    return Path(app.state.config["DATABASE"])


def get_db(request: Request) -> sqlite3.Connection:
    db = getattr(request.state, "db", None)
    if db is None:
        path = database_path(request.app)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        request.state.db = db
    return db


def close_db(request: Request) -> None:
    db = getattr(request.state, "db", None)
    if db is not None:
        db.close()
        request.state.db = None


def init_db(app: FastAPI) -> None:
    path = database_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                real_name TEXT NOT NULL,
                nickname TEXT NOT NULL UNIQUE,
                grade TEXT NOT NULL DEFAULT '',
                program TEXT NOT NULL DEFAULT '',
                is_admin INTEGER NOT NULL DEFAULT 0,
                privacy_consent_at TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mood_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mood_emoji TEXT NOT NULL,
                reason TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );

            CREATE INDEX IF NOT EXISTS idx_mood_entries_user_date
                ON mood_entries (user_id, entry_date);

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS registration_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL,
                user_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );

            CREATE INDEX IF NOT EXISTS idx_registration_attempts_ip_created
                ON registration_attempts (ip_address, created_at);

            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_nickname TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status_code INTEGER,
                event_type TEXT NOT NULL,
                action TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );

            CREATE INDEX IF NOT EXISTS idx_activity_logs_created
                ON activity_logs (created_at);

            CREATE INDEX IF NOT EXISTS idx_activity_logs_user_created
                ON activity_logs (user_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_activity_logs_ip_created
                ON activity_logs (ip_address, created_at);
            """
        )
        ensure_user_profile_columns(db)
        ensure_default_settings(db)
        prune_old_audit_rows(db)
        promote_configured_admin(db, app.state.config.get("ADMIN_NICKNAME", ""))
        db.commit()


def ensure_user_profile_columns(db: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if "grade" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN grade TEXT NOT NULL DEFAULT ''")
    if "program" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN program TEXT NOT NULL DEFAULT ''")
    if "is_admin" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    if "privacy_consent_at" not in columns:
        db.execute(
            "ALTER TABLE users ADD COLUMN privacy_consent_at TEXT NOT NULL DEFAULT ''"
        )


def ensure_default_settings(db: sqlite3.Connection) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        """,
        (
            REGISTRATION_IP_LIMIT_SETTING,
            str(DEFAULT_REGISTRATION_IP_LIMIT),
            now,
        ),
    )


def prune_old_audit_rows(db: sqlite3.Connection) -> None:
    cutoff = (datetime.now() - timedelta(days=AUDIT_RETENTION_DAYS)).isoformat(
        timespec="seconds"
    )
    db.execute("DELETE FROM activity_logs WHERE created_at < ?", (cutoff,))
    db.execute("DELETE FROM registration_attempts WHERE created_at < ?", (cutoff,))


def maybe_prune_audit_rows(request: Request) -> None:
    today = date.today().isoformat()
    if getattr(request.app.state, "audit_pruned_on", None) == today:
        return

    prune_old_audit_rows(get_db(request))
    get_db(request).commit()
    request.app.state.audit_pruned_on = today


def promote_configured_admin(db: sqlite3.Connection, nickname: str | None) -> None:
    admin_nickname = (nickname or "").strip()
    if not admin_nickname:
        return

    db.execute(
        "UPDATE users SET is_admin = 1 WHERE nickname = ?",
        (admin_nickname,),
    )


def get_current_user(request: Request) -> sqlite3.Row | None:
    cached_user = getattr(request.state, "user", _USER_NOT_LOADED)
    if cached_user is not _USER_NOT_LOADED:
        return cached_user

    user_id = request.session.get("user_id")
    if user_id is None:
        request.state.user = None
        return None

    request.state.user = get_db(request).execute(
        """
        SELECT
            id,
            real_name,
            nickname,
            grade,
            program,
            is_admin,
            privacy_consent_at,
            created_at
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    return request.state.user


def require_user(request: Request) -> sqlite3.Row | RedirectResponse:
    user = get_current_user(request)
    if user is None:
        return redirect_to(request, "login", next=request.url.path)
    return user


def require_admin(request: Request) -> sqlite3.Row | RedirectResponse:
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    if not user["is_admin"]:
        flash(request, "只有管理员可以访问后台。", "error")
        return redirect_to(request, "profile")

    return user


def get_client_ip(request: Request) -> str:
    direct_ip = request.client.host if request.client else ""
    if direct_ip in TRUSTED_PROXY_HOSTS:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        forwarded_ip = forwarded_for.split(",", 1)[0].strip()
        if forwarded_ip:
            return forwarded_ip
    return direct_ip or "unknown"


def get_app_setting(request: Request, key: str, default: str) -> str:
    row = get_db(request).execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return default
    return str(row["value"])


def set_app_setting(request: Request, key: str, value: str) -> None:
    get_db(request).execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, datetime.now().isoformat(timespec="seconds")),
    )


def get_registration_ip_limit(request: Request) -> int:
    raw_value = get_app_setting(
        request,
        REGISTRATION_IP_LIMIT_SETTING,
        str(DEFAULT_REGISTRATION_IP_LIMIT),
    )
    try:
        limit = int(raw_value)
    except ValueError:
        return DEFAULT_REGISTRATION_IP_LIMIT
    if limit < 1 or limit > REGISTRATION_IP_LIMIT_MAX:
        return DEFAULT_REGISTRATION_IP_LIMIT
    return limit


def count_recent_registration_attempts(request: Request, ip_address: str) -> int:
    cutoff = (datetime.now() - timedelta(hours=REGISTRATION_WINDOW_HOURS)).isoformat(
        timespec="seconds"
    )
    return int(
        get_db(request)
        .execute(
            """
            SELECT COUNT(*) AS count
            FROM registration_attempts
            WHERE ip_address = ? AND created_at >= ?
            """,
            (ip_address, cutoff),
        )
        .fetchone()["count"]
    )


def create_registration_attempt(
    request: Request,
    ip_address: str,
    nickname: str,
    result: str,
) -> int:
    cursor = get_db(request).execute(
        """
        INSERT INTO registration_attempts
            (ip_address, nickname, result, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            ip_address,
            nickname,
            result,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    get_db(request).commit()
    return int(cursor.lastrowid)


def update_registration_attempt(
    request: Request,
    attempt_id: int,
    result: str,
    user_id: int | None = None,
) -> None:
    get_db(request).execute(
        """
        UPDATE registration_attempts
        SET result = ?, user_id = ?
        WHERE id = ?
        """,
        (result, user_id, attempt_id),
    )
    get_db(request).commit()


def should_log_access(request: Request) -> bool:
    return not request.url.path.startswith("/static/")


def record_activity(
    request: Request,
    event_type: str,
    action: str,
    *,
    status_code: int | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: int | None = None,
    user_nickname: str | None = None,
    ip_address: str | None = None,
) -> None:
    current_user = None if user_id is not None else get_current_user(request)
    if current_user is not None:
        user_id = int(current_user["id"])
        user_nickname = str(current_user["nickname"])
    elif user_nickname is None:
        user_nickname = ""

    get_db(request).execute(
        """
        INSERT INTO activity_logs
            (
                user_id,
                user_nickname,
                ip_address,
                method,
                path,
                status_code,
                event_type,
                action,
                metadata,
                created_at
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            user_nickname or "",
            ip_address or get_client_ip(request),
            request.method,
            request.url.path,
            status_code,
            event_type,
            action,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    get_db(request).commit()


def flash(request: Request, message: str, category: str = "message") -> None:
    messages = list(request.session.get("_flashes", []))
    messages.append((category, message))
    request.session["_flashes"] = messages


@pass_context
def get_flashed_messages(context, with_categories: bool = False):
    request: Request = context["request"]
    messages = request.session.pop("_flashes", [])
    if with_categories:
        return messages
    return [message for _, message in messages]


@pass_context
def template_url_for(context, endpoint: str, **values: Any) -> str:
    request: Request = context["request"]
    path_values = {key: value for key, value in values.items() if value is not None}

    if endpoint == "static" and "filename" in path_values:
        filename = str(path_values.pop("filename"))
        path_values["path"] = "/" + filename.lstrip("/")

    try:
        url = request.url_for(endpoint, **path_values)
        query_values = {}
    except NoMatchFound:
        url = request.url_for(endpoint)
        query_values = path_values

    return relative_url(url, query_values)


def relative_url(url: Any, query_values: dict[str, Any] | None = None) -> str:
    parts = urlsplit(str(url))
    query = parts.query
    filtered_values = {
        key: value for key, value in (query_values or {}).items() if value is not None
    }
    if filtered_values:
        extra_query = urlencode(filtered_values, doseq=True)
        query = f"{query}&{extra_query}" if query else extra_query
    return urlunsplit(("", "", parts.path, query, parts.fragment))


def url_path_for(request: Request, endpoint: str, **query_values: Any) -> str:
    path = str(request.app.url_path_for(endpoint))
    query = {
        key: value for key, value in query_values.items() if value is not None
    }
    if query:
        return f"{path}?{urlencode(query, doseq=True)}"
    return path


def redirect_to(
    request: Request,
    endpoint: str,
    status_code: int = 302,
    **query_values: Any,
) -> RedirectResponse:
    return RedirectResponse(
        url=url_path_for(request, endpoint, **query_values),
        status_code=status_code,
    )


def register_template_helpers() -> None:
    templates.env.globals["get_flashed_messages"] = get_flashed_messages
    templates.env.globals["url_for"] = template_url_for
    templates.env.filters["datetime_cn"] = datetime_cn
    templates.env.filters["date_cn"] = date_cn
    templates.env.filters["mood_reason_parts"] = mood_reason_parts


def render_template(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "moods": MOODS,
            "grades": GRADES,
            "programs": PROGRAMS,
            "current_user": get_current_user(request),
            "auth_background_url": get_auth_background_url(),
            **(context or {}),
        },
        status_code=status_code,
    )


def get_auth_background_url() -> str:
    if not AUTH_BACKGROUND_DIR.exists():
        return "/static/login-campus.png"

    filenames = sorted(
        path.name
        for path in AUTH_BACKGROUND_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in AUTH_BACKGROUND_EXTENSIONS
    )
    if not filenames:
        return "/static/login-campus.png"

    return f"/static/login-backgrounds/{random.choice(filenames)}"


def datetime_cn(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def date_cn(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except ValueError:
        return value


def mood_reason_parts(value: str | None) -> list[dict[str, str]]:
    if not value:
        return []

    parts = []
    for block in str(value).split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        if len(lines) == 1:
            parts.append({"question": "", "answer": lines[0]})
            continue

        parts.append(
            {
                "question": lines[0],
                "answer": "\n".join(lines[1:]),
            }
        )

    return parts


def register_routes(app: FastAPI) -> None:
    @app.get("/", name="index")
    async def index(request: Request):
        if get_current_user(request) is None:
            return redirect_to(request, "login")
        return redirect_to(request, "profile")

    @app.api_route("/register", methods=["GET", "POST"], name="register")
    async def register(request: Request):
        if get_current_user(request) is not None:
            return redirect_to(request, "profile")

        if request.method == "POST":
            form = await request.form()
            real_name = str(form.get("real_name", "")).strip()
            nickname = str(form.get("nickname", "")).strip()
            grade = str(form.get("grade", "")).strip()
            program = str(form.get("program", "")).strip()
            password = str(form.get("password", ""))
            privacy_consent = form.get("privacy_consent") == "yes"
            ip_address = get_client_ip(request)
            registration_limit = get_registration_ip_limit(request)
            recent_attempts = count_recent_registration_attempts(request, ip_address)
            attempt_result = (
                "rate_limited" if recent_attempts >= registration_limit else "started"
            )
            attempt_id = create_registration_attempt(
                request,
                ip_address,
                nickname,
                attempt_result,
            )

            if recent_attempts >= registration_limit:
                record_activity(
                    request,
                    "operation",
                    "register_rate_limited",
                    status_code=429,
                    metadata={
                        "nickname": nickname,
                        "attempts_last_24h": recent_attempts + 1,
                        "limit": registration_limit,
                    },
                    ip_address=ip_address,
                )
                flash(
                    request,
                    "该 IP 注册过于频繁，请 24 小时后再试或联系管理员。",
                    "error",
                )
                return render_template(request, "register.html", status_code=429)

            if not real_name or not nickname or not password:
                update_registration_attempt(request, attempt_id, "missing_fields")
                record_activity(
                    request,
                    "operation",
                    "register_failed",
                    status_code=400,
                    metadata={"reason": "missing_fields", "nickname": nickname},
                    ip_address=ip_address,
                )
                flash(request, "姓名、昵称和密码都需要填写。", "error")
                return render_template(request, "register.html")

            if not privacy_consent:
                update_registration_attempt(request, attempt_id, "privacy_consent_missing")
                record_activity(
                    request,
                    "operation",
                    "register_failed",
                    status_code=400,
                    metadata={
                        "reason": "privacy_consent_missing",
                        "nickname": nickname,
                    },
                    ip_address=ip_address,
                )
                flash(
                    request,
                    "\u8bf7\u5148\u52fe\u9009\u9690\u79c1\u89c4\u5219\u540c\u610f\u9879\u3002",
                    "error",
                )
                return render_template(request, "register.html")

            if not is_valid_optional_choice(grade, GRADES) or not is_valid_optional_choice(
                program,
                PROGRAMS,
            ):
                update_registration_attempt(request, attempt_id, "invalid_profile")
                record_activity(
                    request,
                    "operation",
                    "register_failed",
                    status_code=400,
                    metadata={"reason": "invalid_profile", "nickname": nickname},
                    ip_address=ip_address,
                )
                flash(request, "请选择有效的年级和项目。", "error")
                return render_template(request, "register.html")

            try:
                db = get_db(request)
                user_count = db.execute(
                    "SELECT COUNT(*) AS count FROM users",
                ).fetchone()["count"]
                configured_admin = (
                    request.app.state.config.get("ADMIN_NICKNAME") or ""
                ).strip()
                is_admin = int(user_count == 0 or nickname == configured_admin)
                created_at = datetime.now().isoformat(timespec="seconds")
                db.execute(
                    """
                    INSERT INTO users
                        (
                            real_name,
                            nickname,
                            grade,
                            program,
                            is_admin,
                            privacy_consent_at,
                            password_hash,
                            created_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        real_name,
                        nickname,
                        grade,
                        program,
                        is_admin,
                        created_at,
                        generate_password_hash(password),
                        created_at,
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError:
                update_registration_attempt(request, attempt_id, "duplicate_nickname")
                record_activity(
                    request,
                    "operation",
                    "register_failed",
                    status_code=409,
                    metadata={"reason": "duplicate_nickname", "nickname": nickname},
                    ip_address=ip_address,
                )
                flash(request, "这个昵称已经被注册，请换一个。", "error")
                return render_template(request, "register.html")

            user = get_db(request).execute(
                "SELECT id, nickname FROM users WHERE nickname = ?",
                (nickname,),
            ).fetchone()
            update_registration_attempt(request, attempt_id, "success", user["id"])
            record_activity(
                request,
                "operation",
                "register_success",
                status_code=302,
                metadata={"nickname": nickname},
                user_id=user["id"],
                user_nickname=user["nickname"],
                ip_address=ip_address,
            )
            request.session.clear()
            request.session["user_id"] = user["id"]
            request.session["nickname"] = user["nickname"]
            flash(request, "注册成功，欢迎开始记录今天的心情。", "success")
            return redirect_to(request, "profile")

        return render_template(request, "register.html")

    @app.api_route("/login", methods=["GET", "POST"], name="login")
    async def login(request: Request):
        if get_current_user(request) is not None:
            return redirect_to(request, "profile")

        if request.method == "POST":
            form = await request.form()
            nickname = str(form.get("nickname", "")).strip()
            password = str(form.get("password", ""))
            user = get_db(request).execute(
                "SELECT * FROM users WHERE nickname = ?",
                (nickname,),
            ).fetchone()

            if user is None or not check_password_hash(user["password_hash"], password):
                record_activity(
                    request,
                    "operation",
                    "login_failed",
                    status_code=401,
                    metadata={"nickname": nickname},
                )
                flash(request, "昵称或密码不正确。", "error")
                return render_template(request, "login.html")

            request.session.clear()
            request.session["user_id"] = user["id"]
            request.session["nickname"] = user["nickname"]
            record_activity(
                request,
                "operation",
                "login_success",
                status_code=302,
                metadata={"nickname": user["nickname"]},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            next_url = request.query_params.get("next")
            if not next_url or not next_url.startswith("/"):
                next_url = url_path_for(request, "profile")
            return RedirectResponse(url=next_url, status_code=302)

        return render_template(request, "login.html")

    @app.post("/logout", name="logout")
    async def logout(request: Request):
        user = get_current_user(request)
        record_activity(
            request,
            "operation",
            "logout",
            status_code=302,
            user_id=user["id"] if user is not None else None,
            user_nickname=user["nickname"] if user is not None else "",
        )
        request.session.clear()
        return redirect_to(request, "login")

    @app.post("/privacy-consent", name="accept_privacy_consent")
    async def accept_privacy_consent(request: Request):
        user = require_user(request)
        if isinstance(user, RedirectResponse):
            return user

        form = await request.form()
        next_url = str(form.get("next", "")).strip()
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = url_path_for(request, "profile")

        if form.get("privacy_consent") != "yes":
            record_activity(
                request,
                "operation",
                "privacy_consent_failed",
                status_code=400,
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(
                request,
                "\u8bf7\u5148\u52fe\u9009\u9690\u79c1\u89c4\u5219\u540c\u610f\u9879\u3002",
                "error",
            )
            return RedirectResponse(url=next_url, status_code=302)

        get_db(request).execute(
            "UPDATE users SET privacy_consent_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), user["id"]),
        )
        get_db(request).commit()
        record_activity(
            request,
            "operation",
            "privacy_consent_accepted",
            status_code=302,
            user_id=user["id"],
            user_nickname=user["nickname"],
        )
        request.state.user = None
        flash(
            request,
            "\u5df2\u8bb0\u5f55\u9690\u79c1\u89c4\u5219\u540c\u610f\u72b6\u6001\u3002",
            "success",
        )
        return RedirectResponse(url=next_url, status_code=302)

    @app.get("/profile", name="profile")
    async def profile(request: Request):
        user = require_user(request)
        if isinstance(user, RedirectResponse):
            return user
        return render_template(
            request,
            "profile.html",
            {
                "active_page": "profile",
                "recent_entries": get_recent_entries(request, user["id"]),
            },
        )

    @app.post("/profile/details", name="update_profile_details")
    async def update_profile_details(request: Request):
        user = require_user(request)
        if isinstance(user, RedirectResponse):
            return user

        form = await request.form()
        grade = str(form.get("grade", "")).strip()
        program = str(form.get("program", "")).strip()

        if not is_valid_optional_choice(grade, GRADES) or not is_valid_optional_choice(
            program,
            PROGRAMS,
        ):
            record_activity(
                request,
                "operation",
                "profile_details_failed",
                status_code=400,
                metadata={"reason": "invalid_profile"},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "请选择有效的年级和项目。", "error")
            return redirect_to(request, "profile")

        get_db(request).execute(
            "UPDATE users SET grade = ?, program = ? WHERE id = ?",
            (grade, program, user["id"]),
        )
        get_db(request).commit()
        record_activity(
            request,
            "operation",
            "profile_details_updated",
            status_code=302,
            metadata={"grade": grade, "program": program},
            user_id=user["id"],
            user_nickname=user["nickname"],
        )
        request.state.user = None
        flash(request, "个人资料已更新。", "success")
        return redirect_to(request, "profile")

    @app.post("/profile/nickname", name="update_nickname")
    async def update_nickname(request: Request):
        user = require_user(request)
        if isinstance(user, RedirectResponse):
            return user

        form = await request.form()
        new_nickname = str(form.get("nickname", "")).strip()
        if not new_nickname:
            record_activity(
                request,
                "operation",
                "nickname_update_failed",
                status_code=400,
                metadata={"reason": "missing_nickname"},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "新昵称不能为空。", "error")
            return redirect_to(request, "profile")

        if new_nickname == user["nickname"]:
            record_activity(
                request,
                "operation",
                "nickname_update_skipped",
                status_code=302,
                metadata={"reason": "unchanged"},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "昵称没有变化。", "success")
            return redirect_to(request, "profile")

        try:
            get_db(request).execute(
                "UPDATE users SET nickname = ? WHERE id = ?",
                (new_nickname, user["id"]),
            )
            get_db(request).commit()
        except sqlite3.IntegrityError:
            record_activity(
                request,
                "operation",
                "nickname_update_failed",
                status_code=409,
                metadata={"reason": "duplicate_nickname"},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "这个昵称已经被使用，请换一个。", "error")
            return redirect_to(request, "profile")

        record_activity(
            request,
            "operation",
            "nickname_updated",
            status_code=302,
            metadata={"old_nickname": user["nickname"], "new_nickname": new_nickname},
            user_id=user["id"],
            user_nickname=new_nickname,
        )
        request.session["nickname"] = new_nickname
        request.state.user = None
        flash(request, "昵称已更新。", "success")
        return redirect_to(request, "profile")

    @app.post("/profile/password", name="update_password")
    async def update_password(request: Request):
        user = require_user(request)
        if isinstance(user, RedirectResponse):
            return user

        form = await request.form()
        current_password = str(form.get("current_password", ""))
        new_password = str(form.get("new_password", ""))
        confirm_password = str(form.get("confirm_password", ""))

        if not current_password or not new_password or not confirm_password:
            record_activity(
                request,
                "operation",
                "password_update_failed",
                status_code=400,
                metadata={"reason": "missing_fields"},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "当前密码、新密码和确认密码都需要填写。", "error")
            return redirect_to(request, "profile")

        if new_password != confirm_password:
            record_activity(
                request,
                "operation",
                "password_update_failed",
                status_code=400,
                metadata={"reason": "password_mismatch"},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "两次输入的新密码不一致。", "error")
            return redirect_to(request, "profile")

        db_user = get_db(request).execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if db_user is None or not check_password_hash(
            db_user["password_hash"], current_password
        ):
            record_activity(
                request,
                "operation",
                "password_update_failed",
                status_code=401,
                metadata={"reason": "bad_current_password"},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "当前密码不正确。", "error")
            return redirect_to(request, "profile")

        get_db(request).execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), user["id"]),
        )
        get_db(request).commit()
        record_activity(
            request,
            "operation",
            "password_updated",
            status_code=302,
            user_id=user["id"],
            user_nickname=user["nickname"],
        )
        flash(request, "密码已更新，请使用新密码登录。", "success")
        return redirect_to(request, "profile")

    @app.api_route("/mood-report", methods=["GET", "POST"], name="mood_report")
    async def mood_report(request: Request):
        user = require_user(request)
        if isinstance(user, RedirectResponse):
            return user

        if request.method == "POST":
            form = await request.form()
            mood_emoji = str(form.get("mood_emoji", "")).strip()
            day_event = str(form.get("day_event", "")).strip()
            body_feeling = str(form.get("body_feeling", "")).strip()
            extra_thoughts = str(form.get("extra_thoughts", "")).strip()
            allowed_emojis = {mood["emoji"] for mood in MOODS}

            if mood_emoji not in allowed_emojis:
                record_activity(
                    request,
                    "operation",
                    "mood_report_failed",
                    status_code=400,
                    metadata={"reason": "invalid_mood"},
                    user_id=user["id"],
                    user_nickname=user["nickname"],
                )
                flash(request, "请选择一个心情 emoji。", "error")
                return render_template(
                    request,
                    "mood_report.html",
                    {
                        "active_page": "mood_report",
                        "recent_entries": get_recent_entries(request, user["id"]),
                    },
                )

            if not day_event or not body_feeling:
                record_activity(
                    request,
                    "operation",
                    "mood_report_failed",
                    status_code=400,
                    metadata={"reason": "missing_answers"},
                    user_id=user["id"],
                    user_nickname=user["nickname"],
                )
                flash(request, "请回答前两个问题。", "error")
                return render_template(
                    request,
                    "mood_report.html",
                    {
                        "active_page": "mood_report",
                        "recent_entries": get_recent_entries(request, user["id"]),
                    },
                )

            reason = build_mood_reason(day_event, body_feeling, extra_thoughts)
            now = datetime.now()
            get_db(request).execute(
                """
                INSERT INTO mood_entries
                    (user_id, mood_emoji, reason, entry_date, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    mood_emoji,
                    reason,
                    date.today().isoformat(),
                    now.isoformat(timespec="seconds"),
                ),
            )
            get_db(request).commit()
            record_activity(
                request,
                "operation",
                "mood_report_created",
                status_code=302,
                metadata={
                    "entry_date": date.today().isoformat(),
                    "mood_emoji": mood_emoji,
                },
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "今天的心情已经记录。", "success")
            return redirect_to(request, "mood_calendar")

        return render_template(
            request,
            "mood_report.html",
            {
                "active_page": "mood_report",
                "recent_entries": get_recent_entries(request, user["id"]),
            },
        )

    @app.get("/mood-calendar", name="mood_calendar")
    async def mood_calendar(request: Request):
        user = require_user(request)
        if isinstance(user, RedirectResponse):
            return user

        selected_month = parse_month(request.query_params.get("month"))
        rows, month_entries = build_calendar(request, user["id"], selected_month)
        prev_month, next_month = adjacent_months(selected_month)
        return render_template(
            request,
            "mood_calendar.html",
            {
                "active_page": "mood_calendar",
                "selected_month": selected_month,
                "prev_month": prev_month,
                "next_month": next_month,
                "calendar_rows": rows,
                "month_entries": month_entries,
                "recent_entries": get_recent_entries(request, user["id"]),
            },
        )

    @app.get("/mood-history", name="mood_history")
    async def mood_history(request: Request):
        user = require_user(request)
        if isinstance(user, RedirectResponse):
            return user

        return render_template(
            request,
            "mood_history.html",
            {
                "active_page": "mood_history",
                "entries": get_user_entries(request, user["id"]),
                "recent_entries": get_recent_entries(request, user["id"]),
            },
        )

    @app.get("/admin", name="admin_dashboard")
    async def admin_dashboard(request: Request):
        user = require_admin(request)
        if isinstance(user, RedirectResponse):
            return user

        search_query = request.query_params.get("q", "").strip()
        users = get_admin_users(request, search_query)
        return render_template(
            request,
            "admin.html",
            {
                "active_page": "admin_dashboard",
                "users": users,
                "search_query": search_query,
                "recent_entries": [],
            },
        )

    @app.get("/admin/users/{user_id}", name="admin_user_detail")
    async def admin_user_detail(request: Request, user_id: int):
        user = require_admin(request)
        if isinstance(user, RedirectResponse):
            return user

        target_user, entries = get_admin_user_detail(request, user_id)
        if target_user is None:
            flash(request, "没有找到这个用户。", "error")
            return redirect_to(request, "admin_dashboard")

        return render_template(
            request,
            "admin_user.html",
            {
                "active_page": "admin_dashboard",
                "target_user": target_user,
                "entries": entries,
                "recent_entries": [],
            },
        )

    @app.api_route("/admin/settings", methods=["GET", "POST"], name="admin_settings")
    async def admin_settings(request: Request):
        user = require_admin(request)
        if isinstance(user, RedirectResponse):
            return user

        if request.method == "POST":
            form = await request.form()
            raw_limit = str(form.get("registration_ip_limit_per_24h", "")).strip()
            try:
                registration_limit = int(raw_limit)
            except ValueError:
                registration_limit = 0

            if (
                registration_limit < 1
                or registration_limit > REGISTRATION_IP_LIMIT_MAX
            ):
                record_activity(
                    request,
                    "operation",
                    "admin_settings_update_failed",
                    status_code=400,
                    metadata={"reason": "invalid_registration_limit"},
                    user_id=user["id"],
                    user_nickname=user["nickname"],
                )
                flash(request, "注册限制需要填写 1 到 100 之间的整数。", "error")
                return redirect_to(request, "admin_settings")

            set_app_setting(
                request,
                REGISTRATION_IP_LIMIT_SETTING,
                str(registration_limit),
            )
            get_db(request).commit()
            record_activity(
                request,
                "operation",
                "admin_settings_updated",
                status_code=302,
                metadata={REGISTRATION_IP_LIMIT_SETTING: registration_limit},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "管理员设置已更新。", "success")
            return redirect_to(request, "admin_settings")

        return render_template(
            request,
            "admin_settings.html",
            {
                "active_page": "admin_settings",
                "registration_ip_limit": get_registration_ip_limit(request),
                "registration_ip_limit_max": REGISTRATION_IP_LIMIT_MAX,
                "recent_entries": [],
            },
        )

    @app.get("/admin/activity", name="admin_activity")
    async def admin_activity(request: Request):
        user = require_admin(request)
        if isinstance(user, RedirectResponse):
            return user

        filters = {
            "user_id": request.query_params.get("user_id", "").strip(),
            "ip": request.query_params.get("ip", "").strip(),
            "q": request.query_params.get("q", "").strip(),
        }
        return render_template(
            request,
            "admin_activity.html",
            {
                "active_page": "admin_activity",
                "activity_logs": get_activity_logs(
                    request,
                    filters,
                    limit=None if filters["user_id"] else 200,
                ),
                "activity_user_stats": get_activity_user_stats(request),
                "filters": filters,
                "recent_entries": [],
            },
        )

    @app.get("/admin/activity/user", name="admin_activity_user_detail")
    async def admin_activity_user_detail(request: Request):
        user = require_admin(request)
        if isinstance(user, RedirectResponse):
            return user

        user_key = request.query_params.get("user_id", "").strip()
        if not user_key:
            flash(request, "请选择要查看动态的用户。", "error")
            return redirect_to(request, "admin_activity")

        activity_logs = get_activity_logs(
            request,
            {"user_id": user_key, "ip": "", "q": ""},
            limit=None,
        )
        return render_template(
            request,
            "admin_activity_user.html",
            {
                "active_page": "admin_activity",
                "activity_target": get_activity_user_target(request, user_key),
                "activity_logs": activity_logs,
                "recent_entries": [],
            },
        )

    @app.post("/admin/users/delete", name="delete_users")
    async def delete_users(request: Request):
        user = require_admin(request)
        if isinstance(user, RedirectResponse):
            return user

        form = await request.form()
        selected_ids: list[int] = []
        for raw_user_id in form.getlist("user_ids"):
            try:
                selected_id = int(str(raw_user_id))
            except ValueError:
                continue
            if selected_id not in selected_ids:
                selected_ids.append(selected_id)

        if not selected_ids:
            record_activity(
                request,
                "operation",
                "admin_delete_users_failed",
                status_code=400,
                metadata={"reason": "no_selection"},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "请先选择要删除的用户。", "error")
            return redirect_to(request, "admin_dashboard")

        deletable_ids = [
            selected_id for selected_id in selected_ids if selected_id != user["id"]
        ]
        skipped_self = len(deletable_ids) != len(selected_ids)
        if not deletable_ids:
            record_activity(
                request,
                "operation",
                "admin_delete_users_failed",
                status_code=400,
                metadata={"reason": "self_only", "selected_user_ids": selected_ids},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "不能删除当前登录的管理员账号。", "error")
            return redirect_to(request, "admin_dashboard")

        placeholders = sql_placeholders(deletable_ids)
        target_users = get_db(request).execute(
            f"""
            SELECT id, nickname
            FROM users
            WHERE id IN ({placeholders})
            """,
            tuple(deletable_ids),
        ).fetchall()
        if not target_users:
            record_activity(
                request,
                "operation",
                "admin_delete_users_failed",
                status_code=404,
                metadata={"reason": "no_matching_users", "selected_user_ids": selected_ids},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "没有找到可删除的用户。", "error")
            return redirect_to(request, "admin_dashboard")

        target_ids = [int(target_user["id"]) for target_user in target_users]
        target_nicknames = [str(target_user["nickname"]) for target_user in target_users]
        target_placeholders = sql_placeholders(target_ids)
        target_params = tuple(target_ids)
        db = get_db(request)
        db.execute(
            f"DELETE FROM mood_entries WHERE user_id IN ({target_placeholders})",
            target_params,
        )
        db.execute(
            f"""
            UPDATE registration_attempts
            SET user_id = NULL
            WHERE user_id IN ({target_placeholders})
            """,
            target_params,
        )
        db.execute(
            f"""
            UPDATE activity_logs
            SET user_id = NULL
            WHERE user_id IN ({target_placeholders})
            """,
            target_params,
        )
        db.execute(
            f"DELETE FROM users WHERE id IN ({target_placeholders})",
            target_params,
        )
        db.commit()
        record_activity(
            request,
            "operation",
            "admin_deleted_users",
            status_code=302,
            metadata={
                "deleted_count": len(target_ids),
                "target_user_ids": target_ids,
                "target_nicknames": target_nicknames,
                "skipped_self": skipped_self,
            },
            user_id=user["id"],
            user_nickname=user["nickname"],
        )

        message = f"已删除 {len(target_ids)} 个用户。"
        if skipped_self:
            message += " 当前登录账号已自动跳过。"
        flash(request, message, "success")
        return redirect_to(request, "admin_dashboard")

    @app.post("/admin/users/{user_id}/admin", name="promote_admin")
    async def promote_admin(request: Request, user_id: int):
        user = require_admin(request)
        if isinstance(user, RedirectResponse):
            return user

        target_user = get_db(request).execute(
            "SELECT id, nickname, is_admin FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if target_user is None:
            flash(request, "没有找到这个用户。", "error")
            return redirect_to(request, "admin_dashboard")

        if target_user["is_admin"]:
            record_activity(
                request,
                "operation",
                "admin_promote_skipped",
                status_code=302,
                metadata={"target_user_id": user_id, "reason": "already_admin"},
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, "这个用户已经是管理员。", "success")
        else:
            get_db(request).execute(
                "UPDATE users SET is_admin = 1 WHERE id = ?",
                (user_id,),
            )
            get_db(request).commit()
            record_activity(
                request,
                "operation",
                "admin_promoted_user",
                status_code=302,
                metadata={
                    "target_user_id": user_id,
                    "target_nickname": target_user["nickname"],
                },
                user_id=user["id"],
                user_nickname=user["nickname"],
            )
            flash(request, f"已将 @{target_user['nickname']} 设置为管理员。", "success")

        return RedirectResponse(url=f"/admin/users/{user_id}", status_code=302)


def is_valid_optional_choice(value: str, choices: tuple[str, ...]) -> bool:
    return value == "" or value in choices


def sql_placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values)


def build_mood_reason(day_event: str, body_feeling: str, extra_thoughts: str) -> str:
    answers = [
        ("今天做了什么，什么影响了你的心情？", day_event),
        ("今天身体感觉怎么样？", body_feeling),
    ]
    if extra_thoughts:
        answers.append(("还有什么想说的？", extra_thoughts))

    return "\n\n".join(f"{question}\n{answer}" for question, answer in answers)


def get_recent_entries(
    request: Request,
    user_id: int,
    limit: int = 3,
) -> list[sqlite3.Row]:
    return get_db(request).execute(
        """
        SELECT id, mood_emoji, reason, entry_date, created_at
        FROM mood_entries
        WHERE user_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()


def get_user_entries(request: Request, user_id: int) -> list[sqlite3.Row]:
    return get_db(request).execute(
        """
        SELECT id, mood_emoji, reason, entry_date, created_at
        FROM mood_entries
        WHERE user_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (user_id,),
    ).fetchall()


def get_admin_users(request: Request, search_query: str = "") -> list[sqlite3.Row]:
    sql = """
    SELECT
        users.id,
        users.real_name,
        users.nickname,
        users.grade,
        users.program,
        users.is_admin,
        users.created_at,
        COUNT(mood_entries.id) AS entry_count,
        MAX(mood_entries.created_at) AS latest_entry_at
    FROM users
    LEFT JOIN mood_entries ON mood_entries.user_id = users.id
    """
    params: tuple[str, ...] = ()
    if search_query:
        sql += """
        WHERE users.real_name LIKE ?
           OR users.nickname LIKE ?
           OR users.grade LIKE ?
           OR users.program LIKE ?
           OR CAST(users.id AS TEXT) LIKE ?
        """
        like_query = f"%{search_query}%"
        nickname_query = f"%{search_query.removeprefix('@')}%"
        params = (like_query, nickname_query, like_query, like_query, like_query)

    sql += """
    GROUP BY users.id
    ORDER BY users.created_at DESC, users.id DESC
    """
    return get_db(request).execute(sql, params).fetchall()


def get_admin_user_detail(
    request: Request,
    user_id: int,
) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    db = get_db(request)
    user = db.execute(
        """
        SELECT
            users.id,
            users.real_name,
            users.nickname,
            users.grade,
            users.program,
            users.is_admin,
            users.created_at,
            COUNT(mood_entries.id) AS entry_count,
            MAX(mood_entries.created_at) AS latest_entry_at
        FROM users
        LEFT JOIN mood_entries ON mood_entries.user_id = users.id
        WHERE users.id = ?
        GROUP BY users.id
        """,
        (user_id,),
    ).fetchone()
    if user is None:
        return None, []

    entries = db.execute(
        """
        SELECT id, user_id, mood_emoji, reason, entry_date, created_at
        FROM mood_entries
        WHERE user_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (user_id,),
    ).fetchall()

    return user, entries


def get_activity_user_target(request: Request, user_key: str) -> dict[str, Any]:
    normalized_key = user_key.strip()
    if normalized_key.lower() == "anonymous":
        return {
            "label": "匿名访问",
            "subtitle": "未登录或未绑定用户的访问动态",
            "profile_user_id": None,
        }

    db = get_db(request)
    if normalized_key.startswith("@"):
        nickname = normalized_key.removeprefix("@").strip()
        user = db.execute(
            "SELECT id, real_name, nickname FROM users WHERE nickname = ?",
            (nickname,),
        ).fetchone()
        if user is not None:
            return {
                "label": str(user["real_name"]),
                "subtitle": f"@{user['nickname']}",
                "profile_user_id": int(user["id"]),
            }
        return {
            "label": f"@{nickname}" if nickname else "历史昵称",
            "subtitle": "按日志中保留的历史昵称匹配",
            "profile_user_id": None,
        }

    try:
        user_id = int(normalized_key)
    except ValueError:
        return {
            "label": normalized_key,
            "subtitle": "按用户关键词匹配",
            "profile_user_id": None,
        }

    user = db.execute(
        "SELECT id, real_name, nickname FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if user is None:
        return {
            "label": f"用户 #{user_id}",
            "subtitle": "没有找到这个用户的资料，但仍会显示匹配到的动态",
            "profile_user_id": None,
        }

    return {
        "label": str(user["real_name"]),
        "subtitle": f"@{user['nickname']}",
        "profile_user_id": int(user["id"]),
    }


def get_activity_logs(
    request: Request,
    filters: dict[str, str] | None = None,
    limit: int | None = 200,
) -> list[sqlite3.Row]:
    filters = filters or {}
    sql = """
    SELECT
        activity_logs.id,
        activity_logs.user_id,
        activity_logs.user_nickname,
        activity_logs.ip_address,
        activity_logs.method,
        activity_logs.path,
        activity_logs.status_code,
        activity_logs.event_type,
        activity_logs.action,
        activity_logs.metadata,
        activity_logs.created_at,
        users.real_name,
        users.nickname
    FROM activity_logs
    LEFT JOIN users ON users.id = activity_logs.user_id
    """
    clauses: list[str] = []
    params: list[Any] = []

    user_id = filters.get("user_id", "").strip()
    if user_id:
        if user_id.lower() == "anonymous":
            clauses.append("activity_logs.user_id IS NULL")
        else:
            try:
                selected_user_id = int(user_id)
            except ValueError:
                like_query = f"%{user_id}%"
                nickname_query = f"%{user_id.removeprefix('@')}%"
                clauses.append(
                    """
                    (
                        users.real_name LIKE ?
                        OR users.nickname LIKE ?
                        OR activity_logs.user_nickname LIKE ?
                    )
                    """
                )
                params.extend([like_query, nickname_query, nickname_query])
            else:
                clauses.append("activity_logs.user_id = ?")
                params.append(selected_user_id)

    ip_filter = filters.get("ip", "").strip()
    if ip_filter:
        clauses.append("activity_logs.ip_address LIKE ?")
        params.append(f"%{ip_filter}%")

    search_query = filters.get("q", "").strip()
    if search_query:
        like_query = f"%{search_query}%"
        nickname_query = f"%{search_query.removeprefix('@')}%"
        clauses.append(
            """
            (
                activity_logs.action LIKE ?
                OR activity_logs.path LIKE ?
                OR activity_logs.method LIKE ?
                OR activity_logs.event_type LIKE ?
                OR activity_logs.ip_address LIKE ?
                OR activity_logs.user_nickname LIKE ?
                OR users.real_name LIKE ?
                OR users.nickname LIKE ?
                OR activity_logs.metadata LIKE ?
            )
            """
        )
        params.extend(
            [
                like_query,
                like_query,
                like_query,
                like_query,
                like_query,
                nickname_query,
                like_query,
                nickname_query,
                like_query,
            ]
        )

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    sql += """
    ORDER BY activity_logs.created_at DESC, activity_logs.id DESC
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return get_db(request).execute(sql, tuple(params)).fetchall()


def get_recent_activity_logs(
    request: Request,
    *,
    user_id: int,
    limit: int = 8,
) -> list[sqlite3.Row]:
    return get_activity_logs(request, {"user_id": str(user_id)}, limit=limit)


def get_activity_user_stats(
    request: Request,
    limit: int = 50,
) -> list[sqlite3.Row]:
    return get_db(request).execute(
        """
        SELECT
            activity_logs.user_id,
            COALESCE(NULLIF(users.real_name, ''), '匿名访问') AS real_name,
            COALESCE(
                NULLIF(users.nickname, ''),
                NULLIF(activity_logs.user_nickname, ''),
                'anonymous'
            ) AS nickname,
            COUNT(*) AS activity_count,
            COUNT(DISTINCT activity_logs.ip_address) AS ip_count,
            MAX(activity_logs.created_at) AS latest_activity_at
        FROM activity_logs
        LEFT JOIN users ON users.id = activity_logs.user_id
        GROUP BY activity_logs.user_id
        ORDER BY latest_activity_at DESC, activity_count DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def parse_month(month_value: str | None) -> date:
    today = date.today()
    if not month_value:
        return today.replace(day=1)
    try:
        return datetime.strptime(month_value, "%Y-%m").date().replace(day=1)
    except ValueError:
        return today.replace(day=1)


def adjacent_months(selected_month: date) -> tuple[str, str]:
    year = selected_month.year
    month = selected_month.month
    if month == 1:
        prev_date = date(year - 1, 12, 1)
    else:
        prev_date = date(year, month - 1, 1)

    if month == 12:
        next_date = date(year + 1, 1, 1)
    else:
        next_date = date(year, month + 1, 1)

    return prev_date.strftime("%Y-%m"), next_date.strftime("%Y-%m")


def build_calendar(request: Request, user_id: int, selected_month: date):
    first_day = selected_month
    _, days_in_month = calendar.monthrange(first_day.year, first_day.month)
    last_day = date(first_day.year, first_day.month, days_in_month)

    entries = get_db(request).execute(
        """
        SELECT id, mood_emoji, reason, entry_date, created_at
        FROM mood_entries
        WHERE user_id = ? AND entry_date BETWEEN ? AND ?
        ORDER BY entry_date ASC, id ASC
        """,
        (user_id, first_day.isoformat(), last_day.isoformat()),
    ).fetchall()

    latest_by_day: dict[str, sqlite3.Row] = {}
    for entry in entries:
        latest_by_day[entry["entry_date"]] = entry

    month_entries = list(latest_by_day.values())
    month_days = calendar.Calendar(firstweekday=0).monthdatescalendar(
        first_day.year,
        first_day.month,
    )
    today = date.today()

    rows = []
    for week in month_days:
        rows.append(
            [
                {
                    "date": day,
                    "in_month": day.month == first_day.month,
                    "is_today": day == today,
                    "entry": latest_by_day.get(day.isoformat()),
                }
                for day in week
            ]
        )

    return rows, month_entries


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("MOOD_HOST", "127.0.0.1")
    port = int(os.environ.get("MOOD_PORT", "5000"))
    reload = os.environ.get("FASTAPI_RELOAD") == "1"
    if reload:
        uvicorn.run("main:app", host=host, port=port, reload=True)
    else:
        uvicorn.run(app, host=host, port=port)
