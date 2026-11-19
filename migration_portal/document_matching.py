"""Deterministic duplicate detection used after OCR extraction."""

import re
from difflib import SequenceMatcher

from migration_portal.models import Document


def find_duplicate(project_id, parsed, excluding_id=None):
    def normalize(value):
        return " ".join(re.sub(r"[^а-яa-z0-9]+", " ", (value or "").casefold().replace("ё", "е")).split())

    def number(text):
        match = re.search(
            r"(?:исх(?:одящий)?\.?\s*(?:№|n[оo]?|#)|номер\s+документа\s*[:№]?|(?<![\w])№\s*)\s*([а-яa-z0-9][а-яa-z0-9./-]{1,35})",
            text or "", re.IGNORECASE,
        )
        return normalize(match.group(1)) if match else ""

    def issuer(value):
        quoted = re.findall(r"[«\"]([^»\"]+)[»\"]", value or "")
        return normalize(max(quoted, key=len)) if quoted else normalize(value)

    title = normalize(parsed.working_title or parsed.official_title)
    issuer_value = issuer(parsed.issuer)
    issue_date = normalize(parsed.issue_date)
    doc_number = number(parsed.raw_text)
    document_number = normalize(parsed.document_number)
    if not title or not issue_date:
        return None
    # The Flask application also treated an archived copy as a duplicate;
    # restoring it later should not create a second record for the same issue.
    query = Document.objects.filter(project_id=project_id)
    if excluding_id:
        query = query.exclude(pk=excluding_id)
    for row in query.order_by("id"):
        candidate_title = normalize(row.working_title or row.official_title)
        if not candidate_title or normalize(row.issue_date) != issue_date:
            continue
        if SequenceMatcher(None, title, candidate_title).ratio() < 0.86:
            continue
        candidate_number = number(row.raw_text)
        if doc_number and candidate_number and doc_number != candidate_number:
            continue
        same_number = bool(doc_number and candidate_number and doc_number == candidate_number)
        stored_number = normalize(row.document_number)
        if document_number and stored_number and document_number != stored_number:
            continue
        same_number = same_number or bool(document_number and stored_number and document_number == stored_number)
        candidate_issuer = issuer(row.issuer)
        issuer_match = bool(issuer_value and candidate_issuer) and SequenceMatcher(None, issuer_value, candidate_issuer).ratio() >= 0.76
        if same_number or issuer_match:
            return row
    return None
