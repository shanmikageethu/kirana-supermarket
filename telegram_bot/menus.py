"""
telegram_bot/menus.py
------------------------
A button-driven ("forms") front end for the store agent, sitting ALONGSIDE
the free-text / voice chat in telegram_bot/bot_gemini.py — it never
replaces it. The owner can keep typing or sending voice notes exactly as
before; /menu just gives them a tap-only path for the two things that
benefit most from structure: adding a new product, and building an order.

Design choices, and why:
- Every action here calls tools/store_tools.py DIRECTLY, not through the
  Gemini chat loop. These flows are fully deterministic (a button tap only
  ever means one thing), so there's no reason to spend a Gemini free-tier
  call — or risk the model mis-reading a field — on something a state
  machine can do exactly right every time.
- One in-memory FORM_STATE dict per chat_id, mirroring the SESSIONS /
  SESSION_LOCKS pattern already used in bot_gemini.py. A per-chat asyncio
  lock serializes button taps and typed answers WITHIN one chat (so a
  double-tap can't corrupt that chat's form state) while different chats
  still run fully concurrently.
- Every screen is rendered by editing ONE message per chat (stored as
  state["message_id"]) instead of spamming new messages — this is what
  lets tapping a product show "Maggi x2" in place, and what keeps a
  multi-step form feeling like a single form rather than a chat log.
- bot_gemini.py's handle_message() calls handle_pending_text() first, on
  every text message, before it ever reaches Gemini. If a form is mid-step
  and waiting on typed input (a price, a search query, a customer name),
  this claims the message and returns True; otherwise it returns False and
  the message goes to Gemini exactly as it always has. This is the entire
  mechanism that keeps normal text/voice chat and the new forms feature
  from stepping on each other.
"""

import asyncio
import calendar
import logging
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import tools.store_tools as biz
import tools.documents as docs

logger = logging.getLogger(__name__)

# chat_id -> mutable form state dict. Cleared/reset whenever a flow starts
# or finishes; see _clear().
FORM_STATE: dict[int, dict] = {}
_LOCKS: dict[int, asyncio.Lock] = {}


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _LOCKS:
        _LOCKS[chat_id] = asyncio.Lock()
    return _LOCKS[chat_id]


def _clear(state: dict):
    state.clear()


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


def _money(x) -> str:
    return f"\u20b9{x:,.2f}"


async def _render(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str,
                   keyboard: InlineKeyboardMarkup, state: dict, parse_mode=None):
    """
    Single choke point for showing a screen: edits the chat's one standing
    menu message if we have one, otherwise sends a fresh message and
    remembers its id. This is what makes the menu feel like one evolving
    form instead of a flood of messages, regardless of whether this screen
    was triggered by a button tap or by the owner typing an answer.

    parse_mode is opt-in (e.g. "HTML") for the few screens that need bold /
    highlighted text (see _stock_lines) — every other screen keeps sending
    plain text exactly as before.
    """
    message_id = state.get("message_id")
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=keyboard,
                parse_mode=parse_mode,
            )
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            # Message too old / deleted / not editable — fall through and
            # send a fresh one below.
    msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard,
                                          parse_mode=parse_mode)
    state["message_id"] = msg.message_id


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def _main_menu_kb() -> InlineKeyboardMarkup:
    return _kb([
        [("\U0001f6d2 New Order", "m:neworder")],
        [("\u2795 Add New Item", "m:additem")],
        [("\U0001f4e5 Add Stock", "m:addstock")],
        [("\U0001f4e6 Check Stock", "m:checkstock")],
        [("\u26a0\ufe0f Low Stock", "m:lowstock")],
        [("\U0001f4ca Today's Summary", "m:summary")],
        [("\U0001f4b3 Khata Balance", "m:khata")],
        [("\U0001f4c6 Weekly Deck", "m:weeklydeck")],
    ])


async def _show_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, state: dict):
    await _render(
        context, chat_id,
        "\U0001f4cb Store Menu \u2014 tap an option:\n\n"
        "You can still just type or send a voice note any time instead.",
        _main_menu_kb(), state,
    )


async def handle_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with _get_lock(chat_id):
        state = FORM_STATE.setdefault(chat_id, {})
        _clear(state)
        # /menu always starts a fresh message rather than editing a
        # possibly long-gone old one.
        msg = await update.message.reply_text(
            "\U0001f4cb Store Menu \u2014 tap an option:\n\n"
            "You can still just type or send a voice note any time instead.",
            reply_markup=_main_menu_kb(),
        )
        state["message_id"] = msg.message_id


# ---------------------------------------------------------------------------
# Read-only quick views: low stock / today's summary / khata balance
# ---------------------------------------------------------------------------

async def _show_low_stock(chat_id, context, state):
    result = await asyncio.to_thread(biz.get_low_stock)
    items = result.get("low_stock_items", [])
    if not items:
        body = "Nothing is low on stock right now \U0001f44d"
    else:
        lines = [f"\u2022 {i['name']}: {i['stock_quantity']} {i['unit']} left (reorder at {i['reorder_level']})"
                 for i in items]
        body = "\u26a0\ufe0f Low stock:\n" + "\n".join(lines)
    await _render(context, chat_id, body, _kb([[("\U0001f3e0 Main Menu", "m:main")]]), state)


# ---------------------------------------------------------------------------
# "Check Stock" — read-only frequent-10 list + search, no further action
# ---------------------------------------------------------------------------

def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _stock_lines(products) -> str:
    lines = []
    for p in products:
        name = _escape_html(p["name"])
        line = f"\u2022 {name}: {p['stock_quantity']} {p['unit']}"
        if p.get("reorder_level") is not None and p["stock_quantity"] <= p["reorder_level"]:
            line = f"\U0001f534 <b>{line}</b> (reorder at {p['reorder_level']})"
        lines.append(line)
    return "\n".join(lines)


