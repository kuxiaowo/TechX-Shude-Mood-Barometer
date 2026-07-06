import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import MOODS, create_app, get_client_ip


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "mood_test.sqlite3"),
            "SECRET_KEY": "test-secret",
        }
    )


@pytest.fixture()
def client(app):
    return TestClient(app, follow_redirects=False)


def register(
    client,
    nickname="sunny",
    password="pw",
    grade="2024",
    program="AP",
    real_name="张三",
):
    return client.post(
        "/register",
        data={
            "real_name": real_name,
            "nickname": nickname,
            "grade": grade,
            "program": program,
            "password": password,
            "privacy_consent": "yes",
        },
        follow_redirects=True,
    )


def login(client, nickname="sunny", password="pw"):
    return client.post(
        "/login",
        data={"nickname": nickname, "password": password},
        follow_redirects=True,
    )


def rows(app, sql, params=()):
    with sqlite3.connect(app.state.config["DATABASE"]) as db:
        db.row_factory = sqlite3.Row
        return db.execute(sql, params).fetchall()


def execute(app, sql, params=()):
    with sqlite3.connect(app.state.config["DATABASE"]) as db:
        db.execute(sql, params)
        db.commit()


def calendar_grid(html):
    start = html.index('<section class="calendar-grid"')
    end = html.index("</section>", start)
    return html[start:end]


def admin_user_panel(html):
    start = html.index('<section class="admin-user-panel"')
    end = html.index("</section>", start)
    return html[start:end]


def test_register_success_and_password_hash(app, client):
    response = register(client)

    assert response.status_code == 200
    assert "你好，张三" in response.text
    assert "2024" in response.text
    assert "AP" in response.text

    users = rows(
        app,
        "SELECT nickname, grade, program, privacy_consent_at, password_hash FROM users",
    )
    assert users[0]["nickname"] == "sunny"
    assert users[0]["grade"] == "2024"
    assert users[0]["program"] == "AP"
    assert users[0]["privacy_consent_at"]
    assert users[0]["password_hash"] != "pw"


def test_grade_options_display_class_suffix(client):
    register_page = client.get("/register")
    assert 'value="2024">2024\u7ea7</option>' in register_page.text

    register(client)
    profile_page = client.get("/profile")
    assert "2024\u7ea7" in profile_page.text


def test_auth_pages_use_background_folder(client):
    login_page = client.get("/login")
    register_page = client.get("/register")

    assert "/static/login-backgrounds/auth-bg-" in login_page.text
    assert "/static/login-backgrounds/auth-bg-" in register_page.text
    assert "--auth-background-image" in login_page.text
    assert "--auth-background-image" in register_page.text


def test_register_privacy_consent_uses_modal_without_expanding_form(client):
    response = client.get("/register")
    form_start = response.text.index('<form')
    form_end = response.text.index("</form>", form_start)
    form_html = response.text[form_start:form_end]

    assert 'data-consent-dialog="register-privacy-consent-dialog"' in form_html
    assert 'input name="privacy_consent" type="hidden"' in form_html
    assert "consent-notice" not in form_html
    assert 'type="checkbox"' not in form_html
    assert 'id="register-privacy-consent-dialog"' in response.text


def test_register_rejects_duplicate_and_empty_fields(client):
    register(client)
    client.post("/logout")

    duplicate = register(client)
    assert "这个昵称已经被注册" in duplicate.text

    empty = client.post(
        "/register",
        data={
            "real_name": "",
            "nickname": "",
            "grade": "",
            "program": "",
            "password": "",
        },
        follow_redirects=True,
    )
    assert "都需要填写" in empty.text


