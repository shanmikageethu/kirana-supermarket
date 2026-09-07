"""
tools.py
----------
Plain Python functions implementing the Supermarket Ops Agent's business logic.

IMPORTANT DESIGN NOTES:
- Every function returns a plain dict, e.g. {"status": "ok", ...} or
  {"status": "error", "reason": "..."}. This is what your agent SDK will
  wrap as a "tool result" and feed back to the model — so keep the fields
  simple and descriptive, the model reads them directly.
- Every write operation runs inside a SQLite transaction using BEGIN IMMEDIATE,
  which takes a write lock immediately. This is what prevents two concurrent
  operations (e.g. two sales, or a sale + a stock-in) from corrupting stock.
  SQLite only allows one writer at a time anyway, but BEGIN IMMEDIATE makes
  the lock explicit and fails fast (SQLITE_BUSY) instead of silently
  interleaving reads and writes.
- Stock is NEVER decremented in add_bill_item. It's only decremented in
  finalize_bill, and only once per bill (protected by idempotency_key).
- No function exists to delete or forcibly reduce stock other than through
  a finalized sale. This is deliberate — it's the "don't delete stock"
  guardrail. If you need stock corrections later, add an explicit
  adjust_stock tool with its own audit trail; don't repurpose add_stock.

Run this once before anything else:
    ALTER TABLE bills ADD COLUMN idempotency_key TEXT UNIQUE;
"""

import difflib
import json
from datetime import datetime, date, timedelta

from database.database import get_db, round2 as _round2
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Product lookup helper (shared by many tools)
# ---------------------------------------------------------------------------

def _find_products(conn, product_name):
    """
    Case-insensitive partial match on product name — the fast path that
    handles typed input and most spoken input fine.

    Falls back to fuzzy whole-name matching (via difflib) ONLY when that
    substring search finds nothing at all. This exists for voice input
    specifically: a Tamil/Hindi-accented pronunciation of an English loan
    word often gets transcribed with a different spelling than what's in
    the catalog (e.g. "aata" for "Atta", "dhal" for "Dal") — a plain
    substring search misses that entirely and reports not_found, at which
    point the agent (per its system prompt) can't suggest the real product
    since it's forbidden from guessing names that weren't in a tool
    result. Surfacing the close match here as a normal search result lets
    the existing "ambiguous — ask which one" flow handle it correctly,
    without ever inventing a product that doesn't actually exist.

    Returns the raw list of matching rows — callers decide what to do
    with 0 / 1 / many matches. See _find_products_with_fuzzy_flag if the
    caller needs to know whether a single match came from the fuzzy
    fallback (billing/stock writes should treat that as needing
    confirmation, not an auto-resolved match).
    """
    return _find_products_with_fuzzy_flag(conn, product_name)[0]


def _find_products_with_fuzzy_flag(conn, product_name):
    rows = conn.execute(
        "SELECT * FROM products WHERE name LIKE ? ORDER BY name",
        (f"%{product_name}%",)
    ).fetchall()
    if rows:
        return rows, False

    all_products = conn.execute("SELECT * FROM products").fetchall()
    if not all_products:
        return [], False
    by_lower_name = {r["name"].lower(): r for r in all_products}
    close = difflib.get_close_matches(product_name.lower(), by_lower_name.keys(), n=5, cutoff=0.55)
    return [by_lower_name[n] for n in close], bool(close)


def _resolve_single_product(conn, product_name):
    """
    Returns (product_row, error_dict). Exactly one of the two is None.
    Use this inside tools that need exactly one product to proceed.

    A single match that only turned up via the fuzzy spelling fallback
    (see _find_products) is deliberately treated as "ambiguous" rather
    than auto-resolved — this is a write path (stock/billing), so a
    likely-but-unconfirmed name match should prompt the owner to confirm
    once, not silently act on a guess.
    """
    matches, was_fuzzy = _find_products_with_fuzzy_flag(conn, product_name)
    if len(matches) == 0:
        return None, {
            "status": "not_found",
            "reason": f"No product matching '{product_name}' exists in the catalog.",
        }
    if len(matches) > 1 or was_fuzzy:
        return None, {
            "status": "ambiguous",
            "reason": f"'{product_name}' matches more than one product. Ask the user which one.",
            "candidates": [dict(r) for r in matches],
        }
    return matches[0], None


