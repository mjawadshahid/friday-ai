"""The `friday help` capability guide.

Kept out of main.py so the CLI file stays about wiring, and kept out of the
persona so showing it never costs a model call. This is the answer to
"what can I actually say to this thing?", which is the question a natural
language CLI is worst at answering on its own — there's no menu to look at.
"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# (what you type, what happens)
_LOGGING = [
    ('friday spent 2k on uber, 500 on gas', "Logs both, picks categories itself"),
    ('friday salary came in, 150k', "Logged as income, not spending"),
    ('friday sold my old phone for 20k', "Income too; it works out which"),
    ('friday spent 3k on dinner friday', "Backdated to Friday, not today"),
]

_VIEWING = [
    ('friday show me my expenses', "Month by month, then this month"),
    ('friday where did my money go', "Categories, this month"),
    ('friday how much did i save', "In vs out vs what's left"),
    ('friday my spending last month', "That month's categories"),
    ('friday list my transactions', "Line items, with ids for fixing"),
    ('friday show me this year by month', "12 month trend"),
    ('friday what did i earn this year', "Income, broken down"),
]

_FIXING = [
    ('friday expense 12 was 2500 not 250', "Corrects it; the fix is remembered"),
    ('friday delete expense 12', "Removes that entry"),
    ('friday merge Eating Out into Restaurants', "Folds one category into another"),
]

_OTHER = [
    ('friday organize my downloads', "Sorts files by type or date"),
    ('friday clean junk files', "Previews first, trashes rather than deletes"),
    ('friday remind me to call ali at 5pm', "Natural language reminders"),
    ('friday list my reminders', "What's pending"),
]


def _section(console: Console, title: str, rows: list[tuple[str, str]]) -> None:
    table = Table(show_header=False, box=None, padding=(0, 1, 0, 0))
    table.add_column(style="cyan", no_wrap=False)
    table.add_column(style="dim")
    for command, effect in rows:
        table.add_row(command, effect)
    console.print(f"\n[bold]{title}[/bold]")
    console.print(table)


def show_guide(console: Console | None = None) -> None:
    console = console or Console()
    console.print(Panel.fit(
        "[bold cyan]F.R.I.D.A.Y[/bold cyan] — talk to it in plain English.\n"
        "[dim]Quotes are optional: [/dim]friday i spent 3k on lunch",
        border_style="cyan",
    ))

    _section(console, "Logging money", _LOGGING)
    _section(console, "Seeing it", _VIEWING)
    _section(console, "Fixing mistakes", _FIXING)
    _section(console, "Everything else", _OTHER)

    console.print(
        "\n[bold]Worth knowing[/bold]\n"
        "[dim]You never define categories; they come from what you log, and\n"
        "near-duplicates merge on their own. It gets faster as it learns\n"
        "your habits, and a correction sticks harder than its own guess.\n"
        "Run [/dim]friday[dim] with no message for a chat session, or\n"
        "[/dim]friday --voice[dim] to talk to it.[/dim]\n"
    )
    console.print(
        "[dim]Shell tip: unquoted is fine, but quote anything containing\n"
        "[/dim]'[dim], [/dim]&[dim], [/dim]|[dim], [/dim]>[dim] or [/dim]([dim] "
        "— those mean something to your shell.[/dim]\n"
    )
