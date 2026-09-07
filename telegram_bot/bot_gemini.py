"""
telegram_bot/bot_gemini.py
----------------------------
Telegram wiring for the Gemini version of the agent. Same shape as
bot.py's Claude version — one persistent chat session per Telegram chat,
preferences loaded fresh into every new session's system prompt — but the
tool-calling loop is hand-written here instead of delegated to an SDK.

THIS is the "deep agent" harness option: observe (read the model's
response) -> reason (the model already decided this) -> act (run the
tool) -> observe (send the result back) -> repeat until the model just
replies with text.
"""

import asyncio
import json
import logging
import os
import re
import unicodedata

from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters,
)
from telegram.request import HTTPXRequest

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.gemini_tools import TOOL_DECLARATIONS, build_dispatch_for_chat
import tools.documents as docs
from tools.store_tools import (
    get_all_preferences,
    get_weekly_deck_subscribers,
    add_weekly_deck_subscriber,
    remove_weekly_deck_subscriber,
)
from telegram_bot import menus

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.1-flash-lite"  # free tier: 500 RPD vs 3.6-flash's 20 RPD

SYSTEM_PROMPT_TEMPLATE = """You are the store assistant for an Indian kirana (grocery) shop owner,
talking to them over Telegram. You help with stock, billing, khata (credit),
and daily sales — entirely through natural conversation.

Hard rules, no exceptions:
- NEVER state a price, stock quantity, GST amount, or khata balance from memory.
  Always call a tool to check first. If you haven't called a tool for a fact
  in this turn, don't state that fact.
- If a tool result says "not_found", do NOT propose or guess a specific
  product/customer name yourself, even a real, well-known brand name — you
  only know what tools tell you exists in this catalog, nothing else. Just
  say it wasn't found and ask if they'd like to add it as new.
- If a tool result says "ambiguous", it will include the exact candidate
  names in its "candidates" field — list ONLY those exact names back to
  the owner and ask which one they mean. Never introduce a name that
  didn't come from that candidates list.
- Only call set_preference when the owner EXPLICITLY asks you to save,
  remember, or set a standing default (e.g. "always use UPI", "my
  preferred atta is X"). NEVER call set_preference on your own initiative
  to "remember" something you inferred, guessed, or mentioned in passing —
  doing so would silently turn a guess into something future turns treat
  as a real, owner-given fact.
- Never invent a GST rate or HSN code for a new product — ask the owner.
- When building a bill: call add_bill_item for each item as the owner
  mentions it, use get_bill_summary to read back the running total and
  confirm with the owner, and only call finalize_bill once they've
  confirmed the items and told you the payment method.
- If a tool refuses an action (oversell, khata settlement with no record,
  etc.), explain the refusal plainly to the owner — don't retry blindly
  and don't pretend it succeeded.
- Keep replies short and conversational — this is a chat, not a report.
- House style for confirming things that just happened — one short line,
  no restating internal details like raw file paths or database column
  names. These patterns are written in English below because this prompt
  is in English, but ALWAYS translate them into whichever language the
  owner used THIS turn (English, Hindi, or Tamil) — never send one of
  these confirmations in English when the owner just wrote or spoke in
  Hindi or Tamil. Keep the ✅/📊 emoji, the #<bill_id>, and the ₹ amount
  as-is; translate the surrounding words. Match these patterns:
  - Bill finalized: "✅ Bill #<bill_id> finalized — <payment_method>,
    total ₹<grand_total>."
  - Invoice / sales deck / weekly deck generated: a short one-line
    confirmation only, e.g. "✅ Invoice for Bill #<bill_id> is ready —
    sending it now." or "📊 Here's this week's sales deck." Never mention
    the file_path a tool returns (e.g. "documents/invoice_12.pdf") — the
    actual PDF/PPTX is sent to the owner as a real attachment right after
    your message, so repeating its path in words is just noise to them.
  - Stock added: "✅ Added <qty> <unit> to <product> — new stock:
    <new_qty>."
- Always reply in the SAME language the owner just wrote (or spoke) in —
  English, Hindi, or Tamil ONLY — these are the only three languages this
  bot supports. Match their language every turn; don't ask them to
  switch, and don't default to English if they wrote in Hindi/Tamil. If a
  message mixes languages, reply in whichever one dominates it.
  If you're not confident which language was actually spoken/written,
  default to English rather than guessing Hindi or Tamil.
  Tamil (தமிழ்) is a distinct language and script from Telugu, Kannada,
  and Malayalam — do not substitute one of those for Tamil even though
  they're all South Indian languages. If speech sounds broadly South
  Indian but you can't confirm it's specifically Tamil, default to
  English rather than guessing which South Indian language it is.
  When writing Tamil, use simple, everyday words and complete sentences
  rather than rare or complex script forms — this keeps the Tamil script
  well-formed and reliably readable.
- The owner may send a voice note instead of typing. Listen to it and act
  on it exactly as if it had been typed — same rules apply (call tools for
  any fact, confirm bill items before finalizing, etc.). Identify the
  language from the actual words spoken, not from accent — and reply in
  that exact same spoken language (English, Hindi, or Tamil — see above),
  regardless of what language dominated earlier turns in this conversation.

Standing preferences the owner has set (use these as defaults; e.g. if
default_payment is set, assume that payment method unless the owner says
otherwise for a specific bill):
{preferences}
"""


