from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.db import transaction, close_old_connections
from django.db.models import Q

from app.document_types import DOCUMENT_TYPES
from app.services.checklist_document_fields import project_assignment_fields, extract_project_assignment_text
from app.services.document_titles import apply_filename_title_fallback
from app.services.pipeline import DocumentProcessingPipeline
from migration_portal.checklist_utils import allows_multiple_documents, apply_checklist_document_title, normalize
from migration_portal.document_matching import find_duplicate
from migration_portal.models import ChecklistDocument, Document, ProjectChecklistItem

logger = logging.getLogger(__name__)


def run_pending_document_jobs(limit: int = 2) -> int:
    """Claim and process queued document records directly through Django ORM."""
    completed = 0
    for _ in range(limit):
        now = datetime.now(timezone.utc)
        token = uuid.uuid4().hex
        with transaction.atomic():
            document = Document.objects.select_for_update(skip_locked=True).filter(
                Q(status="queued") | Q(status="processing", processing_lease_until__lt=now) | Q(status="processing", processing_lease_until__isnull=True)
            ).order_by("id").first()
            if document is None:
                break
            document.status = "processing"
            document.error = None
            document.processing_token = token
            document.processing_lease_until = now + timedelta(minutes=10)
            document.save(update_fields=["status", "error", "processing_token", "processing_lease_until"])

        stop = threading.Event()
        def heartbeat(document_id=document.id, ownership=token, stopped=stop):
            try:
                while not stopped.wait(30):
                    close_old_connections()
                    if not Document.objects.filter(pk=document_id, status="processing", processing_token=ownership).update(
                        processing_lease_until=datetime.now(timezone.utc) + timedelta(minutes=10)
                    ):
                        break
            except Exception:
                logger.exception("Document heartbeat failed for id=%s", document_id)
            finally:
                close_old_connections()
        keeper = threading.Thread(target=heartbeat, daemon=True)
        keeper.start()
        try:
            path = Path(settings.MEDIA_ROOT) / document.stored_filename
            if not path.is_file():
                raise FileNotFoundError("Загруженный файл не найден в хранилище.")
            link = ProjectChecklistItem.objects.filter(
                Q(document_id=document.id) | Q(checklistdocument__document_id=document.id)
            ).select_related("project").first()
            parsed = project_assignment_fields(link.title, link.project.customer or "") if link else None
            if parsed is not None:
                parsed.raw_text = extract_project_assignment_text(
                    path, tesseract_lang=settings.TESSERACT_LANG, dpi=settings.OCR_DPI,
                )
            else:
                pipeline = DocumentProcessingPipeline(
                    document_types=DOCUMENT_TYPES,
                    tesseract_lang=settings.TESSERACT_LANG,
                    ocr_dpi=settings.OCR_DPI,
                )
                parsed = pipeline.process(path)
                if link:
                    apply_checklist_document_title(parsed, link.title)
                else:
                    apply_filename_title_fallback(parsed, document.original_filename)

            with transaction.atomic():
                owned = Document.objects.select_for_update().filter(pk=document.id, status="processing", processing_token=token).first()
                if owned is None:
                    continue
                duplicate = find_duplicate(document.project_id, parsed, excluding_id=document.id) if document.project_id else None
                document.working_title = parsed.working_title
                document.official_title = parsed.official_title
                document.issuer = parsed.issuer
                document.document_number = parsed.document_number
                document.issue_date = parsed.issue_date
                document.valid_until = parsed.valid_until
                document.validity_text = parsed.validity_text
                document.no_expiration = parsed.no_expiration
                document.raw_text = parsed.raw_text
                document.status = "error" if duplicate else "processed"
                document.error = (
                    f"Похожий документ уже есть в проекте (документ №{duplicate.id}). Проверьте, не загружен ли он ранее."
                    if duplicate else ""
                )
                document.processing_token = ""
                document.processing_lease_until = None
                document.save(update_fields=[
                    "working_title", "official_title", "issuer", "document_number", "issue_date", "valid_until",
                    "validity_text", "no_expiration", "raw_text", "status", "error", "processing_token", "processing_lease_until",
                ])
                if document.project_id and not duplicate:
                    title = normalize(document.working_title or document.official_title)
                    for item in ProjectChecklistItem.objects.filter(project_id=document.project_id, status="missing", validity_years=0).order_by("id"):
                        if normalize(item.title) != title:
                            continue
                        item.document = document
                        item.status = "received"
                        item.save(update_fields=["document", "status", "updated_at"])
                        ChecklistDocument.objects.get_or_create(checklist_item=item, document=document, defaults={"sort_order": 0})
                        if not allows_multiple_documents(item.title):
                            break
            completed += 1
        except Exception as exc:
            logger.exception("Document upload processing failed for id=%s", document.id)
            Document.objects.filter(pk=document.id, status="processing", processing_token=token).update(status="error", error=str(exc)[:2000], processing_token="", processing_lease_until=None)
            completed += 1
        finally:
            stop.set()
            keeper.join(timeout=5)
    return completed
