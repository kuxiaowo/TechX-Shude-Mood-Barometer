#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-"$ROOT_DIR/.env"}"
INSTALL_SYSTEMD_SERVICE=1
START_SERVICE=1
NO_SYSTEMD_ARG=0
NO_START_ARG=0

usage() {
  cat <<'EOF'
TechX Shude Mood Barometer first-run script

Usage:
  scripts/init_admin.sh [options]

Options:
  --no-systemd   Initialize the database/admin account only.
  --no-start     Create and enable the systemd user service, but do not start it.
  -h, --help     Show this help message.

Environment:
  ENV_FILE                Env file path, default ./.env
  SYSTEMD_SERVICE_NAME    systemd user service name, default techx-shude-mood-barometer.service
  SYSTEMD_START_NOW       1 to start/restart after service creation, 0 to skip

Notes:
  - The script uses the current default python3 from PATH.
  - Activate your virtualenv/conda env before running if the app dependencies live there.
  - The SQLite database defaults to ./data/mood_barometer.sqlite3.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-systemd)
      NO_SYSTEMD_ARG=1
      ;;
    --no-start)
      NO_START_ARG=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

log() {
  printf '[mood-barometer init] %s\n' "$*"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing command: $1" >&2
    echo "Install python3 or activate the environment that provides python3." >&2
    exit 1
  fi
}

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

need_cmd python3
PYTHON_BIN="$(command -v python3)"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

: "${ADMIN_NICKNAME:?ADMIN_NICKNAME is required in .env}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required in .env}"

SYSTEMD_SERVICE_NAME="${SYSTEMD_SERVICE_NAME:-techx-shude-mood-barometer.service}"
case "$SYSTEMD_SERVICE_NAME" in
  *.service)
    ;;
  *)
    SYSTEMD_SERVICE_NAME="$SYSTEMD_SERVICE_NAME.service"
    ;;
esac

case "${INSTALL_SYSTEMD_SERVICE:-1}" in
  1|true|TRUE|yes|YES)
    ;;
  0|false|FALSE|no|NO)
    INSTALL_SYSTEMD_SERVICE=0
    ;;
  *)
    echo "INSTALL_SYSTEMD_SERVICE must be 1 or 0." >&2
    exit 1
    ;;
esac

case "${SYSTEMD_START_NOW:-1}" in
  1|true|TRUE|yes|YES)
    ;;
  0|false|FALSE|no|NO)
    START_SERVICE=0
    ;;
  *)
    echo "SYSTEMD_START_NOW must be 1 or 0." >&2
    exit 1
    ;;
esac

if [ "$NO_SYSTEMD_ARG" = "1" ]; then
  INSTALL_SYSTEMD_SERVICE=0
fi

if [ "$NO_START_ARG" = "1" ]; then
  START_SERVICE=0
fi

APP_HOST="${APP_HOST:-127.0.0.1}"
PORT="${PORT:-5000}"

export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT_DIR"

log "Using python: $PYTHON_BIN"
log "App directory: $ROOT_DIR"
mkdir -p "$ROOT_DIR/data"
chmod 700 "$ROOT_DIR/data"

python3 - <<'PY'
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
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

if [ "$INSTALL_SYSTEMD_SERVICE" = "1" ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found; database/admin initialization is complete." >&2
    exit 0
  fi

  SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  SERVICE_FILE="$SERVICE_DIR/$SYSTEMD_SERVICE_NAME"
  mkdir -p "$SERVICE_DIR"

  log "Writing systemd user service: $SERVICE_FILE"
  cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=TechX Shude Mood Barometer
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment=APP_HOST=$APP_HOST
Environment=PORT=$PORT
Environment=PYTHONPATH=$ROOT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON_BIN -m uvicorn main:app --host $APP_HOST --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SERVICE

  systemctl --user daemon-reload
  systemctl --user enable "$SYSTEMD_SERVICE_NAME"

  if [ "$START_SERVICE" = "1" ]; then
    systemctl --user restart "$SYSTEMD_SERVICE_NAME"
    log "Service status: $(systemctl --user is-active "$SYSTEMD_SERVICE_NAME")"
  else
    log "Service created and enabled, but not started."
  fi

  if command -v loginctl >/dev/null 2>&1; then
    log "To keep the user service running after SSH logout, run: loginctl enable-linger $USER"
  fi
else
  log "Skipped systemd service creation."
fi

log "Initialization complete."