def _build_system_prompt() -> str:
    prefs = get_all_preferences()["preferences"]
    pref_lines = "\n".join(f"- {k}: {v}" for k, v in prefs.items()) if prefs else "(none set yet)"
    return SYSTEM_PROMPT_TEMPLATE.format(preferences=pref_lines)


class ChatSession:
    """Holds one chat's live Gemini chat session (with its own conversation
    history) plus its own update_id holder, so finalize_bill's idempotency
    key is scoped to this chat only."""

    def __init__(self, client: genai.Client):
        self.current_update_id = "no-update-id"
        self.dispatch = build_dispatch_for_chat(lambda: self.current_update_id)
        config = types.GenerateContentConfig(
            system_instruction=_build_system_prompt(),
            tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
            # We dispatch tool calls ourselves (see run_agent_turn) rather
            # than letting the SDK auto-call raw Python functions — this is
            # what lets finalize_bill's idempotency key be injected safely.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # Lower than the default (~1.0): this is a business tool, not a
            # creative one — replies should be the same steady, predictable
            # phrasing every time, not stylistically varied. As a side
            # benefit, less sampling randomness measurably reduces how
            # often complex scripts like Tamil (which build each letter
            # from a base + combining marks) get an unlikely/wrong token
            # picked mid-word — see _clean_reply_text's note on the
            # garbled-Tamil issue this is one layer of defense against.
            temperature=0.3,
        )
        self.chat = client.aio.chats.create(model=MODEL_NAME, config=config)


SESSIONS: dict[int, ChatSession] = {}
# One lock per chat: serializes messages WITHIN the same chat (so one
# owner sending two quick messages can't corrupt that chat's own
# conversation state), while DIFFERENT chats still run fully concurrently
# — which is what lets two people race on the same product for real.
SESSION_LOCKS: dict[int, asyncio.Lock] = {}

# One shared Gemini client for the whole process; created in run() once
# GEMINI_API_KEY is confirmed to exist.
_client: genai.Client | None = None


def get_or_create_session(chat_id: int) -> ChatSession:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = ChatSession(_client)
        SESSION_LOCKS[chat_id] = asyncio.Lock()
    return SESSIONS[chat_id]


MAX_TOOL_ROUNDS = 8  # safety valve against an infinite tool-call loop
MAX_RATE_LIMIT_RETRIES = 3

_RETRY_DELAY_PATTERN = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


async def _send_message_with_retry(session: "ChatSession", message):
    """
    Google's free tier allows only a few requests per minute per model.
    A single conversation turn in this bot can involve several API calls
    (one per tool-call round), so it's easy to hit that limit mid-turn.
    Google's own error message tells us exactly how long to wait ("Please
    retry in 43.97s") — so we parse that and wait exactly that long,
    instead of failing the whole message outright.
    """
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        try:
            return await session.chat.send_message(message)
        except genai_errors.ClientError as e:
            if e.code != 429 or attempt == MAX_RATE_LIMIT_RETRIES - 1:
                raise
            match = _RETRY_DELAY_PATTERN.search(e.message or "")
            wait_seconds = float(match.group(1)) + 1 if match else 20.0
            logger.warning(
                "Rate limited by Gemini free tier — waiting %.1fs before retrying (attempt %d/%d)",
                wait_seconds, attempt + 1, MAX_RATE_LIMIT_RETRIES,
            )
            await asyncio.sleep(wait_seconds)


