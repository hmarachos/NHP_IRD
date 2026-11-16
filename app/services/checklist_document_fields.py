"""Deterministic fields for checklist document types handled without AI."""

from __future__ import annotations

import re

from app.models import ParsedDocument


PROJECT_ASSIGNMENT_TITLE = "Задание на проектирование"


def project_assignment_fields(item_title: str, customer: str = "") -> ParsedDocument | None:
    normalized = re.sub(r"\s+", " ", (item_title or "")).strip().casefold()
    if normalized != PROJECT_ASSIGNMENT_TITLE.casefold():
        return None
    return ParsedDocument(
        working_title=PROJECT_ASSIGNMENT_TITLE,
        issuer=(customer or "").strip()[:300],
    )
