import sqlite3
from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_number TEXT NOT NULL,
    object_name TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    working_title TEXT,
    official_title TEXT,
    issuer TEXT,
    issue_date TEXT,
    valid_until TEXT,
    validity_text TEXT,
    no_expiration INTEGER NOT NULL DEFAULT 0,
    raw_text TEXT,
    status TEXT NOT NULL DEFAULT 'processed',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db() -> None:
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


def close_db(_error: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()