async def run_agent_turn(session: ChatSession, user_message: str | list) -> tuple[str, list[str]]:
    """
    The hand-rolled agent loop:
      1. Send the user's message (or, on later rounds, the tool results).
         user_message can be plain text OR a list of genai Parts (e.g. an
         inline audio Part for a voice note) — Gemini treats both as a
         normal turn, tool-calling included.
      2. If Gemini's response contains function_call parts, run each
         matching tool from session.dispatch and collect function_response
         parts.
      3. If there ARE function_response parts, send them back and repeat.
      4. Otherwise, the model gave a plain text reply — return it.
    Returns (reply_text, file_paths) — file_paths collects any "file_path"
    fields returned by tools this turn (e.g. generate_invoice_pdf,
    generate_sales_deck), so the caller can send them as real Telegram
    documents, not just mention them in text.
    """
    message: str | list = user_message
    file_paths: list[str] = []

    for _round in range(MAX_TOOL_ROUNDS):
        response = await _send_message_with_retry(session, message)
        candidate = response.candidates[0]
        parts = candidate.content.parts if candidate.content else []

        function_calls = [p.function_call for p in parts if p.function_call is not None]
        text_parts = [p.text for p in parts if getattr(p, "text", None)]

        if not function_calls:
            return "".join(text_parts).strip() or "(no response)", file_paths

        response_parts = []
        for call in function_calls:
            args_dict = dict(call.args)
            tool_fn = session.dispatch.get(call.name)
            if tool_fn is None:
                result = {"status": "error", "reason": f"Unknown tool '{call.name}'"}
            else:
                try:
                    result = await asyncio.to_thread(tool_fn, args_dict)
                except Exception as e:
                    logger.exception("Tool %s failed", call.name)
                    result = {"status": "error", "reason": str(e)}
            logger.info("TOOL CALL: %s(%s) -> %s", call.name, args_dict, result)
            if isinstance(result, dict) and result.get("status") == "ok" and result.get("file_path"):
                file_paths.append(result["file_path"])
            response_parts.append(
                types.Part.from_function_response(name=call.name, response=result)
            )

        message = response_parts  # sent back as the next "message" in the loop

    return "That request needed more steps than I could safely take at once — could you break it into smaller parts?", file_paths


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm your store assistant. Tell me things like:\n"
        "\u2022 <b>'50 packets of Maggi came in, cost 12'</b>\n"
        "\u2022 <b>'make a bill: 2kg sugar, 1 atta, UPI'</b>\n"
        "\u2022 <b>'what's low on stock?'</b>\n"
        "\u2022 <b>'put \u20b9500 on Ramesh's credit'</b>\n"
        "\u2022 <b>'close the day'</b>\n\n"
        "You can also just send a voice note instead of typing.\n\n"
        "Prefer tapping buttons? Use /menu for a guided, form-based way to "
        "add items and build orders.\n\n"
        "Use /new any time to start a fresh conversation.\n"
        "Use /subscribe_weekly to get the sales deck here automatically "
        "every week, or /unsubscribe_weekly to stop.",
        parse_mode="HTML",
    )


async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    SESSIONS.pop(chat_id, None)
    SESSION_LOCKS.pop(chat_id, None)
    get_or_create_session(chat_id)
    await update.message.reply_text(
        "Started a fresh conversation. Your stock, bills, khata, and preferences are all still here."
    )


async def handle_subscribe_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await asyncio.to_thread(add_weekly_deck_subscriber, chat_id)
    await update.message.reply_text(
        "Subscribed \u2014 I'll drop the sales deck here automatically once a week."
    )


async def handle_unsubscribe_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await asyncio.to_thread(remove_weekly_deck_subscriber, chat_id)
    await update.message.reply_text("Unsubscribed \u2014 no more automatic weekly deck here.")


def _clean_reply_text(text: str) -> str:
    """
    Gemini occasionally emits Tamil text with combining marks (vowel
    signs, virama) that have no valid Tamil base letter before them —
    e.g. right after a space or an English word, or stacked on another
    mark. Telegram's font shaper can't compose those into a real letter,
    so each one renders as an empty "tofu" box. NFC normalization (which
    only reorders/recomposes otherwise-valid sequences) doesn't fix this;
    the marks here are genuinely orphaned, not just out of order.

    This does two passes:
      1. NFC-normalize (fixes legitimately reorderable sequences).
      2. Drop any Tamil combining mark that isn't immediately preceded by
         a real Tamil base letter, rather than let it render as a box.
    Both are no-ops on ordinary English/Hindi/well-formed Tamil text —
    verified against real good and synthetically-broken samples — so this
    is safe to run unconditionally on every reply.
    """
    text = unicodedata.normalize("NFC", text)
    out = []
    prev_is_tamil_base = False
    for ch in text:
        in_tamil_block = 0x0B80 <= ord(ch) <= 0x0BFF
        is_mark = unicodedata.category(ch).startswith("M")
        if in_tamil_block and is_mark:
            if not prev_is_tamil_base:
                continue  # orphaned Tamil combining mark — drop it
            out.append(ch)
            prev_is_tamil_base = False
            continue
        out.append(ch)
        prev_is_tamil_base = in_tamil_block and not is_mark
    return "".join(out)



