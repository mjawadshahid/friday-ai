"""Central tool registry.

Each tool module exposes a list of tool dicts (or a single one), each with:
    name        - used by the LLM to call it
    description - tells the LLM when to call it
    parameters  - JSON schema for the arguments
    function    - the actual Python callable

The brain iterates over ``TOOLS`` and hands the list to the OpenAI chat
completion API.
"""
from __future__ import annotations

from typing import Any, Callable

from . import file_organizer, junk_cleaner, reminders
from .check_reminders import get_due
from .file_organizer import TOOL_SPEC as _FILE_ORG
from .junk_cleaner import TOOL_SPEC as _JUNK
from .reminders import TOOL_SPECS as _REMINDERS

ToolDict = dict[str, Any]

# Tools the LLM may call.
TOOLS: list[ToolDict] = [
    _FILE_ORG,
    _JUNK,
    *_REMINDERS,  # add_reminder, list_reminders, complete_reminder
]

# name -> function, for fast dispatch.
TOOL_MAP: dict[str, Callable[..., Any]] = {
    spec["name"]: spec["function"] for spec in TOOLS
}

__all__ = ["TOOLS", "TOOL_MAP", "get_due"]
