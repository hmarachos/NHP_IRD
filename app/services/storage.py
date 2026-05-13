from pathlib import Path
from uuid import uuid4

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


class StorageService:
    def __init__(self, upload_dir: Path):
        self.upload_dir = upload_dir

    def save_pdf(self, file: FileStorage) -> tuple[str, str, Path]:
        original_name = file.filename or "document.pdf"
        safe_name = secure_filename(original_name)
        if not safe_name.lower().endswith(".pdf"):
            raise ValueError("Можно загружать только PDF-файлы.")

        stored_name = f"{uuid4().hex}_{safe_name}"
        path = self.upload_dir / stored_name
        file.save(path)
        return original_name, stored_name, path
