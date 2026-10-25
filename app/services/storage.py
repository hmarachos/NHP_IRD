from pathlib import Path
from uuid import uuid4

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


class StorageService:
    def __init__(self, upload_dir: Path, max_file_size: int = 25 * 1024 * 1024):
        self.upload_dir = upload_dir
        self.max_file_size = max_file_size

    def save_pdf(self, file: FileStorage) -> tuple[str, str, Path]:
        original, stored, path, _mime = self.save_upload(file)
        return original, stored, path

    def save_upload(self, file: FileStorage) -> tuple[str, str, Path, str]:
        original_name = file.filename or "document.pdf"
        safe_name = secure_filename(original_name)
        suffix = Path(safe_name).suffix.lower()
        allowed = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
        if suffix not in allowed:
            raise ValueError("Поддерживаются PDF, JPG, PNG, WebP и HEIC.")

        content = file.stream.read()
        file.stream.seek(0)
        if not content:
            raise ValueError("Файл пустой.")
        if len(content) > self.max_file_size:
            raise ValueError("Размер файла превышает допустимые 25 МБ.")
        mime_type = self._validate_content(content, suffix)

        stored_name = f"{uuid4().hex}_{safe_name}"
        path = self.upload_dir / stored_name
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        file.save(path)
        return original_name, stored_name, path, mime_type

    @staticmethod
    def _validate_content(content: bytes, suffix: str) -> str:
        if suffix == ".pdf":
            if not content.lstrip().startswith(b"%PDF-"):
                raise ValueError("Содержимое файла не похоже на PDF.")
            return "application/pdf"

        try:
            from PIL import Image

            if suffix in {".heic", ".heif"}:
                try:
                    import pillow_heif
                except ImportError as exc:
                    raise ValueError("Для чтения HEIC установите pillow-heif.") from exc
                pillow_heif.register_heif_opener()
            import io

            expected_formats = {
                ".jpg": {"JPEG"}, ".jpeg": {"JPEG"}, ".png": {"PNG"},
                ".webp": {"WEBP"}, ".heic": {"HEIF", "HEIC"},
                ".heif": {"HEIF", "HEIC"},
            }
            with Image.open(io.BytesIO(content)) as image:
                image_format = (image.format or "").upper()
                if image_format not in expected_formats[suffix]:
                    raise ValueError("Расширение файла не соответствует содержимому.")
                if image.width * image.height > 40_000_000:
                    raise ValueError("Изображение слишком большое (более 40 мегапикселей).")
                image.verify()
                formats = {
                    "JPEG": "image/jpeg",
                    "PNG": "image/png",
                    "WEBP": "image/webp",
                    "HEIF": "image/heic",
                    "HEIC": "image/heic",
                }
                mime_type = formats.get((image.format or "").upper())
                if not mime_type:
                    raise ValueError("Неподдерживаемый формат изображения.")
                return mime_type
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Не удалось прочитать изображение. Проверьте формат файла.") from exc
