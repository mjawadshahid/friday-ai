"""Initialize data/ and create the tasks table on first run."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "tasks.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    """Return a connection with row_factory set so rows behave like dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        # Single table: tasks. Used by tools/reminders.py.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task        TEXT    NOT NULL,
                due_at      TEXT,
                created_at  TEXT    NOT NULL,
                done        INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at, done)"
        )
        conn.commit()


# Run on import so the DB always exists when the app starts.
init_db()
