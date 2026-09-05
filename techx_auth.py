from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from authlib.integrations.starlette_client import OAuth
from joserfc import jwt
from joserfc.jwk import KeySet
from starlette.datastructures import Headers, MutableHeaders

SESSION_COOKIE = "techx_session"
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cookie_value(headers: Headers, name: str) -> str:
    jar = SimpleCookie()
    jar.load(headers.get("cookie", ""))
    morsel = jar.get(name)
    return morsel.value if morsel else ""


def _set_cookie_header(
    value: str, *, secure: bool, max_age: int, name: str = SESSION_COOKIE
) -> str:
    jar = SimpleCookie()
    jar[name] = value
    morsel = jar[name]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    morsel["max-age"] = max_age
    if secure:
        morsel["secure"] = True
    return morsel.OutputString()


class DatabaseSessionMiddleware:
    """Expose Starlette's session mapping while keeping only an opaque cookie."""

    def __init__(
        self,
        app,
        *,
        database: str,
        secure: bool = True,
        idle_seconds: int = 7 * 86400,
        absolute_seconds: int = 30 * 86400,
    ) -> None:
        self.app = app
        self.database = database
        self.secure = secure
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        now = int(time.time())
        request_headers = Headers(scope=scope)
        raw_token = _cookie_value(request_headers, SESSION_COOKIE)
        legacy_cookie = _cookie_value(request_headers, "session")
        token_hash = token_digest(raw_token) if raw_token else ""
        session_data: dict[str, Any] = {}
        existing = None
        invalid_cookie = False
        if token_hash:
            with self._connect() as db:
                existing = db.execute(
                    """
                    SELECT token_hash, data_json, idle_expires_at, absolute_expires_at
                    FROM web_sessions
                    WHERE token_hash = ?
                    """,
                    (token_hash,),
                ).fetchone()
                if (
                    existing
                    and int(existing["idle_expires_at"]) > now
                    and int(existing["absolute_expires_at"]) > now
                ):
                    try:
                        session_data.update(json.loads(existing["data_json"]))
                    except (TypeError, json.JSONDecodeError):
                        existing = None
                        invalid_cookie = True
                else:
                    invalid_cookie = True
                    if existing:
                        db.execute(
                            "DELETE FROM web_sessions WHERE token_hash = ?",
                            (token_hash,),
                        )
                        db.commit()
                    existing = None

        scope["session"] = session_data

        async def send_wrapper(message) -> None:
            nonlocal raw_token, token_hash, existing
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                if legacy_cookie:
                    response_headers.append(
                        "set-cookie",
                        _set_cookie_header(
                            "", secure=self.secure, max_age=0, name="session"
                        ),
                    )
                with self._connect() as db:
                    rotate = bool(session_data.pop("_rotate", False))
                    if rotate and token_hash:
                        db.execute(
                            "DELETE FROM web_sessions WHERE token_hash = ?",
                            (token_hash,),
                        )
                        raw_token = ""
                        token_hash = ""
                        existing = None
                    if not session_data:
                        if token_hash:
                            db.execute(
                                "DELETE FROM web_sessions WHERE token_hash = ?",
                                (token_hash,),
                            )
                            db.commit()
                        if raw_token or invalid_cookie:
                            response_headers.append(
                                "set-cookie",
                                _set_cookie_header("", secure=self.secure, max_age=0),
                            )
                    else:
                        if not existing:
                            raw_token = secrets.token_urlsafe(48)
                            token_hash = token_digest(raw_token)
                            created_at = now
                            absolute_expires_at = now + self.absolute_seconds
                        else:
                            created_at = now
                            absolute_expires_at = int(existing["absolute_expires_at"])
                        idle_expires_at = min(
                            now + self.idle_seconds, absolute_expires_at
                        )
                        payload = json.dumps(
                            session_data,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        values = (
                            session_data.get("user_id"),
                            session_data.get("auth_sub", ""),
                            session_data.get("oidc_sid", ""),
                            payload,
                            now,
                            idle_expires_at,
                        )
                        if existing:
                            cursor = db.execute(
                                """
                                UPDATE web_sessions
                                SET user_id = ?, auth_sub = ?, oidc_sid = ?,
                                    data_json = ?, last_seen_at = ?, idle_expires_at = ?
                                WHERE token_hash = ?
                                """,
                                (*values, token_hash),
                            )
                            if cursor.rowcount == 0:
                                # A concurrent back-channel logout revoked this row.
                                # Never recreate an authenticated session after revocation.
                                session_data.clear()
                                db.commit()
                                response_headers.append(
                                    "set-cookie",
                                    _set_cookie_header(
                                        "", secure=self.secure, max_age=0
                                    ),
                                )
                                await send(message)
                                return
                        else:
                            db.execute(
                                """
                                INSERT INTO web_sessions (
                                    token_hash, user_id, auth_sub, oidc_sid, data_json,
                                    created_at, last_seen_at, idle_expires_at,
                                    absolute_expires_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    token_hash,
                                    *values[:4],
                                    created_at,
                                    *values[4:],
                                    absolute_expires_at,
                                ),
                            )
                        db.commit()
                        response_headers.append(
                            "set-cookie",
                            _set_cookie_header(
                                raw_token,
                                secure=self.secure,
                                max_age=self.absolute_seconds,
                            ),
                        )
            await send(message)

        await self.app(scope, receive, send_wrapper)


def configure_oidc(config: dict[str, Any]) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="nethub",
        client_id=config["OIDC_CLIENT_ID"],
        client_secret=config["OIDC_CLIENT_SECRET"],
        server_metadata_url=f"{config['OIDC_ISSUER']}/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile",
            "code_challenge_method": "S256",
            "token_endpoint_auth_method": "client_secret_basic",
        },
    )
    return oauth


def validate_backchannel_logout(
    token: str,
    *,
    jwks: dict[str, Any],
    issuer: str,
    client_id: str,
) -> dict[str, Any]:
    key_set = KeySet.import_key_set(jwks)
    claims = dict(jwt.decode(token, key_set, algorithms=["RS256"]).claims)
    if claims.get("iss") != issuer:
        raise ValueError("invalid logout token issuer")
    audience = claims.get("aud", [])
    if isinstance(audience, str):
        audience = [audience]
    if client_id not in audience:
        raise ValueError("invalid logout token audience")
    if not claims.get("iat") or not claims.get("jti"):
        raise ValueError("logout token requires iat and jti")
    events = claims.get("events")
    if not isinstance(events, dict) or BACKCHANNEL_LOGOUT_EVENT not in events:
        raise ValueError("invalid back-channel logout event")
    if claims.get("nonce") is not None:
        raise ValueError("logout token must not contain nonce")
    if not claims.get("sub") and not claims.get("sid"):
        raise ValueError("logout token requires sub or sid")
    if abs(int(time.time()) - int(claims["iat"])) > 300:
        raise ValueError("logout token is too old")
    return claims


def create_test_session(
    database: str | Path,
    *,
    user_id: int,
    auth_sub: str,
    oidc_sid: str = "test-sid",
) -> str:
    """Create a normal opaque session for integration tests without a test-only route."""
    now = int(time.time())
    raw_token = secrets.token_urlsafe(48)
    data = {
        "user_id": user_id,
        "auth_sub": auth_sub,
        "oidc_sid": oidc_sid,
    }
    with sqlite3.connect(database) as db:
        db.execute(
            """
            INSERT INTO web_sessions (
                token_hash, user_id, auth_sub, oidc_sid, data_json,
                created_at, last_seen_at, idle_expires_at, absolute_expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_digest(raw_token),
                user_id,
                auth_sub,
                oidc_sid,
                json.dumps(data, separators=(",", ":")),
                now,
                now,
                now + 7 * 86400,
                now + 30 * 86400,
            ),
        )
        db.commit()
    return raw_token
