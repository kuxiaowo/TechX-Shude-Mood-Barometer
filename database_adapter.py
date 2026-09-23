"""Database adapters used by the mood barometer.

SQLite remains the test/local backend.  Production can use the small signed D1
gateway without exposing D1 credentials to the application process.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import urllib.request
import urllib.error
import uuid
from typing import Any, Sequence


class D1Row(dict):
    """Small sqlite3.Row-compatible mapping for gateway responses."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class D1Cursor:
    def __init__(self, payload: dict[str, Any]):
        rows = payload.get("rows") or []
        columns = payload.get("columns") or ()
        self._rows = [D1Row(r) if isinstance(r, dict) else D1Row(zip(columns, r)) for r in rows]
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
        self.rowcount = int(meta.get("changes") or 0)
        self.lastrowid = meta.get("last_row_id")
        self.description = tuple((c, None, None, None, None, None, None) for c in (payload.get("columns") or (list(rows[0]) if rows and isinstance(rows[0], dict) else ())))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        return iter(self._rows)


class D1GatewayAdapter:
    def __init__(self, url: str, secret: str, timeout: float = 10.0):
        normalized_url = url.rstrip("/")
        self.url = (
            normalized_url
            if normalized_url.endswith("/internal/db")
            else normalized_url + "/internal/db"
        )
        self.secret, self.timeout = secret, timeout

    def _call(self, statements: list[dict[str, Any]], mode: str = "single") -> list[D1Cursor]:
        request_id, timestamp = str(uuid.uuid4()), int(time.time())
        body = {"requestId": request_id, "timestamp": timestamp, "mode": mode, "statements": statements}
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        canonical = f"v1\nPOST\n/internal/db\n{request_id}\n{timestamp}\n{digest}".encode()
        signature = hmac.new(self.secret.encode(), canonical, hashlib.sha256).hexdigest()
        req = urllib.request.Request(self.url, raw, {
            "Content-Type": "application/json",
            "X-DB-Request-ID": request_id,
            "X-DB-Timestamp": str(timestamp),
            "X-DB-Signature": signature,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                result = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            message = str(payload.get("message") or payload.get("error") or exc.reason)
            if exc.code == 409 or payload.get("error") == "database_integrity_error":
                raise sqlite3.IntegrityError(message) from exc
            raise sqlite3.OperationalError(
                f"D1 gateway request failed ({exc.code}): {message}"
            ) from exc
        except Exception as exc:
            if isinstance(exc, sqlite3.Error):
                raise
            raise sqlite3.OperationalError(f"D1 gateway request failed: {exc}") from exc
        if result.get("error"):
            raise sqlite3.OperationalError(str(result["error"]))
        results = result.get("results")
        if (
            not isinstance(results, list)
            or len(results) != len(statements)
            or not all(isinstance(item, dict) for item in results)
        ):
            raise sqlite3.OperationalError("invalid D1 gateway response")
        return [D1Cursor(item) for item in results]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> D1Cursor:
        return self._call([{"sql": sql, "params": list(params)}])[0]

    def executemany(self, sql: str, seq_of_params) -> D1Cursor:
        cursors = self._call([{"sql": sql, "params": list(params)} for params in seq_of_params], "batch")
        return cursors[-1] if cursors else D1Cursor({})

    def batch(self, statements):
        return self._call([{"sql": sql, "params": list(params)} for sql, params in statements], "batch")

    def executescript(self, _script: str):
        raise sqlite3.OperationalError("D1 schema changes are managed separately")

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


class SQLiteAdapter:
    @staticmethod
    def connect(path):
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return db
