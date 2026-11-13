"""E2: SQLite connection helper. WAL mode, foreign keys on, manual transactions."""
import os
import sqlite3

import config

_SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def connect(path=None):
    """Open a new connection. Every process / thread opens its own."""
    config.ensure_dirs()
    conn = sqlite3.connect(path or config.DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_schema(conn):
    with open(_SCHEMA, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