# Telugu, Kannada, and Malayalam Unicode blocks. The bot only ever
# intends to write English, Hindi (Devanagari), or Tamil — so any
# character in these ranges means Gemini silently drifted into a
# neighbouring Dravidian script while "meaning" to write Tamil (a known
# failure mode with these models, especially right after a voice-note
# turn). Unlike orphaned combining marks (see _clean_reply_text), these
# are fully valid letters in their own script, so they don't get
# stripped by that cleaner — and on a phone whose keyboard/fonts are set
# up for Tamil+English only, they render as blank "tofu" boxes instead
# of the intended Tamil.
_WRONG_SOUTH_SCRIPT_RANGES = (
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
    (0x0D00, 0x0D7F),  # Malayalam
)


def _has_wrong_south_indian_script(text: str) -> bool:
    return any(
        any(lo <= ord(ch) <= hi for lo, hi in _WRONG_SOUTH_SCRIPT_RANGES)
        for ch in text
    )


_SCRIPT_CORRECTION_REMINDER = (
    "Your last reply used Telugu, Kannada, or Malayalam script somewhere "
    "in it — none of those are Tamil, even though they look related. "
    "Resend that ENTIRE reply, but written correctly in Tamil (தமிழ்) "
    "script throughout, with the exact same meaning, numbers, ₹ amounts, "
    "and emoji as before."
)


async def _run_turn_and_reply(update: Update, chat_id: int, message: str | list):
    """
    Shared tail end for both text and voice messages: runs one agent turn
    under this chat's lock (idempotency key scoped to this update), then
    sends back the reply plus any generated files. Text and voice notes
    only differ in what `message` is — a string vs. a list of genai Parts.
    """
    session = get_or_create_session(chat_id)
    lock = SESSION_LOCKS[chat_id]

    async with lock:  # serializes THIS chat only; other chats run concurrently
        # Entire idempotency mechanism: whatever finalize_bill runs during
        # this message reads exactly this value, scoped only to this chat.
        session.current_update_id = str(update.update_id)

        file_paths: list[str] = []
        try:
            reply_text, file_paths = await run_agent_turn(session, message)
            if _has_wrong_south_indian_script(reply_text):
                # Fix at the source instead of mangling the text ourselves:
                # there's no way to string-repair "wrong script" into
                # correct Tamil, so ask the model to redo just this reply.
                # One retry only — if it still gets it wrong, we fall back
                # to whatever it gives us rather than looping forever.
                logger.warning(
                    "Chat %s: reply contained non-Tamil South-Indian script, "
                    "retrying once", chat_id,
                )
                retried_text, _ = await run_agent_turn(session, _SCRIPT_CORRECTION_REMINDER)
                if not _has_wrong_south_indian_script(retried_text):
                    reply_text = retried_text
        except Exception:
            logger.exception("Error while handling message for chat %s", chat_id)
            reply_text = "Something went wrong on my end handling that — please try again."

    await update.message.reply_text(_clean_reply_text(reply_text))

    for path in file_paths:
        try:
            with open(path, "rb") as f:
                await update.message.reply_document(document=f, filename=os.path.basename(path))
        except Exception:
            logger.exception("Failed to send generated file %s", path)
            await update.message.reply_text(f"(Generated the file but couldn't send it: {path})")


