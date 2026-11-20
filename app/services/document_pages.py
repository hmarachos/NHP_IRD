"""Restore page boundaries only when the original file proves the mapping."""
import re
import unicodedata
from pathlib import Path

from app.services.document_text import _PAGE_RE


def _canonical(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").replace("\u00ad", ""))


def restore_page_markers(raw_text, source_path):
    """Leave OCR untouched unless page attribution is unambiguous; never run OCR."""
    raw_text = raw_text or ""
    marker = _PAGE_RE.search(raw_text)
    if marker:
        preamble = raw_text[:marker.start()]
        if marker.group(1) == "1" and preamble.startswith("--- letterhead ---\n") and "--- document body ---" in preamble:
            # This preamble is the existing extractor's OCR of the first-page
            # letterhead, not a separate unnumbered page.
            raw_text = f"--- page 1 ---\n{preamble}{raw_text[marker.end():]}"
        if int(marker.group(1)) > 1 and preamble.strip():
            # A later append leaves the old OCR byte-for-byte as a prefix. Map
            # that prefix to complete initial PDF pages before the new markers.
            mapped, mode = restore_page_markers(preamble, source_path)
            if mode != "unavailable":
                return mapped.rstrip() + "\n\n" + raw_text[marker.start():], mode
        return raw_text, "original"
    path = Path(source_path)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        return raw_text, "unavailable"
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        if len(reader.pages) == 1:
            return f"--- page 1 ---\n{raw_text}", "single_page"
        header, separator, body = raw_text.partition("--- document body ---\n")
        if not separator:
            header, body = "", raw_text
        target = _canonical(body)
        if not target:
            return raw_text, "unavailable"
        pages = []
        combined = ""
        for page in reader.pages:
            text = page.extract_text() or ""
            pages.append(text)
            combined += _canonical(text)
            # Letters may include PDF appendices which were never part of OCR.
            # Accept only an exact match ending at a complete PDF page.
            if combined == target:
                mapped = "\n\n".join(
                    f"--- page {index} ---\n{header if index == 1 else ''}{text}"
                    for index, text in enumerate(pages, start=1)
                )
                return mapped, "embedded"
            if not target.startswith(combined):
                break
    except Exception:
        # A missing, scanned or unreadable text layer must not corrupt existing OCR.
        pass
    return raw_text, "unavailable"
