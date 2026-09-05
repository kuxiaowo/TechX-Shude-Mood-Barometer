from __future__ import annotations

import json
import sqlite3
import time
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import import_key

from main import create_app, safe_next_url
from scripts.apply_auth_mapping import apply_mapping
from techx_auth import (
    BACKCHANNEL_LOGOUT_EVENT,
    SESSION_COOKIE,
    create_test_session,
    token_digest,
)


@pytest.fixture()
def oidc_app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "LEGACY_AUTH_ENABLED": False,
            "DATABASE": str(tmp_path / "oidc.sqlite3"),
            "PUBLIC_BASE_URL": "https://techx.test",
            "SESSION_COOKIE_SECURE": False,
            "OIDC_ISSUER": "https://accounts.test",
            "OIDC_CLIENT_ID": "techx",
            "OIDC_CLIENT_SECRET": "test-client-secret",
        }
    )


@pytest.fixture()
def oidc_client(oidc_app):
    return TestClient(oidc_app, follow_redirects=False)


def db_rows(app, sql, params=()):
    with sqlite3.connect(app.state.config["DATABASE"]) as db:
        db.row_factory = sqlite3.Row
        return db.execute(sql, params).fetchall()


def install_fake_oidc(monkeypatch, app, userinfo):
    async def authorize_redirect(request, redirect_uri):
        assert redirect_uri == "https://techx.test/auth/callback"
        request.session["_state_nethub_test-state"] = {"nonce": "test-nonce"}
        return RedirectResponse(
            "https://accounts.test/oauth/authorize?state=test-state",
            status_code=302,
        )

    async def authorize_access_token(request):
        assert request.query_params.get("state") == "test-state"
        return {"access_token": "short-lived", "userinfo": userinfo}

    monkeypatch.setattr(
        app.state.oauth.nethub, "authorize_redirect", authorize_redirect
    )
    monkeypatch.setattr(
        app.state.oauth.nethub, "authorize_access_token", authorize_access_token
    )


def test_local_password_and_registration_endpoints_are_closed(oidc_client):
    assert oidc_client.get("/login").headers["location"].startswith("/auth/login")
    assert oidc_client.get("/register").headers["location"].startswith("/auth/login")
    assert oidc_client.post("/login").status_code == 410
    assert oidc_client.post("/register").status_code == 410
    assert oidc_client.post("/profile/password").status_code == 410


def test_legacy_signed_session_cookie_is_expired(oidc_app):
    browser = TestClient(oidc_app, follow_redirects=False)
    browser.cookies.set("session", "old-starlette-cookie")
    response = browser.get("/auth/logged-out")
    assert response.status_code == 200
    assert 'session=""' in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_service_origins_and_next_redirect_are_restricted(tmp_path):
    with pytest.raises(ValueError, match="MOOD_PUBLIC_BASE_URL"):
        create_app(
            {
                "TESTING": True,
                "LEGACY_AUTH_ENABLED": False,
                "DATABASE": str(tmp_path / "bad-origin.sqlite3"),
                "PUBLIC_BASE_URL": "https://techx.test/unexpected-path",
            }
        )

    assert safe_next_url("https://evil.example") == "/profile"
    assert safe_next_url("//evil.example") == "/profile"
    assert safe_next_url(r"/\evil.example") == "/profile"
    assert safe_next_url("/mood-history?month=2026-01") == (
        "/mood-history?month=2026-01"
    )


def test_oidc_callback_creates_non_admin_member_with_separate_privacy_consent(
    monkeypatch,
    oidc_app,
    oidc_client,
):
    sub = str(uuid.uuid4())
    install_fake_oidc(
        monkeypatch,
        oidc_app,
        {
            "sub": sub,
            "preferred_username": "central-user",
            "name": "Central Display Name",
            "sid": "central-session-id",
        },
    )
    started = oidc_client.get("/auth/login?next=/mood-history")
    assert started.status_code == 302
    assert started.headers["location"].startswith(
        "https://accounts.test/oauth/authorize"
    )
    session_cookie = started.headers["set-cookie"]
    assert "HttpOnly" in session_cookie
    assert "SameSite=Lax" in session_cookie
    cookie = oidc_client.cookies.get(SESSION_COOKIE)
    assert cookie and "central-user" not in cookie and sub not in cookie
    assert db_rows(
        oidc_app,
        "SELECT token_hash FROM web_sessions WHERE token_hash = ?",
        (token_digest(cookie),),
    )

    callback = oidc_client.get("/auth/callback?code=code&state=test-state")
    assert callback.status_code == 302
    assert callback.headers["location"] == "/mood-history"
    user = db_rows(oidc_app, "SELECT * FROM users")[0]
    assert user["auth_sub"] == sub
    assert user["nickname"] == "central-user"
    assert user["real_name"] == "Central Display Name"
    assert user["is_admin"] == 0
    assert user["privacy_consent_at"] == ""
    assert user["password_hash"] == ""

    profile = oidc_client.get("/profile")
    assert profile.status_code == 200
    assert 'id="privacy-consent-dialog"' in profile.text

    blocked = oidc_client.post(
        "/mood-report",
        data={"happy": "5"},
    )
    assert blocked.status_code == 302
    assert blocked.headers["location"] == "/profile"
    assert not db_rows(oidc_app, "SELECT id FROM mood_entries")


