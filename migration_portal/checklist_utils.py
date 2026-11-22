"""Checklist naming rules shared by the Django views and document worker."""

import re

from app.services.document_titles import clean_document_name

CUSTOMER_LETTER_ITEM_TITLE = "Сведения для разработки сметной документации (по типовой форме)"
PROJECT_ASSIGNMENT_ITEM_TITLE = "Задание на проектирование"
MULTI_DOCUMENT_ITEM_TITLES = {CUSTOMER_LETTER_ITEM_TITLE, PROJECT_ASSIGNMENT_ITEM_TITLE}


def normalize(value):
    return " ".join((value or "").casefold().split())


def allows_multiple_documents(title):
    key = normalize(title)
    return any(key == normalize(value) for value in MULTI_DOCUMENT_ITEM_TITLES)


def uses_customer_letter_subject(title):
    return normalize(title) == normalize(CUSTOMER_LETTER_ITEM_TITLE)


def apply_checklist_document_title(parsed, item_title):
    """Prefer the recognized document name; use the checklist title as fallback."""
    normalized = normalize(item_title)
    if normalized == normalize("Письмо заказчика о заключении договора"):
        subject = _explicit_subject(parsed.raw_text)
        if subject:
            subject = re.sub(r"^письмо\s+", "", subject, flags=re.IGNORECASE).strip()
            if subject[:3].casefold() == "об ":
                subject = "об " + subject[3:]
            elif subject[:2].casefold() == "о ":
                subject = "о " + subject[2:]
            elif re.match(r"^(?:по\s+вопросу|касательно)\b", subject, re.IGNORECASE):
                subject = subject[:1].lower() + subject[1:]
            else:
                subject = "о " + subject[:1].lower() + subject[1:]
            parsed.working_title = f"Письмо {subject}"
            return
    if uses_customer_letter_subject(item_title):
        subject = _explicit_subject(parsed.raw_text)
        if subject:
            parsed.working_title = subject
            parsed.official_title = subject
            return
    # Long issuing-agency names often appear in the page header and get
    # appended by classification to the document's own title. When the scan
    # has a standalone title line, keep that short title in the editable name
    # field and leave the agency in issuer.
    first_page = (parsed.raw_text or "").split("--- page 2 ---", 1)[0]
    standalone_title = re.search(
        r"(?im)^\s*(ТЕХНИЧЕСКИЕ\s+ТРЕБОВАНИЯ)\s*[.:]?\s*$",
        first_page,
    )
    if standalone_title:
        parsed.working_title = "Технические требования"
        return
    recognized_title = (parsed.official_title or "").strip()
    working_title = (parsed.working_title or "").strip()
    if normalize(recognized_title) == normalize("Неопределенный документ"):
        recognized_title = ""
    if normalize(working_title) == normalize("Неопределенный документ"):
        working_title = ""
    parsed.working_title = clean_document_name(recognized_title or working_title or (item_title or "").strip(), parsed.issuer)


def _explicit_subject(text):
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" \t\r\n:;—-")
        line = re.sub(r"^(?:тема\s*:\s*)", "", line, flags=re.IGNORECASE).strip()
        if len(line) <= 220 and re.match(r"^(?:о\s+|об\s+|по\s+вопросу\s+|касательно\s+)", line, re.IGNORECASE):
            return line.strip(" .;:—-")
    return ""
