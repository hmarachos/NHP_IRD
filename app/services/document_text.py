"""Deterministic OCR preparation; preserve page markers and paragraph boundaries."""
import re
import unicodedata

_PAGE_RE = re.compile(r"---\s*page\s+(\d+)\s*---", re.IGNORECASE)
_LIST_ITEM_START_RE = re.compile(r"\n[ \t]*(?:\d+(?:\.\d+)*[.)]?|[•●▪◦‣–—-])[ \t]+")
_PARAGRAPH_BOUNDARY_RE = re.compile(r"\n[ \t]*\n+")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?;:])[ \t\n]+")
_LINE_BOUNDARY_RE = re.compile(r"\n+")
_SPACE_BOUNDARY_RE = re.compile(r"\s+")
_OCR_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\u200c\u200d\ufeff]")
_OCR_GARBAGE_LINE_RE = re.compile(r"^[\s|/\\_=~`^*•·.,:;!?'\"()\[\]{}<>—–-]+$")

def _sanitize_ocr_text(text):
    """Conservatively clean OCR for retrieval while leaving stored originals intact."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u00a0", " ").replace("\u00ad", "")
    text = _OCR_CONTROL_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    # Remove the line break after OCR hyphens but keep the hyphen itself: it can
    # be either a wrap mark or a meaningful compound-word separator.
    text = re.sub(r"(?<=[A-Za-zА-Яа-яЁё])[-‑–]\s*\n\s*(?=[A-Za-zА-Яа-яЁё])", "-", text)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[\t ]+", " ", line).strip()
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        if _OCR_GARBAGE_LINE_RE.fullmatch(line):
            continue
        # Drop adjacent repeated OCR header/footer lines, keeping the first copy.
        if len(line) >= 18 and lines and line.casefold() == lines[-1].casefold():
            continue
        lines.append(line)
    return "\n".join(lines)


def _chunk_end(text, start, limit):
    """Prefer complete paragraphs, list items, and sentences near the size target."""
    target = min(len(text), start + limit)
    if target == len(text):
        return target
    earliest = start + max(1, int(limit * 0.62))
    for pattern, boundary_side in (
        (_PARAGRAPH_BOUNDARY_RE, "end"),
        (_LIST_ITEM_START_RE, "start"),
        (_SENTENCE_BOUNDARY_RE, "end"),
        (_LINE_BOUNDARY_RE, "end"),
        (_SPACE_BOUNDARY_RE, "end"),
    ):
        boundaries = [
            match.start() if boundary_side == "start" else match.end()
            for match in pattern.finditer(text, earliest, target + 1)
        ]
        boundaries = [boundary for boundary in boundaries if boundary > start]
        if boundaries:
            return boundaries[-1]
    # Pathological OCR can produce a single long unbroken run. Keep progress
    # while protecting a real word boundary whenever one exists nearby.
    boundary = text.rfind(" ", earliest, target)
    if boundary > start:
        return boundary + 1
    return target


def _next_chunk_start(text, end, overlap, previous_start):
    """Keep the requested overlap while moving its edge out of the middle of a word."""
    target = max(previous_start + 1, end - overlap)
    start = target
    lower_bound = max(previous_start + 1, target - 80)
    while start > lower_bound and start < len(text) and not text[start - 1].isspace() and not text[start].isspace():
        start -= 1
    if start > lower_bound or (start < len(text) and text[start].isspace()):
        return start
    # Avoid pathological one-character progress through OCR tables containing
    # an unusually long unbroken run.
    return target


def _merge_overlapping_chunks(parts, max_overlap=400):
    """Reassemble same-page chunks without assuming every overlap is identical."""
    if not parts:
        return ""
    merged = parts[0]
    for part in parts[1:]:
        overlap_limit = min(len(merged), len(part), max_overlap)
        overlap = next(
            (size for size in range(overlap_limit, 0, -1) if merged.endswith(part[:size])),
            0,
        )
        merged += part[overlap:]
    return merged


def _text_chunks(text, limit=1400, overlap=180):
    text = (text or "").strip()
    if not text:
        return []
    pages = []
    markers = list(_PAGE_RE.finditer(text))
    if markers:
        if markers[0].start():
            pages.append((None, text[:markers[0].start()]))
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            pages.append((int(marker.group(1)), text[marker.end():end]))
    else:
        pages = [(None, text)]

    chunks = []
    for page, page_text in pages:
        start = 0
        while start < len(page_text):
            end = _chunk_end(page_text, start, limit)
            part = page_text[start:end].strip()
            if part:
                chunks.append((page, part))
            if end >= len(page_text):
                break
            start = _next_chunk_start(page_text, end, overlap, start)
    return chunks


