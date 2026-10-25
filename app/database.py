import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from flask import current_app, g

SCHEMA_VERSION = 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_number TEXT NOT NULL,
    name TEXT NOT NULL,
    template_id INTEGER,
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER,
    contract_number TEXT NOT NULL,
    object_name TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'application/pdf',
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

CREATE TABLE IF NOT EXISTS checklist_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS checklist_template_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (template_id) REFERENCES checklist_templates(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_checklist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL,
    source_template_item_id INTEGER,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'missing'
        CHECK (status IN ('missing', 'received', 'not_required')),
    document_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (object_id) REFERENCES objects(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_checklist_object
    ON project_checklist_items(object_id, sort_order, id);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def init_db() -> None:
    db = get_db()
    backup_before_migration(db)
    db.executescript(SCHEMA)
    ensure_column(db, "objects", "template_id", "INTEGER")
    ensure_column(db, "objects", "archived_at", "TEXT")
    ensure_column(db, "documents", "object_id", "INTEGER")
    ensure_column(db, "documents", "note", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "documents", "mime_type", "TEXT NOT NULL DEFAULT 'application/pdf'")
    ensure_column(db, "documents", "archived_at", "TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_documents_object_date ON documents(object_id, created_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_documents_expiry ON documents(valid_until) WHERE valid_until <> '' AND no_expiration = 0")
    migrate_objects(db)
    seed_default_template(db)
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"Database integrity check failed after migration: {integrity}")
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()


def backup_before_migration(db: sqlite3.Connection) -> Path | None:
    """Take a verified SQLite online backup before changing an existing schema."""
    if db.execute(f"PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return None
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if row[0] not in {"sqlite_sequence"}
    }
    if not tables.intersection({"objects", "documents"}):
        return None

    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"Database integrity check failed before migration: {integrity}")

    database_path = Path(current_app.config["DATABASE_PATH"])
    backup_dir = database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{database_path.stem}-before-v{SCHEMA_VERSION}-{timestamp}.sqlite"
    backup = sqlite3.connect(backup_path)
    try:
        db.backup(backup)
        backup_integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        backup.close()
    if backup_integrity != "ok":
        backup_path.unlink(missing_ok=True)
        raise RuntimeError(f"Database backup integrity check failed: {backup_integrity}")
    return backup_path


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


def seed_default_template(db: sqlite3.Connection) -> None:
    from app.document_types import DOCUMENT_TYPES

    initialized = db.execute(
        "SELECT value FROM app_settings WHERE key = 'default_template_seeded'"
    ).fetchone()
    if initialized is not None:
        return

    template = db.execute(
        "SELECT id FROM checklist_templates WHERE name = ?",
        ("Исходные данные для проектирования",),
    ).fetchone()
    if template is None:
        cursor = db.execute(
            "INSERT INTO checklist_templates (name, description) VALUES (?, ?)",
            (
                "Исходные данные для проектирования",
                "Базовый перечень документов для проектно-изыскательских работ.",
            ),
        )
        db.executemany(
            "INSERT INTO checklist_template_items (template_id, title, sort_order) VALUES (?, ?, ?)",
            [(cursor.lastrowid, title, index) for index, title in enumerate(DOCUMENT_TYPES)],
        )
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('default_template_seeded', '1')"
    )


def close_db(_error: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()
