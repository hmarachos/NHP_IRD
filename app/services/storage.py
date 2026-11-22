from pathlib import Path
from uuid import uuid4

from app.services.filenames import secure_stem


MAX_UPLOAD_FILE_SIZE = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 60_000_000


class StorageService:
    def __init__(self, upload_dir: Path, max_file_size: int | None = None):
        self.upload_dir = upload_dir
        self.max_file_size = MAX_UPLOAD_FILE_SIZE if max_file_size is None else max_file_size

    def save_pdf(self, file) -> tuple[str, str, Path]:
        original, stored, path, _mime = self.save_upload(file)
        return original, stored, path

    def save_upload(self, file) -> tuple[str, str, Path, str]:
        original_name = getattr(file, "filename", None) or getattr(file, "name", None) or "document.pdf"
        basename = original_name.replace("\\", "/").rsplit("/", 1)[-1]
        original_path = Path(basename)
        suffix = original_path.suffix.lower()
        allowed = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".doc", ".docx"}
        if suffix not in allowed:
            raise ValueError("Поддерживаются PDF, DOC, DOCX, JPG, PNG, WebP и HEIC.")
        safe_stem = secure_stem(original_path.stem)
        safe_name = f"{safe_stem}{suffix}"

        stream = getattr(file, "stream", None) or getattr(file, "file", None)
        if stream is None:
            raise ValueError("Не удалось прочитать загруженный файл.")
        stored_name = f"{uuid4().hex}_{safe_name}"
        path = self.upload_dir / stored_name
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        try:
            stream.seek(0)
            written = 0
            with path.open("wb") as destination:
                while chunk := stream.read(1024 * 1024):
                    if written + len(chunk) > self.max_file_size:
                        raise ValueError("Размер файла не должен превышать 15 МиБ.")
                    destination.write(chunk)
                    written += len(chunk)
            if not written:
                raise ValueError("Файл пустой.")
            with path.open("rb") as source:
                mime_type = self._validate_stream(source, suffix)
            return original_name, stored_name, path, mime_type
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            stream.seek(0)

    @staticmethod
    def _validate_content(content: bytes, suffix: str) -> str:
        import io
        return StorageService._validate_stream(io.BytesIO(content), suffix)

    @staticmethod
    def _validate_stream(source, suffix: str) -> str:
        content = source.read(1024)
        source.seek(0)
        if suffix == ".pdf":
            if not content.lstrip().startswith(b"%PDF-"):
                raise ValueError("Содержимое файла не похоже на PDF.")
            return "application/pdf"

        if suffix == ".docx":
            import io
            import zipfile

            try:
                with zipfile.ZipFile(source) as archive:
                    if "word/document.xml" not in archive.namelist():
                        raise ValueError("В DOCX-файле отсутствует документ Word.")
                    if archive.testzip() is not None:
                        raise ValueError("Файл DOCX повреждён.")
            except zipfile.BadZipFile as exc:
                raise ValueError("Содержимое файла не похоже на DOCX.") from exc
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

        if suffix == ".doc":
            if content[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
                raise ValueError("Содержимое файла не похоже на DOC.")
            return "application/msword"

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
            with Image.open(source) as image:
                image_format = (image.format or "").upper()
                if image_format not in expected_formats[suffix]:
                    raise ValueError("Расширение файла не соответствует содержимому.")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("Размер изображения превышает допустимое число пикселей.")
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