# ---------------------------------------------------------------------------
# 1. Inventory / product catalog tools
# ---------------------------------------------------------------------------

def add_new_product(name, unit, is_loose, cost_price, sell_price, mrp,
                     hsn_code, gst_rate, initial_stock=0, reorder_level=0):
    """
    Registers a brand-new SKU. Use this only when check_stock / add_stock
    report the product doesn't exist yet.
    """
    if sell_price < cost_price:
        return {"status": "error", "reason": "sell_price cannot be below cost_price."}

    with get_db(exclusive=True) as conn:
        existing = _find_products(conn, name)
        exact = [r for r in existing if r["name"].lower() == name.lower()]
        if exact:
            return {"status": "error", "reason": f"Product '{name}' already exists (id {exact[0]['product_id']})."}

        cur = conn.execute(
            """INSERT INTO products
               (name, unit, is_loose, cost_price, sell_price, mrp,
                stock_quantity, reorder_level, hsn_code, gst_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, unit, int(is_loose), cost_price, sell_price, mrp,
             initial_stock, reorder_level, hsn_code, gst_rate)
        )
        return {
            "status": "ok",
            "product_id": cur.lastrowid,
            "name": name,
            "stock_quantity": initial_stock,
        }


def add_stock(product_name, qty, cost_price=None, mrp=None):
    """
    Receiving stock for an EXISTING product, e.g.
    "50 packets of Maggi came in, cost ₹12, MRP ₹14".
    cost_price and mrp are independent and optional — pass either, both,
    or neither. Any left as None keeps the product's current value
    unchanged (e.g. update just the cost price and leave MRP as-is).
    If the product truly doesn't exist yet, this returns not_found —
    the agent should then call add_new_product (after asking the owner
    for GST rate / HSN / unit, which add_stock has no business inventing).
    """
    if qty <= 0:
        return {"status": "error", "reason": "qty must be positive."}

    with get_db(exclusive=True) as conn:
        product, err = _resolve_single_product(conn, product_name)
        if err:
            return err

        new_qty = product["stock_quantity"] + qty
        new_cost_price = cost_price if cost_price is not None else product["cost_price"]
        new_mrp = mrp if mrp is not None else product["mrp"]
        conn.execute(
            "UPDATE products SET stock_quantity = ?, cost_price = ?, mrp = ? WHERE product_id = ?",
            (new_qty, new_cost_price, new_mrp, product["product_id"])
        )

        return {
            "status": "ok",
            "product_id": product["product_id"],
            "name": product["name"],
            "added": qty,
            "new_stock_quantity": new_qty,
        }


def check_stock(product_name):
    """Stock/product query, e.g. 'how much sugar is left?', 'what's the GST on Amul Butter?'.
    Pure grounding — no writes. Returns everything known about the product,
    not just stock, so this is the right tool for price/GST/HSN questions too."""
    with get_db(exclusive=False) as conn:
        matches = _find_products(conn, product_name)
        if not matches:
            return {"status": "not_found", "reason": f"No product matching '{product_name}'."}
        return {
            "status": "ok",
            "matches": [
                {
                    "product_id": r["product_id"],
                    "name": r["name"],
                    "unit": r["unit"],
                    "stock_quantity": r["stock_quantity"],
                    "reorder_level": r["reorder_level"],
                    "sell_price": r["sell_price"],
                    "mrp": r["mrp"],
                    "cost_price": r["cost_price"],
                    "gst_rate": r["gst_rate"],
                    "hsn_code": r["hsn_code"],
                }
                for r in matches
            ],
        }


def get_low_stock():
    """'What's running out?' — items at or below their reorder level."""
    with get_db(exclusive=False) as conn:
        rows = conn.execute(
            "SELECT * FROM products WHERE stock_quantity <= reorder_level ORDER BY stock_quantity ASC"
        ).fetchall()
        return {
            "status": "ok",
            "low_stock_items": [
                {
                    "product_id": r["product_id"],
                    "name": r["name"],
                    "stock_quantity": r["stock_quantity"],
                    "reorder_level": r["reorder_level"],
                    "unit": r["unit"],
                }
                for r in rows
            ],
        }


# Products projected to run out within this many days are flagged, even if
# their stock is still above the owner-set reorder_level. reorder_level is a
# static floor the owner picked once; this is a live projection from actual
# recent sales pace, so it catches a fast mover BEFORE it crosses that floor.
REORDER_HORIZON_DAYS = 5


def get_reorder_suggestions(lookback_days=14):
    """
    'What should I reorder soon?' — projects, from each product's actual
    sales pace over the lookback window, which ones will run out within
    REORDER_HORIZON_DAYS days.

    Velocity (units/day) is computed ONLY from FINALIZED bills — a draft
    bill's items were never actually sold. Products with zero sales in the
    window are skipped entirely: with no observed velocity there's nothing
    real to project from, and flagging them would just be a guess dressed
    up as a number.
    """
    if lookback_days <= 0:
        return {"status": "error", "reason": "lookback_days must be positive."}

    cutoff = (datetime.now() - timedelta(days=lookback_days)).isoformat()
    with get_db(exclusive=False) as conn:
        rows = conn.execute(
            """SELECT p.product_id, p.name, p.unit, p.stock_quantity, p.reorder_level,
                      COALESCE(SUM(bi.quantity), 0) AS qty_sold
               FROM products p
               LEFT JOIN bill_items bi ON bi.product_id = p.product_id
               LEFT JOIN bills b ON b.bill_id = bi.bill_id
                    AND b.status = 'finalized' AND b.finalized_at >= ?
               GROUP BY p.product_id""",
            (cutoff,)
        ).fetchall()

        suggestions = []
        for r in rows:
            velocity = r["qty_sold"] / lookback_days
            if velocity <= 0:
                continue
            days_left = _round2(r["stock_quantity"] / velocity)
            if days_left > REORDER_HORIZON_DAYS:
                continue
            # Suggest enough to cover another full lookback window at this
            # pace, but never less than the owner's own reorder_level —
            # that floor reflects something about this product (shelf
            # space, supplier minimums) a sales-velocity formula can't see.
            suggested_qty = max(r["reorder_level"], _round2(velocity * lookback_days))
            suggestions.append({
                "product_id": r["product_id"],
                "name": r["name"],
                "unit": r["unit"],
                "stock_quantity": r["stock_quantity"],
                "avg_daily_sales": _round2(velocity),
                "days_of_stock_left": days_left,
                "suggested_reorder_qty": suggested_qty,
            })

        suggestions.sort(key=lambda s: s["days_of_stock_left"])
        return {
            "status": "ok",
            "lookback_days": lookback_days,
            "reorder_horizon_days": REORDER_HORIZON_DAYS,
            "suggestions": suggestions,
        }


def get_frequent_products(limit=8):
    """
    'Frequently purchased' shortlist for the quick-order button grid in
    Telegram (see telegram_bot/menus.py). Ranked by total quantity sold
    across all finalized bills, all-time — a shop's regulars don't drift
    week to week, so there's no reason to window this like sales velocity.

    Cold start (no sales yet): every product has qty_sold=0, so the ORDER
    BY falls through to alphabetical — a brand-new store still gets a
    usable quick-order grid on day one instead of an empty screen.
    """
    with get_db(exclusive=False) as conn:
        rows = conn.execute(
            """SELECT p.product_id, p.name, p.unit, p.sell_price, p.stock_quantity,
                      p.reorder_level,
                      COALESCE(SUM(bi.quantity), 0) AS qty_sold
               FROM products p
               LEFT JOIN bill_items bi ON bi.product_id = p.product_id
               LEFT JOIN bills b ON b.bill_id = bi.bill_id AND b.status = 'finalized'
               GROUP BY p.product_id
               ORDER BY qty_sold DESC, p.name ASC
               LIMIT ?""",
            (limit,)
        ).fetchall()
        return {
            "status": "ok",
            "products": [
                {
                    "product_id": r["product_id"], "name": r["name"], "unit": r["unit"],
                    "sell_price": r["sell_price"], "stock_quantity": r["stock_quantity"],
                    "reorder_level": r["reorder_level"], "qty_sold": r["qty_sold"],
                }
                for r in rows
            ],
        }


def search_products(query, limit=8):
    """
    Plain product search for the Telegram search-and-tap quick-order flow.
    Unlike check_stock (built for the model to read and reason about),
    this never returns "ambiguous"/"not_found" framing — a human tapping
    through a button UI can just look at a list and pick one, or see it's
    empty. Returns at most `limit` matches, ordered alphabetically.
    """
    with get_db(exclusive=False) as conn:
        rows = _find_products(conn, query)
        return {
            "status": "ok",
            "products": [
                {
                    "product_id": r["product_id"], "name": r["name"], "unit": r["unit"],
                    "sell_price": r["sell_price"], "stock_quantity": r["stock_quantity"],
                    "reorder_level": r["reorder_level"],
                }
                for r in rows[:limit]
            ],
        }


# ---------------------------------------------------------------------------
# 2. Billing tools — multi-turn bill building
# ---------------------------------------------------------------------------

def _recalculate_bill_totals(conn, bill_id):
    """Recomputes subtotal / tax_total / grand_total from bill_items. Called
    after every add/update/remove so the bill row is always consistent."""
    items = conn.execute(
        "SELECT quantity, unit_price, gst_amount FROM bill_items WHERE bill_id = ?",
        (bill_id,)
    ).fetchall()
    subtotal = sum(i["quantity"] * i["unit_price"] for i in items)
    tax_total = sum(i["gst_amount"] for i in items)
    subtotal = _round2(subtotal)
    tax_total = _round2(tax_total)
    grand_total = _round2(subtotal + tax_total)
    conn.execute(
        "UPDATE bills SET subtotal = ?, tax_total = ?, grand_total = ? WHERE bill_id = ?",
        (subtotal, tax_total, grand_total, bill_id)
    )
    return subtotal, tax_total, grand_total


def start_bill():
    """Opens a new draft bill. Nothing is committed to stock yet."""
    with get_db(exclusive=True) as conn:
        cur = conn.execute("INSERT INTO bills (status) VALUES ('draft')")
        return {"status": "ok", "bill_id": cur.lastrowid}


def add_bill_item(bill_id, product_name, quantity):
    """
    Adds one line to a draft bill. This is where the OVERSELL GUARD lives:
    it checks requested qty (this line + anything already on this same
    draft bill for the same product) against real stock on hand, and
    refuses if it's not enough. Stock itself is NOT touched here.
    """
    if quantity <= 0:
        return {"status": "error", "reason": "quantity must be positive."}

    with get_db(exclusive=True) as conn:
        bill = conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,)).fetchone()
        if bill is None:
            return {"status": "error", "reason": f"No bill with id {bill_id}."}
        if bill["status"] != "draft":
            return {"status": "error", "reason": f"Bill {bill_id} is '{bill['status']}', not editable."}

        product, err = _resolve_single_product(conn, product_name)
        if err:
            return err

        already_on_bill = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS q FROM bill_items WHERE bill_id = ? AND product_id = ?",
            (bill_id, product["product_id"])
        ).fetchone()["q"]

        if already_on_bill + quantity > product["stock_quantity"]:
            return {
                "status": "refused",
                "reason": (
                    f"Only {product['stock_quantity']} {product['unit']} of "
                    f"{product['name']} in stock; this bill would need "
                    f"{already_on_bill + quantity}."
                ),
                "available": product["stock_quantity"],
            }

        gst_rate = product["gst_rate"]
        unit_price = product["sell_price"]
        line_subtotal = _round2(unit_price * quantity)
        gst_amount = _round2(line_subtotal * gst_rate / 100)
        line_total = _round2(line_subtotal + gst_amount)