def test_register_requires_privacy_consent(app, client):
    response = client.post(
        "/register",
        data={
            "real_name": "student",
            "nickname": "student",
            "grade": "2024",
            "program": "AP",
            "password": "pw",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert 'name="privacy_consent"' in response.text
    assert rows(app, "SELECT COUNT(*) AS count FROM users")[0]["count"] == 0


def test_register_rejects_invalid_grade_and_program(client):
    response = register(client, grade="2023", program="A-Level")

    assert "请选择有效的年级和项目" in response.text


def test_register_allows_empty_grade_and_program(app, client):
    response = register(client, grade="", program="")

    assert response.status_code == 200
    users = rows(app, "SELECT grade, program FROM users")
    assert users[0]["grade"] == ""
    assert users[0]["program"] == ""


def test_register_rate_limit_counts_every_post_by_ip(app, client):
    for index in range(5):
        response = client.post(
            "/register",
            data={
                "real_name": "",
                "nickname": f"limited-{index}",
                "grade": "",
                "program": "",
                "password": "",
            },
        )
        assert response.status_code == 200

    limited = client.post(
        "/register",
        data={
            "real_name": "",
            "nickname": "limited-5",
            "grade": "",
            "program": "",
            "password": "",
        },
    )

    assert limited.status_code == 429
    assert rows(app, "SELECT COUNT(*) AS count FROM users")[0]["count"] == 0
    attempts = rows(
        app,
        "SELECT result FROM registration_attempts ORDER BY id",
    )
    assert len(attempts) == 6
    assert attempts[-1]["result"] == "rate_limited"


def test_admin_can_adjust_registration_rate_limit(app, client):
    register(client, nickname="admin-user", real_name="管理员")

    response = client.post(
        "/admin/settings",
        data={"registration_ip_limit_per_24h": "2"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert 'value="2"' in response.text
    assert rows(
        app,
        "SELECT value FROM app_settings WHERE key = ?",
        ("registration_ip_limit_per_24h",),
    )[0]["value"] == "2"

    invalid = client.post(
        "/admin/settings",
        data={"registration_ip_limit_per_24h": "0"},
        follow_redirects=True,
    )
    assert invalid.status_code == 200
    assert rows(
        app,
        "SELECT value FROM app_settings WHERE key = ?",
        ("registration_ip_limit_per_24h",),
    )[0]["value"] == "2"

    client.post("/logout")
    execute(app, "DELETE FROM registration_attempts")
    for index in range(2):
        response = client.post(
            "/register",
            data={
                "real_name": "",
                "nickname": f"limited-{index}",
                "grade": "",
                "program": "",
                "password": "",
            },
        )
        assert response.status_code == 200

    limited = client.post(
        "/register",
        data={
            "real_name": "",
            "nickname": "limited-2",
            "grade": "",
            "program": "",
            "password": "",
        },
    )
    assert limited.status_code == 429


def test_forwarded_for_is_trusted_only_from_local_proxy():
    class FakeClient:
        def __init__(self, host):
            self.host = host

    class FakeRequest:
        def __init__(self, host, headers):
            self.client = FakeClient(host)
            self.headers = headers

    trusted_proxy = FakeRequest(
        "127.0.0.1",
        {"x-forwarded-for": "203.0.113.10, 10.0.0.1"},
    )
    untrusted_client = FakeRequest(
        "198.51.100.20",
        {"x-forwarded-for": "203.0.113.11"},
    )

    assert get_client_ip(trusted_proxy) == "203.0.113.10"
    assert get_client_ip(untrusted_client) == "198.51.100.20"


def test_audit_rows_older_than_retention_are_pruned(app, client):
    old_created_at = "2020-01-01T00:00:00"
    execute(
        app,
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
            None,
            "",
            "203.0.113.30",
            "GET",
            "/old",
            200,
            "access",
            "old_access",
            "{}",
            old_created_at,
        ),
    )
    execute(
        app,
        """
        INSERT INTO registration_attempts
            (ip_address, nickname, result, created_at)
        VALUES (?, ?, ?, ?)
        """,
        ("203.0.113.30", "old", "missing_fields", old_created_at),
    )

    client.get("/login")

    assert not rows(app, "SELECT id FROM activity_logs WHERE action = 'old_access'")
    assert not rows(
        app,
        "SELECT id FROM registration_attempts WHERE nickname = 'old'",
    )


def test_existing_users_table_gets_grade_and_program_columns(tmp_path):
    db_path = tmp_path / "old_schema.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                real_name TEXT NOT NULL,
                nickname TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    migrated_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    columns = {
        row["name"]
        for row in rows(migrated_app, "PRAGMA table_info(users)")
    }

    assert "grade" in columns
    assert "program" in columns
    assert "is_admin" in columns
    assert "privacy_consent_at" in columns

    tables = {
        row["name"]
        for row in rows(
            migrated_app,
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    assert "app_settings" in tables
    assert "registration_attempts" in tables
    assert "activity_logs" in tables
    setting = rows(
        migrated_app,
        "SELECT value FROM app_settings WHERE key = ?",
        ("registration_ip_limit_per_24h",),
    )[0]
    assert setting["value"] == "5"


def test_configured_admin_nickname_promotes_existing_user(tmp_path):
    db_path = tmp_path / "configured_admin.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                real_name TEXT NOT NULL,
                nickname TEXT NOT NULL UNIQUE,
                grade TEXT NOT NULL DEFAULT '',
                program TEXT NOT NULL DEFAULT '',
                is_admin INTEGER NOT NULL DEFAULT 0,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO users
                (real_name, nickname, grade, program, is_admin, password_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("老师", "teacher", "", "", 0, "hash", "2026-01-01T00:00:00"),
        )

    configured_app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(db_path),
            "SECRET_KEY": "test-secret",
            "ADMIN_NICKNAME": "teacher",
        }
    )

    users = rows(configured_app, "SELECT nickname, is_admin FROM users")
    assert users[0]["nickname"] == "teacher"
    assert users[0]["is_admin"] == 1


def test_login_success_and_bad_password(client):
    register(client)
    client.post("/logout")

    bad = login(client, password="wrong")
    assert "昵称或密码不正确" in bad.text

    good = login(client)
    assert "用户详情" in good.text


def test_login_ignores_external_next_url(client):
    register(client)
    client.post("/logout")

    response = client.post(
        "/login?next=https://example.com",
        data={"nickname": "sunny", "password": "pw"},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/profile"


def test_existing_user_without_privacy_consent_gets_login_modal(app, client):
    register(client)
    execute(
        app,
        "UPDATE users SET privacy_consent_at = '' WHERE nickname = ?",
        ("sunny",),
    )
    client.post("/logout")

    response = login(client)

    assert response.status_code == 200
    assert 'id="privacy-consent-dialog"' in response.text
    assert 'action="/privacy-consent"' in response.text

    denied = client.post(
        "/privacy-consent",
        data={"next": "/profile"},
        follow_redirects=True,
    )
    assert 'id="privacy-consent-dialog"' in denied.text
    assert rows(app, "SELECT privacy_consent_at FROM users")[0][
        "privacy_consent_at"
    ] == ""

    accepted = client.post(
        "/privacy-consent",
        data={"privacy_consent": "yes", "next": "/profile"},
        follow_redirects=True,
    )
    assert 'id="privacy-consent-dialog"' not in accepted.text
    assert rows(app, "SELECT privacy_consent_at FROM users")[0][
        "privacy_consent_at"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/profile",
        "/mood-report",
        "/mood-calendar",
        "/mood-history",
        "/admin",
        "/admin/users/1",
    ],
)
def test_protected_pages_redirect_to_login(client, path):
    response = client.get(path)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_first_registered_user_can_view_admin_dashboard(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post(
        "/mood-report",
        data={
            "mood_emoji": "😄",
            "day_event": "检查后台",
            "body_feeling": "状态稳定",
            "extra_thoughts": "",
        },
        follow_redirects=True,
    )
    client.post("/logout")

    register(
        client,
        nickname="student",
        password="student-pw",
        grade="2025",
        program="IB",
        real_name="李四",
    )
    client.post(
        "/mood-report",
        data={
            "mood_emoji": "🙂",
            "day_event": "完成作业",
            "body_feeling": "有点累",
            "extra_thoughts": "想早点休息",
        },
        follow_redirects=True,
    )
    client.post("/logout")

    response = login(client, nickname="admin-user")
    assert "管理员后台" in response.text
    assert 'href="/admin/activity"' in response.text
    assert 'href="/admin/settings"' in response.text
    assert "进入后台" not in response.text
    assert "reason-question" in response.text
    assert "reason-answer" in response.text

    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "用户列表" in dashboard.text
    assert "搜索用户" in dashboard.text
    assert "admin-user-row" in dashboard.text
    assert "admin-user" in dashboard.text
    assert "student" in dashboard.text
    assert "2025" in dashboard.text
    assert "IB" in dashboard.text
    assert "条记录" in dashboard.text
    assert "检查后台" not in dashboard.text
    assert "完成作业" not in dashboard.text

    users = rows(app, "SELECT id, nickname, is_admin FROM users ORDER BY id")
    assert users[0]["nickname"] == "admin-user"
    assert users[0]["is_admin"] == 1
    assert users[1]["nickname"] == "student"
    assert users[1]["is_admin"] == 0
    assert f'href="/admin/users/{users[1]["id"]}"' in dashboard.text

    student_detail = client.get(f"/admin/users/{users[1]['id']}")
    assert student_detail.status_code == 200
    assert "用户心情记录" in student_detail.text
    assert "student" in student_detail.text
    assert "完成作业" in student_detail.text
    assert "检查后台" not in student_detail.text
    assert "reason-question" in student_detail.text
    assert "reason-answer" in student_detail.text

    filtered = client.get("/admin?q=student")
    filtered_panel = admin_user_panel(filtered.text)
    assert filtered.status_code == 200
    assert 'value="student"' in filtered.text
    assert "@student" in filtered_panel
    assert "@admin-user" not in filtered_panel

    filtered_at_nickname = client.get("/admin?q=@student")
    filtered_at_panel = admin_user_panel(filtered_at_nickname.text)
    assert filtered_at_nickname.status_code == 200
    assert "@student" in filtered_at_panel
    assert "@admin-user" not in filtered_at_panel

    empty = client.get("/admin?q=not-a-user")
    assert empty.status_code == 200
    assert "没有找到匹配的用户" in empty.text


def test_non_admin_user_cannot_view_admin_dashboard(client):
    register(client, nickname="admin-user")
    client.post("/logout")
    register(client, nickname="student")

    response = client.get("/admin", follow_redirects=True)

    assert "只有管理员可以访问后台" in response.text
    assert "用户列表" not in response.text

    settings = client.get("/admin/settings", follow_redirects=True)
    activity = client.get("/admin/activity", follow_redirects=True)
    assert "安全设置" not in settings.text
    assert "activity-log-entry" not in activity.text


def test_admin_can_promote_user_to_admin(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post("/logout")
    register(client, nickname="student", real_name="李四")
    client.post("/logout")

    login(client, nickname="admin-user")
    users = rows(app, "SELECT id, nickname, is_admin FROM users ORDER BY id")
    student_id = users[1]["id"]

    detail = client.get(f"/admin/users/{student_id}")
    assert detail.status_code == 200
    assert "设为管理员" in detail.text

    response = client.post(
        f"/admin/users/{student_id}/admin",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "已将 @student 设置为管理员" in response.text
    assert "设为管理员" not in response.text
    assert "管理员" in response.text

    promoted = rows(
        app,
        "SELECT is_admin FROM users WHERE id = ?",
        (student_id,),
    )[0]
    assert promoted["is_admin"] == 1

    repeated = client.post(
        f"/admin/users/{student_id}/admin",
        follow_redirects=True,
    )
    assert repeated.status_code == 200
    assert "这个用户已经是管理员" in repeated.text


def test_admin_can_bulk_delete_users_and_related_data(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post("/logout")
    register(client, nickname="student", real_name="李四")
    client.post(
        "/mood-report",
        data={
            "mood_emoji": MOODS[0]["emoji"],
            "day_event": "需要删除",
            "body_feeling": "还可以",
            "extra_thoughts": "",
        },
        follow_redirects=True,
    )
    client.post("/logout")
    register(client, nickname="other", real_name="王五")
    client.post("/logout")
    login(client, nickname="admin-user")

    user_rows = rows(app, "SELECT id, nickname FROM users ORDER BY id")
    target_ids = [
        row["id"]
        for row in user_rows
        if row["nickname"] in {"student", "other"}
    ]
    response = client.post(
        "/admin/users/delete",
        data={"user_ids": [str(user_id) for user_id in target_ids]},
        follow_redirects=True,
    )

    assert response.status_code == 200
    remaining_users = rows(app, "SELECT nickname FROM users ORDER BY id")
    assert [row["nickname"] for row in remaining_users] == ["admin-user"]
    assert rows(app, "SELECT COUNT(*) AS count FROM mood_entries")[0]["count"] == 0
    assert all(
        row["user_id"] is None
        for row in rows(
            app,
            """
            SELECT user_id
            FROM registration_attempts
            WHERE nickname IN (?, ?)
            """,
            ("student", "other"),
        )
    )
    assert not rows(
        app,
        f"SELECT id FROM activity_logs WHERE user_id IN ({','.join('?' for _ in target_ids)})",
        tuple(target_ids),
    )
    deletion_log = rows(
        app,
        "SELECT metadata FROM activity_logs WHERE action = ?",
        ("admin_deleted_users",),
    )[0]
    assert "student" in deletion_log["metadata"]
    assert "other" in deletion_log["metadata"]


def test_admin_bulk_delete_skips_current_user(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post("/logout")
    register(client, nickname="student", real_name="李四")
    client.post("/logout")
    login(client, nickname="admin-user")

    users = rows(app, "SELECT id, nickname FROM users ORDER BY id")
    admin_id = users[0]["id"]
    student_id = users[1]["id"]

    self_only = client.post(
        "/admin/users/delete",
        data={"user_ids": str(admin_id)},
        follow_redirects=True,
    )
    assert self_only.status_code == 200
    assert [row["nickname"] for row in rows(app, "SELECT nickname FROM users")] == [
        "admin-user",
        "student",
    ]

    mixed = client.post(
        "/admin/users/delete",
        data={"user_ids": [str(admin_id), str(student_id)]},
        follow_redirects=True,
    )
    assert mixed.status_code == 200
    assert [row["nickname"] for row in rows(app, "SELECT nickname FROM users")] == [
        "admin-user",
    ]


def test_admin_activity_logs_key_actions_and_filters_static_assets(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post("/logout")
    login(client, nickname="admin-user")
    client.post(
        "/mood-report",
        data={
            "mood_emoji": MOODS[0]["emoji"],
            "day_event": "检查动态",
            "body_feeling": "还可以",
            "extra_thoughts": "不记录正文",
        },
        follow_redirects=True,
    )
    client.post(
        "/admin/settings",
        data={"registration_ip_limit_per_24h": "6"},
        follow_redirects=True,
    )
    client.get("/static/styles.css")

    actions = {
        row["action"]
        for row in rows(app, "SELECT action FROM activity_logs")
    }
    assert "register_success" in actions
    assert "login_success" in actions
    assert "logout" in actions
    assert "mood_report_created" in actions
    assert "admin_settings_updated" in actions
    assert not rows(
        app,
        "SELECT id FROM activity_logs WHERE path = ?",
        ("/static/styles.css",),
    )
    assert not rows(
        app,
        "SELECT id FROM activity_logs WHERE metadata LIKE ?",
        ("%不记录正文%",),
    )

    activity_page = client.get("/admin/activity")
    assert activity_page.status_code == 200
    assert "register_success" in activity_page.text
    assert "mood_report_created" in activity_page.text
    assert "admin_settings_updated" in activity_page.text

    admin_id = rows(
        app,
        "SELECT id FROM users WHERE nickname = ?",
        ("admin-user",),
    )[0]["id"]
    filtered = client.get(f"/admin/activity?user_id={admin_id}")
    assert filtered.status_code == 200
    assert "admin-user" in filtered.text

    detail = client.get(f"/admin/users/{admin_id}")
    assert detail.status_code == 200
    assert "心情记录" in detail.text
    assert "最近访问动态" not in detail.text
    assert "mood_report_created" not in detail.text


def test_admin_activity_user_links_show_all_logs_for_selected_user(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post("/logout")
    register(client, nickname="student", real_name="李四")
    client.post("/logout")
    register(client, nickname="other", real_name="王五")
    client.post("/logout")
    login(client, nickname="admin-user")

    user_rows = {
        row["nickname"]: row["id"]
        for row in rows(app, "SELECT id, nickname FROM users")
    }
    student_id = user_rows["student"]
    other_id = user_rows["other"]
    now = datetime.now()
    with sqlite3.connect(app.state.config["DATABASE"]) as db:
        for index in range(205):
            db.execute(
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
                    student_id,
                    "student",
                    "10.0.0.2",
                    "GET",
                    f"/student/{index}",
                    200,
                    "access",
                    f"student_action_{index:03}",
                    "{}",
                    (now + timedelta(seconds=index)).isoformat(timespec="seconds"),
                ),
            )
        db.execute(
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
                other_id,
                "other",
                "10.0.0.3",
                "GET",
                "/other",
                200,
                "access",
                "other_action",
                "{}",
                (now + timedelta(seconds=300)).isoformat(timespec="seconds"),
            ),
        )
        db.commit()

    activity_page = client.get("/admin/activity")
    assert activity_page.status_code == 200
    assert f'href="/admin/activity/user?user_id={student_id}"' in activity_page.text
    assert (
        f'class="activity-user-link" href="/admin/activity/user?user_id={student_id}"'
        in activity_page.text
    )

    detail = client.get(f"/admin/activity/user?user_id={student_id}")
    assert detail.status_code == 200
    assert "用户动态详情" in detail.text
    assert "李四" in detail.text
    assert "@student" in detail.text
    assert "student_action_000" in detail.text
    assert "student_action_204" in detail.text
    assert "other_action" not in detail.text
    assert detail.text.count("activity-log-entry") >= 205

    filtered = client.get(f"/admin/activity?user_id={student_id}")
    assert filtered.status_code == 200
    assert "student_action_000" in filtered.text
    assert "student_action_204" in filtered.text
    assert "other_action" not in filtered.text
    assert filtered.text.count("activity-log-entry") >= 205

    nickname_filtered = client.get("/admin/activity?user_id=@student")
    assert nickname_filtered.status_code == 200
    assert "student_action_204" in nickname_filtered.text
    assert "other_action" not in nickname_filtered.text

    keyword_filtered = client.get("/admin/activity?q=@student")
    assert keyword_filtered.status_code == 200
    assert "student_action_204" in keyword_filtered.text
    assert "other_action" not in keyword_filtered.text


def test_non_admin_user_cannot_promote_users(app, client):
    register(client, nickname="admin-user")
    client.post("/logout")
    register(client, nickname="student")
    users = rows(app, "SELECT id, nickname, is_admin FROM users ORDER BY id")
    admin_id = users[0]["id"]

    response = client.post(
        f"/admin/users/{admin_id}/admin",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "只有管理员可以访问后台" in response.text
    assert rows(app, "SELECT is_admin FROM users WHERE nickname = 'student'")[0][
        "is_admin"
    ] == 0

    delete_response = client.post(
        "/admin/users/delete",
        data={"user_ids": str(admin_id)},
        follow_redirects=True,
    )
    assert delete_response.status_code == 200
    assert "只有管理员可以访问后台" in delete_response.text
    assert rows(app, "SELECT nickname FROM users WHERE id = ?", (admin_id,))


def test_update_nickname_success_and_duplicate(app, client):
    register(client)

    response = client.post(
        "/profile/nickname",
        data={"nickname": "new-sunny"},
        follow_redirects=True,
    )
    assert "昵称已更新" in response.text
    assert rows(app, "SELECT nickname FROM users")[0]["nickname"] == "new-sunny"

    client.post("/logout")
    register(client, nickname="other")
    duplicate = client.post(
        "/profile/nickname",
        data={"nickname": "new-sunny"},
        follow_redirects=True,
    )
    assert "这个昵称已经被使用" in duplicate.text


def test_update_profile_details_accepts_empty_values_and_rejects_invalid(app, client):
    register(client, grade="", program="")

    updated = client.post(
        "/profile/details",
        data={"grade": "2026", "program": "IB"},
        follow_redirects=True,
    )
    assert "个人资料已更新" in updated.text
    user = rows(app, "SELECT grade, program FROM users")[0]
    assert user["grade"] == "2026"
    assert user["program"] == "IB"

    cleared = client.post(
        "/profile/details",
        data={"grade": "", "program": ""},
        follow_redirects=True,
    )
    assert "个人资料已更新" in cleared.text
    user = rows(app, "SELECT grade, program FROM users")[0]
    assert user["grade"] == ""
    assert user["program"] == ""

    invalid = client.post(
        "/profile/details",
        data={"grade": "2023", "program": "IB"},
        follow_redirects=True,
    )
    assert "请选择有效的年级和项目" in invalid.text


def test_update_password_requires_current_password_and_updates_hash(app, client):
    register(client)

    bad_current = client.post(
        "/profile/password",
        data={
            "current_password": "wrong",
            "new_password": "new-pw",
            "confirm_password": "new-pw",
        },
        follow_redirects=True,
    )
    assert "当前密码不正确" in bad_current.text

    mismatch = client.post(
        "/profile/password",
        data={
            "current_password": "pw",
            "new_password": "new-pw",
            "confirm_password": "different",
        },
        follow_redirects=True,
    )
    assert "两次输入的新密码不一致" in mismatch.text

    changed = client.post(
        "/profile/password",
        data={
            "current_password": "pw",
            "new_password": "new-pw",
            "confirm_password": "new-pw",
        },
        follow_redirects=True,
    )
    assert "密码已更新" in changed.text

    client.post("/logout")
    old_password = login(client, password="pw")
    assert "昵称或密码不正确" in old_password.text
    new_password = login(client, password="new-pw")
    assert "用户详情" in new_password.text
    assert rows(app, "SELECT password_hash FROM users")[0]["password_hash"] != "pw"


def test_mood_submission_keeps_history_and_calendar_uses_latest_emoji_only(
    app, client
):
    register(client)

    first = client.post(
        "/mood-report",
        data={
            "mood_emoji": "🙂",
            "day_event": "第一条记录",
            "body_feeling": "身体状态还可以",
            "extra_thoughts": "",
        },
        follow_redirects=True,
    )
    assert first.status_code == 200

    second = client.post(
        "/mood-report",
        data={
            "mood_emoji": "😌",
            "day_event": "第二条记录",
            "body_feeling": "有点累",
            "extra_thoughts": "想早点睡",
        },
        follow_redirects=True,
    )
    html = second.text
    grid_html = calendar_grid(html)

    assert "😌" in grid_html
    assert "🙂" not in grid_html
    assert "第一条记录" not in grid_html
    assert "第二条记录" not in grid_html
    assert rows(app, "SELECT COUNT(*) AS count FROM mood_entries")[0]["count"] == 2


def test_recent_sidebar_limits_to_three_and_history_page_shows_all(app, client):
    register(client)

    for index in range(1, 6):
        client.post(
            "/mood-report",
            data={
                "mood_emoji": "😄",
                "day_event": f"历史记录 {index}",
                "body_feeling": f"身体感受 {index}",
                "extra_thoughts": "",
            },
            follow_redirects=True,
        )

    profile = client.get("/profile")
    assert "查看历史" in profile.text
    assert "历史记录 5" in profile.text
    assert "历史记录 4" in profile.text
    assert "历史记录 3" in profile.text
    assert "历史记录 2" not in profile.text
    assert "历史记录 1" not in profile.text

    history = client.get("/mood-history")
    assert history.status_code == 200
    for index in range(1, 6):
        assert f"历史记录 {index}" in history.text

    latest_entry = rows(app, "SELECT id FROM mood_entries ORDER BY id DESC LIMIT 1")[0]
    assert f'id="entry-{latest_entry["id"]}"' in history.text

    calendar_page = client.get("/mood-calendar")
    grid_html = calendar_grid(calendar_page.text)
    assert f'href="/mood-history#entry-{latest_entry["id"]}"' in grid_html


def test_scroll_lists_adapt_to_available_card_height_and_calendar_links_have_no_hover_underline():
    styles = Path("static/styles.css").read_text(encoding="utf-8")

    shell_rule_start = styles.index(".dashboard-shell {")
    shell_rule_end = styles.index("}", shell_rule_start)
    shell_rule = styles[shell_rule_start:shell_rule_end]
    assert "height: calc(100vh - (var(--shell-margin) * 2));" in shell_rule
    assert "min-height: 0;" in shell_rule

    card_rule_start = styles.index(".content-card.adaptive-scroll-card {")
    card_rule_end = styles.index("}", card_rule_start)
    card_rule = styles[card_rule_start:card_rule_end]
    assert "display: flex;" in card_rule
    assert "overflow: hidden;" in card_rule
    assert "height:" not in card_rule

    history_rule_start = styles.index(".history-entry-list {")
    history_rule_end = styles.index("}", history_rule_start)
    history_rule = styles[history_rule_start:history_rule_end]
    assert "max-height: none;" in history_rule
    assert "overflow-y: auto;" in history_rule
    assert "overscroll-behavior: contain;" in history_rule

    history_flex_rule_start = styles.index(".adaptive-scroll-card > .history-entry-list {")
    history_flex_rule_end = styles.index("}", history_flex_rule_start)
    history_flex_rule = styles[history_flex_rule_start:history_flex_rule_end]
    assert "flex: 1 1 auto;" in history_flex_rule

    recent_emoji_rule_start = styles.index(".recent-emoji {")
    recent_emoji_rule_end = styles.index("}", recent_emoji_rule_start)
    recent_emoji_rule = styles[recent_emoji_rule_start:recent_emoji_rule_end]
    assert "place-items: center;" in recent_emoji_rule

    emoji_glyph_rule_start = styles.index(".emoji-glyph {")
    emoji_glyph_rule_end = styles.index("}", emoji_glyph_rule_start)
    emoji_glyph_rule = styles[emoji_glyph_rule_start:emoji_glyph_rule_end]
    assert "display: grid;" in emoji_glyph_rule
    assert "width: 100%;" in emoji_glyph_rule
    assert "height: 100%;" in emoji_glyph_rule
    assert "place-items: center;" in emoji_glyph_rule
    assert "transform: none;" in emoji_glyph_rule
    assert "translate(" not in emoji_glyph_rule

    admin_panel_rule_start = styles.index(".admin-user-panel {")
    admin_panel_rule_end = styles.index("}", admin_panel_rule_start)
    admin_panel_rule = styles[admin_panel_rule_start:admin_panel_rule_end]
    assert "flex: 1 1 auto;" in admin_panel_rule

    admin_list_rule_start = styles.index(".admin-user-list {")
    admin_list_rule_end = styles.index("}", admin_list_rule_start)
    admin_list_rule = styles[admin_list_rule_start:admin_list_rule_end]
    assert "max-height: none;" in admin_list_rule
    assert "overflow-y: auto;" in admin_list_rule

    admin_name_rule_start = styles.index(".admin-user-name-line strong {")
    admin_name_rule_end = styles.index("}", admin_name_rule_start)
    admin_name_rule = styles[admin_name_rule_start:admin_name_rule_end]
    assert "flex: 0 0 auto;" in admin_name_rule

    calendar_hover_start = styles.index(".day-cell.has-entry:hover {")
    calendar_hover_end = styles.index("}", calendar_hover_start)
    calendar_hover_rule = styles[calendar_hover_start:calendar_hover_end]
    assert "text-decoration: none;" in calendar_hover_rule


def test_init_script_creates_systemd_service_from_env_example():
    script = Path("deploy-first-run.sh").read_text(encoding="utf-8")
    wrapper = Path("scripts/init_admin.sh").read_text(encoding="utf-8")
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert script.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
    assert "BASH_SOURCE" in script
    assert "need_cmd python3" in script
    assert "python3 - <<'PY'" in script
    assert "INSTALL_SYSTEMD=1" in script
    assert "START_SERVICE=1" in script
    assert "MOOD_ADMIN_NICKNAME" in script
    assert "MOOD_ADMIN_NAME" in script
    assert "MOOD_ADMIN_PASSWORD" in script
    assert "MOOD_SERVICE_NAME" in script
    assert "MOOD_PORT" in script
    assert "importlib.util.spec_from_file_location" in script
    assert "main.create_app()" not in script
    assert "mood.init_db(mood.app)" in script
    assert "mood.generate_password_hash(password)" in script
    assert "hashlib.pbkdf2_hmac" not in script
    assert "systemd/user" in script
    assert "ExecStart=/usr/bin/env python3 $APP_DIR/main.py" in script
    assert "Environment=PORT" not in script
    assert "EnvironmentFile" not in script
    assert "systemctl --user daemon-reload" in script
    assert 'systemctl --user enable "$SERVICE_NAME"' in script
    assert 'exec "$ROOT_DIR/deploy-first-run.sh" "$@"' in wrapper

    assert "INSTALL_SYSTEMD_SERVICE" not in env_example
    assert "SYSTEMD_SERVICE_NAME" not in env_example
    assert "APP_HOST" not in env_example
    assert "MOOD_HOST=127.0.0.1" in env_example
    assert "MOOD_PORT=5000" in env_example
    assert "MOOD_SECRET_KEY=replace-with-a-random-secret-key" in env_example
    assert "MOOD_ADMIN_NICKNAME=admin" in env_example


def test_mood_report_requires_emoji_and_required_questions(client):
    register(client)

    missing_emoji = client.post(
        "/mood-report",
        data={
            "mood_emoji": "",
            "day_event": "只有回答",
            "body_feeling": "身体还行",
            "extra_thoughts": "",
        },
        follow_redirects=True,
    )
    assert "请选择一个心情 emoji" in missing_emoji.text

    missing_required_answer = client.post(
        "/mood-report",
        data={
            "mood_emoji": "😄",
            "day_event": "",
            "body_feeling": "身体还行",
            "extra_thoughts": "",
        },
        follow_redirects=True,
    )
    assert "请回答前两个问题" in missing_required_answer.text


def test_sqlite_data_persists_across_app_recreation(tmp_path):
    db_path = tmp_path / "persistent.sqlite3"
    first_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    first_client = TestClient(first_app, follow_redirects=False)
    register(first_client)
    first_client.post(
        "/mood-report",
        data={
            "mood_emoji": "😄",
            "day_event": "重启后也应该还在",
            "body_feeling": "精神不错",
            "extra_thoughts": "",
        },
        follow_redirects=True,
    )

    second_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    second_client = TestClient(second_app, follow_redirects=False)
    response = login(second_client)

    assert "用户详情" in response.text
    calendar_page = second_client.get("/mood-calendar")
    assert "😄" in calendar_grid(calendar_page.text)
    assert rows(second_app, "SELECT COUNT(*) AS count FROM mood_entries")[0]["count"] == 1
