from flask import Blueprint, abort, current_app, redirect, render_template, request, send_from_directory, url_for

from app.models import ParsedDocument
from app.repositories import DocumentRepository
from app.services.pipeline import DocumentProcessingPipeline
from app.services.storage import StorageService


bp = Blueprint("web", __name__)


def build_contract_groups(documents):
    groups_by_contract = {}
    for document in documents:
        contract_number = document["contract_number"]
        group = groups_by_contract.setdefault(
            contract_number,
            {
                "contract_number": contract_number,
                "object_names": [],
                "documents": [],
            },
        )
        object_name = document["object_name"]
        if object_name and object_name not in group["object_names"]:
            group["object_names"].append(object_name)
        group["documents"].append(document)

    return list(groups_by_contract.values())


@bp.get("/")
def index():
    documents = DocumentRepository().list_grouped()
    return render_template("index.html", contract_groups=build_contract_groups(documents))


@bp.post("/documents")
def upload_document():
    contract_number = request.form.get("contract_number", "").strip()
    object_name = request.form.get("object_name", "").strip()
    note = request.form.get("note", "").strip()
    file = request.files.get("pdf_file")

    if not contract_number or not object_name or not file:
        return render_template(
            "index.html",
            contract_groups=build_contract_groups(DocumentRepository().list_grouped()),
            error="Заполните номер договора, наименование объекта и выберите PDF-файл.",
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
            contract_number=contract_number,
            object_name=object_name,
            original_filename=original_name,
            stored_filename=stored_name,
            parsed=parsed,
            note=note,
        )
    except Exception as exc:
        parsed = ParsedDocument()
        document_id = repository.create(
            contract_number=contract_number,
            object_name=object_name,
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
    return render_template("document.html", document=document)


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

    stored_filename = document["stored_filename"]
    deleted = repository.delete(document_id)
    if deleted and stored_filename:
        upload_dir = current_app.config["UPLOAD_DIR"].resolve()
        stored_path = (upload_dir / stored_filename).resolve()
        if upload_dir in stored_path.parents and stored_path.exists():
            stored_path.unlink()

    return redirect(url_for("web.index"))