async def _start_check_stock(chat_id, context, state):
    _clear(state)
    state["flow"] = "check_stock"
    products = (await asyncio.to_thread(biz.get_frequent_products, 10))["products"]
    if products:
        body = "\U0001f4e6 Stock \u2014 your 10 most frequently sold items:\n\n" + _stock_lines(products)
    else:
        body = "\U0001f4e6 No products in the catalog yet \u2014 add one first, or search by name."
    await _render(context, chat_id, body,
                  _kb([[("\U0001f50d Search product", "cs:search")], [("\U0001f3e0 Main Menu", "m:main")]]),
                  state, parse_mode="HTML")


async def _handle_check_stock_callback(chat_id, context, state, data, query):
    if state.get("flow") != "check_stock":
        await query.answer("This screen isn't active anymore.", show_alert=True)
        return
    action = data.split(":")[1]
    if action == "search":
        state["awaiting_text"] = "check_stock_search"
        await _render(context, chat_id, "\U0001f4e6 Type part of the product's name:",
                      _kb([[("\u274c Cancel", "m:main")]]), state)


async def _handle_check_stock_search_text(chat_id, context, state, text):
    products = (await asyncio.to_thread(biz.search_products, text, 10))["products"]
    state["awaiting_text"] = None
    if not products:
        state["awaiting_text"] = "check_stock_search"
        await _render(context, chat_id, f"No product matched \u201c{text}\u201d \u2014 try another name:",
                      _kb([[("\u274c Cancel", "m:main")]]), state)
        return
    body = f"\U0001f4e6 Stock for \u201c{_escape_html(text)}\u201d:\n\n" + _stock_lines(products)
    await _render(context, chat_id, body,
                  _kb([[("\U0001f50d Search again", "cs:search")], [("\U0001f3e0 Main Menu", "m:main")]]),
                  state, parse_mode="HTML")


async def _show_summary(chat_id, context, state):
    _clear(state)
    state["flow"] = "summary"
    s = await asyncio.to_thread(biz.get_daily_summary, None)
    title = f"\U0001f4ca <b>Today's summary ({s['date']})</b>:"
    if s.get("bill_count", 0) == 0 and s.get("total_sales", 0) == 0:
        body = f"{title}\n\nNo finalized sales yet today."
    else:
        top = s.get("top_items", [])
        top_lines = "\n".join(f"  \u2022 <b>{_escape_html(t['name'])}</b>: {t['qty_sold']} sold" for t in top) or "  (none)"
        body = (
            f"{title}\n\n"
            f"<b>Bills</b>: {s['bill_count']}\n"
            f"<b>Total sales</b>: {_money(s['total_sales'])}\n"
            f"<b>Tax collected</b>: {_money(s['tax_collected'])}\n"
            f"<b>Cash</b>: {_money(s['cash_total'])} \u2022 <b>UPI</b>: {_money(s['upi_total'])} \u2022 "
            f"<b>Card</b>: {_money(s['card_total'])}\n\n"
            f"<b>Top items today</b>:\n{top_lines}"
        )
    await _render(context, chat_id, body,
                  _kb([[("\U0001f4ca Get Today's Sales Deck", "sm:deck")], [("\U0001f3e0 Main Menu", "m:main")]]),
                  state, parse_mode="HTML")


async def _handle_summary_callback(chat_id, context, state, data, query):
    if state.get("flow") != "summary":
        await query.answer("This screen isn't active anymore \u2014 open Today's Summary again.", show_alert=True)
        return
    action = data.split(":")[1]
    if action == "deck":
        today = date.today().isoformat()
        result = await asyncio.to_thread(docs.generate_sales_deck, today, today)
        if result["status"] != "ok":
            await query.answer(result.get("reason", "Couldn't generate the sales deck."), show_alert=True)
            return
        with open(result["file_path"], "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f,
                                             filename=f"sales_deck_{today}.pptx")


async def _show_weekly_deck(chat_id, context, state):
    """A standalone main-menu action (not nested under Today's Summary) —
    lets the owner choose between the same 'Weekly Business Review' deck
    the scheduler sends automatically every Sunday (7 days ending today),
    or a specific past week by date."""
    _clear(state)
    state["flow"] = "weekly_deck"
    await _render(
        context, chat_id,
        "\U0001f4c6 Weekly Deck \u2014 which week?",
        _kb([
            [("\U0001f4c5 Current Week", "wd:current")],
            [("\U0001f5d3\ufe0f Pick a Week", "wd:custom")],
            [("\U0001f3e0 Main Menu", "m:main")],
        ]),
        state,
    )


async def _send_weekly_deck(chat_id, context, state, end_date=None):
    """Generates and sends the 'Weekly Business Review' deck for the 7-day
    window ending at end_date (or the 7 days ending today when end_date is
    None)."""
    result = await asyncio.to_thread(docs.generate_weekly_review_deck, end_date)
    _clear(state)
    if result["status"] != "ok":
        await _render(context, chat_id,
                       f"\U0001f4c6 {result.get('reason', 'Could not generate the weekly deck.')}",
                       _kb([[("\U0001f3e0 Main Menu", "m:main")]]), state)
        return
    with open(result["file_path"], "rb") as f:
        await context.bot.send_document(
            chat_id=chat_id, document=f,
            filename=f"weekly_review_{result['start_date']}_to_{result['end_date']}.pptx",
        )
    await _render(
        context, chat_id,
        f"\U0001f4c6 Weekly deck for {result['start_date']} to {result['end_date']} sent above.",
        _kb([[("\U0001f3e0 Main Menu", "m:main")]]), state,
    )


