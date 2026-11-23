"""Suggest project checklist rows for a newly recognized document."""

import re
from difflib import SequenceMatcher

from migration_portal.checklist_utils import allows_multiple_documents
from migration_portal.models import ChecklistDocument, ProjectChecklistItem


def _normalize(value):
    return " ".join(re.sub(r"[^а-яa-z0-9]+", " ", (value or "").casefold().replace("ё", "е")).split())


def suggest_checklist_items(project_id, parsed):
    """Return ranked project rows and a conservative, user-confirmed suggestion."""
    recognized = [
        _normalize(parsed.working_title),
        _normalize(parsed.official_title),
    ]
    searchable_text = _normalize(" ".join((parsed.working_title or "", parsed.official_title or "", (parsed.raw_text or "")[:12000])))
    ranked = []
    for item in ProjectChecklistItem.objects.filter(project_id=project_id).order_by("sort_order", "id"):
        if item.validity_years > 0 and item.title != "Инженерно-геодезические изыскания":
            continue
        title = _normalize(item.title)
        description = _normalize(item.description)
        if not title:
            continue
        name_score = max((SequenceMatcher(None, title, candidate).ratio() for candidate in recognized if candidate), default=0)
        if title in searchable_text:
            text_score = 1.0
        else:
            tokens = [token for token in title.split() if len(token) > 2]
            text_score = sum(token in searchable_text for token in tokens) / len(tokens) if tokens else 0
        description_score = 0.0
        if description and description in searchable_text:
            description_score = 0.85
        score = max(name_score, text_score * 0.78, description_score)
        linked_documents = ChecklistDocument.objects.filter(checklist_item=item).select_related("document")
        current_document = item.document
        current_title = ""
        if current_document and not current_document.archived_at:
            current_title = current_document.working_title or current_document.original_filename
        linked_count = linked_documents.filter(document__archived_at__isnull=True).count()
        ranked.append({
            "id": item.id,
            "title": item.title,
            "score": round(score, 3),
            "status": item.status,
            "has_document": bool(current_title or linked_count),
            "current_document_title": current_title,
            "allows_multiple": allows_multiple_documents(item.title),
        })

    ranked.sort(key=lambda row: (-row["score"], row["title"].casefold(), row["id"]))
    best = ranked[0] if ranked else None
    margin = best["score"] - (ranked[1]["score"] if len(ranked) > 1 else 0)
    suggested_id = best["id"] if best and best["score"] >= 0.70 and margin >= 0.08 else None
    return ranked, suggested_id
