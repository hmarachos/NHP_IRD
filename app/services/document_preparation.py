"""Durable, versioned preparation between OCR persistence and project retrieval."""
import hashlib
import json
import logging
import uuid

from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from app.services.document_pages import restore_page_markers
from app.services.document_text import _sanitize_ocr_text, _text_chunks
from app.services.project_answer_cache import invalidate_project_answer_cache
from migration_portal.models import Correspondence, Document, DocumentChunk, PreparedDocument

logger = logging.getLogger(__name__)
PIPELINE_VERSION = 2
DOCUMENT_FIELDS = ("working_title", "official_title", "issuer", "document_number", "issue_date", "valid_until", "no_expiration", "note")
LETTER_FIELDS = ("direction", "number", "letter_date", "sender", "recipient", "subject", "summary", "valid_until", "record_type")


def _snapshot(source):
    fields = DOCUMENT_FIELDS if isinstance(source, Document) else LETTER_FIELDS
    metadata = {name: getattr(source, name) if getattr(source, name) is not None else "" for name in fields}
    metadata["original_filename"] = source.original_filename
    return metadata


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _fingerprint(source):
    return _digest([PIPELINE_VERSION, source.raw_text or "", _snapshot(source), source.status, source.project_id])


def queue_document_preparation(source):
    """Persist a job inside the source save transaction; never invoke OCR here."""
    field = "document" if isinstance(source, Document) else "correspondence"
    with transaction.atomic():
        # Read persisted fields, including saves with update_fields and concurrent edits.
        source = type(source).objects.select_for_update().get(pk=source.pk)
        prepared, _ = PreparedDocument.objects.get_or_create(**{field: source})
        if isinstance(source, Document) and source.archived_at:
            prepared.chunks.all().delete()
            PreparedDocument.objects.filter(pk=prepared.pk).update(
                status="empty", error="", requested_revision=uuid.uuid4(), updated_at=timezone.now(),
            )
            return
        fingerprint = _fingerprint(source)
        if prepared.input_hash == fingerprint and prepared.status in {"ready", "empty"}:
            return
        PreparedDocument.objects.filter(pk=prepared.pk).update(status="pending", error="", requested_revision=uuid.uuid4(), updated_at=timezone.now())


def prepare_document(prepared_id, requested_revision=None):
    """Publish one complete version atomically, serialized with source edits."""
    ref = PreparedDocument.objects.filter(pk=prepared_id).values("document_id", "correspondence_id").first()
    if ref is None:
        return False
    model, source_id = (Document, ref["document_id"]) if ref["document_id"] else (Correspondence, ref["correspondence_id"])
    with transaction.atomic():
        source = model.objects.select_for_update().filter(pk=source_id).first()
        if source is None:
            return False
        prepared = PreparedDocument.objects.select_for_update().get(pk=prepared_id)
        if prepared.status != "pending" or (requested_revision is not None and prepared.requested_revision != requested_revision):
            return False
        if isinstance(source, Document) and source.archived_at:
            prepared.chunks.all().delete()
            prepared.status = "empty"
            prepared.error = ""
            prepared.save(update_fields=["status", "error", "updated_at"])
            invalidate_project_answer_cache(source.project_id)
            return True
        # The worker owns the source lock through activation. Edits will queue a
        # newer job after this commit, so stale work cannot overwrite a newer card.
        if source.status in {"queued", "processing"}:
            return False
        metadata = _snapshot(source)
        text_hash = _digest([PIPELINE_VERSION, source.raw_text or ""])
        rebuild = prepared.text_hash != text_hash or prepared.pipeline_version != PIPELINE_VERSION
        if rebuild:
            page_text, prepared.page_mapping = restore_page_markers(
                source.raw_text, Path(settings.MEDIA_ROOT) / source.stored_filename,
            )
            normalized = _sanitize_ocr_text(page_text).strip()
            chunks = _text_chunks(normalized)
            prepared.revision += 1
            prepared.chunks.all().delete()
            DocumentChunk.objects.bulk_create([
                DocumentChunk(prepared=prepared, revision=prepared.revision, position=index, page=page, text=text)
                for index, (page, text) in enumerate(chunks)
            ])
            prepared.normalized_text = normalized
        prepared.metadata = metadata
        prepared.text_hash = text_hash
        prepared.input_hash = _fingerprint(source)
        prepared.pipeline_version = PIPELINE_VERSION
        prepared.status = "ready" if prepared.normalized_text and source.status == "processed" else "empty"
        prepared.error = ""
        prepared.save()
        invalidate_project_answer_cache(source.project_id)
    return True


def run_pending_preparations(limit=20):
    """Retryable DB queue; a failed publication rolls back its text and chunks."""
    ids = list(PreparedDocument.objects.filter(status="pending", document__archived_at__isnull=True).exclude(
        document__status__in=["queued", "processing"],
    ).exclude(correspondence__status__in=["queued", "processing"]).order_by("id").values_list("id", "requested_revision")[:limit])
    completed = 0
    for prepared_id, requested_revision in ids:
        try:
            completed += bool(prepare_document(prepared_id, requested_revision))
        except Exception as exc:
            logger.exception("Document preparation failed id=%s", prepared_id)
            # Do not mask a newer job queued while the failed transaction rolled back.
            with transaction.atomic():
                ref = PreparedDocument.objects.filter(pk=prepared_id).first()
                if ref is None:
                    continue
                model = Document if ref.document_id else Correspondence
                source = model.objects.select_for_update().filter(pk=ref.document_id or ref.correspondence_id).first()
                if source is not None:
                    PreparedDocument.objects.filter(pk=prepared_id, status="pending", requested_revision=requested_revision).update(status="error", error=str(exc)[:2000], updated_at=timezone.now())
    return completed
