from app.services.ai_client import ai_client
import copy
import base64
from io import BytesIO
import json
import logging
from pathlib import Path

import os
from django.conf import settings as django_settings


def _config(key):
    if django_settings.configured and hasattr(django_settings, key):
        return getattr(django_settings, key)
    env_name = key
    value = os.getenv(env_name)
    if key == "USE_AI":
        return (value or "true").lower() in {"1", "true", "yes", "on"}
    return value or ""

from app.models import ParsedDocument
from app.prompts import EXTRACTION_PROMPT, EXTRACTION_SCHEMA


logger = logging.getLogger(__name__)
OPENAI_INPUT_CHUNK_CHARS = 12_000


class AiFieldExtractor:
    def __init__(self, fallback_extractor):
        self.fallback_extractor = fallback_extractor

    def extract(self, text: str, document_types: list[str]) -> ParsedDocument:
        if not _config("USE_AI"):
            return self.fallback_extractor.extract(text)

        if not _config("OPENAI_API_KEY"):
            return self.fallback_extractor.extract(text)

        try:
            from openai import OpenAI
        except ImportError:
            return self.fallback_extractor.extract(text)

        client = ai_client(api_key=_config("OPENAI_API_KEY"))

        # Keep the working title as a plain string in the strict output schema.
        # Several Russian document titles contain quotation marks, which the
        # Responses structured-output enum validator rejects. The prompt still
        # supplies the allowed names, and the pipeline/classifier normalizes
        # any answer outside that list.
        schema = copy.deepcopy(EXTRACTION_SCHEMA)
        schema["properties"]["working_title"].pop("enum", None)

        parsed_chunks = []
        for chunk in self._split_text(text, 60_000):
            try:
                response = client.responses.create(
                    model=_config("OPENAI_MODEL"),
                    instructions=EXTRACTION_PROMPT,
                    input=(
                        f"Возможные рабочие названия: {', '.join(document_types)}\n\n"
                        f"OCR-текст документа (часть):\n{chunk}"
                    ),
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "document_fields",
                            "schema": schema,
                            "strict": True,
                        }
                    },
                )
                data = json.loads(response.output_text)
                if data.get("issuer"):
                    data["issuer"] = self.fallback_extractor._clean_issuer(
                        self.fallback_extractor._prefer_russian_text(data["issuer"])
                    )
                parsed_chunks.append(ParsedDocument(raw_text=chunk, **data))
            except Exception:
                logger.exception("OpenAI document extraction failed; using local extraction")
                return self.fallback_extractor.extract(text)

        if not parsed_chunks:
            return self.fallback_extractor.extract(text)
        return self._merge_chunks(text, parsed_chunks)

    def extract_apz_handwritten_header(self, pdf_path: Path, ocr_text: str) -> dict[str, str]:
        """Read the handwritten APZ registration line from its first page."""
        if not _config("USE_AI") or not _config("OPENAI_API_KEY"):
            return {}
        try:
            import pypdfium2 as pdfium
            from openai import OpenAI

            pdf = pdfium.PdfDocument(str(pdf_path))
            if not len(pdf):
                return {}
            page = pdf[0].render(scale=8.33).to_pil().convert("RGB")
            width, height = page.size
            # The registration line is directly below the printed APZ title.
            # This excludes the separate approval dates near the signatures.
            crop = page.crop((int(width * .03), int(height * .275), int(width * .66), int(height * .318)))
            crop.thumbnail((2200, 700))
            buffer = BytesIO()
            crop.save(buffer, format="JPEG", quality=92)
            image_data = base64.b64encode(buffer.getvalue()).decode("ascii")
            schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "document_number": {"type": "string"},
                    "issue_date": {"type": "string"},
                },
                "required": ["document_number", "issue_date"],
            }
            response = ai_client(api_key=_config("OPENAI_API_KEY")).responses.create(
                model=_config("OPENAI_MODEL"),
                instructions=(
                    "Прочитай рукописные номер и дату выдачи архитектурно-планировочного задания "
                    "на приложенном фрагменте. Это строка регистрации прямо под заголовком. "
                    "Не перепутай её с датами утверждения рядом с подписями. Верни только "
                    "номер документа без слова № и дату в формате YYYY-MM-DD. Если символ "
                    "невозможно прочитать уверенно, оставь поле пустым. OCR-контекст страницы: "
                    + ocr_text[:5000]
                ),
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Распознай только номер задания и дату в этой рукописной строке."},
                        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_data}", "detail": "high"},
                    ],
                }],
                text={"format": {"type": "json_schema", "name": "apz_header", "schema": schema, "strict": True}},
            )
            data = json.loads(response.output_text)
            return {key: str(data.get(key, "")).strip() for key in ("document_number", "issue_date")}
        except Exception:
            logger.exception("APZ handwritten header extraction failed")
            return {}

    def extract_handwritten_header_line(self, pdf_path: Path, ocr_text: str, marker: str) -> dict[str, str]:
        """Read a handwritten document number and date near a printed header marker."""
        if not _config("USE_AI") or not _config("OPENAI_API_KEY"):
            return {}
        try:
            import pypdfium2 as pdfium
            from openai import OpenAI

            pdf = pdfium.PdfDocument(str(pdf_path))
            if not len(pdf):
                return {}
            page = pdf[0].render(scale=8.33).to_pil().convert("RGB")
            width, height = page.size
            # The handwritten date and number are on the first page below
            # the issuer address; crop the full line and exclude the recipient.
            crop = page.crop((0, int(height * .19), int(width * .76), int(height * .27)))
            crop.thumbnail((3000, 700))
            buffer = BytesIO()
            crop.save(buffer, format="JPEG", quality=93)
            schema = {
                "type": "object", "additionalProperties": False,
                "properties": {"document_number": {"type": "string"}, "issue_date": {"type": "string"}},
                "required": ["document_number", "issue_date"],
            }
            response = ai_client(api_key=_config("OPENAI_API_KEY")).responses.create(
                model=_config("OPENAI_MODEL"),
                instructions=(
                    f"Изучи изображение первой страницы документа. Распознай рукописные исходящий номер и дату выдачи "
                    f"в верхней шапке рядом с обозначением «{marker}». Не используй номера, напечатанные в тексте, "
                    "адресе или нормативных ссылках. Верни номер как написан, дату в формате YYYY-MM-DD. "
                    "Если поле неразборчиво, верни пустую строку. OCR-контекст: " + ocr_text[:6000]
                ),
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": f"Найди рукописные реквизиты документа у шапки {marker}."},
                    {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"), "detail": "high"},
                ]}],
                text={"format": {"type": "json_schema", "name": "handwritten_header", "schema": schema, "strict": True}},
            )
            data = json.loads(response.output_text)
            return {key: str(data.get(key, "")).strip() for key in ("document_number", "issue_date")}
        except Exception:
            logger.exception("Handwritten header extraction failed")
            return {}

    def extract_gai_stamp_fields(self, pdf_path: Path, ocr_text: str) -> dict[str, str]:
        """Read issuer, handwritten date and number from the GAI blue stamp."""
        if not _config("USE_AI") or not _config("OPENAI_API_KEY"):
            return {}
        try:
            import pypdfium2 as pdfium
            from openai import OpenAI

            pdf = pdfium.PdfDocument(str(pdf_path))
            if not len(pdf):
                return {}
            page = pdf[0].render(scale=8.33).to_pil().convert("RGB")
            width, height = page.size
            # The bilingual blue GAI stamp and its handwritten date/number
            # occupy the upper header immediately left of the recipient block.
            crop = page.crop((0, int(height * .10), int(width * .62), int(height * .30)))
            crop.thumbnail((3000, 1300))
            buffer = BytesIO()
            crop.save(buffer, format="JPEG", quality=94)
            schema = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "issuer": {"type": "string"},
                    "document_number": {"type": "string"},
                    "issue_date": {"type": "string"},
                },
                "required": ["issuer", "document_number", "issue_date"],
            }
            response = ai_client(api_key=_config("OPENAI_API_KEY")).responses.create(
                model=_config("OPENAI_MODEL"),
                instructions=(
                    "На изображении шапка технических требований ГАИ. Извлеки три реквизита. "
                    "issuer: полное русскоязычное название выдавшего подразделения из синей печати "
                    "(правая русская колонка; не бери адресата справа от штампа и не обрывай название). "
                    "document_number: рукописный номер на строке рядом с печатным знаком №. "
                    "issue_date: рукописная дата на той же строке, рядом с печатным словом «от»; "
                    "если год записан двумя цифрами, трактуй его как 20xx. Дату верни YYYY-MM-DD. "
                    "Не используй номера/даты из текста документа или контактного телефона. "
                    "Если поле невозможно разобрать, верни пустую строку. Контекст OCR: " + ocr_text[:5000]
                ),
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": "Прочитай реквизиты именно из синей печати и рукописной строки под ней."},
                    {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"), "detail": "high"},
                ]}],
                text={"format": {"type": "json_schema", "name": "gai_stamp_fields", "schema": schema, "strict": True}},
            )
            data = json.loads(response.output_text)
            return {key: str(data.get(key, "")).strip() for key in ("issuer", "document_number", "issue_date")}
        except Exception:
            logger.exception("GAI stamp extraction failed")
            return {}

    def extract_beltelecom_header_dates(self, pdf_path: Path, ocr_text: str) -> dict[str, str]:
        """Read handwritten issue and expiry dates from Beltelecom conditions."""
        if not _config("USE_AI") or not _config("OPENAI_API_KEY"):
            return {}
        try:
            import pypdfium2 as pdfium
            from openai import OpenAI

            pdf = pdfium.PdfDocument(str(pdf_path))
            if not len(pdf):
                return {}
            page = pdf[0].render(scale=8.33).to_pil().convert("RGB")
            width, height = page.size
            # First image is the № / от line; second is the explicit validity date.
            regions = [
                page.crop((int(width * .08), int(height * .29), int(width * .98), int(height * .39))),
                page.crop((int(width * .06), int(height * .42), int(width * .98), int(height * .49))),
            ]
            images = []
            for region in regions:
                region.thumbnail((3200, 700))
                buffer = BytesIO()
                region.save(buffer, format="JPEG", quality=94)
                images.append("data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
            schema = {
                "type": "object", "additionalProperties": False,
                "properties": {"issue_date": {"type": "string"}, "valid_until": {"type": "string"}},
                "required": ["issue_date", "valid_until"],
            }
            response = ai_client(api_key=_config("OPENAI_API_KEY")).responses.create(
                model=_config("OPENAI_MODEL"),
                instructions=(
                    "На изображениях две строки технических условий РУП Белтелеком. "
                    "На первом фрагменте прочитай дату после печатного слова «от» (это дата выдачи). "
                    "На втором прочитай дату после слов «срок действия до» (это дата окончания). "
                    "В этих строках год напечатан справа. Верни обе даты только в YYYY-MM-DD. "
                    "Не используй дату постановлений в основном тексте. Если дата неразборчива, верни пустую строку. OCR: "
                    + ocr_text[:5000]
                ),
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": "Первое изображение: дата выдачи после «от». Второе: срок действия до указанной даты."},
                    {"type": "input_image", "image_url": images[0], "detail": "high"},
                    {"type": "input_image", "image_url": images[1], "detail": "high"},
                ]}],
                text={"format": {"type": "json_schema", "name": "beltelecom_dates", "schema": schema, "strict": True}},
            )
            data = json.loads(response.output_text)
            return {key: str(data.get(key, "")).strip() for key in ("issue_date", "valid_until")}
        except Exception:
            logger.exception("Beltelecom header date extraction failed")
            return {}

    def extract_minskenergo_header(self, pdf_path: Path, ocr_text: str) -> dict[str, str]:
        """Read the handwritten registration number and date on Minskenergo TUs."""
        if not _config("USE_AI") or not _config("OPENAI_API_KEY"):
            return {}
        try:
            import pypdfium2 as pdfium
            from openai import OpenAI

            pdf = pdfium.PdfDocument(str(pdf_path))
            if not len(pdf):
                return {}
            page = pdf[0].render(scale=9).to_pil().convert("RGB")
            width, height = page.size
            # The issuer's Russian block is at the top right; the handwritten
            # № / date line sits below the "Технические условия направлены" heading.
            issuer_crop = page.crop((int(width * .49), int(height * .015), int(width * .99), int(height * .135)))
            requisites_crop = page.crop((int(width * .04), int(height * .235), int(width * .58), int(height * .295)))
            image_urls = []
            for crop in (issuer_crop, requisites_crop):
                crop.thumbnail((3200, 900))
                buffer = BytesIO()
                crop.save(buffer, format="JPEG", quality=95)
                image_urls.append("data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
            schema = {
                "type": "object", "additionalProperties": False,
                "properties": {"issuer": {"type": "string"}, "document_number": {"type": "string"}, "issue_date": {"type": "string"}},
                "required": ["issuer", "document_number", "issue_date"],
            }
            response = ai_client(api_key=_config("OPENAI_API_KEY")).responses.create(
                model=_config("OPENAI_MODEL"),
                instructions=(
                    "Распознавай реквизиты технических условий филиала Минскэнерго. На первом изображении "
                    "используй только ПРАВЫЙ РУССКИЙ вариант шапки. Верни орган в обычном регистре, "
                    "с заглавной первой буквой, название филиала в кавычках, аббревиатуру РУП прописными. "
                    "На втором изображении прочитай рукописный номер после № и дату после «от». "
                    "Не перепутай их с адресатом, номером заявки, объектом или номером входящего письма. "
                    "Дату верни YYYY-MM-DD. Если значение нельзя уверенно прочитать, оставь пустым. OCR страницы: "
                    + ocr_text[:5000]
                ),
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": "Изображение 1 — русский вариант выдавшего органа справа. Изображение 2 — рукописные номер и дата документа слева."},
                    {"type": "input_image", "image_url": image_urls[0], "detail": "high"},
                    {"type": "input_image", "image_url": image_urls[1], "detail": "high"},
                ]}],
                text={"format": {"type": "json_schema", "name": "minskenergo_header", "schema": schema, "strict": True}},
            )
            data = json.loads(response.output_text)
            return {key: str(data.get(key, "")).strip() for key in ("issuer", "document_number", "issue_date")}
        except Exception:
            logger.exception("Minskenergo header extraction failed")
            return {}

    def extract_document_header(self, pdf_path: Path, ocr_text: str) -> dict[str, str]:
        """Read issuer and issuance pair from the first-page letterhead/header.

        Using the visual header keeps body citations, applicant details and
        validity dates out of the decision. The crop is intentionally broad:
        document issuers place the letterhead and handwritten registration
        line in different positions and often use bilingual columns.
        """
        if not _config("USE_AI") or not _config("OPENAI_API_KEY"):
            return {}
        try:
            import pypdfium2 as pdfium
            from openai import OpenAI

            pdf = pdfium.PdfDocument(str(pdf_path))
            if not len(pdf):
                return {}
            page = pdf[0].render(scale=8.33).to_pil().convert("RGB")
            width, height = page.size
            header = page.crop((0, 0, width, int(height * .43)))
            header.thumbnail((3600, 1800))
            buffer = BytesIO()
            header.save(buffer, format="JPEG", quality=95)
            requisites = page.crop((0, 0, width, int(height * .22)))
            requisites.thumbnail((4200, 1200))
            requisites_buffer = BytesIO()
            requisites.save(requisites_buffer, format="JPEG", quality=97)
            schema = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "issuer": {"type": "string"},
                    "document_number": {"type": "string"},
                    "issue_date": {"type": "string"},
                },
                "required": ["issuer", "document_number", "issue_date"],
            }
            response = ai_client(api_key=_config("OPENAI_API_KEY")).responses.create(
                model=_config("OPENAI_MODEL"),
                instructions=(
                    "Извлеки реквизиты именно выдающего документа из изображения верхней части первой страницы. "
                    "issuer: полное название организации в РУССКОЙ части шапки/бланка; не бери белорусскую "
                    "параллельную колонку, адресата после «Кому», заявителя, заказчика, адрес, банк или организацию, "
                    "упомянутую в основном тексте. Если на бланке указано головное объединение и ниже отдельное "
                    "унитарное предприятие с собственным названием, выбери именно это предприятие как наиболее "
                    "конкретного выдавшего органа, не добавляй головное объединение. Сохрани собственное "
                    "название, а полные организационно-правовые формы сокращай до аббревиатур: "
                    "республиканское унитарное предприятие — РУП, коммунальное унитарное предприятие — КУП, "
                    "унитарное предприятие — УП, открытое акционерное общество — ОАО. "
                    "Оформи обычным регистром (заглавная первая буква), аббревиатуры типа РУП, КУП, ОАО оставь "
                    "прописными, собственное название в кавычках. document_number и issue_date должны быть прочитаны "
                    "как пара из одной строки реквизитов выдачи: номер непосредственно у знака №, дата рядом с «от» "
                    "или в той же заполненной строке. Для писем заказчика отдельно проверь синюю строку вверху "
                    "страницы: обычно в ней стоят исходящие номер и дата письма. Не используй номер/дату адресата, объекта, заявки, договора, "
                    "входящего письма, нормативных актов и основного текста. Не принимай УНП, ОКПО, телефон или "
                    "банковские номера за номер документа. Если на изображении есть дата и номер от руки, увеличь "
                    "внимание к этим полям. Верни дату ISO YYYY-MM-DD. Если поле нельзя прочесть уверенно или нет "
                    "в явном виде, верни пустую строку. "
                    "Сохраняй каждый разделитель номера точно как напечатан или написан: дефисы и косые черты «/» не пропускай и не заменяй; "
                    "например 08-02/4045 нельзя превращать в 08-024045. OCR-контекст используй только для поиска русского варианта шапки: "
                    + ocr_text[:5000]
                ),
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": "Изображение 1 — вся верхняя часть первой страницы. Изображение 2 — увеличенная верхняя строка реквизитов: прочитай номер посимвольно, обязательно сохрани слеши и дефисы."},
                    {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"), "detail": "high"},
                    {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(requisites_buffer.getvalue()).decode("ascii"), "detail": "high"},
                ]}],
                text={"format": {"type": "json_schema", "name": "document_header", "schema": schema, "strict": True}},
            )
            data = json.loads(response.output_text)
            return {key: str(data.get(key, "")).strip() for key in ("issuer", "document_number", "issue_date")}
        except Exception:
            logger.exception("Generic document header extraction failed")
            return {}

    @staticmethod
    def _split_text(text: str, max_chars: int) -> list[str]:
        """Split OCR text without dropping pages or exceeding the request size."""
        if len(text) <= max_chars:
            return [text]

        chunks = []
        current = ""
        for line in text.splitlines(keepends=True):
            remaining = line
            while remaining:
                room = max_chars - len(current)
                if room == 0:
                    chunks.append(current)
                    current = ""
                    room = max_chars

                if len(remaining) <= room:
                    current += remaining
                    remaining = ""
                    continue

                split_at = remaining.rfind(" ", 0, room + 1)
                if split_at < room // 2:
                    split_at = room
                current += remaining[:split_at]
                remaining = remaining[split_at:].lstrip()
                chunks.append(current)
                current = ""

        if current:
            chunks.append(current)
        return chunks

    def _merge_chunks(self, full_text: str, chunks: list[ParsedDocument]) -> ParsedDocument:
        fallback = self.fallback_extractor.extract(full_text)

        def first_value(field: str) -> str:
            return next((getattr(item, field) for item in chunks if getattr(item, field)), "")

        issue_date = first_value("issue_date") or fallback.issue_date
        valid_until = first_value("valid_until") or fallback.valid_until
        validity_text = max(
            (item.validity_text for item in chunks if item.validity_text),
            key=len,
            default=fallback.validity_text,
        )
        working_title = next(
            (
                item.working_title
                for item in chunks
                if item.working_title and item.working_title != "Неопределенный документ"
            ),
            first_value("working_title") or fallback.working_title,
        )

        return ParsedDocument(
            working_title=working_title,
            official_title=first_value("official_title") or fallback.official_title,
            issuer=first_value("issuer") or fallback.issuer,
            issue_date=issue_date,
            valid_until=valid_until,
            validity_text=validity_text,
            document_number=first_value("document_number") or fallback.document_number,
            no_expiration=not valid_until and (
                fallback.no_expiration
                or any(item.no_expiration and item.validity_text for item in chunks)
            ),
            raw_text=full_text,
        )
