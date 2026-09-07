"""
database/init_db.py
---------------------
Creates database/supermarket.db with the tables tools/store_tools.py
expects. This was previously only defined inline inside the test files
(tests/test_each_function.py and tests/test_concurrency.py) — there was
no way to create the REAL database, so the bot would crash on first use
with "no such table". Run this once before starting the bot:

    python3 -m database.init_db

Safe to re-run: it only creates tables that don't already exist yet, so
it will never wipe real data.
"""

import sqlite3
from database.database import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
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
CREATE TABLE IF NOT EXISTS bills (
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_idempotency_key ON bills(idempotency_key);
CREATE TABLE IF NOT EXISTS bill_items (
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
CREATE TABLE IF NOT EXISTS khata (
    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL UNIQUE,
    phone TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS khata_transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    transaction_type TEXT NOT NULL,
    amount REAL NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES khata(customer_id)
);
CREATE TABLE IF NOT EXISTS preferences (
    preference_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL
);
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Database ready at {DB_PATH}")


if __name__ == "__main__":
    init_db()
