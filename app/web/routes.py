import hashlib
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from app.models import ParsedDocument
from app.repositories import ChecklistRepository, DocumentRepository, ObjectRepository
from app.services.expiry import attach_expiry, expiry_state, iso_date
from app.services.pipeline import DocumentProcessingPipeline
from app.services.storage import StorageService


bp = Blueprint("web", __name__)


@bp.get("/")
def index():
    warning_days = current_app.config["EXPIRY_WARNING_DAYS"]
    documents = DocumentRepository().list_all()
    alerts = [
        {"document": document, "expiry": state}
        for document in documents
        if (state := expiry_state(document, warning_days))["key"] in {"expired", "expiring"}
    ]
    return render_template(
        "index.html",
        objects=ObjectRepository().list_all(),
        archived_objects=ObjectRepository().list_archived(),
        templates=ChecklistRepository().list_templates(),
        alerts=alerts,
        warning_days=warning_days,
    )


@bp.post("/objects")
def create_object():
    contract_number = request.form.get("contract_number", "").strip()
    name = request.form.get("name", "").strip()
    if not contract_number or not name:
        return _index_error("Заполните номер договора и наименование проекта.")
    object_id = ObjectRepository().create(
        contract_number=contract_number,
        name=name,
        template_id=_optional_int(request.form.get("template_id")),
    )
    return redirect(url_for("web.object_workspace", object_id=object_id))


@bp.post("/objects/<int:object_id>/update")
def update_object(object_id: int):
    contract_number = request.form.get("contract_number", "").strip()
    name = request.form.get("name", "").strip()
    if not contract_number or not name:
        return _index_error("Заполните номер договора и наименование проекта.")
    if not ObjectRepository().update(object_id, contract_number=contract_number, name=name):
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
    stored_filenames = ObjectRepository().permanently_delete_archived(object_id)
    if stored_filenames is None:
        abort(404)

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
    checklist = ChecklistRepository().list_project_items(object_id)
    archived_documents = DocumentRepository().list_archived_by_object(object_id)
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
    if not ChecklistRepository().update_project_item(object_id, item_id, status, document_id):
        abort(404)
    return redirect(url_for("web.object_workspace", object_id=object_id) + "#checklist")


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
    pipeline = DocumentProcessingPipeline(
        document_types=current_app.config["DOCUMENT_TYPES"],
        tesseract_lang=current_app.config["TESSERACT_LANG"],
        ocr_dpi=current_app.config["OCR_DPI"],
    )
    created = []
    note = request.form.get("note", "").strip()
    for file in files:
        original_name = file.filename or "document"
        stored_name, mime_type, path = "", "application/octet-stream", None
        parsed = ParsedDocument()
        try:
            original_name, stored_name, path, mime_type = storage.save_upload(file)
            parsed = pipeline.process(path)
            document_id = repository.create(
                object_id=object_id,
                contract_number=obj["contract_number"],
                object_name=obj["name"],
                original_filename=original_name,
                stored_filename=stored_name,
                mime_type=mime_type,
                parsed=parsed,
                note=note,
            )
        except Exception as exc:
            # Keep the original even when OCR or extraction fails.
            document_id = repository.create(
                object_id=object_id,
                contract_number=obj["contract_number"],
                object_name=obj["name"],
                original_filename=original_name,
                stored_filename=stored_name,
                mime_type=mime_type,
                parsed=parsed,
                note=note,
                status="error",
                error=str(exc),
            )
        created.append(document_id)
    if len(created) == 1:
        return redirect(url_for("web.document_detail", document_id=created[0]))
    flash(f"Добавлено файлов: {len(created)}.", "success")
    return redirect(url_for("web.object_workspace", object_id=object_id))


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
        "issue_date": request.form.get("issue_date", "").strip(),
        "valid_until": "" if no_expiration else request.form.get("valid_until", "").strip(),
        "no_expiration": int(no_expiration),
    }
    if not DocumentRepository().update_fields(document_id, values):
        abort(404)
    if document["object_id"]:
        ChecklistRepository().match_document(document["object_id"], document_id, values["working_title"])
    return redirect(url_for("web.document_detail", document_id=document_id))


@bp.post("/documents/<int:document_id>/note")
def update_document_note(document_id: int):
    if not DocumentRepository().update_note(document_id, request.form.get("note", "").strip()):
        abort(404)
    return redirect(url_for("web.document_detail", document_id=document_id))


@bp.get("/documents/<int:document_id>/preview")
def document_preview(document_id: int):
    document = DocumentRepository().get(document_id)
    if document is None or document["archived_at"] or not document["stored_filename"]:
        abort(404)
    return send_from_directory(
        current_app.config["UPLOAD_DIR"], document["stored_filename"],
        mimetype=document["mime_type"] or "application/pdf", as_attachment=False,
        download_name=document["original_filename"] or document["stored_filename"],
    )


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
    checklist = ChecklistRepository().list_project_items(object_id)
    archive = io.BytesIO()
    manifest = {"created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z", "files": []}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("project.json", json.dumps(dict(obj), ensure_ascii=False, indent=2))
        bundle.writestr("documents.json", json.dumps([dict(row) for row in documents], ensure_ascii=False, indent=2))
        bundle.writestr("checklist.json", json.dumps([dict(row) for row in checklist], ensure_ascii=False, indent=2))
        upload_dir = current_app.config["UPLOAD_DIR"].resolve()
        for document in documents:
            stored_name = document["stored_filename"]
            candidate = Path(stored_name) if stored_name else None
            path = (upload_dir / candidate).resolve() if candidate else None
            if path is None or candidate.is_absolute() or candidate.name != stored_name or path.parent != upload_dir or not path.is_file():
                manifest["files"].append({"document_id": document["id"], "missing": True})
                continue
            member = f"originals/{document['id']}-{path.name}"
            content = path.read_bytes()
            bundle.writestr(member, content)
            manifest["files"].append({
                "document_id": document["id"], "path": member,
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
            })
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    response = make_response(archive.getvalue())
    filename = secure_filename(obj["name"]) or f"project-{object_id}"
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f'attachment; filename="NHP_IRD-{filename}.zip"'
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
    return redirect(url_for("web.manage_templates") + f"#template-{template_id}")


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
    return redirect(url_for("web.manage_templates"))


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
    return render_template("index.html", objects=ObjectRepository().list_all(), archived_objects=ObjectRepository().list_archived(), templates=ChecklistRepository().list_templates(), alerts=alerts, warning_days=warning_days, error=message), 400


def _object_error(object_id: int, message: str):
    repo = ChecklistRepository()
    checklist = repo.list_project_items(object_id)
    documents = [attach_expiry(row, current_app.config["EXPIRY_WARNING_DAYS"]) for row in DocumentRepository().list_by_object(object_id)]
    archived_documents = DocumentRepository().list_archived_by_object(object_id)
    alerts = [{"document": row, "expiry": row["expiry"]} for row in documents if row["expiry"]["key"] in {"expired", "expiring"}]
    return render_template("object.html", object=ObjectRepository().get(object_id), documents=documents, archived_documents=archived_documents, checklist=checklist, templates=repo.list_templates(), alerts=alerts, missing_count=sum(row["status"] == "missing" for row in checklist), received_count=sum(row["status"] == "received" for row in checklist), error=message), 400


def _optional_int(value):
    try:
        return int(value) if value else None
    except (ValueError, TypeError):
        return None
