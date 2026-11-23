"""Conservative recovery for scanned PDFs whose cross-reference data is broken."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import tempfile


_OBJECT_HEADER = re.compile(rb"(?:^|[\r\n])(\d+)\s+0\s+obj\b")


def repair_scanned_pdf(source_path: Path) -> Path | None:
    """Rebuild an image-only PDF from intact JPEG page objects, if unambiguous."""
    source_path = Path(source_path)
    data = source_path.read_bytes()
    headers = list(_OBJECT_HEADER.finditer(data))
    objects: dict[int, tuple[int, bytes]] = {}
    for index, header in enumerate(headers):
        start = header.end()
        next_start = headers[index + 1].start() if index + 1 < len(headers) else len(data)
        end_object = data.find(b"endobj", start, next_start)
        end = end_object if end_object >= 0 else next_start
        objects[int(header.group(1))] = (header.start(), start, data[start:end])

    pages = []
    for object_id, (position, _body_start, body) in objects.items():
        if re.search(rb"/Type\s*/Page\b", body):
            pages.append((position, object_id, body))
    pages.sort()
    expected_count = re.search(rb"/Linearized\s+1\b.{0,180}?/N\s+(\d+)", data[:2048], re.S)
    page_count = int(expected_count.group(1)) if expected_count else len(pages)
    if not pages or len(pages) != page_count:
        return None

    decoded_pages = []
    try:
        from PIL import Image

        for _position, _page_id, page_body in pages:
            resource_match = re.search(rb"/Resources\s+(\d+)\s+0\s+R", page_body)
            if not resource_match:
                return None
            resource_entry = objects.get(int(resource_match.group(1)))
            if not resource_entry:
                return None
            xobject_match = re.search(rb"/XObject\s*<<(.*?)>>", resource_entry[2], re.S)
            if not xobject_match:
                return None
            image_ids = re.findall(rb"/\w+\s+(\d+)\s+0\s+R", xobject_match.group(1))
            if len(image_ids) != 1:
                return None
            image_entry = objects.get(int(image_ids[0]))
            if not image_entry:
                return None
            image_body_start, image_body = image_entry[1], image_entry[2]
            if not re.search(rb"/Subtype\s*/Image\b", image_body) or not re.search(rb"/Filter\s*/DCTDecode\b", image_body):
                return None
            length_match = re.search(rb"/Length\s+(\d+)", image_body)
            stream_match = re.search(rb"\bstream\r?\n", image_body)
            if not length_match or not stream_match:
                return None
            stream_start = stream_match.end()
            declared_length = int(length_match.group(1))
            declared_end = min(stream_start + declared_length, len(image_body))
            jpeg_end_marker = data.find(b"\xff\xd9", image_body_start + stream_start, image_body_start + declared_end)
            if jpeg_end_marker < 0:
                return None
            jpeg_start = image_body_start + stream_start
            jpeg_data = data[jpeg_start:jpeg_end_marker + 2]
            image = Image.open(BytesIO(jpeg_data))
            image.load()
            if image.format != "JPEG":
                image.close()
                return None
            width = re.search(rb"/Width\s+(\d+)", image_body)
            height = re.search(rb"/Height\s+(\d+)", image_body)
            if width and height and image.size != (int(width.group(1)), int(height.group(1))):
                image.close()
                return None
            decoded_pages.append(image.convert("RGB"))

        if len(decoded_pages) != page_count:
            return None
        with tempfile.NamedTemporaryFile(
            prefix=f".{source_path.stem}.recovered-",
            suffix=source_path.suffix,
            dir=source_path.parent,
            delete=False,
        ) as temporary:
            output = Path(temporary.name)
        output.unlink(missing_ok=True)
        decoded_pages[0].save(
            output,
            "PDF",
            save_all=True,
            append_images=decoded_pages[1:],
            resolution=300.0,
        )
        return output
    except Exception:
        if "output" in locals():
            output.unlink(missing_ok=True)
        return None
    finally:
        for image in decoded_pages:
            image.close()


def ensure_pdfium_compatible(pdf_path: Path) -> bool:
    """Keep a PDFium-readable file in place, repairing safe scan-only cases."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return False

    pdf_path = Path(pdf_path)
    try:
        _check_with_pdfium(pdfium, pdf_path)
        return False
    except Exception as original_error:
        recovered = repair_scanned_pdf(pdf_path)
        if not recovered:
            raise ValueError(
                "PDF не открывается в просмотрщике. Автоматически восстановить структуру не удалось; "
                "пересохраните документ в PDF и загрузите его повторно."
            ) from original_error
        try:
            _check_with_pdfium(pdfium, recovered)
            recovered.replace(pdf_path)
            return True
        except Exception as repair_error:
            recovered.unlink(missing_ok=True)
            raise ValueError(
                "PDF повреждён: удалось извлечь страницы, но восстановленную копию не удалось проверить. "
                "Пересохраните документ в PDF и загрузите его повторно."
            ) from repair_error


def _check_with_pdfium(pdfium, pdf_path: Path) -> None:
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        if len(document) == 0:
            raise ValueError("PDF не содержит страниц.")
        for page_index in range(len(document)):
            page = document[page_index]
            bitmap = page.render(scale=0.1)
            if bitmap.width <= 0 or bitmap.height <= 0:
                raise ValueError(f"Не удалось отобразить страницу {page_index + 1}.")
            bitmap.close()
            page.close()
    finally:
        document.close()
