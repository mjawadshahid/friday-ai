"""Money in and out: logging, automatic categorization, and reporting.

Schema (SQLite, ``data/tasks.db``):
    expenses(id, amount, currency, category, description,
             spent_at, created_at, raw_text, kind)
    categories(name, fold, created_at, uses, kind)

Both directions of money live in one table, separated by ``kind``
('expense' or 'income'). Amounts are always stored positive; the kind
carries the sign. Category vocabularies are namespaced per kind, so an
income category called "Sale" can never fuzzy-merge into a spending
category called "Sales Tax".

Why there is no fixed category list
-----------------------------------
The user never defines categories. The model reads free-form English
("2k on uber, 500 groceries, 1200 for the electricity bill") and invents
whatever category fits each item.

Left alone, that drifts: "Groceries" today, "grocery" tomorrow,
"Food & Groceries" next week — three rows that should be one, and totals
that mean nothing. So every incoming category is funnelled through
``_canonical_category`` before it is written:

    1. exact match on the folded key      -> reuse the existing name
    2. close fuzzy match (difflib >= .84) -> reuse the existing name
    3. otherwise                          -> register it as a new category

The vocabulary therefore *emerges* from the user's actual spending and
then stays stable on its own. ``merge_categories`` is the escape hatch
for the cases where two names genuinely do drift apart.

Exposed to the LLM as eight tools:
    log_expenses, log_income, list_expenses, summarize_expenses,
    summarize_cashflow, correct_expense, delete_expense, merge_categories
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import dateparser
from pydantic import BaseModel, Field

from config import settings
from . import categorizer
from ._db import get_conn, init_db

# Make sure the tables exist on first import.
init_db()

KIND_EXPENSE = "expense"
KIND_INCOME = "income"

UNCATEGORIZED = "Uncategorized"
UNCATEGORIZED_INCOME = "Other Income"

# How similar two folded names must be to count as the same category.
# 0.84 merges grocery/groceries and uber/ubers, but keeps "Fuel" and "Food"
# apart (they score ~0.5). Raise it if unrelated categories start merging.
_MATCH_CUTOFF = 0.84

# Bar width in the terminal summary, in characters.
_BAR_WIDTH = 26


# ---------- helpers ----------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fold(name: str) -> str:
    """Normalize a category name into a match key.

    Lowercases, drops punctuation, expands '&' to 'and', and crudely
    singularizes the last word so 'Groceries' and 'grocery' collide.
    """
    s = (name or "").strip().lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    if not s:
        return ""
    words = s.split()
    last = words[-1]
    if last.endswith("ies") and len(last) > 4:
        last = last[:-3] + "y"
    elif last.endswith("s") and not last.endswith("ss") and len(last) > 3:
        last = last[:-1]
    words[-1] = last
    return " ".join(words)


def _titleize(name: str) -> str:
    """Present a model-supplied category consistently: 'uber rides' -> 'Uber Rides'."""
    cleaned = " ".join((name or "").strip().split())
    if not cleaned:
        return ""  # caller picks the right fallback for the kind
    # Leave names that already carry deliberate casing (e.g. "PSO", "iPhone").
    if any(c.isupper() for c in cleaned[1:]):
        return cleaned
    return cleaned.title()


def _fold_key(name: str, kind: str) -> str:
    """Namespaced match key: income and expense categories never collide."""
    base = _fold(name)
    return f"{kind}:{base}" if base else ""


def _lookup_category(conn, raw: str, kind: str = KIND_EXPENSE) -> Optional[str]:
    """Find the existing category matching `raw` within `kind`, or None.

    Never writes.
    """
    fold = _fold(raw)
    if not fold:
        return None

    # Match on the *base* fold, not the namespaced key — the shared 'expense:'
    # prefix would otherwise defeat both the length guard and difflib.
    rows = conn.execute("SELECT name, fold FROM categories WHERE kind = ?", (kind,)).fetchall()
    known = {r["fold"].split(":", 1)[-1]: r["name"] for r in rows}

    # 1. exact match on the folded key ("Groceries" == "grocery")
    if fold in known:
        return known[fold]

    # 2. same word family by prefix ("transport" -> "transportation").
    #    Min length 5 keeps short unrelated stems ("car"/"career") apart.
    for other_fold, name in known.items():
        short, long = sorted((fold, other_fold), key=len)
        if len(short) >= 5 and long.startswith(short):
            return name

    # 3. nearest existing category, if it's close enough
    match = difflib.get_close_matches(fold, list(known.keys()), n=1, cutoff=_MATCH_CUTOFF)
    return known[match[0]] if match else None


def _canonical_category(conn, raw: str, kind: str = KIND_EXPENSE) -> tuple[str, bool]:
    """Map a model-invented category onto the user's existing vocabulary.

    Registers the category if it's genuinely new. Returns (name, is_new).
    """
    name = _titleize(raw)
    if not name:
        name = UNCATEGORIZED_INCOME if kind == KIND_INCOME else UNCATEGORIZED

    existing = _lookup_category(conn, name, kind)
    if existing is not None:
        return existing, False

    conn.execute(
        "INSERT OR IGNORE INTO categories (name, fold, created_at, uses, kind) "
        "VALUES (?, ?, ?, 0, ?)",
        (name, _fold_key(name, kind), _now_iso(), kind),
    )
    return name, True


def _coerce_amount(value: Any) -> Optional[float]:
    """Accept 2000, '2000', '2,000', 'Rs 2000', '2k', '1.5k'. None if unparseable."""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value or "").strip().lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*([km])?", text)
    if not m:
        return None
    amount = float(m.group(1))
    if m.group(2) == "k":
        amount *= 1_000
    elif m.group(2) == "m":
        amount *= 1_000_000
    return amount if amount > 0 else None


def _parse_when(text: str) -> str:
    """Parse 'yesterday', 'last friday', '3 jan' into an ISO timestamp.

    Defaults to now — an expense with no stated date happened today.
    """
    if not text or not text.strip():
        return _now_iso()
    dt = dateparser.parse(text, settings={"PREFER_DATES_FROM": "past"})
    return (dt.replace(microsecond=0).isoformat() if dt else _now_iso())


def _period_range(period: str) -> tuple[Optional[str], Optional[str], str]:
    """Turn 'this month' / 'last week' / 'today' into (start_iso, end_iso, label).

    (None, None) means no time filter at all.
    """
    p = " ".join((period or "").strip().lower().split())
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def span(start: datetime, end: datetime, label: str):
        return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), label

    if not p or p in {"all", "all time", "alltime", "ever", "everything", "total"}:
        return None, None, "all time"
    if p == "today":
        return span(midnight, midnight + timedelta(days=1), "today")
    if p == "yesterday":
        return span(midnight - timedelta(days=1), midnight, "yesterday")
    if p in {"this week", "week", "current week"}:
        start = midnight - timedelta(days=midnight.weekday())
        return span(start, start + timedelta(days=7), "this week")
    if p == "last week":
        start = midnight - timedelta(days=midnight.weekday() + 7)
        return span(start, start + timedelta(days=7), "last week")
    if p in {"this month", "month", "current month"}:
        start = midnight.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        return span(start, end, "this month")
    if p == "last month":
        end = midnight.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        return span(start, end, "last month")
    if p in {"this year", "year", "current year"}:
        start = midnight.replace(month=1, day=1)
        return span(start, start.replace(year=start.year + 1), "this year")
    if p == "last year":
        end = midnight.replace(month=1, day=1)
        return span(end.replace(year=end.year - 1), end, "last year")

    # "last 7 days", "past 30 days"
    m = re.match(r"(?:last|past)\s+(\d+)\s+day", p)
    if m:
        days = int(m.group(1))
        return span(midnight - timedelta(days=days), midnight + timedelta(days=1), f"last {days} days")

    # Fall back to a specific date ("3 january", "jan 2024") -> that whole day.
    dt = dateparser.parse(p, settings={"PREFER_DATES_FROM": "past"})
    if dt:
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return span(start, start + timedelta(days=1), start.strftime("%d %b %Y"))

    # Unrecognized -> don't silently return the wrong window; use this month.
    start = midnight.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    return span(start, end, "this month")


def _where_period(period: str) -> tuple[str, list, str]:
    start, end, label = _period_range(period)
    if start is None:
        return "", [], label
    return "spent_at >= ? AND spent_at < ?", [start, end], label


def _money(amount: float, currency: str = "") -> str:
    cur = currency or settings.currency
    body = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    return f"{cur} {body}".strip()


# ---------- terminal rendering ----------
# The tool result goes to the model, but a table of numbers is something the
# *human* should see rendered, not paraphrased by an LLM. So these print
# directly to the terminal and the model is told (in persona.py) not to
# repeat what's already on screen.

def _console():
    from rich.console import Console
    return Console()


def _render_summary(title: str, totals: list[dict], grand: float, currency: str,
                    bucket_label: str = "Category") -> None:
    from rich.table import Table

    table = Table(title=title, title_style="bold cyan", header_style="bold")
    table.add_column(bucket_label)
    table.add_column("Amount", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("")

    top = max((t["total"] for t in totals), default=0) or 1
    for t in totals:
        share = (t["total"] / grand * 100) if grand else 0
        bar = "█" * max(1, round(t["total"] / top * _BAR_WIDTH))
        table.add_row(t["category"], _money(t["total"], currency), f"{share:.0f}%", f"[cyan]{bar}[/cyan]")

    table.add_section()
    table.add_row("[bold]Total[/bold]", f"[bold]{_money(grand, currency)}[/bold]", "", "")
    _console().print(table)


def _render_rows(label: str, rows: list[dict], currency: str,
                 kind: str = KIND_EXPENSE) -> None:
    from rich.table import Table

    income = kind == KIND_INCOME
    title = f"{'Income' if income else 'Expenses'} — {label}"
    table = Table(title=title, title_style="bold cyan", header_style="bold")
    table.add_column("id", justify="right", style="dim")
    table.add_column("Date")
    table.add_column("Category")
    table.add_column("What")
    table.add_column("Amount", justify="right")

    style = "green" if income else ""
    for r in rows:
        date = (r["spent_at"] or "")[:10]
        amount = _money(r["amount"], r["currency"] or currency)
        table.add_row(str(r["id"]), date, r["category"], r["description"] or "—",
                      f"[{style}]{amount}[/{style}]" if style else amount)
    _console().print(table)


def _render_logged(logged: list[dict], kind: str, currency: str) -> None:
    """Show exactly what hit the database, straight after writing it.

    The model is not trusted to report these numbers back — a small local
    model will happily invent a total. The user sees ground truth here and
    the assistant's prose is just commentary on top.
    """
    from rich.table import Table

    income = kind == KIND_INCOME
    table = Table(title=f"Logged {'income' if income else 'expenses'}",
                  title_style="bold cyan", header_style="bold")
    table.add_column("id", justify="right", style="dim")
    table.add_column("Category")
    table.add_column("What")
    table.add_column("Amount", justify="right")

    tone = "green" if income else ""
    for e in logged:
        amount = _money(e["amount"], e["currency"] or currency)
        table.add_row(str(e["id"]), e["category"], e["description"] or "—",
                      f"[{tone}]{amount}[/{tone}]" if tone else amount)

    table.add_section()
    total = _money(sum(e["amount"] for e in logged), currency)
    table.add_row("", "[bold]Total[/bold]", "", f"[bold]{total}[/bold]")
    _console().print(table)


def _render_cashflow(label: str, income: float, spent: float, net: float,
                     currency: str) -> None:
    from rich.table import Table

    table = Table(title=f"Cash flow — {label}", title_style="bold cyan", header_style="bold")
    table.add_column("")
    table.add_column("Amount", justify="right")
    table.add_column("")

    width = _BAR_WIDTH
    top = max(income, spent) or 1
    table.add_row("[green]In[/green]", f"[green]{_money(income, currency)}[/green]",
                  f"[green]{'█' * max(1, round(income / top * width))}[/green]")
    table.add_row("[red]Out[/red]", f"[red]{_money(spent, currency)}[/red]",
                  f"[red]{'█' * max(1, round(spent / top * width))}[/red]")
    table.add_section()

    tone = "green" if net >= 0 else "red"
    label_net = "Left over" if net >= 0 else "Overspent by"
    table.add_row(f"[bold]{label_net}[/bold]",
                  f"[bold {tone}]{_money(abs(net), currency)}[/bold {tone}]", "")
    _console().print(table)


# ---------- public tool functions ----------

def _log_entries(items: Any, when: str, kind: str) -> dict:
    """Shared writer for both log_expenses and log_income."""
    noun = "income entries" if kind == KIND_INCOME else "expenses"
    if not items:
        return {"error": f"No {noun} to log.", "logged": [], "count": 0}
    if isinstance(items, dict):  # tolerate a single item sent unwrapped
        items = [items]
    if not isinstance(items, list):
        return {"error": "`items` must be a list of objects.", "logged": [], "count": 0}

    default_at = _parse_when(when)
    logged: list[dict] = []
    rejected: list[dict] = []
    new_categories: list[str] = []
    to_learn: list[tuple[str, str]] = []

    with get_conn() as conn:
        for item in items:
            if not isinstance(item, dict):
                rejected.append({"item": str(item), "reason": "not an object"})
                continue

            amount = _coerce_amount(item.get("amount"))
            if amount is None:
                rejected.append({
                    "item": str(item.get("description") or item.get("amount") or item),
                    "reason": "could not read an amount",
                })
                continue

            category, is_new = _canonical_category(conn, str(item.get("category") or ""), kind)
            if is_new:
                new_categories.append(category)

            description = " ".join(str(item.get("description") or "").split())
            currency = str(item.get("currency") or "").strip() or settings.currency
            spent_at = _parse_when(str(item.get("when") or "")) if item.get("when") else default_at

            cur = conn.execute(
                "INSERT INTO expenses (amount, currency, category, description, "
                "spent_at, created_at, raw_text, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (amount, currency, category, description, spent_at, _now_iso(),
                 str(item.get("raw_text") or ""), kind),
            )
            conn.execute("UPDATE categories SET uses = uses + 1 WHERE fold = ?",
                         (_fold_key(category, kind),))
            if description:
                to_learn.append((description, category))
            logged.append({
                "id": cur.lastrowid,
                "amount": amount,
                "currency": currency,
                "category": category,
                "description": description,
                "spent_at": spent_at,
                "kind": kind,
            })
        conn.commit()

        known = [r["name"] for r in conn.execute(
            "SELECT name FROM categories WHERE kind = ? ORDER BY uses DESC, name ASC", (kind,)
        ).fetchall()]

    # Learn only after the write transaction has closed — the categorizer
    # opens its own connection, and SQLite will lock if it does so while
    # this one still holds the write.
    for description, category in to_learn:
        categorizer.remember(description, category, kind)

    if logged:
        _render_logged(logged, kind, settings.currency)
    # Shown even when nothing was logged — a silently dropped item is how a
    # tracker quietly loses your money.
    for r in rejected:
        _console().print(f"[yellow]skipped:[/yellow] {r['item']} — {r['reason']}")

    return {
        "count": len(logged),
        "_rendered": bool(logged),
        "kind": kind,
        "total": sum(e["amount"] for e in logged),
        "logged": logged,
        "rejected": rejected,
        "new_categories": new_categories,
        # Sent back so the model reuses this exact vocabulary next time
        # instead of inventing a near-duplicate.
        "known_categories": known,
    }


def log_expenses(items: list[dict], when: str = "") -> dict:
    """Log money going out. `items` is already-parsed structured data."""
    return _log_entries(items, when, KIND_EXPENSE)


def log_income(items: list[dict], when: str = "") -> dict:
    """Log money coming in — salary, freelance payment, dividend, a sale."""
    return _log_entries(items, when, KIND_INCOME)


def list_expenses(period: str = "this month", category: str = "",
                  limit: int = 50, kind: str = KIND_EXPENSE) -> dict:
    """List individual entries for a period, newest first."""
    kind = KIND_INCOME if str(kind).lower().startswith("in") else KIND_EXPENSE
    clause, params, label = _where_period(period)
    wheres = ["kind = ?"]
    params = [kind] + params
    if clause:
        wheres.append(clause)

    if category.strip():
        with get_conn() as conn:
            canonical = _lookup_category(conn, category, kind)
        if canonical is None:  # asking about a category that doesn't exist yet
            return {"count": 0, "period": label, "category": category, "kind": kind,
                    "expenses": [], "note": f"No {kind} category matching {category!r}."}
        wheres.append("category = ?")
        params.append(canonical)

    sql = ("SELECT id, amount, currency, category, description, spent_at, kind "
           "FROM expenses WHERE " + " AND ".join(wheres) +
           " ORDER BY spent_at DESC, id DESC LIMIT ?")
    params.append(max(1, min(int(limit or 50), 500)))

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if rows:
        _render_rows(label, rows, settings.currency, kind)

    return {
        "count": len(rows),
        "period": label,
        "kind": kind,
        "total": sum(r["amount"] for r in rows),
        "expenses": rows,
        "_rendered": bool(rows),
    }


def summarize_expenses(period: str = "this month", group_by: str = "category",
                       kind: str = KIND_EXPENSE) -> dict:
    """Totals for a period, broken down by category (or day/month)."""
    kind = KIND_INCOME if str(kind).lower().startswith("in") else KIND_EXPENSE
    clause, params, label = _where_period(period)
    where = " WHERE kind = ?" + (f" AND {clause}" if clause else "")
    params = [kind] + params

    # spent_at is stored as ISO 8601, so a substring is a valid date bucket:
    # chars 1-7 are YYYY-MM, 1-10 are YYYY-MM-DD, 1-4 are the year.
    key = {"category": "category", "day": "substr(spent_at, 1, 10)",
           "month": "substr(spent_at, 1, 7)",
           "year": "substr(spent_at, 1, 4)"}.get(group_by, "category")
    bucket_label = {"day": "Day", "month": "Month",
                    "year": "Year"}.get(group_by, "Category")

    # Categories rank by size; time buckets read chronologically, because a
    # month-by-month trend is meaningless out of order.
    order = "bucket ASC" if group_by in {"day", "month", "year"} else "total DESC"

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {key} AS bucket, SUM(amount) AS total, COUNT(*) AS n "
            f"FROM expenses{where} GROUP BY bucket ORDER BY {order}",
            params,
        ).fetchall()

    totals = [{"category": r["bucket"], "total": r["total"], "count": r["n"]} for r in rows]
    grand = sum(t["total"] for t in totals)
    noun = "Income" if kind == KIND_INCOME else "Spending"

    if not totals:
        return {"period": label, "kind": kind, "total": 0, "breakdown": [],
                "note": f"No {kind} logged for {label}."}

    _render_summary(f"{noun} — {label}", totals, grand, settings.currency, bucket_label)

    top = max(totals, key=lambda t: t["total"])
    return {
        "period": label,
        "kind": kind,
        "total": grand,
        "currency": settings.currency,
        "breakdown": totals,
        "biggest": {"category": top["category"], "total": top["total"],
                    "share_pct": round(top["total"] / grand * 100) if grand else 0},
        "_rendered": True,
    }


def summarize_cashflow(period: str = "this month") -> dict:
    """Income vs spending vs what's left over for a period."""
    clause, params, label = _where_period(period)
    where = f" WHERE {clause}" if clause else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT kind, SUM(amount) AS total, COUNT(*) AS n FROM expenses{where} "
            f"GROUP BY kind", params,
        ).fetchall()

    by_kind = {r["kind"]: {"total": r["total"], "count": r["n"]} for r in rows}
    income = by_kind.get(KIND_INCOME, {}).get("total", 0) or 0
    spent = by_kind.get(KIND_EXPENSE, {}).get("total", 0) or 0
    net = income - spent

    if not rows:
        return {"period": label, "income": 0, "spent": 0, "net": 0,
                "note": f"Nothing logged for {label}."}

    _render_cashflow(label, income, spent, net, settings.currency)

    return {
        "period": label,
        "currency": settings.currency,
        "income": income,
        "spent": spent,
        "net": net,
        "saved_pct": round(net / income * 100) if income else None,
        "_rendered": True,
    }