def _calendar_keyboard(year: int, month: int, *, mode: str, min_date=None, max_date=None,
                        extra_rows=None) -> InlineKeyboardMarkup:
    """A month-view inline calendar.
    mode='s': picking the week's START date — any day up to today is
      tappable, future days are blank.
    mode='e': picking the week's END date — ONLY days within
      [min_date, max_date] (the 7-day window anchored at the chosen start)
      are tappable; every other day is blank, so a mismatched range can't
      be picked in the first place.
    ‹ / › re-render this same message for the previous/next month, staying
    in the same mode with the same min/max constraints."""
    first_of_month = date(year, month, 1)
    prev_month = (first_of_month - timedelta(days=1)).replace(day=1)
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    today = date.today()

    rows = [[
        InlineKeyboardButton("\u2039", callback_data=f"wd:nav:{mode}:{prev_month.year:04d}-{prev_month.month:02d}"),
        InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="wd:noop"),
        InlineKeyboardButton("\u203a", callback_data=f"wd:nav:{mode}:{next_month.year:04d}-{next_month.month:02d}"),
    ]]
    rows.append([InlineKeyboardButton(d, callback_data="wd:noop")
                 for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]])

    day_prefix = "wd:sd:" if mode == "s" else "wd:ed:"
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="wd:noop"))
                continue
            d = date(year, month, day)
            enabled = d <= today if mode == "s" else (min_date <= d <= max_date)
            if enabled:
                row.append(InlineKeyboardButton(str(day), callback_data=f"{day_prefix}{d.isoformat()}"))
            else:
                row.append(InlineKeyboardButton(" ", callback_data="wd:noop"))
        rows.append(row)

    for row in (extra_rows or []):
        rows.append([InlineKeyboardButton(label, callback_data=cb) for label, cb in row])
    rows.append([InlineKeyboardButton("\U0001f3e0 Main Menu", callback_data="m:main")])
    return InlineKeyboardMarkup(rows)


async def _handle_weekly_deck_callback(chat_id, context, state, data, query):
    if state.get("flow") != "weekly_deck":
        await query.answer("This screen isn't active anymore \u2014 open Weekly Deck again.", show_alert=True)
        return
    parts = data.split(":")
    action = parts[1]

    if action == "current":
        await _send_weekly_deck(chat_id, context, state, end_date=None)

    elif action == "custom":
        state.pop("wd_start", None)
        today = date.today()
        await _render(
            context, chat_id,
            "\U0001f4c5 Tap the week's START date:",
            _calendar_keyboard(today.year, today.month, mode="s"), state,
        )

    elif action == "nav":
        mode, ym = parts[2], parts[3]
        year, month = map(int, ym.split("-"))
        if mode == "s":
            await _render(
                context, chat_id,
                "\U0001f4c5 Tap the week's START date:",
                _calendar_keyboard(year, month, mode="s"), state,
            )
        else:
            start = date.fromisoformat(state["wd_start"])
            end_max = min(start + timedelta(days=6), date.today())
            await _render(
                context, chat_id,
                f"\u2705 Start date: {start.isoformat()}\n\n"
                f"\U0001f4c5 Now tap the END date \u2014 only {start.isoformat()} to {end_max.isoformat()} "
                f"is selectable (can't go past today):",
                _calendar_keyboard(year, month, mode="e", min_date=start, max_date=end_max,
                                    extra_rows=[[("\U0001f519 Change Start Date", "wd:custom")]]),
                state,
            )

    elif action == "sd":
        start = date.fromisoformat(parts[2])
        state["wd_start"] = start.isoformat()
        end_max = min(start + timedelta(days=6), date.today())
        await _render(
            context, chat_id,
            f"\u2705 Start date: {start.isoformat()}\n\n"
            f"\U0001f4c5 Now tap the END date \u2014 only {start.isoformat()} to {end_max.isoformat()} "
            f"is selectable (can't go past today):",
            _calendar_keyboard(start.year, start.month, mode="e", min_date=start, max_date=end_max,
                                extra_rows=[[("\U0001f519 Change Start Date", "wd:custom")]]),
            state,
        )

    elif action == "ed":
        start_raw = state.get("wd_start")
        if not start_raw:
            today = date.today()
            await _render(
                context, chat_id,
                "That selection expired \u2014 tap the week's START date again:",
                _calendar_keyboard(today.year, today.month, mode="s"), state,
            )
            return
        start = date.fromisoformat(start_raw)
        end = date.fromisoformat(parts[2])
        end_max = min(start + timedelta(days=6), date.today())
        if not (start <= end <= end_max):
            await query.answer("Pick a date within the highlighted week.", show_alert=True)
            return
        await _send_weekly_deck(chat_id, context, state, end_date=end.isoformat())

    elif action == "noop":
        pass


async def _show_khata_total(chat_id, context, state):
    """Entry point for m:khata — leads with the ONE number an owner
    usually wants first (how much is outstanding overall), and only
    drills into individual customers if they ask for that next."""
    _clear(state)
    state["flow"] = "khata"
    customers = (await asyncio.to_thread(biz.list_khata_balances, 1000))["customers"]
    count = len(customers)
    if count == 0:
        body = "\U0001f4b3 No outstanding khata balances right now."
    else:
        total = sum(c["balance"] for c in customers)
        body = (
            f"\U0001f4b3 <b>Total outstanding khata: {_money(total)}</b>\n"
            f"across {count} customer{'s' if count != 1 else ''}."
        )
    rows = [[("\U0001f465 Check a customer", "kh:list")],
            [("\U0001f50d Search customer", "kh:search")],
            [("\U0001f3e0 Main Menu", "m:main")]]
    await _render(context, chat_id, body, _kb(rows), state, parse_mode="HTML")


async def _show_khata_list(chat_id, context, state):
    """The 'check a customer' drill-down: top debtors to tap, or search."""
    customers = (await asyncio.to_thread(biz.list_khata_balances, 10))["customers"]
    state["catalog"] = {str(c["customer_id"]): c["customer_name"] for c in customers}

    rows = [[(f"{c['customer_name']} \u2014 {_money(c['balance'])}", f"kh:pick:{c['customer_id']}")]
            for c in customers]
    rows.append([("\U0001f50d Search customer", "kh:search")])
    rows.append([("\u25c0 Back to total", "kh:total")])

    if customers:
        header = "\U0001f4b3 Top khata balances \u2014 tap a name below (no typing needed):"
    else:
        header = "\U0001f4b3 No outstanding khata balances right now."
    await _render(context, chat_id, header, _kb(rows), state)


async def _start_khata_search(chat_id, context, state):
    state["awaiting_text"] = "khata_search"
    await _render(context, chat_id, "\U0001f4b3 Type part of the customer's name:",
                  _kb([[("\u25c0 Back to total", "kh:total")]]), state)


