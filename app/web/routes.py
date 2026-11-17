import hashlib
import io
import json
import logging
import re
import zipfile
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from flask import Blueprint, abort, current_app, flash, jsonify, make_response, redirect, render_template, request, send_file, send_from_directory, url_for
from werkzeug.utils import secure_filename

from app.database import get_db
from app.models import ParsedDocument
from app.repositories import ChecklistRepository, CorrespondenceRepository, DocumentRepository, HandoverActRepository, ObjectRepository
from app.services.expiry import attach_expiry, expiry_state, iso_date
from app.services.checklist_document_fields import project_assignment_fields
from app.services.field_extractor import RegexFieldExtractor
from app.services.organization_names import normalize_organization_forms
from app.services.pipeline import DocumentProcessingPipeline
from app.services.document_generator import DOCUMENT_TYPES, generate_project_document
from app.services.pdf_text import PdfTextExtractor
from app.services.storage import StorageService
from app.services.word_documents import WORD_MIME_TYPES, is_word_document, remove_word_preview, word_preview_path


def _archive_name(value: str, fallback: str) -> str:
    """Make a short, readable Unicode path component for a project ZIP."""
    value = re.sub(r"[<>:\\|?*/\"\x00-\x1f]", " ", value or "")
    value = re.sub(r"\s+", "_", value.strip(" ._"))
    value = re.sub(r"_+", "_", value)[:72].rstrip(" ._")
    return value or fallback


bp = Blueprint("web", __name__)
logger = logging.getLogger(__name__)


@bp.get("/")
def index():
    warning_days = current_app.config["EXPIRY_WARNING_DAYS"]
    object_repository = ObjectRepository()
    documents = DocumentRepository().list_all()
    alerts = [
        {"document": document, "expiry": state}
        for document in documents
        if (state := expiry_state(document, warning_days))["key"] in {"expired", "expiring"}
    ]
    return render_template(
        "index.html",
        objects=object_repository.list_all(),
        archived_objects=object_repository.list_archived(),
        customers=object_repository.list_customers(),
        templates=ChecklistRepository().list_templates(),
        alerts=alerts,
        warning_days=warning_days,
    )


@bp.post("/objects")
def create_object():
    contract_number = request.form.get("contract_number", "").strip()
    contract_date = request.form.get("contract_date", "").strip()
    name = request.form.get("name", "").strip()
    customer = request.form.get("customer", "").strip()
    if not contract_number or not name:
        return _index_error("Заполните номер договора и наименование проекта.")
    object_id = ObjectRepository().create(
        contract_number=contract_number,
        contract_date=contract_date,
        name=name,
        customer=customer,
        template_id=_optional_int(request.form.get("template_id")),
    )
    return redirect(url_for("web.object_workspace", object_id=object_id))


@bp.post("/objects/<int:object_id>/update")
def update_object(object_id: int):
    contract_number = request.form.get("contract_number", "").strip()
    contract_date = request.form.get("contract_date", "").strip()
    name = request.form.get("name", "").strip()
    customer = request.form.get("customer", "").strip()
    if not contract_number or not name:
        return _index_error("Заполните номер договора и наименование проекта.")
    if not ObjectRepository().update(object_id, contract_number=contract_number, contract_date=contract_date, name=name, customer=customer):
        abort(404)
    return redirect(url_for("web.index"))


@bp.post("/objects/<int:object_id>/archive")
def archive_object(object_id: int):
    if not ObjectRepository().archive(object_id):
        abort(404)
    flash("Проект перемещён в архив. Документы и файлы сохранены.", "success")
    return redirect(url_for("web.index"))


@bp.post("/objects/<int:object_id>/restore")
def restore_object(object_id: int):
    if not ObjectRepository().restore(object_id):
        abort(404)
    flash("Проект восстановлен.", "success")
    return redirect(url_for("web.index"))


@bp.post("/objects/<int:object_id>/delete")
def delete_object(object_id: int):
    act_filenames = HandoverActRepository().filenames_for_object(object_id)
    correspondence_repo = CorrespondenceRepository()
    correspondence_filenames = [row["stored_filename"] for kind in ("correspondence", "approval") for row in correspondence_repo.list_by_object(object_id, kind)]
    stored_filenames = ObjectRepository().permanently_delete(object_id)
    if stored_filenames is None:
        abort(404)
    stored_filenames.extend(correspondence_filenames)

    upload_dir = Path(current_app.config["UPLOAD_DIR"]).resolve()
    cleanup_failed = False
    for stored_name in stored_filenames:
        candidate = Path(stored_name)
        path = (upload_dir / candidate).resolve()
        if candidate.is_absolute() or candidate.name != stored_name or path.parent != upload_dir:
            cleanup_failed = True
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            cleanup_failed = True

    acts_root = Path(current_app.config["ACTS_DIR"]).resolve()
    project_acts_dir = (acts_root / str(object_id)).resolve()
    if project_acts_dir.parent != acts_root:
        cleanup_failed = True
    for stored_name in act_filenames:
        candidate = Path(stored_name)
        path = (project_acts_dir / candidate).resolve()
        if candidate.is_absolute() or candidate.name != stored_name or project_acts_dir.parent != acts_root or path.parent != project_acts_dir:
            cleanup_failed = True
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            cleanup_failed = True
    try:
        project_acts_dir.rmdir()
    except OSError:
        pass

    if cleanup_failed:
        flash("Проект удалён. Некоторые исходные файлы не удалось очистить автоматически.", "error")
    else:
        flash("Проект, документы, чек-лист и исходные файлы удалены.", "success")
    return redirect(url_for("web.index"))


@bp.get("/objects/<int:object_id>")
def object_workspace(object_id: int):
    obj = ObjectRepository().get(object_id)
    if obj is None or obj["archived_at"]:
        abort(404)
    warning_days = current_app.config["EXPIRY_WARNING_DAYS"]
    documents = [attach_expiry(row, warning_days) for row in DocumentRepository().list_by_object(object_id)]
    checklist_repository = ChecklistRepository()
    checklist_repository.sync_documents_to_checklist(object_id, documents)
    checklist = checklist_repository.list_project_items(object_id)
    checklist = _attach_checklist_expiry(checklist, warning_days)
    template_items = checklist_repository.list_template_items(obj["template_id"]) if obj["template_id"] else []
    existing_template_item_ids = {
        row["source_template_item_id"] for row in checklist if row["source_template_item_id"] is not None
    }
    existing_titles = {checklist_repository._normalize(row["title"]) for row in checklist}
    template_items = [
        {**dict(item), "already_added": item["id"] in existing_template_item_ids
         or checklist_repository._normalize(item["title"]) in existing_titles}
        for item in template_items
    ]
    documents_by_id = {document["id"]: document for document in documents}
    checklist_documents = {}
    for link in checklist_repository.list_linked_documents(object_id):
        document = documents_by_id.get(link["document_id"])
        if document is not None:
            checklist_documents.setdefault(link["checklist_item_id"], []).append(document)
    multi_document_item_ids = {
        row["id"] for row in checklist
        if checklist_repository.allows_multiple_documents(row["title"])
    }
    archived_documents = [attach_expiry(row, warning_days) for row in DocumentRepository().list_archived_by_object(object_id)]
    return render_template(
        "object.html",
        object=obj,
        documents=documents,
        archived_documents=archived_documents,
        checklist=checklist,
        template_items=template_items,
        checklist_documents=checklist_documents,
        multi_document_item_ids=multi_document_item_ids,
        missing_count=sum(row["status"] == "missing" for row in checklist),
        received_count=sum(row["status"] == "received" for row in checklist),
        warning_days=warning_days,
        correspondence=CorrespondenceRepository().list_by_object(object_id),
        approvals=CorrespondenceRepository().list_by_object(object_id, "approval"),
    )


