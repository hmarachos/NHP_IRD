from pathlib import Path

from app.models import ParsedDocument
from app.services.ai_extractor import AiFieldExtractor
from app.services.classifier import DocumentClassifier
from app.services.field_extractor import RegexFieldExtractor
from app.services.pdf_text import PdfTextExtractor


class DocumentProcessingPipeline:
    def __init__(self, *, document_types: list[str], tesseract_lang: str, ocr_dpi: int):
        self.document_types = document_types
        self.text_extractor = PdfTextExtractor(tesseract_lang=tesseract_lang, dpi=ocr_dpi)
        self.regex_extractor = RegexFieldExtractor()
        self.ai_extractor = AiFieldExtractor(self.regex_extractor)
        self.classifier = DocumentClassifier(document_types)

    def process(self, pdf_path: Path) -> ParsedDocument:
        text = self.text_extractor.extract(pdf_path)
        parsed = self.ai_extractor.extract(text, self.document_types)
        if not parsed.working_title or parsed.working_title == "Неопределенный документ":
            parsed.working_title = self.classifier.classify(text, parsed.official_title)
        if self._should_use_classified_title(parsed.official_title, parsed.working_title):
            parsed.official_title = parsed.working_title
        parsed.raw_text = text
        return parsed

    @staticmethod
    def _should_use_classified_title(official_title: str, working_title: str) -> bool:
        if not working_title or working_title == "Неопределенный документ":
            return False
        normalized = " ".join((official_title or "").upper().split())
        generic_titles = {
            "",
            "ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ",
            "ТЕХНИЧЕСКИЕ УСЛОВИЯ",
            "ТУ",
            "АКТ",
            "ПИСЬМО",
            "РЕШЕНИЕ",
            "ПРОТОКОЛ",
            "ВЕДОМОСТЬ",
            "СПРАВКА",
            "СВИДЕТЕЛЬСТВО",
        }
        return normalized in generic_titles
