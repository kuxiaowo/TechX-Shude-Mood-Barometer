import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import (
    PANAS_ITEMS,
    calculate_panas_scores,
    create_app,
    get_client_ip,
    panas_display_scores,
    score_mood,
)


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "mood_test.sqlite3"),
            "SECRET_KEY": "test-secret",
            "ADMIN_NICKNAME": "admin-user",
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
    form_start = response.text.index("<form")
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
    assert (
        rows(
            app,
            "SELECT value FROM app_settings WHERE key = ?",
            ("registration_ip_limit_per_24h",),
        )[0]["value"]
        == "2"
    )

    invalid = client.post(
        "/admin/settings",
        data={"registration_ip_limit_per_24h": "0"},
        follow_redirects=True,
    )
    assert invalid.status_code == 200
    assert (
        rows(
            app,
            "SELECT value FROM app_settings WHERE key = ?",
            ("registration_ip_limit_per_24h",),
        )[0]["value"]
        == "2"
    )

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


def panas_data(positive=4, negative=2):
    return {
        item["key"]: str(positive if item["dimension"] == "positive" else negative)
        for item in PANAS_ITEMS
    }


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
    columns = {row["name"] for row in rows(migrated_app, "PRAGMA table_info(users)")}

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
    assert (
        rows(app, "SELECT privacy_consent_at FROM users")[0]["privacy_consent_at"] == ""
    )

    accepted = client.post(
        "/privacy-consent",
        data={"privacy_consent": "yes", "next": "/profile"},
        follow_redirects=True,
    )
    assert 'id="privacy-consent-dialog"' not in accepted.text
    assert rows(app, "SELECT privacy_consent_at FROM users")[0]["privacy_consent_at"]


