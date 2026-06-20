#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-"$ROOT_DIR/.env"}"
case "$ENV_FILE" in
  /*)
    ;;
  *)
    ENV_FILE="$ROOT_DIR/$ENV_FILE"
    ;;
esac

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

INSTALL_SYSTEMD_SERVICE="${INSTALL_SYSTEMD_SERVICE:-1}"
SYSTEMD_SERVICE_NAME="${SYSTEMD_SERVICE_NAME:-techx-shude-mood-barometer}"
SYSTEMD_SERVICE_USER="${SYSTEMD_SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
SYSTEMD_START_NOW="${SYSTEMD_START_NOW:-0}"
APP_HOST="${APP_HOST:-127.0.0.1}"
PORT="${PORT:-5000}"
INIT_PYTHON="${INIT_PYTHON:-}"
SYSTEMD_PYTHON="${SYSTEMD_PYTHON:-}"

resolve_init_python() {
  if [ -n "$INIT_PYTHON" ]; then
    printf '%s\n' "$INIT_PYTHON"
    return
  fi

  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi

  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi

  echo "Python interpreter not found. Install python3 or set INIT_PYTHON in .env." >&2
  exit 1
}

export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT_DIR"

"$(resolve_init_python)" - <<'PY'
from __future__ import annotations

import os
import sqlite3
import hashlib
import secrets
from datetime import datetime
from pathlib import Path

BASE_DIR = Path.cwd()
GRADES = ("", "2024", "2025", "2026")
PROGRAMS = ("", "AP", "IB")
PASSWORD_HASH_ITERATIONS = 1_000_000


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def generate_password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_HASH_ITERATIONS,
    )
    return f"pbkdf2:sha256:{PASSWORD_HASH_ITERATIONS}${salt}${derived_key.hex()}"


database_value = env_value("MOOD_DB_PATH", "data/mood_barometer.sqlite3")
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

resolve_service_python() {
  if [ -n "$SYSTEMD_PYTHON" ]; then
    printf '%s\n' "$SYSTEMD_PYTHON"
    return
  fi

  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi

  command -v python
}

create_systemd_service() {
  case "$INSTALL_SYSTEMD_SERVICE" in
    1|true|TRUE|yes|YES)
      ;;
    0|false|FALSE|no|NO)
      echo "Systemd service creation skipped: INSTALL_SYSTEMD_SERVICE=$INSTALL_SYSTEMD_SERVICE"
      return
      ;;
    *)
      echo "INSTALL_SYSTEMD_SERVICE must be 1 or 0." >&2
      exit 1
      ;;
  esac

  case "$SYSTEMD_SERVICE_NAME" in
    ""|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@-]*)
      echo "SYSTEMD_SERVICE_NAME contains invalid characters." >&2
      exit 1
      ;;
  esac

  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is required to create the auto-start service." >&2
    echo "Set INSTALL_SYSTEMD_SERVICE=0 in .env to only initialize the database/admin account." >&2
    exit 1
  fi

  if [ "$(id -u)" -ne 0 ]; then
    echo "Creating a systemd service requires root." >&2
    echo "Re-run with sudo, or set INSTALL_SYSTEMD_SERVICE=0 in .env to skip service creation." >&2
    exit 1
  fi

  service_python="$(resolve_service_python)"
  service_file="/etc/systemd/system/$SYSTEMD_SERVICE_NAME.service"
  tmp_file="$(mktemp)"

  cat > "$tmp_file" <<SERVICE
[Unit]
Description=TechX Shude Mood Barometer
After=network.target

[Service]
Type=simple
User=$SYSTEMD_SERVICE_USER
WorkingDirectory=$ROOT_DIR
Environment=APP_HOST=$APP_HOST
Environment=PORT=$PORT
Environment=PYTHONPATH=$ROOT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$service_python -m uvicorn main:app --host \${APP_HOST} --port \${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

  install -m 0644 "$tmp_file" "$service_file"
  rm -f "$tmp_file"
  systemctl daemon-reload
  systemctl enable "$SYSTEMD_SERVICE_NAME"

  if [ "$SYSTEMD_START_NOW" = "1" ]; then
    systemctl restart "$SYSTEMD_SERVICE_NAME"
  fi

  echo "Systemd service installed: $service_file"
  echo "Service enabled: $SYSTEMD_SERVICE_NAME"
}

create_systemd_service
