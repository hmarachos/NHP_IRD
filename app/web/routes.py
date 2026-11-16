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
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, send_file, send_from_directory, url_for
from werkzeug.utils import secure_filename

from app.models import ParsedDocument
from app.repositories import ChecklistRepository, CorrespondenceRepository, DocumentRepository, HandoverActRepository, ObjectRepository
from app.services.expiry import attach_expiry, expiry_state, iso_date
from app.services.document_generator import DOCUMENT_TYPES, generate_project_document
from app.services.pdf_text import PdfTextExtractor
from app.services.storage import StorageService


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
    name = request.form.get("name", "").strip()
    customer = request.form.get("customer", "").strip()
    if not contract_number or not name:
        return _index_error("Заполните номер договора и наименование проекта.")
    object_id = ObjectRepository().create(
        contract_number=contract_number,
        name=name,
        customer=customer,
        template_id=_optional_int(request.form.get("template_id")),
    )
    return redirect(url_for("web.object_workspace", object_id=object_id))


@bp.post("/objects/<int:object_id>/update")
def update_object(object_id: int):
    contract_number = request.form.get("contract_number", "").strip()
    name = request.form.get("name", "").strip()
    customer = request.form.get("customer", "").strip()
    if not contract_number or not name:
        return _index_error("Заполните номер договора и наименование проекта.")
    if not ObjectRepository().update(object_id, contract_number=contract_number, name=name, customer=customer):
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
    archived_documents = [attach_expiry(row, warning_days) for row in DocumentRepository().list_archived_by_object(object_id)]
    alerts = [
        {"document": row, "expiry": row["expiry"]}
        for row in documents if row["expiry"]["key"] in {"expired", "expiring"}
    ]
    return render_template(
        "object.html",
        object=obj,
        documents=documents,
        archived_documents=archived_documents,
        checklist=checklist,
        templates=ChecklistRepository().list_templates(),
        alerts=alerts,
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
    return render_template(
        "document_generation.html",
        project=project,
        kind=kind,
        spec=DOCUMENT_TYPES[kind],
        checklist=checklist,
        documents=documents,
        missing_count=sum(item["status"] == "missing" for item in checklist),
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
    if kind == "request" and missing_count == 0:
        flash("В чек-листе проекта нет недостающих документов для запроса.", "error")
        return redirect(url_for("web.project_document_form", object_id=object_id, kind=kind))
    if kind == "handover" and not documents:
        flash("Добавьте документы проекта перед созданием акта приёма-передачи.", "error")
        return redirect(url_for("web.project_document_form", object_id=object_id, kind=kind))

    fields = {
        name: request.form.get(name, "").strip()[:250]
        for name in ("recipient", "sender", "sender_signatory", "number", "date")
    }
    content = generate_project_document(kind, project, documents, checklist, fields)
    project_slug = secure_filename(project["name"]) or f"project-{object_id}"
    filename = f"{DOCUMENT_TYPES[kind]['filename']}_{project_slug}.docx"
    if kind == "handover":
        now = datetime.now(timezone.utc).replace(microsecond=0)
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        local_stamp = now.astimezone(ZoneInfo(current_app.config["MAIL_TIMEZONE"]))
        filename = f"Akt_IRD_manual_{local_stamp:%Y%m%d_%H%M%S}_{uuid4().hex[:8]}_{project_slug}.docx"
        project_dir = Path(current_app.config["ACTS_DIR"]) / str(object_id)
        project_dir.mkdir(parents=True, exist_ok=True)
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


@bp.post("/objects/<int:object_id>/template")
def apply_template(object_id: int):
    if ObjectRepository().get(object_id) is None:
        abort(404)
    template_id = _optional_int(request.form.get("template_id"))
    if not template_id or ChecklistRepository().get_template(template_id) is None:
        flash("Выберите существующий шаблон.", "error")
        return redirect(url_for("web.object_workspace", object_id=object_id))
    added = ChecklistRepository().apply_template(object_id, template_id)
    flash(f"Шаблон применён: добавлено новых пунктов — {added}.", "success")
    return redirect(url_for("web.object_workspace", object_id=object_id))


@bp.post("/objects/<int:object_id>/checklist/<int:item_id>")
def update_checklist_item(object_id: int, item_id: int):
    status = request.form.get("status", "missing")
    document_id = _optional_int(request.form.get("document_id"))
    issue_date = request.form.get("issue_date") if "issue_date" in request.form else None
    if not ChecklistRepository().update_project_item(object_id, item_id, status, document_id, issue_date):
        abort(404)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.object_workspace", object_id=object_id) + "#checklist")


@bp.post("/objects/<int:object_id>/checklist/<int:item_id>/upload")
def upload_checklist_document(object_id: int, item_id: int):
    obj = ObjectRepository().get(object_id)
    item = next((row for row in ChecklistRepository().list_project_items(object_id) if row["id"] == item_id), None)
    if obj is None or obj["archived_at"] or item is None:
        abort(404)
    files = [file for file in request.files.getlist("files") if file and file.filename]
    if not files:
        flash("Выберите PDF или фотографии страниц документа.", "error")
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
        if len(saved) == 1 and saved[0][3] == "application/pdf":
            final_name, final_stored, final_path, final_mime = saved[0]
        else:
            if any(not mime.startswith("image/") for _name, _stored, _path, mime in saved):
                raise ValueError("Для многостраничного документа выберите несколько фотографий или один PDF.")
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
                ChecklistRepository().update_project_item(object_id, item_id, "received", existing["id"])
                flash("Такой файл уже был загружен. Существующий документ привязан к строке комплектности.", "success")
                return redirect(url_for("web.document_detail", document_id=existing["id"]))
    document_id = repository.create(
        object_id=object_id, contract_number=obj["contract_number"], object_name=obj["name"],
        original_filename=final_name, stored_filename=final_stored, mime_type=final_mime,
        parsed=ParsedDocument(), note="", status="queued", error="",
    )
    ChecklistRepository().update_project_item(object_id, item_id, "received", document_id)
    flash(f"Файл «{item['title']}» принят. Распознавание выполняется в фоне.", "success")
    return redirect(url_for("web.document_detail", document_id=document_id))


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
    return redirect(url_for("web.object_workspace", object_id=object_id, tab="documents"))


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
        flash("Выберите PDF или фотографии страниц письма.", "error")
        return redirect(url_for("web.object_workspace", object_id=object_id, tab=target_tab))

    storage = StorageService(current_app.config["UPLOAD_DIR"], max_file_size=current_app.config["MAX_UPLOAD_FILE_SIZE"])
    saved_files = []
    for file in files:
        try:
            saved_files.append(storage.save_upload(file))
        except Exception as exc:
            flash(f"{file.filename}: {exc}", "error")

    image_files = [item for item in saved_files if item[3].startswith("image/")]
    pdf_files = [item for item in saved_files if item[3] == "application/pdf"]
    if image_files:
        combined_path = Path(current_app.config["UPLOAD_DIR"]) / f"{uuid4().hex}_letter-pages.pdf"
        try:
            _combine_image_uploads(image_files, combined_path)
            pdf_files.append((f"Письмо — {len(image_files)} страниц.pdf", combined_path.name, combined_path, "application/pdf"))
        except Exception as exc:
            flash(f"Не удалось собрать фотографии письма в PDF: {exc}", "error")
        finally:
            for _name, _stored, source_path, _mime in image_files:
                source_path.unlink(missing_ok=True)

    extractor = PdfTextExtractor(tesseract_lang=current_app.config["TESSERACT_LANG"], dpi=current_app.config["OCR_DPI"])
    repository = CorrespondenceRepository()
    created = 0
    for original_name, stored_name, path, mime_type in pdf_files:
        raw_text = ""
        values = {"direction": direction, "number": "", "letter_date": "", "sender": "", "recipient": "", "subject": "", "summary": ""}
        status, error = "processed", ""
        try:
            raw_text = extractor.extract(path)
            values.update(_extract_correspondence_fields(raw_text))
            if not raw_text.strip():
                raise ValueError("В письме не удалось распознать текст.")
        except Exception as exc:
            status, error = "error", str(exc)
            logger.exception("Correspondence OCR or extraction failed")
        repository.create(
            object_id=object_id, **values, original_filename=original_name,
            stored_filename=stored_name, mime_type=mime_type, raw_text=raw_text,
            status=status, error=error, record_type=record_type,
        )
        created += 1
    if created:
        flash(f"Добавлено писем: {created}. Проверьте распознанные реквизиты.", "success")
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
        sender=request.form.get("sender", "").strip()[:500],
        recipient=request.form.get("recipient", "").strip()[:500],
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


@bp.get("/documents/<int:document_id>")
def document_detail(document_id: int):
    document = DocumentRepository().get(document_id)
    if document is None or document["archived_at"]:
        abort(404)
    obj = ObjectRepository().get(document["object_id"]) if document["object_id"] else None
    return render_template(
        "document.html",
        document=attach_expiry(document, current_app.config["EXPIRY_WARNING_DAYS"]),
        object=obj,
        issue_date_iso=iso_date(document["issue_date"]),
        valid_until_iso=iso_date(document["valid_until"]),
    )


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
    if document["object_id"]:
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
    response = send_from_directory(
        current_app.config["UPLOAD_DIR"], document["stored_filename"],
        mimetype=document["mime_type"] or "application/pdf", as_attachment=False,
        download_name=document["original_filename"] or document["stored_filename"],
    )
    response.headers["Content-Disposition"] = "inline; filename*=UTF-8''" + quote(document["original_filename"] or document["stored_filename"])
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.post("/documents/<int:document_id>/delete")
def delete_document(document_id: int):
    repository = DocumentRepository()
    document = repository.get(document_id)
    if document is None:
        abort(404)
    object_id = document["object_id"]
    deleted = repository.delete(document_id)
    if not deleted:
        abort(404)
    if object_id:
        return redirect(url_for("web.object_workspace", object_id=object_id))
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
    documents = DocumentRepository().list_by_object(object_id, include_archived=True)
    correspondence_repo = CorrespondenceRepository()
    letters = correspondence_repo.list_by_object(object_id)
    approvals = correspondence_repo.list_by_object(object_id, "approval")
    checklist = ChecklistRepository().list_project_items(object_id)
    acts = HandoverActRepository().list_by_object(object_id)
    archive = io.BytesIO()
    manifest = {"created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z", "files": []}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("Проект.json", json.dumps(dict(obj), ensure_ascii=False, indent=2))
        bundle.writestr("Перечень_ИРД.json", json.dumps([dict(row) for row in documents], ensure_ascii=False, indent=2))
        bundle.writestr("Переписка.json", json.dumps([dict(row) for row in letters], ensure_ascii=False, indent=2))
        bundle.writestr("Согласования.json", json.dumps([dict(row) for row in approvals], ensure_ascii=False, indent=2))
        bundle.writestr("Комплектность.json", json.dumps([dict(row) for row in checklist], ensure_ascii=False, indent=2))
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


@bp.get("/templates")
def manage_templates():
    repo = ChecklistRepository()
    templates = [{"template": row, "items": repo.list_template_items(row["id"])} for row in repo.list_templates()]
    return render_template("templates.html", templates=templates)


@bp.post("/templates")
def create_template():
    name, description = request.form.get("name", "").strip(), request.form.get("description", "").strip()
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
    name, description = request.form.get("name", "").strip(), request.form.get("description", "").strip()
    if not name or not ChecklistRepository().update_template(template_id, name, description):
        abort(400)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("web.manage_templates") + f"#template-{template_id}")


@bp.post("/templates/<int:template_id>/copy")
def copy_template(template_id: int):
    repo = ChecklistRepository()
    if repo.get_template(template_id) is None:
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        source = repo.get_template(template_id)
        name = f"{source['name']} — копия"
    try:
        copied_id = repo.copy_template(template_id, name)
    except Exception:
        flash("Шаблон с таким названием уже существует. Укажите другое название.", "error")
        return redirect(url_for("web.manage_templates") + f"#template-{template_id}")
    if copied_id is None:
        abort(404)
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
    title, description = request.form.get("title", "").strip(), request.form.get("description", "").strip()
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
    archived_documents = [attach_expiry(row, warning_days) for row in DocumentRepository().list_archived_by_object(object_id)]
    alerts = [{"document": row, "expiry": row["expiry"]} for row in documents if row["expiry"]["key"] in {"expired", "expiring"}]
    return render_template("object.html", object=ObjectRepository().get(object_id), documents=documents, archived_documents=archived_documents, checklist=checklist, templates=repo.list_templates(), alerts=alerts, missing_count=sum(row["status"] == "missing" for row in checklist), received_count=sum(row["status"] == "received" for row in checklist), warning_days=warning_days, error=message), 400


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
                    "номер, дату письма в ISO YYYY-MM-DD, полное наименование отправителя, получателя, "
                    "тему и краткое содержание в 1-3 предложениях. Учитывай шапку, исходящий номер и дату, "
                    "адресата и подпись/организацию автора. Не выдумывай отсутствующие значения; тогда верни пустую строку. "
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

    date_pattern = r"(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:19|20)\d{2}"
    if not values["letter_date"]:
        match = re.search(rf"(?:от\s*)?({date_pattern})", cleaned, re.IGNORECASE)
        if match:
            values["letter_date"] = _normalize_letter_date(match.group(1))
    if not values["number"]:
        match = re.search(r"(?:№|N(?:o|º|°)?)\s*([\wА-Яа-яЁё./-]{1,40})", cleaned, re.IGNORECASE)
        if match:
            values["number"] = match.group(1).strip(" .,:;-")
    lines = [re.sub(r"\s+", " ", line).strip(" \t\r\n:;—-") for line in cleaned.splitlines()]
    lines = [line for line in lines if line]
    if not values["recipient"]:
        match = re.search(r"(?:^|\n)\s*((?:Директору|Руководителю|Начальнику|Кому)\b[^\n]{0,250})", cleaned, re.IGNORECASE)
        if match:
            values["recipient"] = re.sub(r"\s+", " ", match.group(1)).strip()
    if not values["sender"]:
        match = re.search(r"((?:РУП|ОАО|ОДО|ООО|УП|ЗАО|Государственн\w+ предприятие)[^\n]{3,180}?)\s+(?:просит|просим|сообщает|сообщаем|направляет|направляем|информирует|информируем)\b", cleaned, re.IGNORECASE)
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
    return values


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
