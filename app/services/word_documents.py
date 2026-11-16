"""Helpers for converting uploaded Word documents to PDF previews."""

from __future__ import annotations

import subprocess
import shutil
import tempfile
from pathlib import Path


WORD_SUFFIXES = {".doc", ".docx"}
WORD_MIME_TYPES = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def is_word_document(path: Path) -> bool:
    return path.suffix.lower() in WORD_SUFFIXES


def word_preview_path(source: Path) -> Path:
    """Convert a Word file to a cached PDF used for extraction and preview."""
    source = source.resolve()
    if not is_word_document(source):
        raise ValueError("Файл не является документом Word.")
    if not source.is_file():
        raise FileNotFoundError("Загруженный документ Word не найден.")

    preview_dir = source.parent / ".word-previews"
    preview = preview_dir / f"{source.name}.pdf"
    if preview.is_file() and preview.stat().st_mtime >= source.stat().st_mtime:
        return preview

    preview_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ird-word-") as temp_name:
        temp_dir = Path(temp_name)
        profile = temp_dir / "profile"
        command = [
            "libreoffice",
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(temp_dir),
            str(source),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError("Конвертер Word не установлен на сервере.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Не удалось преобразовать Word-файл за отведённое время.") from exc

        converted = temp_dir / f"{source.stem}.pdf"
        if result.returncode != 0 or not converted.is_file():
            detail = (result.stderr or result.stdout or "").strip()
            raise ValueError(f"Не удалось прочитать документ Word.{(' ' + detail[:300]) if detail else ''}")
        if not converted.read_bytes().startswith(b"%PDF-"):
            raise ValueError("Word-файл не удалось преобразовать в PDF.")
        with tempfile.NamedTemporaryFile(prefix="word-preview-", suffix=".pdf", dir=preview_dir, delete=False) as staged_file:
            staged = Path(staged_file.name)
        shutil.copyfile(converted, staged)
        staged.replace(preview)

    return preview


def remove_word_preview(source: Path) -> None:
    """Remove the cached PDF when the source Word attachment is deleted."""
    preview = source.parent / ".word-previews" / f"{source.name}.pdf"
    preview.unlink(missing_ok=True)