@bp.get("/objects/<int:object_id>/documents/<kind>")
def project_document_form(object_id: int, kind: str):
    if kind not in DOCUMENT_TYPES:
        abort(404)
    project = ObjectRepository().get(object_id)
    if project is None or project["archived_at"]:
        abort(404)
    checklist = ChecklistRepository().list_project_items(object_id)
    documents = DocumentRepository().list_by_object(object_id)
    template = "document_generation_fragment.html" if request.args.get("partial") == "1" else "document_generation.html"
    return render_template(
        template,
        project=project,
        kind=kind,
        spec=DOCUMENT_TYPES[kind],
        checklist=checklist,
        documents=documents,
        missing_count=sum(item["status"] == "missing" for item in checklist),
        replacement_count=sum(bool(item["replacement_required"]) for item in checklist),
        in_modal=False,
    )


@bp.post("/objects/<int:object_id>/documents/<kind>")
def download_project_document(object_id: int, kind: str):
    if kind not in DOCUMENT_TYPES:
        abort(404)
    project = ObjectRepository().get(object_id)
    if project is None or project["archived_at"]:
        abort(404)

    checklist = ChecklistRepository().list_project_items(object_id)
    documents = DocumentRepository().list_by_object(object_id)
    missing_count = sum(item["status"] == "missing" for item in checklist)
    replacement_count = sum(bool(item["replacement_required"]) for item in checklist)
    if kind == "request" and missing_count == 0 and replacement_count == 0:
        flash("В чек-листе проекта нет недостающих документов или документов, требующих замены.", "error")
        return redirect(url_for("web.project_document_form", object_id=object_id, kind=kind))
    if kind == "handover" and not documents:
        flash("Добавьте документы проекта перед созданием акта приёма-передачи.", "error")
        return redirect(url_for("web.project_document_form", object_id=object_id, kind=kind))

    field_limits = {
        "recipient": 1000,
        "sender": 250,
        "sender_signatory": 250,
        "signatory_position": 500,
        "greeting_name": 120,
        "number": 100,
        "date": 20,
        "incoming_number": 100,
        "incoming_date": 20,
        "contract_date": 20,
        "deadline": 20,
        "contact_phone": 100,
    }
    fields = {
        name: request.form.get(name, "").strip()[:limit]
        for name, limit in field_limits.items()
    }
    checklist_document_titles = {}
    if kind == "handover":
        checklist_by_id = {item["id"]: item for item in checklist}
        for link in ChecklistRepository().list_linked_documents(object_id):
            item = checklist_by_id.get(link["checklist_item_id"])
            if item and ChecklistRepository.uses_customer_letter_subject(item["title"]):
                checklist_document_titles[link["document_id"]] = item["title"]
    content = generate_project_document(
        kind, project, documents, checklist, fields,
        checklist_document_titles=checklist_document_titles,
    )
    project_slug = secure_filename(project["name"]) or f"project-{object_id}"
    filename = f"{DOCUMENT_TYPES[kind]['filename']}_{project_slug}.docx"
    if kind == "handover":
        now = datetime.now(timezone.utc).replace(microsecond=0)
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        project_dir = Path(current_app.config["ACTS_DIR"]) / str(object_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        contract_slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё.-]+", "-", project["contract_number"] or "").strip("-._")
        contract_slug = contract_slug or f"project-{object_id}"
        filename_prefix = f"Акт_{contract_slug}_"
        sequence_numbers = []
        for candidate in project_dir.glob(f"{filename_prefix}*.docx"):
            match = re.fullmatch(re.escape(filename_prefix) + r"(\d+)\.docx", candidate.name)
            if match:
                sequence_numbers.append(int(match.group(1)))
        existing_manual_acts = sum(
            act["source"] == "manual"
            for act in HandoverActRepository().list_by_object(object_id)
        )
        sequence = max(max(sequence_numbers, default=0), existing_manual_acts) + 1
        filename = f"{filename_prefix}{sequence}.docx"
        while (project_dir / filename).exists():
            sequence += 1
            filename = f"{filename_prefix}{sequence}.docx"
        target = project_dir / filename
        temp = project_dir / f".{filename}.tmp"
        temp.write_bytes(content)
        temp.replace(target)
        HandoverActRepository().create_manual(object_id, stamp, stamp, filename)
        return send_file(
            target,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=filename,
        )
    return send_file(
        io.BytesIO(content),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=filename,
    )


@bp.get("/objects/<int:object_id>/acts")
def project_acts(object_id: int):
    project = ObjectRepository().get(object_id)
    if project is None or project["archived_at"]:
        abort(404)
    return render_template(
        "project_acts.html",
        project=project,
        acts=HandoverActRepository().list_by_object(object_id),
        mail_configured=bool(
            current_app.config["MAIL_USERNAME"]
            and current_app.config["MAIL_APP_PASSWORD"]
        ),
    )


@bp.get("/objects/<int:object_id>/acts/<int:act_id>")
def download_handover_act(object_id: int, act_id: int):
    project = ObjectRepository().get(object_id)
    act = next(
        (row for row in HandoverActRepository().list_by_object(object_id) if row["id"] == act_id),
        None,
    )
    if project is None or act is None:
        abort(404)
    acts_root = Path(current_app.config["ACTS_DIR"]).resolve()
    project_dir = (acts_root / str(object_id)).resolve()
    filename = act["stored_filename"]
    candidate = (project_dir / filename).resolve()
    if Path(filename).name != filename or candidate.parent != project_dir or not candidate.is_file():
        abort(404)
    return send_file(
        candidate,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=filename,
    )


@bp.get("/templates")
def manage_templates():
    repo = ChecklistRepository()
    templates = [{"template": row, "items": repo.list_template_items(row["id"])} for row in repo.list_templates()]
    return render_template("templates.html", templates=templates)


@bp.post("/templates")
def create_template():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name:
        flash("Укажите название шаблона.", "error")
        return redirect(url_for("web.manage_templates"))
    try:
        template_id = ChecklistRepository().create_template(name, description)
    except Exception:
        flash("Шаблон с таким названием уже существует.", "error")
        return redirect(url_for("web.manage_templates"))
    return redirect(url_for("web.manage_templates") + f"#template-{template_id}")


@bp.post("/templates/<int:template_id>/update")
def update_template(template_id: int):
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name or not ChecklistRepository().update_template(template_id, name, description):
        abort(400)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.manage_templates") + f"#template-{template_id}")


@bp.post("/templates/<int:template_id>/copy")
def copy_template(template_id: int):
    repo = ChecklistRepository()
    source = repo.get_template(template_id)
    if source is None:
        abort(404)
    name = request.form.get("name", "").strip() or f"{source['name']} — копия"
    try:
        copied_id = repo.copy_template(template_id, name)
    except Exception:
        flash("Шаблон с таким названием уже существует. Укажите другое название.", "error")
        return redirect(url_for("web.manage_templates") + f"#template-{template_id}")
    flash("Шаблон скопирован вместе со списком документов.", "success")
    return redirect(url_for("web.manage_templates") + f"#template-{copied_id}")


@bp.post("/templates/<int:template_id>/delete")
def delete_template(template_id: int):
    if not ChecklistRepository().delete_template(template_id):
        abort(404)
    flash("Шаблон удалён. Уже созданные списки проектов сохранены.", "success")
    return redirect(url_for("web.manage_templates"))


@bp.post("/templates/<int:template_id>/items")
def add_template_item(template_id: int):
    repo = ChecklistRepository()
    if repo.get_template(template_id) is None:
        abort(404)
    title = request.form.get("title", "").strip()
    if title:
        repo.add_template_item(template_id, title, request.form.get("description", "").strip())
    return redirect(url_for("web.manage_templates") + f"#template-{template_id}")


@bp.post("/templates/items/<int:item_id>/update")
def update_template_item(item_id: int):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    if not title or not ChecklistRepository().update_template_item(item_id, title, description):
        abort(400)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    template_id = request.form.get("template_id", "").strip()
    suffix = f"?edit={template_id}#template-{template_id}" if template_id.isdigit() else ""
    return redirect(url_for("web.manage_templates") + suffix)