#         logger.info(
#     "Product pricing: gst_rate=%.2f%%, unit_price=%.2f, quantity=%s, "
#     "line_subtotal=%.2f, gst_amount=%.2f, line_total=%.2f",
#     gst_rate,
#     unit_price,
#     quantity,
#     line_subtotal,
#     gst_amount,
#     line_total
# )
        
        cur = conn.execute(
            """INSERT INTO bill_items
               (bill_id, product_id, quantity, unit_price, gst_rate, gst_amount, line_total)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bill_id, product["product_id"], quantity, unit_price, gst_rate, gst_amount, line_total)
        )
        subtotal, tax_total, grand_total = _recalculate_bill_totals(conn, bill_id)

        return {
            "status": "ok",
            "bill_item_id": cur.lastrowid,
            "product": product["name"],
            "quantity": quantity,
            "line_total": line_total,
            "bill_subtotal": subtotal,
            "bill_tax_total": tax_total,
            "bill_grand_total": grand_total,
        }


def update_bill_item_quantity(bill_item_id, new_quantity):
    """'make it 6 Maggi' — changes an existing line's quantity, re-checking
    the oversell guard against current stock."""
    if new_quantity <= 0:
        return {"status": "error", "reason": "Use remove_bill_item to remove a line, not quantity 0."}

    with get_db(exclusive=True) as conn:
        item = conn.execute("SELECT * FROM bill_items WHERE bill_item_id = ?", (bill_item_id,)).fetchone()
        if item is None:
            return {"status": "error", "reason": f"No bill item {bill_item_id}."}

        bill = conn.execute("SELECT * FROM bills WHERE bill_id = ?", (item["bill_id"],)).fetchone()
        if bill["status"] != "draft":
            return {"status": "error", "reason": f"Bill {bill['bill_id']} is no longer editable."}

        product = conn.execute("SELECT * FROM products WHERE product_id = ?", (item["product_id"],)).fetchone()

        other_qty_on_bill = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM bill_items "
            "WHERE bill_id = ? AND product_id = ? AND bill_item_id != ?",
            (item["bill_id"], item["product_id"], bill_item_id)
        ).fetchone()["q"]

        if other_qty_on_bill + new_quantity > product["stock_quantity"]:
            return {
                "status": "refused",
                "reason": f"Only {product['stock_quantity']} {product['unit']} of {product['name']} in stock.",
            }

        line_subtotal = _round2(item["unit_price"] * new_quantity)
        gst_amount = _round2(line_subtotal * item["gst_rate"] / 100)
        line_total = _round2(line_subtotal + gst_amount)

        conn.execute(
            "UPDATE bill_items SET quantity = ?, gst_amount = ?, line_total = ? WHERE bill_item_id = ?",
            (new_quantity, gst_amount, line_total, bill_item_id)
        )
        subtotal, tax_total, grand_total = _recalculate_bill_totals(conn, item["bill_id"])
        return {
            "status": "ok",
            "bill_item_id": bill_item_id,
            "new_quantity": new_quantity,
            "bill_grand_total": grand_total,
        }


def remove_bill_item(bill_item_id):
    """'drop the butter' — removes a line from a draft bill."""
    with get_db(exclusive=True) as conn:
        item = conn.execute("SELECT * FROM bill_items WHERE bill_item_id = ?", (bill_item_id,)).fetchone()
        if item is None:
            return {"status": "error", "reason": f"No bill item {bill_item_id}."}
        bill = conn.execute("SELECT * FROM bills WHERE bill_id = ?", (item["bill_id"],)).fetchone()
        if bill["status"] != "draft":
            return {"status": "error", "reason": f"Bill {bill['bill_id']} is no longer editable."}

        conn.execute("DELETE FROM bill_items WHERE bill_item_id = ?", (bill_item_id,))
        subtotal, tax_total, grand_total = _recalculate_bill_totals(conn, item["bill_id"])
        return {"status": "ok", "removed_bill_item_id": bill_item_id, "bill_grand_total": grand_total}


def get_bill_summary(bill_id):
    """Reads back the current state of a bill — used for grounding before
    finalize, and for invoice generation."""
    with get_db(exclusive=False) as conn:
        bill = conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,)).fetchone()
        if bill is None:
            return {"status": "error", "reason": f"No bill {bill_id}."}
        items = conn.execute(
            """SELECT bi.*, p.name AS product_name, p.unit, p.hsn_code
               FROM bill_items bi JOIN products p ON p.product_id = bi.product_id
               WHERE bi.bill_id = ?""",
            (bill_id,)
        ).fetchall()
        return {
            "status": "ok",
            "bill_id": bill_id,
            "bill_status": bill["status"],
            "payment_method": bill["payment_method"],
            "subtotal": bill["subtotal"],
            "tax_total": bill["tax_total"],
            "grand_total": bill["grand_total"],
            "items": [
                {
                    "bill_item_id": i["bill_item_id"],
                    "product": i["product_name"],
                    "hsn_code": i["hsn_code"],
                    "quantity": i["quantity"],
                    "unit": i["unit"],
                    "unit_price": i["unit_price"],
                    "gst_rate": i["gst_rate"],
                    "cgst_amount": _round2(i["gst_amount"] / 2),
                    "sgst_amount": _round2(i["gst_amount"] / 2),
                    "line_total": i["line_total"],
                }
                for i in items
            ],
        }


def finalize_bill(bill_id, payment_method, idempotency_key, payment_reference=None):
    """
    Locks in the sale: decrements stock (atomically, only now) and marks
    the bill finalized. idempotency_key should be something stable derived
    from the incoming Telegram update (e.g. the update_id, or a key the
    agent generates once per finalize attempt and reuses on retry).

    If this exact idempotency_key has already been used for this bill,
    this is a no-op that just returns the already-finalized bill — this
    is what stops a redelivered Telegram message from double-billing.
    """
    with get_db(exclusive=True) as conn:
        bill = conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,)).fetchone()
        if bill is None:
            return {"status": "error", "reason": f"No bill {bill_id}."}

        if bill["status"] == "finalized":
            if bill["idempotency_key"] == idempotency_key:
                return {
                    "status": "ok",
                    "already_finalized": True,
                    "bill_id": bill_id,
                    "grand_total": bill["grand_total"],
                }
            return {"status": "error", "reason": f"Bill {bill_id} was already finalized."}

        items = conn.execute("SELECT * FROM bill_items WHERE bill_id = ?", (bill_id,)).fetchall()
        if not items:
            return {"status": "error", "reason": "Cannot finalize an empty bill."}

        # Final oversell re-check right before committing, in case stock
        # changed since the lines were added (e.g. another sale in between).
        for item in items:
            product = conn.execute(
                "SELECT * FROM products WHERE product_id = ?", (item["product_id"],)
            ).fetchone()
            if item["quantity"] > product["stock_quantity"]:
                return {
                    "status": "refused",
                    "reason": f"Not enough {product['name']} in stock to finalize (have {product['stock_quantity']}, need {item['quantity']}).",
                }

        for item in items:
            conn.execute(
                "UPDATE products SET stock_quantity = stock_quantity - ? WHERE product_id = ?",
                (item["quantity"], item["product_id"])
            )

        conn.execute(
            """UPDATE bills SET status = 'finalized', payment_method = ?, payment_reference = ?,
               idempotency_key = ?, finalized_at = ? WHERE bill_id = ?""",
            (payment_method, payment_reference, idempotency_key, datetime.now().isoformat(), bill_id)
        )

        return {
            "status": "ok",
            "already_finalized": False,
            "bill_id": bill_id,
            "grand_total": bill["grand_total"],
        }


# ---------------------------------------------------------------------------
# 3. Khata (credit ledger) tools
# ---------------------------------------------------------------------------

def _get_or_create_customer(conn, customer_name):
    row = conn.execute(
        "SELECT * FROM khata WHERE customer_name = ? COLLATE NOCASE", (customer_name,)
    ).fetchone()
    if row:
        return row["customer_id"]
    cur = conn.execute("INSERT INTO khata (customer_name) VALUES (?)", (customer_name,))
    return cur.lastrowid


def add_khata_charge(customer_name, amount, description=None):
    """'Put ₹500 on Ramesh's credit'. Creates the customer if new — a charge
    is how a khata customer comes into existence in the first place."""
    if amount <= 0:
        return {"status": "error", "reason": "amount must be positive."}
    with get_db(exclusive=True) as conn:
        customer_id = _get_or_create_customer(conn, customer_name)
        conn.execute(
            "INSERT INTO khata_transactions (customer_id, transaction_type, amount, description) "
            "VALUES (?, 'charge', ?, ?)",
            (customer_id, amount, description)
        )
        balance = _compute_balance(conn, customer_id)
        return {"status": "ok", "customer": customer_name, "charged": amount, "new_balance": balance}


def record_khata_payment(customer_name, amount, description=None):
    """'Ramesh paid ₹300'. Refuses if the customer has no khata record at all —
    you can't settle credit that was never extended."""
    if amount <= 0:
        return {"status": "error", "reason": "amount must be positive."}
    with get_db(exclusive=True) as conn:
        customer = conn.execute(
            "SELECT * FROM khata WHERE customer_name = ? COLLATE NOCASE", (customer_name,)
        ).fetchone()
        if customer is None:
            return {"status": "refused", "reason": f"No khata exists for '{customer_name}'."}
        conn.execute(
            "INSERT INTO khata_transactions (customer_id, transaction_type, amount, description) "
            "VALUES (?, 'payment', ?, ?)",
            (customer["customer_id"], amount, description)
        )
        balance = _compute_balance(conn, customer["customer_id"])
        return {"status": "ok", "customer": customer_name, "paid": amount, "new_balance": balance}


def _compute_balance(conn, customer_id):
    row = conn.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN transaction_type = 'charge' THEN amount ELSE 0 END), 0) -
             COALESCE(SUM(CASE WHEN transaction_type = 'payment' THEN amount ELSE 0 END), 0) AS balance
           FROM khata_transactions WHERE customer_id = ?""",
        (customer_id,)
    ).fetchone()
    return _round2(row["balance"])


def get_khata_balance(customer_name):
    """'Ramesh's balance?'"""
    with get_db(exclusive=False) as conn:
        customer = conn.execute(
            "SELECT * FROM khata WHERE customer_name = ? COLLATE NOCASE", (customer_name,)
        ).fetchone()
        if customer is None:
            return {"status": "not_found", "reason": f"No khata exists for '{customer_name}'."}
        balance = _compute_balance(conn, customer["customer_id"])
        return {"status": "ok", "customer": customer_name, "balance": balance}


