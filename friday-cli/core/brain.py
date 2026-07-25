"""The brain: speaks to the LLM and orchestrates the tool-calling loop.

Flow per turn:
    1. Send the full conversation + tool list to the chat completion API.
    2. If the model returns tool_calls, execute each one, append the
       results to the conversation, and ask the model again.
    3. Stop when the model returns a plain assistant message (no tool
       calls).

Why a loop? Because the model may need multiple round-trips — e.g. it
calls clean_junk with dry_run=true, sees the preview, asks the user, the
user says "yes", and the model re-invokes clean_junk with dry_run=false.
The loop handles all of that automatically.
"""
from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from config import settings
from core.logger import log_action
from core.persona import SYSTEM_PROMPT
from tools import TOOL_MAP, TOOLS


def _client() -> OpenAI:
    return OpenAI(api_key=settings.api_key, base_url=settings.base_url)


def complete(system: str, user: str, max_tokens: int = 200) -> str:
    """One plain completion — no tools, no persona, no history.

    The tool-calling loop sends 13 tool schemas and a 6KB system prompt on
    every turn, which is most of what a local model spends its time on.
    When all we need is a short judgement call (see core/fastpath.py), this
    asks the narrow question instead and is several times faster.

    Reasoning models (Qwen3, and anything else that thinks before it
    answers) will happily spend the entire token budget on hidden reasoning
    and return an empty string for a question this small, so we ask them
    not to. Providers that don't understand the parameter reject the call,
    hence the retry without it.
    """
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    try:
        resp = _client().chat.completions.create(
            model=settings.model, messages=messages,
            max_tokens=max_tokens, reasoning_effort="none",
        )
    except Exception:
        resp = _client().chat.completions.create(
            model=settings.model, messages=messages, max_tokens=max_tokens,
        )
    return (resp.choices[0].message.content or "").strip()


def _run_tool(name: str, raw_args: str) -> str:
    """Dispatch one tool call. Returns a string result the model can read."""
    if name not in TOOL_MAP:
        msg = f"Unknown tool: {name}"
        log_action(name, {"_raw": raw_args}, msg)
        return json.dumps({"error": msg})
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as e:
        msg = f"Bad JSON arguments from model: {e}"
        log_action(name, {"_raw": raw_args}, msg)
        return json.dumps({"error": msg})
    try:
        result = TOOL_MAP[name](**args)
    except Exception as e:
        result = json.dumps({"error": f"{type(e).__name__}: {e}"})
    if not isinstance(result, str):
        result = json.dumps(result, default=str)
    log_action(name, args, result)
    return result


def chat(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Run a full conversation turn, handling all tool calls.

    Returns:
        final_text: the assistant's last natural-language reply
        messages:   the updated message list (caller appends to history)
    """
    client = _client()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    # Hard cap so a buggy loop can't run forever. 8 = plenty for a single turn.
    for _ in range(8):
        resp = client.chat.completions.create(
            model=settings.model,
            messages=messages,
            tools=[
                {
                    "type": "function",
                    "function": {k: v for k, v in spec.items() if k != "function"},
                }
                for spec in TOOLS
            ],
        )
        msg = resp.choices[0].message
        content = msg.content or ""
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            return content, messages

        # Record the assistant's tool-call turn, then each tool result.
        messages.append(msg.model_dump(exclude_unset=False))
        for call in tool_calls:
            result = _run_tool(call.function.name, call.function.arguments or "{}")
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.function.name,
                "content": result,
            })

    return "(friday: tool loop exceeded max iterations, stopping)", messages
