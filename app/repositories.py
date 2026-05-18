from sqlite3 import Row

from app.database import get_db
from app.models import ParsedDocument


class ObjectRepository:
    def create(self, *, contract_number: str, name: str) -> int:
        db = get_db()
        cursor = db.execute(
            "INSERT INTO objects (contract_number, name) VALUES (?, ?)",
            (contract_number, name),
        )
        db.commit()
        return int(cursor.lastrowid)

    def list_all(self) -> list[Row]:
        return list(
            get_db().execute(
                """
                SELECT
                    objects.*,
                    COUNT(documents.id) AS documents_count,
                    MAX(documents.created_at) AS last_document_at
                FROM objects
                LEFT JOIN documents ON documents.object_id = objects.id
                GROUP BY objects.id
                ORDER BY objects.created_at DESC, objects.id DESC
                """
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

    def delete(self, object_id: int) -> bool:
        db = get_db()
        cursor = db.execute("DELETE FROM objects WHERE id = ?", (object_id,))
        db.commit()
        return cursor.rowcount > 0


class DocumentRepository:
    def create(
        self,
        *,
        object_id: int,
        contract_number: str,
        object_name: str,
        original_filename: str,
        stored_filename: str,
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
                working_title, official_title, issuer, issue_date, valid_until,
                validity_text, no_expiration, raw_text, note, status, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                object_id,
                contract_number,
                object_name,
                original_filename,
                stored_filename,
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
        return int(cursor.lastrowid)

    def list_recent(self) -> list[Row]:
        return list(
            get_db().execute(
                "SELECT * FROM documents ORDER BY created_at DESC, id DESC LIMIT 50"
            )
        )

    def list_grouped(self) -> list[Row]:
        return list(
            get_db().execute(
                """
                SELECT *
                FROM documents
                ORDER BY contract_number COLLATE NOCASE ASC, created_at DESC, id DESC
                """
            )
        )

    def list_by_object(self, object_id: int) -> list[Row]:
        return list(
            get_db().execute(
                """
                SELECT *
                FROM documents
                WHERE object_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (object_id,),
            )
        )

    def get(self, document_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()

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
        cursor = db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        db.commit()
        return cursor.rowcount > 0

    def delete_by_object(self, object_id: int) -> int:
        db = get_db()
        cursor = db.execute("DELETE FROM documents WHERE object_id = ?", (object_id,))
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
