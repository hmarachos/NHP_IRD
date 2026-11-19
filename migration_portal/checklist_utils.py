"""Checklist naming rules shared by the Django views and document worker."""

import re

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
    """Keep the user-selected checklist item as the working document title."""
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
            parsed.working_title = f"Письмо {subject}"[:300]
            return
    if uses_customer_letter_subject(item_title):
        subject = _explicit_subject(parsed.raw_text)
        if subject:
            parsed.working_title = subject[:300]
            parsed.official_title = subject[:300]
            return
    parsed.working_title = (item_title or "")[:300]


def _explicit_subject(text):
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" \t\r\n:;—-")
        line = re.sub(r"^(?:тема\s*:\s*)", "", line, flags=re.IGNORECASE).strip()
        if len(line) <= 220 and re.match(r"^(?:о\s+|об\s+|по\s+вопросу\s+|касательно\s+)", line, re.IGNORECASE):
            return line.strip(" .;:—-")
    return ""