async def _handle_khata_search_text(chat_id, context, state, text):
    customers = (await asyncio.to_thread(biz.list_khata_balances, 10, text))["customers"]
    state["awaiting_text"] = None
    if not customers:
        state["awaiting_text"] = "khata_search"
        await _render(context, chat_id, f"No khata customer matched \u201c{text}\u201d \u2014 try another name:",
                      _kb([[("\u25c0 Back to total", "kh:total")]]), state)
        return
    state["catalog"] = {str(c["customer_id"]): c["customer_name"] for c in customers}
    rows = [[(f"{c['customer_name']} \u2014 {_money(c['balance'])}", f"kh:pick:{c['customer_id']}")]
            for c in customers]
    rows.append([("\u25c0 Back to total", "kh:total")])
    await _render(context, chat_id, "Which one?", _kb(rows), state)


async def _handle_khata_callback(chat_id, context, state, data, query):
    if state.get("flow") != "khata":
        await query.answer("This flow isn't active anymore.", show_alert=True)
        return
    parts = data.split(":")
    action = parts[1]

    if action == "total":
        await _show_khata_total(chat_id, context, state)
        return

    if action == "list":
        await _show_khata_list(chat_id, context, state)
        return

    if action == "search":
        await _start_khata_search(chat_id, context, state)
        return

    if action == "pick":
        customer_name = state["catalog"].get(parts[2])
        if not customer_name:
            await query.answer("Unknown customer \u2014 start over.", show_alert=True)
            return
        result = await asyncio.to_thread(biz.get_khata_balance, customer_name)
        _clear(state)
        if result["status"] == "not_found":
            body = f"No khata record exists for '{customer_name}'."
        else:
            # e.g. "Ramesh: \u20b9300.00"
            body = f"\U0001f4b3 <b>{_escape_html(result['customer'])}</b>: <b>{_money(result['balance'])}</b>"
        await _render(context, chat_id, body, _kb([[("\U0001f3e0 Main Menu", "m:main")]]), state, parse_mode="HTML")


# ---------------------------------------------------------------------------
# "Add New Item" form — a guided sequence of fields
# ---------------------------------------------------------------------------

ADD_ITEM_STEPS = [
    {"key": "name", "prompt": "What's the product name?", "kind": "text"},
    {"key": "unit", "prompt": "Which unit is it sold in?", "kind": "choice", "choices": [
        ("kg", "kg"), ("g", "g"), ("litre", "litre"), ("ml", "ml"),
        ("packet", "packet"), ("dozen", "dozen"), ("piece", "piece"),
    ]},
    {"key": "is_loose", "prompt": "Sold loose (by weight/volume) or packaged/counted?", "kind": "choice", "choices": [
        ("Loose", "1"), ("Packaged", "0"),
    ]},
    {"key": "cost_price", "prompt": "Cost price (\u20b9 you pay per unit)?", "kind": "number"},
    {"key": "sell_price", "prompt": "Selling price (\u20b9 you charge)?", "kind": "number"},
    {"key": "mrp", "prompt": "MRP (\u20b9)?", "kind": "number"},
    {"key": "gst_rate", "prompt": "GST rate (%)? Type the number, <b>e.g. 0, 5, 12, 18, 28</b>.", "kind": "number"},
    {"key": "hsn_code", "prompt": "HSN code? (or Skip)", "kind": "text", "skippable": True, "default": ""},
    {"key": "initial_stock", "prompt": "Initial stock quantity right now? (or Skip for 0)",
     "kind": "number", "skippable": True, "default": 0},
    {"key": "reorder_level", "prompt": "Reorder alert level \u2014 warn when stock falls to/below this? (or Skip for 0)",
     "kind": "number", "skippable": True, "default": 0},
]


async def _start_add_item(chat_id, context, state):
    """Entry point for m:additem — jumps straight to the one-message
    "quick add" prompt (see _start_add_item_bulk below), since that's the
    fast path most owners want. Step-by-step is still one tap away, via
    the "Switch to step-by-step" button on that screen, for anyone unsure
    of the field order."""
    _clear(state)
    state["flow"] = "add_item"
    await _start_add_item_bulk(chat_id, context, state)


_BULK_UNITS = {"kg", "g", "litre", "ml", "packet", "dozen", "piece"}

# Accepted spellings for the loose/packaged field, mapped to is_loose (1/0).
# The owner types this explicitly now — it's no longer guessed from the unit,
# since e.g. a "kg" item can just as easily be a pre-packed 1kg bag.
_LOOSE_CHOICES = {
    "loose": 1, "l": 1, "yes": 1, "y": 1, "1": 1,
    "packaged": 0, "package": 0, "packed": 0, "p": 0, "no": 0, "n": 0, "0": 0,
}

# name, unit, loose/packaged, cost, sell, mrp, gst are required; hsn/stock/
# reorder are optional and default to "" / 0 / 0 if left off the end of the line.
_BULK_EXAMPLE = "Maggi 70g, packet, packaged, 10, 14, 14, 12, 19023090, 20, 5"
_BULK_PROMPT = (
    "\u26a1 Quick add \u2014 send ONE message with fields separated by commas, in this order:\n\n"
    "<b>name, unit, loose or packaged, cost price, sell price, mrp, gst rate, hsn code, "
    "initial stock, reorder level</b>\n\n"
    f"Example:\n<b>{_BULK_EXAMPLE}</b>\n\n"
    "The last three (hsn code, initial stock, reorder level) are optional \u2014 "
    "leave them off and they'll default to blank / 0 / 0.\n"
    f"Unit must be one of: {', '.join(sorted(_BULK_UNITS))}.\n"
    "Loose or packaged must be one of: loose, packaged (or yes/no, 1/0)."
)


async def _start_add_item_bulk(chat_id, context, state):
    state["step"] = None
    state["data"] = {}
    state["awaiting_text"] = "add_item_bulk"
    await _render(context, chat_id, _BULK_PROMPT,
                  _kb([[("\U0001fa9c Switch to step-by-step", "ai:mode:steps")],
                       [("\u274c Cancel", "ai:cancel")]]), state, parse_mode="HTML")


