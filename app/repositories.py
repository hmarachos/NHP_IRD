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
        status: str = "processed",
        error: str = "",
    ) -> int:
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO documents (
                contract_number, object_name, original_filename, stored_filename,
                working_title, official_title, issuer, issue_date, valid_until,
                validity_text, no_expiration, raw_text, status, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def get(self, document_id: int) -> Row | None:
        return get_db().execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
