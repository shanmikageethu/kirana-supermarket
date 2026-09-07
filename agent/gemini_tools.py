"""
agent/gemini_tools.py
------------------------
Tool declarations + dispatch table for a hand-rolled ("deep agent" style)
agent loop, used instead of the Claude Agent SDK version in agent_tools.py,
so this project can run on Google Gemini's free tier.

The JSON-schema tool declarations below are UNCHANGED from the Claude
version — Gemini uses the same OpenAPI-style schema (type/properties/
required/enum), so nothing was lost porting them over.

What's different from the Claude Agent SDK version:
- No SDK-managed tool loop. telegram_bot/bot_gemini.py implements the
  observe -> reason -> act -> observe loop itself: send message + tools,
  check the response for function_call parts, run the matching Python
  function below, send the result back, repeat until Gemini just replies
  with text. This IS the "deep agent" harness option named in the brief.
- Dispatch functions here are plain sync functions (not async @tool
  handlers) — bot_gemini.py wraps each call in asyncio.to_thread itself
  when it invokes them, same reasoning as before: don't block the event
  loop on a blocking sqlite call.
- finalize_bill's idempotency key is still injected server-side, never
  chosen by the model, via the same closure-factory pattern as the
  Claude version and for the exact same reason (see create_finalize_bill_dispatch).
"""

from typing import Any, Callable

import tools.store_tools as biz
import tools.documents as docs


# ---------------------------------------------------------------------------
# Tool declarations — same JSON schema as the Claude Agent SDK version
# ---------------------------------------------------------------------------