def test_existing_member_keeps_local_profile_role_and_privacy(
    monkeypatch, oidc_app, oidc_client
):
    sub = str(uuid.uuid4())
    with sqlite3.connect(oidc_app.state.config["DATABASE"]) as db:
        db.execute(
            """
            INSERT INTO users (
                real_name, nickname, grade, program, is_admin, is_active,
                privacy_consent_at, password_hash, auth_sub, created_at
            ) VALUES ('Local Real Name', 'local-name', '2025', 'IB', 1, 1,
                      '2026-01-01T00:00:00', '', ?, '2025-01-01T00:00:00')
            """,
            (sub,),
        )
        db.commit()
    install_fake_oidc(
        monkeypatch,
        oidc_app,
        {
            "sub": sub,
            "preferred_username": "changed-central-name",
            "name": "Changed Central Display",
            "sid": "second-session",
        },
    )
    oidc_client.get("/auth/login")
    assert oidc_client.get("/auth/callback?code=x&state=test-state").status_code == 302
    user = db_rows(oidc_app, "SELECT * FROM users")[0]
    assert user["real_name"] == "Local Real Name"
    assert user["nickname"] == "local-name"
    assert user["grade"] == "2025"
    assert user["program"] == "IB"
    assert user["is_admin"] == 1
    assert user["privacy_consent_at"] == "2026-01-01T00:00:00"


def test_accounts_outage_does_not_break_existing_local_session(monkeypatch, oidc_app):
    sub = str(uuid.uuid4())
    with sqlite3.connect(oidc_app.state.config["DATABASE"]) as db:
        cursor = db.execute(
            """
            INSERT INTO users (
                real_name, nickname, is_admin, is_active, privacy_consent_at,
                password_hash, auth_sub, created_at
            ) VALUES ('Existing', 'existing', 0, 1, '2026-01-01', '', ?, '2026-01-01')
            """,
            (sub,),
        )
        db.commit()
        user_id = cursor.lastrowid
    raw = create_test_session(
        oidc_app.state.config["DATABASE"], user_id=user_id, auth_sub=sub
    )
    browser = TestClient(oidc_app, follow_redirects=False)
    browser.cookies.set(SESSION_COOKIE, raw)
    assert browser.get("/profile").status_code == 200

    async def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("accounts unavailable")

    monkeypatch.setattr(oidc_app.state.oauth.nethub, "authorize_redirect", unavailable)
    fresh_browser = TestClient(oidc_app, follow_redirects=False)
    assert fresh_browser.get("/auth/login").status_code == 502
    assert browser.get("/profile").status_code == 200


def test_revoked_session_is_not_recreated_by_an_inflight_response(oidc_app):
    sub = str(uuid.uuid4())
    with sqlite3.connect(oidc_app.state.config["DATABASE"]) as db:
        cursor = db.execute(
            """
            INSERT INTO users (
                real_name, nickname, is_admin, is_active, privacy_consent_at,
                password_hash, auth_sub, created_at
            ) VALUES ('User', 'inflight', 0, 1, '2026-01-01', '', ?, '2026-01-01')
            """,
            (sub,),
        )
        db.commit()
        user_id = cursor.lastrowid
    raw = create_test_session(
        oidc_app.state.config["DATABASE"], user_id=user_id, auth_sub=sub
    )

    @oidc_app.get("/_test/revoke-during-request")
    async def revoke_during_request():
        with sqlite3.connect(oidc_app.state.config["DATABASE"]) as db:
            db.execute("DELETE FROM web_sessions")
            db.commit()
        return {"ok": True}

    browser = TestClient(oidc_app, follow_redirects=False)
    browser.cookies.set(SESSION_COOKIE, raw)
    response = browser.get("/_test/revoke-during-request")
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert not db_rows(oidc_app, "SELECT token_hash FROM web_sessions")


