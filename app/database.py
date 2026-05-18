import sqlite3
from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_number TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER,
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
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'processed',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (object_id) REFERENCES objects(id) ON DELETE SET NULL
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
    ensure_column(db, "documents", "object_id", "INTEGER")
    ensure_column(db, "documents", "note", "TEXT NOT NULL DEFAULT ''")
    migrate_objects(db)
    db.commit()


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_objects(db: sqlite3.Connection) -> None:
    documents_without_object = db.execute(
        """
        SELECT DISTINCT contract_number, object_name
        FROM documents
        WHERE object_id IS NULL
          AND contract_number <> ''
          AND object_name <> ''
        """
    ).fetchall()

    for document in documents_without_object:
        obj = db.execute(
            """
            SELECT id
            FROM objects
            WHERE contract_number = ? AND name = ?
            LIMIT 1
            """,
            (document["contract_number"], document["object_name"]),
        ).fetchone()
        if obj is None:
            cursor = db.execute(
                "INSERT INTO objects (contract_number, name) VALUES (?, ?)",
                (document["contract_number"], document["object_name"]),
            )
            object_id = cursor.lastrowid
        else:
            object_id = obj["id"]

        db.execute(
            """
            UPDATE documents
            SET object_id = ?
            WHERE object_id IS NULL
              AND contract_number = ?
              AND object_name = ?
            """,
            (object_id, document["contract_number"], document["object_name"]),
        )


def close_db(_error: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()
