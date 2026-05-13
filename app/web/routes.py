from flask import Blueprint, current_app, redirect, render_template, request, url_for

from app.models import ParsedDocument
from app.repositories import DocumentRepository
from app.services.pipeline import DocumentProcessingPipeline
from app.services.storage import StorageService


bp = Blueprint("web", __name__)


@bp.get("/")
def index():
    documents = DocumentRepository().list_recent()
    return render_template("index.html", documents=documents)


@bp.post("/documents")
def upload_document():
    contract_number = request.form.get("contract_number", "").strip()
    object_name = request.form.get("object_name", "").strip()
    file = request.files.get("pdf_file")

    if not contract_number or not object_name or not file:
        return render_template(
            "index.html",
            documents=DocumentRepository().list_recent(),
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
        )
    except Exception as exc:
        parsed = ParsedDocument()
        document_id = repository.create(
            contract_number=contract_number,
            object_name=object_name,
            original_filename=file.filename or "",
            stored_filename="",
            parsed=parsed,
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
