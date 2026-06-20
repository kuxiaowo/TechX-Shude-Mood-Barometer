from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = BASE_DIR / "mood_barometer.sqlite3"
DEMO_PASSWORD = "test123456"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')

    return values


def database_path() -> Path:
    env = load_env_file(BASE_DIR / ".env")
    path = Path(env.get("MOOD_DB_PATH") or DEFAULT_DATABASE)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def init_schema(db: sqlite3.Connection) -> None:
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


def seed_demo_data() -> None:
    users = [
        ("demo_alice", "陈安然", "2024", "AP"),
        ("demo_ben", "周柏言", "2024", "IB"),
        ("demo_cathy", "林可欣", "2025", "AP"),
        ("demo_david", "何远航", "2025", "IB"),
        ("demo_ella", "吴一诺", "2026", "AP"),
        ("demo_finn", "赵明朗", "2026", "IB"),
        ("demo_grace", "宋嘉禾", "", "AP"),
        ("demo_hank", "许景行", "2025", ""),
        ("demo_iris", "沈清越", "2024", "AP"),
        ("demo_jason", "唐亦辰", "2026", "IB"),
    ]
    moods = ["😄", "🙂", "😌", "😟", "😢", "😡"]
    events = [
        "完成了一次小组展示，过程比想象中顺利。",
        "数学作业有点卡住，后来问同学弄明白了。",
        "午休时间和朋友聊天，心情放松了一些。",
        "今天测试比较密集，感觉脑子有点满。",
        "体育课跑步后很累，但是精神还可以。",
        "和同桌有一点误会，放学前已经说开。",
        "社团活动准备得不错，看到成果很开心。",
        "早上起晚了，节奏有点乱。",
        "英语阅读完成度不错，给自己一点信心。",
        "今天比较平稳，没有特别大的波动。",
    ]
    body_feelings = [
        "睡眠还可以，精力正常。",
        "有点累，肩颈紧。",
        "胃口不错，身体轻松。",
        "下午有些困，需要早点睡。",
        "运动后腿有点酸。",
    ]
    extra_thoughts = [
        "",
        "希望明天安排更从容一点。",
        "想把任务拆小一点。",
        "今天总体还能接受。",
        "需要多喝水。",
    ]

    db_path = database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    password_hash = generate_password_hash(DEMO_PASSWORD)
    now = datetime.now()
    start_day = date.today() - timedelta(days=20)

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        init_schema(db)

        existing_demo_users = db.execute(
            "SELECT id FROM users WHERE nickname LIKE 'demo_%'"
        ).fetchall()
        for user in existing_demo_users:
            db.execute("DELETE FROM mood_entries WHERE user_id = ?", (user["id"],))

        created_users = 0
        updated_users = 0
        user_ids: list[int] = []

        for index, (nickname, real_name, grade, program) in enumerate(users):
            existing = db.execute(
                "SELECT id FROM users WHERE nickname = ?",
                (nickname,),
            ).fetchone()
            created_at = (now - timedelta(days=45 - index)).isoformat(
                timespec="seconds"
            )

            if existing:
                db.execute(
                    """
                    UPDATE users
                    SET real_name = ?,
                        grade = ?,
                        program = ?,
                        password_hash = ?
                    WHERE id = ?
                    """,
                    (real_name, grade, program, password_hash, existing["id"]),
                )
                user_id = existing["id"]
                updated_users += 1
            else:
                cursor = db.execute(
                    """
                    INSERT INTO users
                        (
                            real_name,
                            nickname,
                            grade,
                            program,
                            is_admin,
                            password_hash,
                            created_at
                        )
                    VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        real_name,
                        nickname,
                        grade,
                        program,
                        password_hash,
                        created_at,
                    ),
                )
                user_id = cursor.lastrowid
                created_users += 1

            user_ids.append(user_id)

        inserted_entries = 0
        for user_index, user_id in enumerate(user_ids):
            for entry_index in range(8):
                day = start_day + timedelta(
                    days=(user_index * 2 + entry_index * 3) % 21
                )
                created_at = datetime.combine(day, datetime.min.time()).replace(
                    hour=8 + (entry_index % 10),
                    minute=(user_index * 7 + entry_index * 5) % 60,
                )
                mood = moods[(user_index + entry_index) % len(moods)]
                optional_extra = extra_thoughts[
                    (user_index + entry_index) % len(extra_thoughts)
                ]
                answers = [
                    (
                        "今天做了什么，什么影响了你的心情？\n"
                        f"{events[(user_index + entry_index) % len(events)]}"
                    ),
                    (
                        "今天身体感觉怎么样？\n"
                        f"{body_feelings[(user_index + entry_index) % len(body_feelings)]}"
                    ),
                ]
                if optional_extra:
                    answers.append(f"还有什么想说的？\n{optional_extra}")

                db.execute(
                    """
                    INSERT INTO mood_entries
                        (user_id, mood_emoji, reason, entry_date, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        mood,
                        "\n\n".join(answers),
                        day.isoformat(),
                        created_at.isoformat(timespec="seconds"),
                    ),
                )
                inserted_entries += 1

        db.commit()

    print(f"Database: {db_path}")
    print(f"Demo users created: {created_users}")
    print(f"Demo users updated: {updated_users}")
    print(f"Demo mood entries inserted: {inserted_entries}")
    print(f"Demo password for demo_* users: {DEMO_PASSWORD}")


if __name__ == "__main__":
    seed_demo_data()
