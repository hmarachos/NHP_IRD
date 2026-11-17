from sqlite3 import Row

from app.database import get_db
from app.models import ParsedDocument
from app.services.expiry import add_years, iso_date


class ObjectRepository:
    def create(self, *, contract_number: str, name: str, customer: str = "", contract_date: str = "", template_id: int | None = None) -> int:
        db = get_db()
        cursor = db.execute(
            "INSERT INTO objects (contract_number, contract_date, name, customer, template_id) VALUES (?, ?, ?, ?, ?)",
            (contract_number, contract_date, name, customer, template_id),
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

    def list_customers(self) -> list[str]:
        return [
            row["customer"]
            for row in get_db().execute(
                """
                SELECT MIN(TRIM(customer)) AS customer
                FROM objects
                WHERE TRIM(customer) <> ''
                GROUP BY LOWER(TRIM(customer))
                ORDER BY LOWER(MIN(TRIM(customer)))
                """
            )
        ]

    def get(self, object_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM objects WHERE id = ?", (object_id,)
        ).fetchone()

    def update(self, object_id: int, *, contract_number: str, name: str, customer: str = "", contract_date: str = "") -> bool:
        db = get_db()
        cursor = db.execute(
            """
            UPDATE objects
            SET contract_number = ?, contract_date = ?, name = ?, customer = ?
            WHERE id = ?
            """,
            (contract_number, contract_date, name, customer, object_id),
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

    def permanently_delete(self, object_id: int) -> list[str] | None:
        """Delete a project and its document rows, returning stored file names."""
        db = get_db()
        project = db.execute(
            "SELECT id FROM objects WHERE id = ?",
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
                "DELETE FROM objects WHERE id = ?",
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
                validity_text, no_expiration, raw_text, note, status, error, document_number
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                parsed.document_number,
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

    def list_received_in_period(self, object_id: int, start: str, end: str) -> list[Row]:
        return list(
            get_db().execute(
                """
                SELECT * FROM documents
                WHERE object_id = ? AND archived_at IS NULL AND stored_filename <> ''
                  AND created_at >= ? AND created_at < ?
                ORDER BY created_at, id
                """,
                (object_id, start, end),
            )
        )

    def list_expiring_within(self, today: str, cutoff: str) -> list[Row]:
        return list(
            get_db().execute(
                """
                SELECT documents.*,
                       objects.name AS notification_project_name,
                       objects.contract_number AS notification_contract_number
                FROM documents
                JOIN objects ON objects.id = documents.object_id
                WHERE documents.archived_at IS NULL
                  AND objects.archived_at IS NULL
                  AND documents.no_expiration = 0
                  AND documents.valid_until <> ''
                  AND date(documents.valid_until) > date(?)
                  AND date(documents.valid_until) <= date(?)
                  AND NOT EXISTS (
                      SELECT 1 FROM expiry_notifications
                      WHERE expiry_notifications.document_id = documents.id
                        AND expiry_notifications.notification_type = ('expiry_30_days:' || documents.valid_until)
                  )
                ORDER BY date(documents.valid_until), objects.name, documents.id
                """,
                (today, cutoff),
            )
        )

    def mark_expiry_notifications_sent(self, documents: list[Row]) -> None:
        db = get_db()
        db.executemany(
            "INSERT OR IGNORE INTO expiry_notifications (document_id, notification_type) VALUES (?, ?)",
            [(row["id"], f"expiry_30_days:{row['valid_until']}") for row in documents],
        )
        db.commit()

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
        links = list(db.execute(
            """
            SELECT checklist.id, checklist.object_id, checklist.title, checklist.document_id
            FROM project_checklist_documents AS link
            JOIN project_checklist_items AS checklist ON checklist.id = link.checklist_item_id
            WHERE link.document_id = ?
            """,
            (document_id,),
        ))
        for item in links:
            if item["document_id"] != document_id:
                continue
            if ChecklistRepository.allows_multiple_documents(item["title"]):
                next_document = db.execute(
                    """
                    SELECT link.document_id
                    FROM project_checklist_documents AS link
                    JOIN documents ON documents.id = link.document_id
                    WHERE link.checklist_item_id = ? AND link.document_id <> ?
                      AND documents.archived_at IS NULL
                    ORDER BY link.sort_order, link.created_at, link.document_id
                    LIMIT 1
                    """,
                    (item["id"], document_id),
                ).fetchone()
                replacement_id = next_document["document_id"] if next_document else None
                db.execute(
                    "UPDATE project_checklist_items SET document_id = ?, status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (replacement_id, "received" if replacement_id else "missing", item["id"]),
                )
            else:
                db.execute(
                    "UPDATE project_checklist_items SET status = 'missing', document_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (item["id"],),
                )
        # Also handle legacy document_id links that have not yet been copied
        # into the association table (for example, immediately after upgrade).
        db.execute(
            """
            UPDATE project_checklist_items
            SET status = 'missing', document_id = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE document_id = ? AND validity_years = 0
              AND NOT EXISTS (
                  SELECT 1 FROM project_checklist_documents
                  WHERE project_checklist_documents.checklist_item_id = project_checklist_items.id
                    AND project_checklist_documents.document_id <> ?
              )
            """,
            (document_id, document_id),
        )
        cursor = db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        db.commit()
        return cursor.rowcount > 0

    def restore(self, document_id: int) -> bool:
        db = get_db()
        row = db.execute(
            "SELECT object_id, working_title FROM documents WHERE id = ? AND archived_at IS NOT NULL",
            (document_id,),
        ).fetchone()
        had_checklist_link = db.execute(
            """SELECT 1 FROM project_checklist_items WHERE document_id = ?
               UNION ALL SELECT 1 FROM project_checklist_documents WHERE document_id = ? LIMIT 1""",
            (document_id, document_id),
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
            "valid_until", "no_expiration", "document_number",
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
    CUSTOMER_LETTER_ITEM_TITLE = "Сведения для разработки сметной документации (по типовой форме)"
    PROJECT_ASSIGNMENT_ITEM_TITLE = "Задание на проектирование"
    MULTI_DOCUMENT_ITEM_TITLES = {
        CUSTOMER_LETTER_ITEM_TITLE,
        PROJECT_ASSIGNMENT_ITEM_TITLE,
    }

    @classmethod
    def allows_multiple_documents(cls, title: str) -> bool:
        normalized = cls._normalize(title)
        return any(normalized == cls._normalize(item) for item in cls.MULTI_DOCUMENT_ITEM_TITLES)

    @classmethod
    def uses_customer_letter_subject(cls, title: str) -> bool:
        return cls._normalize(title) == cls._normalize(cls.CUSTOMER_LETTER_ITEM_TITLE)

    def list_linked_documents(self, object_id: int) -> list[Row]:
        return list(get_db().execute(
            """
            SELECT attachments.checklist_item_id, attachments.document_id
            FROM (
                SELECT checklist.id AS checklist_item_id, documents.id AS document_id,
                       -1 AS sort_order, documents.created_at AS created_at
                FROM project_checklist_items AS checklist
                JOIN documents ON documents.id = checklist.document_id
                WHERE checklist.object_id = ? AND documents.archived_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM project_checklist_documents AS existing
                      WHERE existing.checklist_item_id = checklist.id
                        AND existing.document_id = documents.id
                  )
                UNION ALL
                SELECT link.checklist_item_id, documents.id, link.sort_order, link.created_at
                FROM project_checklist_documents AS link
                JOIN project_checklist_items AS checklist ON checklist.id = link.checklist_item_id
                JOIN documents ON documents.id = link.document_id
                WHERE checklist.object_id = ? AND documents.archived_at IS NULL
            ) AS attachments
            ORDER BY attachments.checklist_item_id, attachments.sort_order, attachments.created_at, attachments.document_id
            """,
            (object_id, object_id),
        ))

    def link_document(self, object_id: int, item_id: int, document_id: int) -> bool:
        db = get_db()
        item = db.execute(
            "SELECT title, validity_years, document_id FROM project_checklist_items WHERE id = ? AND object_id = ?",
            (item_id, object_id),
        ).fetchone()
        document = db.execute(
            "SELECT id FROM documents WHERE id = ? AND object_id = ? AND archived_at IS NULL",
            (document_id, object_id),
        ).fetchone()
        if item is None or document is None or (item["validity_years"] > 0 and item["title"] != "Инженерно-геодезические изыскания"):
            return False
        if self.allows_multiple_documents(item["title"]):
            order = db.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM project_checklist_documents WHERE checklist_item_id = ?",
                (item_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO project_checklist_documents (checklist_item_id, document_id, sort_order) VALUES (?, ?, ?)",
                (item_id, document_id, order),
            )
            primary = db.execute(
                "SELECT id FROM documents WHERE id = ? AND archived_at IS NULL",
                (item["document_id"],),
            ).fetchone() if item["document_id"] else None
            primary_id = primary["id"] if primary else document_id
        else:
            db.execute("DELETE FROM project_checklist_documents WHERE checklist_item_id = ?", (item_id,))
            db.execute(
                "INSERT INTO project_checklist_documents (checklist_item_id, document_id, sort_order) VALUES (?, ?, 0)",
                (item_id, document_id),
            )
            primary_id = document_id
        cursor = db.execute(
            "UPDATE project_checklist_items SET status = 'received', document_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND object_id = ?",
            (primary_id, item_id, object_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def list_expiring_within(self, today: str, cutoff: str) -> list[Row]:
        return list(get_db().execute(
            """
            SELECT checklist.id AS checklist_item_id,
                   checklist.title AS working_title,
                   checklist.title AS official_title,
                   checklist.title AS original_filename,
                   '' AS issuer,
                   checklist.issue_date,
                   checklist.valid_until,
                   objects.name AS notification_project_name,
                   objects.contract_number AS notification_contract_number
            FROM project_checklist_items AS checklist
            JOIN objects ON objects.id = checklist.object_id
            WHERE checklist.validity_years > 0
              AND checklist.issue_date <> ''
              AND checklist.valid_until <> ''
              AND objects.archived_at IS NULL
              AND date(checklist.valid_until) > date(?)
              AND date(checklist.valid_until) <= date(?)
              AND NOT EXISTS (
                  SELECT 1 FROM checklist_expiry_notifications AS sent
                  WHERE sent.checklist_item_id = checklist.id
                    AND sent.valid_until = checklist.valid_until
              )
            ORDER BY date(checklist.valid_until), objects.name, checklist.id
            """,
            (today, cutoff),
        ))

    def mark_expiry_notifications_sent(self, items: list[Row]) -> None:
        db = get_db()
        db.executemany(
            "INSERT OR IGNORE INTO checklist_expiry_notifications (checklist_item_id, valid_until) VALUES (?, ?)",
            [(row["checklist_item_id"], row["valid_until"]) for row in items],
        )
        db.commit()

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

    def copy_template(self, template_id: int, name: str | None = None) -> int | None:
        db = get_db()
        source = self.get_template(template_id)
        if source is None:
            return None
        cursor = db.execute(
            "INSERT INTO checklist_templates (name, description) VALUES (?, ?)",
            ((name or f"{source['name']} — копия").strip(), source["description"]),
        )
        copied_id = int(cursor.lastrowid)
        db.execute(
            "INSERT INTO checklist_template_items (template_id, title, description, sort_order, validity_years) "
            "SELECT ?, title, description, sort_order, validity_years FROM checklist_template_items "
            "WHERE template_id = ? ORDER BY sort_order, id",
            (copied_id, template_id),
        )
        db.commit()
        return copied_id

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
        validity_years = 2 if title.strip() == "Инженерно-геодезические изыскания" else 0
        cursor = db.execute(
            "INSERT INTO checklist_template_items (template_id, title, description, sort_order, validity_years) VALUES (?, ?, ?, ?, ?)",
            (template_id, title, description, next_order, validity_years),
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
                "INSERT INTO project_checklist_items (object_id, source_template_item_id, title, description, sort_order, validity_years) VALUES (?, ?, ?, ?, ?, ?)",
                (object_id, item["id"], item["title"], item["description"], item["sort_order"], item["validity_years"]),
            )
            existing.add(key)
            added += 1
        db.execute("UPDATE objects SET template_id = ? WHERE id = ?", (template_id, object_id))
        db.commit()
        return added

    def list_project_items(self, object_id: int) -> list[Row]:
        db = get_db()
        # A received checklist item must point to a live project document.
        # Clear stale links left by older versions or by externally removed files.
        db.execute(
            """
            UPDATE project_checklist_items
            SET status = 'missing', document_id = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE object_id = ?
              AND status = 'received'
              AND validity_years = 0
              AND NOT EXISTS (
                  SELECT 1 FROM documents
                  WHERE documents.id = project_checklist_items.document_id
                    AND documents.archived_at IS NULL
              )
              AND NOT EXISTS (
                  SELECT 1 FROM project_checklist_documents AS link
                  JOIN documents ON documents.id = link.document_id
                  WHERE link.checklist_item_id = project_checklist_items.id
                    AND documents.archived_at IS NULL
              )
            """,
            (object_id,),
        )
        db.commit()
        return list(
            db.execute(
                """
                SELECT checklist.*, documents.working_title AS linked_document_title,
                       COALESCE(NULLIF(TRIM(documents.issuer), ''), (
                           SELECT group_concat(NULLIF(TRIM(linked.issuer), ''), '; ')
                           FROM project_checklist_documents AS linked_documents
                           JOIN documents AS linked ON linked.id = linked_documents.document_id
                           WHERE linked_documents.checklist_item_id = checklist.id
                       )) AS linked_document_issuer,
                       COALESCE(NULLIF(TRIM(documents.document_number), ''), (
                           SELECT group_concat(NULLIF(TRIM(linked.document_number), ''), '; ')
                           FROM project_checklist_documents AS linked_documents
                           JOIN documents AS linked ON linked.id = linked_documents.document_id
                           WHERE linked_documents.checklist_item_id = checklist.id
                       )) AS linked_document_number,
                       COALESCE(NULLIF(TRIM(documents.issue_date), ''), (
                           SELECT group_concat(NULLIF(TRIM(linked.issue_date), ''), '; ')
                           FROM project_checklist_documents AS linked_documents
                           JOIN documents AS linked ON linked.id = linked_documents.document_id
                           WHERE linked_documents.checklist_item_id = checklist.id
                       )) AS linked_document_issue_date,
                       COALESCE(NULLIF(TRIM(documents.note), ''), (
                           SELECT group_concat(NULLIF(TRIM(linked.note), ''), '; ')
                           FROM project_checklist_documents AS linked_documents
                           JOIN documents AS linked ON linked.id = linked_documents.document_id
                           WHERE linked_documents.checklist_item_id = checklist.id
                       )) AS linked_document_note,
                       documents.archived_at AS linked_document_archived_at
                FROM project_checklist_items AS checklist
                LEFT JOIN documents ON documents.id = checklist.document_id
                WHERE checklist.object_id = ?
                ORDER BY
                    CASE
                        WHEN checklist.status = 'received' THEN 0
                        WHEN documents.id IS NULL OR documents.archived_at IS NOT NULL THEN 1
                        ELSE 2
                    END,
                    checklist.sort_order,
                    checklist.id
                """,
                (object_id,),
            )
        )

    def add_project_item(self, object_id: int, title: str, note: str = "") -> int | None:
        db = get_db()
        normalized = self._normalize(title)
        if not normalized:
            return None
        existing_titles = db.execute(
            "SELECT title FROM project_checklist_items WHERE object_id = ?",
            (object_id,),
        )
        if any(self._normalize(row["title"]) == normalized for row in existing_titles):
            return None
        sort_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM project_checklist_items WHERE object_id = ?",
            (object_id,),
        ).fetchone()[0]
        cursor = db.execute(
            "INSERT INTO project_checklist_items (object_id, title, note, sort_order) VALUES (?, ?, ?, ?)",
            (object_id, title.strip(), note.strip()[:2000], sort_order),
        )
        db.commit()
        return int(cursor.lastrowid)

    def add_project_template_item(self, object_id: int, template_item_id: int) -> int | None:
        """Add one item from this project's assigned template to its checklist."""
        db = get_db()
        template_item = db.execute(
            """
            SELECT template_items.*
            FROM checklist_template_items AS template_items
            JOIN objects ON objects.template_id = template_items.template_id
            WHERE objects.id = ? AND template_items.id = ?
            """,
            (object_id, template_item_id),
        ).fetchone()
        if template_item is None:
            return None

        normalized_title = self._normalize(template_item["title"])
        existing = db.execute(
            "SELECT source_template_item_id, title FROM project_checklist_items WHERE object_id = ?",
            (object_id,),
        )
        if any(
            row["source_template_item_id"] == template_item_id
            or self._normalize(row["title"]) == normalized_title
            for row in existing
        ):
            return None

        sort_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM project_checklist_items WHERE object_id = ?",
            (object_id,),
        ).fetchone()[0]
        cursor = db.execute(
            """
            INSERT INTO project_checklist_items
                (object_id, source_template_item_id, title, description, sort_order, validity_years)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                object_id,
                template_item_id,
                template_item["title"],
                template_item["description"],
                sort_order,
                template_item["validity_years"],
            ),
        )
        db.commit()
        return int(cursor.lastrowid)

    def update_project_item(
        self, object_id: int, item_id: int, status: str, document_id: int | None = None,
        issue_date: str | None = None, note: str | None = None,
    ) -> bool:
        db = get_db()
        item = db.execute(
            "SELECT validity_years, document_id FROM project_checklist_items WHERE id = ? AND object_id = ?",
            (item_id, object_id),
        ).fetchone()
        if item is None:
            return False
        if item["validity_years"] > 0:
            normalized_date = iso_date(issue_date or "")
            valid_until = add_years(normalized_date, item["validity_years"])
            cursor = db.execute(
                "UPDATE project_checklist_items SET issue_date = ?, valid_until = ?, status = ?, note = COALESCE(?, note), updated_at = CURRENT_TIMESTAMP WHERE id = ? AND object_id = ?",
                (normalized_date, valid_until, "received" if normalized_date or item["document_id"] else "missing", note[:2000] if note is not None else None, item_id, object_id),
            )
            db.commit()
            return cursor.rowcount > 0
        if document_id is not None:
            linked = db.execute(
                "SELECT id FROM documents WHERE id = ? AND object_id = ? AND archived_at IS NULL",
                (document_id, object_id),
            ).fetchone()
            if linked is None:
                document_id = None
        db.execute("DELETE FROM project_checklist_documents WHERE checklist_item_id = ?", (item_id,))
        if document_id is not None:
            db.execute(
                "INSERT INTO project_checklist_documents (checklist_item_id, document_id, sort_order) VALUES (?, ?, 0)",
                (item_id, document_id),
            )
        status = "received" if document_id is not None else "missing"
        cursor = db.execute(
            """
            UPDATE project_checklist_items
            SET status = ?, document_id = ?, note = COALESCE(?, note), updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND object_id = ?
            """,
            (status, document_id if status == "received" else None, note[:2000] if note is not None else None, item_id, object_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def delete_project_item(self, object_id: int, item_id: int) -> bool:
        db = get_db()
        cursor = db.execute(
            "DELETE FROM project_checklist_items WHERE id = ? AND object_id = ?",
            (item_id, object_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def update_project_properties(
        self, object_id: int, item_id: int, title: str, description: str, note: str, issue_date: str, validity_years: int,
    ) -> dict | None:
        db = get_db()
        normalized_title = self._normalize(title)
        if not normalized_title:
            return None
        current = db.execute(
            "SELECT issue_date, title FROM project_checklist_items WHERE id = ? AND object_id = ?",
            (item_id, object_id),
        ).fetchone()
        if current is None:
            return None
        if normalized_title != self._normalize(current["title"]):
            existing_titles = db.execute(
                "SELECT title FROM project_checklist_items WHERE object_id = ? AND id <> ?",
                (object_id, item_id),
            )
            if any(self._normalize(row["title"]) == normalized_title for row in existing_titles):
                return None
        years = max(0, min(int(validity_years), 10))
        issue_date = issue_date.strip()[:10] or (current["issue_date"] or "")
        valid_until = add_years(issue_date, years) if years else ""
        db.execute(
            """UPDATE project_checklist_items
               SET title = ?, description = ?, note = ?, issue_date = ?, validity_years = ?, valid_until = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND object_id = ?""",
            (title.strip()[:300], description.strip()[:500], note.strip()[:2000], issue_date, years, valid_until, item_id, object_id),
        )
        db.commit()
        return {
            "title": title.strip()[:300],
            "description": description.strip()[:500],
            "note": note.strip()[:2000],
            "validity_years": years,
            "issue_date": issue_date,
            "valid_until": valid_until,
        }

    def update_replacement_required(self, object_id: int, item_id: int, required: bool) -> bool:
        db = get_db()
        cursor = db.execute(
            "UPDATE project_checklist_items SET replacement_required = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND object_id = ?",
            (1 if required else 0, item_id, object_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def match_document(self, object_id: int, document_id: int, title: str) -> None:
        normalized = self._normalize(title)
        if not normalized:
            return
        db = get_db()
        active_document = db.execute(
            "SELECT 1 FROM documents WHERE id = ? AND object_id = ? AND archived_at IS NULL",
            (document_id, object_id),
        ).fetchone()
        if active_document is None:
            return
        for row in db.execute(
            "SELECT id, title, status FROM project_checklist_items WHERE object_id = ? AND status = 'missing' AND validity_years = 0 ORDER BY id",
            (object_id,),
        ):
            if self.allows_multiple_documents(row["title"]):
                continue
            if self._normalize(row["title"]) == normalized:
                db.execute(
                    "UPDATE project_checklist_items SET status = 'received', document_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (document_id, row["id"]),
                )
                db.commit()
                return

    def sync_documents_to_checklist(self, object_id: int, documents: list[Row]) -> None:
        """Link already-uploaded documents to exact title matches in an applied checklist."""
        for document in documents:
            title = document["working_title"] or document["official_title"]
            if title:
                self.match_document(object_id, document["id"], title)

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join((value or "").casefold().split())


class HandoverActRepository:
    def get(self, object_id: int, act_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM handover_acts WHERE object_id = ? AND id = ?",
            (object_id, act_id),
        ).fetchone()

    def delete(self, object_id: int, act_id: int) -> bool:
        db = get_db()
        cursor = db.execute(
            "DELETE FROM handover_acts WHERE object_id = ? AND id = ?",
            (object_id, act_id),
        )
        db.commit()
        return cursor.rowcount > 0

    def list_by_object(self, object_id: int) -> list[Row]:
        return list(
            get_db().execute(
                "SELECT * FROM handover_acts WHERE object_id = ? ORDER BY period_end DESC, id DESC",
                (object_id,),
            )
        )

    def last_sent(self, object_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM handover_acts WHERE object_id = ? AND sent_at IS NOT NULL ORDER BY period_end DESC, id DESC LIMIT 1",
            (object_id,),
        ).fetchone()

    def pending(self, object_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM handover_acts WHERE object_id = ? AND sent_at IS NULL AND source = 'automatic' ORDER BY id DESC LIMIT 1",
            (object_id,),
        ).fetchone()

    def save_pending(
        self, object_id: int, period_start: str, period_end: str, stored_filename: str
    ) -> Row:
        db = get_db()
        current = self.pending(object_id)
        if current:
            db.execute(
                "UPDATE handover_acts SET period_start = ?, period_end = ?, stored_filename = ?, email_error = NULL WHERE id = ?",
                (period_start, period_end, stored_filename, current["id"]),
            )
            act_id = current["id"]
        else:
            cursor = db.execute(
                "INSERT INTO handover_acts (object_id, period_start, period_end, stored_filename) VALUES (?, ?, ?, ?)",
                (object_id, period_start, period_end, stored_filename),
            )
            act_id = cursor.lastrowid
        db.commit()
        return db.execute("SELECT * FROM handover_acts WHERE id = ?", (act_id,)).fetchone()

    def create_manual(
        self, object_id: int, period_start: str, period_end: str, stored_filename: str
    ) -> int:
        db = get_db()
        cursor = db.execute(
            "INSERT INTO handover_acts (object_id, period_start, period_end, stored_filename, source) VALUES (?, ?, ?, ?, 'manual')",
            (object_id, period_start, period_end, stored_filename),
        )
        db.commit()
        return int(cursor.lastrowid)

    def mark_sent(self, act_id: int, sent_at: str) -> None:
        db = get_db()
        db.execute(
            "UPDATE handover_acts SET sent_at = ?, email_error = NULL WHERE id = ?",
            (sent_at, act_id),
        )
        db.commit()

    def mark_error(self, act_id: int, message: str) -> None:
        db = get_db()
        db.execute(
            "UPDATE handover_acts SET email_error = ? WHERE id = ?",
            (message[:1000], act_id),
        )
        db.commit()

    def filenames_for_object(self, object_id: int) -> list[str]:
        return [
            row["stored_filename"]
            for row in get_db().execute(
                "SELECT stored_filename FROM handover_acts WHERE object_id = ?",
                (object_id,),
            )
        ]


class AppSettingsRepository:
    def get(self, key: str) -> str | None:
        row = get_db().execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


class CorrespondenceRepository:
    def list_organizations(self):
        rows = get_db().execute(
            "SELECT sender AS organization FROM correspondence UNION SELECT recipient AS organization FROM correspondence ORDER BY organization COLLATE NOCASE"
        )
        return [row["organization"].strip() for row in rows if row["organization"] and row["organization"].strip()]

    def list_by_object(self, object_id: int, record_type: str = "correspondence"):
        return list(get_db().execute("SELECT * FROM correspondence WHERE object_id = ? AND record_type = ? ORDER BY COALESCE(letter_date, '' ) DESC, id DESC", (object_id, record_type)))

    def get(self, letter_id: int):
        return get_db().execute("SELECT * FROM correspondence WHERE id = ?", (letter_id,)).fetchone()

    def create(self, **values) -> int:
        db = get_db()
        fields = ('object_id','direction','number','letter_date','sender','recipient','subject','summary','original_filename','stored_filename','mime_type','raw_text','status','error','record_type','valid_until')
        cur = db.execute(f"INSERT INTO correspondence ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(values.get(k, '') for k in fields))
        db.commit()
        return int(cur.lastrowid)

    def update(self, letter_id: int, **values) -> bool:
        keys = ('direction','number','letter_date','sender','recipient','subject','summary','valid_until')
        cur = get_db().execute('UPDATE correspondence SET ' + ', '.join(f'{k} = ?' for k in keys) + ' WHERE id = ?', tuple(values.get(k, '') for k in keys) + (letter_id,))
        get_db().commit()
        return cur.rowcount > 0

    def delete(self, letter_id: int) -> bool:
        db = get_db()
        cur = db.execute("DELETE FROM correspondence WHERE id = ?", (letter_id,))
        db.commit()
        return cur.rowcount > 0