async def _start_add_item_steps(chat_id, context, state):
    state["step"] = 0
    state["data"] = {}
    await _add_item_prompt(chat_id, context, state)


def _parse_bulk_add_item(text: str):
    """Returns (data_dict, None) on success, or (None, error_message)."""
    fields = [f.strip() for f in text.split(",")]
    if len(fields) < 7:
        return None, (
            "That needs at least 7 fields: name, unit, loose or packaged, cost price, "
            f"sell price, mrp, gst rate (got {len(fields)}). See the format above and try again."
        )

    name, unit, loose_s, cost_s, sell_s, mrp_s, gst_s = fields[:7]
    hsn_code = fields[7] if len(fields) > 7 else ""
    stock_s = fields[8] if len(fields) > 8 else "0"
    reorder_s = fields[9] if len(fields) > 9 else "0"

    if not name:
        return None, "Product name can't be blank."
    unit = unit.lower()
    if unit not in _BULK_UNITS:
        return None, f"Unit must be one of: {', '.join(sorted(_BULK_UNITS))} (got '{unit}')."

    is_loose = _LOOSE_CHOICES.get(loose_s.lower())
    if is_loose is None:
        return None, f"Loose or packaged must be one of: loose, packaged (got '{loose_s}')."

    try:
        cost_price = float(cost_s.replace(",", "."))
        sell_price = float(sell_s.replace(",", "."))
        mrp = float(mrp_s.replace(",", "."))
        gst_rate = float(gst_s.replace(",", "."))
        initial_stock = float(stock_s.replace(",", ".")) if stock_s else 0.0
        reorder_level = float(reorder_s.replace(",", ".")) if reorder_s else 0.0
    except ValueError:
        return None, "Cost price, sell price, mrp, gst rate, stock and reorder level must all be numbers."

    if cost_price <= 0 or sell_price <= 0 or mrp <= 0:
        return None, "Cost price, sell price and mrp must all be positive."
    if sell_price < cost_price:
        return None, "Selling price can't be below the cost price."

    return {
        "name": name, "unit": unit, "is_loose": is_loose,
        "cost_price": cost_price, "sell_price": sell_price, "mrp": mrp,
        "gst_rate": gst_rate, "hsn_code": hsn_code,
        "initial_stock": initial_stock, "reorder_level": reorder_level,
    }, None


async def _handle_add_item_bulk_text(chat_id, context, state, text):
    data, error = _parse_bulk_add_item(text)
    if error:
        await _render(context, chat_id, f"\u274c {error}\n\n{_BULK_PROMPT}",
                      _kb([[("\U0001fa9c Switch to step-by-step", "ai:mode:steps")],
                           [("\u274c Cancel", "ai:cancel")]]), state, parse_mode="HTML")
        return
    state["awaiting_text"] = None
    state["data"] = data
    await _add_item_confirm_screen(chat_id, context, state)


async def _add_item_prompt(chat_id, context, state):
    idx = state["step"]
    if idx >= len(ADD_ITEM_STEPS):
        await _add_item_confirm_screen(chat_id, context, state)
        return

    step = ADD_ITEM_STEPS[idx]
    rows = []
    if step["kind"] == "choice":
        state["awaiting_text"] = None
        choices = step["choices"]
        for i in range(0, len(choices), 3):
            rows.append([(label, f"ai:v:{idx}:{val}") for label, val in choices[i:i + 3]])
    else:
        state["awaiting_text"] = "add_item"
    if step.get("skippable"):
        rows.append([("\u23ed Skip", f"ai:skip:{idx}")])
    rows.append([("\u274c Cancel", "ai:cancel")])

    await _render(context, chat_id, f"\u2795 Add New Item (step {idx + 1}/{len(ADD_ITEM_STEPS)})\n\n{step['prompt']}",
                  _kb(rows), state, parse_mode="HTML")


async def _add_item_confirm_screen(chat_id, context, state):
    d = state["data"]
    text = (
        "\u2795 Confirm new item:\n\n"
        f"Name: {d.get('name')}\n"
        f"Unit: {d.get('unit')} ({'Loose' if d.get('is_loose') else 'Packaged'})\n"
        f"Cost price: {_money(d.get('cost_price', 0))}\n"
        f"Sell price: {_money(d.get('sell_price', 0))}\n"
        f"MRP: {_money(d.get('mrp', 0))}\n"
        f"GST: {d.get('gst_rate', 0)}%\n"
        f"HSN code: {d.get('hsn_code') or '(none)'}\n"
        f"Initial stock: {d.get('initial_stock', 0)}\n"
        f"Reorder level: {d.get('reorder_level', 0)}\n\n"
        "Add this product?"
    )
    await _render(context, chat_id, text, _kb([[("\u2705 Confirm", "ai:confirm"), ("\u274c Cancel", "ai:cancel")]]), state)


async def _handle_add_item_text(chat_id, context, state, text):
    idx = state["step"]
    if idx >= len(ADD_ITEM_STEPS):
        return
    step = ADD_ITEM_STEPS[idx]

    if step["kind"] == "number":
        try:
            value = float(text.replace(",", ".").strip())
        except ValueError:
            await _render(context, chat_id, f"That doesn't look like a number. {step['prompt']}",
                          _kb([[("\u274c Cancel", "ai:cancel")]] if not step.get("skippable")
                              else [[("\u23ed Skip", f"ai:skip:{idx}")], [("\u274c Cancel", "ai:cancel")]]),
                          state, parse_mode="HTML")
            return
        if step["key"] in ("cost_price", "sell_price", "mrp") and value <= 0:
            await _render(context, chat_id, f"That must be a positive amount. {step['prompt']}",
                          _kb([[("\u274c Cancel", "ai:cancel")]]), state, parse_mode="HTML")
            return
        if step["key"] == "sell_price" and value < state["data"].get("cost_price", 0):
            await _render(context, chat_id,
                          f"Selling price can't be below the cost price ({_money(state['data'].get('cost_price', 0))}). "
                          f"{step['prompt']}",
                          _kb([[("\u274c Cancel", "ai:cancel")]]), state, parse_mode="HTML")
            return
        if step["key"] == "gst_rate" and not (0 <= value <= 100):
            await _render(context, chat_id, f"GST rate must be between 0 and 100. {step['prompt']}",
                          _kb([[("\u274c Cancel", "ai:cancel")]]), state, parse_mode="HTML")
            return
        state["data"][step["key"]] = value
    else:
        state["data"][step["key"]] = text.strip()

    state["step"] = idx + 1
    await _add_item_prompt(chat_id, context, state)


