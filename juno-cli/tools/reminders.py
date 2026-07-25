"""Reminders / pending tasks.

Schema (SQLite, ``data/tasks.db``):
    tasks(id INTEGER PRIMARY KEY, task TEXT, due_at TEXT,
          created_at TEXT, done INTEGER DEFAULT 0)

* ``due_at`` and ``created_at`` are ISO 8601 strings. ``due_at`` may be NULL
  when the user's natural-language time could not be parsed — we still keep
  the reminder but flag it in the response.

Exposed to the LLM as three separate tools (per Prompt 5):
    add_reminder
    list_reminders
    complete_reminder

Internal only:
    get_due_reminders  — main.py calls this at startup, and
    tools/check_reminders.py calls it from outside the CLI.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import dateparser
from pydantic import BaseModel, Field

from ._db import get_conn, init_db

# Make sure the table exists on first import.
init_db()


# ---------- helpers ----------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_due(text: str) -> Optional[str]:
    """Parse natural-language time; return ISO 8601 string or None on failure."""
    dt = dateparser.parse(text, settings={"PREFER_DATES_FROM": "future"})
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat()


# ---------- public tool functions ----------

def add_reminder(task: str, due: str) -> dict:
    """Add a reminder. `due` may be natural language like 'tomorrow 5pm'."""
    if not task or not task.strip():
        return {"error": "task is required", "id": None, "due_at": None}

    due_at = _parse_due(due)
    if due_at is None:
        # We still store the row with due_at=NULL and tell the user.
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (task, due_at, created_at, done) VALUES (?, NULL, ?, 0)",
                (task.strip(), _now_iso()),
            )
            conn.commit()
            new_id = cur.lastrowid
        return {
            "id": new_id,
            "task": task.strip(),
            "due_at": None,
            "warning": f"Couldn't parse time {due!r}. Reminder stored with no due date.",
        }

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (task, due_at, created_at, done) VALUES (?, ?, ?, 0)",
            (task.strip(), due_at, _now_iso()),
        )
        conn.commit()
        new_id = cur.lastrowid
    return {"id": new_id, "task": task.strip(), "due_at": due_at}


def list_reminders(include_done: bool = False) -> dict:
    """Return reminders sorted by due_at ascending, with NULLs last."""
    where = "" if include_done else "WHERE done = 0"
    sql = f"""
        SELECT id, task, due_at, created_at, done
        FROM tasks
        {where}
        ORDER BY
            CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
            due_at ASC,
            id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return {
        "count": len(rows),
        "reminders": [
            {
                "id": r["id"],
                "task": r["task"],
                "due_at": r["due_at"],
                "created_at": r["created_at"],
                "done": bool(r["done"]),
            }
            for r in rows
        ],
    }


def complete_reminder(task_id: int) -> dict:
    """Mark a reminder done by id. Returns a tiny status dict."""
    with get_conn() as conn:
        cur = conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))
        conn.commit()
        if cur.rowcount == 0:
            return {"status": "not_found", "id": task_id}
    return {"status": "done", "id": task_id}


def get_due_reminders() -> dict:
    """Internal: reminders whose due_at <= now and not yet done."""
    now_iso = _now_iso()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, task, due_at, created_at FROM tasks "
            "WHERE done = 0 AND due_at IS NOT NULL AND due_at <= ? "
            "ORDER BY due_at ASC",
            (now_iso,),
        ).fetchall()
    return {
        "count": len(rows),
        "reminders": [dict(r) for r in rows],
    }


# ---------- OpenAI tool descriptors ----------

class AddReminderArgs(BaseModel):
    task: str = Field(..., description="Short description of the reminder.")
    due: str = Field(
        ...,
        description="Natural-language time, e.g. 'tomorrow 5pm', 'in 3 hours', 'friday'.",
    )


class ListRemindersArgs(BaseModel):
    include_done: bool = Field(
        False, description="If true, also include completed reminders.",
    )


class CompleteReminderArgs(BaseModel):
    task_id: int = Field(..., description="The numeric id of the reminder to mark done.")


TOOL_SPECS = [
    {
        "name": "add_reminder",
        "description": (
            "Store a reminder. `due` is natural language ('tomorrow 5pm', "
            "'in 3 hours', 'friday', 'next monday at 9am'). If the time "
            "can't be parsed, the reminder is still saved but with no "
            "due date — the response will include a warning."
        ),
        "parameters": AddReminderArgs.model_json_schema(),
        "function": add_reminder,
    },
    {
        "name": "list_reminders",
        "description": (
            "List reminders, sorted by due date (earliest first, items with "
            "no due date last). Set include_done=true to also see completed "
            "reminders."
        ),
        "parameters": ListRemindersArgs.model_json_schema(),
        "function": list_reminders,
    },
    {
        "name": "complete_reminder",
        "description": "Mark a reminder as done by its numeric id.",
        "parameters": CompleteReminderArgs.model_json_schema(),
        "function": complete_reminder,
    },
]
