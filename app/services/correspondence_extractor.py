"""Business-letter OCR field extraction independent from Flask request state."""
from app.services.ai_client import ai_client
import json
import logging
import re
from datetime import date

from django.conf import settings
from app.services.organization_names import normalize_organization_forms

logger = logging.getLogger(__name__)

def _extract_correspondence_fields(text: str) -> dict[str, str]:
    """Extract letter metadata without applying IRD validity or expiry rules."""
    values = {"number": "", "letter_date": "", "sender": "", "recipient": "", "subject": "", "summary": ""}
    cleaned = text.strip()
    if not cleaned:
        return values
    first_page = re.split(r"---\s*page\s+2\s*---", cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
    header_match = re.search(
        r"---\s*letterhead\s*---\s*(.*?)\s*---\s*document body\s*---",
        cleaned,
        re.IGNORECASE | re.DOTALL,
    )
    header_fields = _extract_correspondence_header_requisites(header_match.group(1) if header_match else "")

    if settings.USE_AI and settings.OPENAI_API_KEY:
        try:
            from openai import OpenAI

            schema = {
                "type": "object",
                "properties": {key: {"type": "string"} for key in values},
                "required": list(values),
                "additionalProperties": False,
            }
            response = ai_client(api_key=settings.OPENAI_API_KEY).responses.create(
                model=settings.OPENAI_MODEL,
                instructions=(
                    "Извлеки реквизиты делового письма на русском языке. Верни только данные самого письма: "
                    "номер, дату письма в ISO YYYY-MM-DD, наименование отправителя и получателя с сокращёнными "
                    "организационно-правовыми формами (например: унитарное предприятие — УП, "
                    "республиканское унитарное предприятие — РУП, общество с ограниченной ответственностью — ООО), "
                    "тему и краткое содержание в 1-3 предложениях. Учитывай шапку, исходящий номер и дату, "
                    "в первую очередь проверь верхний колонтитул первой страницы: синюю строку с исходящим номером и датой. "
                    "Номер и дату бери из одной строки, если она есть. Учитывай адресата и подпись/организацию автора. "
                    "Не выдумывай отсутствующие значения; тогда верни пустую строку. "
                    "Срок действия документа не извлекай."
                ),
                input=cleaned[:24000],
                text={"format": {"type": "json_schema", "name": "correspondence_fields", "schema": schema, "strict": True}},
            )
            extracted = json.loads(response.output_text)
            for key in values:
                if isinstance(extracted.get(key), str):
                    values[key] = extracted[key].strip()
            values["letter_date"] = _normalize_letter_date(values["letter_date"])
        except Exception:
            logger.exception("AI correspondence extraction failed; using local extraction")

    # Read the blue outgoing number/date line in the first-page letterhead as
    # one pair and let it override AI results that may come from body citations.
    for key in ("number", "letter_date"):
        if header_fields.get(key):
            values[key] = header_fields[key]

    date_pattern = r"(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:19|20)\d{2}"
    if not values["letter_date"]:
        match = re.search(rf"(?:от\s*)?({date_pattern})", first_page[:5000], re.IGNORECASE)
        if match:
            values["letter_date"] = _normalize_letter_date(match.group(1))
    if not values["number"]:
        match = re.search(r"(?:№|N(?:o|º|°)?)\s*([\wА-Яа-яЁё./\\-]{1,40})", first_page[:5000], re.IGNORECASE)
        if match:
            values["number"] = match.group(1).strip(" .,:;-")
    lines = [re.sub(r"\s+", " ", line).strip(" \t\r\n:;—-") for line in cleaned.splitlines()]
    lines = [line for line in lines if line]
    if not values["recipient"]:
        match = re.search(r"(?:^|\n)\s*((?:Директору|Руководителю|Начальнику|Кому)\b[^\n]{0,250})", cleaned, re.IGNORECASE)
        if match:
            values["recipient"] = re.sub(r"\s+", " ", match.group(1)).strip()
    if not values["sender"]:
        organization_start = (
            r"(?:РУП|ОАО|ОДО|ООО|УП|ЗАО|ЧУП|"
            r"Республиканск\w*\s+(?:дочерн\w*\s+)?унитарн\w*\s+предприяти\w*|"
            r"Коммунальн\w*\s+унитарн\w*\s+предприяти\w*|"
            r"Частн\w*\s+унитарн\w*\s+предприяти\w*|"
            r"Унитарн\w*\s+предприяти\w*|"
            r"Обществ\w*\s+с\s+(?:ограниченной|дополнительной)\s+ответственностью|"
            r"(?:Открытое|Закрытое)\s+акционерное\s+общество|"
            r"Государственн\w+ предприятие)"
        )
        match = re.search(
            rf"({organization_start}\b[^\n]{{3,180}}?)\s+"
            r"(?:просит|просим|сообщает|сообщаем|направляет|направляем|информирует|информируем)\b",
            cleaned,
            re.IGNORECASE,
        )
        if match:
            values["sender"] = re.sub(r"\s+", " ", match.group(1)).strip(" ,.;")
    if not values["subject"]:
        for i, line in enumerate(lines[:40]):
            if re.search(r"^(?:о\s+|тема\s*:|касательно\s+|по вопросу\s+)", line, re.IGNORECASE) and len(line) < 220:
                values["subject"] = re.sub(r"^тема\s*:\s*", "", line, flags=re.IGNORECASE)
                break
    if not values["summary"]:
        body = re.sub(r"\s+", " ", cleaned)
        # Skip letterhead and address fragments where possible.
        for marker in ("просит ", "просим ", "сообщает ", "сообщаем ", "направляет ", "информирует ", "информируем "):
            start = body.lower().find(marker)
            if start >= 0:
                body = body[start:]
                break
        values["summary"] = body[:600].strip()
    values["letter_date"] = _normalize_letter_date(values["letter_date"])
    for key in ("sender", "recipient"):
        values[key] = normalize_organization_forms(values[key])
    return values


def _extract_correspondence_filename_fields(filename: str) -> dict[str, str]:
    """Extract letter number and date hints from the first uploaded filename."""
    stem = re.sub(r"_+", "/", __import__("pathlib").Path(filename or "").stem)
    stem = re.sub(r"/\s*(№|N(?:o|º|°)?|#)\s*/?", r"\1 ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"/\s*(от|исх(?:одящий|\.)?)\s*/?", r" \1 ", stem, flags=re.IGNORECASE)
    values = {"number": "", "letter_date": ""}
    if not stem:
        return values

    date_pattern = r"(?<!\d)(?:[0-3]?\d)[./-](?:[01]?\d)[./-](?:19|20)\d{2}(?!\d)"
    date_match = re.search(date_pattern, stem)
    if date_match:
        values["letter_date"] = _normalize_letter_date(date_match.group(0))

    number_match = re.search(
        r"(?:исх(?:одящий|\.)?\s*)?(?:№|N(?:o|º|°)?|#)\s*([A-ZА-ЯЁ0-9]+(?:\s*[-./\\]\s*[A-ZА-ЯЁ0-9]+){0,6})",
        stem,
        re.IGNORECASE,
    )
    if number_match:
        values["number"] = re.sub(r"\s+", "", number_match.group(1)).strip(" .,:;-")
    elif date_match:
        # Common scan names place the outgoing number immediately before the date.
        prefix = stem[:date_match.start()].rstrip(" _-.,;:")
        candidate = re.search(r"([A-ZА-ЯЁ0-9]+(?:/[A-ZА-ЯЁ0-9]+){0,5})\s*$", prefix, re.IGNORECASE)
        if candidate and re.search(r"\d", candidate.group(1)):
            values["number"] = candidate.group(1).strip(" .,:;-")
    return values


def _extract_correspondence_header_requisites(header: str) -> dict[str, str]:
    """Extract the numbered date pair from the top letterhead OCR only."""
    result = {"number": "", "letter_date": ""}
    if not header.strip():
        return result
    date_pattern = re.compile(
        r"(?<!\d)([0-3]?\d)\s*[./-]\s*([01]?\d)\s*[./-]\s*((?:19|20)\d{2})(?!\d)"
    )
    number_pattern = re.compile(
        r"(?:исх(?:одящий|\.)?\s*)?(?:№|N(?:o|º|°)?|#)\s*([A-ZА-ЯЁ0-9]+(?:\s*[-./\\]\s*[A-ZА-ЯЁ0-9]+){0,6})",
        re.IGNORECASE,
    )
    lines = [re.sub(r"\s+", " ", line).strip() for line in header.splitlines() if line.strip()]
    for line in lines:
        date_match = date_pattern.search(line)
        number_match = number_pattern.search(line)
        if date_match and number_match:
            result["letter_date"] = _normalize_letter_date(
                f"{date_match.group(1)}.{date_match.group(2)}.{date_match.group(3)}"
            )
            result["number"] = re.sub(r"\s+", "", number_match.group(1)).strip(" .,:;-")
            return result
    flattened = " ".join(lines)
    date_match = date_pattern.search(flattened)
    if date_match:
        result["letter_date"] = _normalize_letter_date(
            f"{date_match.group(1)}.{date_match.group(2)}.{date_match.group(3)}"
        )
    number_match = number_pattern.search(flattened)
    if number_match:
        result["number"] = re.sub(r"\s+", "", number_match.group(1)).strip(" .,:;-")
    return result


def _extract_explicit_subject(text: str) -> str:
    """Prefer a short standalone letter subject line over issuer or document labels."""
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" \t\r\n:;—-")
        line = re.sub(r"^(?:тема\s*:\s*)", "", line, flags=re.IGNORECASE).strip()
        if len(line) > 220:
            continue
        if re.match(r"^(?:о\s+|об\s+|по\s+вопросу\s+|касательно\s+)", line, re.IGNORECASE):
            return line.strip(" .;:—-")
    return ""


def _normalize_letter_date(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    match = re.search(r"((?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:19|20)\d{2})", value)
    if not match:
        return ""
    day, month, year = re.split(r"[./-]", match.group(1))
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""
