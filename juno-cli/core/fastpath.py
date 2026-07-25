"""Handle money messages without the tool-calling loop.

The full loop sends every tool schema plus the whole persona to the model
and waits for it to choose a tool, fill in arguments, run, and then write
prose about the result. On a local model that is seconds. Most of the time
it is also unnecessary, because the message is one of two very predictable
shapes:

    "spent 2k on uber and 450 on groceries"   -> log it
    "where did my money go this month"        -> show the summary

Neither needs a language model to *understand*. Amounts are found by regex
(tools/money_parser.py) and categories are usually already known
(tools/categorizer.py). So this module tries to answer first, and only
falls back to the LLM when it genuinely cannot.

What still reaches the model, and why:
    * a description never seen before, which needs a category invented —
      and even then it is one short completion, not the full tool loop
    * anything that isn't clearly logging or reporting

Everything handled here produces no model-written prose at all, which is
both faster and safer: the numbers on screen come from the database.
"""
from __future__ import annotations

import re
from typing import Optional

from tools import categorizer, expenses
from tools.money_parser import parse_money_text

# Phrases that name a time window, longest first so "last month" wins over
# "month". Anything not listed falls through to tools/expenses._period_range.
_PERIODS = [
    "all time", "everything", "last month", "this month", "last week",
    "this week", "last year", "this year", "yesterday", "today",
]
_LAST_N_DAYS = re.compile(r"(?:last|past)\s+(\d+)\s+days?")

# Words that mean the message is about one of F.R.I.D.A.Y's *other* jobs.
# Without this, "remind me to call ali tomorrow at 5" parses as a 5-rupee
# expense called "remind me to call ali tomorrow" — a reminder silently
# turning into a bogus expense is exactly the failure that would make this
# tool untrustworthy, so any hint of another intent defers to the model.
_OTHER_INTENT = {
    "remind", "reminder", "reminders", "remember", "task", "tasks", "todo",
    "organize", "organise", "tidy", "sort", "clean", "cleanup", "junk",
    "trash", "delete", "file", "files", "folder", "folders", "downloads",
    "desktop", "schedule", "alarm", "meeting", "email", "message", "call",
    "note", "notes", "install", "open", "run", "search",
}

# The message must be about money at all. Checked before anything is
# written, so an ordinary question can never become a database row.
_DOMAIN_WORDS = {
    "money", "spend", "spends", "spent", "spending", "expense", "expenses",
    "income", "earn", "earned", "earning", "salary", "budget", "cash",
    "cashflow", "save", "saved", "saving", "savings", "cost", "costs",
    "transaction", "transactions", "category", "categories", "paid", "pay",
    "bought", "buy", "sold", "sell", "sale", "dividend", "refund", "bill",
    "bills", "rent", "wallet", "account", "balance", "left", "afford",
}
_DOMAIN_PHRASES = ("money go", "money going", "cash flow", "came in",
                   "coming in", "paid me", "left over", "leftover")

_CASHFLOW_CUES = ("save", "saved", "saving", "left over", "leftover",
                  "left this", "left for", "cash flow", "cashflow", "net",
                  "in and out", "come in")
_INCOME_CUES = ("income", "earn", "earned", "earning", "coming in",
                "came in", "made this", "revenue")
_LIST_CUES = ("list", "line item", "transactions", "entries", "each one",
              "individual", "itemis", "itemiz")
_REPORT_CUES = ("spend", "spent", "spending", "expense", "money go",
                "money going", "breakdown", "summary", "report", "budget",
                "how much", "where did")


def _period(text: str) -> str:
    m = _LAST_N_DAYS.search(text)
    if m:
        return f"last {m.group(1)} days"
    for phrase in _PERIODS:
        if phrase in text:
            return phrase
    return "this month"


def _group_by(text: str) -> str:
    if "by month" in text or "monthly" in text or "each month" in text:
        return "month"
    if "by day" in text or "daily" in text or "each day" in text:
        return "day"
    if "by year" in text or "yearly" in text or "each year" in text:
        return "year"
    return "category"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text))


def _is_money_talk(text: str) -> bool:
    """True only when the message is plainly about money and nothing else."""
    if _words(text) & _OTHER_INTENT:
        return False
    return bool(_words(text) & _DOMAIN_WORDS) or any(p in text for p in _DOMAIN_PHRASES)


def _looks_like_report(text: str) -> bool:
    """A question about money already logged, rather than a new entry."""
    if not _is_money_talk(text):
        return False
    return any(cue in text for cue in _REPORT_CUES + _CASHFLOW_CUES + _LIST_CUES)


_MONTHLY_CUES = ("month by month", "monthly", "by month", "each month",
                 "per month", "trend", "over time", "chart", "graph",
                 "visuali", "last few months", "recent months")


def _named_a_period(text: str) -> bool:
    return bool(_LAST_N_DAYS.search(text)) or any(p in text for p in _PERIODS)


