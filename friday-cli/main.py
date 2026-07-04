"""F.R.I.D.A.Y CLI entry point.

Usage:
    friday                  -> interactive chat loop
    friday "do the thing"   -> one-shot, prints the reply and exits
    friday --voice "..."    -> enable voice mode (STT + TTS)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.spinner import Spinner

# Make `from config import …` work no matter where friday is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings                       # noqa: E402
from core.brain import chat                       # noqa: E402
from tools.check_reminders import format_due, get_due  # noqa: E402

app = typer.Typer(add_completion=False, help="F.R.I.D.A.Y — your terminal AI assistant.")
console = Console()


def _preflight() -> None:
    """Surface setup mistakes and any due reminders before the first prompt."""
    if not settings.has_key:
        console.print(Panel.fit(
            "[bold red]No API key set.[/bold red]\n\n"
            "Copy [cyan].env.example[/cyan] to [cyan].env[/cyan] and fill in\n"
            "OPENAI_API_KEY, OPENAI_BASE_URL, and FRIDAY_MODEL.",
            title="F.R.I.D.A.Y",
        ))
        raise typer.Exit(1)
    due = get_due()
    if due:
        console.print(Panel(format_due(due), style="yellow"))


def _render_reply(text: str, voice: bool = False) -> None:
    """Render the assistant's reply. Optionally speak it out loud."""
    if not text.strip():
        text = "(no reply)"
    console.print(Panel(Markdown(text), title="F.R.I.D.A.Y", border_style="cyan"))
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

    with Live(Spinner("dots", text="[cyan]friday is thinking…[/cyan]"),
              transient=True, console=console):
        reply, _ = chat(list(history))
    _render_reply(reply, voice=voice_out)
    return reply


# ---------- CLI entry point ----------

@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    message: Optional[str] = typer.Argument(
        None, help="Chat with friday interactively, or pass a one-shot message.",
    ),
    voice: bool = typer.Option(
        False, "--voice", "-V",
        help="Enable voice mode: STT for input, TTS for output.",
    ),
) -> None:
    _preflight()

    if message is not None:
        history: list[dict] = [{"role": "user", "content": message}]
        _run_turn(history, voice_in=voice, voice_out=voice)
        return

    console.print(Panel.fit(
        "[bold cyan]F.R.I.D.A.Y[/bold cyan] online. Type [b]/quit[/b] to exit, "
        "[b]/clear[/b] to reset memory.",
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
