"""
database/database.py
----------------------
The single place that knows how to open a connection to supermarket.db.
Every tool in tools/store_tools.py goes through get_db() — nothing opens
sqlite3.connect() directly anywhere else in the project.

exclusive=True uses BEGIN IMMEDIATE, which takes a write lock the moment
the transaction opens. This is what stops two concurrent operations
(two sales, or a sale + a stock-in) from corrupting stock — SQLite will
make the second writer wait (or fail with 'database is locked' if it
waits past the timeout) instead of silently interleaving.
"""

import sqlite3
import math
from contextlib import contextmanager

DB_PATH = "database/supermarket.db"


@contextmanager
def get_db(exclusive=True):
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("BEGIN IMMEDIATE" if exclusive else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def round2(x):
    """Round to 2 decimals the way currency should round (half-up)."""
    return math.floor(x * 100 + 0.5) / 100
