"""Initialize data/ and create the app's tables on first run.

Tables:
    tasks       — reminders (tools/reminders.py)
    expenses    — logged spending (tools/expenses.py)
    categories  — the category vocabulary learned from the user's own
                  spending. Not seeded with anything: it grows as the model
                  invents categories, and `fold` is the normalized key used
                  to keep "Groceries"/"grocery" from becoming two rows.
"""
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


def _ensure_column(conn, table: str, column: str, decl: str) -> None:
    """Add a column to an existing table if it isn't there yet.

    CREATE TABLE IF NOT EXISTS silently does nothing when the table already
    exists, so new columns need this to reach databases created earlier.
    """
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with get_conn() as conn:
        # Used by tools/reminders.py.
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

        # --- Used by tools/expenses.py ---
        # One table holds both directions of money: `kind` is 'expense' or
        # 'income'. Amounts are always stored positive — the kind carries the
        # sign — so a mistyped sign can never silently flip a total.
        # `raw_text` keeps the user's original phrasing for that item so a
        # miscategorized entry can always be traced back to what was said.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                amount      REAL    NOT NULL,
                currency    TEXT    NOT NULL DEFAULT '',
                category    TEXT    NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                spent_at    TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                raw_text    TEXT    NOT NULL DEFAULT '',
                kind        TEXT    NOT NULL DEFAULT 'expense'
            )
            """
        )
        _ensure_column(conn, "expenses", "kind", "TEXT NOT NULL DEFAULT 'expense'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expenses_kind ON expenses(kind, spent_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expenses_spent ON expenses(spent_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expenses_cat ON expenses(category)"
        )

        # `name` is what the user sees; `fold` is the normalized match key
        # (see tools/expenses.py:_fold_key). UNIQUE on fold is what actually
        # prevents duplicate categories from creeping in. The fold is prefixed
        # with the kind ('expense:fuel' vs 'income:salary') so spending and
        # earning categories stay separate namespaces under one constraint.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                name        TEXT    NOT NULL,
                fold        TEXT    NOT NULL UNIQUE,
                created_at  TEXT    NOT NULL,
                uses        INTEGER NOT NULL DEFAULT 0,
                kind        TEXT    NOT NULL DEFAULT 'expense'
            )
            """
        )
        _ensure_column(conn, "categories", "kind", "TEXT NOT NULL DEFAULT 'expense'")

        # Learned categorization rules (tools/categorizer.py). Every time an
        # entry is categorized, what it looked like and what it was called
        # are remembered here, so the same purchase never needs the model
        # twice. `scope` is 'phrase' (a whole description) or 'token' (one
        # word); `hits` is how often that mapping has been confirmed.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rules (
                pattern     TEXT    NOT NULL,
                scope       TEXT    NOT NULL,
                kind        TEXT    NOT NULL,
                category    TEXT    NOT NULL,
                hits        INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL,
                UNIQUE(pattern, scope, kind, category)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rules_lookup ON rules(kind, scope, pattern)"
        )
        conn.commit()


# Run on import so the DB always exists when the app starts.
init_db()