def test_signed_backchannel_logout_revokes_matching_session(oidc_app):
    sub = str(uuid.uuid4())
    sid = "central-sid-to-revoke"
    with sqlite3.connect(oidc_app.state.config["DATABASE"]) as db:
        cursor = db.execute(
            """
            INSERT INTO users (
                real_name, nickname, is_admin, is_active, privacy_consent_at,
                password_hash, auth_sub, created_at
            ) VALUES ('User', 'user', 0, 1, '2026-01-01', '', ?, '2026-01-01')
            """,
            (sub,),
        )
        db.commit()
        user_id = cursor.lastrowid
    raw = create_test_session(
        oidc_app.state.config["DATABASE"],
        user_id=user_id,
        auth_sub=sub,
        oidc_sid=sid,
    )
    browser = TestClient(oidc_app, follow_redirects=False)
    browser.cookies.set(SESSION_COOKIE, raw)
    assert browser.get("/profile").status_code == 200

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key = import_key(pem, "RSA", {"kid": "logout-test-key"})
    oidc_app.state.oidc_jwks = {
        "keys": [
            key.as_dict(private=False, use="sig", alg="RS256", kid="logout-test-key")
        ]
    }
    now = int(time.time())
    logout_token = jwt.encode(
        {"alg": "RS256", "kid": "logout-test-key"},
        {
            "iss": "https://accounts.test",
            "aud": ["techx"],
            "iat": now,
            "jti": str(uuid.uuid4()),
            "sub": sub,
            "sid": sid,
            "events": {BACKCHANNEL_LOGOUT_EVENT: {}},
        },
        key,
    )
    accounts = TestClient(oidc_app, follow_redirects=False)
    response = accounts.post(
        "/auth/backchannel-logout",
        data={"logout_token": logout_token},
    )
    assert response.status_code == 200
    assert response.json()["revoked"] == 1
    assert browser.get("/profile").status_code == 302

    replay = accounts.post(
        "/auth/backchannel-logout",
        data={"logout_token": logout_token},
    )
    assert replay.status_code == 200
    assert replay.json()["revoked"] == 0


def test_mapping_preserves_ids_business_data_roles_and_privacy(tmp_path):
    database = tmp_path / "techx.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "LEGACY_AUTH_ENABLED": False,
            "DATABASE": str(database),
            "SESSION_COOKIE_SECURE": False,
        }
    )
    with sqlite3.connect(database) as db:
        cursor = db.execute(
            """
            INSERT INTO users (
                real_name, nickname, grade, program, is_admin, is_active,
                privacy_consent_at, password_hash, created_at
            ) VALUES ('Student', 'student', '2024', 'AP', 1, 1,
                      '2026-02-03T04:05:06', 'scrypt:legacy', '2025-01-01')
            """
        )
        user_id = cursor.lastrowid
        db.execute(
            """
            INSERT INTO mood_entries (
                user_id, panas_responses, positive_score, negative_score,
                mood_score, entry_date, created_at
            ) VALUES (?, '{}', 20, 10, 75, '2026-01-01', '2026-01-01')
            """,
            (user_id,),
        )
        db.commit()
    sub = str(uuid.uuid4())
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "version": 1,
                "mappings": [
                    {
                        "source_app": "techx",
                        "source_user_id": str(user_id),
                        "central_sub": sub,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    apply_mapping(database, mapping, dry_run=True)
    assert (
        db_rows(app, "SELECT auth_sub, password_hash FROM users")[0]["auth_sub"] is None
    )

    result = apply_mapping(database, mapping, dry_run=False)
    assert result == {"users": 1, "passwords_archived": 1}
    user = db_rows(app, "SELECT * FROM users")[0]
    assert user["id"] == user_id
    assert user["auth_sub"] == sub
    assert user["password_hash"] == ""
    assert user["is_admin"] == 1
    assert user["privacy_consent_at"] == "2026-02-03T04:05:06"
    assert db_rows(app, "SELECT user_id FROM mood_entries")[0]["user_id"] == user_id
    assert (
        db_rows(app, "SELECT password_hash FROM archived_local_passwords")[0][
            "password_hash"
        ]
        == "scrypt:legacy"
    )
    assert apply_mapping(database, mapping, dry_run=False)["users"] == 1
