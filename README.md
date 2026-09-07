# Supermarket Agent (Telegram + Gemini)

A Telegram bot that runs the day-to-day operations of a small kirana
(grocery) store: billing, stock, khata (credit) ledger, daily
summaries, and auto-generated PDF invoices / PPTX sales decks — all
driven by a Google Gemini agent the owner talks to in plain English,
Hindi, or Tamil (text or voice notes), plus a tap-based menu for the
two flows that work better as a form than a chat.

---

## 1. Setup (Windows)

### Prerequisites

- Python 3.11+ ([python.org/downloads](https://www.python.org/downloads/) — check **"Add python.exe to PATH"** during install)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A free Gemini API key from [aistudio.google.com](https://aistudio.google.com)

### Steps

Open Command Prompt or PowerShell in the project folder.

**1. Create and activate a virtual environment**

```cmd
python -m venv venv
venv\Scripts\activate
```

If PowerShell blocks the activation script with an execution-policy error, run this once in PowerShell (as your normal user, not admin) and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**2. Install Python dependencies**

```cmd
pip install -r requirements.txt
```

**3. Install the font used for ₹ rendering in PDFs**

Windows already ships DejaVu-compatible fonts in most cases, but if invoices show a black box instead of ₹, download DejaVu Sans from [dejavu-fonts.github.io](https://dejavu-fonts.github.io/), then right-click the `.ttf` file → **Install**.

**4. Create a `.env` file in the project root** (use Notepad — save as "All Files", not `.txt`):

```
TELEGRAM_BOT_TOKEN=your-token-from-BotFather
GEMINI_API_KEY=your-key-from-aistudio.google.com

REM optional — weekly sales-deck job, defaults shown
WEEKLY_DECK_TIMEZONE=Asia/Kolkata
WEEKLY_DECK_DAY=mon
WEEKLY_DECK_HOUR=9
WEEKLY_DECK_MINUTE=0
```

**5. Create the database** (one-time, safe to re-run):

```cmd
python -m database.init_db
```

**6. Run the bot**

```cmd
python -m app.main
```

Runs in long-polling mode — no public URL, port forwarding, or webhook setup needed. Once running, open Telegram, message your bot, and it's live. Try things like:

- `50 packets of Maggi came in, cost 12`
- `make a bill: 2kg sugar, 1 atta, UPI`
- `what's low on stock?`
- `put ₹500 on Ramesh's credit`
- send a voice note instead of typing
- `/menu` for a button-based flow

### Optional: run the test scripts

These build their own throwaway SQLite databases and never touch `database/supermarket.db`:

```cmd
python -m tests.test_each_function   REM exercises every tool function
python -m tests.test_concurrency     REM races real concurrent threads against stock
python -m tests.test_tools           REM scripted end-to-end scenario
```

---

## 2. Design Outline

```
Telegram (text / voice / button tap)
        │
        ▼
telegram_bot/bot_gemini.py  ──┬──▶  telegram_bot/menus.py (button flows)
        │                     │            │
        ▼                     │            ▼
   Gemini API                 │      tools/store_tools.py
        │                     │            │
        ▼                     │            ▼
agent/gemini_tools.py ────────┴──▶  database/database.py (SQLite)
        │
        ▼
tools/documents.py ──▶ PDF invoice / PPTX deck
```

| Component | Responsibility |
|---|---|
| `app/main.py` | Entry point — loads `.env`, validates required keys, starts the bot |
| `telegram_bot/bot_gemini.py` | Telegram ↔ Gemini glue: the agent loop, session management, scheduler |
| `telegram_bot/menus.py` | Tap-based UI for adding products and building orders |
| `agent/gemini_tools.py` | Tool schema (JSON) + dispatch table connecting Gemini's tool calls to real functions |
| `tools/store_tools.py` | All business logic: billing, stock, khata, summaries |
| `tools/documents.py` | Generates PDF invoices and PPTX sales decks from live DB queries |
| `database/database.py` | Single connection helper — every write goes through here |
| `database/init_db.py` | Creates the SQLite schema on first run |
| `tests/` | Standalone scripts, each against its own throwaway test database |

**Request flow:**
1. Owner sends a message (text, voice, or button tap) via Telegram.
2. Text/voice goes to Gemini along with the tool schema; a button tap calls `store_tools.py` directly, skipping Gemini entirely.
3. Gemini decides which tool(s) to call → `agent/gemini_tools.py` dispatches into `store_tools.py` (reads/writes SQLite) or `documents.py` (generates a file).
4. Tool results are sent back to Gemini, which writes a natural-language reply.
5. The reply — and any generated file — is sent back to the owner on Telegram.

### Key design decisions

- **Hand-rolled Gemini agent loop, not an SDK.** Keeps tool dispatch synchronous, easy to log/inspect, and lets the bot inject the `finalize_bill` idempotency key server-side instead of trusting the model to generate one.
- **SQLite with `BEGIN IMMEDIATE`, not an ORM or hosted DB.** Every write takes a write lock the instant its transaction starts, preventing concurrent sales from corrupting stock. Simple and correct for one store's traffic, but would need a real server DB (e.g. Postgres) to scale to multiple stores.
- **Server-side idempotency key on `finalize_bill`.** Uses the Telegram `update_id`, never a value the model can see or choose — protects against double-charging stock on a retry.
- **One backend, two front ends.** Free-text/voice chat and the tap-based `/menu` both call the same `store_tools.py` functions; menus just skip the Gemini round-trip since a button tap is always unambiguous.
- **Documents are grounded in the database, never the model.** `tools/documents.py` queries the DB directly for every number in a PDF/PPTX — the model only decides *when* to generate one.
- **Trilingual support reinforced every turn**, not just at session start, so a mid-conversation language switch doesn't "stick" to whichever language dominated recent turns.
- **Voice notes go to Gemini natively** — no separate transcription step (trade-off: no cached transcript for later reference).
- **Single-tenant by design** — one `.env`, one SQLite file. A deliberate scope limit, not an oversight.

---
## 2. Bot

**Telegram Bot:** @super_market_kirana_bot — message this bot on Telegram to try the supermarket agent live.

## 3. Demo

**Loom walkthrough:** [https://www.loom.com/share/4fb7fc2e1cd3418d99a96d3148ddde45]

---

**4. Create a `.env` file in the project root** (use Notepad — save as "All Files", not `.txt`):

#### A. Getting your Telegram bot token
1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` (or `/mybots` → select an existing bot → **API Token** if you already made one).
3. Follow the prompts to name your bot — BotFather replies with a token like `123456789:AAExampleTokenTextHere`.
4. Copy that whole string into `TELEGRAM_BOT_TOKEN` below.

#### B. Getting your Gemini API key
1. Go to [aistudio.google.com](https://aistudio.google.com) and sign in with a Google account.
2. Click **Get API key** (left sidebar) → **Create API key**.
3. Choose a Google Cloud project (or let it create one), then copy the generated key.
4. Paste it into `GEMINI_API_KEY` below.

> ℹ️ **Any Gemini model works** — this project isn't locked to one. Use whichever model your key/project has quota for (e.g. `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.0-flash`). Free-tier daily limits vary by model — check your live quota at [aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit) and set `GEMINI_MODEL` accordingly.

#### C. Weekly sales-deck schedule (optional)
The bot can auto-generate a PPTX sales deck on a recurring schedule. These four fields control when that job runs — all optional, defaults shown below:

| Field | What it controls | Example |
|---|---|---|
| `WEEKLY_DECK_TIMEZONE` | Timezone the schedule is evaluated in | `Asia/Kolkata` |
| `WEEKLY_DECK_DAY` | Day of the week the deck is generated (`mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`) | `sun` |
| `WEEKLY_DECK_HOUR` | Hour of the day, 24-hour format | `22` |
| `WEEKLY_DECK_MINUTE` | Minute of the hour | `7` |

The example below runs the deck job every **Sunday at 10:07 PM IST**.

#### 