def list_khata_balances(limit=10, name_query=None):
    """
    Khata customers ranked by outstanding balance, highest first — for the
    owner's khata dashboard in Telegram. Only customers who currently owe
    something (balance > 0) are included; settled or credit-only customers
    don't show up as debtors.

    name_query, if given, filters to customers whose name contains it
    (case-insensitive partial match) — this is what backs the khata search
    button, sharing the same highest-to-lowest ordering as the main list.
    """
    with get_db(exclusive=False) as conn:
        sql = """SELECT k.customer_id, k.customer_name,
                        COALESCE(SUM(CASE WHEN t.transaction_type = 'charge' THEN t.amount ELSE 0 END), 0) -
                        COALESCE(SUM(CASE WHEN t.transaction_type = 'payment' THEN t.amount ELSE 0 END), 0) AS balance
                 FROM khata k
                 LEFT JOIN khata_transactions t ON t.customer_id = k.customer_id"""
        params = []
        if name_query:
            sql += " WHERE k.customer_name LIKE ?"
            params.append(f"%{name_query}%")
        sql += " GROUP BY k.customer_id HAVING balance > 0 ORDER BY balance DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return {
            "status": "ok",
            "customers": [
                {"customer_id": r["customer_id"], "customer_name": r["customer_name"],
                 "balance": _round2(r["balance"])}
                for r in rows
            ],
        }


