from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path


def techx_mappings(path: Path) -> dict[int, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("mappings"), list):
        raise ValueError("不支持的映射文件格式")
    result: dict[int, str] = {}
    seen_subs: set[str] = set()
    for item in payload["mappings"]:
        if item.get("source_app") != "techx":
            continue
        user_id = int(item["source_user_id"])
        auth_sub = str(uuid.UUID(str(item["central_sub"])))
        if user_id in result:
            raise ValueError(f"TechX 用户 ID 重复：{user_id}")
        if auth_sub in seen_subs:
            raise ValueError(f"中央 sub 重复映射到多个 TechX 用户：{auth_sub}")
        result[user_id] = auth_sub
        seen_subs.add(auth_sub)
    if not result:
        raise ValueError("映射文件中没有 TechX 用户")
    return result


def ensure_schema(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "auth_sub" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN auth_sub TEXT")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_auth_sub "
        "ON users (auth_sub) WHERE auth_sub IS NOT NULL"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS archived_local_passwords (
            user_id INTEGER PRIMARY KEY,
            password_hash TEXT NOT NULL,
            archived_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS web_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER,
            auth_sub TEXT NOT NULL DEFAULT '',
            oidc_sid TEXT NOT NULL DEFAULT '',
            data_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            idle_expires_at INTEGER NOT NULL,
            absolute_expires_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )


def apply_mapping(
    database: Path, mapping_path: Path, *, dry_run: bool
) -> dict[str, int]:
    mappings = techx_mappings(mapping_path)
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE")
        ensure_schema(db)
        users = db.execute(
            "SELECT id, auth_sub, password_hash FROM users ORDER BY id"
        ).fetchall()
        local_ids = {int(user["id"]) for user in users}
        mapping_ids = set(mappings)
        if local_ids != mapping_ids:
            raise ValueError(
                f"映射必须覆盖全部本地用户；缺少 {sorted(local_ids - mapping_ids)}，"
                f"多出 {sorted(mapping_ids - local_ids)}"
            )
        archived = updated = 0
        now = datetime.now().isoformat(timespec="seconds")
        for user in users:
            user_id = int(user["id"])
            auth_sub = mappings[user_id]
            existing_sub = str(user["auth_sub"] or "")
            if existing_sub and existing_sub != auth_sub:
                raise ValueError(f"用户 {user_id} 已绑定不同的中央 sub")
            password_hash = str(user["password_hash"] or "")
            if password_hash:
                db.execute(
                    """
                    INSERT INTO archived_local_passwords (user_id, password_hash, archived_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO NOTHING
                    """,
                    (user_id, password_hash, now),
                )
                archived += 1
            db.execute(
                "UPDATE users SET auth_sub = ?, password_hash = '' WHERE id = ?",
                (auth_sub, user_id),
            )
            updated += 1
        db.execute("DELETE FROM web_sessions")
        if dry_run:
            db.rollback()
        else:
            db.commit()
    return {"users": updated, "passwords_archived": archived}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply NetHub Accounts mappings to TechX"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.database.is_file() or not args.mapping.is_file():
        parser.error("database and mapping must be existing files")
    result = apply_mapping(args.database, args.mapping, dry_run=args.dry_run)
    mode = "dry-run" if args.dry_run else "applied"
    print(
        f"{mode}: users={result['users']}, passwords_archived={result['passwords_archived']}"
    )


if __name__ == "__main__":
    main()
