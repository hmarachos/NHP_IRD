from flask import Blueprint, abort, current_app, redirect, render_template, request, send_from_directory, url_for

from app.models import ParsedDocument
from app.repositories import DocumentRepository, ObjectRepository
from app.services.pipeline import DocumentProcessingPipeline
from app.services.storage import StorageService


bp = Blueprint("web", __name__)


@bp.get("/")
def index():
    objects = ObjectRepository().list_all()
    return render_template("index.html", objects=objects)


@bp.post("/objects")
def create_object():
    contract_number = request.form.get("contract_number", "").strip()
    name = request.form.get("name", "").strip()

    if not contract_number or not name:
        return render_template(
            "index.html",
            objects=ObjectRepository().list_all(),
            error="Заполните номер договора и наименование объекта.",
        ), 400

    object_id = ObjectRepository().create(
        contract_number=contract_number,
        name=name,
    )
    return redirect(url_for("web.object_workspace", object_id=object_id))


@bp.post("/objects/<int:object_id>/update")
def update_object(object_id: int):
    contract_number = request.form.get("contract_number", "").strip()
    name = request.form.get("name", "").strip()

    if not contract_number or not name:
        return render_template(
            "index.html",
            objects=ObjectRepository().list_all(),
            error="Заполните номер договора и наименование объекта.",
        ), 400

    updated = ObjectRepository().update(
        object_id,
        contract_number=contract_number,
        name=name,
    )
    if not updated:
        abort(404)
    return redirect(url_for("web.index"))


@bp.post("/objects/<int:object_id>/delete")
def delete_object(object_id: int):
    object_repository = ObjectRepository()
    document_repository = DocumentRepository()
    obj = object_repository.get(object_id)
    if obj is None:
        abort(404)

    stored_filenames = document_repository.list_stored_filenames_by_object(object_id)
    document_repository.delete_by_object(object_id)
    deleted = object_repository.delete(object_id)
    if deleted:
        delete_stored_files(stored_filenames)

    return redirect(url_for("web.index"))


@bp.get("/objects/<int:object_id>")
def object_workspace(object_id: int):
    obj = ObjectRepository().get(object_id)
    if obj is None:
        return "Объект не найден", 404

    documents = DocumentRepository().list_by_object(object_id)
    return render_template("object.html", object=obj, documents=documents)


@bp.post("/objects/<int:object_id>/documents")
def upload_document(object_id: int):
    obj = ObjectRepository().get(object_id)
    if obj is None:
        abort(404)

    note = request.form.get("note", "").strip()
    file = request.files.get("pdf_file")

    if not file:
        return render_template(
            "object.html",
            object=obj,
            documents=DocumentRepository().list_by_object(object_id),
            error="Выберите PDF-файл.",
        ), 400

    storage = StorageService(current_app.config["UPLOAD_DIR"])
    repository = DocumentRepository()

    try:
        original_name, stored_name, path = storage.save_pdf(file)
        pipeline = DocumentProcessingPipeline(
            document_types=current_app.config["DOCUMENT_TYPES"],
            tesseract_lang=current_app.config["TESSERACT_LANG"],
            ocr_dpi=current_app.config["OCR_DPI"],
        )
        parsed = pipeline.process(path)
        document_id = repository.create(
            object_id=object_id,
            contract_number=obj["contract_number"],
            object_name=obj["name"],
            original_filename=original_name,
            stored_filename=stored_name,
            parsed=parsed,
            note=note,
        )
    except Exception as exc:
        parsed = ParsedDocument()
        document_id = repository.create(
            object_id=object_id,
            contract_number=obj["contract_number"],
            object_name=obj["name"],
            original_filename=file.filename or "",
            stored_filename="",
            parsed=parsed,
            note=note,
            status="error",
            error=str(exc),
        )

    return redirect(url_for("web.document_detail", document_id=document_id))


@bp.get("/documents/<int:document_id>")
def document_detail(document_id: int):
    document = DocumentRepository().get(document_id)
    if document is None:
        return "Документ не найден", 404
    obj = ObjectRepository().get(document["object_id"]) if document["object_id"] else None
    return render_template("document.html", document=document, object=obj)


@bp.post("/documents/<int:document_id>/note")
def update_document_note(document_id: int):
    note = request.form.get("note", "").strip()
    updated = DocumentRepository().update_note(document_id, note)
    if not updated:
        abort(404)
    return redirect(url_for("web.document_detail", document_id=document_id))


@bp.get("/documents/<int:document_id>/preview")
def document_preview(document_id: int):
    document = DocumentRepository().get(document_id)
    if document is None or not document["stored_filename"]:
        abort(404)

    upload_dir = current_app.config["UPLOAD_DIR"]
    return send_from_directory(
        upload_dir,
        document["stored_filename"],
        mimetype="application/pdf",
        as_attachment=False,
        download_name=document["original_filename"] or document["stored_filename"],
    )


@bp.post("/documents/<int:document_id>/delete")
def delete_document(document_id: int):
    repository = DocumentRepository()
    document = repository.get(document_id)
    if document is None:
        abort(404)

    object_id = document["object_id"]
    stored_filename = document["stored_filename"]
    deleted = repository.delete(document_id)
    if deleted and stored_filename:
        delete_stored_files([stored_filename])

    if object_id:
        return redirect(url_for("web.object_workspace", object_id=object_id))

    return redirect(url_for("web.index"))


def delete_stored_files(stored_filenames: list[str]) -> None:
    upload_dir = current_app.config["UPLOAD_DIR"].resolve()
    for stored_filename in stored_filenames:
        stored_path = (upload_dir / stored_filename).resolve()
        if upload_dir in stored_path.parents and stored_path.exists():
            stored_path.unlink()
