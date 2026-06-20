#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-"$ROOT_DIR/.env"}"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing env file: $ENV_FILE" >&2
  echo "Copy .env.example to .env and set ADMIN_NICKNAME and ADMIN_PASSWORD." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

: "${ADMIN_NICKNAME:?ADMIN_NICKNAME is required in .env}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required in .env}"

export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT_DIR"

python - <<'PY'
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE_DIR = Path.cwd()
GRADES = ("", "2024", "2025", "2026")
PROGRAMS = ("", "AP", "IB")


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


database_value = env_value("MOOD_DB_PATH", "mood_barometer.sqlite3")
database_path = Path(database_value)
if not database_path.is_absolute():
    database_path = BASE_DIR / database_path

admin_nickname = env_value("ADMIN_NICKNAME")
admin_password = os.environ.get("ADMIN_PASSWORD", "")
admin_real_name = env_value("ADMIN_REAL_NAME", "管理员") or "管理员"
admin_grade = env_value("ADMIN_GRADE")
admin_program = env_value("ADMIN_PROGRAM")

if not admin_nickname:
    raise SystemExit("ADMIN_NICKNAME cannot be empty.")
if not admin_password:
    raise SystemExit("ADMIN_PASSWORD cannot be empty.")
if admin_grade not in GRADES:
    raise SystemExit("ADMIN_GRADE must be empty, 2024, 2025, or 2026.")
if admin_program not in PROGRAMS:
    raise SystemExit("ADMIN_PROGRAM must be empty, AP, or IB.")

database_path.parent.mkdir(parents=True, exist_ok=True)

with sqlite3.connect(database_path) as db:
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

    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if "grade" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN grade TEXT NOT NULL DEFAULT ''")
    if "program" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN program TEXT NOT NULL DEFAULT ''")
    if "is_admin" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    password_hash = generate_password_hash(admin_password)
    existing = db.execute(
        "SELECT id FROM users WHERE nickname = ?",
        (admin_nickname,),
    ).fetchone()

    if existing:
        db.execute(
            """
            UPDATE users
            SET real_name = ?,
                grade = ?,
                program = ?,
                is_admin = 1,
                password_hash = ?
            WHERE id = ?
            """,
            (
                admin_real_name,
                admin_grade,
                admin_program,
                password_hash,
                existing["id"],
            ),
        )
        action = "updated"
    else:
        db.execute(
            """
            INSERT INTO users
                (real_name, nickname, grade, program, is_admin, password_hash, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (
                admin_real_name,
                admin_nickname,
                admin_grade,
                admin_program,
                password_hash,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        action = "created"

    db.commit()

print(f"Database initialized: {database_path}")
print(f"Admin account {action}: {admin_nickname}")
PY
