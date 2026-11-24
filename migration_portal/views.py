import os
import logging
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import zipfile
from html.parser import HTMLParser
from io import BytesIO
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_not_required, login_required
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import connection, transaction
from django.db.utils import IntegrityError
from django.db.models import Count, Max, Q
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.http import FileResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.utils.http import content_disposition_header
from django.utils import timezone as django_timezone

from app.services.document_titles import apply_filename_title_fallback, attach_document_title, clean_document_name, format_document_title
from app.services.expiry import add_years, attach_expiry, expiry_state, iso_date
from migration_portal.models import (
    ChecklistTemplate,
    ChecklistTemplateItem,
    ChecklistDocument,
    Correspondence,
    Document,
    DocumentVersionGroup,
    Project,
    ProjectShare,
    ProjectChecklistItem,
    ProjectNotebookEntry,
)
from app.services.storage import StorageService
from migration_portal.checklist_utils import (
    allows_multiple_documents, apply_checklist_document_title, normalize,
    uses_customer_letter_subject,
)
from migration_portal.checklist_matching import suggest_checklist_items
from migration_portal.accounts import visible_customers, visible_projects

logger = logging.getLogger(__name__)
_ANALYTICS_JOBS = {}
_ANALYTICS_JOBS_LOCK = threading.Lock()
_ANALYTICS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="project-analytics")


def _analytics_public_sources(sources):
    """Drop retrieval-only sets and scoring fields before returning sources as JSON."""
    public_keys = {"citation", "title", "kind", "page", "text", "metadata", "url"}
    return [{key: source[key] for key in public_keys if key in source} for source in sources]


def _run_project_analytics_job(job_id, user_id, project_id, question, cancel_event):
    from app.services.project_qa import (
        ProjectQuestionCancelled, answer_project_question, retrieve_project_sources,
    )
    from app.services.project_answer_cache import (
        get_cached_project_answer, project_search_revision, store_project_answer,
    )

    answer = ""
    error = ""
    cancelled = False
    cache_hit = False
    public_sources = []
    try:
        cached = get_cached_project_answer(project_id, question)
        if cached:
            answer = cached["answer"]
            public_sources = cached["sources"]
            cache_hit = True
        else:
            source_revision = project_search_revision(project_id)
            sources = retrieve_project_sources(project_id, question)
            public_sources = _analytics_public_sources(sources)
        if cancel_event.is_set():
            cancelled = True
        elif cache_hit:
            pass
        elif not sources:
            error = "В обработанных документах проекта не нашлось подходящих фрагментов. Проверьте формулировку вопроса или дождитесь распознавания новых файлов."
        elif not settings.OPENAI_API_KEY or not settings.USE_AI:
            error = "Поиск нашёл фрагменты, но ответы ИИ сейчас отключены."
        else:
            answer = answer_project_question(
                question, sources, cancel_check=cancel_event.is_set,
                on_delta=lambda delta: _append_analytics_delta(job_id, user_id, project_id, delta),
            )
            store_project_answer(project_id, question, answer, sources, source_revision)
    except ProjectQuestionCancelled:
        cancelled = True
    except Exception:
        logger.exception("Project analytics answer failed project_id=%s", project_id)
        error = "Не удалось подготовить ответ. Попробуйте ещё раз; найденные источники сохранены ниже."
        public_sources = locals().get("public_sources", [])
    with _ANALYTICS_JOBS_LOCK:
        job = _ANALYTICS_JOBS.get(job_id)
        if job and (job["user_id"], job["project_id"]) == (user_id, project_id):
            job.update({
                "status": "cancelled" if cancelled else "complete",
                "answer": answer,
                "stream_answer": answer,
                "stream_seq": job.get("stream_seq", 0),
                "error": error,
                "sources": public_sources,
                "cached": cache_hit,
                "expires_at": time.monotonic() + 300,
            })


def _append_analytics_delta(job_id, user_id, project_id, delta):
    with _ANALYTICS_JOBS_LOCK:
        job = _ANALYTICS_JOBS.get(job_id)
        if job and (job["user_id"], job["project_id"]) == (user_id, project_id) and job["status"] == "processing":
            job["stream_answer"] += delta
            job["stream_seq"] += 1


def _upload_storage():
    return StorageService(Path(settings.MEDIA_ROOT))


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _find_exact_project_file(project_id, path):
    candidate = Path(path)
    candidate_size = candidate.stat().st_size
    candidate_digest = _file_sha256(candidate)
    for document in Document.objects.filter(project_id=project_id).order_by("id"):
        try:
            stored_path = _stored_document_path(document)
        except Http404:
            continue
        if stored_path.stat().st_size == candidate_size and _file_sha256(stored_path) == candidate_digest:
            return document
    return None


def _exact_duplicate_message(filename, existing):
    title = existing.working_title or existing.official_title or existing.original_filename or "документ"
    location = "в архиве проекта" if existing.archived_at else "в проекте"
    return f"Файл «{filename}» уже есть {location} как «{title}». Повторная загрузка не выполнена."


def _upload_size_exceeded(request):
    return bool(getattr(request, "upload_file_too_large", False))


class _NotebookHTMLSanitizer(HTMLParser):
    ALLOWED = {"p", "br", "strong", "b", "em", "i", "u", "s", "ul", "ol", "li", "blockquote", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in self.ALLOWED:
            self.parts.append(f"<{tag}>")
            if tag != "br":
                self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.parts.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data):
        from html import escape
        self.parts.append(escape(data))

    def finish(self):
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        return "".join(self.parts)[:200000]


def _sanitize_notebook_html(value):
    parser = _NotebookHTMLSanitizer()
    parser.feed(value or "")
    parser.close()
    return parser.finish()


@require_GET
def project_notebook(request, object_id):
    project = visible_projects(request.user).filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    entries = ProjectNotebookEntry.objects.filter(project=project)
    checklist = list(ProjectChecklistItem.objects.filter(project=project).values("status"))
    received_count = sum(item["status"] == "received" for item in checklist)
    correspondence_count = Correspondence.objects.filter(
        project=project, record_type="correspondence",
    ).count()
    return render(request, "project_notebook.html", {
        "object": project,
        "entries": entries,
        "checklist": checklist,
        "received_count": received_count,
        "correspondence_count": correspondence_count,
    }, using="jinja2")


@require_POST
def save_notebook_entry(request, object_id):
    project = visible_projects(request.user).filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    entry_id = request.POST.get("entry_id")
    if entry_id:
        entry = ProjectNotebookEntry.objects.filter(pk=entry_id, project=project).first()
        if entry is None:
            raise Http404
    else:
        entry = ProjectNotebookEntry(project=project)
    entry.title = request.POST.get("title", "").strip()[:300] or "Запись"
    entry.content = _sanitize_notebook_html(request.POST.get("content", ""))
    entry.updated_at = datetime.now(timezone.utc).isoformat()
    entry.save()
    return JsonResponse({"id": entry.id, "title": entry.title, "content": entry.content, "updated_at": entry.updated_at})


@require_POST
def delete_notebook_entry(request, object_id, entry_id):
    entry = ProjectNotebookEntry.objects.filter(
        pk=entry_id,
        project__in=visible_projects(request.user).filter(archived_at__isnull=True),
    ).first()
    if entry is None:
        raise Http404
    entry.delete()
    return HttpResponse(status=204)


@require_POST
def upload_documents(request, object_id):
    if request.headers.get("X-IRD-Analyze") == "1":
        return upload_checklist_document(request, object_id, None)
    project = Project.objects.filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    if _upload_size_exceeded(request):
        messages.error(request, "Размер каждого файла не должен превышать 15 МиБ.")
        return redirect("web_object_workspace", object_id=object_id)
    files = [f for f in request.FILES.getlist("files") if f and f.name]
    if not files and request.FILES.get("pdf_file"):
        files = [request.FILES["pdf_file"]]
    if not files:
        messages.error(request, "Выберите PDF или фотографию документа.")
        return redirect("web_object_workspace", object_id=object_id)
    storage = _upload_storage()
    saved, images = [], []
    upload_errors = []
    try:
        for file in files:
            try:
                item = storage.save_upload(file)
            except Exception as exc:
                upload_errors.append(f"{file.name}: {exc}")
                continue
            (images if item[3].startswith("image/") else saved).append(item)
        if images:
            combined = Path(settings.MEDIA_ROOT) / f"{__import__('uuid').uuid4().hex}_phone-pages.pdf"
            _combine_images(images, combined)
            saved.append((f"Фотографии документа — {len(images)} страниц.pdf", combined.name, combined, "application/pdf"))
        created = []
        for original_name, stored_name, path, mime_type in saved:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=project.id)
                duplicate = _find_exact_project_file(project.id, path)
                if duplicate:
                    document = None
                else:
                    document = Document.objects.create(
                        project=project, contract_number=project.contract_number, object_name=project.name,
                        original_filename=original_name, stored_filename=stored_name, mime_type=mime_type,
                        status="queued", note=request.POST.get("note", "").strip(),
                    )
            if duplicate:
                Path(path).unlink(missing_ok=True)
                upload_errors.append(_exact_duplicate_message(original_name, duplicate))
                continue
            created.append(document.id)
        for _name, _stored, image_path, _mime in images:
            Path(image_path).unlink(missing_ok=True)
    except Exception as exc:
        for *_prefix, saved_path, _mime in saved + images:
            Path(saved_path).unlink(missing_ok=True)
        messages.error(request, str(exc) if isinstance(exc, ValueError) else "Не удалось сохранить документ.")
        return redirect("web_object_workspace", object_id=object_id)
    for error in upload_errors:
        messages.error(request, error)
    if not created:
        if not upload_errors:
            messages.error(request, "Не удалось сохранить выбранные файлы.")
        return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab=checklist")
    if len(created) == 1:
        return redirect("web_document_detail", document_id=created[0])
    messages.success(request, f"Файлы приняты: {len(created)}. Распознавание выполняется в фоне.")
    return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab=checklist")


@require_POST
def upload_checklist_document(request, object_id, item_id):
    project = Project.objects.filter(pk=object_id, archived_at__isnull=True).first()
    item = ProjectChecklistItem.objects.filter(
        pk=item_id, project_id=object_id, project__archived_at__isnull=True,
    ).first() if item_id is not None else None
    if project is None or (item_id is not None and item is None):
        raise Http404
    if _upload_size_exceeded(request):
        message = "Размер каждого файла не должен превышать 15 МиБ."
        if request.headers.get("X-IRD-Analyze") == "1":
            return JsonResponse({"error": message}, status=413)
        messages.error(request, message)
        return redirect("web_object_workspace", object_id=object_id)
    item_title = item.title if item else ""
    if not request.FILES.getlist("files") and request.headers.get("X-IRD-Analyze") == "1":
        return JsonResponse({"error": "Выберите файл документа."}, status=422)
    files = [f for f in request.FILES.getlist("files") if f and f.name]
    if not files:
        messages.error(request, "Выберите PDF, Word-файл или фотографии страниц документа.")
        return redirect("web_object_workspace", object_id=object_id)
    storage = _upload_storage()
    saved = []
    uploaded_parts = []
    final_path = None
    try:
        saved = [storage.save_upload(file) for file in files]
        if len(saved) == 1 and (saved[0][3] == "application/pdf" or saved[0][3] in {
            "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }):
            final_name, final_stored, final_path, final_mime = saved[0]
        else:
            from app.services.word_documents import WORD_MIME_TYPES
            if any(mime in WORD_MIME_TYPES.values() for _name, _stored, _path, mime in saved):
                raise ValueError("Загружайте Word-документ отдельно, по одному файлу.")
            if any(not mime.startswith("image/") for _name, _stored, _path, mime in saved):
                raise ValueError("Для многостраничного документа выберите один PDF, один файл Word или несколько фотографий.")
            final_stored = f"{__import__('uuid').uuid4().hex}_checklist-pages.pdf"
            final_path = Path(settings.MEDIA_ROOT) / final_stored
            _combine_images(saved, final_path)
            final_name, final_mime = f"{item_title[:100] or 'Документ'}.pdf", "application/pdf"
            for _name, _stored, source_path, _mime in saved:
                Path(source_path).unlink(missing_ok=True)
            saved = []
        existing = _find_exact_project_file(project.id, final_path)
        if existing:
            final_path.unlink(missing_ok=True)
            if existing.archived_at:
                raise ValueError(_exact_duplicate_message(final_name, existing))
            from app.services.checklist_document_fields import project_assignment_fields
            manual_assignment = project_assignment_fields(item_title, project.customer or "")
            if manual_assignment is not None:
                existing.working_title = manual_assignment.working_title
                existing.official_title = ""
                existing.issuer = manual_assignment.issuer
                existing.document_number = ""
                existing.valid_until = ""
                existing.no_expiration = True
                existing.note = ""
                existing.save(update_fields=[
                    "working_title", "official_title", "issuer", "document_number",
                    "valid_until", "no_expiration", "note",
                ])
            if item:
                if request.headers.get("X-Add-Checklist-File") == "1":
                    _append_checklist_document(project.id, item, existing)
                else:
                    _link_checklist_document(project.id, item, existing)
            if request.headers.get("X-IRD-Analyze") == "1":
                matches, suggested_item_id = suggest_checklist_items(project.id, existing) if item is None else ([], None)
                return JsonResponse({"id": existing.id, "existing_document": True, "general_upload": item is None, "preview": reverse("web_document_preview", args=[existing.id]), "checklist_matches": matches, "suggested_checklist_item_id": suggested_item_id, "fields": {
                    "working_title": clean_document_name(existing.working_title or item_title, existing.issuer, project.customer), "official_title": existing.official_title or "",
                    "issuer": existing.issuer or "", "document_number": existing.document_number or "",
                    "issue_date": existing.issue_date or "", "valid_until": existing.valid_until or "",
                    "no_expiration": existing.no_expiration, "note": existing.note or "",
                }})
            messages.success(request, "Такой файл уже был загружен. Существующий документ привязан к строке комплектности.")
            return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab=checklist")
        parsed = None
        if request.headers.get("X-IRD-Analyze") == "1":
            from app.services.checklist_document_fields import project_assignment_fields, extract_project_assignment_text
            parsed = project_assignment_fields(item_title, project.customer or "")
            if parsed is not None:
                parsed.raw_text = extract_project_assignment_text(
                    final_path, tesseract_lang=settings.TESSERACT_LANG, dpi=settings.OCR_DPI,
                )
            if parsed is None:
                from app.services.pipeline import DocumentProcessingPipeline
                from app.document_types import DOCUMENT_TYPES
                pipeline = DocumentProcessingPipeline(
                    document_types=DOCUMENT_TYPES,
                    tesseract_lang=settings.TESSERACT_LANG,
                    ocr_dpi=settings.OCR_DPI,
                )
                parsed = pipeline.process(final_path)
                apply_checklist_document_title(parsed, item_title)
            if item is None:
                apply_filename_title_fallback(parsed, final_name)
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=project.id)
                duplicate = _find_exact_project_file(project.id, final_path)
                if duplicate:
                    raise ValueError(_exact_duplicate_message(final_name, duplicate))
                document = Document.objects.create(
                    project=project, contract_number=project.contract_number, object_name=project.name,
                    original_filename=final_name, stored_filename=final_stored, mime_type=final_mime,
                    working_title=parsed.working_title, official_title=parsed.official_title,
                    issuer=parsed.issuer, document_number=parsed.document_number,
                    issue_date=parsed.issue_date, valid_until=parsed.valid_until,
                    validity_text=parsed.validity_text, no_expiration=parsed.no_expiration,
                    raw_text=parsed.raw_text, status="processed",
                )
                if item:
                    if request.headers.get("X-Add-Checklist-File") == "1":
                        _append_checklist_document(project.id, item, document)
                    else:
                        _link_checklist_document(project.id, item, document)
            fields = _checklist_analysis_fields(parsed, item_title)
            matches, suggested_item_id = suggest_checklist_items(project.id, parsed) if item is None else ([], None)
            return JsonResponse({"id": document.id, "general_upload": item is None, "preview": reverse("web_document_preview", args=[document.id]), "checklist_matches": matches, "suggested_checklist_item_id": suggested_item_id, "fields": {
                **fields,
            }})
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=project.id)
            duplicate = _find_exact_project_file(project.id, final_path)
            if duplicate:
                raise ValueError(_exact_duplicate_message(final_name, duplicate))
            document = Document.objects.create(
                project=project, contract_number=project.contract_number, object_name=project.name,
                original_filename=final_name, stored_filename=final_stored, mime_type=final_mime,
                status="queued",
            )
            if item:
                _link_checklist_document(project.id, item, document)
        messages.success(request, f"Файл «{item_title}» принят. Распознавание выполняется в фоне.")
        return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab=checklist")
    except Exception as exc:
        for _name, _stored, source_path, _mime in saved:
            Path(source_path).unlink(missing_ok=True)
        if final_path:
            Path(final_path).unlink(missing_ok=True)
        if request.headers.get("X-IRD-Analyze") == "1":
            return JsonResponse({"error": str(exc)[:500] or "Не удалось распознать документ."}, status=422)
        messages.error(request, str(exc) if isinstance(exc, ValueError) else "Не удалось подготовить документ.")
        return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab=checklist")


