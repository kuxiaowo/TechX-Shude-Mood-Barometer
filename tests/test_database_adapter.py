import json
import sqlite3
import urllib.error
from io import BytesIO
from unittest.mock import patch

from database_adapter import D1GatewayAdapter


def test_d1_gateway_execute_maps_rows_and_metadata():
    response = {"results": [{"rows": [{"id": 3, "name": "x"}], "meta": {"changes": 0, "last_row_id": 3}}]}

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(response).encode()

    with patch("database_adapter.urllib.request.urlopen", return_value=FakeResponse()):
        cursor = D1GatewayAdapter("https://example.test", "secret").execute("SELECT ?", (3,))
    assert cursor.fetchone()["name"] == "x"
    assert cursor.lastrowid == 3


def test_d1_gateway_batch_posts_batch_mode():
    seen = {}
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"results":[{"rows":[],"meta":{"changes":2}}]}'
    def fake(req, timeout):
        seen.update(json.loads(req.data))
        return FakeResponse()
    with patch("database_adapter.urllib.request.urlopen", side_effect=fake):
        D1GatewayAdapter("https://example.test", "secret").batch([("DELETE FROM x", ())])
    assert seen["mode"] == "batch"


def test_d1_gateway_maps_constraint_http_error_to_integrity_error():
    body = json.dumps({
        "error": "database_integrity_error",
        "message": "Database constraint rejected the operation",
    }).encode()
    error = urllib.error.HTTPError(
        "https://example.test/internal/db", 409, "Conflict", {}, BytesIO(body)
    )
    with patch("database_adapter.urllib.request.urlopen", side_effect=error):
        try:
            D1GatewayAdapter("https://example.test", "secret").execute("INSERT INTO x VALUES (?)", (1,))
        except sqlite3.IntegrityError as exc:
            assert "constraint" in str(exc)
        else:
            raise AssertionError("expected sqlite3.IntegrityError")


def test_d1_gateway_commit_and_rollback_are_explicit_noops():
    adapter = D1GatewayAdapter("https://example.test", "secret")
    assert adapter.commit() is None
    assert adapter.rollback() is None
