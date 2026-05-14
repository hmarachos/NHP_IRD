import os
from pathlib import Path

from dotenv import load_dotenv

from .document_types import DOCUMENT_TYPES


BASE_DIR = Path(__file__).resolve().parent.parent


load_dotenv(BASE_DIR / ".env")


def resolve_project_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    DATABASE_PATH = resolve_project_path(os.getenv("DATABASE_PATH", BASE_DIR / "instance" / "documents.db"))
    UPLOAD_DIR = resolve_project_path(os.getenv("UPLOAD_DIR", BASE_DIR / "instance" / "uploads"))
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(25 * 1024 * 1024)))

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")
    USE_AI = os.getenv("USE_AI", "true").lower() in {"1", "true", "yes", "on"}

    TESSERACT_LANG = os.getenv("TESSERACT_LANG", "rus+eng")
    OCR_DPI = int(os.getenv("OCR_DPI", "300"))

    DOCUMENT_TYPES = DOCUMENT_TYPES

    @classmethod
    def ensure_directories(cls) -> None:
        cls.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cls.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
