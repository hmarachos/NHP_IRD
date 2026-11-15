import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from flask import current_app, g

SCHEMA_VERSION = 5


SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_number TEXT NOT NULL,
    name TEXT NOT NULL,
    customer TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS correspondence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
    number TEXT NOT NULL DEFAULT '',
    letter_date TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    original_filename TEXT NOT NULL DEFAULT '',
    stored_filename TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT 'application/pdf',
    raw_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'processed',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(object_id) REFERENCES objects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_correspondence_object_date ON correspondence(object_id, letter_date DESC, id DESC);

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
    validity_years INTEGER NOT NULL DEFAULT 0,
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
    validity_years INTEGER NOT NULL DEFAULT 0,
    issue_date TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (object_id) REFERENCES objects(id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handover_acts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'automatic',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,
    email_error TEXT,
    FOREIGN KEY (object_id) REFERENCES objects(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_handover_acts_pending_object
    ON handover_acts(object_id) WHERE sent_at IS NULL AND source = 'automatic';

CREATE TABLE IF NOT EXISTS expiry_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    notification_type TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    UNIQUE(document_id, notification_type)
);

CREATE TABLE IF NOT EXISTS checklist_expiry_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checklist_item_id INTEGER NOT NULL,
    valid_until TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (checklist_item_id) REFERENCES project_checklist_items(id) ON DELETE CASCADE,
    UNIQUE(checklist_item_id, valid_until)
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
    db.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('mail_notifications_start_at', CURRENT_TIMESTAMP)"
    )
    ensure_column(db, "objects", "customer", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "objects", "template_id", "INTEGER")
    ensure_column(db, "objects", "archived_at", "TEXT")
    ensure_column(db, "documents", "object_id", "INTEGER")
    ensure_column(db, "documents", "note", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "documents", "mime_type", "TEXT NOT NULL DEFAULT 'application/pdf'")
    ensure_column(db, "documents", "archived_at", "TEXT")
    ensure_column(db, "checklist_template_items", "validity_years", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "project_checklist_items", "validity_years", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "project_checklist_items", "issue_date", "TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "project_checklist_items", "valid_until", "TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE INDEX IF NOT EXISTS idx_documents_object_date ON documents(object_id, created_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_documents_expiry ON documents(valid_until) WHERE valid_until <> '' AND no_expiration = 0")
    migrate_objects(db)
    seed_default_template(db)
    ensure_geodetic_survey_item(db)
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

    # Rename the original built-in checklist in place so existing project links
    # and checklist item references remain valid.
    db.execute(
        "UPDATE checklist_templates SET name = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE name = ? AND NOT EXISTS (SELECT 1 FROM checklist_templates WHERE name = ?)",
        (
            "Возведение/реконструкция АЗС",
            "Исходные данные для проектирования",
            "Возведение/реконструкция АЗС",
        ),
    )
    db.execute(
        "UPDATE checklist_templates SET description = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE name = ? AND description = ?",
        (
            "Перечень исходных документов для возведения или реконструкции АЗС.",
            "Возведение/реконструкция АЗС",
            "Базовый перечень документов для проектно-изыскательских работ.",
        ),
    )

    initialized = db.execute(
        "SELECT value FROM app_settings WHERE key = 'default_template_seeded'"
    ).fetchone()
    if initialized is not None:
        return

    template = db.execute(
        "SELECT id FROM checklist_templates WHERE name = ?",
        ("Возведение/реконструкция АЗС",),
    ).fetchone()
    if template is None:
        cursor = db.execute(
            "INSERT INTO checklist_templates (name, description) VALUES (?, ?)",
            (
                "Возведение/реконструкция АЗС",
                "Перечень исходных документов для возведения или реконструкции АЗС.",
            ),
        )
        db.executemany(
            "INSERT INTO checklist_template_items (template_id, title, sort_order) VALUES (?, ?, ?)",
            [(cursor.lastrowid, title, index) for index, title in enumerate(DOCUMENT_TYPES)],
        )
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('default_template_seeded', '1')"
    )


def ensure_geodetic_survey_item(db: sqlite3.Connection) -> None:
    """Add the dated, non-file geodetic item to the standard template and its existing projects."""
    if db.execute(
        "SELECT 1 FROM app_settings WHERE key = 'geodetic_survey_template_migrated_v2'"
    ).fetchone():
        return
    title = "Инженерно-геодезические изыскания"
    description = "Указать дату изысканий; срок действия — 2 года. Файл не прикладывается."
    template = db.execute(
        "SELECT id FROM checklist_templates WHERE name = ?",
        ("Возведение/реконструкция АЗС",),
    ).fetchone()
    if template is None:
        return
    template_id = template["id"]
    item = db.execute(
        "SELECT id FROM checklist_template_items WHERE template_id = ? AND title = ?",
        (template_id, title),
    ).fetchone()
    if item is None:
        sort_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM checklist_template_items WHERE template_id = ?",
            (template_id,),
        ).fetchone()[0]
        cursor = db.execute(
            "INSERT INTO checklist_template_items (template_id, title, description, sort_order, validity_years) VALUES (?, ?, ?, ?, 2)",
            (template_id, title, description, sort_order),
        )
        item_id = cursor.lastrowid
    else:
        item_id = item["id"]
        db.execute(
            "UPDATE checklist_template_items SET validity_years = 2, description = CASE WHEN TRIM(description) = '' THEN ? ELSE description END WHERE id = ?",
            (description, item_id),
        )
    projects = db.execute(
        """
        SELECT id FROM objects
        WHERE (
            template_id = ? OR id IN (
                SELECT DISTINCT object_id FROM project_checklist_items
                WHERE source_template_item_id IN (
                    SELECT id FROM checklist_template_items WHERE template_id = ?
                )
            )
        )
        """,
        (template_id, template_id),
    ).fetchall()
    for project in projects:
        existing = db.execute(
            "SELECT id FROM project_checklist_items WHERE object_id = ? AND (source_template_item_id = ? OR title = ?) ORDER BY CASE WHEN source_template_item_id = ? THEN 0 ELSE 1 END LIMIT 1",
            (project["id"], item_id, title, item_id),
        ).fetchone()
        if existing is None:
            sort_order = db.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM project_checklist_items WHERE object_id = ?",
                (project["id"],),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO project_checklist_items (object_id, source_template_item_id, title, description, sort_order, validity_years) VALUES (?, ?, ?, ?, ?, 2)",
                (project["id"], item_id, title, description, sort_order),
            )
        else:
            db.execute(
                "UPDATE project_checklist_items SET validity_years = 2, description = CASE WHEN TRIM(description) = '' THEN ? ELSE description END, document_id = NULL, status = CASE WHEN issue_date <> '' THEN 'received' ELSE 'missing' END WHERE id = ?",
                (description, existing["id"]),
            )
    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('geodetic_survey_template_migrated_v2', '1')"
    )


def close_db(_error: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()
