"""Learned categorization: remember what things were called last time.

No LLM involved. This is the layer that makes the model progressively
unnecessary.

The observation this is built on: spending is repetitive. The same shops,
the same bills, the same rides, month after month. Asking a language model
to categorize "uber to the office" is reasonable the first time and pure
waste the fiftieth. So every categorization that *does* happen — whether
the model made it or the user corrected it — is remembered here, and the
next identical or similar description is answered from memory in
microseconds.

Two kinds of memory, in ``rules``:

    phrase  a whole normalized description  -> category
            "uber to the office" -> Transport
            Matched exactly, then fuzzily (so "uber to office" hits too).

    token   a single word -> category
            "uber" -> Transport
            Only trusted once the same word has been confirmed at least
            twice with one consistent category. That threshold is what
            stops a one-off "office" in "uber to the office" from later
            dragging "office supplies" into Transport.

Nothing is seeded. The vocabulary is entirely the user's own, which is the
point — they never wanted to define categories, and this way they never
have to, while still not paying for the same decision twice.
"""
from __future__ import annotations

import difflib
import re
from typing import Optional

from ._db import get_conn, init_db
from .money_parser import _FILLER

init_db()

# A token must be confirmed this many times before it can decide a category
# on its own. Phrases are trusted immediately; single words are not.
_TOKEN_MIN_HITS = 2

# How close a remembered phrase must be to count as the same thing.
_PHRASE_CUTOFF = 0.82

# Words that carry no category signal.
_STOPWORDS = _FILLER | {
    "bill", "payment", "purchase", "run", "trip", "stuff", "things", "item",
    "items", "misc", "random", "small", "big", "few", "couple",
}


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation and digits, collapse whitespace."""
    s = re.sub(r"[^a-z\s]", " ", (text or "").lower())
    return " ".join(s.split())


def _tokens(text: str) -> list[str]:
    """Meaningful words from a description, in order, deduplicated."""
    seen: list[str] = []
    for word in _normalize(text).split():
        if len(word) > 2 and word not in _STOPWORDS and word not in seen:
            seen.append(word)
    return seen


def categorize(description: str, kind: str = "expense") -> Optional[tuple[str, str]]:
    """Look up a category from memory. Returns (category, how) or None.

    `how` is 'exact', 'similar' or 'keyword' — useful for explaining to the
    user why something landed where it did.
    """
    norm = _normalize(description)
    if not norm:
        return None

    with get_conn() as conn:
        # 1. This exact description has been categorized before.
        row = conn.execute(
            "SELECT category FROM rules WHERE scope = 'phrase' AND kind = ? "
            "AND pattern = ? ORDER BY hits DESC LIMIT 1",
            (kind, norm),
        ).fetchone()
        if row:
            return row["category"], "exact"

        # 2. Something close enough has been ("uber to office" vs "uber to the office").
        phrases = conn.execute(
            "SELECT pattern, category FROM rules WHERE scope = 'phrase' AND kind = ?",
            (kind,),
        ).fetchall()
        known = {r["pattern"]: r["category"] for r in phrases}
        match = difflib.get_close_matches(norm, list(known), n=1, cutoff=_PHRASE_CUTOFF)
        if match:
            return known[match[0]], "similar"

        # 3. A word in it is a reliable signal on its own.
        words = _tokens(norm)
        if not words:
            return None
        placeholders = ",".join("?" * len(words))
        rows = conn.execute(
            f"SELECT category, SUM(hits) AS score FROM rules "
            f"WHERE scope = 'token' AND kind = ? AND pattern IN ({placeholders}) "
            f"GROUP BY category ORDER BY score DESC",
            [kind, *words],
        ).fetchall()

    if not rows or rows[0]["score"] < _TOKEN_MIN_HITS:
        return None
    # Refuse to guess when two categories are equally supported.
    if len(rows) > 1 and rows[1]["score"] == rows[0]["score"]:
        return None
    return rows[0]["category"], "keyword"


def remember(description: str, category: str, kind: str = "expense",
             weight: int = 1) -> None:
    """Record that `description` was categorized as `category`.

    `weight` is how strongly to trust it — a user correction is worth more
    than a model's first guess.
    """
    norm = _normalize(description)
    if not norm or not (category or "").strip():
        return

    entries = [(norm, "phrase")] + [(w, "token") for w in _tokens(norm)]
    with get_conn() as conn:
        for pattern, scope in entries:
            conn.execute(
                "INSERT INTO rules (pattern, scope, kind, category, hits, created_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(pattern, scope, kind, category) "
                "DO UPDATE SET hits = hits + ?",
                (pattern, scope, kind, category, weight, weight),
            )
        conn.commit()


def forget(description: str, kind: str = "expense") -> None:
    """Drop what was learned from one description.

    Used when a categorization turns out to be wrong, so the mistake isn't
    reinforced every time a similar entry comes in.
    """
    norm = _normalize(description)
    if not norm:
        return
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM rules WHERE scope = 'phrase' AND kind = ? AND pattern = ?",
            (kind, norm),
        )
        for word in _tokens(norm):
            conn.execute(
                "UPDATE rules SET hits = hits - 1 "
                "WHERE scope = 'token' AND kind = ? AND pattern = ?",
                (kind, word),
            )
        conn.execute("DELETE FROM rules WHERE hits <= 0")
        conn.commit()


def stats() -> dict:
    """How much the categorizer has learned. Handy for 'is this working?'."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT scope, kind, COUNT(*) AS n FROM rules GROUP BY scope, kind"
        ).fetchall()
    return {f"{r['kind']}_{r['scope']}s": r["n"] for r in rows}
