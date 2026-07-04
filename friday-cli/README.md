# F.R.I.D.A.Y

A personal AI assistant you run from your own terminal. The LLM is a free
cloud API (OpenRouter, Groq, Gemini, or any OpenAI-compatible endpoint);
all the real work on your computer is done by plain Python functions the
LLM is allowed to call.

## Features

* **Organize files** — sort any folder by file type or by date.
* **Clean junk** — scan temp dirs and browser caches; trashes (never deletes).
* **Reminders** — natural-language times (`"tomorrow 5pm"`, `"in 3 hours"`).
* **Voice mode** (optional) — local Whisper for STT, `pyttsx3` for TTS.

## Setup

```bash
cd friday-cli
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env               # fill in OPENAI_API_KEY, OPENAI_BASE_URL, FRIDAY_MODEL
```

`-e` = editable mode. Code changes take effect immediately — no reinstall.

## Usage

```bash
friday                                  # interactive REPL
friday "organize my Downloads folder"   # one-shot
friday --voice                          # talk to it (requires `pip install -e ".[voice]"`)
friday --help
```

## Configuring the model

Everything is in `.env`:

```ini
OPENAI_API_KEY=sk-or-v1-...
OPENAI_BASE_URL=https://openrouter.ai/api/v1
FRIDAY_MODEL=meta-llama/llama-3.1-70b-instruct
```

Swap providers any time by changing those three lines. The model name is
never hardcoded inside the code.

## Project layout

```
friday-cli/
├── main.py                CLI entry (typer + rich)
├── pyproject.toml         packaging + the `friday` script entry point
├── config.py              loads provider/model/base_url from .env
├── core/
│   ├── brain.py           openai SDK + tool-calling loop
│   ├── persona.py         F.R.I.D.A.Y system prompt
│   ├── voice.py           optional STT (faster-whisper / Groq) + TTS (pyttsx3)
│   └── logger.py          append-only logs/actions.log
├── tools/
│   ├── file_organizer.py  organize_files(directory, mode)
│   ├── junk_cleaner.py    clean_junk(directory=None, dry_run=True)
│   ├── reminders.py       add_reminder / list_reminders / complete_reminder
│   ├── check_reminders.py CLI notifier: python -m tools.check_reminders
│   └── _db.py             sqlite init
├── data/tasks.db          auto-created on first run
└── logs/actions.log       auto-created on first run
```

## Safety rules (baked in)

1. **Trash, never delete.** `clean_junk` uses `send2trash` — recoverable.
2. **Dry run first.** `clean_junk(dry_run=True)` returns a preview before
   anything is touched. The system prompt tells the LLM to always do this.
3. **Everything is logged.** Every tool call appends to `logs/actions.log`.

## Background notifications (reminders when the CLI is closed)

The CLI's startup banner is nice, but you also want a popup when you're
not running `friday`. Use `python -m tools.check_reminders` with your
OS's scheduler.

### Windows — Task Scheduler

1. Open **Task Scheduler** → **Create Task…** (not "Basic Task").
2. **General** tab: name it `F.R.I.D.A.Y reminders`, check
   "Run whether user is logged on or not".
3. **Triggers** tab → **New…** → Daily, repeat every 5 minutes for 1 day.
4. **Actions** tab → **New…**:
   * Program/script: `C:\path\to\friday-cli\.venv\Scripts\python.exe`
   * Add arguments: `-m tools.check_reminders`
   * Start in: `C:\path\to\friday-cli`
5. **Conditions** tab: uncheck "Start only if on AC power".
6. Save (it'll ask for your Windows password).

### macOS / Linux — cron

Edit your crontab:

```bash
crontab -e
```

Add this line (runs every 5 minutes):

```
*/5 * * * * cd /path/to/friday-cli && .venv/bin/python -m tools.check_reminders
```

> On macOS, the system needs to grant your terminal/cron agent permission
> to send notifications. The first time `python -m tools.check_reminders`
> runs, macOS will pop up a permission prompt.

## Optional: voice mode

```bash
pip install -e ".[voice]"
friday --voice
```

This pulls in `faster-whisper` (local Whisper), `sounddevice` (mic),
`pyttsx3` (TTS). If you'd rather use Groq's free cloud Whisper instead of
downloading a Whisper model, also `pip install -e ".[groq-stt]"` and set
`GROQ_API_KEY` in `.env` — `core/voice.py` will auto-detect it.

## License

MIT.