def _link_checklist_document(project_id, item, document):
    if document.project_id != project_id or document.archived_at:
        raise Http404
    if item.validity_years > 0 and item.title != "Инженерно-геодезические изыскания":
        return False
    if document.version_group_id:
        group = DocumentVersionGroup.objects.get(pk=document.version_group_id)
        if group.project_id != project_id or (group.checklist_item_id and group.checklist_item_id != item.pk):
            return False
        group.checklist_item = item
        group.save(update_fields=["checklist_item"])
        ChecklistDocument.objects.get_or_create(
            checklist_item=item,
            document=document,
            defaults={"sort_order": document.version_order},
        )
        if not item.document_id or item.document.archived_at:
            item.document = document
        item.status = "received"
        item.save(update_fields=["document", "status", "updated_at"])
        return True
    if allows_multiple_documents(item.title):
        next_order = (ChecklistDocument.objects.filter(checklist_item=item).aggregate(value=Max("sort_order"))["value"] or -1) + 1
        ChecklistDocument.objects.get_or_create(checklist_item=item, document=document, defaults={"sort_order": next_order})
        primary = item.document if item.document_id and not item.document.archived_at else document
    else:
        # Keep archived versions linked so the checklist and document dialog can
        # show the complete revision history alongside the active file.
        ChecklistDocument.objects.filter(
            checklist_item=item, document__archived_at__isnull=True,
        ).exclude(document=document).delete()
        ChecklistDocument.objects.update_or_create(
            checklist_item=item, document=document, defaults={"sort_order": 0},
        )
        primary = document
    item.document = primary
    item.status = "received"
    item.save(update_fields=["document", "status", "updated_at"])
    return True


def _append_checklist_document(project_id, item, document):
    """Attach another file to a checklist item without detaching its current file."""
    if document.project_id != project_id or document.archived_at:
        raise Http404
    if item.validity_years > 0 and item.title != "Инженерно-геодезические изыскания":
        return False
    if document.version_group_id:
        return _link_checklist_document(project_id, item, document)
    if allows_multiple_documents(item.title):
        return _link_checklist_document(project_id, item, document)
    next_order = (ChecklistDocument.objects.filter(checklist_item=item).aggregate(value=Max("sort_order"))["value"] or -1) + 1
    ChecklistDocument.objects.get_or_create(
        checklist_item=item, document=document, defaults={"sort_order": next_order},
    )
    if not item.document_id or item.document.archived_at:
        item.document = document
    item.status = "received"
    item.save(update_fields=["document", "status", "updated_at"])
    return True


@require_POST
def add_document_version(request, document_id):
    """Join a second uncommitted batch upload to the open document's version group."""
    target = _document_or_404(document_id, active_only=True)
    if target.project.archived_at:
        raise Http404
    try:
        source_id = int(request.POST.get("source_document_id", ""))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Выберите файл для переноса."}, status=400)
    if source_id == target.pk:
        return JsonResponse({"error": "Нельзя добавить документ как собственную версию."}, status=400)

    with transaction.atomic():
        Project.objects.select_for_update().get(pk=target.project_id)
        target = Document.objects.select_for_update().get(pk=target.pk)
        source = Document.objects.select_for_update().filter(
            pk=source_id, project_id=target.project_id, archived_at__isnull=True,
        ).first()
        if source is None or source.status in {"queued", "processing"}:
            return JsonResponse({"error": "Файл не найден в этом проекте или ещё распознаётся."}, status=404)
        if target.status in {"queued", "processing"}:
            return JsonResponse({"error": "Дождитесь завершения распознавания открытого файла."}, status=409)
        if ChecklistDocument.objects.filter(document_id__in=[target.pk, source.pk]).exists() or ProjectChecklistItem.objects.filter(
            document_id__in=[target.pk, source.pk],
        ).exists():
            return JsonResponse({"error": "Объединять можно только документы, ещё не сохранённые из этого пакета."}, status=409)
        if target.version_group_id and source.version_group_id and target.version_group_id != source.version_group_id:
            return JsonResponse({"error": "Эти файлы уже принадлежат разным группам версий."}, status=409)

        group = target.version_group or source.version_group
        if group is None:
            group = DocumentVersionGroup.objects.create(project_id=target.project_id)
        if group.project_id != target.project_id:
            return JsonResponse({"error": "Группа версий относится к другому проекту."}, status=400)
        selected_item_id = request.POST.get("checklist_item_id", "").strip()
        selected_item = None
        if selected_item_id:
            selected_item = ProjectChecklistItem.objects.filter(
                pk=selected_item_id, project_id=target.project_id,
            ).first()
            if selected_item is None:
                return JsonResponse({"error": "Выбранный пункт комплектности не относится к проекту."}, status=400)
            if selected_item.validity_years > 0 and selected_item.title != "Инженерно-геодезические изыскания":
                return JsonResponse({"error": "К этому пункту нельзя привязать файл."}, status=400)
        if group.checklist_item_id and selected_item and group.checklist_item_id != selected_item.pk:
            return JsonResponse({"error": "У версий должен быть один общий пункт комплектности."}, status=409)
        if selected_item and not group.checklist_item_id:
            group.checklist_item = selected_item
            group.save(update_fields=["checklist_item"])

        members = list(group.versions.order_by("version_order", "id"))
        next_order = max((version.version_order for version in members), default=0) + 1
        if not target.version_group_id:
            target.version_group = group
            target.version_order = next_order
            target.save(update_fields=["version_group", "version_order"])
            next_order += 1
        if not source.version_group_id:
            source.version_group = group
            source.version_order = next_order
            source.save(update_fields=["version_group", "version_order"])

    return JsonResponse({
        "group_id": group.pk,
        "checklist_item_id": group.checklist_item_id or "",
        "version_ids": list(group.versions.order_by("version_order", "id").values_list("id", flat=True)),
    })


def _checklist_analysis_fields(parsed, fallback_title):
    """Keep the X-IRD-Analyze response in step with Flask's OCR corrections."""
    from app.services.field_extractor import RegexFieldExtractor

    raw_text = parsed.raw_text or ""
    first_page = raw_text.split("--- page 2 ---", 1)[0]
    from app.services.checklist_document_fields import PROJECT_ASSIGNMENT_TITLE
    project_assignment = normalize(fallback_title) == normalize(PROJECT_ASSIGNMENT_TITLE)
    eroks_card = parsed.working_title == "Копия регистрационной карточки в ЕРОКС"
    beltelecom = RegexFieldExtractor._is_beltelecom_technical_conditions(raw_text)
    minskenergo = RegexFieldExtractor._is_minskenergo_electrical_conditions(raw_text)
    header_number = "" if (eroks_card or beltelecom or minskenergo) else RegexFieldExtractor._extract_committee_decision_number(first_page)
    number_match = None
    if not (eroks_card or beltelecom or minskenergo):
        number_match = re.search(r"(?:№|N\s*|номер\s*)\s*([А-ЯЁA-Z0-9][А-ЯЁA-Z0-9./-]{1,30})", first_page, re.I)
        if number_match and not re.search(r"\d", number_match.group(1)):
            number_match = None
    document_number = "" if (eroks_card or project_assignment) else (
        parsed.document_number if (beltelecom or minskenergo)
        else (header_number or parsed.document_number or (number_match.group(1) if number_match else ""))
    )
    return {
        "working_title": clean_document_name(parsed.working_title or fallback_title, parsed.issuer),
        "official_title": parsed.official_title or "",
        "issuer": parsed.issuer or "",
        "document_number": document_number,
        "issue_date": iso_date(parsed.issue_date),
        "valid_until": "" if project_assignment else iso_date(parsed.valid_until),
        "no_expiration": True if project_assignment else parsed.no_expiration,
        "note": "",
    }


def _document_or_404(document_id, *, active_only=False):
    query = Document.objects.select_related("project").filter(pk=document_id)
    if active_only:
        query = query.filter(archived_at__isnull=True)
    document = query.first()
    if document is None or document.project_id is None:
        raise Http404
    return document


def _stored_document_path(document):
    root = Path(settings.MEDIA_ROOT).resolve()
    name = document.stored_filename or ""
    candidate = Path(name)
    target = (root / candidate).resolve()
    if not name or candidate.is_absolute() or candidate.name != name or target.parent != root or not target.is_file():
        raise Http404
    return target


@require_GET
def document_detail(request, document_id):
    document = _document_or_404(document_id, active_only=True)
    if document.project.archived_at:
        raise Http404
    return redirect(f"{reverse('web_object_workspace', args=[document.project_id])}?tab=checklist&open_document={document.id}")


@require_POST
def update_document_fields(request, document_id):
    document = _document_or_404(document_id)
    requested_item_id = request.POST.get("checklist_item_id") if "checklist_item_id" in request.POST else None
    version_group = None
    if document.version_group_id:
        version_group = DocumentVersionGroup.objects.select_related("checklist_item").get(pk=document.version_group_id)
        if version_group.project_id != document.project_id:
            return JsonResponse({"error": "Группа версий относится к другому проекту."}, status=400)
        if not requested_item_id and version_group.checklist_item_id:
            requested_item_id = str(version_group.checklist_item_id)
    selected_item = None
    if requested_item_id:
        try:
            selected_item = ProjectChecklistItem.objects.filter(
                pk=int(requested_item_id), project_id=document.project_id,
            ).first()
        except (TypeError, ValueError):
            selected_item = None
        if selected_item is None:
            return JsonResponse({"error": "Выбранный пункт не относится к этому проекту."}, status=400)
        if selected_item.validity_years > 0 and selected_item.title != "Инженерно-геодезические изыскания":
            return JsonResponse({"error": "К этому пункту нельзя привязать файл."}, status=400)
        if version_group and version_group.checklist_item_id and version_group.checklist_item_id != selected_item.pk:
            return JsonResponse({"error": "У всех версий документа должен быть один общий пункт комплектности."}, status=409)
        active_linked_ids = set(ChecklistDocument.objects.filter(
            checklist_item=selected_item,
            document__archived_at__isnull=True,
        ).values_list("document_id", flat=True))
        if selected_item.document_id and not selected_item.document.archived_at:
            active_linked_ids.add(selected_item.document_id)
        active_linked_ids.discard(document.id)
        same_version_group_item = bool(
            version_group and version_group.checklist_item_id == selected_item.pk
        )
        if active_linked_ids and not same_version_group_item and not allows_multiple_documents(selected_item.title):
            return JsonResponse({"error": "В списке доступны только пункты без загруженных документов."}, status=409)
    no_expiration = request.POST.get("no_expiration") == "on"
    document.working_title = request.POST.get("working_title", "").strip()
    document.official_title = request.POST.get("official_title", "").strip()
    document.issuer = request.POST.get("issuer", "").strip()
    document.document_number = request.POST.get("document_number", "").strip()
    document.working_title = clean_document_name(
        document.working_title, document.issuer, document.project.customer if document.project_id else '',
    )
    document.issue_date = iso_date(request.POST.get("issue_date", "").strip())
    document.valid_until = "" if no_expiration else iso_date(request.POST.get("valid_until", "").strip())
    document.no_expiration = no_expiration
    from app.services.checklist_document_fields import PROJECT_ASSIGNMENT_TITLE
    project_assignment = ProjectChecklistItem.objects.filter(project_id=document.project_id).filter(
        Q(document_id=document.id) | Q(checklistdocument__document_id=document.id),
        title__iexact=PROJECT_ASSIGNMENT_TITLE,
    ).exists()
    if project_assignment:
        document.document_number = ""
        document.valid_until = ""
        document.no_expiration = True
    document.save(update_fields=["working_title", "official_title", "issuer", "document_number", "issue_date", "valid_until", "no_expiration"])
    if selected_item is not None and not _link_checklist_document(document.project_id, selected_item, document):
        return JsonResponse({"error": "Не удалось привязать документ к выбранному пункту."}, status=400)
    if request.headers.get("X-Add-To-Checklist") == "1":
        title = document.working_title or document.official_title or document.original_filename
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=document.project_id)
            item = ProjectChecklistItem.objects.filter(project_id=document.project_id, document=document).first()
            if item is None:
                item = next((row for row in ProjectChecklistItem.objects.filter(
                    project_id=document.project_id, status="missing", validity_years=0,
                ).order_by("id") if normalize(row.title) == normalize(title)), None)
            if item is None:
                order = ProjectChecklistItem.objects.filter(project_id=document.project_id).aggregate(value=Max("sort_order"))["value"]
                item = ProjectChecklistItem.objects.create(
                    project_id=document.project_id, title=title, sort_order=(order if order is not None else -1) + 1,
                )
            _link_checklist_document(document.project_id, item, document)
    if request.headers.get("X-Autosave") != "1":
        from app.services.email_notifications import queue_saved_document_notification
        queue_saved_document_notification(document)
    if request.headers.get("X-Preserve-Checklist-Link") != "1":
        title = (document.working_title or "").strip().casefold()
        if title:
            item = ProjectChecklistItem.objects.filter(project_id=document.project_id, status="missing", validity_years=0).order_by("id").first()
            if item and item.title.strip().casefold() == title:
                item.status, item.document = "received", document
                item.save(update_fields=["status", "document", "updated_at"])
                ChecklistDocument.objects.get_or_create(checklist_item=item, document=document, defaults={"sort_order": 0})
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({}, status=204)
    return redirect("web_document_detail", document_id=document.id)


