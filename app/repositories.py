from sqlite3 import Row

from app.database import get_db
from app.models import ParsedDocument


class DocumentRepository:
    def create(
        self,
        *,
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
                contract_number, object_name, original_filename, stored_filename,
                working_title, official_title, issuer, issue_date, valid_until,
                validity_text, no_expiration, raw_text, note, status, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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

    def get(self, document_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()

    def delete(self, document_id: int) -> bool:
        db = get_db()
        cursor = db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        db.commit()
        return cursor.rowcount > 0

    def update_note(self, document_id: int, note: str) -> bool:
        db = get_db()
        cursor = db.execute(
            "UPDATE documents SET note = ? WHERE id = ?",
            (note, document_id),
        )
        db.commit()
        return cursor.rowcount > 0