@pytest.mark.parametrize(
    "path",
    [
        "/profile",
        "/mood-report",
        "/mood-calendar",
        "/mood-trends",
        "/mood-history",
        "/admin",
        "/admin/users/1",
    ],
)
def test_protected_pages_redirect_to_login(client, path):
    response = client.get(path)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_configured_admin_user_can_view_admin_dashboard(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post(
        "/mood-report",
        data=panas_data(),
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
        data=panas_data(positive=3, negative=3),
        follow_redirects=True,
    )
    client.post("/logout")

    response = login(client, nickname="admin-user")
    assert "管理控制台" in response.text
    assert 'href="/admin"' in response.text
    assert 'href="/admin/activity"' not in response.text
    assert 'href="/admin/settings"' not in response.text
    assert "进入后台" not in response.text
    assert "recent-score" in response.text
    assert "recent-score-meta" in response.text

    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "用户管理" in dashboard.text
    assert "管理控制台" in dashboard.text
    assert "返回用户端" in dashboard.text
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
    assert "entry-score-grid" in student_detail.text
    assert "情感平衡" in student_detail.text
    assert "正性情感" in student_detail.text
    assert "负性情感" in student_detail.text

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
    assert "用户管理" not in response.text

    settings = client.get("/admin/settings", follow_redirects=True)
    activity = client.get("/admin/activity", follow_redirects=True)
    assert "安全设置" not in settings.text
    assert "activity-log-entry" not in activity.text


def test_admin_can_change_user_role(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post("/logout")
    register(client, nickname="student", real_name="李四")
    client.post("/logout")

    login(client, nickname="admin-user")
    users = rows(app, "SELECT id, nickname, is_admin FROM users ORDER BY id")
    student_id = users[1]["id"]

    detail = client.get(f"/admin/users/{student_id}")
    assert detail.status_code == 200
    assert "更新角色" in detail.text

    response = client.post(
        f"/admin/users/{student_id}/role",
        data={"role": "admin"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "已将 @student 设为管理员" in response.text
    assert "管理员" in response.text

    promoted = rows(
        app,
        "SELECT is_admin FROM users WHERE id = ?",
        (student_id,),
    )[0]
    assert promoted["is_admin"] == 1

    demoted = client.post(
        f"/admin/users/{student_id}/role",
        data={"role": "member"},
        follow_redirects=True,
    )
    assert demoted.status_code == 200
    assert "已将 @student 设为普通用户" in demoted.text
    assert (
        rows(app, "SELECT is_admin FROM users WHERE id = ?", (student_id,))[0][
            "is_admin"
        ]
        == 0
    )


def test_admin_can_disable_and_enable_user_account(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post("/logout")
    register(client, nickname="student", password="student-pw", real_name="李四")
    client.post("/logout")
    login(client, nickname="admin-user")

    student_id = rows(
        app,
        "SELECT id FROM users WHERE nickname = 'student'",
    )[0]["id"]
    disabled = client.post(
        f"/admin/users/{student_id}/status",
        data={"status": "disabled"},
        follow_redirects=True,
    )
    assert "@student 已停用" in disabled.text
    assert (
        rows(app, "SELECT is_active FROM users WHERE id = ?", (student_id,))[0][
            "is_active"
        ]
        == 0
    )

    client.post("/logout")
    denied = login(client, nickname="student", password="student-pw")
    assert denied.status_code == 403
    assert "账号已被停用" in denied.text

    login(client, nickname="admin-user")
    enabled = client.post(
        f"/admin/users/{student_id}/status",
        data={"status": "active"},
        follow_redirects=True,
    )
    assert "@student 已启用" in enabled.text
    client.post("/logout")
    assert (
        "用户详情"
        in login(
            client,
            nickname="student",
            password="student-pw",
        ).text
    )


def test_admin_can_bulk_delete_users_and_related_data(app, client):
    register(client, nickname="admin-user", real_name="管理员")
    client.post("/logout")
    register(client, nickname="student", real_name="李四")
    client.post(
        "/mood-report",
        data=panas_data(),
        follow_redirects=True,
    )
    client.post("/logout")
    register(client, nickname="other", real_name="王五")
    client.post("/logout")
    login(client, nickname="admin-user")

    user_rows = rows(app, "SELECT id, nickname FROM users ORDER BY id")
    target_ids = [
        row["id"] for row in user_rows if row["nickname"] in {"student", "other"}
    ]
    student_id = next(row["id"] for row in user_rows if row["nickname"] == "student")
    execute(
        app,
        """
        INSERT INTO legacy_mood_entries (
            source_entry_id,
            user_id,
            mood_emoji,
            reason,
            entry_date,
            created_at,
            archived_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            9001,
            student_id,
            "😌",
            "旧版存档",
            "2025-01-01",
            "2025-01-01T12:00:00",
            "2026-01-01T12:00:00",
        ),
    )
    response = client.post(
        "/admin/users/delete",
        data={"user_ids": [str(user_id) for user_id in target_ids]},
        follow_redirects=True,
    )

    assert response.status_code == 200
    remaining_users = rows(app, "SELECT nickname FROM users ORDER BY id")
    assert [row["nickname"] for row in remaining_users] == ["admin-user"]
    assert rows(app, "SELECT COUNT(*) AS count FROM mood_entries")[0]["count"] == 0
    assert (
        rows(app, "SELECT COUNT(*) AS count FROM legacy_mood_entries")[0]["count"] == 0
    )
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
        data=panas_data(),
        follow_redirects=True,
    )
    client.post(
        "/admin/settings",
        data={"registration_ip_limit_per_24h": "6"},
        follow_redirects=True,
    )
    client.get("/static/styles.css")

    actions = {row["action"] for row in rows(app, "SELECT action FROM activity_logs")}
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
        ("%cheerful%",),
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


def test_non_admin_user_cannot_control_users(app, client):
    register(client, nickname="admin-user")
    client.post("/logout")
    register(client, nickname="student")
    users = rows(app, "SELECT id, nickname, is_admin FROM users ORDER BY id")
    admin_id = users[0]["id"]

    response = client.post(
        f"/admin/users/{admin_id}/role",
        data={"role": "member"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "只有管理员可以访问后台" in response.text
    assert (
        rows(app, "SELECT is_admin FROM users WHERE nickname = 'student'")[0][
            "is_admin"
        ]
        == 0
    )

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


def test_mood_submission_keeps_history_and_calendar_uses_latest_score_only(app, client):
    register(client)

    first = client.post(
        "/mood-report",
        data=panas_data(positive=5, negative=2),
        follow_redirects=True,
    )
    assert first.status_code == 200

    second = client.post(
        "/mood-report",
        data=panas_data(positive=2, negative=4),
        follow_redirects=True,
    )
    html = second.text
    grid_html = calendar_grid(html)

    assert 'style="--score: 25"' in grid_html
    assert 'style="--score: 88"' not in grid_html
    assert "↘" in grid_html
    assert "😄" not in grid_html
    assert rows(app, "SELECT COUNT(*) AS count FROM mood_entries")[0]["count"] == 2

    profile = client.get("/profile")
    assert "data-mood-chart" not in profile.text
    assert 'href="/mood-trends"' in profile.text

    calendar = client.get("/mood-calendar")
    assert "data-mood-chart" not in calendar.text
    assert "calendar-scroll-card" in calendar.text

    trends = client.get("/mood-trends")
    assert trends.status_code == 200
    assert trends.text.count("data-mood-chart") == 2
    assert '"score": 25' in trends.text
    assert '"score": 88' not in trends.text
    assert 'class="nav-item is-active" href="/mood-trends"' in trends.text


def test_recent_sidebar_limits_to_three_and_history_page_shows_all(app, client):
    register(client)

    for index in range(1, 6):
        client.post(
            "/mood-report",
            data=panas_data(positive=index, negative=3),
            follow_redirects=True,
        )

    profile = client.get("/profile")
    assert "查看历史" in profile.text
    assert profile.text.count('class="recent-score"') == 3

    history = client.get("/mood-history")
    assert history.status_code == 200
    assert history.text.count('class="history-entry"') == 5
    for item in PANAS_ITEMS:
        assert item["label"] in history.text

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

    history_flex_rule_start = styles.index(
        ".adaptive-scroll-card > .history-entry-list {"
    )
    history_flex_rule_end = styles.index("}", history_flex_rule_start)
    history_flex_rule = styles[history_flex_rule_start:history_flex_rule_end]
    assert "flex: 1 1 auto;" in history_flex_rule

    recent_score_rule_start = styles.index(".recent-score {")
    recent_score_rule_end = styles.index("}", recent_score_rule_start)
    recent_score_rule = styles[recent_score_rule_start:recent_score_rule_end]
    assert "place-items: center;" in recent_score_rule

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
    assert "need_cmd conda" in script
    assert '"$PYTHON_BIN" - "$INSTALL_SYSTEMD" "$START_SERVICE" <<\'PY\'' in script
    assert "INSTALL_SYSTEMD=1" in script
    assert "START_SERVICE=1" in script
    assert "MOOD_ADMIN_NICKNAME" not in script
    assert "MOOD_ADMIN_PASSWORD" not in script
    assert "MOOD_SERVICE_NAME" in script
    assert "MOOD_CONDA_ENV" in script
    assert "MOOD_PORT" in script
    assert "main.init_db(main.app)" in script
    assert "hashlib.pbkdf2_hmac" not in script
    assert "systemd/user" in script
    assert "ExecStart=$PYTHON_BIN $APP_DIR/main.py" in script
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
    assert "MOOD_PUBLIC_BASE_URL=https://" in env_example
    assert "ACCOUNTS_CLIENT_ID=techx" in env_example
    assert "ACCOUNTS_CLIENT_SECRET=replace-with-the-client-secret" in env_example
    assert "MOOD_ADMIN_NICKNAME" not in env_example


def test_mood_report_requires_all_ten_valid_panas_answers(client):
    register(client)

    missing_answer_data = panas_data()
    missing_answer_data.pop("cheerful")
    missing_answer = client.post(
        "/mood-report",
        data=missing_answer_data,
        follow_redirects=True,
    )
    assert "请完成全部 10 项" in missing_answer.text

    invalid_answer_data = panas_data()
    invalid_answer_data["sad"] = "6"
    invalid_answer = client.post(
        "/mood-report",
        data=invalid_answer_data,
        follow_redirects=True,
    )
    assert "请完成全部 10 项" in invalid_answer.text


def test_mood_report_uses_child_friendly_five_point_wording(client):
    register(client)

    response = client.get("/mood-report")

    assert response.status_code == 200
    assert "参考 PANAS-C-SF" in response.text
    assert "心情愉快的" in response.text
    assert "痛苦、难受的" in response.text
    assert "有一点" in response.text
    assert "比较强烈" in response.text
    assert "非常强烈" in response.text
    assert "正性与负性分别看" in response.text
    assert "范围 -20～+20" in response.text


def test_panas_scores_include_raw_dimensions_balance_and_stored_percentages():
    high = calculate_panas_scores(panas_data(positive=5, negative=1))
    middle = calculate_panas_scores(panas_data(positive=3, negative=3))

    assert high == {
        "responses": {
            item["key"]: 5 if item["dimension"] == "positive" else 1
            for item in PANAS_ITEMS
        },
        "positive_raw": 25,
        "negative_raw": 5,
        "balance_score": 20,
        "positive_score": 100,
        "negative_score": 0,
        "mood_score": 100,
    }
    assert middle["positive_score"] == 50
    assert middle["negative_score"] == 50
    assert middle["mood_score"] == 50
    assert middle["positive_raw"] == 15
    assert middle["negative_raw"] == 15
    assert middle["balance_score"] == 0


def test_stored_percentage_scores_can_be_displayed_as_raw_scores():
    assert panas_display_scores(100, 0) == {
        "positive_raw": 25,
        "negative_raw": 5,
        "balance": 20,
        "balance_label": "+20",
    }
    assert panas_display_scores(50, 50)["balance_label"] == "0"


@pytest.mark.parametrize(
    ("score", "emoji", "label"),
    [
        (0, "↘", "负性较多"),
        (49, "↘", "负性较多"),
        (50, "↔", "正负相等"),
        (51, "↗", "正性较多"),
        (100, "↗", "正性较多"),
    ],
)
def test_balance_percentage_maps_to_calendar_direction(score, emoji, label):
    mood = score_mood(score)

    assert mood["emoji"] == emoji
    assert mood["label"] == label


def test_sqlite_data_persists_across_app_recreation(tmp_path):
    db_path = tmp_path / "persistent.sqlite3"
    first_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    first_client = TestClient(first_app, follow_redirects=False)
    register(first_client)
    first_client.post(
        "/mood-report",
        data=panas_data(positive=5, negative=1),
        follow_redirects=True,
    )

    second_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    second_client = TestClient(second_app, follow_redirects=False)
    response = login(second_client)

    assert "用户详情" in response.text
    calendar_page = second_client.get("/mood-calendar")
    assert 'style="--score: 100"' in calendar_grid(calendar_page.text)
    assert (
        rows(second_app, "SELECT COUNT(*) AS count FROM mood_entries")[0]["count"] == 1
    )


def test_legacy_mood_records_are_archived_and_only_shown_in_calendar_and_history(
    tmp_path,
):
    db_path = tmp_path / "legacy.sqlite3"
    first_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    first_client = TestClient(first_app, follow_redirects=False)
    register(first_client)
    user_id = rows(first_app, "SELECT id FROM users")[0]["id"]
    today = datetime.now().date()
    old_reason = "今天做了什么，什么影响了你的心情？\n完成了旧版测试\n\n今天身体感觉怎么样？\n有一点疲惫"

    with sqlite3.connect(db_path) as db:
        db.execute("DROP INDEX idx_mood_entries_user_date")
        db.execute("DROP TABLE mood_entries")
        db.execute(
            """
            CREATE TABLE mood_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mood_emoji TEXT NOT NULL,
                reason TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO mood_entries (
                id, user_id, mood_emoji, reason, entry_date, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                77,
                user_id,
                "😡",
                old_reason,
                today.isoformat(),
                f"{today.isoformat()}T08:30:00",
            ),
        )
        db.commit()

    migrated_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    migrated_client = TestClient(migrated_app, follow_redirects=False)
    login(migrated_client)

    assert (
        rows(migrated_app, "SELECT COUNT(*) AS count FROM mood_entries")[0]["count"]
        == 0
    )
    archive = rows(migrated_app, "SELECT * FROM legacy_mood_entries")
    assert len(archive) == 1
    assert archive[0]["source_entry_id"] == 77
    assert archive[0]["mood_emoji"] == "😡"

    calendar = migrated_client.get(f"/mood-calendar?month={today:%Y-%m}")
    grid = calendar_grid(calendar.text)
    assert "😡" in grid
    assert "旧版心情存档" in grid
    assert "is-legacy-entry" in grid
    assert "存档" in grid
    assert "含 1 天旧版存档" in calendar.text

    history = migrated_client.get("/mood-history")
    assert "旧版心情存档" in history.text
    assert "完成了旧版测试" in history.text
    assert "有一点疲惫" in history.text
    assert "不计入趋势统计" in history.text

    trends = migrated_client.get("/mood-trends")
    assert "😡" not in trends.text
    assert '"score": null' in trends.text

    recreated_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    assert (
        rows(
            recreated_app,
            "SELECT COUNT(*) AS count FROM legacy_mood_entries",
        )[0]["count"]
        == 1
    )