@bp.post("/templates/items/<int:item_id>/delete")
def delete_template_item(item_id: int):
    if not ChecklistRepository().delete_template_item(item_id):
        abort(404)
    return redirect(url_for("web.manage_templates"))


@bp.post("/objects/<int:object_id>/acts/<int:act_id>/delete")
def delete_handover_act(object_id: int, act_id: int):
    project = ObjectRepository().get(object_id)
    repository = HandoverActRepository()
    act = repository.get(object_id, act_id)
    if project is None or act is None:
        abort(404)

    acts_root = Path(current_app.config["ACTS_DIR"]).resolve()
    project_dir = (acts_root / str(object_id)).resolve()
    filename = act["stored_filename"]
    candidate = (project_dir / filename).resolve()
    if Path(filename).name != filename or project_dir.parent != acts_root or candidate.parent != project_dir:
        abort(404)
    candidate.unlink(missing_ok=True)
    if not repository.delete(object_id, act_id):
        abort(404)
    flash("Акт удалён вместе с файлом.", "success")
    return redirect(url_for("web.project_acts", object_id=object_id))


@bp.post("/objects/<int:object_id>/checklist/<int:item_id>")
def update_checklist_item(object_id: int, item_id: int):
    status = request.form.get("status", "missing")
    document_id = _optional_int(request.form.get("document_id"))
    issue_date = request.form.get("issue_date") if "issue_date" in request.form else None
    note = request.form.get("note") if "note" in request.form else None
    if not ChecklistRepository().update_project_item(object_id, item_id, status, document_id, issue_date, note):
        abort(404)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.object_workspace", object_id=object_id) + "#checklist")


@bp.post("/objects/<int:object_id>/checklist/<int:item_id>/replacement")
def update_checklist_replacement(object_id: int, item_id: int):
    required = request.form.get("required") == "1"
    if not ChecklistRepository().update_replacement_required(object_id, item_id, required):
        abort(404)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.object_workspace", object_id=object_id) + "#checklist")


@bp.post("/objects/<int:object_id>/checklist/add")
def add_checklist_item(object_id: int):
    obj = ObjectRepository().get(object_id)
    add_mode = request.form.get("add_mode", "custom")
    template_item_id = request.form.get("template_item_id", "").strip()
    title = request.form.get("title", "").strip()[:300]
    note = request.form.get("note", "").strip()[:2000]
    if obj is None or obj["archived_at"]:
        abort(404)
    repository = ChecklistRepository()
    if add_mode == "template":
        try:
            added = repository.add_project_template_item(object_id, int(template_item_id)) if template_item_id else None
        except (TypeError, ValueError):
            added = None
        if added is None:
            flash("Пункт шаблона уже добавлен или не относится к шаблону проекта.", "error")
        else:
            flash("Документ из шаблона добавлен в комплектность проекта.", "success")
    elif add_mode != "custom":
        abort(400)
    elif not title:
        flash("Укажите название документа.", "error")
    elif repository.add_project_item(object_id, title, note) is None:
        flash("Документ с таким названием уже есть в списке.", "error")
    else:
        flash("Документ добавлен в комплектность проекта.", "success")
    return redirect(url_for("web.object_workspace", object_id=object_id, tab="checklist"))


@bp.post("/objects/<int:object_id>/checklist/<int:item_id>/delete")
def delete_checklist_item(object_id: int, item_id: int):
    if ObjectRepository().get(object_id) is None or not ChecklistRepository().delete_project_item(object_id, item_id):
        abort(404)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    flash("Пункт убран из списка проекта. Загруженные документы сохранены в архиве.", "success")
    return redirect(url_for("web.object_workspace", object_id=object_id, tab="checklist"))


@bp.post("/objects/<int:object_id>/checklist/<int:item_id>/note")
def update_checklist_note(object_id: int, item_id: int):
    if ObjectRepository().get(object_id) is None or not ChecklistRepository().update_project_note(
        object_id, item_id, request.form.get("note", "")
    ):
        abort(404)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.object_workspace", object_id=object_id, tab="checklist") + "#checklist")