@require_POST
def update_document_note(request, document_id):
    document = _document_or_404(document_id)
    document.note = request.POST.get("note", "").strip()
    document.save(update_fields=["note"])
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({}, status=204)
    return redirect("web_document_detail", document_id=document.id)


@require_GET
def document_preview(request, document_id):
    document = _document_or_404(document_id)
    if document.project.archived_at:
        raise Http404
    path = _stored_document_path(document)
    from app.services.word_documents import is_word_document, word_preview_path
    if is_word_document(path):
        try:
            path = word_preview_path(path)
        except (OSError, RuntimeError, ValueError):
            return JsonResponse({"error": "Не удалось подготовить предварительный просмотр."}, status=415)
    response = FileResponse(path.open("rb"), content_type="application/pdf" if path.suffix.lower() == ".pdf" else document.mime_type)
    response["Content-Disposition"] = content_disposition_header(False, Path(document.original_filename or path.name).name)
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def document_viewer(request, document_id):
    document = _document_or_404(document_id)
    if document.project.archived_at:
        raise Http404
    source = _stored_document_path(document)
    from app.services.word_documents import is_word_document, word_preview_path
    try:
        pdf_path = word_preview_path(source) if is_word_document(source) else source
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            page_count = len(pdf)
        finally:
            pdf.close()
    except Exception as exc:
        logging.getLogger(__name__).exception("Could not open PDF preview for document id=%s", document_id)
        return HttpResponse("Preview unavailable", status=415)
    try:
        initial_page = int(request.GET.get("page", "1"))
    except (TypeError, ValueError):
        initial_page = 1
    initial_page = min(max(initial_page, 1), max(page_count, 1))
    return render(request, "document_viewer.html", _document_viewer_context(
        document_id, page_count, initial_page,
        page_route="web_document_viewer_page",
        highlight_route="web_document_viewer_highlights",
        original_url=reverse("web_document_preview", args=[document_id]),
        cache_version=f"{pdf_path.stat().st_mtime_ns}-{pdf_path.stat().st_size}",
    ), using="jinja2")


def _document_viewer_context(record_id, page_count, initial_page, *, page_route, highlight_route, original_url, cache_version=""):
    from django.urls import reverse

    page_url = reverse(page_route, args=[record_id, 0])
    page_base = page_url.rsplit("/", 1)[0]
    versioned_page_url = f"{page_url}?v={cache_version}"
    highlight_url = reverse(highlight_route, args=[record_id, 0])
    return {
        "document_id": record_id, "page_count": page_count,
        "initial_page": initial_page,
        "page_url_template": versioned_page_url,
        "page_image_url": f"{page_base}/{initial_page - 1}?v={cache_version}",
        "page_image_urls": [f"{page_base}/{index}?v={cache_version}" for index in range(page_count)],
        "thumbnail_urls": [
            f"{page_base}/{index}?v={cache_version}&thumbnail=1"
            for index in range(page_count)
        ],
        "highlight_url_template": highlight_url,
        "original_url": original_url,
    }


def _cached_pdf_page(pdf_path, cache_id, page_index, thumbnail=False):
    import pypdfium2 as pdfium

    stat = pdf_path.stat()
    cache = Path(settings.BASE_DIR) / "instance" / "preview-cache" / f"{cache_id}-{stat.st_mtime_ns}-{stat.st_size}"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{page_index}-{'thumb' if thumbnail else 'page'}.jpg"
    valid_cache = False
    try:
        if target.is_file() and target.stat().st_size > 4:
            with target.open("rb") as cached_image:
                valid_cache = cached_image.read(2) == bytes((255, 216))
                cached_image.seek(-2, os.SEEK_END)
                valid_cache = valid_cache and cached_image.read(2) == bytes((255, 217))
    except OSError:
        valid_cache = False

    if not valid_cache:
        temp_target = cache / f".{target.name}.{uuid.uuid4().hex}.tmp"
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            if page_index < 0 or page_index >= len(pdf):
                raise Http404
            bitmap = pdf[page_index].render(scale=0.3 if thumbnail else 1.6)
            bitmap.to_pil().convert("RGB").save(
                temp_target, format="JPEG", quality=82 if thumbnail else 90, optimize=True,
            )
            if temp_target.stat().st_size <= 4:
                raise OSError("Rendered PDF page is empty")
            os.replace(temp_target, target)
        finally:
            pdf.close()
            temp_target.unlink(missing_ok=True)
    return target


@require_GET
def document_viewer_page(request, document_id, page_index):
    document = _document_or_404(document_id)
    if document.project.archived_at:
        raise Http404
    source = _stored_document_path(document)
    from app.services.word_documents import is_word_document, word_preview_path
    try:
        pdf_path = word_preview_path(source) if is_word_document(source) else source
        thumbnail = request.GET.get("thumbnail") == "1"
        target = _cached_pdf_page(pdf_path, f"{document_id}", page_index, thumbnail)
        response = FileResponse(target.open("rb"), content_type="image/jpeg")
        response["Cache-Control"] = "no-cache, must-revalidate"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    except Http404:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Could not render PDF page %s for document id=%s", page_index, document_id)
        return HttpResponse("Preview unavailable", status=415)


def _stored_correspondence_path(letter):
    root = Path(settings.MEDIA_ROOT).resolve()
    name = letter.stored_filename or ""
    candidate = Path(name)
    source = (root / candidate).resolve()
    if candidate.is_absolute() or candidate.name != name or source.parent != root or not source.is_file():
        raise Http404
    from app.services.word_documents import is_word_document, word_preview_path
    return word_preview_path(source) if is_word_document(source) else source


@require_GET
def correspondence_viewer(request, letter_id):
    letter = Correspondence.objects.filter(pk=letter_id).select_related("project").first()
    if letter is None or letter.project.archived_at:
        raise Http404
    try:
        pdf_path = _stored_correspondence_path(letter)
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            page_count = len(pdf)
        finally:
            pdf.close()
    except Http404:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Could not open PDF preview for correspondence id=%s", letter_id)
        return HttpResponse("Preview unavailable", status=415)
    try:
        initial_page = int(request.GET.get("page", "1"))
    except (TypeError, ValueError):
        initial_page = 1
    initial_page = min(max(initial_page, 1), max(page_count, 1))
    return render(request, "document_viewer.html", _document_viewer_context(
        letter_id, page_count, initial_page,
        page_route="web_correspondence_viewer_page",
        highlight_route="web_correspondence_viewer_highlights",
        original_url=reverse("web_correspondence_preview", args=[letter_id]),
        cache_version=f"{pdf_path.stat().st_mtime_ns}-{pdf_path.stat().st_size}",
    ), using="jinja2")


def _render_correspondence_page(request, letter_id, page_index):
    letter = Correspondence.objects.filter(pk=letter_id).select_related("project").first()
    if letter is None or letter.project.archived_at:
        raise Http404
    return _render_pdf_viewer_page(request, _stored_correspondence_path(letter), f"letter-{letter_id}", page_index)


def _render_pdf_viewer_page(request, pdf_path, cache_id, page_index):
    try:
        thumbnail = request.GET.get("thumbnail") == "1"
        target = _cached_pdf_page(pdf_path, cache_id, page_index, thumbnail)
        response = FileResponse(target.open("rb"), content_type="image/jpeg")
        response["Cache-Control"] = "no-cache, must-revalidate"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    except Http404:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Could not render correspondence page %s id=%s", page_index, cache_id)
        return HttpResponse("Preview unavailable", status=415)


@require_GET
def correspondence_viewer_page(request, letter_id, page_index):
    return _render_correspondence_page(request, letter_id, page_index)


@require_POST
def document_viewer_highlights(request, document_id, page_index):
    document = _document_or_404(document_id)
    if document.project.archived_at:
        raise Http404
    return _viewer_highlight_response(request, _stored_document_path(document), f"document-{document_id}", page_index)


@require_POST
def correspondence_viewer_highlights(request, letter_id, page_index):
    letter = Correspondence.objects.filter(pk=letter_id).select_related("project").first()
    if letter is None or letter.project.archived_at:
        raise Http404
    return _viewer_highlight_response(request, _stored_correspondence_path(letter), f"letter-{letter_id}", page_index)