TOOL_DECLARATIONS = [
    {
        "name": "add_new_product",
        "description": (
            "Register a brand-new product/SKU that does not exist in the catalog yet. "
            "Use this only after check_stock confirms the product truly doesn't exist. "
            "Ask the owner for gst_rate and hsn_code if they weren't given — never guess them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Product name, e.g. 'Amul Butter 100g'"},
                "unit": {"type": "string", "enum": ["kg", "g", "litre", "ml", "packet", "dozen", "piece"]},
                "is_loose": {"type": "boolean", "description": "True for loose items sold by weight/volume"},
                "cost_price": {"type": "number"},
                "sell_price": {"type": "number"},
                "mrp": {"type": "number"},
                "hsn_code": {"type": "string"},
                "gst_rate": {"type": "number", "description": "GST percent, e.g. 0, 5, 12, 18"},
                "initial_stock": {"type": "number"},
                "reorder_level": {"type": "number"},
            },
            "required": ["name", "unit", "is_loose", "cost_price", "sell_price", "mrp", "hsn_code", "gst_rate"],
        },
    },
    {
        "name": "add_stock",
        "description": (
            "Receive new stock for a product that ALREADY exists in the catalog, e.g. "
            "'50 packets of Maggi came in, cost 12, MRP 14'. If this returns "
            "status 'not_found', the product is new — call add_new_product instead, "
            "after asking the owner for GST rate and HSN code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string"},
                "qty": {"type": "number"},
                "cost_price": {"type": "number"},
                "mrp": {"type": "number"},
            },
            "required": ["product_name", "qty"],
        },
    },
    {
    "name": "check_stock",
    "description": (
        "Look up everything known about a product by name — stock quantity, "
        "sell price, MRP, GST rate, HSN code, and reorder level. Use this for "
        "grounding before answering ANY factual question about an existing "
        "product (stock, price, GST, HSN, etc.) — never state such a fact "
        "from memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {"product_name": {"type": "string"}},
        "required": ["product_name"],
    },
},
    {
        "name": "get_low_stock",
        "description": "List all products at or below their reorder level, for 'what's running out?'.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_reorder_suggestions",
        "description": (
            "Project which products will run out soon based on their actual recent "
            "sales pace (not just the static reorder_level). Use for 'what should I "
            "reorder?' or 'anything about to run out?'. Returns only items genuinely "
            "trending toward zero stock, each with a suggested reorder quantity."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "lookback_days": {
                    "type": "integer",
                    "description": "How many past days of sales to compute velocity from. Defaults to 14.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "start_bill",
        "description": "Open a new draft bill before adding items to it. Call this once at the start of a 'make a bill' request.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "add_bill_item",
        "description": (
            "Add one product line to a draft bill. Enforces the oversell guard: refused "
            "if the store doesn't have enough stock. Does NOT change stock yet — stock "
            "only changes when the bill is finalized."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "product_name": {"type": "string"},
                "quantity": {"type": "number"},
            },
            "required": ["bill_id", "product_name", "quantity"],
        },
    },
    {
        "name": "update_bill_item_quantity",
        "description": (
            "Change the quantity of an existing line on a draft bill, e.g. 'make it 6 Maggi'. "
            "Get the bill_item_id from get_bill_summary first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bill_item_id": {"type": "integer"},
                "new_quantity": {"type": "number"},
            },
            "required": ["bill_item_id", "new_quantity"],
        },
    },
    {
        "name": "remove_bill_item",
        "description": (
            "Remove a line from a draft bill, e.g. 'drop the butter'. "
            "Get the bill_item_id from get_bill_summary first."
        ),
        "parameters": {
            "type": "object",
            "properties": {"bill_item_id": {"type": "integer"}},
            "required": ["bill_item_id"],
        },
    },
    {
    "name": "get_bill_summary",
    "description": (
        "Read back the current items, quantities and totals on a bill. Use this "
        "before finalize_bill to confirm the order with the owner, and whenever "
        "you need a bill_item_id to edit or remove a line. When showing this to "
        "the owner in chat, always show the CGST and SGST split per item (from "
        "cgst_amount/sgst_amount), not just one lump tax figure — a legible tax "
        "breakup is required, not just the grand total."
    ),
    "parameters": {
        "type": "object",
        "properties": {"bill_id": {"type": "integer"}},
        "required": ["bill_id"],
    },
},
    {
    "name": "cancel_bill",
    "description": (
        "Abandon a draft bill the owner no longer wants, e.g. 'cancel this bill', "
        "'never mind, start over'. Only works on drafts — refuses on an already-"
        "finalized bill, since that's a real sale record."
    ),
    "parameters": {
        "type": "object",
        "properties": {"bill_id": {"type": "integer"}},
        "required": ["bill_id"],
    },
},
    {
        "name": "finalize_bill",
        "description": (
            "Lock in a completed bill: decrements stock and marks it paid. Only call "
            "this once the owner has confirmed the items and given a payment method. "
            "Safe to call again if unsure whether it already ran — repeated calls "
            "for the same incoming message will not double-charge or double-decrement stock."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "payment_method": {"type": "string", "enum": ["Cash", "UPI", "Card"]},
                "payment_reference": {"type": "string", "description": "UPI transaction ID or card auth code, if any"},
            },
            "required": ["bill_id", "payment_method"],
        },
    },
    {
        "name": "add_khata_charge",
        "description": (
            "Add credit for a customer, e.g. 'put ₹500 on Ramesh's credit'. "
            "Creates the customer's khata record if this is their first charge."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "amount": {"type": "number"},
                "description": {"type": "string"},
            },
            "required": ["customer_name", "amount"],
        },
    },
    {
        "name": "record_khata_payment",
        "description": (
            "Record a customer paying down their khata balance, e.g. 'Ramesh paid ₹300'. "
            "Refused if the customer has no existing khata record."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "amount": {"type": "number"},
                "description": {"type": "string"},
            },
            "required": ["customer_name", "amount"],
        },
    },
    {
        "name": "get_khata_balance",
        "description": "Look up a customer's current khata (credit) balance, e.g. \"Ramesh's balance?\"",
        "parameters": {
            "type": "object",
            "properties": {"customer_name": {"type": "string"}},
            "required": ["customer_name"],
        },
    },
    {
        "name": "get_daily_summary",
        "description": (
            "Get total sales, tax collected, payment-method breakdown and top items "
            "for a given date. Defaults to today if no date is given. "
            "Use for 'today's sales?' or 'close the day'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
            },
            "required": [],
        },
    },
    {
        "name": "set_preference",
        "description": (
            "Save a standing preference for the owner, e.g. default payment method, "
            "preferred brand, shop name or GSTIN for invoices. Persists across chats."
        ),
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["key", "value"],
        },
    },
    {
        "name": "get_preference",
        "description": "Look up a single stored preference by key.",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "generate_invoice_pdf",
        "description": (
            "Generate a downloadable PDF invoice for an already-finalized bill. "
            "Refuses if the bill is still a draft — finalize it first."
        ),
        "parameters": {
            "type": "object",
            "properties": {"bill_id": {"type": "integer"}},
            "required": ["bill_id"],
        },
    },
    {
        "name": "generate_sales_deck",
        "description": (
            "Generate a downloadable PPTX sales-analysis deck (charts + insights) for a "
            "date range. Defaults to the last 7 days if no dates are given — use this "
            "for 'this week's sales', 'sales analysis', or similar requests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD, defaults to 7 days before end_date"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
            },
            "required": [],
        },
    },
    {
        "name": "generate_weekly_review_deck",
        "description": (
            "Generate a downloadable PPTX 'Weekly Business Review' \u2014 a DIFFERENT, more "
            "forward-looking document than generate_sales_deck. Covers the multi-week "
            "revenue trend, which products are trending up or down vs last week, reorder "
            "priorities for next week, customers to follow up with on khata, and a short "
            "action-items list. Use this for 'weekly review', 'what should I do next "
            "week', or 'send me the business review', not for a plain sales summary."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "end_date": {"type": "string", "description": "YYYY-MM-DD, last day of the review week; defaults to today"},
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatch table — everything except finalize_bill (needs per-chat state)
# ---------------------------------------------------------------------------

def _dispatch_add_new_product(args):
    return biz.add_new_product(
        args["name"], args["unit"], args["is_loose"],
        args["cost_price"], args["sell_price"], args["mrp"],
        args["hsn_code"], args["gst_rate"],
        args.get("initial_stock", 0), args.get("reorder_level", 0),
    )


def _dispatch_add_stock(args):
    return biz.add_stock(args["product_name"], args["qty"], args.get("cost_price"), args.get("mrp"))


def _dispatch_check_stock(args):
    return biz.check_stock(args["product_name"])


def _dispatch_get_low_stock(args):
    return biz.get_low_stock()


def _dispatch_get_reorder_suggestions(args):
    return biz.get_reorder_suggestions(args.get("lookback_days", 14))


def _dispatch_start_bill(args):
    return biz.start_bill()


def _dispatch_add_bill_item(args):
    return biz.add_bill_item(args["bill_id"], args["product_name"], args["quantity"])


def _dispatch_update_bill_item_quantity(args):
    return biz.update_bill_item_quantity(args["bill_item_id"], args["new_quantity"])


def _dispatch_remove_bill_item(args):
    return biz.remove_bill_item(args["bill_item_id"])


def _dispatch_get_bill_summary(args):
    return biz.get_bill_summary(args["bill_id"])

def _dispatch_cancel_bill(args):
    return biz.cancel_bill(args["bill_id"])


def _dispatch_add_khata_charge(args):
    return biz.add_khata_charge(args["customer_name"], args["amount"], args.get("description"))


def _dispatch_record_khata_payment(args):
    return biz.record_khata_payment(args["customer_name"], args["amount"], args.get("description"))


def _dispatch_get_khata_balance(args):
    return biz.get_khata_balance(args["customer_name"])


def _dispatch_get_daily_summary(args):
    return biz.get_daily_summary(args.get("target_date"))


def _dispatch_set_preference(args):
    return biz.set_preference(args["key"], args["value"])


def _dispatch_get_preference(args):
    return biz.get_preference(args["key"])


def _dispatch_generate_invoice_pdf(args):
    return docs.generate_invoice_pdf(args["bill_id"])


def _dispatch_generate_sales_deck(args):
    return docs.generate_sales_deck(args.get("start_date"), args.get("end_date"))


def _dispatch_generate_weekly_review_deck(args):
    return docs.generate_weekly_review_deck(args.get("end_date"))


SHARED_DISPATCH: dict[str, Callable[[dict], dict]] = {
    "add_new_product": _dispatch_add_new_product,
    "add_stock": _dispatch_add_stock,
    "check_stock": _dispatch_check_stock,
    "get_low_stock": _dispatch_get_low_stock,
    "get_reorder_suggestions": _dispatch_get_reorder_suggestions,
    "start_bill": _dispatch_start_bill,
    "add_bill_item": _dispatch_add_bill_item,
    "update_bill_item_quantity": _dispatch_update_bill_item_quantity,
    "remove_bill_item": _dispatch_remove_bill_item,
    "get_bill_summary": _dispatch_get_bill_summary,
    "cancel_bill": _dispatch_cancel_bill,
    "add_khata_charge": _dispatch_add_khata_charge,
    "record_khata_payment": _dispatch_record_khata_payment,
    "get_khata_balance": _dispatch_get_khata_balance,
    "get_daily_summary": _dispatch_get_daily_summary,
    "set_preference": _dispatch_set_preference,
    "get_preference": _dispatch_get_preference,
    "generate_invoice_pdf": _dispatch_generate_invoice_pdf,
    "generate_sales_deck": _dispatch_generate_sales_deck,
    "generate_weekly_review_deck": _dispatch_generate_weekly_review_deck,
}


def build_dispatch_for_chat(get_update_id: Callable[[], str]) -> dict[str, Callable[[dict], dict]]:
    """
    Call this ONCE per Telegram chat. Returns a dispatch table including a
    finalize_bill entry whose idempotency key is scoped to THIS chat only,
    via the get_update_id closure — same reasoning as the Claude version's
    create_finalize_bill_tool.
    """
    def _dispatch_finalize_bill(args):
        key = get_update_id()
        return biz.finalize_bill(args["bill_id"], args["payment_method"], key, args.get("payment_reference"))

    return {**SHARED_DISPATCH, "finalize_bill": _dispatch_finalize_bill}
