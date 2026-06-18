from __future__ import annotations

import calendar
import os
import sqlite3
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR / "mood_barometer.sqlite3"

MOODS = [
    {"emoji": "😄", "label": "开心"},
    {"emoji": "🙂", "label": "平静"},
    {"emoji": "😌", "label": "放松"},
    {"emoji": "😟", "label": "担心"},
    {"emoji": "😢", "label": "难过"},
    {"emoji": "😡", "label": "生气"},
]


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key-change-me"),
        DATABASE=os.environ.get("MOOD_DB_PATH", str(DEFAULT_DATABASE)),
    )

    if test_config:
        app.config.update(test_config)

    app.teardown_appcontext(close_db)
    app.before_request(load_logged_in_user)
    register_template_helpers(app)
    register_routes(app)

    with app.app_context():
        init_db()

    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        database_path = Path(current_app_config("DATABASE"))
        database_path.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(database_path)
        g.db.row_factory = sqlite3.Row
    return g.db


def current_app_config(key: str) -> str:
    from flask import current_app

    return current_app.config[key]


def close_db(error: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            real_name TEXT NOT NULL,
            nickname TEXT NOT NULL UNIQUE,
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
    db.commit()


def load_logged_in_user() -> None:
    user_id = session.get("user_id")
    g.user = None
    if user_id is None:
        return

    g.user = get_db().execute(
        "SELECT id, real_name, nickname, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(**kwargs)

    return wrapped_view


def register_template_helpers(app: Flask) -> None:
    @app.context_processor
    def inject_template_state() -> dict:
        return {"moods": MOODS, "current_user": g.get("user")}

    @app.template_filter("datetime_cn")
    def datetime_cn(value: str | None) -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value

    @app.template_filter("date_cn")
    def date_cn(value: str | None) -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d")
        except ValueError:
            return value


def register_routes(app: Flask) -> None:
    @app.route("/")
    def index():
        if g.user is None:
            return redirect(url_for("login"))
        return redirect(url_for("profile"))

    @app.route("/register", methods=("GET", "POST"))
    def register():
        if g.user is not None:
            return redirect(url_for("profile"))

        if request.method == "POST":
            real_name = request.form.get("real_name", "").strip()
            nickname = request.form.get("nickname", "").strip()
            password = request.form.get("password", "")

            if not real_name or not nickname or not password:
                flash("姓名、昵称和密码都需要填写。", "error")
                return render_template("register.html")

            try:
                db = get_db()
                db.execute(
                    """
                    INSERT INTO users (real_name, nickname, password_hash, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        real_name,
                        nickname,
                        generate_password_hash(password),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError:
                flash("这个昵称已经被注册，请换一个。", "error")
                return render_template("register.html")

            user = get_db().execute(
                "SELECT id, nickname FROM users WHERE nickname = ?",
                (nickname,),
            ).fetchone()
            session.clear()
            session["user_id"] = user["id"]
            session["nickname"] = user["nickname"]
            flash("注册成功，欢迎开始记录今天的心情。", "success")
            return redirect(url_for("profile"))

        return render_template("register.html")

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if g.user is not None:
            return redirect(url_for("profile"))

        if request.method == "POST":
            nickname = request.form.get("nickname", "").strip()
            password = request.form.get("password", "")
            user = get_db().execute(
                "SELECT * FROM users WHERE nickname = ?",
                (nickname,),
            ).fetchone()

            if user is None or not check_password_hash(user["password_hash"], password):
                flash("昵称或密码不正确。", "error")
                return render_template("login.html")

            session.clear()
            session["user_id"] = user["id"]
            session["nickname"] = user["nickname"]
            next_url = request.args.get("next")
            if not next_url or not next_url.startswith("/"):
                next_url = url_for("profile")
            return redirect(next_url)

        return render_template("login.html")

    @app.route("/logout", methods=("POST",))
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/profile")
    @login_required
    def profile():
        return render_template(
            "profile.html",
            active_page="profile",
            recent_entries=get_recent_entries(g.user["id"]),
        )

    @app.route("/mood-report", methods=("GET", "POST"))
    @login_required
    def mood_report():
        if request.method == "POST":
            mood_emoji = request.form.get("mood_emoji", "").strip()
            reason = request.form.get("reason", "").strip()
            allowed_emojis = {mood["emoji"] for mood in MOODS}

            if mood_emoji not in allowed_emojis:
                flash("请选择一个心情 emoji。", "error")
                return render_template(
                    "mood_report.html",
                    active_page="mood_report",
                    recent_entries=get_recent_entries(g.user["id"]),
                )

            if not reason:
                flash("请写下造成这个心情的原因。", "error")
                return render_template(
                    "mood_report.html",
                    active_page="mood_report",
                    recent_entries=get_recent_entries(g.user["id"]),
                )

            now = datetime.now()
            get_db().execute(
                """
                INSERT INTO mood_entries
                    (user_id, mood_emoji, reason, entry_date, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    g.user["id"],
                    mood_emoji,
                    reason,
                    date.today().isoformat(),
                    now.isoformat(timespec="seconds"),
                ),
            )
            get_db().commit()
            flash("今天的心情已经记录。", "success")
            return redirect(url_for("mood_calendar"))

        return render_template(
            "mood_report.html",
            active_page="mood_report",
            recent_entries=get_recent_entries(g.user["id"]),
        )

    @app.route("/mood-calendar")
    @login_required
    def mood_calendar():
        selected_month = parse_month(request.args.get("month"))
        rows, month_entries = build_calendar(g.user["id"], selected_month)
        prev_month, next_month = adjacent_months(selected_month)
        return render_template(
            "mood_calendar.html",
            active_page="mood_calendar",
            selected_month=selected_month,
            prev_month=prev_month,
            next_month=next_month,
            calendar_rows=rows,
            month_entries=month_entries,
            recent_entries=get_recent_entries(g.user["id"]),
        )


def get_recent_entries(user_id: int, limit: int = 5) -> list[sqlite3.Row]:
    return get_db().execute(
        """
        SELECT mood_emoji, reason, entry_date, created_at
        FROM mood_entries
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
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


def build_calendar(user_id: int, selected_month: date):
    first_day = selected_month
    _, days_in_month = calendar.monthrange(first_day.year, first_day.month)
    last_day = date(first_day.year, first_day.month, days_in_month)

    entries = get_db().execute(
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
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