@bp.post("/objects/<int:object_id>/checklist/<int:item_id>/upload")
def upload_checklist_document(object_id: int, item_id: int):
    obj = ObjectRepository().get(object_id)
    item = next((row for row in ChecklistRepository().list_project_items(object_id) if row["id"] == item_id), None)
    if obj is None or obj["archived_at"] or item is None:
        abort(404)
    files = [file for file in request.files.getlist("files") if file and file.filename]
    if not files:
        flash("Выберите PDF, Word-файл или фотографии страниц документа.", "error")
        return redirect(url_for("web.object_workspace", object_id=object_id) + "#checklist")

    storage = StorageService(current_app.config["UPLOAD_DIR"], max_file_size=current_app.config["MAX_UPLOAD_FILE_SIZE"])
    saved_sources = []
    final_path = None
    final_stored = ""
    final_name = ""
    final_mime = "application/pdf"
    try:
        saved = [storage.save_upload(file) for file in files]
        saved_sources = [(name, stored, path, mime) for name, stored, path, mime in saved]
        if len(saved) == 1 and saved[0][3] in {"application/pdf", *WORD_MIME_TYPES.values()}:
            final_name, final_stored, final_path, final_mime = saved[0]
        else:
            if any(mime in WORD_MIME_TYPES.values() for _name, _stored, _path, mime in saved):
                raise ValueError("Загружайте Word-документ отдельно, по одному файлу.")
            if any(not mime.startswith("image/") for _name, _stored, _path, mime in saved):
                raise ValueError("Для многостраничного документа выберите один PDF, один файл Word или несколько фотографий.")
            from PIL import Image, ImageOps
            if any(Path(name).suffix.lower() in {".heic", ".heif"} for name, *_rest in saved):
                import pillow_heif
                pillow_heif.register_heif_opener()
            pages = []
            for _name, _stored, image_path, _mime in saved:
                with Image.open(image_path) as source:
                    pages.append(ImageOps.exif_transpose(source).convert("RGB"))
            final_stored = f"{uuid4().hex}_checklist-pages.pdf"
            final_path = Path(current_app.config["UPLOAD_DIR"]) / final_stored
            final_path.parent.mkdir(parents=True, exist_ok=True)
            pages[0].save(final_path, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
            for page in pages:
                page.close()
            final_name = f"{item['title'][:100]}.pdf"
            # Keep the combined, ordered PDF as the project source; discard only temporary uploads.
            for _name, _stored, source_path, _mime in saved_sources:
                source_path.unlink(missing_ok=True)
            saved_sources = []
    except Exception as exc:
        for _name, _stored, source_path, _mime in saved_sources:
            source_path.unlink(missing_ok=True)
        flash(str(exc) if isinstance(exc, ValueError) else "Не удалось подготовить страницы документа.", "error")
        return redirect(url_for("web.object_workspace", object_id=object_id) + "#checklist")

    repository = DocumentRepository()
    if final_path and final_path.is_file():
        uploaded_hash = hashlib.sha256(final_path.read_bytes()).digest()
        for existing in repository.list_by_object(object_id):
            if (existing["original_filename"] or "").casefold() != final_name.casefold():
                continue
            stored_name = existing["stored_filename"] or ""
            if not stored_name or Path(stored_name).name != stored_name:
                continue
            existing_path = Path(current_app.config["UPLOAD_DIR"]) / stored_name
            if existing_path.is_file() and hashlib.sha256(existing_path.read_bytes()).digest() == uploaded_hash:
                final_path.unlink(missing_ok=True)
                manual_assignment = project_assignment_fields(item["title"], obj["customer"] or "")
                if manual_assignment is not None:
                    repository.update_fields(existing["id"], {
                        "working_title": manual_assignment.working_title,
                        "official_title": "",
                        "issuer": manual_assignment.issuer,
                        "document_number": "",
                        "issue_date": "",
                        "valid_until": "",
                        "no_expiration": 0,
                    })
                    repository.update_note(existing["id"], "")
                ChecklistRepository().link_document(object_id, item_id, existing["id"])
                flash("Такой файл уже был загружен. Существующий документ привязан к строке комплектности.", "success")
                return redirect(url_for("web.object_workspace", object_id=object_id, tab="checklist"))
    if request.headers.get("X-IRD-Analyze") == "1":
        try:
            parsed = project_assignment_fields(item["title"], obj["customer"] or "")
            if parsed is None:
                pipeline = DocumentProcessingPipeline(
                    document_types=current_app.config["DOCUMENT_TYPES"],
                    tesseract_lang=current_app.config["TESSERACT_LANG"],
                    ocr_dpi=current_app.config["OCR_DPI"],
                )
                parsed = pipeline.process(final_path)
                _apply_checklist_document_title(parsed, item["title"])
            document_id = repository.create(
                object_id=object_id, contract_number=obj["contract_number"], object_name=obj["name"],
                original_filename=final_name, stored_filename=final_stored, mime_type=final_mime,
                parsed=parsed, note="", status="processed", error="",
            )
            ChecklistRepository().link_document(object_id, item_id, document_id)
            first_page = parsed.raw_text.split("--- page 2 ---", 1)[0]
            number_match = None
            is_eroks_card = parsed.working_title == "Копия регистрационной карточки в ЕРОКС"
            is_beltelecom = RegexFieldExtractor._is_beltelecom_technical_conditions(parsed.raw_text)
            is_minskenergo = RegexFieldExtractor._is_minskenergo_electrical_conditions(parsed.raw_text)
            header_number = "" if (is_eroks_card or is_beltelecom or is_minskenergo) else RegexFieldExtractor._extract_committee_decision_number(first_page)
            if not number_match and not (is_eroks_card or is_beltelecom or is_minskenergo):
                number_match = re.search(r"(?:№|N\s*|номер\s*)\s*([А-ЯЁA-Z0-9][А-ЯЁA-Z0-9./-]{1,30})", first_page, re.I)
            return {"id": document_id, "preview": url_for("web.document_viewer", document_id=document_id),
                    "fields": {"working_title": parsed.working_title or item["title"], "official_title": parsed.official_title, "issuer": parsed.issuer,
                               "document_number": "" if is_eroks_card else (parsed.document_number if (is_beltelecom or is_minskenergo) else (header_number or parsed.document_number or (number_match.group(1) if number_match else ""))), "issue_date": iso_date(parsed.issue_date),
                               "valid_until": iso_date(parsed.valid_until), "no_expiration": parsed.no_expiration}}
        except Exception as exc:
            logger.exception("Checklist document extraction failed")
            for _name, _stored, source_path, _mime in saved_sources:
                source_path.unlink(missing_ok=True)
            if final_path:
                final_path.unlink(missing_ok=True)
            return {"error": str(exc)[:500] or "Не удалось распознать документ."}, 422
    document_id = repository.create(
        object_id=object_id, contract_number=obj["contract_number"], object_name=obj["name"],
        original_filename=final_name, stored_filename=final_stored, mime_type=final_mime,
        parsed=ParsedDocument(), note="", status="queued", error="",
    )
    ChecklistRepository().link_document(object_id, item_id, document_id)
    flash(f"Файл «{item['title']}» принят. Распознавание выполняется в фоне.", "success")
    return redirect(url_for("web.object_workspace", object_id=object_id, tab="checklist"))


@bp.post("/objects/<int:object_id>/documents")
def upload_documents(object_id: int):
    obj = ObjectRepository().get(object_id)
    if obj is None or obj["archived_at"]:
        abort(404)
    files = request.files.getlist("files")
    if not files:
        legacy_file = request.files.get("pdf_file")
        files = [legacy_file] if legacy_file else []
    files = [file for file in files if file and file.filename]
    if not files:
        return _object_error(object_id, "Выберите PDF или фотографию документа.")

    storage = StorageService(
        current_app.config["UPLOAD_DIR"],
        max_file_size=current_app.config["MAX_UPLOAD_FILE_SIZE"],
    )
    repository = DocumentRepository()
    created = []
    photo_sources = []
    groups = []
    note = request.form.get("note", "").strip()
    for file in files:
        try:
            saved = storage.save_upload(file)
        except Exception as exc:
            flash(f"{file.filename}: {exc}", "error")
            continue
        if saved[3].startswith("image/"):
            photo_sources.append(saved)
        else:
            groups.append(saved)

    if photo_sources:
        combined_path = Path(current_app.config["UPLOAD_DIR"]) / f"{uuid4().hex}_phone-pages.pdf"
        try:
            _combine_image_uploads(photo_sources, combined_path)
            groups.append((f"Фотографии документа — {len(photo_sources)} страниц.pdf", combined_path.name, combined_path, "application/pdf"))
        except Exception as exc:
            flash(f"Не удалось объединить фотографии в PDF: {exc}", "error")
        finally:
            for _name, _stored, source_path, _mime in photo_sources:
                source_path.unlink(missing_ok=True)

    for original_name, stored_name, _path, mime_type in groups:
        document_id = repository.create(
            object_id=object_id,
            contract_number=obj["contract_number"],
            object_name=obj["name"],
            original_filename=original_name,
            stored_filename=stored_name,
            mime_type=mime_type,
            parsed=ParsedDocument(),
            note=note,
            status="queued",
        )
        created.append(document_id)
    if len(created) == 1:
        return redirect(url_for("web.document_detail", document_id=created[0]))
    if created:
        flash(f"Файлы приняты: {len(created)}. Распознавание выполняется в фоне.", "success")
    return redirect(url_for("web.object_workspace", object_id=object_id, tab="checklist"))


@bp.post("/objects/<int:object_id>/correspondence")
def upload_correspondence(object_id: int):
    obj = ObjectRepository().get(object_id)
    if obj is None or obj["archived_at"]:
        abort(404)
    record_type = request.form.get("record_type", "correspondence")
    if record_type not in {"correspondence", "approval"}:
        record_type = "correspondence"
    target_tab = "approvals" if record_type == "approval" else "correspondence"
    direction = request.form.get("direction", "incoming")
    if direction not in {"incoming", "outgoing"}:
        direction = "incoming"
    files = [file for file in request.files.getlist("files") if file and file.filename]
    if not files:
        if request.headers.get("X-Correspondence-Analyze") == "1":
            return jsonify(error="Выберите PDF, Word-файл или изображения страниц письма."), 400
        flash("Выберите PDF или фотографии страниц письма.", "error")
        return redirect(url_for("web.object_workspace", object_id=object_id, tab=target_tab))

    storage = StorageService(current_app.config["UPLOAD_DIR"], max_file_size=current_app.config["MAX_UPLOAD_FILE_SIZE"])
    saved_files = []
    analyze = request.headers.get("X-Correspondence-Analyze") == "1"
    try:
        for file in files:
            saved_files.append(storage.save_upload(file))
        word_files = [item for item in saved_files if is_word_document(item[2])]
        pdf_files = [item for item in saved_files if item[3] == "application/pdf"]
        image_files = [item for item in saved_files if item[3].startswith("image/")]
        if word_files:
            if len(saved_files) != 1:
                raise ValueError("Загружайте один Word-файл за раз.")
            prepared_files = word_files
        elif pdf_files:
            if len(saved_files) != 1:
                raise ValueError("Выберите один PDF либо несколько изображений страниц.")
            prepared_files = pdf_files
        elif image_files and len(image_files) == len(saved_files):
            combined_path = Path(current_app.config["UPLOAD_DIR"]) / f"{uuid4().hex}_letter-pages.pdf"
            _combine_image_uploads(image_files, combined_path)
            for _name, _stored, source_path, _mime in image_files:
                source_path.unlink(missing_ok=True)
            prepared_files = [(f"Письмо — {len(image_files)} страниц.pdf", combined_path.name, combined_path, "application/pdf")]
            saved_files = prepared_files
        else:
            raise ValueError("Выберите один PDF, один Word-файл или несколько изображений страниц письма.")
    except Exception as exc:
        for _name, _stored, source_path, _mime in saved_files:
            source_path.unlink(missing_ok=True)
        if analyze:
            return jsonify(error=str(exc) if isinstance(exc, ValueError) else "Не удалось подготовить файл письма."), 422
        flash(str(exc) if isinstance(exc, ValueError) else "Не удалось подготовить файл письма.", "error")
        return redirect(url_for("web.object_workspace", object_id=object_id, tab=target_tab))

    extractor = PdfTextExtractor(tesseract_lang=current_app.config["TESSERACT_LANG"], dpi=current_app.config["OCR_DPI"])
    repository = CorrespondenceRepository()
    original_name, stored_name, path, mime_type = prepared_files[0]
    raw_text = ""
    values = {"direction": direction, "number": "", "letter_date": "", "sender": "", "recipient": "", "subject": "", "summary": ""}
    status, error = "processed", ""
    try:
        extraction_path = word_preview_path(path) if is_word_document(path) else path
        raw_text = extractor.extract(extraction_path, correspondence=True)
        values.update(_extract_correspondence_fields(raw_text))
        if not raw_text.strip():
            status, error = "error", "Не удалось распознать текст письма. Заполните реквизиты вручную."
    except Exception as exc:
        status, error = "error", str(exc)
        logger.exception("Correspondence OCR or extraction failed")
        if not analyze:
            flash(f"Не удалось распознать письмо: {exc}", "error")
    values["direction"] = direction
    letter_id = repository.create(
        object_id=object_id, **values, original_filename=original_name,
        stored_filename=stored_name, mime_type=mime_type, raw_text=raw_text,
        status=status, error=error, record_type=record_type,
    )
    if analyze:
        return jsonify(
            id=letter_id,
            preview=url_for("web.correspondence_preview", letter_id=letter_id),
            record_type=record_type,
            fields={key: values[key] for key in ("direction", "number", "letter_date", "sender", "recipient", "subject", "summary")}
                  | {"valid_until": ""},
            warning=error,
        )
    if letter_id:
        flash("Письмо добавлено. Проверьте распознанные реквизиты.", "success")
    return redirect(url_for("web.object_workspace", object_id=object_id, tab=target_tab))


@bp.post("/objects/<int:object_id>/correspondence/<int:letter_id>")
def update_correspondence(object_id: int, letter_id: int):
    repo = CorrespondenceRepository()
    letter = repo.get(letter_id)
    if letter is None or letter["object_id"] != object_id:
        abort(404)
    direction = request.form.get("direction", letter["direction"])
    if direction not in {"incoming", "outgoing"}:
        direction = letter["direction"]
    repo.update(
        letter_id,
        direction=direction,
        number=request.form.get("number", "").strip()[:100],
        letter_date=_normalize_letter_date(request.form.get("letter_date", "")),
        sender=normalize_organization_forms(request.form.get("sender", "").strip())[:500],
        recipient=normalize_organization_forms(request.form.get("recipient", "").strip())[:500],
        subject=request.form.get("subject", "").strip()[:500],
        summary=request.form.get("summary", "").strip()[:4000],
        valid_until=request.form.get("valid_until", "").strip()[:20],
    )
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.object_workspace", object_id=object_id, tab="correspondence"))


@bp.post("/objects/<int:object_id>/correspondence/<int:letter_id>/delete")
def delete_correspondence(object_id: int, letter_id: int):
    repo = CorrespondenceRepository()
    letter = repo.get(letter_id)
    if letter is None or letter["object_id"] != object_id:
        abort(404)
    stored = letter["stored_filename"]
    if not repo.delete(letter_id):
        abort(404)
    if stored and Path(stored).name == stored:
        (Path(current_app.config["UPLOAD_DIR"]) / stored).unlink(missing_ok=True)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.object_workspace", object_id=object_id, tab="correspondence"))


@bp.get("/correspondence/<int:letter_id>/file")
def correspondence_file(letter_id: int):
    letter = CorrespondenceRepository().get(letter_id)
    if letter is None or not letter["stored_filename"]:
        abort(404)
    stored = letter["stored_filename"]
    if Path(stored).name != stored:
        abort(404)
    response = send_from_directory(
        current_app.config["UPLOAD_DIR"], stored,
        mimetype=letter["mime_type"] or "application/pdf", as_attachment=False,
        download_name=letter["original_filename"] or stored,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.get("/correspondence/<int:letter_id>/preview")
def correspondence_preview(letter_id: int):
    letter = CorrespondenceRepository().get(letter_id)
    if letter is None or not letter["stored_filename"]:
        abort(404)
    upload_dir = Path(current_app.config["UPLOAD_DIR"])
    stored = letter["stored_filename"]
    if Path(stored).name != stored:
        abort(404)
    source = upload_dir / stored
    if is_word_document(source):
        try:
            preview = word_preview_path(source)
        except (OSError, RuntimeError, ValueError):
            logger.exception("Correspondence Word preview conversion failed for letter id=%s", letter_id)
            abort(415)
        filename = f"{Path(letter['original_filename'] or stored).stem}.pdf"
        response = send_file(preview, mimetype="application/pdf", as_attachment=False, download_name=filename)
        response.headers["Content-Disposition"] = "inline; filename*=UTF-8''" + quote(filename)
    else:
        response = send_from_directory(
            upload_dir, stored, mimetype=letter["mime_type"] or "application/pdf",
            as_attachment=False, download_name=letter["original_filename"] or stored,
        )
        response.headers["Content-Disposition"] = "inline; filename*=UTF-8''" + quote(letter["original_filename"] or stored)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.get("/documents/<int:document_id>")
def document_detail(document_id: int):
    document = DocumentRepository().get(document_id)
    if document is None or document["archived_at"]:
        abort(404)
    if not document["object_id"]:
        abort(404)
    return redirect(url_for(
        "web.object_workspace",
        object_id=document["object_id"],
        tab="checklist",
        open_document=document_id,
    ))


@bp.post("/documents/<int:document_id>/fields")
def update_document_fields(document_id: int):
    document = DocumentRepository().get(document_id)
    if document is None:
        abort(404)
    no_expiration = request.form.get("no_expiration") == "on"
    values = {
        "working_title": request.form.get("working_title", "").strip(),
        "official_title": request.form.get("official_title", "").strip(),
        "issuer": request.form.get("issuer", "").strip(),
        "document_number": request.form.get("document_number", "").strip(),
        "issue_date": request.form.get("issue_date", "").strip(),
        "valid_until": "" if no_expiration else request.form.get("valid_until", "").strip(),
        "no_expiration": int(no_expiration),
    }
    if not DocumentRepository().update_fields(document_id, values):
        abort(404)
    if document["object_id"] and request.headers.get("X-Preserve-Checklist-Link") != "1":
        ChecklistRepository().match_document(document["object_id"], document_id, values["working_title"])
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.document_detail", document_id=document_id))


@bp.post("/documents/<int:document_id>/note")
def update_document_note(document_id: int):
    if not DocumentRepository().update_note(document_id, request.form.get("note", "").strip()):
        abort(404)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.document_detail", document_id=document_id))


@bp.get("/documents/<int:document_id>/preview")
def document_preview(document_id: int):
    document = DocumentRepository().get(document_id)
    if document is None or document["archived_at"] or not document["stored_filename"]:
        abort(404)
    upload_dir = Path(current_app.config["UPLOAD_DIR"])
    source = upload_dir / document["stored_filename"]
    if is_word_document(source):
        try:
            preview = word_preview_path(source)
        except (OSError, RuntimeError, ValueError):
            logger.exception("Word preview conversion failed for document id=%s", document_id)
            abort(415)
        preview_name = f"{Path(document['original_filename'] or source.name).stem}.pdf"
        response = send_file(preview, mimetype="application/pdf", as_attachment=False, download_name=preview_name)
        response.headers["Content-Disposition"] = "inline; filename*=UTF-8''" + quote(preview_name)
    else:
        response = send_from_directory(
            upload_dir, document["stored_filename"],
            mimetype=document["mime_type"] or "application/pdf", as_attachment=False,
            download_name=document["original_filename"] or document["stored_filename"],
        )
        response.headers["Content-Disposition"] = "inline; filename*=UTF-8''" + quote(document["original_filename"] or document["stored_filename"])
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _document_pdf_path(document):
    upload_dir = Path(current_app.config["UPLOAD_DIR"])
    source = upload_dir / document["stored_filename"]
    if is_word_document(source):
        try:
            return word_preview_path(source)
        except (OSError, RuntimeError, ValueError):
            logger.exception("Word preview conversion failed for document id=%s", document["id"])
            abort(415)
    return source


@bp.get("/documents/<int:document_id>/viewer")
def document_viewer(document_id: int):
    document = DocumentRepository().get(document_id)
    if document is None or document["archived_at"] or not document["stored_filename"]:
        abort(404)
    pdf_path = _document_pdf_path(document)
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            page_count = len(pdf)
        finally:
            pdf.close()
    except Exception:
        logger.exception("Could not open PDF preview for document id=%s", document_id)
        abort(415)
    return render_template("document_viewer.html", document_id=document_id, page_count=page_count)


@bp.get("/documents/<int:document_id>/viewer/pages/<int:page_index>")
def document_viewer_page(document_id: int, page_index: int):
    document = DocumentRepository().get(document_id)
    if document is None or document["archived_at"] or not document["stored_filename"]:
        abort(404)
    pdf_path = _document_pdf_path(document)
    thumbnail = request.args.get("thumbnail") == "1"
    try:
        import pypdfium2 as pdfium

        stat = pdf_path.stat()
        cache_dir = Path(current_app.instance_path) / "preview-cache" / f"{document_id}-{stat.st_mtime_ns}-{stat.st_size}"
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / f"{page_index}-{'thumb' if thumbnail else 'page'}.jpg"
        if not target.is_file():
            pdf = pdfium.PdfDocument(str(pdf_path))
            try:
                if page_index < 0 or page_index >= len(pdf):
                    abort(404)
                scale = 0.3 if thumbnail else 1.6
                bitmap = pdf[page_index].render(scale=scale)
                image = bitmap.to_pil().convert("RGB")
                image.save(target, format="JPEG", quality=82 if thumbnail else 90, optimize=True)
            finally:
                pdf.close()
        response = send_file(target, mimetype="image/jpeg", conditional=True, max_age=86400)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except Exception as exc:
        if getattr(exc, "code", None) == 404:
            raise
        logger.exception("Could not render PDF page %s for document id=%s", page_index, document_id)
        abort(415)


@bp.post("/documents/<int:document_id>/delete")
def delete_document(document_id: int):
    repository = DocumentRepository()
    document = repository.get(document_id)
    if document is None:
        abort(404)
    object_id = document["object_id"]
    stored_name = document["stored_filename"] or ""
    deleted = repository.delete(document_id)
    if not deleted:
        abort(404)
    if stored_name and Path(stored_name).name == stored_name:
        upload_dir = Path(current_app.config["UPLOAD_DIR"]).resolve()
        file_path = (upload_dir / stored_name).resolve()
        if file_path.parent == upload_dir:
            still_referenced = get_db().execute(
                "SELECT 1 FROM documents WHERE stored_filename = ? UNION ALL SELECT 1 FROM correspondence WHERE stored_filename = ? LIMIT 1",
                (stored_name, stored_name),
            ).fetchone()
            if still_referenced is None:
                file_path.unlink(missing_ok=True)
                remove_word_preview(file_path)
    if object_id:
        return redirect(url_for("web.object_workspace", object_id=object_id, tab="checklist"))
    return redirect(url_for("web.index"))


@bp.post("/documents/<int:document_id>/restore")
def restore_document(document_id: int):
    repository = DocumentRepository()
    document = repository.get(document_id)
    if document is None or not document["object_id"] or not repository.restore(document_id):
        abort(404)
    flash("Документ восстановлен вместе с исходным файлом.", "success")
    return redirect(url_for("web.object_workspace", object_id=document["object_id"]))


@bp.get("/objects/<int:object_id>/export")
def export_object(object_id: int):
    obj = ObjectRepository().get(object_id)
    if obj is None:
        abort(404)
    documents = DocumentRepository().list_by_object(object_id)
    correspondence_repo = CorrespondenceRepository()
    letters = correspondence_repo.list_by_object(object_id)
    approvals = correspondence_repo.list_by_object(object_id, "approval")
    checklist_repository = ChecklistRepository()
    checklist = checklist_repository.list_project_items(object_id)
    checklist_document_ids = {}
    for link in checklist_repository.list_linked_documents(object_id):
        checklist_document_ids.setdefault(link["checklist_item_id"], []).append(link["document_id"])
    acts = HandoverActRepository().list_by_object(object_id)
    archive = io.BytesIO()
    manifest = {"created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z", "files": []}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("Проект.json", json.dumps(dict(obj), ensure_ascii=False, indent=2))
        bundle.writestr("Перечень_ИРД.json", json.dumps([dict(row) for row in documents], ensure_ascii=False, indent=2))
        bundle.writestr("Переписка.json", json.dumps([dict(row) for row in letters], ensure_ascii=False, indent=2))
        bundle.writestr("Согласования.json", json.dumps([dict(row) for row in approvals], ensure_ascii=False, indent=2))
        checklist_export = [
            {**dict(row), "document_ids": checklist_document_ids.get(row["id"], [])}
            for row in checklist
        ]
        bundle.writestr("Комплектность.json", json.dumps(checklist_export, ensure_ascii=False, indent=2))
        bundle.writestr("Акты_приёма-передачи.json", json.dumps([dict(row) for row in acts], ensure_ascii=False, indent=2))
        upload_dir = current_app.config["UPLOAD_DIR"].resolve()
        for document in documents:
            stored_name = document["stored_filename"]
            candidate = Path(stored_name) if stored_name else None
            path = (upload_dir / candidate).resolve() if candidate else None
            if path is None or candidate.is_absolute() or candidate.name != stored_name or path.parent != upload_dir or not path.is_file():
                manifest["files"].append({"document_id": document["id"], "missing": True})
                continue
            short_title = _archive_name(document["working_title"] or document["official_title"] or document["original_filename"], "Документ")
            extension = Path(document["original_filename"] or path.name).suffix or path.suffix
            member = f"01_ИРД/{document['id']:03d}_{short_title}{extension}"
            content = path.read_bytes()
            bundle.writestr(member, content)
            manifest["files"].append({
                "document_id": document["id"], "path": member,
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
            })
        for letter in letters:
            stored_name = letter["stored_filename"]
            candidate = Path(stored_name) if stored_name else None
            path = (upload_dir / candidate).resolve() if candidate else None
            if path is None or candidate.is_absolute() or candidate.name != stored_name or path.parent != upload_dir or not path.is_file():
                manifest["files"].append({"correspondence_id": letter["id"], "missing": True})
                continue
            short_title = _archive_name(letter["subject"] or letter["sender"] or "Письмо", "Письмо")
            date_part = _archive_name(letter["letter_date"] or "без_даты", "без_даты")
            member = f"02_Переписка/{letter['id']:03d}_{date_part}_{short_title}{path.suffix}"
            content = path.read_bytes()
            bundle.writestr(member, content)
            manifest["files"].append({
                "correspondence_id": letter["id"], "path": member,
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
            })
        for letter in approvals:
            stored_name = letter["stored_filename"]
            candidate = Path(stored_name) if stored_name else None
            path = (upload_dir / candidate).resolve() if candidate else None
            if path is None or candidate.is_absolute() or candidate.name != stored_name or path.parent != upload_dir or not path.is_file():
                manifest["files"].append({"approval_id": letter["id"], "missing": True})
                continue
            short_title = _archive_name(letter["subject"] or letter["sender"] or "Согласование", "Согласование")
            date_part = _archive_name(letter["letter_date"] or "без_даты", "без_даты")
            member = f"03_Согласования/{letter['id']:03d}_{date_part}_{short_title}{path.suffix}"
            content = path.read_bytes()
            bundle.writestr(member, content)
            manifest["files"].append({"approval_id": letter["id"], "path": member, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
        acts_root = Path(current_app.config["ACTS_DIR"]).resolve()
        acts_dir = (acts_root / str(object_id)).resolve()
        for act in acts:
            stored_name = act["stored_filename"]
            candidate = Path(stored_name)
            path = (acts_dir / candidate).resolve()
            if candidate.is_absolute() or candidate.name != stored_name or acts_dir.parent != acts_root or path.parent != acts_dir or not path.is_file():
                manifest["files"].append({"act_id": act["id"], "missing": True})
                continue
            period = _archive_name(act["period_end"] or act["created_at"][:10], "акт")
            member = f"04_Акты_приёма-передачи/{act['id']:03d}_Акт_за_период_до_{period}{path.suffix}"
            content = path.read_bytes()
            bundle.writestr(member, content)
            manifest["files"].append({
                "act_id": act["id"], "path": member,
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
            })
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    response = make_response(archive.getvalue())
    filename = _archive_name(obj["name"], f"Проект_{object_id}")
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote('Архив_проекта_' + filename + '.zip')}"
    return response


def _index_error(message: str):
    warning_days = current_app.config["EXPIRY_WARNING_DAYS"]
    documents = DocumentRepository().list_all()
    alerts = [{"document": row, "expiry": expiry_state(row, warning_days)} for row in documents]
    alerts = [row for row in alerts if row["expiry"]["key"] in {"expired", "expiring"}]
    object_repository = ObjectRepository()
    return render_template("index.html", objects=object_repository.list_all(), archived_objects=object_repository.list_archived(), customers=object_repository.list_customers(), templates=ChecklistRepository().list_templates(), alerts=alerts, warning_days=warning_days, error=message), 400


def _object_error(object_id: int, message: str):
    repo = ChecklistRepository()
    warning_days = current_app.config["EXPIRY_WARNING_DAYS"]
    checklist = _attach_checklist_expiry(repo.list_project_items(object_id), warning_days)
    documents = [attach_expiry(row, warning_days) for row in DocumentRepository().list_by_object(object_id)]
    documents_by_id = {document["id"]: document for document in documents}
    checklist_documents = {}
    for link in repo.list_linked_documents(object_id):
        document = documents_by_id.get(link["document_id"])
        if document is not None:
            checklist_documents.setdefault(link["checklist_item_id"], []).append(document)
    archived_documents = [attach_expiry(row, warning_days) for row in DocumentRepository().list_archived_by_object(object_id)]
    return render_template("object.html", object=ObjectRepository().get(object_id), documents=documents, archived_documents=archived_documents, checklist=checklist, checklist_documents=checklist_documents, multi_document_item_ids={row["id"] for row in checklist if repo.allows_multiple_documents(row["title"])}, missing_count=sum(row["status"] == "missing" for row in checklist), received_count=sum(row["status"] == "received" for row in checklist), warning_days=warning_days, error=message), 400


def _attach_checklist_expiry(items, warning_days: int):
    result = []
    for row in items:
        item = dict(row)
        if item.get("validity_years", 0):
            item["expiry"] = expiry_state(
                {"no_expiration": 0, "valid_until": item.get("valid_until", "")},
                warning_days,
            )
        result.append(item)
    return result


def _find_duplicate_document(repository: DocumentRepository, object_id: int, parsed: ParsedDocument):
    """Find a likely same issued document by number, date, title and issuer."""
    def normalize(value: str | None) -> str:
        return " ".join(re.sub(r"[^а-яa-z0-9]+", " ", (value or "").casefold().replace("ё", "е")).split())

    def document_number(text: str | None) -> str:
        match = re.search(
            r"(?:исх(?:одящий)?\.?\s*(?:№|n[оo]?|#)|номер\s+документа\s*[:№]?|(?<![\w])№\s*)\s*([а-яa-z0-9][а-яa-z0-9./-]{1,35})",
            text or "", re.IGNORECASE,
        )
        return normalize(match.group(1)) if match else ""

    def issuer_identity(value: str | None) -> str:
        quoted_names = re.findall(r"[«\"]([^»\"]+)[»\"]", value or "")
        if quoted_names:
            return normalize(max(quoted_names, key=len))
        return normalize(value)

    title = normalize(parsed.working_title or parsed.official_title)
    issuer = issuer_identity(parsed.issuer)
    issue_date = normalize(parsed.issue_date)
    number = document_number(parsed.raw_text)
    if not (title and issue_date):
        return None

    candidates = repository.list_by_object(object_id, include_archived=True)
    for candidate in candidates:
        candidate_title = normalize(candidate["working_title"] or candidate["official_title"])
        candidate_issuer = issuer_identity(candidate["issuer"])
        candidate_date = normalize(candidate["issue_date"])
        if not (candidate_title and candidate_date == issue_date):
            continue
        title_match = SequenceMatcher(None, title, candidate_title).ratio() >= 0.86
        if not title_match:
            continue
        candidate_number = document_number(candidate["raw_text"])
        if number and candidate_number and number != candidate_number:
            continue
        same_number = bool(number and candidate_number and number == candidate_number)
        issuer_match = bool(issuer and candidate_issuer) and SequenceMatcher(None, issuer, candidate_issuer).ratio() >= 0.76
        if not (same_number or issuer_match):
            continue
        return candidate
    return None


def _combine_image_uploads(sources, output_path: Path) -> None:
    from PIL import Image, ImageOps

    if any(Path(name).suffix.lower() in {".heic", ".heif"} for name, *_rest in sources):
        import pillow_heif
        pillow_heif.register_heif_opener()
    pages = []
    try:
        for _name, _stored, source_path, mime_type in sources:
            if not mime_type.startswith("image/"):
                raise ValueError("Для многостраничного PDF нужны фотографии одного документа.")
            with Image.open(source_path) as source:
                pages.append(ImageOps.exif_transpose(source).convert("RGB"))
        if not pages:
            raise ValueError("Не выбраны страницы для PDF.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pages[0].save(output_path, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    finally:
        for page in pages:
            page.close()


def _extract_correspondence_fields(text: str) -> dict[str, str]:
    """Extract letter metadata without applying IRD validity or expiry rules."""
    values = {"number": "", "letter_date": "", "sender": "", "recipient": "", "subject": "", "summary": ""}
    cleaned = text.strip()
    if not cleaned:
        return values
    first_page = re.split(r"---\s*page\s+2\s*---", cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
    header_match = re.search(
        r"---\s*letterhead\s*---\s*(.*?)\s*---\s*document body\s*---",
        cleaned,
        re.IGNORECASE | re.DOTALL,
    )
    header_fields = _extract_correspondence_header_requisites(header_match.group(1) if header_match else "")

    if current_app.config.get("USE_AI") and current_app.config.get("OPENAI_API_KEY"):
        try:
            from openai import OpenAI

            schema = {
                "type": "object",
                "properties": {key: {"type": "string"} for key in values},
                "required": list(values),
                "additionalProperties": False,
            }
            response = OpenAI(api_key=current_app.config["OPENAI_API_KEY"]).responses.create(
                model=current_app.config["OPENAI_MODEL"],
                instructions=(
                    "Извлеки реквизиты делового письма на русском языке. Верни только данные самого письма: "
                    "номер, дату письма в ISO YYYY-MM-DD, наименование отправителя и получателя с сокращёнными "
                    "организационно-правовыми формами (например: унитарное предприятие — УП, "
                    "республиканское унитарное предприятие — РУП, общество с ограниченной ответственностью — ООО), "
                    "тему и краткое содержание в 1-3 предложениях. Учитывай шапку, исходящий номер и дату, "
                    "в первую очередь проверь верхний колонтитул первой страницы: синюю строку с исходящим номером и датой. "
                    "Номер и дату бери из одной строки, если она есть. Учитывай адресата и подпись/организацию автора. "
                    "Не выдумывай отсутствующие значения; тогда верни пустую строку. "
                    "Срок действия документа не извлекай."
                ),
                input=cleaned[:24000],
                text={"format": {"type": "json_schema", "name": "correspondence_fields", "schema": schema, "strict": True}},
            )
            extracted = json.loads(response.output_text)
            for key in values:
                if isinstance(extracted.get(key), str):
                    values[key] = extracted[key].strip()
            values["letter_date"] = _normalize_letter_date(values["letter_date"])
        except Exception:
            logger.exception("AI correspondence extraction failed; using local extraction")

    # Read the blue outgoing number/date line in the first-page letterhead as
    # one pair and let it override AI results that may come from body citations.
    for key in ("number", "letter_date"):
        if header_fields.get(key):
            values[key] = header_fields[key]

    date_pattern = r"(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:19|20)\d{2}"
    if not values["letter_date"]:
        match = re.search(rf"(?:от\s*)?({date_pattern})", first_page[:5000], re.IGNORECASE)
        if match:
            values["letter_date"] = _normalize_letter_date(match.group(1))
    if not values["number"]:
        match = re.search(r"(?:№|N(?:o|º|°)?)\s*([\wА-Яа-яЁё./\\-]{1,40})", first_page[:5000], re.IGNORECASE)
        if match:
            values["number"] = match.group(1).strip(" .,:;-")
    lines = [re.sub(r"\s+", " ", line).strip(" \t\r\n:;—-") for line in cleaned.splitlines()]
    lines = [line for line in lines if line]
    if not values["recipient"]:
        match = re.search(r"(?:^|\n)\s*((?:Директору|Руководителю|Начальнику|Кому)\b[^\n]{0,250})", cleaned, re.IGNORECASE)
        if match:
            values["recipient"] = re.sub(r"\s+", " ", match.group(1)).strip()
    if not values["sender"]:
        organization_start = (
            r"(?:РУП|ОАО|ОДО|ООО|УП|ЗАО|ЧУП|"
            r"Республиканск\w*\s+(?:дочерн\w*\s+)?унитарн\w*\s+предприяти\w*|"
            r"Коммунальн\w*\s+унитарн\w*\s+предприяти\w*|"
            r"Частн\w*\s+унитарн\w*\s+предприяти\w*|"
            r"Унитарн\w*\s+предприяти\w*|"
            r"Обществ\w*\s+с\s+(?:ограниченной|дополнительной)\s+ответственностью|"
            r"(?:Открытое|Закрытое)\s+акционерное\s+общество|"
            r"Государственн\w+ предприятие)"
        )
        match = re.search(
            rf"({organization_start}\b[^\n]{{3,180}}?)\s+"
            r"(?:просит|просим|сообщает|сообщаем|направляет|направляем|информирует|информируем)\b",
            cleaned,
            re.IGNORECASE,
        )
        if match:
            values["sender"] = re.sub(r"\s+", " ", match.group(1)).strip(" ,.;")
    if not values["subject"]:
        for i, line in enumerate(lines[:40]):
            if re.search(r"^(?:о\s+|тема\s*:|касательно\s+|по вопросу\s+)", line, re.IGNORECASE) and len(line) < 220:
                values["subject"] = re.sub(r"^тема\s*:\s*", "", line, flags=re.IGNORECASE)
                break
    if not values["summary"]:
        body = re.sub(r"\s+", " ", cleaned)
        # Skip letterhead and address fragments where possible.
        for marker in ("просит ", "просим ", "сообщает ", "сообщаем ", "направляет ", "информирует ", "информируем "):
            start = body.lower().find(marker)
            if start >= 0:
                body = body[start:]
                break
        values["summary"] = body[:600].strip()
    values["letter_date"] = _normalize_letter_date(values["letter_date"])
    for key in ("sender", "recipient"):
        values[key] = normalize_organization_forms(values[key])
    return values


def _extract_correspondence_header_requisites(header: str) -> dict[str, str]:
    """Extract the numbered date pair from the top letterhead OCR only."""
    result = {"number": "", "letter_date": ""}
    if not header.strip():
        return result
    date_pattern = re.compile(
        r"(?<!\d)([0-3]?\d)\s*[./-]\s*([01]?\d)\s*[./-]\s*((?:19|20)\d{2})(?!\d)"
    )
    number_pattern = re.compile(
        r"(?:исх(?:одящий|\.)?\s*)?(?:№|N(?:o|º|°)?|#)\s*([A-ZА-ЯЁ0-9]+(?:\s*[-./\\]\s*[A-ZА-ЯЁ0-9]+){0,6})",
        re.IGNORECASE,
    )
    lines = [re.sub(r"\s+", " ", line).strip() for line in header.splitlines() if line.strip()]
    for line in lines:
        date_match = date_pattern.search(line)
        number_match = number_pattern.search(line)
        if date_match and number_match:
            result["letter_date"] = _normalize_letter_date(
                f"{date_match.group(1)}.{date_match.group(2)}.{date_match.group(3)}"
            )
            result["number"] = re.sub(r"\s+", "", number_match.group(1)).strip(" .,:;-")
            return result
    flattened = " ".join(lines)
    date_match = date_pattern.search(flattened)
    if date_match:
        result["letter_date"] = _normalize_letter_date(
            f"{date_match.group(1)}.{date_match.group(2)}.{date_match.group(3)}"
        )
    number_match = number_pattern.search(flattened)
    if number_match:
        result["number"] = re.sub(r"\s+", "", number_match.group(1)).strip(" .,:;-")
    return result


def _apply_checklist_letter_subject(parsed: ParsedDocument, item_title: str) -> bool:
    """Use the document's explicit subject in the checklist's letter rows."""
    uses_estimate_letter = ChecklistRepository.uses_customer_letter_subject(item_title)
    uses_contract_letter = ChecklistRepository._normalize(item_title) == ChecklistRepository._normalize(
        "Письмо заказчика о заключении договора"
    )
    if not (uses_estimate_letter or uses_contract_letter):
        return False
    subject = _extract_explicit_subject(parsed.raw_text)
    if uses_estimate_letter and not subject:
        subject = _extract_correspondence_fields(parsed.raw_text).get("subject", "").strip()
    if subject:
        if uses_contract_letter:
            subject = re.sub(r"^письмо\s+", "", subject, flags=re.IGNORECASE).strip()
            if subject[:3].casefold() == "об ":
                subject = "об " + subject[3:]
            elif subject[:2].casefold() == "о ":
                subject = "о " + subject[2:]
            elif re.match(r"^(?:по\s+вопросу|касательно)\b", subject, re.IGNORECASE):
                subject = subject[:1].lower() + subject[1:]
            else:
                subject = "о " + subject[:1].lower() + subject[1:]
            parsed.working_title = f"Письмо {subject}"[:300]
        else:
            parsed.working_title = subject[:300]
            parsed.official_title = subject[:300]
        return True
    return False


def _apply_checklist_document_title(parsed: ParsedDocument, item_title: str) -> None:
    """Keep the review title anchored to the checklist row the user selected."""
    if not _apply_checklist_letter_subject(parsed, item_title):
        parsed.working_title = item_title[:300]


def _extract_explicit_subject(text: str) -> str:
    """Prefer a short standalone letter subject line over issuer or document labels."""
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" \t\r\n:;—-")
        line = re.sub(r"^(?:тема\s*:\s*)", "", line, flags=re.IGNORECASE).strip()
        if len(line) > 220:
            continue
        if re.match(r"^(?:о\s+|об\s+|по\s+вопросу\s+|касательно\s+)", line, re.IGNORECASE):
            return line.strip(" .;:—-")
    return ""


def _normalize_letter_date(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    match = re.search(r"((?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:19|20)\d{2})", value)
    if not match:
        return ""
    day, month, year = re.split(r"[./-]", match.group(1))
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _optional_int(value):
    try:
        return int(value) if value else None
    except (ValueError, TypeError):
        return None
