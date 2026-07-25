"""Deterministic parser: plain English -> structured money entries.

No LLM involved. "spent 2k on uber, 1200 on the k-electric bill" becomes
two items with amounts and descriptions, using nothing but regex.

Why bother, when the model can already do this?
    Speed. Every LLM round-trip on a local model costs seconds; this costs
    microseconds. Splitting text and reading numbers is a solved problem
    that does not need a neural network. The model's real value is judging
    what *category* something belongs to, and even that only the first time
    (see tools/categorizer.py). So the LLM is a fallback, not the path.

Algorithm — amounts anchor everything
    1. Find every amount in the text. Each one becomes exactly one entry.
    2. The text between two amounts is split at the first connector
       (",", ".", "and", "also", …). What comes before the connector
       describes the amount on the left; what comes after describes the
       amount on the right.
    3. If an amount ends up with nothing on its right, it takes the text
       on its left instead. That's what makes "my salary came in, 150k"
       work as well as "spent 150k on rent".

The parser reports its own confidence. Anything it isn't sure about is
handed to the LLM rather than guessed at, because a silently mis-parsed
amount is the one bug this tool must never have.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# k / lakh / crore included: this is used with PKR and INR amounts as often
# as with plain numbers.
_MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "lac": 100_000, "lakh": 100_000, "lakhs": 100_000,
    "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
    "cr": 10_000_000, "crore": 10_000_000,
    "b": 1_000_000_000, "billion": 1_000_000_000,
}

_AMOUNT_RE = re.compile(
    r"(?:(?:rs|pkr|inr|usd)\.?\s*|[₨$])?"      # optional currency marker
    r"(\d[\d,]*(?:\.\d+)?)"                     # the number itself
    r"(?:\s*(" + "|".join(_MULTIPLIERS) + r")\b)?",  # optional multiplier
    re.IGNORECASE,
)

# Where one item ends and the next begins.
_CONNECTOR_RE = re.compile(
    r"[,;.]|\n|&|\b(?:and|also|plus|then|but|while|as\s+well\s+as)\b",
    re.IGNORECASE,
)

# Stripped from the ends of a description, never from the middle.
_FILLER = {
    "spent", "spend", "spending", "paid", "pay", "bought", "buy", "purchased",
    "got", "get", "received", "receive", "sold", "sell", "earned", "made",
    "on", "for", "to", "of", "from", "in", "at", "with", "by",
    "a", "an", "the", "some", "another", "new", "my", "me", "i", "mine",
    "like", "about", "around", "approx", "approximately", "roughly", "just",
    "only", "total", "worth", "was", "were", "is", "are", "am", "it", "that",
    "this", "rs", "pkr", "inr", "usd", "rupees", "bucks",
    "today", "yesterday", "also", "and", "then", "plus", "there", "here",
    "came", "come", "comes", "coming", "went", "gone", "did", "do", "done",
}

# Direction cues. Checked against the raw segment before filler is stripped,
# so "sold my old phone" is still recognizable as money coming in.
_INCOME_CUES = {
    "salary", "sold", "sell", "sale", "got", "received", "receive", "earned",
    "earn", "income", "dividend", "dividends", "refund", "refunded", "bonus",
    "credited", "credit", "deposit", "deposited", "cashback", "reimbursed",
    "reimbursement", "payout", "profit", "commission", "freelance", "client",
    "invoice", "paid me", "came in", "wage", "wages", "stipend", "pension",
    "interest", "rent from", "gift",
}
_EXPENSE_CUES = {
    "spent", "spend", "paid", "pay", "bought", "buy", "purchased", "cost",
    "bill", "charged", "expense", "on",
}


@dataclass
class ParsedItem:
    amount: float
    description: str
    kind: str  # 'expense' | 'income'
    raw: str = ""


@dataclass
class ParseResult:
    items: list[ParsedItem] = field(default_factory=list)
    confident: bool = False
    reason: str = ""

    def as_tool_items(self) -> list[dict]:
        """Shape the expenses tools expect."""
        return [
            {"amount": i.amount, "description": i.description, "raw_text": i.raw}
            for i in self.items
        ]


def _to_amount(number: str, multiplier: Optional[str]) -> Optional[float]:
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    if multiplier:
        value *= _MULTIPLIERS[multiplier.lower()]
    return value if value > 0 else None


def _split_at_connector(text: str) -> tuple[str, str]:
    """Return (belongs_to_previous_amount, belongs_to_next_amount)."""
    m = _CONNECTOR_RE.search(text)
    if not m:
        return text, ""
    return text[: m.start()], text[m.end():]


def _clean(text: str) -> str:
    """Trim filler words off both ends, keeping the meaningful middle."""
    words = re.findall(r"[\w'&/-]+", text or "")
    while words and words[0].lower().strip(".,") in _FILLER:
        words.pop(0)
    while words and words[-1].lower().strip(".,") in _FILLER:
        words.pop()
    return " ".join(words).strip()


def _direction(*segments: str) -> str:
    """Decide income vs expense from the words around an amount."""
    blob = " ".join(s.lower() for s in segments if s)
    if not blob.strip():
        return "expense"
    # Phrase cues first — "paid me" means the opposite of "paid".
    if "paid me" in blob or "came in" in blob or "credited" in blob:
        return "income"
    words = set(re.findall(r"[a-z]+", blob))
    income_hits = len(words & _INCOME_CUES)
    expense_hits = len(words & _EXPENSE_CUES)
    if income_hits and income_hits >= expense_hits:
        return "income"
    return "expense"


def parse_money_text(text: str, default_kind: str = "") -> ParseResult:
    """Parse a sentence of spending/earning into structured items.

    `default_kind` forces every item to one direction; leave empty to
    detect it per item from the surrounding words.
    """
    text = (text or "").strip()
    if not text:
        return ParseResult(reason="empty input")

    matches = [m for m in _AMOUNT_RE.finditer(text) if _to_amount(m.group(1), m.group(2))]
    if not matches:
        return ParseResult(reason="no amounts found")

    items: list[ParsedItem] = []
    # Text before the first amount seeds the first item's description.
    pending_pre = text[: matches[0].start()]

    for idx, m in enumerate(matches):
        amount = _to_amount(m.group(1), m.group(2))
        end = m.end()
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        between = text[end:next_start]

        own, carry = _split_at_connector(between)

        # Prefer the words right after the amount; fall back to the ones
        # before it ("my salary came in, 150k").
        description = _clean(own) or _clean(pending_pre)
        kind = default_kind or _direction(pending_pre, own)

        items.append(ParsedItem(
            amount=amount,
            description=description,
            kind=kind,
            raw=(pending_pre + m.group(0) + own).strip(),
        ))
        pending_pre = carry

    missing = [i for i in items if not i.description]
    if missing:
        return ParseResult(
            items=items,
            confident=False,
            reason=f"{len(missing)} amount(s) had no description",
        )

    return ParseResult(items=items, confident=True, reason="ok")


# Quick check: `python -m tools.money_parser "spent 2k on uber and 500 on daal"`
if __name__ == "__main__":
    import sys

    samples = sys.argv[1:] or [
        "spent 2k on uber to the office, 1200 on the k-electric bill, "
        "like 3000 on dinner with friends and 450 on a grocery run",
        "my salary came in today, 150k. also sold my old iphone for 20 "
        "thousand and got a 3500 dividend from meezan",
        "got 45k from a freelance client yesterday but also spent 8k on "
        "groceries and 2500 on petrol",
    ]
    for s in samples:
        r = parse_money_text(s)
        print(f"\n{s}\n  confident={r.confident} ({r.reason})")
        for i in r.items:
            print(f"    {i.kind:8} {i.amount:>10,.0f}  {i.description}")
