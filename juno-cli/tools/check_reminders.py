"""Standalone reminder notifier + startup scan helpers.

Two entry points:

1. As a library (used by main.py at startup):
       from tools.check_reminders import get_due, format_due
       due = get_due()
       if due: print(format_due(due))

2. As a CLI (used by Task Scheduler / cron):
       python -m tools.check_reminders
   This calls ``get_due_reminders()`` from the reminders module and fires
   a desktop notification for each due item via ``plyer``. Exit code 0
   even if no notifications fire.

Scheduling (see README for the full guide):
  Windows Task Scheduler:
      Action:  Start a program
      Program: C:\\path\\to\\juno-cli\\.venv\\Scripts\\python.exe
      Args:    -m tools.check_reminders
      Trigger: every 5 minutes (or on logon)
  macOS / Linux cron:
      *\\/5 * * * *  /path/to/juno-cli/.venv/bin/python -m tools.check_reminders
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure the project root is importable when this file is run as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.reminders import get_due_reminders  # noqa: E402


def get_due() -> list[dict[str, Any]]:
    """Return currently-due reminders as plain dicts (empty list if none)."""
    return get_due_reminders().get("reminders", [])


def format_due(items: list[dict[str, Any]]) -> str:
    """Pretty-print for the CLI startup banner."""
    if not items:
        return ""
    n = len(items)
    lines = [f"⏰  You have {n} pending task{'s' if n != 1 else ''}:"]
    for r in items:
        lines.append(f"   • [{r['id']}] {r['task']}  (due {r['due_at']})")
    return "\n".join(lines)


def _notify(title: str, message: str) -> None:
    """Fire one desktop notification. Falls back to a printed banner."""
    try:
        from plyer import notification  # type: ignore
        notification.notify(
            title=title,
            message=message,
            app_name="Juno",
            timeout=10,
        )
    except Exception as e:  # plyer missing or platform unsupported
        print(f"[notify-fallback: {e}] {title} — {message}")


def main() -> int:
    """CLI entry point. Returns 0 even on partial failure (cron-friendly)."""
    due = get_due()
    if not due:
        print("No reminders due.")
        return 0
    title = "Juno — Reminder"
    for r in due:
        msg = f"{r['task']} (due {r['due_at']})"
        _notify(title, msg)
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