async def _handle_add_item_callback(chat_id, context, state, data, query):
    parts = data.split(":")
    action = parts[1]

    if action == "cancel":
        _clear(state)
        await _render(context, chat_id, "Cancelled \u2014 no item was added.",
                      _kb([[("\U0001f3e0 Main Menu", "m:main")]]), state)
        return

    if action == "mode":
        mode = parts[2]
        if mode == "bulk":
            await _start_add_item_bulk(chat_id, context, state)
        else:
            await _start_add_item_steps(chat_id, context, state)
        return

    if action == "confirm":
        d = state.get("data", {})
        result = await asyncio.to_thread(
            biz.add_new_product,
            d.get("name"), d.get("unit"), bool(d.get("is_loose")),
            d.get("cost_price"), d.get("sell_price"), d.get("mrp"),
            d.get("hsn_code", ""), d.get("gst_rate"),
            d.get("initial_stock", 0), d.get("reorder_level", 0),
        )
        _clear(state)
        if result["status"] == "ok":
            name = _escape_html(result["name"])
            body = f"\u2705 Added <b>{name}</b> (id {result['product_id']}) with stock <b>{result['stock_quantity']}</b>."
        else:
            body = f"\u274c Couldn't add that item: {result.get('reason')}"
        await _render(context, chat_id, body, _kb([[("\U0001f3e0 Main Menu", "m:main")]]), state, parse_mode="HTML")
        return

    idx = int(parts[2])
    if state.get("flow") != "add_item" or state.get("step") != idx:
        await query.answer("That step has already passed.", show_alert=True)
        return

    step = ADD_ITEM_STEPS[idx]
    if action == "skip":
        state["data"][step["key"]] = step.get("default")
    elif action == "v":
        raw_val = parts[3]
        if step["key"] == "is_loose":
            value = int(raw_val)
        elif step["key"] == "gst_rate":
            value = float(raw_val)
        else:
            value = raw_val
        state["data"][step["key"]] = value

    state["step"] = idx + 1
    await _add_item_prompt(chat_id, context, state)


# ---------------------------------------------------------------------------
# "New Order" flow — frequent-item grid + search, tap-to-add, live bill
# ---------------------------------------------------------------------------

async def _start_order(chat_id, context, state):
    bill = await asyncio.to_thread(biz.start_bill)
    _clear(state)
    state["flow"] = "order"
    state["bill_id"] = bill["bill_id"]
    state["view"] = "frequent"
    await _render_order_screen(chat_id, context, state)


def _order_action_rows(has_items: bool) -> list[list[tuple[str, str]]]:
    rows = [[("\U0001f50d Search product", "o:search")]]
    if has_items:
        rows.append([("\U0001f9fe Review & Pay", "o:review")])
    rows.append([("\u274c Cancel Order", "o:cancel")])
    return rows


async def _render_order_screen(chat_id, context, state):
    bill_id = state["bill_id"]
    summary = await asyncio.to_thread(biz.get_bill_summary, bill_id)
    qty_by_name = {i["product"]: i["quantity"] for i in summary["items"]}

    if state["view"] == "search":
        products = state.get("search_results", [])
        header = f"\U0001f50d Search results for \u201c{state.get('search_query', '')}\u201d:"
        empty_msg = "No products matched. Tap Search to try again."
        product_rows_prefix = [[("\u25c0 Back to frequent items", "o:back")]]
    else:
        products = state.get("frequent_cache") or (await asyncio.to_thread(biz.get_frequent_products, 8))["products"]
        state["frequent_cache"] = products
        header = "\U0001f6d2 Tap a product to add it:"
        empty_msg = "No products in the catalog yet \u2014 add one first with \u2795 Add New Item."
        product_rows_prefix = []

    state["catalog"] = {str(p["product_id"]): p["name"] for p in products}

    product_rows = []
    for p in products:
        qty = qty_by_name.get(p["name"], 0)
        label = f"{p['name']} x{qty}" if qty > 0 else f"{p['name']} \u2014 {_money(p['sell_price'])}"
        if p["stock_quantity"] <= 0:
            label += " (out of stock)"
        product_rows.append([(label, f"o:add:{p['product_id']}")])

    if summary["items"]:
        lines = "\n".join(f"\u2022 {i['product']} x{i['quantity']} \u2014 {_money(i['line_total'])}"
                           for i in summary["items"])
        bill_block = (
            f"\n\n\U0001f9fe Current order:\n{lines}\n\n"
            f"Subtotal: {_money(summary['subtotal'])}  Tax: {_money(summary['tax_total'])}\n"
            f"Total: {_money(summary['grand_total'])}"
        )
    else:
        bill_block = "\n\nNo items yet."

    text = f"{header}{bill_block}"
    if not products:
        text += f"\n\n{empty_msg}"

    keyboard_rows = product_rows_prefix + product_rows + _order_action_rows(bool(summary["items"]))
    await _render(context, chat_id, text, _kb(keyboard_rows), state)


async def _order_add_one(state, product_id: str):
    """Adds one unit of product_id to the current order bill, merging into
    an existing line (via update_bill_item_quantity) rather than creating a
    duplicate line, so repeated taps read as 'Product x2' not two lines."""
    product_name = state["catalog"].get(product_id)
    if product_name is None:
        return {"status": "error", "reason": "Unknown product \u2014 refresh the menu."}

    bill_id = state["bill_id"]
    summary = await asyncio.to_thread(biz.get_bill_summary, bill_id)
    existing = next((i for i in summary["items"] if i["product"] == product_name), None)
    if existing:
        return await asyncio.to_thread(biz.update_bill_item_quantity, existing["bill_item_id"], existing["quantity"] + 1)
    return await asyncio.to_thread(biz.add_bill_item, bill_id, product_name, 1)