def _handle_report(text: str) -> Optional[str]:
    """Answer a question about existing data. The tools print their own tables."""
    period, group_by = _period(text), _group_by(text)
    income = any(cue in text for cue in _INCOME_CUES)

    # Specific asks win over the default view.
    if any(cue in text for cue in _CASHFLOW_CUES) and not income:
        result = expenses.summarize_cashflow(period)
        return result.get("note") or ""

    if any(cue in text for cue in _LIST_CUES):
        result = expenses.list_expenses(
            period, kind="income" if income else "expense")
        return result.get("note") or ("" if result["count"] else
                                      f"Nothing logged for {result['period']}.")

    # Monthly is the default frame: an open "show me my expenses" means the
    # month-by-month view, not just whatever happens to be in this month.
    wants_monthly = any(cue in text for cue in _MONTHLY_CUES)
    if not income and (wants_monthly or not _named_a_period(text)):
        return expenses.monthly_report(12 if "year" in text else 6).get("note") or ""

    result = expenses.summarize_expenses(
        period, group_by=group_by, kind="income" if income else "expense")
    return result.get("note") or ""


def _fill_categories(items: list[dict], kind: str) -> tuple[list[dict], int]:
    """Categorize from memory; ask the model only about what's left.

    Returns the items with categories filled and how many needed the model.
    """
    unknown: list[int] = []
    for idx, item in enumerate(items):
        hit = categorizer.categorize(item["description"], kind)
        if hit:
            item["category"] = hit[0]
        else:
            unknown.append(idx)

    if not unknown:
        return items, 0

    known = expenses.known_categories(kind)
    labels = _ask_for_categories([items[i]["description"] for i in unknown], known, kind)
    for idx, label in zip(unknown, labels):
        items[idx]["category"] = label
    return items, len(unknown)


def _ask_for_categories(descriptions: list[str], known: list[str],
                        kind: str) -> list[str]:
    """One short completion asking only for category names.

    Deliberately not the tool loop: no schemas, no persona, no tool call to
    get wrong. If it fails, the entries are still logged, just uncategorized.
    """
    from core.brain import complete

    noun = "money received" if kind == "income" else "spending"
    n = len(descriptions)
    system = (
        f"You label personal finance entries ({noun}) with a category. "
        f"Use one or two plain words a person would use, like Transport, "
        f"Groceries, Rent, Utilities, Eating Out, Salary. "
        f"Reply with exactly {n} line{'s' if n > 1 else ''}, one category "
        f"per line, in the same order as the numbered list. Nothing else: "
        f"no numbering, no punctuation, no explanation, no blank lines."
    )
    if known:
        system += f" Reuse one of these whenever it fits: {', '.join(known)}."

    listing = "\n".join(f"{n}. {d}" for n, d in enumerate(descriptions, 1))
    try:
        reply = complete(system, listing, max_tokens=20 * len(descriptions) + 40)
    except Exception:
        return ["" for _ in descriptions]

    lines = [ln for ln in (_clean_label(x) for x in reply.splitlines()) if ln]
    # Pad or trim so every description gets exactly one answer.
    lines += [""] * (len(descriptions) - len(lines))
    return lines[: len(descriptions)]


def _clean_label(line: str) -> str:
    """Turn one line of model output into a category name, or reject it.

    A small model asked for a category will sometimes answer with a
    sentence ("where possible: no specific 'parking' in the user..."). That
    string would otherwise be stored verbatim and become a permanent
    category, so anything not shaped like a label is dropped and the entry
    falls back to Uncategorized — which the user can correct once, and the
    correction is then remembered.
    """
    text = re.sub(r"^\s*\d+[.)]?\s*", "", line).strip(" -*\t\"'.")
    # Explanatory prose gives itself away with punctuation.
    if any(ch in text for ch in ":;,()\"'"):
        return ""
    words = text.split()
    if not words or len(words) > 3 or len(text) > 24:
        return ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z &/-]*", text):
        return ""
    return text.title() if text.islower() else text


def _handle_log(text: str) -> Optional[str]:
    """Log spending/income parsed straight out of the message."""
    if not _is_money_talk(text.lower()):
        return None

    parsed = parse_money_text(text)
    if not parsed.confident:
        return None

    by_kind: dict[str, list[dict]] = {}
    for item in parsed.items:
        by_kind.setdefault(item.kind, []).append(
            {"amount": item.amount, "description": item.description,
             "raw_text": item.raw}
        )

    asked = 0
    summary: list[str] = []
    for kind, items in by_kind.items():
        items, n = _fill_categories(items, kind)
        asked += n
        fn = expenses.log_income if kind == "income" else expenses.log_expenses
        result = fn(items)
        if result.get("count"):
            noun = "entry" if result["count"] == 1 else "entries"
            summary.append(f"{result['count']} {kind} {noun}")

    if not summary:
        return None
    note = "Logged " + " and ".join(summary) + "."
    if asked:
        note += f" ({asked} new to me, so I asked for a category.)"
    return note


def try_fast_path(text: str) -> Optional[str]:
    """Answer without the tool loop, or return None to let the LLM handle it."""
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return None
    lowered = raw.lower()

    # A question about money already logged wins over logging, even when it
    # mentions a number ("anything over 5000 this month").
    if _looks_like_report(lowered) and not parse_money_text(raw).confident:
        return _handle_report(lowered)

    return _handle_log(raw)
