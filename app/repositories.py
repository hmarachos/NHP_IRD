from sqlite3 import Row

from app.database import get_db
from app.models import ParsedDocument


class ObjectRepository:
    def create(self, *, contract_number: str, name: str, template_id: int | None = None) -> int:
        db = get_db()
        cursor = db.execute(
            "INSERT INTO objects (contract_number, name, template_id) VALUES (?, ?, ?)",
            (contract_number, name, template_id),
        )
        object_id = int(cursor.lastrowid)
        if template_id:
            ChecklistRepository().apply_template(object_id, template_id)
        db.commit()
        return object_id

    def list_all(self) -> list[Row]:
        return list(
            get_db().execute(
                """
                SELECT
                    objects.*,
                    COUNT(documents.id) AS documents_count,
                    MAX(documents.created_at) AS last_document_at
                FROM objects
                LEFT JOIN documents ON documents.object_id = objects.id AND documents.archived_at IS NULL
                WHERE objects.archived_at IS NULL
                GROUP BY objects.id
                ORDER BY objects.created_at DESC, objects.id DESC
                """
            )
        )

    def list_archived(self) -> list[Row]:
        return list(
            get_db().execute(
                "SELECT * FROM objects WHERE archived_at IS NOT NULL ORDER BY archived_at DESC"
            )
        )

    def get(self, object_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM objects WHERE id = ?", (object_id,)
        ).fetchone()

    def update(self, object_id: int, *, contract_number: str, name: str) -> bool:
        db = get_db()
        cursor = db.execute(
            """
            UPDATE objects
            SET contract_number = ?, name = ?
            WHERE id = ?
            """,
            (contract_number, name, object_id),
        )
        db.execute(
            """
            UPDATE documents
            SET contract_number = ?, object_name = ?
            WHERE object_id = ?
            """,
            (contract_number, name, object_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def archive(self, object_id: int) -> bool:
        db = get_db()
        cursor = db.execute(
            "UPDATE objects SET archived_at = CURRENT_TIMESTAMP WHERE id = ? AND archived_at IS NULL",
            (object_id,),
        )
        db.commit()
        return cursor.rowcount > 0

    def restore(self, object_id: int) -> bool:
        db = get_db()
        cursor = db.execute(
            "UPDATE objects SET archived_at = NULL WHERE id = ? AND archived_at IS NOT NULL",
            (object_id,),
        )
        db.commit()
        return cursor.rowcount > 0

    def permanently_delete_archived(self, object_id: int) -> list[str] | None:
        """Delete an archived project and its document rows, returning stored file names."""
        db = get_db()
        project = db.execute(
            "SELECT id FROM objects WHERE id = ? AND archived_at IS NOT NULL",
            (object_id,),
        ).fetchone()
        if project is None:
            return None

        stored_filenames = [
            row["stored_filename"]
            for row in db.execute(
                "SELECT stored_filename FROM documents WHERE object_id = ?",
                (object_id,),
            )
            if row["stored_filename"]
        ]
        try:
            db.execute("DELETE FROM documents WHERE object_id = ?", (object_id,))
            cursor = db.execute(
                "DELETE FROM objects WHERE id = ? AND archived_at IS NOT NULL",
                (object_id,),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return None
            db.commit()
        except Exception:
            db.rollback()
            raise
        return stored_filenames


class DocumentRepository:
    def create(
        self,
        *,
        object_id: int,
        contract_number: str,
        object_name: str,
        original_filename: str,
        stored_filename: str,
        mime_type: str = "application/pdf",
        parsed: ParsedDocument,
        note: str = "",
        status: str = "processed",
        error: str = "",
    ) -> int:
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO documents (
                object_id, contract_number, object_name, original_filename, stored_filename,
                mime_type, working_title, official_title, issuer, issue_date, valid_until,
                validity_text, no_expiration, raw_text, note, status, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                object_id,
                contract_number,
                object_name,
                original_filename,
                stored_filename,
                mime_type,
                parsed.working_title,
                parsed.official_title,
                parsed.issuer,
                parsed.issue_date,
                parsed.valid_until,
                parsed.validity_text,
                1 if parsed.no_expiration else 0,
                parsed.raw_text,
                note,
                status,
                error,
            ),
        )
        db.commit()
        document_id = int(cursor.lastrowid)
        if object_id and parsed.working_title:
            ChecklistRepository().match_document(object_id, document_id, parsed.working_title)
        db.commit()
        return document_id

    def list_recent(self) -> list[Row]:
        return list(
            get_db().execute(
                "SELECT * FROM documents WHERE archived_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 50"
            )
        )

    def list_all(self) -> list[Row]:
        return list(
            get_db().execute("SELECT * FROM documents WHERE archived_at IS NULL ORDER BY created_at DESC, id DESC")
        )

    def list_grouped(self) -> list[Row]:
        return list(
            get_db().execute(
                """
                SELECT *
                FROM documents
                WHERE archived_at IS NULL
                ORDER BY contract_number COLLATE NOCASE ASC, created_at DESC, id DESC
                """
            )
        )

    def list_by_object(self, object_id: int, *, include_archived: bool = False) -> list[Row]:
        archived_filter = "" if include_archived else "AND archived_at IS NULL"
        return list(
            get_db().execute(
                f"""
                SELECT *
                FROM documents
                WHERE object_id = ? {archived_filter}
                ORDER BY created_at DESC, id DESC
                """,
                (object_id,),
            )
        )

    def get(self, document_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()

    def list_archived_by_object(self, object_id: int) -> list[Row]:
        return list(get_db().execute(
            "SELECT * FROM documents WHERE object_id = ? AND archived_at IS NOT NULL ORDER BY archived_at DESC, id DESC",
            (object_id,),
        ))

    def list_stored_filenames_by_object(self, object_id: int) -> list[str]:
        rows = get_db().execute(
            """
            SELECT stored_filename
            FROM documents
            WHERE object_id = ? AND stored_filename <> ''
            """,
            (object_id,),
        )
        return [row["stored_filename"] for row in rows]

    def delete(self, document_id: int) -> bool:
        db = get_db()
        cursor = db.execute(
            "UPDATE documents SET archived_at = CURRENT_TIMESTAMP WHERE id = ? AND archived_at IS NULL",
            (document_id,),
        )
        db.commit()
        return cursor.rowcount > 0

    def restore(self, document_id: int) -> bool:
        db = get_db()
        row = db.execute(
            "SELECT object_id, working_title FROM documents WHERE id = ? AND archived_at IS NOT NULL",
            (document_id,),
        ).fetchone()
        had_checklist_link = db.execute(
            "SELECT 1 FROM project_checklist_items WHERE document_id = ? LIMIT 1",
            (document_id,),
        ).fetchone() is not None
        cursor = db.execute(
            "UPDATE documents SET archived_at = NULL WHERE id = ? AND archived_at IS NOT NULL",
            (document_id,),
        )
        if cursor.rowcount and row and not had_checklist_link and row["object_id"] and row["working_title"]:
            ChecklistRepository().match_document(row["object_id"], document_id, row["working_title"])
        db.commit()
        return cursor.rowcount > 0

    def delete_by_object(self, object_id: int) -> int:
        db = get_db()
        cursor = db.execute(
            "UPDATE documents SET archived_at = CURRENT_TIMESTAMP WHERE object_id = ? AND archived_at IS NULL",
            (object_id,),
        )
        db.commit()
        return cursor.rowcount

    def update_note(self, document_id: int, note: str) -> bool:
        db = get_db()
        cursor = db.execute(
            "UPDATE documents SET note = ? WHERE id = ?",
            (note, document_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def update_fields(self, document_id: int, values: dict[str, object]) -> bool:
        allowed = {
            "working_title", "official_title", "issuer", "issue_date",
            "valid_until", "no_expiration",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return False
        assignments = ", ".join(f"{key} = ?" for key in updates)
        db = get_db()
        cursor = db.execute(
            f"UPDATE documents SET {assignments} WHERE id = ?",
            (*updates.values(), document_id),
        )
        db.commit()
        return cursor.rowcount > 0


class ChecklistRepository:
    def list_templates(self) -> list[Row]:
        return list(
            get_db().execute(
                "SELECT * FROM checklist_templates ORDER BY name COLLATE NOCASE"
            )
        )

    def get_template(self, template_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM checklist_templates WHERE id = ?", (template_id,)
        ).fetchone()

    def list_template_items(self, template_id: int) -> list[Row]:
        return list(
            get_db().execute(
                "SELECT * FROM checklist_template_items WHERE template_id = ? ORDER BY sort_order, id",
                (template_id,),
            )
        )

    def create_template(self, name: str, description: str = "") -> int:
        db = get_db()
        cursor = db.execute(
            "INSERT INTO checklist_templates (name, description) VALUES (?, ?)",
            (name, description),
        )
        db.commit()
        return int(cursor.lastrowid)

    def update_template(self, template_id: int, name: str, description: str) -> bool:
        db = get_db()
        cursor = db.execute(
            "UPDATE checklist_templates SET name = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (name, description, template_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def delete_template(self, template_id: int) -> bool:
        db = get_db()
        cursor = db.execute("DELETE FROM checklist_templates WHERE id = ?", (template_id,))
        db.commit()
        return cursor.rowcount > 0

    def add_template_item(self, template_id: int, title: str, description: str = "") -> bool:
        db = get_db()
        next_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM checklist_template_items WHERE template_id = ?",
            (template_id,),
        ).fetchone()[0]
        cursor = db.execute(
            "INSERT INTO checklist_template_items (template_id, title, description, sort_order) VALUES (?, ?, ?, ?)",
            (template_id, title, description, next_order),
        )
        db.commit()
        return cursor.rowcount > 0

    def update_template_item(self, item_id: int, title: str, description: str) -> bool:
        db = get_db()
        cursor = db.execute(
            "UPDATE checklist_template_items SET title = ?, description = ? WHERE id = ?",
            (title, description, item_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def delete_template_item(self, item_id: int) -> bool:
        db = get_db()
        cursor = db.execute("DELETE FROM checklist_template_items WHERE id = ?", (item_id,))
        db.commit()
        return cursor.rowcount > 0

    def apply_template(self, object_id: int, template_id: int) -> int:
        db = get_db()
        template = self.get_template(template_id)
        if template is None:
            return 0
        existing = {
            self._normalize(row[0])
            for row in db.execute(
                "SELECT title FROM project_checklist_items WHERE object_id = ?", (object_id,)
            )
        }
        added = 0
        for item in self.list_template_items(template_id):
            key = self._normalize(item["title"])
            if key in existing:
                continue
            db.execute(
                "INSERT INTO project_checklist_items (object_id, source_template_item_id, title, description, sort_order) VALUES (?, ?, ?, ?, ?)",
                (object_id, item["id"], item["title"], item["description"], item["sort_order"]),
            )
            existing.add(key)
            added += 1
        db.execute("UPDATE objects SET template_id = ? WHERE id = ?", (template_id, object_id))
        db.commit()
        return added

    def list_project_items(self, object_id: int) -> list[Row]:
        return list(
            get_db().execute(
                """
                SELECT checklist.*, documents.working_title AS linked_document_title
                FROM project_checklist_items AS checklist
                LEFT JOIN documents ON documents.id = checklist.document_id
                WHERE checklist.object_id = ?
                ORDER BY checklist.sort_order, checklist.id
                """,
                (object_id,),
            )
        )

    def update_project_item(
        self, object_id: int, item_id: int, status: str, document_id: int | None = None
    ) -> bool:
        if status not in {"missing", "received", "not_required"}:
            return False
        db = get_db()
        if document_id is not None:
            linked = db.execute(
                "SELECT id FROM documents WHERE id = ? AND object_id = ?",
                (document_id, object_id),
            ).fetchone()
            if linked is None:
                document_id = None
        cursor = db.execute(
            """
            UPDATE project_checklist_items
            SET status = ?, document_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND object_id = ?
            """,
            (status, document_id if status == "received" else None, item_id, object_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def match_document(self, object_id: int, document_id: int, title: str) -> None:
        normalized = self._normalize(title)
        if not normalized:
            return
        db = get_db()
        for row in db.execute(
            "SELECT id, title FROM project_checklist_items WHERE object_id = ? AND status <> 'not_required' ORDER BY id",
            (object_id,),
        ):
            if self._normalize(row["title"]) == normalized:
                db.execute(
                    "UPDATE project_checklist_items SET status = 'received', document_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (document_id, row["id"]),
                )
                db.commit()
                return

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join((value or "").casefold().split())