def _viewer_highlight_response(request, source_path, cache_id, page_index):
    import hashlib
    import json

    query = request.POST.get("q", "").strip()[:1200]
    if not query:
        return JsonResponse({"rects": []})
    try:
        pdf_path = source_path
        from app.services.word_documents import is_word_document, word_preview_path
        if is_word_document(pdf_path):
            pdf_path = word_preview_path(pdf_path)
        stat = pdf_path.stat()
        cache = Path(settings.BASE_DIR) / "instance" / "preview-cache" / f"{cache_id}-{stat.st_mtime_ns}-{stat.st_size}"
        cache.mkdir(parents=True, exist_ok=True)
        from app.services.document_highlight import HIGHLIGHT_VERSION, highlight_paragraph
        key = hashlib.sha256(f"{HIGHLIGHT_VERSION}\0{page_index}\0{query}".encode("utf-8")).hexdigest()[:24]
        target = cache / f"highlight-{key}.json"
        if target.is_file():
            return JsonResponse(json.loads(target.read_text(encoding="utf-8")))
        rects = highlight_paragraph(
            pdf_path, page_index, query,
            lang=settings.TESSERACT_LANG, dpi=settings.OCR_DPI,
        )
        payload = {"rects": rects}
        old_cache = sorted(cache.glob("highlight-*.json"), key=lambda item: item.stat().st_mtime)
        for old in old_cache[:-255]:
            old.unlink(missing_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return JsonResponse(payload)
    except Exception:
        logging.getLogger(__name__).exception("Could not locate cited text in source=%s page=%s", cache_id, page_index)
        return JsonResponse({"rects": []})


@require_POST
def archive_document(request, document_id):
    document = _document_or_404(document_id, active_only=True)
    document.archived_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    linked_items = list(ProjectChecklistItem.objects.filter(document=document, validity_years=0))
    with transaction.atomic():
        for item in linked_items:
            link = ChecklistDocument.objects.filter(
                checklist_item=item, document__archived_at__isnull=True,
            ).exclude(document=document).select_related("document").order_by(
                "sort_order", "created_at", "document_id",
            ).first()
            replacement = link.document if link else None
            item.document = replacement
            item.status = "received" if replacement else "missing"
            item.save(update_fields=["document", "status", "updated_at"])
        legacy_item_ids = [item.pk for item in linked_items]
        for item in ProjectChecklistItem.objects.filter(
            project_id=document.project_id, document__isnull=True, validity_years=0,
        ).exclude(pk__in=legacy_item_ids):
            if normalize(item.title) in {
                normalize(document.working_title or ""), normalize(document.official_title or ""),
            }:
                item.status = "missing"
                item.save(update_fields=["status", "updated_at"])
        if request.headers.get("X-Preserve-Checklist-Link") != "1" and not document.version_group_id:
            ChecklistDocument.objects.filter(document=document).delete()
        document.save(update_fields=["archived_at"])
    messages.success(request, "Документ перемещён в архив проекта. Файл сохранён.")
    return redirect("web_object_workspace", object_id=document.project_id)


@require_POST
def restore_document(request, document_id):
    document = _document_or_404(document_id)
    if not document.archived_at:
        raise Http404
    checklist_item_id = request.POST.get("checklist_item_id", "").strip()
    with transaction.atomic():
        document.archived_at = None
        document.save(update_fields=["archived_at"])
        if checklist_item_id:
            item = _checklist_item_or_404(document.project_id, checklist_item_id)
            if request.headers.get("X-Add-Checklist-File") == "1":
                _append_checklist_document(document.project_id, item, document)
            else:
                _link_checklist_document(document.project_id, item, document)
    messages.success(request, "Документ восстановлен вместе с исходным файлом.")
    return redirect("web_object_workspace", object_id=document.project_id)


@require_POST
def delete_document(request, document_id):
    document = _document_or_404(document_id)
    project_id, stored_name = document.project_id, document.stored_filename
    version_group_id = document.version_group_id
    path = None
    try:
        path = _stored_document_path(document)
    except Http404:
        pass
    with transaction.atomic():
        linked_items = list(ProjectChecklistItem.objects.filter(document=document, validity_years=0))
        for item in linked_items:
            replacement_link = ChecklistDocument.objects.filter(
                checklist_item=item, document__archived_at__isnull=True,
            ).exclude(document=document).select_related("document").order_by(
                "sort_order", "created_at", "document_id",
            ).first()
            replacement = replacement_link.document if replacement_link else None
            item.document = replacement
            item.status = "received" if replacement else "missing"
            item.save(update_fields=["document", "status", "updated_at"])
        # Clear a legacy primary relation even if no association row exists.
        linked_ids = [row.pk for row in linked_items]
        for item in ProjectChecklistItem.objects.filter(
            project_id=document.project_id, document__isnull=True, validity_years=0,
        ).exclude(pk__in=linked_ids):
            if normalize(item.title) in {
                normalize(document.working_title or ""), normalize(document.official_title or ""),
            }:
                item.status = "missing"
                item.save(update_fields=["status", "updated_at"])
        ChecklistDocument.objects.filter(document=document).delete()
        document.delete()
        if version_group_id:
            group = DocumentVersionGroup.objects.filter(pk=version_group_id).first()
            remaining = list(group.versions.order_by("version_order", "id")) if group else []
            if group and len(remaining) <= 1:
                if remaining:
                    remaining[0].version_group = None
                    remaining[0].version_order = 0
                    remaining[0].save(update_fields=["version_group", "version_order"])
                group.delete()
    if path and not Document.objects.filter(stored_filename=stored_name).exists() and not Correspondence.objects.filter(stored_filename=stored_name).exists():
        path.unlink(missing_ok=True)
        from app.services.word_documents import remove_word_preview
        remove_word_preview(path)
    return redirect("web_object_workspace", object_id=project_id)

def _share_token_digest(token):
    if not token or len(token) > 100:
        raise Http404
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _active_project_share(token):
    share = ProjectShare.objects.select_related("project").filter(
        token_digest=_share_token_digest(token), revoked_at__isnull=True,
        expires_at__gt=django_timezone.now(), project__archived_at__isnull=True,
    ).first()
    if share is None:
        raise Http404
    return share


def _share_is_unlocked(request, share):
    return request.session.get(f"project_share_{share.pk}") == share.password_hash


def _share_response_headers(response):
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def manage_project_share(request, object_id):
    project = visible_projects(request.user).filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    can_manage = request.user.is_superuser or getattr(
        getattr(request.user, "profile", None), "access_level", None,
    ) == "edit"
    if not can_manage:
        raise PermissionDenied

    share = ProjectShare.objects.filter(
        project=project, created_by=request.user,
    ).order_by("-created_at", "-pk").first()
    checklist = list(ProjectChecklistItem.objects.filter(project=project).values(
        "status", "validity_years", "issue_date",
    ))
    received_count = sum(
        bool(row["status"] == "received" or (row["validity_years"] and row["issue_date"]))
        for row in checklist
    )
    correspondence_count = Correspondence.objects.filter(
        project=project, record_type="correspondence",
    ).count()
    approvals = Correspondence.objects.filter(
        project=project, record_type="approval",
    )
    share_session_key = (
        f"project_share_url_user_{request.user.pk}_{share.pk}" if share else None
    )
    share_active = bool(share and share.revoked_at is None and share.expires_at > django_timezone.now())
    generated_url = (
        request.session.get(share_session_key)
        if share_active and share_session_key else None
    )
    error = ""
    if request.method == "POST":
        if request.POST.get("action") == "revoke":
            if share and share.revoked_at is None:
                share.revoked_at = django_timezone.now()
                share.save(update_fields=["revoked_at"])
                if share_session_key:
                    request.session.pop(share_session_key, None)
            messages.success(request, "Ссылка отключена.")
            return redirect("web_manage_project_share", object_id=project.pk)
        password = request.POST.get("password", "")
        expires_raw = request.POST.get("expires_on", "")
        try:
            expires_date = datetime.strptime(expires_raw, "%Y-%m-%d").date()
            expires_at = datetime.combine(
                expires_date, datetime_time.max, tzinfo=django_timezone.get_current_timezone(),
            )
        except (TypeError, ValueError):
            expires_at = None
        if len(password) < 8:
            error = "Пароль должен содержать не менее 8 символов."
        elif expires_at is None or expires_at <= django_timezone.now():
            error = "Укажите дату окончания действия в будущем."
        else:
            raw_token = secrets.token_urlsafe(32)
            now = django_timezone.now()
            if share_session_key:
                request.session.pop(share_session_key, None)
            ProjectShare.objects.filter(
                project=project,
                created_by=request.user,
                revoked_at__isnull=True,
                expires_at__gt=now,
            ).update(revoked_at=now)
            share = ProjectShare.objects.create(
                project=project,
                token_digest=_share_token_digest(raw_token),
                password_hash=make_password(password),
                expires_at=expires_at,
                created_by=request.user,
            )
            share_session_key = f"project_share_url_user_{request.user.pk}_{share.pk}"
            generated_url = request.build_absolute_uri(
                reverse("web_project_shared_view", kwargs={"share_token": raw_token}),
            )
            request.session[share_session_key] = generated_url
            share_active = True
    response = render(request, "project_share_manage.html", {
        "object": project,
        "active_project": project,
        "checklist": checklist,
        "received_count": received_count,
        "correspondence_count": correspondence_count,
        "approvals": approvals,
        "share": share,
        "generated_url": generated_url,
        "error": error,
        "expiry_min": django_timezone.localdate().isoformat(),
        "default_expiry": (django_timezone.localdate() + timedelta(days=30)).isoformat(),
        "share_active": share_active,
    }, using="jinja2")
    return _share_response_headers(response)


@login_not_required
@require_http_methods(["GET", "POST"])
def project_shared_view(request, share_token):
    share = _active_project_share(share_token)
    session_key = f"project_share_{share.pk}"
    if not _share_is_unlocked(request, share):
        error = ""
        if request.method == "POST":
            ip = request.META.get("REMOTE_ADDR", "")
            throttle_id = hashlib.sha256(ip.encode("utf-8")).hexdigest()[:24]
            throttle_key = f"project-share-attempts:{share.pk}:{throttle_id}"
            attempts = cache.get(throttle_key, 0)
            if attempts >= 8:
                error = "Слишком много попыток. Попробуйте снова через 15 минут."
            elif check_password(request.POST.get("password", ""), share.password_hash):
                cache.delete(throttle_key)
                request.session.cycle_key()
                request.session[session_key] = share.password_hash
                return redirect("web_project_shared_view", share_token=share_token)
            else:
                cache.set(throttle_key, attempts + 1, timeout=15 * 60)
                error = "Неверный пароль. Проверьте его и попробуйте ещё раз."
        response = render(request, "project_share_password.html", {
            "project": share.project, "share_token": share_token, "error": error,
        }, using="jinja2")
        return _share_response_headers(response)

    project = share.project
    documents = []
    for document in Document.objects.filter(project=project, archived_at__isnull=True).order_by("-created_at", "-id"):
        documents.append({
            "title": format_document_title(
                document.working_title or document.official_title or document.original_filename,
                document.issuer,
            ),
            "number": document.document_number,
            "issue_date": document.issue_date,
            "valid_until": document.valid_until,
            "file_url": reverse("web_project_shared_file", kwargs={
                "share_token": share_token, "kind": "document", "record_id": document.pk,
            }),
        })
    letters = []
    for kind, label in (("correspondence", "Письма"), ("approval", "Согласования")):
        rows = []
        for letter in Correspondence.objects.filter(project=project, record_type=kind).order_by("-letter_date", "-id"):
            rows.append({
                "number": letter.number,
                "date": letter.letter_date,
                "sender": letter.sender,
                "recipient": letter.recipient,
                "subject": letter.subject,
                "summary": letter.summary,
                "file_url": reverse("web_project_shared_file", kwargs={
                    "share_token": share_token, "kind": kind, "record_id": letter.pk,
                }) if letter.stored_filename else None,
            })
        letters.append({"kind": kind, "label": label, "rows": rows})
    response = render(request, "project_shared_view.html", {
        "project": project,
        "documents": documents,
        "letters": letters,
        "share_token": share_token,
        "expires_at": share.expires_at,
    }, using="jinja2")
    return _share_response_headers(response)


@login_not_required
@require_GET
def project_shared_archive(request, share_token):
    share = _active_project_share(share_token)
    if not _share_is_unlocked(request, share):
        raise Http404

    project = share.project
    documents = list(Document.objects.filter(
        project=project, archived_at__isnull=True,
    ).order_by("-created_at", "-id").values())
    letters = list(Correspondence.objects.filter(
        project=project, record_type="correspondence",
    ).order_by("-letter_date", "-id").values())
    approvals = list(Correspondence.objects.filter(
        project=project, record_type="approval",
    ).order_by("-letter_date", "-id").values())

    archive = BytesIO()
    upload_root = Path(settings.MEDIA_ROOT).resolve()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        def add_file(record, folder, label, title, date_value=""):
            stored = record.get("stored_filename") or ""
            candidate = Path(stored)
            path = (upload_root / candidate).resolve() if stored else None
            if path is None or candidate.is_absolute() or candidate.name != stored or path.parent != upload_root or not path.is_file():
                return
            safe_title = _archive_name(title, label)
            if folder == "01_ИРД":
                extension = Path(record.get("original_filename") or path.name).suffix or path.suffix
                member = f"{folder}/{record['id']:03d}_{safe_title}{extension}"
            else:
                date_part = _archive_name(date_value or "без_даты", "без_даты")
                member = f"{folder}/{record['id']:03d}_{date_part}_{safe_title}{path.suffix}"
            bundle.writestr(member, path.read_bytes())

        for row in documents:
            add_file(row, "01_ИРД", "Документ",
                     row.get("working_title") or row.get("official_title") or row.get("original_filename") or "Документ")
        for row in letters:
            add_file(row, "02_Переписка", "Письмо",
                     row.get("subject") or row.get("sender") or "Письмо", row.get("letter_date"))
        for row in approvals:
            add_file(row, "03_Согласования", "Согласование",
                     row.get("subject") or row.get("sender") or "Согласование", row.get("letter_date"))

    filename = _archive_name(project.name, f"Проект_{project.pk}")
    response = FileResponse(BytesIO(archive.getvalue()), content_type="application/zip")
    response["Content-Disposition"] = content_disposition_header(True, f"Архив_проекта_{filename}.zip")
    return _share_response_headers(response)


@login_not_required
@require_GET
def project_shared_file(request, share_token, kind, record_id):
    share = _active_project_share(share_token)
    if not _share_is_unlocked(request, share):
        raise Http404
    is_download = request.GET.get("download") == "1"
    preview = request.GET.get("preview") == "1"
    if kind == "document":
        record = Document.objects.filter(pk=record_id, project=share.project, archived_at__isnull=True).first()
        if record is None:
            raise Http404
        path = _stored_document_path(record)
        filename = Path(record.original_filename or path.name).name
        content_type = record.mime_type or "application/octet-stream"
    elif kind in {"correspondence", "approval"}:
        record = Correspondence.objects.filter(pk=record_id, project=share.project, record_type=kind).first()
        if record is None:
            raise Http404
        stored = record.stored_filename or ""
        candidate = Path(stored)
        root = Path(settings.MEDIA_ROOT).resolve()
        path = (root / candidate).resolve()
        if not stored or candidate.is_absolute() or candidate.name != stored or path.parent != root or not path.is_file():
            raise Http404
        filename = Path(record.original_filename or path.name).name
        content_type = record.mime_type or "application/octet-stream"
    else:
        raise Http404
    if preview:
        from app.services.word_documents import is_word_document, word_preview_path
        if is_word_document(path):
            try:
                path = word_preview_path(path)
            except (OSError, RuntimeError, ValueError):
                raise Http404
            content_type = "application/pdf"
            filename = f"{Path(filename).stem}.pdf"
    response = FileResponse(path.open("rb"), content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(is_download, filename)
    return _share_response_headers(response)


@require_GET
@login_not_required
def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok", "framework": "Django", "database": "connected"})


@require_GET
def object_workspace(request, object_id):
    project = visible_projects(request.user).filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    warning_days = int(os.getenv("EXPIRY_WARNING_DAYS", "30"))
    obj = {field: getattr(project, field) for field in (
        "id", "contract_number", "contract_date", "stage", "name", "customer", "template_id", "chief_engineer_id", "archived_at", "created_at",
    )}
    obj["chief_engineer_name"] = project.chief_engineer.get_full_name() if project.chief_engineer_id else ""
    obj["chief_engineer_last_name"] = project.chief_engineer.last_name if project.chief_engineer_id else ""
    documents = [attach_document_title(attach_expiry(row, warning_days)) for row in Document.objects.filter(
        project=project, archived_at__isnull=True,
    ).order_by("-created_at", "-id").values()]
    # Reconcile pre-existing uploads only on editable visits: GET requests from
    # a read-only guest must not mutate checklist relationships.
    if request.user.is_superuser or request.user.profile.access_level == "edit":
        for document in documents:
            title = document.get("working_title") or document.get("official_title")
            if not title:
                continue
            item = next((candidate for candidate in ProjectChecklistItem.objects.filter(
                project=project, status="missing", validity_years=0,
            ).order_by("id") if normalize(candidate.title) == normalize(title)), None)
            if item and not allows_multiple_documents(item.title) and normalize(item.title) == normalize(title):
                _link_checklist_document(project.id, item, Document.objects.get(pk=document["id"]))
    archived_documents = [attach_document_title(attach_expiry(row, warning_days)) for row in Document.objects.filter(
        project=project, archived_at__isnull=False,
    ).order_by("-archived_at", "-id").values()]
    active_by_id = {row["id"]: row for row in documents}
    all_by_id = {row["id"]: row for row in documents + archived_documents}
    checklist = []
    for item in ProjectChecklistItem.objects.filter(project=project).select_related("document"):
        row = {field: getattr(item, field) for field in (
            "id", "project_id", "source_template_item_id", "title", "description", "sort_order", "status",
            "document_id", "validity_years", "issue_date", "valid_until", "note", "replacement_required",
        )}
        linked = all_by_id.get(item.document_id)
        row.update({
            "linked_document_title": linked.get("working_title") if linked else None,
            "linked_document_issuer": linked.get("issuer") if linked else None,
            "linked_document_number": linked.get("document_number") if linked else None,
            "linked_document_issue_date": linked.get("issue_date") if linked else None,
            "linked_document_note": linked.get("note") if linked else None,
            "linked_document_archived_at": linked.get("archived_at") if linked else None,
            "expiry": expiry_state({"no_expiration": False, "valid_until": item.valid_until}, warning_days)
                     if item.validity_years else (linked.get("expiry") if linked else None),
        })
        checklist.append(row)
    normalize_title = normalize
    checklist.sort(key=lambda row: (
        0 if row["status"] == "received" else 1, row["sort_order"], row["id"],
    ))
    links = ChecklistDocument.objects.filter(checklist_item__project=project).select_related(
        "document", "checklist_item",
    ).order_by("checklist_item_id", "sort_order", "created_at", "document_id")
    checklist_documents, archived_item_document_ids, archived_document_item_ids = {}, {}, {}
    for link in links:
        document = all_by_id.get(link.document_id)
        if document and not document["archived_at"]:
            checklist_documents.setdefault(link.checklist_item_id, []).append(document)
        elif document:
            archived_item_document_ids.setdefault(link.checklist_item_id, []).append(link.document_id)
            archived_document_item_ids.setdefault(link.document_id, []).append(link.checklist_item_id)
    for item in checklist:
        if item["document_id"] in active_by_id if item["document_id"] else False:
            attached = checklist_documents.setdefault(item["id"], [])
            if all(row["id"] != item["document_id"] for row in attached):
                attached.insert(0, active_by_id[item["document_id"]])
    archived_item_ids = set(archived_item_document_ids)
    for item in checklist:
        title = normalize_title(item["title"])
        linked_title = normalize_title(item["linked_document_title"] or "")
        match = next((doc for doc in archived_documents
                      if (doc["working_title"] or doc["original_filename"])
                      and normalize_title(doc["working_title"] or doc["original_filename"]) in {title, linked_title}), None)
        if match:
            archived_item_ids.add(item["id"])
            item_archives = archived_item_document_ids.setdefault(item["id"], [])
            if match["id"] not in item_archives:
                item_archives.append(match["id"])
            archived_document_item_ids.setdefault(match["id"], []).append(item["id"])
    existing_sources = {row["source_template_item_id"] for row in checklist}
    existing_titles = {normalize_title(row["title"]) for row in checklist}
    template_choices = []
    for template in ChecklistTemplate.objects.order_by("name"):
        items = list(ChecklistTemplateItem.objects.filter(template_id=template.id).order_by("sort_order", "id").values())
        for row in items:
            row["already_added"] = row["id"] in existing_sources or normalize_title(row["title"]) in existing_titles
        template_choices.append({"template": template, "items": items, "item_count": len(items)})
    template_choice_data = [{
        "template": {"id": choice["template"].id, "name": choice["template"].name},
        "items": choice["items"],
    } for choice in template_choices]
    multi_ids = {row["id"] for row in checklist if allows_multiple_documents(row["title"])}
    multi_ids.update(
        row["id"] for row in checklist
        if len(checklist_documents.get(row["id"], [])) > 1
        or (
            len(checklist_documents.get(row["id"], [])) == 1
            and checklist_documents[row["id"]][0]["id"] != row["document_id"]
        )
    )
    for group in DocumentVersionGroup.objects.filter(project=project, checklist_item__isnull=False).annotate(
        version_count=Count("versions"),
    ):
        if group.version_count > 1:
            multi_ids.add(group.checklist_item_id)
    received_count = sum(bool(
        row["id"] in archived_item_ids
        or (row["id"] in multi_ids and checklist_documents.get(row["id"]))
        or (row["validity_years"] and row["issue_date"])
        or (row["document_id"] and row["document_id"] in active_by_id)
    ) for row in checklist)
    correspondence = list(Correspondence.objects.filter(project=project, record_type="correspondence").values())
    approvals = list(Correspondence.objects.filter(project=project, record_type="approval").values())
    def correspondence_date_key(row):
        normalized = _normalize_letter_date(row.get("letter_date", ""))
        return (bool(normalized), normalized, row["id"])
    correspondence.sort(key=correspondence_date_key, reverse=True)
    approvals.sort(key=correspondence_date_key, reverse=True)
    organizations = set()
    for sender, recipient in Correspondence.objects.values_list("sender", "recipient"):
        organizations.update(value.strip() for value in (sender, recipient) if value and value.strip())
    return render(request, "object.html", {
        "object": obj, "documents": documents, "archived_documents": archived_documents,
        "archived_document_titles": {(row["working_title"] or row["original_filename"] or "").strip().lower() for row in archived_documents},
        "archived_item_ids": archived_item_ids, "archived_item_document_ids": archived_item_document_ids,
        "archived_document_item_ids": archived_document_item_ids, "checklist": checklist,
        "template_choices": template_choices, "template_choice_data": template_choice_data,
        "checklist_documents": checklist_documents,
        "version_group_item_ids": {
            group.checklist_item_id for group in DocumentVersionGroup.objects.filter(
                project=project, checklist_item__isnull=False,
            ).annotate(version_count=Count("versions")) if group.version_count > 1
        },
        "multi_document_item_ids": multi_ids, "missing_count": len(checklist) - received_count,
        "received_count": received_count, "warning_days": warning_days, "correspondence": correspondence,
        "approvals": approvals, "correspondence_organizations": sorted(organizations, key=str.casefold),
        "error": "",
    }, using="jinja2")


@require_http_methods(["GET", "POST"])
def project_analytics(request, object_id):
    """Answer a question from searchable text belonging to the open project only."""
    from app.services.project_qa import (
        ProjectQuestionCancelled, answer_project_question, retrieve_project_sources,
    )

    if not request.user.is_superuser and not request.user.profile.show_ai_analytics:
        if request.method == "POST" and request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"error": "ИИ-аналитика отключена для вашей учётной записи."}, status=403)
        raise PermissionDenied("ИИ-аналитика отключена для вашей учётной записи.")

    project = visible_projects(request.user).filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404

    question = request.POST.get("question", "").strip()[:1000] if request.method == "POST" else ""
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if is_ajax and request.method == "POST":
        if not question:
            return JsonResponse({"error": "Введите вопрос по документам проекта.", "sources": []}, status=400)
        try:
            job_id = str(uuid.UUID(request.POST.get("request_id", "")))
        except (ValueError, AttributeError):
            job_id = str(uuid.uuid4())
        cancel_event = threading.Event()
        cancelled_before_start = False
        with _ANALYTICS_JOBS_LOCK:
            now = time.monotonic()
            for expired_id, job in list(_ANALYTICS_JOBS.items()):
                if job.get("expires_at", 0) and job["expires_at"] <= now:
                    _ANALYTICS_JOBS.pop(expired_id, None)
            previous = _ANALYTICS_JOBS.get(job_id)
            cancelled_before_start = bool(
                previous
                and (previous["user_id"], previous["project_id"]) == (request.user.pk, project.pk)
                and previous["status"] == "cancelled"
            )
            if not cancelled_before_start:
                _ANALYTICS_JOBS[job_id] = {
                    "user_id": request.user.pk,
                    "project_id": project.pk,
                    "cancel_event": cancel_event,
                    "status": "processing",
                    "answer": "",
                    "stream_answer": "",
                    "stream_seq": 0,
                    "error": "",
                    "sources": [],
                    "cached": False,
                    "expires_at": 0,
                }
            elif previous:
                previous.update({"answer": "", "stream_answer": "", "stream_seq": 0, "error": "", "sources": [], "cached": False})
        if cancelled_before_start:
            return JsonResponse({"request_id": job_id, "status": "cancelled", "sources": []}, status=409)
        _ANALYTICS_EXECUTOR.submit(
            _run_project_analytics_job,
            job_id, request.user.pk, project.pk, question, cancel_event,
        )
        return JsonResponse({"request_id": job_id, "status": "processing", "sources": []}, status=202)

    answer = ""
    sources = []
    error = ""
    cancelled = False
    answer_cached = False
    if request.method == "POST" and not question:
        error = "Введите вопрос по документам проекта."
    elif request.method == "POST":
        from app.services.project_answer_cache import (
            get_cached_project_answer, project_search_revision, store_project_answer,
        )
        cached_answer = get_cached_project_answer(project.id, question)
        if cached_answer:
            answer = cached_answer["answer"]
            sources = cached_answer["sources"]
            answer_cached = True
        else:
            source_revision = project_search_revision(project.id)
            sources = retrieve_project_sources(project.id, question)
        if answer_cached:
            pass
        elif not sources:
            error = "В обработанных документах проекта не нашлось подходящих фрагментов. Проверьте формулировку вопроса или дождитесь распознавания новых файлов."
        elif not settings.OPENAI_API_KEY or not settings.USE_AI:
            error = "Поиск нашёл фрагменты, но ответы ИИ сейчас отключены. Источники показаны ниже."
        else:
            try:
                answer = answer_project_question(
                    question, sources,
                    cancel_check=None,
                )
                store_project_answer(project.id, question, answer, sources, source_revision)
            except ProjectQuestionCancelled:
                cancelled = True
            except Exception:
                logger.exception("Project analytics answer failed project_id=%s", project.id)
                error = "Не удалось подготовить ответ. Попробуйте ещё раз; найденные источники сохранены ниже."

    active_docs = Document.objects.filter(project=project, archived_at__isnull=True)
    active_letters = Correspondence.objects.filter(project=project)
    from migration_portal.models import PreparedDocument
    ready = PreparedDocument.objects.filter(status="ready")
    indexed_count = ready.filter(document__in=active_docs, document__status="processed").count()
    indexed_count += ready.filter(correspondence__in=active_letters, correspondence__status="processed").count()
    pending_count = active_docs.filter(status__in=["queued", "processing"]).count()
    pending_count += active_letters.filter(status__in=["queued", "processing"]).count()
    pending_count += active_docs.exclude(status__in=["queued", "processing"]).filter(prepared__status="pending").count()
    pending_count += active_letters.exclude(status__in=["queued", "processing"]).filter(prepared__status="pending").count()
    preparation_error_count = PreparedDocument.objects.filter(status="error").filter(
        Q(document__in=active_docs) | Q(correspondence__in=active_letters)
    ).count()
    checklist = list(ProjectChecklistItem.objects.filter(project=project).values("status"))
    received_count = sum(item["status"] == "received" for item in checklist)
    correspondence_count = active_letters.filter(record_type="correspondence").count()
    return render(request, "project_analytics.html", {
        "project": project, "object": project, "question": question,
        "answer": answer, "sources": sources, "analytics_error": error,
        "answer_cached": answer_cached,
        "indexed_count": indexed_count, "pending_count": pending_count,
        "preparation_error_count": preparation_error_count,
        "checklist": checklist, "received_count": received_count,
        "correspondence_count": correspondence_count,
    }, using="jinja2")