# ---------------------------------------------------------------------------
# 4. Daily close / analytics
# ---------------------------------------------------------------------------

def get_daily_summary(target_date=None):
    """'Today's sales?' / 'close the day'. target_date as 'YYYY-MM-DD'; defaults to today."""
    target_date = target_date or date.today().isoformat()
    with get_db(exclusive=False) as conn:
        bills = conn.execute(
            "SELECT * FROM bills WHERE status = 'finalized' AND date(finalized_at) = ?",
            (target_date,)
        ).fetchall()
        if not bills:
            return {"status": "ok", "date": target_date, "total_sales": 0, "tax_collected": 0,
                    "cash_total": 0, "upi_total": 0, "card_total": 0, "top_items": []}

        total_sales = _round2(sum(b["grand_total"] for b in bills))
        tax_collected = _round2(sum(b["tax_total"] for b in bills))
        by_method = {"Cash": 0, "UPI": 0, "Card": 0}
        for b in bills:
            by_method[b["payment_method"]] = by_method.get(b["payment_method"], 0) + b["grand_total"]

        bill_ids = [b["bill_id"] for b in bills]
        placeholders = ",".join("?" * len(bill_ids))
        top_items = conn.execute(
            f"""SELECT p.name, SUM(bi.quantity) AS qty_sold, SUM(bi.line_total) AS revenue
                FROM bill_items bi JOIN products p ON p.product_id = bi.product_id
                WHERE bi.bill_id IN ({placeholders})
                GROUP BY p.product_id ORDER BY qty_sold DESC LIMIT 5""",
            bill_ids
        ).fetchall()

        return {
            "status": "ok",
            "date": target_date,
            "total_sales": total_sales,
            "tax_collected": tax_collected,
            "cash_total": _round2(by_method.get("Cash", 0)),
            "upi_total": _round2(by_method.get("UPI", 0)),
            "card_total": _round2(by_method.get("Card", 0)),
            "bill_count": len(bills),
            "top_items": [{"name": r["name"], "qty_sold": r["qty_sold"], "revenue": r["revenue"]} for r in top_items],
        }


