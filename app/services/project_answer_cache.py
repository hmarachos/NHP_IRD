"""Persistent cache for project QA answers with data-change invalidation."""
import hashlib
import re
import unicodedata
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from migration_portal.models import ProjectAnswerCache, ProjectSearchRevision


CACHE_PROMPT_VERSION = 1
CACHE_TTL = timedelta(days=30)
_SOURCE_CACHE_FIELDS = ("citation", "title", "kind", "page", "text", "metadata", "url")


def normalize_question(question):
    value = unicodedata.normalize("NFKC", question or "").casefold().replace("ё", "е")
    return " ".join(re.findall(r"[0-9a-zа-я]+", value))


def _question_hash(normalized_question):
    value = "\0".join((
        str(CACHE_PROMPT_VERSION),
        getattr(settings, "OPENAI_MODEL", ""),
        normalized_question,
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def project_search_revision(project_id):
    revision, _ = ProjectSearchRevision.objects.get_or_create(
        project_id=project_id,
        defaults={"revision": uuid.uuid4()},
    )
    return revision.revision


def invalidate_project_answer_cache(project_id):
    """Rotate the project data revision and purge its cached answers."""
    if not project_id:
        return
    with transaction.atomic():
        revision, created = ProjectSearchRevision.objects.get_or_create(
            project_id=project_id,
            defaults={"revision": uuid.uuid4()},
        )
        if not created:
            ProjectSearchRevision.objects.filter(project_id=project_id).update(
                revision=uuid.uuid4(), updated_at=timezone.now(),
            )
        ProjectAnswerCache.objects.filter(project_id=project_id).delete()


def get_cached_project_answer(project_id, question):
    normalized = normalize_question(question)
    if not normalized:
        return None
    ProjectAnswerCache.objects.filter(
        project_id=project_id, expires_at__lte=timezone.now(),
    ).delete()
    revision = project_search_revision(project_id)
    entry = ProjectAnswerCache.objects.filter(
        project_id=project_id,
        question_hash=_question_hash(normalized),
        normalized_question=normalized,
        source_revision=revision,
        model_name=getattr(settings, "OPENAI_MODEL", ""),
        prompt_version=CACHE_PROMPT_VERSION,
        expires_at__gt=timezone.now(),
    ).first()
    if entry is None:
        return None
    # A source may have changed while the cache row was being read.
    if project_search_revision(project_id) != revision:
        return None
    return {"answer": entry.answer, "sources": entry.sources}


def store_project_answer(project_id, question, answer, sources, source_revision):
    """Store a successful answer only if source data stayed at the same revision."""
    normalized = normalize_question(question)
    if not normalized or not answer or project_search_revision(project_id) != source_revision:
        return False
    safe_sources = [
        {key: source[key] for key in _SOURCE_CACHE_FIELDS if key in source}
        for source in sources
    ]
    ProjectAnswerCache.objects.update_or_create(
        project_id=project_id,
        question_hash=_question_hash(normalized),
        defaults={
            "normalized_question": normalized,
            "source_revision": source_revision,
            "model_name": getattr(settings, "OPENAI_MODEL", ""),
            "prompt_version": CACHE_PROMPT_VERSION,
            "answer": answer,
            "sources": safe_sources,
            "expires_at": timezone.now() + CACHE_TTL,
        },
    )
    ProjectAnswerCache.objects.filter(
        project_id=project_id, expires_at__lte=timezone.now(),
    ).delete()
    stale_ids = list(
        ProjectAnswerCache.objects.filter(project_id=project_id)
        .order_by("-created_at", "-id").values_list("id", flat=True)[100:]
    )
    if stale_ids:
        ProjectAnswerCache.objects.filter(id__in=stale_ids).delete()
    return True
