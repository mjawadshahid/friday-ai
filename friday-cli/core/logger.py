"""Append-only action log.

Every tool call — successful, failed, or refused — is recorded with a
timestamp, the tool name, its arguments, and its result. This gives the
user a forensic trail and helps debug bad LLM calls.

Why plain text and not the database?
    The log is human-readable, grep-able, and survives database corruption.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from config import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "actions.log"

# Make sure the directory exists on first import.
LOG_DIR.mkdir(parents=True, exist_ok=True)

# We use stdlib logging — it's boring, reliable, and thread-safe enough
# for a single-user CLI. Format: 2026-07-04 15:55:42 | tool | args | result
_logger = logging.getLogger("friday.actions")
_logger.setLevel(logging.INFO)
_logger.propagate = False  # don't double-log via root

if not _logger.handlers:
    _handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    _logger.addHandler(_handler)


def log_action(tool: str, arguments: dict, result: str) -> None:
    """Record one tool invocation. Truncates long results so the log stays readable."""
    safe_args = {k: (str(v)[:200] + "…") if len(str(v)) > 200 else v for k, v in arguments.items()}
    safe_result = (result[:500] + "…") if len(result) > 500 else result
    _logger.info("%s | args=%s | result=%s", tool, json.dumps(safe_args, default=str), safe_result)
