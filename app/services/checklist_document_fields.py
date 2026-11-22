"""Deterministic fields for checklist document types handled without AI."""

from __future__ import annotations

import re
from pathlib import Path

from app.models import ParsedDocument


PROJECT_ASSIGNMENT_TITLE = "Задание на проектирование"


def project_assignment_fields(item_title: str, customer: str = "", raw_text: str = "") -> ParsedDocument | None:
    normalized = re.sub(r"\s+", " ", (item_title or "")).strip().casefold()
    if normalized != PROJECT_ASSIGNMENT_TITLE.casefold():
        return None
    return ParsedDocument(
        working_title=PROJECT_ASSIGNMENT_TITLE,
        issuer=(customer or "").strip()[:300],
        document_number="",
        valid_until="",
        no_expiration=True,
        raw_text=raw_text,
    )


def extract_project_assignment_text(path: Path, *, tesseract_lang: str, dpi: int) -> str:
    """Extract searchable assignment text locally, without invoking an LLM."""
    from app.services.pdf_text import PdfTextExtractor
    from app.services.word_documents import is_word_document, word_preview_path

    source = word_preview_path(path) if is_word_document(path) else path
    extractor = PdfTextExtractor(tesseract_lang=tesseract_lang, dpi=dpi)
    try:
        from pypdf import PdfReader

        page_texts = [page.extract_text() or "" for page in PdfReader(str(source)).pages]
        embedded = "\n".join(page_texts)
        if extractor._has_enough_text(embedded):
            return "\n\n".join(
                f"--- page {index} ---\n{text.strip()}"
                for index, text in enumerate(page_texts, start=1)
            ).strip()
    except Exception:
        pass
    # Scanned pages use the application's existing OCR path, which also retains
    # page markers for citations in the project search results.
    return extractor.extract(source)
