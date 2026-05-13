from pathlib import Path


class PdfTextExtractor:
    def __init__(self, *, tesseract_lang: str = "rus+eng", dpi: int = 300):
        self.tesseract_lang = tesseract_lang
        self.dpi = dpi

    def extract(self, pdf_path: Path) -> str:
        text = self._extract_embedded_text(pdf_path)
        if self._has_enough_text(text):
            return text
        return self._extract_with_ocr(pdf_path)

    def _extract_embedded_text(self, pdf_path: Path) -> str:
        try:
            from pypdf import PdfReader
        except ImportError:
            return ""

        try:
            reader = PdfReader(str(pdf_path))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n\n".join(pages).strip()
        except Exception:
            return ""

    def _extract_with_ocr(self, pdf_path: Path) -> str:
        try:
            import pypdfium2 as pdfium
            import pytesseract
            from PIL import ImageFilter, ImageOps
        except ImportError as exc:
            raise RuntimeError(
                "Для сканированных PDF установите зависимости OCR: pypdfium2, pytesseract, Pillow."
            ) from exc

        self._ensure_tesseract_languages(pytesseract)

        pdf = pdfium.PdfDocument(str(pdf_path))
        scale = self.dpi / 72
        pages_text: list[str] = []

        for index in range(len(pdf)):
            page = pdf[index]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().convert("RGB")
            image = ImageOps.grayscale(image)
            image = ImageOps.autocontrast(image)
            image = image.filter(ImageFilter.MedianFilter(size=3))
            image = image.filter(ImageFilter.SHARPEN)
            image = image.point(lambda value: 255 if value > 175 else 0)
            try:
                text = pytesseract.image_to_string(
                    image,
                    lang=self.tesseract_lang,
                    config="--oem 1 --psm 6 -c preserve_interword_spaces=1",
                )
            except pytesseract.TesseractError as exc:
                message = str(exc)
                if "Failed loading language" in message or "Error opening data file" in message:
                    raise RuntimeError(
                        "Tesseract не нашел языковой пакет для OCR. "
                        f"Запрошен язык '{self.tesseract_lang}'. Для русских PDF установите "
                        "tesseract-lang и используйте TESSERACT_LANG=rus+eng."
                    ) from exc
                raise
            pages_text.append(f"--- page {index + 1} ---\n{text.strip()}")

        return "\n\n".join(pages_text).strip()

    def _ensure_tesseract_languages(self, pytesseract) -> None:
        available = set(pytesseract.get_languages(config=""))
        requested = {
            language
            for language in self.tesseract_lang.replace(",", "+").split("+")
            if language and language != "osd"
        }
        missing = sorted(requested - available)
        if missing:
            raise RuntimeError(
                "OCR настроен на русский язык, но Tesseract не нашел языковой пакет: "
                f"{', '.join(missing)}. Установите tesseract-lang и используйте "
                "TESSERACT_LANG=rus+eng. Без rus.traineddata русский текст будет "
                "распознаваться как латиница."
            )

    @staticmethod
    def _has_enough_text(text: str) -> bool:
        letters = [char for char in text if char.isalpha()]
        return len(letters) >= 200