@require_GET
def project_analytics_status(request, object_id):
    if not request.user.is_superuser and not request.user.profile.show_ai_analytics:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"error": "ИИ-аналитика отключена для вашей учётной записи."}, status=403)
        raise PermissionDenied("ИИ-аналитика отключена для вашей учётной записи.")
    project = visible_projects(request.user).filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    try:
        job_id = str(uuid.UUID(request.GET.get("request_id", "")))
    except (ValueError, AttributeError):
        return JsonResponse({"error": "Некорректный идентификатор запроса."}, status=400)
    with _ANALYTICS_JOBS_LOCK:
        job = _ANALYTICS_JOBS.get(job_id)
        if not job or (job["user_id"], job["project_id"]) != (request.user.pk, project.pk):
            raise Http404
        result = {
            "status": job["status"],
            "answer": job["answer"],
            "stream_answer": job.get("stream_answer", job["answer"]),
            "stream_seq": job.get("stream_seq", 0),
            "error": job["error"],
            "sources": job["sources"],
            "cached": job.get("cached", False),
        }
    return JsonResponse(result)


@require_POST
def cancel_project_analytics(request, object_id):
    if not request.user.is_superuser and not request.user.profile.show_ai_analytics:
        raise PermissionDenied("ИИ-аналитика отключена для вашей учётной записи.")
    project = visible_projects(request.user).filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    try:
        job_id = str(uuid.UUID(request.POST.get("request_id", "")))
    except (ValueError, AttributeError):
        return JsonResponse({"cancelled": False}, status=400)
    with _ANALYTICS_JOBS_LOCK:
        now = time.monotonic()
        for expired_id, value in list(_ANALYTICS_JOBS.items()):
            if value.get("expires_at", 0) and value["expires_at"] <= now:
                _ANALYTICS_JOBS.pop(expired_id, None)
        job = _ANALYTICS_JOBS.get(job_id)
        if job and (job["user_id"], job["project_id"]) == (request.user.pk, project.pk):
            job["cancel_event"].set()
            job["status"] = "cancelled" if job["status"] != "complete" else job["status"]
            job["answer"] = ""
            job["stream_answer"] = ""
            job["stream_seq"] = 0
            job["expires_at"] = time.monotonic() + 300
            cancelled = True
        elif not job:
            event = threading.Event()
            event.set()
            _ANALYTICS_JOBS[job_id] = {
                "user_id": request.user.pk,
                "project_id": project.pk,
                "cancel_event": event,
                "status": "cancelled",
                "answer": "",
                "error": "",
                "sources": [],
                "cached": False,
                "expires_at": time.monotonic() + 120,
            }
            cancelled = True
        else:
            cancelled = False
    return JsonResponse({"cancelled": cancelled})


