from __future__ import annotations

import calendar
import os
import sqlite3
from datetime import date, datetime
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

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
_USER_NOT_LOADED = object()


def create_app(test_config: dict[str, Any] | None = None) -> FastAPI:
    app = FastAPI()
    config = {
        "SECRET_KEY": os.environ.get("SECRET_KEY", "dev-secret-key-change-me"),
        "DATABASE": os.environ.get("MOOD_DB_PATH", str(DEFAULT_DATABASE)),
        "ADMIN_NICKNAME": os.environ.get("ADMIN_NICKNAME", "").strip(),
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
        try:
            return await call_next(request)
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
            """
        )
        ensure_user_profile_columns(db)
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
        SELECT id, real_name, nickname, grade, program, is_admin, created_at
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
            **(context or {}),
        },
        status_code=status_code,
    )


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

            if not real_name or not nickname or not password:
                flash(request, "姓名、昵称和密码都需要填写。", "error")
                return render_template(request, "register.html")

            if not is_valid_optional_choice(grade, GRADES) or not is_valid_optional_choice(
                program,
                PROGRAMS,
            ):
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
                db.execute(
                    """
                    INSERT INTO users
                        (
                            real_name,
                            nickname,
                            grade,
                            program,
                            is_admin,
                            password_hash,
                            created_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        real_name,
                        nickname,
                        grade,
                        program,
                        is_admin,
                        generate_password_hash(password),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError:
                flash(request, "这个昵称已经被注册，请换一个。", "error")
                return render_template(request, "register.html")

            user = get_db(request).execute(
                "SELECT id, nickname FROM users WHERE nickname = ?",
                (nickname,),
            ).fetchone()
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
                flash(request, "昵称或密码不正确。", "error")
                return render_template(request, "login.html")

            request.session.clear()
            request.session["user_id"] = user["id"]
            request.session["nickname"] = user["nickname"]
            next_url = request.query_params.get("next")
            if not next_url or not next_url.startswith("/"):
                next_url = url_path_for(request, "profile")
            return RedirectResponse(url=next_url, status_code=302)

        return render_template(request, "login.html")

    @app.post("/logout", name="logout")
    async def logout(request: Request):
        request.session.clear()
        return redirect_to(request, "login")

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
            flash(request, "请选择有效的年级和项目。", "error")
            return redirect_to(request, "profile")

        get_db(request).execute(
            "UPDATE users SET grade = ?, program = ? WHERE id = ?",
            (grade, program, user["id"]),
        )
        get_db(request).commit()
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
            flash(request, "新昵称不能为空。", "error")
            return redirect_to(request, "profile")

        if new_nickname == user["nickname"]:
            flash(request, "昵称没有变化。", "success")
            return redirect_to(request, "profile")

        try:
            get_db(request).execute(
                "UPDATE users SET nickname = ? WHERE id = ?",
                (new_nickname, user["id"]),
            )
            get_db(request).commit()
        except sqlite3.IntegrityError:
            flash(request, "这个昵称已经被使用，请换一个。", "error")
            return redirect_to(request, "profile")

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
            flash(request, "当前密码、新密码和确认密码都需要填写。", "error")
            return redirect_to(request, "profile")

        if new_password != confirm_password:
            flash(request, "两次输入的新密码不一致。", "error")
            return redirect_to(request, "profile")

        db_user = get_db(request).execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if db_user is None or not check_password_hash(
            db_user["password_hash"], current_password
        ):
            flash(request, "当前密码不正确。", "error")
            return redirect_to(request, "profile")

        get_db(request).execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), user["id"]),
        )
        get_db(request).commit()
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


def is_valid_optional_choice(value: str, choices: tuple[str, ...]) -> bool:
    return value == "" or value in choices


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
        """
        like_query = f"%{search_query}%"
        params = (like_query, like_query, like_query, like_query)

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
    port = int(os.environ.get("PORT", "5000"))
    reload = os.environ.get("FASTAPI_RELOAD") == "1"
    if reload:
        uvicorn.run("main:app", host="127.0.0.1", port=port, reload=True)
    else:
        uvicorn.run(app, host="127.0.0.1", port=port)
