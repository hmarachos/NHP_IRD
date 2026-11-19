"""Import a read-only snapshot of the Flask SQLite store into PostgreSQL."""
import hashlib
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Model

from migration_portal import models


# Source and destination column names, ordered exactly as in the legacy tables.
IMPORTS = (
    ("app_settings", models.AppSetting, ("key", "value"), ("key", "value"), "key"),
    ("checklist_templates", models.ChecklistTemplate, ("id", "name", "description", "created_at", "updated_at"), ("id", "name", "description", "created_at", "updated_at"), "id"),
    ("objects", models.Project, ("id", "contract_number", "contract_date", "name", "customer", "template_id", "archived_at", "created_at"), ("id", "contract_number", "contract_date", "name", "customer", "template_id", "archived_at", "created_at"), "id"),
    ("documents", models.Document, ("id", "object_id", "contract_number", "object_name", "original_filename", "stored_filename", "mime_type", "working_title", "official_title", "issuer", "issue_date", "valid_until", "validity_text", "no_expiration", "raw_text", "note", "status", "error", "created_at", "document_number", "archived_at"), ("id", "project_id", "contract_number", "object_name", "original_filename", "stored_filename", "mime_type", "working_title", "official_title", "issuer", "issue_date", "valid_until", "validity_text", "no_expiration", "raw_text", "note", "status", "error", "created_at", "document_number", "archived_at"), "id"),
    ("correspondence", models.Correspondence, ("id", "object_id", "direction", "number", "letter_date", "sender", "recipient", "subject", "summary", "original_filename", "stored_filename", "mime_type", "raw_text", "status", "error", "record_type", "valid_until", "created_at"), ("id", "project_id", "direction", "number", "letter_date", "sender", "recipient", "subject", "summary", "original_filename", "stored_filename", "mime_type", "raw_text", "status", "error", "record_type", "valid_until", "created_at"), "id"),
    ("checklist_template_items", models.ChecklistTemplateItem, ("id", "template_id", "title", "description", "sort_order", "validity_years"), ("id", "template_id", "title", "description", "sort_order", "validity_years"), "id"),
    ("project_checklist_items", models.ProjectChecklistItem, ("id", "object_id", "source_template_item_id", "title", "description", "sort_order", "status", "document_id", "validity_years", "issue_date", "valid_until", "note", "replacement_required", "created_at", "updated_at"), ("id", "project_id", "source_template_item_id", "title", "description", "sort_order", "status", "document_id", "validity_years", "issue_date", "valid_until", "note", "replacement_required", "created_at", "updated_at"), "id"),
    ("project_checklist_documents", models.ChecklistDocument, ("checklist_item_id", "document_id", "sort_order", "created_at"), ("checklist_item_id", "document_id", "sort_order", "created_at"), "checklist_item_id, document_id"),
    ("handover_acts", models.HandoverAct, ("id", "object_id", "period_start", "period_end", "stored_filename", "source", "created_at", "sent_at", "email_error"), ("id", "project_id", "period_start", "period_end", "stored_filename", "source", "created_at", "sent_at", "email_error"), "id"),
    ("expiry_notifications", models.ExpiryNotification, ("id", "document_id", "notification_type", "sent_at"), ("id", "document_id", "notification_type", "sent_at"), "id"),
    ("checklist_expiry_notifications", models.ChecklistExpiryNotification, ("id", "checklist_item_id", "valid_until", "sent_at"), ("id", "checklist_item_id", "valid_until", "sent_at"), "id"),
)
BOOL_COLUMNS = {"documents": {"no_expiration"}, "project_checklist_items": {"replacement_required"}}