async def _handle_order_search_text(chat_id, context, state, text):
    result = await asyncio.to_thread(biz.search_products, text, 8)
    state["awaiting_text"] = None
    state["view"] = "search"
    state["search_query"] = text
    state["search_results"] = result["products"]
    await _render_order_screen(chat_id, context, state)


async def _handle_order_callback(chat_id, context, state, data, query):
    parts = data.split(":")
    action = parts[1]

    # The invoice button is shown AFTER a bill is finalized, at which point
    # state["flow"] has already been cleared (there's no more active order
    # to protect) — so this action is checked before, not after, the
    # flow=="order" guard below. It only depends on last_bill_id, set at
    # the moment of finalizing.
    if action == "invoice":
        bill_id = state.get("last_bill_id")
        if not bill_id:
            await query.answer("No recent bill to invoice.", show_alert=True)
            return
        result = await asyncio.to_thread(docs.generate_invoice_pdf, bill_id)
        if result["status"] != "ok":
            await query.answer(result.get("reason", "Couldn't generate the invoice."), show_alert=True)
            return
        with open(result["file_path"], "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f,
                                             filename=f"invoice_{bill_id}.pdf")
        return

    if state.get("flow") != "order":
        await query.answer("This order isn't active anymore \u2014 use \U0001f3e0 Main Menu to start over.", show_alert=True)
        return

    if action == "add":
        result = await _order_add_one(state, parts[2])
        if result["status"] in ("refused", "error", "not_found", "ambiguous"):
            await query.answer(result.get("reason", "Couldn't add that."), show_alert=True)
        await _render_order_screen(chat_id, context, state)

    elif action == "search":
        state["awaiting_text"] = "order_search"
        await _render(context, chat_id, "Type part of the product name to search for:",
                      _kb([[("\u25c0 Back", "o:back")]]), state)

    elif action == "back":
        state["awaiting_text"] = None
        state["view"] = "frequent"
        await _render_order_screen(chat_id, context, state)

    elif action == "review":
        summary = await asyncio.to_thread(biz.get_bill_summary, state["bill_id"])
        if not summary["items"]:
            await query.answer("The order is empty \u2014 add something first.", show_alert=True)
            return
        lines = "\n".join(
            f"\u2022 {i['product']} x{i['quantity']} @ {_money(i['unit_price'])} "
            f"(GST {i['gst_rate']}% = CGST {_money(i['cgst_amount'])} + SGST {_money(i['sgst_amount'])}) "
            f"= {_money(i['line_total'])}"
            for i in summary["items"]
        )
        text = (
            f"\U0001f9fe Review order (Bill #{summary['bill_id']}):\n\n{lines}\n\n"
            f"Subtotal: {_money(summary['subtotal'])}\nTax: {_money(summary['tax_total'])}\n"
            f"Grand total: {_money(summary['grand_total'])}\n\nChoose payment method:"
        )
        rows = [
            [("\U0001f4b5 Cash", "o:pay:Cash"), ("\U0001f4f1 UPI", "o:pay:UPI"), ("\U0001f4b3 Card", "o:pay:Card")],
            [("\u25c0 Back to items", "o:back")],
            [("\u274c Cancel Order", "o:cancel")],
        ]
        await _render(context, chat_id, text, _kb(rows), state)

    elif action == "pay":
        method = parts[2]
        result = await asyncio.to_thread(
            biz.finalize_bill, state["bill_id"], method, f"menu-cbq-{query.id}", None,
        )
        if result["status"] != "ok":
            await query.answer(result.get("reason", "Couldn't finalize the bill."), show_alert=True)
            return
        bill_id = state["bill_id"]
        _clear(state)
        state["last_bill_id"] = bill_id
        text = (
            f"\u2705 Bill <b>#{bill_id}</b> finalized \u2014 <b>{method}</b>, "
            f"total <b>{_money(result['grand_total'])}</b>."
        )
        await _render(context, chat_id, text,
                      _kb([[("\U0001f4c4 Get Invoice PDF", "o:invoice")], [("\U0001f3e0 Main Menu", "m:main")]]),
                      state, parse_mode="HTML")

    elif action == "cancel":
        bill_id = state.get("bill_id")
        if bill_id:
            await asyncio.to_thread(biz.cancel_bill, bill_id)
        _clear(state)
        await _render(context, chat_id, "Order cancelled.", _kb([[("\U0001f3e0 Main Menu", "m:main")]]), state)


# ---------------------------------------------------------------------------
# "Add Stock" flow — search/pick an existing product, then qty (+ optional
# cost/MRP update)
# ---------------------------------------------------------------------------

async def _start_add_stock(chat_id, context, state):
    _clear(state)
    state["flow"] = "add_stock"
    products = (await asyncio.to_thread(biz.get_frequent_products, 10))["products"]
    state["catalog"] = {str(p["product_id"]): p["name"] for p in products}

    rows = [[(f"{p['name']} ({p['stock_quantity']} {p['unit']} left)", f"as:pick:{p['product_id']}")]
            for p in products]
    rows.append([("\U0001f50d Search product", "as:search")])
    rows.append([("\u274c Cancel", "m:main")])

    header = "\U0001f4e5 Which product are you restocking?"
    if products:
        header += " Tap one below (your 10 most frequently sold), or search by name:"
    else:
        header += " No products in the catalog yet \u2014 add one first, or search by name:"
    await _render(context, chat_id, header, _kb(rows), state)


async def _start_add_stock_search(chat_id, context, state):
    state["awaiting_text"] = "add_stock_search"
    await _render(context, chat_id, "\U0001f4e5 Type part of the product's name:",
                  _kb([[("\u274c Cancel", "m:main")]]), state)