# ---------------------------------------------------------------------------
# 5. Preferences (cross-session memory)
# ---------------------------------------------------------------------------

def set_preference(key, value):
    with get_db(exclusive=True) as conn:
        conn.execute(
            "INSERT INTO preferences (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value))
        )
        return {"status": "ok", "key": key, "value": value}


def get_preference(key):
    with get_db(exclusive=False) as conn:
        row = conn.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
        return {"status": "ok", "key": key, "value": row["value"] if row else None}


def get_all_preferences():
    """Call this at the START of every conversation (not just when asked)
    and load the result into the agent's context — this is what makes
    preferences survive a /new chat."""
    with get_db(exclusive=False) as conn:
        rows = conn.execute("SELECT key, value FROM preferences").fetchall()
        return {"status": "ok", "preferences": {r["key"]: r["value"] for r in rows}}
    
    
def cancel_bill(bill_id):
    """'cancel this bill' / 'never mind, start over' — abandons a draft bill.
    Only works on drafts: a finalized bill is a real sale record and must
    never be cancelled or deleted, only referred to."""
    with get_db(exclusive=True) as conn:
        bill = conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,)).fetchone()
        if bill is None:
            return {"status": "error", "reason": f"No bill {bill_id}."}
        if bill["status"] != "draft":
            return {
                "status": "refused",
                "reason": f"Bill {bill_id} is '{bill['status']}', not a draft — finalized sales can't be cancelled.",
            }
        conn.execute("UPDATE bills SET status = 'cancelled' WHERE bill_id = ?", (bill_id,))
        return {"status": "ok", "cancelled_bill_id": bill_id}


