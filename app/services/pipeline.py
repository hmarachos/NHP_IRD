from pathlib import Path
from datetime import date
import re

from app.models import ParsedDocument
from app.services.ai_extractor import AiFieldExtractor
from app.services.classifier import DocumentClassifier
from app.services.field_extractor import RegexFieldExtractor
from app.services.pdf_text import PdfTextExtractor
from app.services.word_documents import is_word_document, word_preview_path


class DocumentProcessingPipeline:
    def __init__(self, *, document_types: list[str], tesseract_lang: str, ocr_dpi: int):
        self.document_types = document_types
        self.text_extractor = PdfTextExtractor(tesseract_lang=tesseract_lang, dpi=ocr_dpi)
        self.regex_extractor = RegexFieldExtractor()
        self.ai_extractor = AiFieldExtractor(self.regex_extractor)
        self.classifier = DocumentClassifier(document_types)

    def process(self, pdf_path: Path) -> ParsedDocument:
        if is_word_document(pdf_path):
            pdf_path = word_preview_path(pdf_path)
        text = self.text_extractor.extract(pdf_path)
        explicit_title = self.classifier.classify_explicit_title(text)
        if explicit_title == "Копия регистрационной карточки в ЕРОКС":
            # ЕРОКС registration cards contain identifiers and dates for the
            # registered asset. They are not document requisites. Keep these
            # fields blank and mark the attachment as having no expiry.
            return ParsedDocument(
                working_title=explicit_title,
                no_expiration=True,
                validity_text="Бессрочно",
                raw_text=text,
            )
        parsed = self.ai_extractor.extract(text, self.document_types)
        fallback_parsed = self.regex_extractor.extract(text)
        explicit_expiry = self.regex_extractor._extract_explicit_valid_until(text)
        if explicit_expiry:
            # An explicit expiry phrase in the document, even when OCR noise
            # separates it from the date, takes precedence over a missing or
            # speculative AI value.
            parsed.valid_until = explicit_expiry
            parsed.no_expiration = False
        is_apz = self.classifier.classify_explicit_title(text) == "Архитектурно-планировочное задание"
        ministry_header = self.regex_extractor._extract_ministry_technical_header(text)
        if self.regex_extractor._is_ministry_technical_requirements(text) and (not ministry_header.get("document_number") or not ministry_header.get("issue_date")):
            handwritten_fields = self.ai_extractor.extract_handwritten_header_line(pdf_path, text, "Технические требования")
            ministry_header.update({key: value for key, value in handwritten_fields.items() if value})
        if ministry_header:
            parsed.document_number = ministry_header.get("document_number") or parsed.document_number
            parsed.issue_date = ministry_header.get("issue_date") or parsed.issue_date
            if fallback_parsed.issuer and (
                "Республиканский центр государственной экологической экспертизы" in fallback_parsed.issuer
                or "Узденский районный центр гигиены и эпидемиологии" in fallback_parsed.issuer
            ):
                parsed.issuer = fallback_parsed.issuer
        if self.regex_extractor._is_gai_technical_requirements(text):
            gai_fields = self.ai_extractor.extract_gai_stamp_fields(pdf_path, text)
            if gai_fields.get("issuer"):
                parsed.issuer = self.regex_extractor._clean_issuer(gai_fields["issuer"])
            if gai_fields.get("document_number"):
                parsed.document_number = re.sub(r"^№\s*", "", gai_fields["document_number"], flags=re.IGNORECASE).strip()
            if gai_fields.get("issue_date"):
                try:
                    parsed.issue_date = date.fromisoformat(gai_fields["issue_date"]).isoformat()
                except ValueError:
                    pass
        if self.regex_extractor._is_beltelecom_technical_conditions(text):
            # The issuing party is identified by the approval stamp above the
            # title; applicant and project numbers appear elsewhere in the text.
            parsed.issuer = 'РУП «Белтелеком»'
            parsed.document_number = self.regex_extractor._extract_beltelecom_number(text)
            dates = self.ai_extractor.extract_beltelecom_header_dates(pdf_path, text)
            for field in ("issue_date", "valid_until"):
                try:
                    value = date.fromisoformat(dates.get(field, ""))
                except ValueError:
                    continue
                setattr(parsed, field, value.isoformat())
            # The last page also states that these conditions are valid for
            # one year. Use that term to disambiguate the handwritten day in
            # point 2 (the OCR/vision model can confuse 03 with 05).
            if parsed.issue_date and re.search(r"действуют\s*[|l!i1]?\s*год", text, re.IGNORECASE):
                parsed.valid_until = self.regex_extractor._add_years(
                    date.fromisoformat(parsed.issue_date), 1
                ).isoformat()
            if parsed.valid_until:
                parsed.no_expiration = False
        is_minskenergo = self.regex_extractor._is_minskenergo_electrical_conditions(text)
        if is_minskenergo:
            header = self.ai_extractor.extract_minskenergo_header(pdf_path, text)
            parsed.issuer = 'Минское республиканское унитарное предприятие электроэнергетики «Минскэнерго», филиал «Столбцовские электрические сети» РУП «Минскэнерго»'
            parsed.document_number = re.sub(r"\s+", "", re.sub(r"^№\s*", "", header.get("document_number", ""), flags=re.IGNORECASE))
            if not re.fullmatch(r"\d{1,4}/\d{1,6}", parsed.document_number):
                parsed.document_number = ""
            try:
                parsed.issue_date = date.fromisoformat(header.get("issue_date", "")).isoformat()
            except ValueError:
                pass
        if is_apz:
            apz_fields = self.ai_extractor.extract_apz_handwritten_header(pdf_path, text)
            number = re.sub(r"^№\s*", "", apz_fields.get("document_number", ""), flags=re.IGNORECASE).strip().replace(",", ".")
            parsed.document_number = number if re.fullmatch(r"\d+[\-/]\d+[\-/]\d+(?:\.\d+)?", number) else ""
            handwritten_date = apz_fields.get("issue_date", "")
            try:
                parsed.issue_date = date.fromisoformat(handwritten_date).isoformat()
            except ValueError:
                parsed.issue_date = ""
            if fallback_parsed.issuer and "райисполком" in fallback_parsed.issuer.lower():
                parsed.issuer = fallback_parsed.issuer
        # A municipal executive committee's decision date is printed beside
        # its number in the heading. Prefer that date over dates cited in the
        # legal basis (which OCR/AI can mistake for the issue date).
        if (
            "районный исполнительный комитет" in text.lower()
            and "решение" in text.lower()
            and fallback_parsed.issue_date
        ):
            parsed.issue_date = fallback_parsed.issue_date
        committee_number = self.regex_extractor._extract_committee_decision_number(text)
        if committee_number:
            parsed.document_number = committee_number
        if (
            "районный исполнительный комитет" in text.lower()
            and "решение" in text.lower()
            and "районный исполнительный комитет" in fallback_parsed.issuer.lower()
        ):
            parsed.issuer = fallback_parsed.issuer
        # Generic visual-header pass for documents without an established
        # layout-specific reader. Header positions differ, but the issuer and
        # the № / issue-date pair are usually together near the top of page 1.
        # This prevents body citations, applicant IDs and validity dates from
        # being promoted by generic whole-document OCR/AI extraction.
        is_curated_header = (
            is_apz
            or self.regex_extractor._is_gai_technical_requirements(text)
            or self.regex_extractor._is_beltelecom_technical_conditions(text)
            or is_minskenergo
            or self.regex_extractor._is_ministry_technical_requirements(text)
            or ("районный исполнительный комитет" in text.lower() and "решение" in text.lower())
        )
        header_issue_date = ""
        if not is_curated_header:
            header_fields = self.ai_extractor.extract_document_header(pdf_path, text)
            if header_fields.get("issuer"):
                issuer = self.regex_extractor._clean_issuer(
                    self.regex_extractor._prefer_russian_text(header_fields["issuer"])
                )
                if issuer:
                    parsed.issuer = issuer
            candidate_number = re.sub(
                r"^№\s*", "", header_fields.get("document_number", ""), flags=re.IGNORECASE
            ).strip()
            if candidate_number and len(candidate_number) <= 32 and re.search(r"\d", candidate_number) and not re.search(r"\s", candidate_number):
                parsed.document_number = candidate_number
            try:
                header_issue_date = date.fromisoformat(header_fields.get("issue_date", "")).isoformat()
                parsed.issue_date = header_issue_date
            except ValueError:
                pass
            if header_issue_date and self.regex_extractor._extract_duration_years(text):
                # A whole-page scan may mistake a regulation date for an
                # explicit expiry. Recompute duration-based validity from the
                # visually confirmed issue date in the header.
                calculated_expiry = self.regex_extractor._calculate_valid_until_from_duration(
                    text, header_issue_date
                )
                if calculated_expiry:
                    parsed.valid_until = calculated_expiry
                    parsed.no_expiration = False
        if not is_apz and not parsed.issue_date and fallback_parsed.issue_date:
            parsed.issue_date = fallback_parsed.issue_date
        if not is_curated_header:
            # Preserve separators read by OCR from the outgoing header. Vision
            # models sometimes merge groups such as 08-02/4045 into 08-024045.
            header_number = self.regex_extractor._extract_header_document_number(text)
            if header_number:
                parsed.document_number = header_number
        if (self.regex_extractor._is_ministry_technical_requirements(text) or self.regex_extractor._is_gai_technical_requirements(text) or is_minskenergo) and parsed.issue_date:
            parsed.valid_until = self.regex_extractor._calculate_valid_until_from_duration(text, parsed.issue_date)
            if parsed.valid_until:
                parsed.no_expiration = False
        if not parsed.issuer and fallback_parsed.issuer:
            parsed.issuer = fallback_parsed.issuer
        if not parsed.official_title and fallback_parsed.official_title:
            parsed.official_title = fallback_parsed.official_title
        if not parsed.validity_text and fallback_parsed.validity_text:
            parsed.validity_text = fallback_parsed.validity_text
        if fallback_parsed.valid_until and (
            not parsed.valid_until or parsed.no_expiration
        ):
            parsed.valid_until = fallback_parsed.valid_until
            parsed.no_expiration = False
        elif not parsed.validity_text and fallback_parsed.validity_text:
            parsed.validity_text = fallback_parsed.validity_text
            parsed.no_expiration = fallback_parsed.no_expiration
        explicit_title = self.classifier.classify_explicit_title(text)
        rule_title = self.classifier.classify_by_rule(text, parsed.official_title)
        if explicit_title:
            parsed.working_title = explicit_title
            parsed.official_title = explicit_title
        elif rule_title:
            parsed.working_title = rule_title
        elif not parsed.working_title or parsed.working_title == "Неопределенный документ":
            parsed.working_title = self.classifier.classify(text, parsed.official_title)
        if self._should_use_classified_title(parsed.official_title, parsed.working_title):
            parsed.official_title = parsed.working_title
        if self._is_untitled_outgoing_letter(text, parsed):
            letter_title = self._outgoing_letter_title(parsed.issuer)
            parsed.working_title = letter_title
            if not self._title_is_explicit(parsed.official_title, text):
                parsed.official_title = letter_title
        parsed.raw_text = text
        return parsed

    @staticmethod
    def _is_untitled_outgoing_letter(text: str, parsed: ParsedDocument) -> bool:
        first_page = text.split("--- page 2 ---", 1)[0]
        if not parsed.issuer or not parsed.document_number or not parsed.issue_date:
            return False
        has_letter_action = re.search(
            r"\b(?:сообщает|сообщаем|просит|просим|информирует|информируем|"
            r"направляет|направляем|уведомляет|уведомляем|рассмотрев ваше письмо)\b",
            first_page,
            re.IGNORECASE,
        )
        return bool(has_letter_action and not DocumentProcessingPipeline._title_is_explicit(parsed.working_title, first_page))

    @staticmethod
    def _title_is_explicit(title: str, text: str) -> bool:
        """Accept a title only when it appears as a short, standalone heading."""
        normalized_title = DocumentClassifier._normalize(title or "")
        if not normalized_title:
            return False
        first_page = text.split("--- page 2 ---", 1)[0]
        for line in first_page.splitlines()[:35]:
            cleaned = line.strip(" \t -*•")
            normalized_line = DocumentClassifier._normalize(cleaned)
            if not normalized_line or len(cleaned) > 180:
                continue
            if normalized_title in normalized_line and not re.search(r"[.!?;]", cleaned):
                return True
        return False

    @staticmethod
    def _outgoing_letter_title(issuer: str) -> str:
        if re.search(r"узденск\w* районн\w* отдел\w* по чрезвычайным ситуациям", issuer or "", re.IGNORECASE):
            return "Письмо Узденского РОЧС"
        issuer = re.sub(r"\s+", " ", (issuer or "").strip())
        return f"Письмо {issuer}" if issuer else "Письмо"

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