def _combine_images(files, output_path):
    from PIL import Image, ImageOps
    from app.services.storage import StorageService
    if any(Path(name).suffix.lower() in {".heic", ".heif"} for name, *_ in files):
        import pillow_heif
        pillow_heif.register_heif_opener()
    pages = []
    try:
        for _name, _stored, source, _mime in files:
            with Image.open(source) as image:
                pages.append(ImageOps.exif_transpose(image).convert("RGB"))
        if not pages:
            raise ValueError("Не выбраны страницы для PDF.")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        pages[0].save(output_path, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    finally:
        for page in pages:
            page.close()


def _merge_correspondence_files(files, output_path):
    """Convert the ordered letter and its appendices to one reviewable PDF."""
    from pypdf import PdfWriter
    from app.services.word_documents import is_word_document, word_preview_path

    writer = PdfWriter()
    temporary_paths = []
    try:
        for index, part in enumerate(files, start=1):
            original_name, _stored_name, source_path, mime_type = part
            source_path = Path(source_path)
            if mime_type == "application/pdf":
                pdf_path = source_path
            elif is_word_document(source_path):
                pdf_path = Path(word_preview_path(source_path))
                temporary_paths.append(pdf_path)
            elif mime_type.startswith("image/"):
                pdf_path = Path(output_path).parent / f"{__import__('uuid').uuid4().hex}_correspondence-part-{index}.pdf"
                _combine_images([part], pdf_path)
                temporary_paths.append(pdf_path)
            else:
                raise ValueError(f"Формат файла «{Path(original_name).name}» не поддерживается для письма с приложениями.")
            writer.append(str(pdf_path))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(output_path).open("wb") as destination:
            writer.write(destination)
    finally:
        writer.close()
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


def _letter_or_404(object_id, letter_id):
    row = Correspondence.objects.filter(
        project_id=object_id, project__archived_at__isnull=True, pk=letter_id,
    ).first()
    if row is None:
        raise Http404
    return row


def _normalize_letter_date(value):
    from datetime import date
    import re
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return ""
    match = re.search(r"((?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:19|20)\d{2})", value)
    if not match:
        return ""
    day, month, year = re.split(r"[./-]", match.group(1))
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


@require_POST
def upload_correspondence(request, object_id):
    project = Project.objects.filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    analyze = request.headers.get("X-Correspondence-Analyze") == "1"
    record_type = request.POST.get("record_type", "correspondence")
    record_type = record_type if record_type in {"correspondence", "approval"} else "correspondence"
    target_tab = "approvals" if record_type == "approval" else "correspondence"
    if _upload_size_exceeded(request):
        message = "Размер каждого файла не должен превышать 15 МиБ."
        if analyze:
            return JsonResponse({"error": message}, status=413)
        messages.error(request, message)
        return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab={target_tab}")
    direction = request.POST.get("direction", "incoming")
    direction = direction if direction in {"incoming", "outgoing"} else "incoming"
    files = [f for f in request.FILES.getlist("files") if f and f.name]
    if not files:
        if analyze:
            return JsonResponse({"error": "Выберите PDF, Word-файл или изображения страниц письма."}, status=400)
        messages.error(request, "Выберите PDF или фотографии страниц письма.")
        return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab={target_tab}")
    saved = []
    uploaded_parts = []
    final_path = None
    try:
        storage = _upload_storage()
        saved = [storage.save_upload(f) for f in files]
        uploaded_parts = list(saved)
        if len(saved) == 1 and not saved[0][3].startswith("image/"):
            prepared = saved[0]
        else:
            final_path = Path(settings.MEDIA_ROOT) / f"{__import__('uuid').uuid4().hex}_correspondence.pdf"
            _merge_correspondence_files(saved, final_path)
            if len(saved) > 1:
                title_base = Path(saved[0][0]).stem[:100] or "Письмо"
                original_name = f"{title_base} + {len(saved) - 1} приложений.pdf"
            else:
                original_name = f"Письмо — {Path(saved[0][0]).name}"
            prepared = (original_name, final_path.name, final_path, "application/pdf")
            saved = [prepared]
    except Exception as exc:
        for _name, _stored, source, _mime in uploaded_parts or saved:
            Path(source).unlink(missing_ok=True)
        if final_path:
            final_path.unlink(missing_ok=True)
        message = str(exc) if isinstance(exc, ValueError) else "Не удалось подготовить письмо и приложения."
        if analyze:
            return JsonResponse({"error": message}, status=422)
        messages.error(request, message)
        return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab={target_tab}")

    original_name, stored_name, path, mime_type = prepared
    raw_text = ""
    values = {"direction": direction, "number": "", "letter_date": "", "sender": "", "recipient": "", "subject": "", "summary": ""}
    from app.services.correspondence_extractor import _extract_correspondence_filename_fields
    filename_values = _extract_correspondence_filename_fields(uploaded_parts[0][0])
    values.update(filename_values)
    status, error = "processed", ""
    ocr_temporary_path = None
    try:
        from app.services.pdf_text import PdfTextExtractor
        from app.services.word_documents import is_word_document, word_preview_path
        # The merged PDF is for viewing. Correspondence fields are always read
        # from the first uploaded part (the letter), never from its appendices.
        _first_name, _first_stored_name, first_source, first_mime_type = uploaded_parts[0]
        first_source = Path(first_source)
        if is_word_document(first_source):
            extraction_path = word_preview_path(first_source)
        elif first_mime_type.startswith("image/"):
            ocr_temporary_path = Path(settings.MEDIA_ROOT) / f"{__import__('uuid').uuid4().hex}_correspondence-letter-ocr.pdf"
            _combine_images([uploaded_parts[0]], ocr_temporary_path)
            extraction_path = ocr_temporary_path
        else:
            extraction_path = first_source
        extractor = PdfTextExtractor(tesseract_lang=settings.TESSERACT_LANG, dpi=settings.OCR_DPI)
        raw_text = extractor.extract(extraction_path, correspondence=True)
        from app.services.correspondence_extractor import _extract_correspondence_fields
        extracted_values = _extract_correspondence_fields(raw_text)
        values.update(extracted_values)
        for key in ("number", "letter_date"):
            if filename_values[key] and not values[key]:
                values[key] = filename_values[key]
        minskenergo_letter = bool(re.search(
            r"минск(?:ие|их|ими)?\s+электрическ\w+\s+сет|минскэнерго",
            raw_text,
            re.IGNORECASE,
        )) and record_type == "approval"
        if minskenergo_letter:
            # These incoming letters show the author's handwritten outgoing
            # number/date above a printed "На № ... от ..." reference. OCR and
            # generic extraction often mistake that reference for this letter's
            # own date, so use a dedicated visual read of the handwritten line.
            values["number"] = ""
            values["letter_date"] = ""
            from app.services.ai_extractor import AiFieldExtractor
            from app.services.field_extractor import RegexFieldExtractor
            handwritten = AiFieldExtractor(RegexFieldExtractor()).extract_minskenergo_correspondence_header(
                extraction_path, raw_text,
            )
            handwritten_number = re.sub(r"^№\s*|\s+", "", handwritten.get("number", ""), flags=re.IGNORECASE)
            if re.fullmatch(r"[A-ZА-ЯЁ0-9]+(?:[./-][A-ZА-ЯЁ0-9]+){0,5}", handwritten_number, re.IGNORECASE):
                values["number"] = handwritten_number
            handwritten_date = _normalize_letter_date(handwritten.get("letter_date", ""))
            if handwritten_date:
                values["letter_date"] = handwritten_date
            if not values["number"] or not values["letter_date"]:
                error = "Не удалось уверенно прочитать рукописные номер и дату письма. Проверьте реквизиты вручную."
        # OCR/AI occasionally appends the letter date (and its timestamp) to
        # the number. Apply this after filename fallbacks too, so every source
        # follows the same rule before the draft is shown in the review modal.
        from app.services.correspondence_extractor import _strip_appended_letter_date
        values["number"] = _strip_appended_letter_date(values["number"], values["letter_date"])
        if not raw_text.strip():
            status, error = "error", "Не удалось распознать текст письма. Заполните реквизиты вручную."
    except Exception as exc:
        status, error = "error", str(exc)[:2000]
        logging.getLogger(__name__).exception("Correspondence OCR or extraction failed")
    finally:
        if ocr_temporary_path:
            ocr_temporary_path.unlink(missing_ok=True)
        # The final merged PDF is the persisted document. Remove its uploaded
        # parts after OCR, including Word preview cache for discarded sources.
        if len(uploaded_parts) > 1 or uploaded_parts[0][3].startswith("image/"):
            from app.services.word_documents import remove_word_preview
            for _name, _stored, source, _mime in uploaded_parts:
                source = Path(source)
                if is_word_document(source):
                    remove_word_preview(source)
                source.unlink(missing_ok=True)
    values["direction"] = direction
    from app.services.organization_names import normalize_organization_forms
    values["sender"] = normalize_organization_forms(values["sender"])
    values["recipient"] = normalize_organization_forms(values["recipient"])
    letter = Correspondence.objects.create(
        project=project, **values, original_filename=original_name, stored_filename=stored_name,
        mime_type=mime_type, raw_text=raw_text, status=status, error=error, record_type=record_type,
    )
    if analyze:
        return JsonResponse({
            "id": letter.id, "preview": reverse("web_correspondence_preview", args=[letter.id]),
            "record_type": record_type, "fields": {
                key: getattr(letter, key) for key in ("direction", "number", "letter_date", "sender", "recipient", "subject", "summary", "valid_until")
            }, "warning": error,
        })
    messages.success(request, "Письмо добавлено. Проверьте распознанные реквизиты.")
    return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab={target_tab}")


@require_POST
def append_correspondence(request, object_id, letter_id):
    _letter_or_404(object_id, letter_id)
    if _upload_size_exceeded(request):
        return JsonResponse({"error": "Размер каждого файла не должен превышать 15 МиБ."}, status=413)
    from app.services.correspondence_attachments import append_correspondence_files
    ocr_warnings = []
    try:
        added, total = append_correspondence_files(object_id, letter_id, request.FILES.getlist("files"), ocr_warnings=ocr_warnings)
    except Http404:
        raise
    except ValueError as exc:
        logging.getLogger(__name__).exception("Could not append correspondence id=%s", letter_id)
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        logging.getLogger(__name__).exception("Could not append correspondence id=%s", letter_id)
        return JsonResponse({"error": "Не удалось добавить документ. Проверьте формат файлов и попробуйте ещё раз."}, status=400)
    return JsonResponse({
        "added_pages": added, "page_count": total,
        "warning": " ".join(ocr_warnings),
        "preview": reverse("web_correspondence_viewer", args=[letter_id]) + f"?page={total-added+1}",
    })


@require_POST
def update_correspondence(request, object_id, letter_id):
    letter = _letter_or_404(object_id, letter_id)
    direction = request.POST.get("direction", letter.direction)
    if direction in {"incoming", "outgoing"}:
        letter.direction = direction
    letter.letter_date = _normalize_letter_date(request.POST.get("letter_date", ""))
    from app.services.correspondence_extractor import _strip_appended_letter_date
    letter.number = _strip_appended_letter_date(
        request.POST.get("number", "").strip()[:100], letter.letter_date
    )
    from app.services.organization_names import normalize_organization_forms
    letter.sender = normalize_organization_forms(request.POST.get("sender", "").strip())[:500]
    letter.recipient = normalize_organization_forms(request.POST.get("recipient", "").strip())[:500]
    letter.subject = request.POST.get("subject", "").strip()[:500]
    letter.summary = request.POST.get("summary", "").strip()[:4000]
    letter.valid_until = request.POST.get("valid_until", "").strip()[:20]
    letter.save(update_fields=["direction", "number", "letter_date", "sender", "recipient", "subject", "summary", "valid_until"])
    if request.headers.get("X-Requested-With") == "fetch":
        return HttpResponse(status=204)
    return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab={ 'approvals' if letter.record_type == 'approval' else 'correspondence'}")


@require_POST
def delete_correspondence(request, object_id, letter_id):
    letter = _letter_or_404(object_id, letter_id)
    stored, record_type = letter.stored_filename, letter.record_type
    with transaction.atomic():
        letter.delete()
    if stored and Path(stored).name == stored and not Document.objects.filter(stored_filename=stored).exists() and not Correspondence.objects.filter(stored_filename=stored).exists():
        path = (Path(settings.MEDIA_ROOT).resolve() / stored).resolve()
        if path.parent == Path(settings.MEDIA_ROOT).resolve():
            path.unlink(missing_ok=True)
    if request.headers.get("X-Requested-With") == "fetch":
        return HttpResponse(status=204)
    return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab={ 'approvals' if record_type == 'approval' else 'correspondence'}")


def _correspondence_file_response(letter_id, *, preview):
    letter = Correspondence.objects.filter(pk=letter_id).first()
    if letter is None or not letter.stored_filename:
        raise Http404
    root = Path(settings.MEDIA_ROOT).resolve()
    name = letter.stored_filename
    candidate = Path(name)
    source = (root / candidate).resolve()
    if candidate.is_absolute() or candidate.name != name or source.parent != root or not source.is_file():
        raise Http404
    from app.services.word_documents import is_word_document, word_preview_path
    if preview and is_word_document(source):
        try:
            source = word_preview_path(source)
        except (OSError, RuntimeError, ValueError):
            return HttpResponse("Preview unavailable", status=415)
    content_type = "application/pdf" if preview and source.suffix.lower() == ".pdf" else letter.mime_type or "application/octet-stream"
    response = FileResponse(source.open("rb"), content_type=content_type)
    filename = Path(letter.original_filename or name).name
    if preview and source.suffix.lower() == ".pdf":
        filename = Path(filename).stem + ".pdf"
    response["Content-Disposition"] = content_disposition_header(not preview, filename)
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def correspondence_file(request, letter_id):
    return _correspondence_file_response(letter_id, preview=False)


@require_GET
def correspondence_preview(request, letter_id):
    return _correspondence_file_response(letter_id, preview=True)


def _project_rows(project_id):
    project = Project.objects.filter(pk=project_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    obj = {field: getattr(project, field) for field in (
        "id", "contract_number", "contract_date", "stage", "name", "customer", "template_id", "chief_engineer_id", "archived_at", "created_at",
    )}
    obj["chief_engineer_name"] = project.chief_engineer.get_full_name() if project.chief_engineer_id else ""
    documents = list(
        Document.objects.filter(project=project, archived_at__isnull=True)
        .order_by("-created_at", "-id").values()
    )
    checklist = list(ProjectChecklistItem.objects.filter(project=project).order_by("sort_order", "id").values())
    documents_by_id = {
        row["id"]: row for row in Document.objects.filter(project=project).values(
            "id", "issuer", "document_number", "issue_date", "note",
        )
    }
    for item in checklist:
        linked = documents_by_id.get(item["document_id"])
        item.update({
            "linked_document_issuer": linked.get("issuer") if linked else None,
            "linked_document_number": linked.get("document_number") if linked else None,
            "linked_document_issue_date": linked.get("issue_date") if linked else None,
            "linked_document_note": linked.get("note") if linked else None,
        })
    return project, obj, documents, checklist


@require_GET
def project_acts(request, object_id):
    project, obj, documents, checklist = _project_rows(object_id)
    active_doc_ids = set(Document.objects.filter(project=project, archived_at__isnull=True).values_list("id", flat=True))
    linked_ids = set()
    for link in ChecklistDocument.objects.filter(checklist_item__project=project).values_list("checklist_item_id", "document_id"):
        if link[1] in active_doc_ids:
            linked_ids.add(link[0])
    archived_item_ids = set(ProjectChecklistItem.objects.filter(
        project=project, document__archived_at__isnull=False,
    ).values_list("id", flat=True))
    received_count = sum(bool(
        item["id"] in archived_item_ids
        or (allows_multiple_documents(item["title"]) and item["id"] in linked_ids)
        or (item["validity_years"] and item["issue_date"])
        or item["document_id"] in active_doc_ids
    ) for item in checklist)
    from migration_portal.models import HandoverAct
    acts = list(HandoverAct.objects.filter(project=project).order_by("-created_at", "-id").values())
    correspondence_count = Correspondence.objects.filter(
        project=project, record_type="correspondence",
    ).count()
    return render(request, "project_acts.html", {
        "project": obj, "checklist": checklist, "received_count": received_count, "acts": acts,
        "correspondence_count": correspondence_count,
        "mail_configured": bool(os.getenv("MAIL_USERNAME") and os.getenv("MAIL_APP_PASSWORD")),
    }, using="jinja2")


@require_GET
def download_handover_act(request, object_id, act_id):
    project, _, _, _ = _project_rows(object_id)
    from migration_portal.models import HandoverAct
    act = HandoverAct.objects.filter(project=project, pk=act_id).first()
    if act is None:
        raise Http404
    root = Path(settings.ACTS_DIR).resolve()
    project_root = (root / str(object_id)).resolve()
    name = act.stored_filename
    candidate = Path(name)
    path = (project_root / candidate).resolve()
    if candidate.is_absolute() or candidate.name != name or project_root.parent != root or path.parent != project_root or not path.is_file():
        raise Http404
    response = FileResponse(path.open("rb"), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    response["Content-Disposition"] = content_disposition_header(True, name)
    return response


@require_POST
def delete_handover_act(request, object_id, act_id):
    project, _, _, _ = _project_rows(object_id)
    from migration_portal.models import HandoverAct
    act = HandoverAct.objects.filter(project=project, pk=act_id).first()
    if act is None:
        raise Http404
    root = Path(settings.ACTS_DIR).resolve()
    project_root = (root / str(object_id)).resolve()
    name = act.stored_filename
    candidate = Path(name)
    path = (project_root / candidate).resolve()
    if candidate.is_absolute() or candidate.name != name or project_root.parent != root or path.parent != project_root:
        raise Http404
    path.unlink(missing_ok=True)
    act.delete()
    messages.success(request, "Акт удалён вместе с файлом.")
    return redirect("web_project_acts", object_id=object_id)


@require_GET
def project_document_form(request, object_id, kind):
    project, obj, documents, checklist = _project_rows(object_id)
    if project.archived_at:
        raise Http404
    from app.services.document_generator import DOCUMENT_TYPES as GENERATABLE_TYPES, REQUEST_TEXT_SPEC, generate_missing_data_request_text
    if kind != "request" and kind not in GENERATABLE_TYPES:
        raise Http404
    spec = REQUEST_TEXT_SPEC if kind == "request" else GENERATABLE_TYPES[kind]
    context = {
        "project": obj, "kind": kind, "spec": spec, "checklist": checklist,
        "documents": documents, "in_modal": request.GET.get("partial") == "1",
    }
    if kind == "request":
        context["request_text"] = generate_missing_data_request_text(obj, checklist)
    template = "document_generation_fragment.html" if request.GET.get("partial") == "1" else "document_generation.html"
    return render(request, template, context, using="jinja2")


def download_project_document(request, object_id, kind):
    if request.method == "GET":
        return project_document_form(request, object_id, kind)
    if request.method != "POST":
        return HttpResponse(status=405)
    project, obj, documents, checklist = _project_rows(object_id)
    from app.services.document_generator import DOCUMENT_TYPES as GENERATABLE_TYPES, generate_project_document
    if kind not in GENERATABLE_TYPES:
        raise Http404
    if project.archived_at:
        raise Http404
    if kind == "handover" and not documents:
        messages.error(request, "Добавьте документы проекта перед созданием акта приёма-передачи.")
        return redirect("web_project_document_form", object_id=object_id, kind=kind)
    checklist_document_titles = {}
    if kind == "handover":
        items = {row["id"]: row for row in checklist}
        for link in ChecklistDocument.objects.filter(checklist_item__project=project).select_related("checklist_item", "document"):
            item = items.get(link.checklist_item_id)
            if item and uses_customer_letter_subject(item["title"]):
                checklist_document_titles[link.document_id] = item["title"]
    elif kind == "inventory":
        documents_by_id = {row["id"]: row for row in documents}
        linked_ids_by_item = {}
        for item_id, document_id in ChecklistDocument.objects.filter(
            checklist_item__project=project,
            document__project=project,
            document__archived_at__isnull=True,
        ).order_by("checklist_item_id", "sort_order", "created_at", "document_id").values_list(
            "checklist_item_id", "document_id",
        ):
            linked_ids_by_item.setdefault(item_id, []).append(document_id)
        ordered_ids = []
        for item in sorted(checklist, key=lambda row: (
            0 if row["status"] == "received" else 1, row["sort_order"], row["id"],
        )):
            item_document_ids = linked_ids_by_item.get(item["id"], [])
            if item["document_id"] in documents_by_id and item["document_id"] not in item_document_ids:
                item_document_ids = [item["document_id"], *item_document_ids]
            ordered_ids.extend(item_document_ids)
        ordered_ids = list(dict.fromkeys(document_id for document_id in ordered_ids if document_id in documents_by_id))
        seen_ids = set(ordered_ids)
        ordered_ids.extend(row["id"] for row in documents if row["id"] not in seen_ids)
        documents = [documents_by_id[document_id] for document_id in ordered_ids]
    content = generate_project_document(kind, obj, documents, checklist, checklist_document_titles=checklist_document_titles)
    from app.services.filenames import secure_stem
    slug = secure_stem(project.name, fallback=f"project-{object_id}")
    filename = f"{GENERATABLE_TYPES[kind]['filename']}_{slug}.docx"
    if kind == "handover":
        root = Path(settings.ACTS_DIR).resolve()
        project_dir = (root / str(object_id)).resolve()
        project_dir.mkdir(parents=True, exist_ok=True)
        import re
        prefix = f"Акт_{re.sub(r'[^0-9A-Za-zА-Яа-яЁё.-]+', '-', project.contract_number or '').strip('-._') or f'project-{object_id}'}_"
        numbers = []
        for candidate in project_dir.glob(f"{prefix}*.docx"):
            match = re.fullmatch(re.escape(prefix) + r"(\d+)\.docx", candidate.name)
            if match:
                numbers.append(int(match.group(1)))
        from migration_portal.models import HandoverAct
        manual_count = HandoverAct.objects.filter(project=project, source="manual").count()
        sequence = max(max(numbers, default=0), manual_count) + 1
        filename = f"{prefix}{sequence}.docx"
        target = project_dir / filename
        while target.exists():
            sequence += 1
            filename = f"{prefix}{sequence}.docx"
            target = project_dir / filename
        target.write_bytes(content)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        HandoverAct.objects.create(project=project, period_start=now, period_end=now, stored_filename=filename, source="manual")
        response = FileResponse(target.open("rb"), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    else:
        from io import BytesIO
        response = FileResponse(BytesIO(content), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    response["Content-Disposition"] = content_disposition_header(True, filename)
    return response


def project_document_dispatch(request, object_id, kind):
    if request.method == "GET":
        return project_document_form(request, object_id, kind)
    return download_project_document(request, object_id, kind)


def _archive_name(value, fallback):
    value = re.sub(r'[<>:\\|?*/"\x00-\x1f]', " ", value or "")
    value = re.sub(r"\s+", "_", value.strip(" ._"))
    value = re.sub(r"_+", "_", value)[:72].rstrip(" ._")
    return value or fallback


@require_GET
def export_object(request, object_id):
    project, _obj, documents, _checklist = _project_rows(object_id)
    letters = list(Correspondence.objects.filter(project=project, record_type="correspondence").order_by("-letter_date", "-id").values())
    approvals = list(Correspondence.objects.filter(project=project, record_type="approval").order_by("-letter_date", "-id").values())
    from migration_portal.models import HandoverAct
    acts = list(HandoverAct.objects.filter(project=project).order_by("-created_at", "-id").values())
    archive = BytesIO()
    upload_root = Path(settings.MEDIA_ROOT).resolve()
    acts_root = Path(settings.ACTS_DIR).resolve()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        def add_file(record, directory, folder, label, title, date_value=""):
            stored = record.get("stored_filename") or ""
            candidate = Path(stored)
            root = directory.resolve()
            path = (root / candidate).resolve() if stored else None
            if path is None or candidate.is_absolute() or candidate.name != stored or path.parent != root or not path.is_file():
                return
            safe_title = _archive_name(title, label)
            if folder == "01_ИРД":
                extension = Path(record.get("original_filename") or path.name).suffix or path.suffix
                member = f"{folder}/{record['id']:03d}_{safe_title}{extension}"
            elif folder == "04_Акты_приёма-передачи":
                period = _archive_name(date_value or "акт", "акт")
                member = f"{folder}/{record['id']:03d}_Акт_за_период_до_{period}{path.suffix}"
            else:
                date_part = _archive_name(date_value or "без_даты", "без_даты")
                member = f"{folder}/{record['id']:03d}_{date_part}_{safe_title}{path.suffix}"
            content = path.read_bytes()
            bundle.writestr(member, content)

        for row in documents:
            add_file(row, upload_root, "01_ИРД", "Документ",
                     row.get("working_title") or row.get("official_title") or row.get("original_filename") or "Документ")
        for row in letters:
            add_file(row, upload_root, "02_Переписка", "Письмо",
                     row.get("subject") or row.get("sender") or "Письмо", row.get("letter_date"))
        for row in approvals:
            add_file(row, upload_root, "03_Согласования", "Согласование",
                     row.get("subject") or row.get("sender") or "Согласование", row.get("letter_date"))
        for row in acts:
            add_file(row, acts_root / str(object_id), "04_Акты_приёма-передачи", "Акт",
                     "Акт", row.get("period_end") or (row.get("created_at") or "")[:10])
    filename = _archive_name(project.name, f"Проект_{object_id}")
    response = FileResponse(BytesIO(archive.getvalue()), content_type="application/zip")
    response["Content-Disposition"] = content_disposition_header(True, f"Архив_проекта_{filename}.zip")
    return response


def home(request):
    warning_days = int(os.getenv("EXPIRY_WARNING_DAYS", "30"))
    all_visible_projects = visible_projects(request.user)
    try:
        is_chief_engineer = request.user.profile.is_gip
    except Exception:
        is_chief_engineer = False
    gip_filter_active = is_chief_engineer and request.GET.get("scope") != "all"
    project_scope = all_visible_projects.filter(chief_engineer=request.user) if gip_filter_active else all_visible_projects
    objects = list(
        project_scope.filter(archived_at__isnull=True)
        .select_related("chief_engineer")
        .annotate(
            documents_count=Count("document", filter=Q(document__archived_at__isnull=True), distinct=True),
            last_document_at=Max("document__created_at"),
        )
        .order_by("-created_at", "-id")
    )
    archived_objects = project_scope.filter(archived_at__isnull=False).order_by("-archived_at", "-id")
    customers = []
    seen = set()
    customer_rows = {}
    for customer in all_visible_projects.exclude(customer="").values_list("customer", flat=True):
        value = customer.strip()
        key = value.casefold()
        if value:
            customer_rows[key] = min(value, customer_rows.get(key, value))
    customers = sorted(customer_rows.values(), key=str.casefold)
    templates = ChecklistTemplate.objects.order_by("name")
    alerts = []
    for document in Document.objects.filter(
        archived_at__isnull=True, project__archived_at__isnull=True, project__in=project_scope,
    ).order_by("-created_at", "-id"):
        row = {
            "id": document.id,
            "object_id": document.project_id,
            "no_expiration": document.no_expiration,
            "valid_until": document.valid_until,
            "working_title": document.working_title,
            "original_filename": document.original_filename,
            "issuer": document.issuer,
        }
        state = expiry_state(row, warning_days)
        if state["key"] in {"expired", "expiring"}:
            row["alert_title"] = format_document_title(
                document.working_title or document.official_title or document.original_filename,
                document.issuer,
            )
            alerts.append({"document": row, "expiry": state})
    context = {
        "objects": objects,
        "archived_objects": archived_objects,
        "customers": customers,
        "templates": templates,
        "gips": User.objects.filter(is_active=True, profile__is_gip=True).order_by("last_name", "first_name", "email"),
        "is_chief_engineer": is_chief_engineer,
        "gip_filter_active": gip_filter_active,
        "alerts": alerts,
        "warning_days": warning_days,
        "error": "",
    }
    return render(request, "index.html", context, using="jinja2")


@require_POST
def create_object(request):
    allowed_stages = {"Предпроектная документация", "Строительный проект", "Архитектурный проект"}
    contract_number = request.POST.get("contract_number", "").strip()
    contract_date = request.POST.get("contract_date", "").strip()
    stage = request.POST.get("stage", "").strip()
    if stage not in allowed_stages:
        stage = ""
    name = request.POST.get("name", "").strip()
    customer = request.POST.get("customer", "").strip()
    chief_engineer_id = request.POST.get("chief_engineer_id", "").strip()
    chief_engineer = None
    if not chief_engineer_id:
        messages.error(request, "Выберите главного инженера проекта.")
        return redirect("web_index")
    chief_engineer = User.objects.filter(pk=chief_engineer_id, is_active=True, profile__is_gip=True).first()
    if chief_engineer is None:
        messages.error(request, "Выберите активного пользователя с признаком «Главный инженер проекта».")
        return redirect("web_index")
    self_assigned_gip_project = chief_engineer.pk == request.user.pk
    if not request.user.is_superuser:
        allowed_customer = visible_customers(request.user).filter(name__iexact=customer).first()
        if allowed_customer is not None:
            customer = allowed_customer.name
        elif not self_assigned_gip_project:
            messages.error(request, "Выберите заказчика, назначенного вашей учётной записи.")
            return redirect("web_index")
    try:
        template_id = int(request.POST.get("template_id", "")) or None
    except (TypeError, ValueError):
        template_id = None
    if not contract_number or not name:
        messages.error(request, "Заполните номер договора и наименование проекта.")
        return redirect("web_index")
    with transaction.atomic():
        project = Project.objects.create(
            contract_number=contract_number,
            contract_date=contract_date,
            stage=stage,
            name=name,
            customer=customer,
            chief_engineer=chief_engineer,
            template_id=template_id if ChecklistTemplate.objects.filter(pk=template_id).exists() else None,
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        if project.template_id:
            items = ChecklistTemplateItem.objects.filter(template_id=project.template_id).order_by("sort_order", "id")
            ProjectChecklistItem.objects.bulk_create([
                ProjectChecklistItem(
                    project_id=project.id,
                    source_template_item_id=item.id,
                    title=item.title,
                    description=item.description,
                    sort_order=item.sort_order,
                    validity_years=item.validity_years,
                )
                for item in items
            ])
    if self_assigned_gip_project and not request.user.is_superuser:
        return redirect("web_index")
    return redirect("web_object_workspace", object_id=project.id)


@require_POST
def update_object(request, object_id):
    project = Project.objects.filter(pk=object_id).first()
    if project is None:
        raise Http404
    contract_number = request.POST.get("contract_number", "").strip()
    contract_date = request.POST.get("contract_date", "").strip()
    allowed_stages = {"Предпроектная документация", "Строительный проект", "Архитектурный проект"}
    stage = request.POST.get("stage", "").strip()
    if stage not in allowed_stages:
        stage = ""
    name = request.POST.get("name", "").strip()
    customer = request.POST.get("customer", "").strip()
    chief_engineer = project.chief_engineer
    if "chief_engineer_id" in request.POST:
        chief_engineer_id = request.POST.get("chief_engineer_id", "").strip()
        chief_engineer = None
        if chief_engineer_id:
            chief_engineer = User.objects.filter(pk=chief_engineer_id, is_active=True, profile__is_gip=True).first()
            if chief_engineer is None:
                messages.error(request, "Выберите активного пользователя с признаком «Главный инженер проекта».")
                return redirect("web_index")
    if not request.user.is_superuser and customer.casefold() != project.customer.casefold():
        allowed_customer = visible_customers(request.user).filter(name__iexact=customer).first()
        if allowed_customer is None:
            messages.error(request, "Вы не можете назначить этому проекту другого заказчика.")
            return redirect("web_index")
        customer = allowed_customer.name
    if not contract_number or not name:
        messages.error(request, "Заполните номер договора и наименование проекта.")
        return redirect("web_index")
    try:
        template_id = int(request.POST.get("template_id", "")) or None
    except (TypeError, ValueError):
        template_id = None
    selected_template = ChecklistTemplate.objects.filter(pk=template_id).first() if template_id else None
    with transaction.atomic():
        project.contract_number = contract_number
        project.contract_date = contract_date
        project.stage = stage
        project.name = name
        project.customer = customer
        project.template = selected_template
        project.chief_engineer = chief_engineer
        project.save(update_fields=["contract_number", "contract_date", "stage", "name", "customer", "template", "chief_engineer"])
        Document.objects.filter(project_id=project.id).update(contract_number=contract_number, object_name=name)
        if selected_template:
            existing_items = list(ProjectChecklistItem.objects.filter(project=project).values_list("source_template_item_id", "title"))
            existing_sources = {source_id for source_id, _title in existing_items if source_id}
            existing_titles = {normalize(title) for _source_id, title in existing_items}
            new_items = []
            for template_item in ChecklistTemplateItem.objects.filter(template=selected_template).order_by("sort_order", "id"):
                if template_item.id in existing_sources or normalize(template_item.title) in existing_titles:
                    continue
                new_items.append(ProjectChecklistItem(
                    project=project, source_template_item=template_item, title=template_item.title,
                    description=template_item.description, sort_order=template_item.sort_order,
                    validity_years=template_item.validity_years,
                ))
                existing_titles.add(normalize(template_item.title))
            if new_items:
                next_order = (ProjectChecklistItem.objects.filter(project=project).aggregate(value=Max("sort_order"))["value"] or -1) + 1
                for offset, item in enumerate(new_items):
                    item.sort_order += next_order + offset
                ProjectChecklistItem.objects.bulk_create(new_items)
                from app.services.project_answer_cache import invalidate_project_answer_cache
                invalidate_project_answer_cache(project.id)
    return redirect("web_index")


@require_POST
def archive_object(request, object_id):
    updated = Project.objects.filter(pk=object_id, archived_at__isnull=True).update(
        archived_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    )
    if not updated:
        raise Http404
    messages.success(request, "Проект перемещён в архив. Документы и файлы сохранены.")
    return redirect("web_index")


@require_POST
def restore_object(request, object_id):
    updated = Project.objects.filter(pk=object_id, archived_at__isnull=False).update(archived_at=None)
    if not updated:
        raise Http404
    messages.success(request, "Проект восстановлен.")
    return redirect("web_index")


@require_POST
def delete_object(request, object_id):
    project = Project.objects.filter(pk=object_id).first()
    if project is None:
        raise Http404
    upload_root = Path(settings.MEDIA_ROOT).resolve()
    acts_root = Path(getattr(settings, "ACTS_DIR", Path(settings.MEDIA_ROOT).parent / "acts")).resolve()
    upload_names = list(Document.objects.filter(project_id=object_id).values_list("stored_filename", flat=True))
    upload_names.extend(project.correspondence_set.values_list("stored_filename", flat=True))
    act_names = list(project.handoveract_set.values_list("stored_filename", flat=True))
    # The legacy schema deliberately keeps document rows when project_id is
    # nulled by a direct FK deletion, so match the former repository's explicit
    # project document cleanup before removing the project.
    with transaction.atomic():
        Document.objects.filter(project_id=object_id).delete()
        project.delete()
    failed = False
    for stored_name in upload_names:
        if not stored_name:
            continue
        candidate = Path(stored_name or "")
        target = (upload_root / candidate).resolve()
        if candidate.is_absolute() or candidate.name != stored_name or target.parent != upload_root:
            failed = True
            continue
        try:
            target.unlink(missing_ok=True)
        except OSError:
            failed = True
    project_acts_dir = (acts_root / str(object_id)).resolve()
    if project_acts_dir.parent != acts_root:
        failed = True
    for stored_name in act_names:
        if not stored_name:
            continue
        candidate = Path(stored_name or "")
        target = (project_acts_dir / candidate).resolve()
        if candidate.is_absolute() or candidate.name != stored_name or target.parent != project_acts_dir:
            failed = True
            continue
        try:
            target.unlink(missing_ok=True)
        except OSError:
            failed = True
    try:
        project_acts_dir.rmdir()
    except OSError:
        pass
    if failed:
        messages.error(request, "Проект удалён. Некоторые исходные файлы не удалось очистить автоматически.")
    else:
        messages.success(request, "Проект, документы, чек-лист и исходные файлы удалены.")
    return redirect("web_index")


@require_GET
def manage_templates(request):
    templates = ChecklistTemplate.objects.order_by("name")
    template_rows = [{
        "template": template,
        "items": list(ChecklistTemplateItem.objects.filter(template_id=template.id).order_by("sort_order", "id")),
    } for template in templates]
    return render(request, "templates.html", {"templates": template_rows}, using="jinja2")


def templates_root(request):
    if request.method == "GET":
        return manage_templates(request)
    if request.method == "POST":
        return create_template(request)
    return JsonResponse({"detail": "Method not allowed"}, status=405)


@require_POST
def create_template(request):
    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    if not name:
        messages.error(request, "Укажите название шаблона.")
        return redirect("web_manage_templates")
    try:
        template = ChecklistTemplate.objects.create(name=name, description=description)
    except IntegrityError:
        messages.error(request, "Шаблон с таким названием уже существует.")
        return redirect("web_manage_templates")
    return HttpResponseRedirect(f"{reverse('web_manage_templates')}#template-{template.id}")


@require_POST
def update_template(request, template_id):
    template = ChecklistTemplate.objects.filter(pk=template_id).first()
    if template is None:
        raise Http404
    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    if not name:
        messages.error(request, "Укажите название шаблона.")
        return redirect("web_manage_templates")
    try:
        template.name = name
        template.description = description
        template.save(update_fields=["name", "description", "updated_at"])
    except IntegrityError:
        if request.headers.get("X-Requested-With") == "fetch":
            return JsonResponse({"ok": False, "error": "Шаблон с таким названием уже существует."}, status=409)
        messages.error(request, "Шаблон с таким названием уже существует.")
    if request.headers.get("X-Requested-With") == "fetch":
        if template.name != name:
            return JsonResponse({"ok": False, "error": "Шаблон с таким названием уже существует."}, status=409)
        return HttpResponse(status=204)
    return HttpResponseRedirect(f"{reverse('web_manage_templates')}#template-{template.id}")


@require_POST
def copy_template(request, template_id):
    source = ChecklistTemplate.objects.filter(pk=template_id).first()
    if source is None:
        raise Http404
    name = request.POST.get("name", "").strip() or f"{source.name} — копия"
    try:
        with transaction.atomic():
            copied = ChecklistTemplate.objects.create(name=name, description=source.description)
            ChecklistTemplateItem.objects.bulk_create([
                ChecklistTemplateItem(
                    template_id=copied.id,
                    title=item.title,
                    description=item.description,
                    sort_order=item.sort_order,
                    validity_years=item.validity_years,
                )
                for item in ChecklistTemplateItem.objects.filter(template_id=source.id).order_by("sort_order", "id")
            ])
    except Exception:
        messages.error(request, "Не удалось скопировать шаблон. Проверьте его название.")
        return HttpResponseRedirect(f"{reverse('web_manage_templates')}#template-{source.id}")
    return HttpResponseRedirect(f"{reverse('web_manage_templates')}#template-{copied.id}")


@require_POST
def delete_template(request, template_id):
    template = ChecklistTemplate.objects.filter(pk=template_id).first()
    if template is None:
        raise Http404
    with transaction.atomic():
        Project.objects.filter(template=template).update(template=None)
        # The legacy SQLite FK intentionally leaves project snapshots intact
        # when a template is deleted, even though Django owns the PostgreSQL FK.
        ChecklistTemplateItem.objects.filter(template=template).delete()
        template.delete()
    return redirect("web_manage_templates")


@require_POST
def add_template_item(request, template_id):
    template = ChecklistTemplate.objects.filter(pk=template_id).first()
    title = request.POST.get("title", "").strip()
    if template is None:
        raise Http404
    if title:
        next_order = (ChecklistTemplateItem.objects.filter(template_id=template.id).aggregate(value=Max("sort_order"))["value"] or -1) + 1
        ChecklistTemplateItem.objects.create(
            template_id=template.id,
            title=title,
            description=request.POST.get("description", "").strip(),
            sort_order=next_order,
            validity_years=2 if title == "Инженерно-геодезические изыскания" else 0,
        )
    return HttpResponseRedirect(f"{reverse('web_manage_templates')}#template-{template.id}")


@require_POST
def update_template_item(request, item_id):
    item = ChecklistTemplateItem.objects.filter(pk=item_id).first()
    if item is None:
        raise Http404
    item.title = request.POST.get("title", "").strip()
    item.description = request.POST.get("description", "").strip()
    if not item.title:
        return JsonResponse({"ok": False, "error": "Укажите название пункта."}, status=400)
    item.save(update_fields=["title", "description"])
    if request.headers.get("X-Requested-With") == "fetch":
        return HttpResponse(status=204)
    template_id = request.POST.get("template_id", "").strip()
    suffix = f"?edit={template_id}#template-{template_id}" if template_id.isdigit() else ""
    return HttpResponseRedirect(f"{reverse('web_manage_templates')}{suffix}")


@require_POST
def delete_template_item(request, item_id):
    item = ChecklistTemplateItem.objects.filter(pk=item_id).first()
    if item is None:
        raise Http404
    template_id = item.template_id
    item.delete()
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"ok": True, "template_id": template_id})
    return HttpResponseRedirect(f"{reverse('web_manage_templates')}#template-{template_id}")


def _checklist_item_or_404(project_id, item_id):
    item = ProjectChecklistItem.objects.filter(project_id=project_id, pk=item_id).first()
    if item is None:
        raise Http404
    return item


@require_POST
def update_checklist_item(request, object_id, item_id):
    item = _checklist_item_or_404(object_id, item_id)
    note = request.POST.get("note") if "note" in request.POST else None
    if item.validity_years > 0:
        issue_date = iso_date(request.POST.get("issue_date", ""))
        item.issue_date = issue_date
        item.valid_until = add_years(issue_date, item.validity_years)
        item.status = "received" if issue_date or item.document_id else "missing"
        if note is not None:
            item.note = note[:2000]
        item.save(update_fields=["issue_date", "valid_until", "status", "note", "updated_at"])
    else:
        raw_id = request.POST.get("document_id")
        try:
            document_id = int(raw_id) if raw_id else None
        except (TypeError, ValueError):
            document_id = None
        document = Document.objects.filter(pk=document_id, project_id=object_id, archived_at__isnull=True).first() if document_id else None
        if item.validity_years > 0 and item.title != "Инженерно-геодезические изыскания":
            document = None
        with transaction.atomic():
            if allows_multiple_documents(item.title):
                if document:
                    order = ChecklistDocument.objects.filter(checklist_item=item).aggregate(value=Max("sort_order"))["value"]
                    ChecklistDocument.objects.get_or_create(
                        checklist_item=item, document=document,
                        defaults={"sort_order": (order if order is not None else -1) + 1},
                    )
                primary = item.document if item.document_id and Document.objects.filter(pk=item.document_id, archived_at__isnull=True).exists() else document
            else:
                ChecklistDocument.objects.filter(checklist_item=item).delete()
                if document:
                    ChecklistDocument.objects.create(checklist_item=item, document=document, sort_order=0)
                primary = document
            item.status = "received" if document else "missing"
            item.document = primary if document else None
            if note is not None:
                item.note = note[:2000]
            item.save(update_fields=["status", "document", "note", "updated_at"])
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({}, status=204)
    return redirect(f"{reverse('web_object_workspace', args=[object_id])}#checklist")


@require_POST
def update_checklist_replacement(request, object_id, item_id):
    item = _checklist_item_or_404(object_id, item_id)
    item.replacement_required = request.POST.get("required") == "1"
    item.save(update_fields=["replacement_required", "updated_at"])
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({}, status=204)
    return redirect(f"{reverse('web_object_workspace', args=[object_id])}#checklist")


@require_POST
def add_checklist_item(request, object_id):
    project = Project.objects.filter(pk=object_id, archived_at__isnull=True).first()
    if project is None:
        raise Http404
    mode = request.POST.get("add_mode", "custom")
    if mode == "template":
        try:
            template_id = int(request.POST.get("template_id", ""))
        except (TypeError, ValueError):
            template_id = None
        template = ChecklistTemplate.objects.filter(pk=template_id).first()
        if template:
            template_items = list(ChecklistTemplateItem.objects.filter(template=template).order_by("sort_order", "id"))
            existing_rows = list(ProjectChecklistItem.objects.filter(project=project).values_list("source_template_item_id", "title"))
            existing_sources = {source_id for source_id, _title in existing_rows if source_id is not None}
            existing_titles = {normalize(title) for _source_id, title in existing_rows}
            to_add = [item for item in template_items if item.id not in existing_sources and normalize(item.title) not in existing_titles]
            if to_add:
                next_order = (ProjectChecklistItem.objects.filter(project=project).aggregate(value=Max("sort_order"))["value"] or -1) + 1
                ProjectChecklistItem.objects.bulk_create([
                    ProjectChecklistItem(
                        project=project, source_template_item=item, title=item.title,
                        description=item.description, sort_order=next_order + index,
                        validity_years=item.validity_years,
                    )
                    for index, item in enumerate(to_add)
                ])
                from app.services.project_answer_cache import invalidate_project_answer_cache
                invalidate_project_answer_cache(project.id)
                messages.success(request, f"Из шаблона «{template.name}» добавлено пунктов: {len(to_add)}.")
            else:
                messages.info(request, f"Все пункты шаблона «{template.name}» уже есть в проекте.")
        else:
            messages.error(request, "Выберите существующий шаблон.")
    elif mode == "custom":
        title = request.POST.get("title", "").strip()[:300]
        normalized = normalize(title)
        existing = ProjectChecklistItem.objects.filter(project=project).values_list("title", flat=True)
        if not title:
            messages.error(request, "Укажите название документа.")
        elif any(normalize(value) == normalized for value in existing):
            messages.error(request, "Документ с таким названием уже есть в списке.")
        else:
            next_order = (ProjectChecklistItem.objects.filter(project=project).aggregate(value=Max("sort_order"))["value"] or -1) + 1
            ProjectChecklistItem.objects.create(project=project, title=title, note=request.POST.get("note", "").strip()[:2000], sort_order=next_order)
            messages.success(request, "Документ добавлен в комплектность проекта.")
    else:
        return JsonResponse({"detail": "Invalid add mode"}, status=400)
    return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab=checklist")


@require_POST
def delete_checklist_item(request, object_id, item_id):
    item = _checklist_item_or_404(object_id, item_id)
    item.delete()
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({}, status=204)
    messages.success(request, "Пункт убран из списка проекта. Загруженные документы сохранены в архиве.")
    return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab=checklist")


@require_POST
def update_checklist_properties(request, object_id, item_id):
    item = _checklist_item_or_404(object_id, item_id)
    title = request.POST.get("title", "").strip()[:300]
    description = request.POST.get("description", "").strip()[:500]
    note = request.POST.get("note", "").strip()[:2000]
    if not title:
        return JsonResponse({"ok": False, "error": "Укажите название пункта."}, status=400)
    if ProjectChecklistItem.objects.filter(project_id=object_id).exclude(pk=item.id).exists():
        for other in ProjectChecklistItem.objects.filter(project_id=object_id).exclude(pk=item.id).only("title"):
            if normalize(other.title) == normalize(title):
                return JsonResponse({"ok": False, "error": "Пункт с таким названием уже есть."}, status=409)
    try:
        years = max(0, min(int(request.POST.get("validity_years", "0")), 10))
    except (TypeError, ValueError):
        years = 0
    requested_issue_date = iso_date(request.POST.get("issue_date", "").strip())
    issue_date = requested_issue_date or item.issue_date
    if not requested_issue_date and item.validity_years == 0:
        issue_date = ""
    item.title, item.description, item.note = title, description, note
    item.issue_date, item.validity_years = issue_date, years
    item.valid_until = add_years(issue_date, years) if years else ""
    item.save(update_fields=["title", "description", "note", "issue_date", "validity_years", "valid_until", "updated_at"])
    payload = {"title": title, "description": description, "note": note, "issue_date": issue_date, "validity_years": years, "valid_until": item.valid_until}
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse(payload)
    return redirect(f"{reverse('web_object_workspace', args=[object_id])}?tab=checklist#checklist")
