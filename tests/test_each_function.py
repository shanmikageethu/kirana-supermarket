"""
tests/test_each_function.py
-----------------------------
Walks through EVERY public function in tools/store_tools.py, one at a time,
and prints a pass/fail check for each. Uses a throwaway database
(database/test_supermarket.db) so your real supermarket.db is never touched.

Run from the project root:
    python3 -m tests.test_each_function

Read top to bottom — each section says which function it's testing and
what specifically it's checking (the normal case, and the guardrail where
relevant). If something fails, the assertion message tells you which
function and which behavior broke.
"""

import os
import sqlite3
from datetime import date

import database.database as dbmod

# --- Point every function at a disposable test database, not the real one ---
TEST_DB = "database/test_supermarket.db"
dbmod.DB_PATH = TEST_DB


def _build_schema():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
    CREATE TABLE products (
        product_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        unit TEXT NOT NULL,
        is_loose INTEGER NOT NULL DEFAULT 0,
        cost_price REAL NOT NULL,
        sell_price REAL NOT NULL,
        mrp REAL NOT NULL,
        stock_quantity REAL NOT NULL DEFAULT 0,
        reorder_level REAL NOT NULL DEFAULT 0,
        hsn_code TEXT NOT NULL,
        gst_rate REAL NOT NULL
    );
    CREATE TABLE bills (
        bill_id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL DEFAULT 'draft',
        payment_method TEXT,
        payment_reference TEXT,
        subtotal REAL NOT NULL DEFAULT 0,
        tax_total REAL NOT NULL DEFAULT 0,
        grand_total REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finalized_at TEXT,
        idempotency_key TEXT
    );
    CREATE UNIQUE INDEX idx_bills_idempotency_key ON bills(idempotency_key);
    CREATE TABLE bill_items (
        bill_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity REAL NOT NULL,
        unit_price REAL NOT NULL,
        gst_rate REAL NOT NULL,
        gst_amount REAL NOT NULL DEFAULT 0,
        line_total REAL NOT NULL DEFAULT 0,
        FOREIGN KEY (bill_id) REFERENCES bills(bill_id),
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    );
    CREATE TABLE khata (
        customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_name TEXT NOT NULL UNIQUE,
        phone TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE khata_transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        transaction_type TEXT NOT NULL,
        amount REAL NOT NULL,
        description TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES khata(customer_id)
    );
    CREATE TABLE preferences (
        preference_id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL UNIQUE,
        value TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


# Import AFTER dbmod.DB_PATH is overridden, so every call inside store_tools
# uses the test database.
from tools import store_tools as t  # noqa: E402

_passed = 0
_failed = 0


def check(condition, label):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  \u2713 {label}")
    else:
        _failed += 1
        print(f"  \u2717 FAILED: {label}")


def section(title):
    print(f"\n=== {title} ===")


def main():
    _build_schema()

    # -----------------------------------------------------------------
    section("1. add_new_product — registers a brand-new SKU")
    # -----------------------------------------------------------------
    r = t.add_new_product(
        name="Maggi 70g", unit="packet", is_loose=False,
        cost_price=10, sell_price=14, mrp=14,
        hsn_code="19023090", gst_rate=12, initial_stock=0, reorder_level=10,
    )
    check(r["status"] == "ok", "creates a product successfully")
    check(r["stock_quantity"] == 0, "starts at the initial_stock given (0)")

    r_dup = t.add_new_product(
        name="Maggi 70g", unit="packet", is_loose=False,
        cost_price=10, sell_price=14, mrp=14,
        hsn_code="19023090", gst_rate=12,
    )
    check(r_dup["status"] == "error", "refuses to create the same product twice")

    r_bad = t.add_new_product(
        name="Underpriced Item", unit="piece", is_loose=False,
        cost_price=100, sell_price=50, mrp=120,
        hsn_code="00000000", gst_rate=5,
    )
    check(r_bad["status"] == "error", "refuses sell_price below cost_price at creation")

    t.add_new_product(
        name="Sugar", unit="kg", is_loose=True,
        cost_price=40, sell_price=45, mrp=45,
        hsn_code="17019900", gst_rate=0, initial_stock=6, reorder_level=5,
    )
    t.add_new_product(
        name="Aashirvaad Atta 5kg", unit="kg", is_loose=False,
        cost_price=200, sell_price=230, mrp=235,
        hsn_code="11010000", gst_rate=0, initial_stock=10, reorder_level=3,
    )
    t.add_new_product(
        name="Loose Atta", unit="kg", is_loose=True,
        cost_price=35, sell_price=40, mrp=40,
        hsn_code="11010000", gst_rate=0, initial_stock=20, reorder_level=5,
    )

    # -----------------------------------------------------------------
    section("2. add_stock — receiving more of an EXISTING product")
    # -----------------------------------------------------------------
    r = t.add_stock("Maggi", 50)
    check(r["status"] == "ok", "adds stock to an existing product by partial name match")
    check(r["new_stock_quantity"] == 50, "0 + 50 = 50")

    r2 = t.add_stock("Maggi", 20, cost_price=11, mrp=15)
    check(r2["new_stock_quantity"] == 70, "50 + 20 = 70, and cost/MRP updated together")

    r_missing = t.add_stock("Nonexistent Product XYZ", 10)
    check(r_missing["status"] == "not_found", "refuses to invent a product that doesn't exist")

    # -----------------------------------------------------------------
    section("3. check_stock — grounding: read real data, never guess")
    # -----------------------------------------------------------------
    r = t.check_stock("Sugar")
    check(r["status"] == "ok" and len(r["matches"]) == 1, "finds an exact single match")
    check(r["matches"][0]["stock_quantity"] == 6, "reports the real stock quantity (6)")

    r_amb = t.check_stock("atta")
    check(r_amb["status"] == "ok" and len(r_amb["matches"]) == 2,
          "returns BOTH matches for an ambiguous name instead of guessing one")

    r_none = t.check_stock("Bournvita")
    check(r_none["status"] == "not_found", "reports not_found instead of hallucinating a product")

    # -----------------------------------------------------------------
    section("4. get_low_stock — items at/below reorder level")
    # -----------------------------------------------------------------
    t.add_new_product(
        name="Parle-G", unit="packet", is_loose=False,
        cost_price=8, sell_price=10, mrp=10,
        hsn_code="19053100", gst_rate=18, initial_stock=2, reorder_level=15,
    )
    r = t.get_low_stock()
    names = [i["name"] for i in r["low_stock_items"]]
    check("Parle-G" in names, "Parle-G (2 in stock, reorder level 15) correctly flagged as low")
    check("Sugar" not in names, "Sugar (6 in stock > reorder level 5) correctly NOT flagged")

    # -----------------------------------------------------------------
    section("5. start_bill — opens a new draft, no side effects on stock")
    # -----------------------------------------------------------------
    bill = t.start_bill()
    check(bill["status"] == "ok" and "bill_id" in bill, "creates a bill and returns its id")
    bill_id = bill["bill_id"]

    # -----------------------------------------------------------------
    section("6. add_bill_item — adds a line + enforces the OVERSELL GUARD")
    # -----------------------------------------------------------------
    r = t.add_bill_item(bill_id, "Maggi", 4)
    check(r["status"] == "ok", "adds 4 Maggi to the draft bill")
    check(r["bill_grand_total"] == r["line_total"], "bill total matches the single line so far")

    r2 = t.add_bill_item(bill_id, "Sugar", 2)
    check(r2["status"] == "ok", "adds 2kg sugar to the same bill")

    r_over = t.add_bill_item(bill_id, "Sugar", 8)
    check(r_over["status"] == "refused", "OVERSELL GUARD: refuses 8 more kg (only 6kg total exist)")
    check(r_over["available"] == 6, "tells us exactly how much is actually available")

    stock_after = t.check_stock("Sugar")["matches"][0]["stock_quantity"]
    check(stock_after == 6, "stock is UNCHANGED by add_bill_item — only finalize touches stock")

    # -----------------------------------------------------------------
    section("7. update_bill_item_quantity — mid-build edit, e.g. 'make it 6 Maggi'")
    # -----------------------------------------------------------------
    maggi_item_id = t.get_bill_summary(bill_id)["items"][0]["bill_item_id"]
    r = t.update_bill_item_quantity(maggi_item_id, 6)
    check(r["status"] == "ok" and r["new_quantity"] == 6, "updates quantity to 6")

    r_over2 = t.update_bill_item_quantity(maggi_item_id, 1000)
    check(r_over2["status"] == "refused", "refuses to update into an oversell (1000 Maggi don't exist)")

    # -----------------------------------------------------------------
    section("8. remove_bill_item — e.g. 'drop the butter'")
    # -----------------------------------------------------------------
    temp_item = t.add_bill_item(bill_id, "Aashirvaad", 1)
    r = t.remove_bill_item(temp_item["bill_item_id"])
    check(r["status"] == "ok", "removes the line successfully")
    remaining_ids = [i["bill_item_id"] for i in t.get_bill_summary(bill_id)["items"]]
    check(temp_item["bill_item_id"] not in remaining_ids, "the removed line is really gone from the bill")

    # -----------------------------------------------------------------
    section("9. get_bill_summary — reads back items + totals for grounding")
    # -----------------------------------------------------------------
    summary = t.get_bill_summary(bill_id)
    check(summary["status"] == "ok", "reads the bill successfully")
    check(len(summary["items"]) == 2, "shows exactly the 2 lines left (Maggi x6, Sugar x2)")
    check(summary["bill_status"] == "draft", "still draft — not finalized yet")

    # -----------------------------------------------------------------
    section("10. finalize_bill — decrements stock ONCE, idempotently")
    # -----------------------------------------------------------------
    sugar_before = t.check_stock("Sugar")["matches"][0]["stock_quantity"]
    r1 = t.finalize_bill(bill_id, "UPI", idempotency_key="update-9001")
    check(r1["status"] == "ok" and r1["already_finalized"] is False, "finalizes successfully the first time")

    sugar_after = t.check_stock("Sugar")["matches"][0]["stock_quantity"]
    check(sugar_after == sugar_before - 2, "stock decremented by exactly the billed quantity (2kg)")

    r2 = t.finalize_bill(bill_id, "UPI", idempotency_key="update-9001")
    check(r2["already_finalized"] is True, "IDEMPOTENCY: retried finalize with same key is a no-op")

    sugar_after_retry = t.check_stock("Sugar")["matches"][0]["stock_quantity"]
    check(sugar_after_retry == sugar_after, "stock did NOT decrement a second time on retry")

    r3 = t.finalize_bill(bill_id, "UPI", idempotency_key="a-different-key-entirely")
    check(r3["status"] == "error", "a genuinely different key on an already-finalized bill is refused, not silently accepted")

    # -----------------------------------------------------------------
    section("11. add_khata_charge — 'put ₹500 on Ramesh's credit'")
    # -----------------------------------------------------------------
    r = t.add_khata_charge("Ramesh", 500, "groceries on credit")
    check(r["status"] == "ok" and r["new_balance"] == 500, "creates Ramesh's khata and charges 500")

    # -----------------------------------------------------------------
    section("12. record_khata_payment — 'Ramesh paid ₹300'")
    # -----------------------------------------------------------------
    r = t.record_khata_payment("Ramesh", 300)
    check(r["status"] == "ok" and r["new_balance"] == 200, "500 - 300 = 200")

    r_ghost = t.record_khata_payment("GhostCustomer", 100)
    check(r_ghost["status"] == "refused", "GUARDRAIL: refuses to settle a khata that doesn't exist")

    # -----------------------------------------------------------------
    section("13. get_khata_balance — \"Ramesh's balance?\"")
    # -----------------------------------------------------------------
    r = t.get_khata_balance("Ramesh")
    check(r["status"] == "ok" and r["balance"] == 200, "reports the correct current balance")

    r_none = t.get_khata_balance("NobodyEver")
    check(r_none["status"] == "not_found", "reports not_found for a customer with no khata at all")

    # -----------------------------------------------------------------
    section("14. get_daily_summary — 'today's sales?' / 'close the day'")
    # -----------------------------------------------------------------
    r = t.get_daily_summary(date.today().isoformat())
    check(r["status"] == "ok", "computes today's summary")
    check(r["bill_count"] == 1, "counts exactly the 1 finalized bill from today")
    check(r["upi_total"] == r["total_sales"], "the one bill was UPI, so upi_total == total_sales")
    check(len(r["top_items"]) == 2, "lists both products sold (Maggi, Sugar)")

    # -----------------------------------------------------------------
    section("15/16/17. set_preference / get_preference / get_all_preferences")
    # -----------------------------------------------------------------
    r = t.set_preference("default_payment", "UPI")
    check(r["status"] == "ok", "sets a preference")

    r_upd = t.set_preference("default_payment", "Cash")
    check(r_upd["value"] == "Cash", "updates (not duplicates) an existing preference key")

    r = t.get_preference("default_payment")
    check(r["value"] == "Cash", "reads back the latest value")

    r_missing = t.get_preference("shop_name")
    check(r_missing["value"] is None, "returns None for a preference that was never set, not an error")

    t.set_preference("shop_name", "Geetika General Store")
    r_all = t.get_all_preferences()
    check(r_all["preferences"]["default_payment"] == "Cash", "get_all_preferences includes default_payment")
    check(r_all["preferences"]["shop_name"] == "Geetika General Store", "and shop_name")

    # -----------------------------------------------------------------
    print(f"\n{'='*50}")
    print(f"RESULT: {_passed} passed, {_failed} failed")
    print(f"{'='*50}")
    if _failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
