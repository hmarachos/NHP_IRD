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
    ACTS_DIR = resolve_project_path(os.getenv("ACTS_DIR", BASE_DIR / "instance" / "acts"))
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(25 * 1024 * 1024)))
    MAX_UPLOAD_FILE_SIZE = int(os.getenv("MAX_UPLOAD_FILE_SIZE", str(25 * 1024 * 1024)))

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
    USE_AI = os.getenv("USE_AI", "true").lower() in {"1", "true", "yes", "on"}

    TESSERACT_LANG = os.getenv("TESSERACT_LANG", "rus+eng")
    OCR_DPI = int(os.getenv("OCR_DPI", "300"))
    EXPIRY_WARNING_DAYS = int(os.getenv("EXPIRY_WARNING_DAYS", "30"))

    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
    MAIL_APP_PASSWORD = os.getenv("MAIL_APP_PASSWORD", "")
    MAIL_FROM = os.getenv("MAIL_FROM", "") or MAIL_USERNAME
    MAIL_RECIPIENT = os.getenv("MAIL_RECIPIENT", "a.kazantsev@beloil.by")
    MAIL_TIMEZONE = os.getenv("MAIL_TIMEZONE", "Europe/Minsk")
    MAIL_SEND_HOUR = int(os.getenv("MAIL_SEND_HOUR", "8"))

    DOCUMENT_TYPES = DOCUMENT_TYPES

    @classmethod
    def ensure_directories(cls) -> None:
        cls.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cls.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cls.ACTS_DIR.mkdir(parents=True, exist_ok=True)