# The system prompt states the "match the owner's language" rule once, at
# session start — but in a long multi-turn chat, a model leans more on the
# pattern already established over several turns than on an instruction
# that's now far back in context. So every single turn also carries this
# short reminder right next to the actual content, which is what actually
# keeps a mid-conversation language switch (e.g. Tamil voice notes, then a
# plain English question) working instead of "sticking" to whatever
# language dominated the last few turns.
_TEXT_LANGUAGE_REMINDER = (
    "(Reply in the same language as this message, regardless of what "
    "language was used earlier in this conversation.)"
)
_VOICE_LANGUAGE_REMINDER = (
    "Listen to the voice note that follows this message and act on it. "
    "Identify the language from the actual words spoken — it will be "
    "English, Hindi, or Tamil (தமிழ்), and no other language — judge this "
    "by the words themselves, not by accent. Tamil is a distinct language "
    "and script from Telugu, Kannada, and Malayalam; do not reply in one "
    "of those by mistake. Reply in the exact same language that was "
    "spoken, regardless of what language was used earlier in this "
    "conversation. If you're not confident which of the three it is, "
    "default to English rather than guessing."
)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return
    # If /menu has an in-progress form waiting on typed input (a price, a
    # search query, a customer name...), it claims the message here and we
    # stop — it never reaches Gemini. Everything else falls through to the
    # normal chat/LLM path exactly as before.
    if await menus.handle_pending_text(update, context):
        return
    message = f"{text}\n\n{_TEXT_LANGUAGE_REMINDER}"
    await _run_turn_and_reply(update, update.effective_chat.id, message)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Voice-note orders: Gemini accepts audio natively, so there's no separate
    transcription step — the raw OGG/Opus bytes Telegram gives us go
    straight into the same chat session and the same tool-calling loop as
    a typed message. The model hears it, decides which tools to call
    (add_stock, add_bill_item, etc.), and replies in the language it was
    spoken in, per the system prompt.
    """
    voice = update.message.voice
    if voice is None:
        return

    tg_file = await context.bot.get_file(voice.file_id)
    audio_bytes = bytes(await tg_file.download_as_bytearray())
    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
    reminder_part = types.Part.from_text(text=_VOICE_LANGUAGE_REMINDER)

    # Reminder first, audio second: the language-identification instruction
    # is easy for the model to lose track of if it's an afterthought stuck
    # on the end of a multimodal turn — reading it before the audio primes
    # what to listen for.
    await _run_turn_and_reply(update, update.effective_chat.id, [reminder_part, audio_part])


async def send_weekly_deck(bot):
    """
    The scheduled job: builds the 'Weekly Business Review' deck (trend +
    movers + reorder priorities + khata follow-ups + action items — see
    tools/documents.py's module note on why this is deliberately NOT the
    same document as the on-demand 'this week's sales' deck), then pushes
    it to every subscribed chat.
    """
    result = await asyncio.to_thread(docs.generate_weekly_review_deck)
    if result["status"] != "ok":
        logger.warning("Weekly deck job: nothing to send (%s)", result.get("reason"))
        return

    chat_ids = (await asyncio.to_thread(get_weekly_deck_subscribers))["chat_ids"]
    if not chat_ids:
        logger.info("Weekly deck job: no subscribers, skipping send.")
        return

    file_path = result["file_path"]
    caption = (
        f"Here's your Weekly Business Review for "
        f"{result['start_date']} to {result['end_date']}!"
    )
    for chat_id in chat_ids:
        try:
            with open(file_path, "rb") as f:
                await bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=os.path.basename(file_path),
                    caption=caption,
                )
        except Exception:
            logger.exception("Weekly deck job: failed to send to chat %s", chat_id)
            
            
def run(telegram_token: str, gemini_api_key: str):
    global _client
    _client = genai.Client(api_key=gemini_api_key)

    # One shared scheduler for the process — future scheduled jobs (e.g. a
    # daily closing reminder) can reuse this same instance rather than
    # spinning up another one.
    timezone = os.environ.get("WEEKLY_DECK_TIMEZONE", "Asia/Kolkata")
    day_of_week = os.environ.get("WEEKLY_DECK_DAY", "mon")
    hour = int(os.environ.get("WEEKLY_DECK_HOUR", "9"))
    minute = int(os.environ.get("WEEKLY_DECK_MINUTE", "0"))
    scheduler = AsyncIOScheduler(timezone=timezone)

    async def _post_init(app: Application):
        # APScheduler's AsyncIOScheduler needs a running event loop to
        # start against — python-telegram-bot's post_init hook runs inside
        # that loop right before polling begins, which is exactly the
        # right moment.
        scheduler.add_job(
            send_weekly_deck,
            trigger=CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute),
            args=[app.bot],
            id="weekly_sales_deck",
            replace_existing=True,
        )
        scheduler.start()
        logger.info(
            "Weekly deck scheduler started: every %s at %02d:%02d %s",
            day_of_week, hour, minute, timezone,
        )

    # Default python-telegram-bot timeouts are quite short (5s connect/read),
    # which is easy to trip on a slower or briefly-unstable home connection
    # and was causing TimedOut errors on ordinary button taps. Give requests
    # to Telegram's API more slack; get_updates (long-polling) needs its own
    # longer read timeout since it deliberately holds the connection open.
    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=40.0,
    )

    app = (
        Application.builder()
        .token(telegram_token)
        .request(request)
        .get_updates_request(get_updates_request)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("new", handle_new))
    app.add_handler(CommandHandler("menu", menus.handle_menu_command))
    app.add_handler(CommandHandler("subscribe_weekly", handle_subscribe_weekly))
    app.add_handler(CommandHandler("unsubscribe_weekly", handle_unsubscribe_weekly))
    app.add_handler(CallbackQueryHandler(menus.handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting (polling mode, Gemini backend)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
