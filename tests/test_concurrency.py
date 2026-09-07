"""
tests/test_concurrency.py
----------------------------
Proves the "two things happening at once must not corrupt stock" guarantee
with REAL concurrent threads racing against each other on purpose — not
just sequential calls that happen to look concurrent on paper.

Uses threading.Barrier to force every thread to hit the database at the
exact same instant, which is the actual worst case: maximum contention.
Each thread gets its own sqlite3 connection (get_db() opens a fresh one
per call), which is required — sqlite3 connections aren't thread-safe to
share, but concurrent connections to the same FILE are exactly what
BEGIN IMMEDIATE is there to serialize safely.

Run from the project root:
    python3 -m tests.test_concurrency
"""

import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import database.database as dbmod

TEST_DB = "database/test_concurrency.db"
dbmod.DB_PATH = TEST_DB


def _build_schema():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript("""
    CREATE TABLE products (
        product_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, unit TEXT NOT NULL,
        is_loose INTEGER NOT NULL DEFAULT 0, cost_price REAL NOT NULL, sell_price REAL NOT NULL,
        mrp REAL NOT NULL, stock_quantity REAL NOT NULL DEFAULT 0, reorder_level REAL NOT NULL DEFAULT 0,
        hsn_code TEXT NOT NULL, gst_rate REAL NOT NULL
    );
    CREATE TABLE bills (
        bill_id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL DEFAULT 'draft',
        payment_method TEXT, payment_reference TEXT, subtotal REAL NOT NULL DEFAULT 0,
        tax_total REAL NOT NULL DEFAULT 0, grand_total REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finalized_at TEXT, idempotency_key TEXT
    );
    CREATE UNIQUE INDEX idx_bills_idempotency_key ON bills(idempotency_key);
    CREATE TABLE bill_items (
        bill_item_id INTEGER PRIMARY KEY AUTOINCREMENT, bill_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
        quantity REAL NOT NULL, unit_price REAL NOT NULL, gst_rate REAL NOT NULL,
        gst_amount REAL NOT NULL DEFAULT 0, line_total REAL NOT NULL DEFAULT 0,
        FOREIGN KEY (bill_id) REFERENCES bills(bill_id), FOREIGN KEY (product_id) REFERENCES products(product_id)
    );
    CREATE TABLE khata (
        customer_id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT NOT NULL UNIQUE,
        phone TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE khata_transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
        transaction_type TEXT NOT NULL, amount REAL NOT NULL, description TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES khata(customer_id)
    );
    CREATE TABLE preferences (
        preference_id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, value TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


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


def _full_sale_flow(product_name, qty, idem_key, barrier):
    """Simulates one complete incoming Telegram message: build a bill and
    finalize it. Waits at the barrier so every thread fires at the same
    instant — maximum realistic contention."""
    barrier.wait()
    bill = t.start_bill()
    add_result = t.add_bill_item(bill["bill_id"], product_name, qty)
    if add_result["status"] != "ok":
        return {"stage": "add_bill_item", "result": add_result}
    finalize_result = t.finalize_bill(bill["bill_id"], "UPI", idem_key)
    return {"stage": "finalize_bill", "result": finalize_result}


def test_race_for_limited_stock():
    """
    THE key scenario: stock=10. Two threads simultaneously try to sell 6
    units each (12 total demanded, only 10 exist). At most one should
    succeed at 6 units; the other must be refused. Final stock must never
    go negative and must exactly match what was actually sold.
    """
    section("1. Race for limited stock (two threads, 6 units each, only 10 in stock)")

    t.add_new_product(
        name="RaceProduct", unit="piece", is_loose=False,
        cost_price=5, sell_price=10, mrp=10, hsn_code="0000", gst_rate=0,
        initial_stock=10, reorder_level=0,
    )

    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_full_sale_flow, "RaceProduct", 6, "race-thread-A", barrier),
            pool.submit(_full_sale_flow, "RaceProduct", 6, "race-thread-B", barrier),
        ]
        outcomes = [f.result() for f in as_completed(futures)]

    succeeded = [o for o in outcomes if o["result"]["status"] == "ok"]
    refused = [o for o in outcomes if o["result"]["status"] in ("refused", "error")]

    final_stock = t.check_stock("RaceProduct")["matches"][0]["stock_quantity"]

    check(len(succeeded) == 1, f"exactly one thread succeeded (got {len(succeeded)})")
    check(len(refused) == 1, f"exactly one thread was refused (got {len(refused)})")
    check(final_stock == 4.0, f"final stock is exactly 10-6=4, never negative (got {final_stock})")
    print(f"    (outcomes: {[(o['stage'], o['result']['status']) for o in outcomes]})")


def _add_stock_thread(product_name, qty, barrier):
    barrier.wait()
    return t.add_stock(product_name, qty)


def _sell_thread(product_name, qty, idem_key, barrier):
    barrier.wait()
    bill = t.start_bill()
    t.add_bill_item(bill["bill_id"], product_name, qty)
    return t.finalize_bill(bill["bill_id"], "Cash", idem_key)


def test_no_lost_update():
    """
    Stock=20. One thread adds +15 stock at the same instant another
    thread sells -5. This is the classic "lost update" bug: if both
    threads read stock=20 before either writes, one write can silently
    overwrite the other, losing an update. Correct locking must produce
    exactly 20+15-5=30, not 35 (sale lost) or 15 (restock lost).
    """
    section("2. Concurrent restock + sale on the same product (no lost update)")

    t.add_new_product(
        name="LostUpdateProduct", unit="piece", is_loose=False,
        cost_price=5, sell_price=10, mrp=10, hsn_code="0000", gst_rate=0,
        initial_stock=20, reorder_level=0,
    )

    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_add_stock_thread, "LostUpdateProduct", 15, barrier)
        f2 = pool.submit(_sell_thread, "LostUpdateProduct", 5, "lost-update-key", barrier)
        r1, r2 = f1.result(), f2.result()

    final_stock = t.check_stock("LostUpdateProduct")["matches"][0]["stock_quantity"]
    check(r1["status"] == "ok", "restock succeeded")
    check(r2["status"] == "ok", "sale succeeded")
    check(final_stock == 30.0, f"final stock reflects BOTH operations: 20+15-5=30 (got {final_stock})")


def test_stress_many_buyers():
    """
    Stock=50. 15 threads simultaneously each try to buy 5 units (75
    demanded total, only 50 exist -> at most 10 can succeed). Verifies
    at NO point does total sold exceed available stock, across real
    concurrent contention, not just two threads.
    """
    section("3. Stress test: 15 simultaneous buyers, only enough stock for 10")

    t.add_new_product(
        name="StressProduct", unit="piece", is_loose=False,
        cost_price=5, sell_price=10, mrp=10, hsn_code="0000", gst_rate=0,
        initial_stock=50, reorder_level=0,
    )

    n_threads = 15
    barrier = threading.Barrier(n_threads)
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [
            pool.submit(_full_sale_flow, "StressProduct", 5, f"stress-key-{i}", barrier)
            for i in range(n_threads)
        ]
        outcomes = [f.result() for f in as_completed(futures)]

    succeeded = [o for o in outcomes if o["result"]["status"] == "ok"]
    final_stock = t.check_stock("StressProduct")["matches"][0]["stock_quantity"]
    expected_sold = len(succeeded) * 5

    check(len(succeeded) <= 10, f"at most 10 of 15 buyers could succeed (got {len(succeeded)})")
    check(final_stock == 50 - expected_sold, f"stock exactly matches units actually sold (50-{expected_sold}={50-expected_sold}, got {final_stock})")
    check(final_stock >= 0, f"stock never went negative (got {final_stock})")


def main():
    _build_schema()
    test_race_for_limited_stock()
    test_no_lost_update()
    test_stress_many_buyers()

    print(f"\n{'='*55}")
    print(f"RESULT: {_passed} passed, {_failed} failed")
    print(f"{'='*55}")
    if _failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