def correct_expense(expense_id: int, amount: float = None, category: str = "",
                    description: str = "") -> dict:
    """Fix a logged expense — wrong amount, wrong category, or wrong description."""
    sets: list[str] = []
    params: list[Any] = []

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if row is None:
            return {"status": "not_found", "id": expense_id}

        if amount is not None:
            value = _coerce_amount(amount)
            if value is None:
                return {"status": "error", "id": expense_id, "error": f"Bad amount: {amount!r}"}
            sets.append("amount = ?")
            params.append(value)
        if category.strip():
            # Categorize within the entry's own kind — recategorizing an
            # income row must not pull in a spending category.
            canonical, _ = _canonical_category(conn, category, row["kind"])
            sets.append("category = ?")
            params.append(canonical)
        if description.strip():
            sets.append("description = ?")
            params.append(" ".join(description.split()))

        if not sets:
            return {"status": "error", "id": expense_id, "error": "Nothing to change."}

        params.append(expense_id)
        conn.execute(f"UPDATE expenses SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
        updated = dict(conn.execute(
            "SELECT id, amount, currency, category, description, spent_at "
            "FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone())

    # A correction is the strongest signal available: the user personally
    # said this was wrong. Unlearn the old pairing and weight the new one
    # heavily, so the same mistake isn't repeated on the next similar entry.
    if category.strip():
        categorizer.forget(row["description"], row["kind"])
        categorizer.remember(updated["description"], updated["category"],
                             row["kind"], weight=5)

    return {"status": "updated", "before": {k: row[k] for k in ("amount", "category", "description")},
            "after": updated}


def delete_expense(expense_id: int) -> dict:
    """Delete a logged expense by id."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT amount, currency, category, description FROM expenses WHERE id = ?",
            (expense_id,),
        ).fetchone()
        if row is None:
            return {"status": "not_found", "id": expense_id}
        conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        conn.commit()
    return {"status": "deleted", "id": expense_id, "was": dict(row)}


def merge_categories(source: str, target: str, kind: str = KIND_EXPENSE) -> dict:
    """Fold every entry in `source` into `target` and drop `source`.

    The escape hatch for when auto-categorization does drift — e.g.
    'Eating Out' and 'Restaurants' both took root before the fuzzy match
    could catch them.
    """
    kind = KIND_INCOME if str(kind).lower().startswith("in") else KIND_EXPENSE
    src_fold, tgt_fold = _fold_key(source, kind), _fold_key(target, kind)
    if not src_fold or not tgt_fold:
        return {"status": "error", "error": "Both source and target are required."}
    if src_fold == tgt_fold:
        return {"status": "error", "error": "Source and target are the same category."}

    with get_conn() as conn:
        src = conn.execute("SELECT name FROM categories WHERE fold = ?", (src_fold,)).fetchone()
        tgt = conn.execute("SELECT name FROM categories WHERE fold = ?", (tgt_fold,)).fetchone()
        if src is None:
            return {"status": "not_found", "error": f"No {kind} category named {source!r}."}
        if tgt is None:
            return {"status": "not_found", "error": f"No {kind} category named {target!r}."}

        cur = conn.execute("UPDATE expenses SET category = ? WHERE category = ? AND kind = ?",
                           (tgt["name"], src["name"], kind))
        conn.execute("DELETE FROM categories WHERE fold = ?", (src_fold,))
        conn.execute("UPDATE categories SET uses = uses + ? WHERE fold = ?",
                     (cur.rowcount, tgt_fold))
        conn.commit()

    return {"status": "merged", "moved": cur.rowcount, "kind": kind,
            "from": src["name"], "into": tgt["name"]}


# ---------- OpenAI tool descriptors ----------

# NOTE: log_expenses' schema is hand-written rather than generated from a
# nested pydantic model. Pydantic emits $defs/$ref for nested models, and
# smaller/local models handle inline schemas far more reliably. Validation
# still happens inside the function.
_LOG_EXPENSES_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "One entry per distinct expense mentioned.",
            "items": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Numeric amount, e.g. 2000 for '2k'."},
                    "description": {"type": "string", "description": "What it was spent on, a few words."},
                    "category": {"type": "string", "description": "Category you judge best. Invent one if needed."},
                    "currency": {"type": "string", "description": "Only if the user named one explicitly."},
                    "when": {"type": "string", "description": "Only if this item has its own date, e.g. 'yesterday'."},
                },
                "required": ["amount", "description", "category"],
            },
        },
        "when": {
            "type": "string",
            "description": "Date for the whole batch, e.g. 'yesterday'. Omit for today.",
        },
    },
    "required": ["items"],
}


def _income_schema() -> dict:
    """Same shape as _LOG_EXPENSES_SCHEMA, worded for money coming in."""
    schema = json.loads(json.dumps(_LOG_EXPENSES_SCHEMA))  # deep copy
    props = schema["properties"]["items"]["items"]["properties"]
    props["amount"]["description"] = "Numeric amount received, e.g. 150000."
    props["description"]["description"] = "Where the money came from, a few words."
    props["category"]["description"] = (
        "Income category you judge best, e.g. Salary, Freelance, Dividends, Sale."
    )
    schema["properties"]["items"]["description"] = "One entry per distinct payment received."
    return schema


class ListExpensesArgs(BaseModel):
    period: str = Field("this month", description="'today', 'this week', 'last month', 'all time', etc.")
    category: str = Field("", description="Optional: only this category.")
    limit: int = Field(50, description="Max rows to return.")
    kind: str = Field("expense", description="'expense' (default) or 'income'.")


class SummarizeExpensesArgs(BaseModel):
    period: str = Field("this month", description="'today', 'this week', 'last month', 'all time', etc.")
    group_by: str = Field(
        "category",
        description="'category' (default), or 'day' / 'month' / 'year' for a trend over time.",
    )
    kind: str = Field("expense", description="'expense' (default) or 'income'.")


class SummarizeCashflowArgs(BaseModel):
    period: str = Field("this month", description="'this month', 'last month', 'this year', etc.")


class CorrectExpenseArgs(BaseModel):
    expense_id: int = Field(..., description="Numeric id of the expense to fix.")
    amount: Optional[float] = Field(None, description="New amount, if it was wrong.")
    category: str = Field("", description="New category, if it was wrong.")
    description: str = Field("", description="New description, if it was wrong.")


class DeleteExpenseArgs(BaseModel):
    expense_id: int = Field(..., description="Numeric id of the expense to delete.")


class MergeCategoriesArgs(BaseModel):
    source: str = Field(..., description="Category to absorb (this one disappears).")
    target: str = Field(..., description="Category to keep.")
    kind: str = Field("expense", description="'expense' (default) or 'income'.")


TOOL_SPECS = [
    {
        "name": "log_expenses",
        "description": (
            "Log one or more expenses the user just described in plain English. "
            "YOU do the parsing: split their message into separate items, read "
            "each amount ('2k' -> 2000), and assign each one a category you "
            "judge appropriate. Never ask the user which category to use — "
            "decide yourself. The response returns `known_categories`; reuse "
            "those exact names whenever one fits so totals stay consistent, and "
            "only invent a new name when nothing fits. Handles a whole messy "
            "paragraph of spending in a single call."
        ),
        "parameters": _LOG_EXPENSES_SCHEMA,
        "function": log_expenses,
    },
    {
        "name": "log_income",
        "description": (
            "Log money COMING IN — salary, freelance or client payment, a "
            "dividend, a refund, cash from selling something, a gift. Same "
            "rules as log_expenses: you split the message into items, read "
            "the amounts, and choose each category yourself. Use this instead "
            "of log_expenses whenever the user received money rather than "
            "spent it ('got my salary', 'sold my old phone for 20k', "
            "'dividend came in')."
        ),
        "parameters": _income_schema(),
        "function": log_income,
    },
    {
        "name": "list_expenses",
        "description": (
            "List individual logged entries for a period, newest first, "
            "optionally filtered to one category. Set kind='income' to list "
            "money received instead of money spent. Prints a table to the "
            "terminal itself. Use when the user wants to see the line items."
        ),
        "parameters": ListExpensesArgs.model_json_schema(),
        "function": list_expenses,
    },
    {
        "name": "summarize_expenses",
        "description": (
            "Totals for a period broken down by category, with a bar chart "
            "printed to the terminal. Use for 'where is my money going' or "
            "'how much did I spend this month'. Set kind='income' to break "
            "down earnings instead ('where is my money coming from')."
        ),
        "parameters": SummarizeExpensesArgs.model_json_schema(),
        "function": summarize_expenses,
    },
    {
        "name": "summarize_cashflow",
        "description": (
            "Income vs spending vs what's left over for a period, as a chart. "
            "Use when the user asks how much they saved, whether they're in "
            "the red, 'what's left this month', or wants the overall picture "
            "rather than just spending."
        ),
        "parameters": SummarizeCashflowArgs.model_json_schema(),
        "function": summarize_cashflow,
    },
    {
        "name": "correct_expense",
        "description": (
            "Fix an already-logged expense by id — wrong amount, wrong "
            "category, or wrong description. Only pass the fields that change. "
            "If the user doesn't know the id, call list_expenses first."
        ),
        "parameters": CorrectExpenseArgs.model_json_schema(),
        "function": correct_expense,
    },
    {
        "name": "delete_expense",
        "description": (
            "Delete a logged expense by its numeric id. Confirm with the user "
            "first if there's any doubt about which one they mean."
        ),
        "parameters": DeleteExpenseArgs.model_json_schema(),
        "function": delete_expense,
    },
    {
        "name": "merge_categories",
        "description": (
            "Merge one category into another — every entry moves to `target` "
            "and `source` is removed. Use when two categories mean the same "
            "thing, e.g. merge 'Eating Out' into 'Restaurants'. Pass "
            "kind='income' to merge income categories."
        ),
        "parameters": MergeCategoriesArgs.model_json_schema(),
        "function": merge_categories,
    },
]


# Quick smoke test: `python -m tools.expenses`
if __name__ == "__main__":
    print(json.dumps(log_expenses([
        {"amount": 2000, "description": "uber to office", "category": "Transport"},
        {"amount": 500, "description": "groceries", "category": "Groceries"},
        {"amount": 750, "description": "weekly grocery run", "category": "grocery"},
    ]), indent=2))
    print(json.dumps(log_income([
        {"amount": "150k", "description": "monthly salary", "category": "Salary"},
        {"amount": 20000, "description": "sold old phone", "category": "Sale"},
    ]), indent=2))
    print(json.dumps(summarize_cashflow("today"), indent=2))
