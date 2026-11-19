"""Compare a read-only Flask SQLite snapshot with Django PostgreSQL."""

import hashlib
import json
import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from migration_portal.management.commands.import_legacy_sqlite import (
    BOOL_COLUMNS,
    IMPORTS,
    _update_hash,
)


def _normalized(table, columns, values):
    values = list(values)
    for index, column in enumerate(columns):
        if column in BOOL_COLUMNS.get(table, set()) and values[index] is not None:
            values[index] = bool(values[index])
    return values


def _stable(values):
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str)


class Command(BaseCommand):
    help = "Read-only row-level audit of Flask SQLite against Django PostgreSQL."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="Path to Flask SQLite database; opened read-only.")
        parser.add_argument("--sample-limit", type=int, default=20, help="Maximum difference keys shown per table.")

    def handle(self, *args, **options):
        source = Path(options["source"]).resolve()
        if not source.is_file():
            raise CommandError(f"Source database does not exist: {source}")
        if options["sample_limit"] < 0:
            raise CommandError("--sample-limit must be zero or greater.")

        source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        source_db.row_factory = sqlite3.Row
        differences = []
        table_reports = []
        try:
            source_db.execute("BEGIN")
            integrity = source_db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise CommandError(f"Source integrity check failed: {integrity}")
            available = {row[0] for row in source_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = {table for table, *_ in IMPORTS} - available
            if missing:
                raise CommandError(f"Source schema is missing tables: {', '.join(sorted(missing))}")

            for table, model, source_columns, target_columns, order_by in IMPORTS:
                sort_columns = [column.strip() for column in order_by.split(",")]
                source_rows = list(source_db.execute(
                    f'SELECT {", ".join(source_columns)} FROM "{table}" ORDER BY {order_by}'
                ))
                target_rows = list(
                    model.objects.order_by(*sort_columns).values_list(*target_columns)
                )

                source_hash = hashlib.sha256()
                target_hash = hashlib.sha256()
                for row in source_rows:
                    _update_hash(source_hash, row, source_columns, BOOL_COLUMNS.get(table, set()))
                for row in target_rows:
                    _update_hash(target_hash, row, source_columns, BOOL_COLUMNS.get(table, set()))

                source_key_indexes = [source_columns.index(column) for column in sort_columns]
                target_key_indexes = [target_columns.index(column) for column in sort_columns]
                source_by_key = {}
                for row in source_rows:
                    values = _normalized(table, source_columns, row)
                    key = _stable([values[index] for index in source_key_indexes])
                    source_by_key[key] = values
                target_by_key = {}
                for row in target_rows:
                    values = _normalized(table, target_columns, row)
                    key = _stable([values[index] for index in target_key_indexes])
                    target_by_key[key] = values

                source_keys, target_keys = set(source_by_key), set(target_by_key)
                missing_keys = sorted(source_keys - target_keys)
                extra_keys = sorted(target_keys - source_keys)
                common_keys = source_keys & target_keys
                changed_keys = sorted(
                    key for key in common_keys
                    if _stable(source_by_key[key]) != _stable(target_by_key[key])
                )
                limit = options["sample_limit"]

                table_reports.append({
                    "table": table,
                    "source_rows": len(source_rows),
                    "postgres_rows": len(target_rows),
                    "source_sha256": source_hash.hexdigest(),
                    "postgres_sha256": target_hash.hexdigest(),
                    "matches": not (missing_keys or extra_keys or changed_keys),
                })
                if missing_keys or extra_keys or changed_keys:
                    differences.append({
                        "table": table,
                        "missing_in_postgres": [json.loads(key) for key in missing_keys[:limit]],
                        "only_in_postgres": [json.loads(key) for key in extra_keys[:limit]],
                        "changed_keys": [json.loads(key) for key in changed_keys[:limit]],
                        "omitted_differences": max(0, len(missing_keys) - limit)
                        + max(0, len(extra_keys) - limit) + max(0, len(changed_keys) - limit),
                    })
        finally:
            source_db.close()

        self.stdout.write(json.dumps({
            "source": str(source),
            "source_integrity": integrity,
            "tables_compared": len(table_reports),
            "all_match": not differences,
            "tables": table_reports,
            "differences": differences,
        }, ensure_ascii=False, indent=2))
        if differences:
            self.stderr.write(self.style.WARNING("SQLite and PostgreSQL differ; audit made no changes."))
        else:
            self.stdout.write(self.style.SUCCESS("All imported table rows match; audit made no changes."))
