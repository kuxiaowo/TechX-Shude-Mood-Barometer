import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import create_app


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
    return app.test_client()


def register(client, nickname="sunny", password="pw"):
    return client.post(
        "/register",
        data={"real_name": "张三", "nickname": nickname, "password": password},
        follow_redirects=True,
    )


def login(client, nickname="sunny", password="pw"):
    return client.post(
        "/login",
        data={"nickname": nickname, "password": password},
        follow_redirects=True,
    )


def rows(app, sql, params=()):
    with sqlite3.connect(app.config["DATABASE"]) as db:
        db.row_factory = sqlite3.Row
        return db.execute(sql, params).fetchall()


def test_register_success_and_password_hash(app, client):
    response = register(client)

    assert response.status_code == 200
    assert "你好，张三" in response.get_data(as_text=True)

    users = rows(app, "SELECT nickname, password_hash FROM users")
    assert users[0]["nickname"] == "sunny"
    assert users[0]["password_hash"] != "pw"


def test_register_rejects_duplicate_and_empty_fields(client):
    register(client)
    client.post("/logout")

    duplicate = register(client)
    assert "这个昵称已经被注册" in duplicate.get_data(as_text=True)

    empty = client.post(
        "/register",
        data={"real_name": "", "nickname": "", "password": ""},
        follow_redirects=True,
    )
    assert "都需要填写" in empty.get_data(as_text=True)


def test_login_success_and_bad_password(client):
    register(client)
    client.post("/logout")

    bad = login(client, password="wrong")
    assert "昵称或密码不正确" in bad.get_data(as_text=True)

    good = login(client)
    assert "用户详情" in good.get_data(as_text=True)


def test_login_ignores_external_next_url(client):
    register(client)
    client.post("/logout")

    response = client.post(
        "/login?next=https://example.com",
        data={"nickname": "sunny", "password": "pw"},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/profile"


@pytest.mark.parametrize("path", ["/profile", "/mood-report", "/mood-calendar"])
def test_protected_pages_redirect_to_login(client, path):
    response = client.get(path)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_mood_submission_keeps_history_and_calendar_uses_latest(app, client):
    register(client)

    first = client.post(
        "/mood-report",
        data={"mood_emoji": "🙂", "reason": "第一条记录"},
        follow_redirects=True,
    )
    assert first.status_code == 200

    second = client.post(
        "/mood-report",
        data={"mood_emoji": "😌", "reason": "第二条记录"},
        follow_redirects=True,
    )
    html = second.get_data(as_text=True)

    assert "第二条记录" in html
    assert rows(app, "SELECT COUNT(*) AS count FROM mood_entries")[0]["count"] == 2


def test_mood_report_requires_emoji_and_reason(client):
    register(client)

    missing_emoji = client.post(
        "/mood-report",
        data={"mood_emoji": "", "reason": "只有原因"},
        follow_redirects=True,
    )
    assert "请选择一个心情 emoji" in missing_emoji.get_data(as_text=True)

    missing_reason = client.post(
        "/mood-report",
        data={"mood_emoji": "😄", "reason": ""},
        follow_redirects=True,
    )
    assert "请写下造成这个心情的原因" in missing_reason.get_data(as_text=True)


def test_sqlite_data_persists_across_app_recreation(tmp_path):
    db_path = tmp_path / "persistent.sqlite3"
    first_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    first_client = first_app.test_client()
    register(first_client)
    first_client.post(
        "/mood-report",
        data={"mood_emoji": "😄", "reason": "重启后也应该还在"},
        follow_redirects=True,
    )

    second_app = create_app(
        {"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "test-secret"}
    )
    second_client = second_app.test_client()
    response = login(second_client)

    assert "用户详情" in response.get_data(as_text=True)
    calendar_page = second_client.get("/mood-calendar")
    assert "重启后也应该还在" in calendar_page.get_data(as_text=True)