# ---------------------------------------------------------------------------
# 6. Weekly deck subscribers (Telegram chat_ids to auto-send the deck to)
# ---------------------------------------------------------------------------
#
# Deliberately NOT a new table — reuses the existing preferences key/value
# store, same as everything else here. One key holds a JSON array of
# chat_ids. Called from telegram_bot/bot_gemini.py's /subscribe_weekly and
# /unsubscribe_weekly commands (deterministic actions, not model tool calls
# — no reason to route a chat_id through the LLM).

_WEEKLY_DECK_SUBSCRIBERS_KEY = "weekly_deck_subscribers"


def get_weekly_deck_subscribers():
    """Returns the Telegram chat_ids currently subscribed to the automatic
    weekly sales deck."""
    raw = get_preference(_WEEKLY_DECK_SUBSCRIBERS_KEY)["value"]
    chat_ids = json.loads(raw) if raw else []
    return {"status": "ok", "chat_ids": chat_ids}


def add_weekly_deck_subscriber(chat_id):
    chat_ids = get_weekly_deck_subscribers()["chat_ids"]
    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        set_preference(_WEEKLY_DECK_SUBSCRIBERS_KEY, json.dumps(chat_ids))
    return {"status": "ok", "chat_ids": chat_ids}


def remove_weekly_deck_subscriber(chat_id):
    chat_ids = get_weekly_deck_subscribers()["chat_ids"]
    if chat_id in chat_ids:
        chat_ids.remove(chat_id)
        set_preference(_WEEKLY_DECK_SUBSCRIBERS_KEY, json.dumps(chat_ids))
    return {"status": "ok", "chat_ids": chat_ids}
