# Supermarket Agent (Telegram + Gemini)

A Telegram bot for running a small supermarket: billing, stock, khata
(credit) ledger, daily summaries, and auto-generated PDF invoices /
PPTX sales decks — driven by a Google Gemini agent plus a tap-based
menu UI.

## What's in this cleaned build

Only the files that are actually wired together and used at runtime.
Everything below is imported, either directly or transitively, from
`app/main.py`:

```
app/main.py                  entry point — loads .env, starts the bot
telegram_bot/bot_gemini.py   Telegram <-> Gemini glue, scheduler, run()
telegram_bot/menus.py        tap-driven button UI (billing, stock, khata...)
agent/gemini_tools.py        Gemini tool declarations + dispatch
tools/store_tools.py         all business logic (billing, stock, khata, etc.)
tools/documents.py           PDF invoice + PPTX deck generation
database/database.py         sqlite connection helper (get_db, round2)
database/init_db.py          creates the tables on first run (see below)
tests/                       standalone test scripts, each builds its own
                              throwaway test database — not needed to run
                              the bot, but useful to verify logic changes
```

### Removed (dead / superseded code from earlier build phases)

These were fully commented-out, unused by anything, and not imported
by any live file — safe to delete:

- `telegram_bot/bot.py` — an older Claude-Agent-SDK version of the bot,
  replaced by `telegram_bot/bot_gemini.py`.
- `telegram_bot/forms.py` — an earlier draft of the tap-menu UI,
  replaced by `telegram_bot/menus.py`.
- `agent/agent_tools.py` — tool wrappers for the old Claude-Agent-SDK
  bot, replaced by `agent/gemini_tools.py`.
- `invoice/generator.py` — an earlier invoice generator, replaced by
  `tools/documents.py`.

### One real gap that was fixed

The database schema (tables `products`, `bills`, `bill_items`, `khata`,
`khata_transactions`, `preferences`) previously existed **only** inside
`tests/test_each_function.py` and `tests/test_concurrency.py`, each
building its own throwaway test database. There was no script to
create the real `database/supermarket.db` — the bot would have failed
on first run with "no such table". `database/init_db.py` was added to
fix that.

## Setup

1. Install system fonts used for PDF text rendering (Ubuntu/Debian):
   ```
   sudo apt-get install fonts-dejavu-core
   ```
2. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Fill in `.env` (already gitignored) with your own values:
   ```
   TELEGRAM_BOT_TOKEN=your-token-from-BotFather
   GEMINI_API_KEY=your-key-from-aistudio.google.com
   ```
4. Create the database (one-time, safe to re-run):
   ```
   python3 -m database.init_db
   ```
5. Run the bot:
   ```
   python3 -m app.main
   ```

## Tests (optional, don't touch your real data)

```
python3 -m tests.test_each_function
python3 -m tests.test_concurrency
python3 -m tests.test_tools
```
