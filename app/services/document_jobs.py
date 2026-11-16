from __future__ import annotations

import logging
from pathlib import Path

from flask import current_app

from app.database import get_db
from app.models import ParsedDocument
from app.repositories import ChecklistRepository, DocumentRepository
from app.services.pipeline import DocumentProcessingPipeline

logger = logging.getLogger(__name__)


def run_pending_document_jobs(limit: int = 2) -> int:
    """Process accepted document uploads outside the web request."""
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM documents
        WHERE status = 'queued'
           OR (status = 'processing' AND created_at < datetime('now', '-10 minutes'))
        ORDER BY id LIMIT ?
        """,
        (limit,),
    ).fetchall()
    completed = 0
    for row in rows:
        claimed = db.execute(
            "UPDATE documents SET status = 'processing', error = NULL WHERE id = ? AND status IN ('queued', 'processing')",
            (row["id"],),
        ).rowcount
        db.commit()
        if not claimed:
            continue
        try:
            path = Path(current_app.config["UPLOAD_DIR"]) / row["stored_filename"]
            if not path.is_file():
                raise FileNotFoundError("Загруженный файл не найден в хранилище.")
            pipeline = DocumentProcessingPipeline(
                document_types=current_app.config["DOCUMENT_TYPES"],
                tesseract_lang=current_app.config["TESSERACT_LANG"],
                ocr_dpi=current_app.config["OCR_DPI"],
            )
            parsed = pipeline.process(path)

            # Keep the duplicate guard used by synchronous uploads. A duplicate
            # remains attached and reviewable, with a clear explanation.
            from app.web.routes import _find_duplicate_document

            duplicate = _find_duplicate_document(DocumentRepository(), row["object_id"], parsed) if row["object_id"] else None
            error = ""
            status = "processed"
            if duplicate and duplicate["id"] != row["id"]:
                status = "error"
                error = f"Похожий документ уже есть в проекте (документ №{duplicate['id']}). Проверьте, не загружен ли он ранее."

            db.execute(
                """UPDATE documents SET working_title=?, official_title=?, issuer=?, issue_date=?, valid_until=?,
                   validity_text=?, no_expiration=?, raw_text=?, status=?, error=? WHERE id=?""",
                (parsed.working_title, parsed.official_title, parsed.issuer, parsed.issue_date, parsed.valid_until,
                 parsed.validity_text, 1 if parsed.no_expiration else 0, parsed.raw_text, status, error, row["id"]),
            )
            db.commit()
            if row["object_id"] and status == "processed":
                document = DocumentRepository().get(row["id"])
                ChecklistRepository().sync_documents_to_checklist(row["object_id"], [document])
            completed += 1
        except Exception as exc:
            logger.exception("Document upload processing failed for id=%s", row["id"])
            db.execute("UPDATE documents SET status='error', error=? WHERE id=?", (str(exc)[:2000], row["id"]))
            db.commit()
            completed += 1
    return completed
