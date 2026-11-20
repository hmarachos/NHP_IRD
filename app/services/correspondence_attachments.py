"""Append pages to an existing letter without changing its card or original file."""
import logging
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.http import Http404

from app.services.document_text import _PAGE_RE
from app.services.pdf_text import PdfTextExtractor
from app.services.word_documents import remove_word_preview
from migration_portal.models import Correspondence


def append_correspondence_files(project_id, letter_id, files, *, ocr_warnings=None):
    # Reuse the same format validation and conversion as initial uploads.
    from migration_portal.views import _upload_storage, _merge_correspondence_files, _stored_correspondence_path
    from pypdf import PdfReader

    if not files:
        raise ValueError("Выберите документ для добавления.")
    root = Path(settings.MEDIA_ROOT)
    saved = []
    appendix = root / f"{uuid4().hex}_new-attachments.pdf"
    combined = root / f"{uuid4().hex}_letter-with-attachments.pdf"
    committed = False
    try:
        storage = _upload_storage()
        for file in files:
            saved.append(storage.save_upload(file))
        _merge_correspondence_files(saved, appendix)
        added_pages = len(PdfReader(appendix).pages)
        if not added_pages:
            raise ValueError("В добавленном документе нет страниц.")
        try:
            text = PdfTextExtractor(tesseract_lang=settings.TESSERACT_LANG, dpi=settings.OCR_DPI).extract(appendix)
            from app.services.document_pages import restore_page_markers
            text, _ = restore_page_markers(text, appendix)
            if not _PAGE_RE.sub("", text).strip():
                raise ValueError("В добавленных страницах не найден текст.")
        except Exception:
            logging.getLogger(__name__).warning("Appendix OCR unavailable for correspondence id=%s; retaining pages", letter_id, exc_info=True)
            text = "\n\n".join(f"--- page {page} ---" for page in range(1, added_pages + 1))
            if ocr_warnings is not None:
                ocr_warnings.append("Документ добавлен, но текст новых страниц не удалось распознать. Страницы доступны для просмотра.")
        with transaction.atomic():
            letter = Correspondence.objects.select_for_update().filter(
                pk=letter_id, project_id=project_id, project__archived_at__isnull=True,
            ).first()
            if letter is None:
                raise Http404
            previous = _stored_correspondence_path(letter)
            _merge_correspondence_files([
                (letter.original_filename, letter.stored_filename, previous, letter.mime_type),
                (appendix.name, appendix.name, appendix, "application/pdf"),
            ], combined)
            page_count = len(PdfReader(combined).pages)
            old_pages = page_count - added_pages
            if old_pages < 1:
                raise ValueError("Исходное письмо не содержит страниц.")
            shifted = _PAGE_RE.sub(lambda match: f"--- page {int(match.group(1)) + old_pages} ---", text)
            letter.raw_text = (letter.raw_text or "") + "\n\n" + shifted
            letter.stored_filename = combined.name
            letter.mime_type = "application/pdf"
            letter.original_filename = Path(letter.original_filename).stem + ".pdf"
            letter.save(update_fields=["stored_filename", "original_filename", "mime_type", "raw_text"])
        committed = True
        # The old source file is intentionally retained as a recovery copy.
        return added_pages, page_count
    finally:
        for _name, _stored, path, _mime in saved:
            remove_word_preview(Path(path))
            Path(path).unlink(missing_ok=True)
        appendix.unlink(missing_ok=True)
        if not committed:
            combined.unlink(missing_ok=True)
