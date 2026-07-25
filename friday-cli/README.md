# F.R.I.D.A.Y

A personal AI assistant you run from your own terminal. The LLM is a free
cloud API (OpenRouter, Groq, Gemini, or any OpenAI-compatible endpoint);
all the real work on your computer is done by plain Python functions the
LLM is allowed to call.

## Features

* **Organize files** — sort any folder by file type or by date.
* **Clean junk** — scan temp dirs and browser caches; trashes (never deletes).
* **Reminders** — natural-language times (`"tomorrow 5pm"`, `"in 3 hours"`).
* **Money tracking** — log spending *and* income in plain English; categories
  are assigned automatically. See below.
* **Voice mode** (optional) — local Whisper for STT, `pyttsx3` for TTS.

## Money tracking

Talk at it. One messy sentence, however many amounts:

```bash
friday "spent 2k on uber, 1200 on the k-electric bill and like 3000 on dinner"
friday "salary came in, 150k. also sold my old iphone for 20 thousand"
friday "where did my money go this month"
friday "how much did I save this month"
```

### How little of this actually uses the model

Money messages skip the tool-calling loop entirely. Amounts are found by
regex, categories come from what you've logged before, and every table is
printed straight from the database. Measured on an M4 with `qwen3.5:4b`:

| Message | Model calls | Time |
|---|---|---|
| `where did my money go this month` | 0 | ~0.4s |
| `spent 2k on uber` (seen before) | 0 | ~0.4s |
| `spent 2k on uber` (brand new) | 1 short one | ~1.7s |
| `organize my downloads` | full tool loop | seconds |

The only thing still worth a model is naming a category for a description
it has never seen, and even that is one short completion rather than the
full loop with 13 tool schemas attached. Because spending repeats, that
case gets rarer the longer you use it.

Three layers, in order:

1. `tools/money_parser.py` — splits text into amounts and descriptions.
   Reports when it isn't sure instead of guessing.
2. `tools/categorizer.py` — remembers every categorization. A whole
   description is trusted immediately; a single word has to be confirmed
   twice before it can decide anything on its own.
3. `core/fastpath.py` — routes the message, and refuses anything that
   isn't plainly about money so a reminder can't become an expense.

Anything these can't answer falls through to the normal LLM path, so
nothing is lost — it's just not the default anymore.

**You never define categories.** The model reads each item and picks one.
To stop that drifting into `Groceries` / `grocery` / `Food & Groceries` as
three separate buckets, every category is normalized on write against the
ones you already have — exact match, then word-family prefix
(`transport` → `transportation`), then fuzzy match. The vocabulary grows
out of your own spending and then stays put. If two do drift apart:

```bash
friday "merge Eating Out into Restaurants"
```

Money in and money out are stored in one table separated by `kind`, with
amounts always positive — the kind carries the sign. Their category
vocabularies are separate namespaces, so an income category `Sale` can
never merge into an expense category `Sales Tax`.

Every money tool prints its own table to the terminal, and the system
prompt forbids the assistant from restating figures it didn't read from a
tool result — **the numbers you see are always straight from the database,
never the model's arithmetic.**

Set your currency label in `.env` (display only, no conversion):

```ini
FRIDAY_CURRENCY=PKR
```

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

### Running fully local (Ollama)

```ini
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
FRIDAY_MODEL=qwen3.5:9b
```

**Pick the model for tool calling, not for size.** Everything F.R.I.D.A.Y
does goes through function calls, so a model that's merely good at chatting
is useless here. Tested on an M4 / 16 GB:

| Model | Result |
|---|---|
| `llama3.1:8b` | ✗ Prints the tool call as chat text instead of calling it. Nothing gets logged. |
| `qwen3.5:4b` | ✓ Recommended. 3.4 GB, fast, and correct on everything the money tools need. |

The Qwen3.5 MoE (`35b-a3b`) is the nicer architecture — only ~3B active
params — but 35B total is ~21 GB and won't fit in 16 GB. It's the one to
use if you have 32 GB+.

Two things make a small model safe here. The money tools render their own
tables and the assistant is forbidden from restating figures, so a weaker
model produces *worse commentary* but never wrong stored data. And most
money messages never reach the model at all (see above).

Qwen3.5 is a reasoning model: on a short question it will spend its whole
token budget thinking and return an empty string. `core/brain.py` passes
`reasoning_effort="none"` for the short calls, and retries without it for
providers that don't accept the parameter.

## Project layout

```
friday-cli/
├── main.py                CLI entry (typer + rich)
├── pyproject.toml         packaging + the `friday` script entry point
├── config.py              loads provider/model/base_url from .env
├── core/
│   ├── brain.py           openai SDK + tool-calling loop
│   ├── fastpath.py        answers money messages without the LLM
│   ├── persona.py         F.R.I.D.A.Y system prompt
│   ├── voice.py           optional STT (faster-whisper / Groq) + TTS (pyttsx3)
│   └── logger.py          append-only logs/actions.log
├── tools/
│   ├── file_organizer.py  organize_files(directory, mode)
│   ├── junk_cleaner.py    clean_junk(directory=None, dry_run=True)
│   ├── reminders.py       add_reminder / list_reminders / complete_reminder
│   ├── expenses.py        log_expenses / log_income / summaries / corrections
│   ├── money_parser.py    plain English -> amounts, no LLM
│   ├── categorizer.py     learned description -> category memory
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
