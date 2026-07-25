"""Juno CLI entry point.

Usage:
    juno                  -> interactive chat loop
    juno "do the thing"   -> one-shot, prints the reply and exits
    juno --voice "..."    -> enable voice mode (STT + TTS)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.spinner import Spinner

# Make `from config import …` work no matter where juno is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings                       # noqa: E402
from core.brain import chat                       # noqa: E402
from core.fastpath import try_fast_path           # noqa: E402
from core.guide import show_guide                 # noqa: E402
from tools.check_reminders import format_due, get_due  # noqa: E402

app = typer.Typer(add_completion=False, help="Juno — your terminal AI assistant.")
console = Console()


def _preflight() -> None:
    """Surface setup mistakes and any due reminders before the first prompt."""
    if not settings.has_key:
        console.print(Panel.fit(
            "[bold red]No API key set.[/bold red]\n\n"
            "Copy [cyan].env.example[/cyan] to [cyan].env[/cyan] and fill in\n"
            "OPENAI_API_KEY, OPENAI_BASE_URL, and JUNO_MODEL.",
            title="Juno",
        ))
        raise typer.Exit(1)
    due = get_due()
    if due:
        console.print(Panel(format_due(due), style="yellow"))


def _render_reply(text: str, voice: bool = False) -> None:
    """Render the assistant's reply. Optionally speak it out loud."""
    if not text.strip():
        text = "(no reply)"
    console.print(Panel(Markdown(text), title="Juno", border_style="cyan"))
    if voice:
        try:
            from core.voice import speak
            speak(text)
        except Exception as e:  # never let TTS failure break the CLI
            console.print(f"[dim](voice failed: {e})[/dim]")


def _run_turn(history: list[dict], voice_in: bool, voice_out: bool) -> str:
    """Send history, handle the tool loop, return the final assistant text."""
    if voice_in:
        try:
            from core.voice import listen
            console.print("[dim]Listening…[/dim]")
            history.append({"role": "user", "content": listen()})
        except Exception as e:
            console.print(f"[red](voice input failed: {e})[/red]")

    # Logging money and asking for a summary don't need the tool loop —
    # they're handled locally in milliseconds. Only fall through to the
    # model when the message is something else. See core/fastpath.py.
    last = history[-1]["content"] if history else ""
    try:
        quick = try_fast_path(last)
    except Exception as e:  # never let the shortcut break the assistant
        console.print(f"[dim](fast path failed, asking the model: {e})[/dim]")
        quick = None

    if quick is not None:
        # The tool already printed its table; an empty reply means there is
        # genuinely nothing to add, so don't show an empty panel.
        if quick.strip():
            _render_reply(quick, voice=voice_out)
        return quick

    with Live(Spinner("dots", text="[cyan]juno is thinking…[/cyan]"),
              transient=True, console=console):
        reply, _ = chat(list(history))
    _render_reply(reply, voice=voice_out)
    return reply


# ---------- CLI entry point ----------

@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    # Variadic so quotes are optional: `juno i spent 3k on lunch` works
    # the same as `juno "i spent 3k on lunch"`. The words are joined back
    # into one message.
    words: Optional[List[str]] = typer.Argument(
        None, help="Chat with juno interactively, or pass a one-shot message.",
    ),
    voice: bool = typer.Option(
        False, "--voice", "-V",
        help="Enable voice mode: STT for input, TTS for output.",
    ),
) -> None:
    message = " ".join(words).strip() if words else ""

    # `juno help` prints the capability guide. Handled before preflight so
    # it works even when no API key is set — a brand new user asking what
    # this thing does shouldn't be told off about configuration first.
    if message.lower().strip("?") in {"help", "commands", "what can you do",
                                      "what can i do", ""} and message:
        show_guide(console)
        return

    _preflight()

    if message:
        history: list[dict] = [{"role": "user", "content": message}]
        _run_turn(history, voice_in=voice, voice_out=voice)
        return

    console.print(Panel.fit(
        "[bold cyan]Juno[/bold cyan] online. Type [b]/help[/b] for what "
        "you can say,\n[b]/clear[/b] to reset memory, [b]/quit[/b] to exit.",
        border_style="cyan",
    ))
    history: list[dict] = []
    while True:
        try:
            user_input = Prompt.ask("[bold green]you[/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            raise typer.Exit(0)
        if not user_input:
            continue
        if user_input.lower() in {"/quit", "/exit", "exit", "quit"}:
            console.print("[dim]Goodbye.[/dim]")
            return
        if user_input.lower() == "/clear":
            history = []
            console.print("[dim]Memory cleared.[/dim]")
            continue
        if user_input.lower() in {"/help", "help", "/?"}:
            show_guide(console)
            continue

        if voice and not user_input:
            try:
                from core.voice import listen
                user_input = listen()
            except Exception as e:
                console.print(f"[red](voice input failed: {e})[/red]")
                continue

        history.append({"role": "user", "content": user_input})
        reply = _run_turn(history, voice_in=False, voice_out=voice)
        history.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    app()