async def _handle_add_stock_search_text(chat_id, context, state, text):
    result = await asyncio.to_thread(biz.search_products, text, 8)
    products = result["products"]
    state["awaiting_text"] = None
    if not products:
        state["awaiting_text"] = "add_stock_search"
        await _render(context, chat_id, f"No product matched \u201c{text}\u201d \u2014 try another name:",
                      _kb([[("\u274c Cancel", "m:main")]]), state)
        return
    state["catalog"] = {str(p["product_id"]): p["name"] for p in products}
    rows = [[(f"{p['name']} ({p['stock_quantity']} {p['unit']} left)", f"as:pick:{p['product_id']}")]
            for p in products]
    rows.append([("\u274c Cancel", "m:main")])
    await _render(context, chat_id, "Which one?", _kb(rows), state)


async def _handle_add_stock_callback(chat_id, context, state, data, query):
    if state.get("flow") != "add_stock":
        await query.answer("This flow isn't active anymore.", show_alert=True)
        return
    parts = data.split(":")
    action = parts[1]

    if action == "search":
        await _start_add_stock_search(chat_id, context, state)
        return

    if action == "pick":
        product_name = state["catalog"].get(parts[2])
        if not product_name:
            await query.answer("Unknown product \u2014 start over.", show_alert=True)
            return
        state["product_name"] = product_name
        state["awaiting_text"] = "add_stock_qty"
        await _render(context, chat_id, f"How many units of {product_name} came in?",
                      _kb([[("\u274c Cancel", "m:main")]]), state)


async def _handle_add_stock_qty_text(chat_id, context, state, text):
    try:
        qty = float(text.replace(",", ".").strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        await _render(context, chat_id, "That should be a positive number. How many units came in?",
                      _kb([[("\u274c Cancel", "m:main")]]), state)
        return
    result = await asyncio.to_thread(biz.add_stock, state["product_name"], qty)
    await _finish_add_stock(chat_id, context, state, result)


async def _finish_add_stock(chat_id, context, state, result):
    _clear(state)
    if result["status"] == "ok":
        name = _escape_html(result["name"])
        body = (
            f"\u2705 Added <b>{result['added']}</b> to <b>{name}</b>. New stock: <b>{result['new_stock_quantity']}</b>.\n\n"
            "Want to change the cost price, selling price, or MRP? Just type or send a voice note "
            f"saying so \u2014 e.g. <b>\u201cchange cost price of {name} to 12\u201d</b>."
        )
    else:
        body = f"\u274c Couldn't add stock: {result.get('reason')}"
    await _render(context, chat_id, body, _kb([[("\U0001f3e0 Main Menu", "m:main")]]), state, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Entry points used by telegram_bot/bot_gemini.py
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data or ""

    async with _get_lock(chat_id):
        state = FORM_STATE.setdefault(chat_id, {})
        try:
            if data == "m:main":
                _clear(state)
                await _show_main_menu(chat_id, context, state)
            elif data == "m:neworder":
                await _start_order(chat_id, context, state)
            elif data == "m:additem":
                await _start_add_item(chat_id, context, state)
            elif data == "m:addstock":
                await _start_add_stock(chat_id, context, state)
            elif data == "m:checkstock":
                await _start_check_stock(chat_id, context, state)
            elif data == "m:lowstock":
                await _show_low_stock(chat_id, context, state)
            elif data == "m:summary":
                await _show_summary(chat_id, context, state)
            elif data == "m:weeklydeck":
                await _show_weekly_deck(chat_id, context, state)
            elif data == "m:khata":
                await _show_khata_total(chat_id, context, state)
            elif data.startswith("ai:"):
                await _handle_add_item_callback(chat_id, context, state, data, query)
            elif data.startswith("o:"):
                await _handle_order_callback(chat_id, context, state, data, query)
            elif data.startswith("as:"):
                await _handle_add_stock_callback(chat_id, context, state, data, query)
            elif data.startswith("cs:"):
                await _handle_check_stock_callback(chat_id, context, state, data, query)
            elif data.startswith("sm:"):
                await _handle_summary_callback(chat_id, context, state, data, query)
            elif data.startswith("kh:"):
                await _handle_khata_callback(chat_id, context, state, data, query)
            elif data.startswith("wd:"):
                await _handle_weekly_deck_callback(chat_id, context, state, data, query)
        except Exception:
            logger.exception("Menu callback failed for chat %s (data=%s)", chat_id, data)
            _clear(state)
            await context.bot.send_message(chat_id, "Something went wrong on that step \u2014 use /menu to start over.")


async def handle_pending_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Called from bot_gemini.py's handle_message() BEFORE anything is sent to
    Gemini. Returns True if this text was consumed by an in-progress form
    (a price, a search query, a customer name, etc.); the caller must then
    stop and not forward the message to the chat/LLM. Returns False for
    every ordinary chat message, leaving normal text/voice handling
    completely untouched.
    """
    if update.message is None or update.message.text is None:
        return False
    chat_id = update.effective_chat.id
    state = FORM_STATE.get(chat_id)
    if not state or not state.get("awaiting_text"):
        return False

    async with _get_lock(chat_id):
        state = FORM_STATE.get(chat_id)
        if not state or not state.get("awaiting_text"):
            return False
        awaiting = state["awaiting_text"]
        text = update.message.text.strip()
        try:
            if awaiting == "add_item":
                await _handle_add_item_text(chat_id, context, state, text)
            elif awaiting == "add_item_bulk":
                await _handle_add_item_bulk_text(chat_id, context, state, text)
            elif awaiting == "order_search":
                await _handle_order_search_text(chat_id, context, state, text)
            elif awaiting == "add_stock_search":
                await _handle_add_stock_search_text(chat_id, context, state, text)
            elif awaiting == "add_stock_qty":
                await _handle_add_stock_qty_text(chat_id, context, state, text)
            elif awaiting == "check_stock_search":
                await _handle_check_stock_search_text(chat_id, context, state, text)
            elif awaiting == "khata_search":
                await _handle_khata_search_text(chat_id, context, state, text)
            else:
                return False
        except Exception:
            logger.exception("Menu form text handling failed for chat %s", chat_id)
            _clear(state)
            await context.bot.send_message(chat_id, "Something went wrong on that step \u2014 use /menu to start over.")
    return True