def _update_hash(hasher, values, columns, bool_columns=()):
    values = list(values)
    for index, column in enumerate(columns):
        if column in bool_columns and values[index] is not None:
            values[index] = bool(values[index])
    hasher.update(json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    hasher.update(b"\n")


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Command(BaseCommand):
    help = "Import legacy SQLite records into the isolated Django PostgreSQL database."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="Path to the Flask SQLite database; it is opened read-only.")
        parser.add_argument("--replace", action="store_true", help="Atomically replace existing Django records after confirmation by flag.")

    def handle(self, *args, **options):
        source = Path(options["source"]).resolve()
        if not source.is_file():
            raise CommandError(f"Source database does not exist: {source}")

        source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        source_db.row_factory = sqlite3.Row
        try:
            integrity = source_db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise CommandError(f"Source integrity check failed: {integrity}")
            available = {row[0] for row in source_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = {table for table, *_ in IMPORTS} - available
            if missing:
                raise CommandError(f"Source schema is missing tables: {', '.join(sorted(missing))}")
            source_counts = {table: source_db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table, *_ in IMPORTS}
            source_hashes = {}
            for table, _model, source_columns, _target_columns, order_by in IMPORTS:
                digest = hashlib.sha256()
                cursor = source_db.execute(f'SELECT {", ".join(source_columns)} FROM "{table}" ORDER BY {order_by}')
                for row in cursor:
                    _update_hash(digest, row, source_columns, BOOL_COLUMNS.get(table, set()))
                source_hashes[table] = digest.hexdigest()

            destination_has_rows = models.LegacyImportState.objects.exists() or any(model.objects.exists() for _table, model, *_rest in IMPORTS)
            if destination_has_rows and not options["replace"]:
                raise CommandError("PostgreSQL already contains application rows. Re-run with --replace to atomically replace them.")

            counts = {}
            source_file_hash = _file_hash(source)
            with transaction.atomic():
                if destination_has_rows:
                    models.LegacyImportState.objects.all().delete()
                    for _table, model, *_rest in reversed(IMPORTS):
                        model.objects.all().delete()
                for table, model, source_columns, target_columns, order_by in IMPORTS:
                    cursor = source_db.execute(f'SELECT {", ".join(source_columns)} FROM "{table}" ORDER BY {order_by}')
                    inserted = 0
                    while batch := cursor.fetchmany(1000):
                        records = []
                        for row in batch:
                            values = dict(zip(target_columns, row))
                            if table == "project_checklist_documents":
                                records.append(model(**values))
                            else:
                                records.append(model(**values))
                        model.objects.bulk_create(records, batch_size=1000)
                        inserted += len(records)
                    counts[table] = inserted
                    if inserted != source_counts[table]:
                        raise CommandError(f"Inserted count mismatch for {table}: {inserted} != {source_counts[table]}")

                # Explicit source IDs do not advance PostgreSQL sequences.
                with connection.cursor() as cursor:
                    for table, model, source_columns, target_columns, order_by in IMPORTS:
                        if "id" not in source_columns:
                            continue
                        cursor.execute(
                            f"SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table}",
                            [table],
                        )
                # Validate every imported value, not just table row counts.
                destination_hashes = {}
                for table, model, source_columns, target_columns, order_by in IMPORTS:
                    digest = hashlib.sha256()
                    fields = list(target_columns)
                    for row in model.objects.order_by(*[name.strip() for name in order_by.split(",")]).values_list(*fields).iterator(chunk_size=1000):
                        _update_hash(digest, row, source_columns, BOOL_COLUMNS.get(table, set()))
                    destination_hashes[table] = digest.hexdigest()
                if source_hashes != destination_hashes:
                    mismatch = [table for table in source_hashes if source_hashes[table] != destination_hashes[table]]
                    raise CommandError(f"Imported data differs from SQLite source in: {', '.join(mismatch)}")
                models.LegacyImportState.objects.update_or_create(
                    key="flask-sqlite-v1",
                    defaults={
                        "source_path": str(source),
                        "source_sha256": source_file_hash,
                        "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )

            report = {
                "source": str(source),
                "destination": "PostgreSQL Django database",
                "source_sha256": source_file_hash,
                "source_integrity": integrity,
                "tables": len(counts),
                "rows": counts,
                "sha256_by_table": source_hashes,
                "verified": "all source columns match in order",
            }
            report_path = source.with_name("django-postgres-import-report.json")
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Imported and verified {sum(counts.values())} rows across {len(counts)} tables into PostgreSQL. Report: {report_path}"))
        finally:
            source_db.close()
