from __future__ import annotations

from copy import deepcopy
from datetime import date
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from pymorphy3 import MorphAnalyzer

from app.services.document_titles import format_document_title

HANDOVER_TEMPLATE = Path(__file__).resolve().parents[1] / "document_templates" / "handover_act_template.docx"


DOCUMENT_TYPES = {
    "inventory": {
        "title": "Опись документов",
        "button": "Создать опись документов",
        "filename": "Opis_dokumentov",
        "description": "Сформировать перечень загруженных документов проекта.",
    },
    "handover": {
        "title": "Акт приёма-передачи ИРД",
        "button": "Создать акт приёма-передачи ИРД",
        "filename": "Akt_priema_peredachi_IRD",
        "description": "Составить акт по документам, включённым в проект.",
    },
}


REQUEST_TEXT_SPEC = {
    "title": "Текст запроса недостающих исходных данных",
    "description": "Скопируйте текст и оформите письмо самостоятельно.",
}


def generate_missing_data_request_text(project: Any, checklist: list[Any]) -> str:
    replacements = [item for item in checklist if item["replacement_required"]]
    missing = [item for item in checklist if item["status"] == "missing" and not item["replacement_required"]]
    if not missing and not replacements:
        return ""

    if missing and replacements:
        request = "просим предоставить недостающие исходные данные и заменить указанные документы."
    elif missing:
        request = "просим предоставить следующие исходные данные."
    else:
        request = "просим заменить следующие документы."
    lines = [
        f"В рамках исполнения обязательств по договору № {project['contract_number']} "
        f"по объекту «{project['name']}» {request}"
    ]
    if missing:
        lines.extend(["", "Недостающие документы:"])
        lines.extend(f"{index}. {item['title']}" for index, item in enumerate(missing, start=1))
    if replacements:
        lines.extend(["", "Документы, требующие замены:"])
        for index, item in enumerate(replacements, start=1):
            title = item["title"]
            issuer = (item["linked_document_issuer"] or "").strip()
            number = (item["linked_document_number"] or "").strip()
            issue_date = _format_request_date(item["linked_document_issue_date"])
            requisites = []
            if issue_date:
                requisites.append(f"от {issue_date}")
            if number:
                requisites.append(f"№ {number}")
            line = f"{index}. {title}"
            if requisites:
                line += " " + " ".join(requisites)
            if issuer:
                line += f", {_issued_agreement(title)} {_organization_instrumental(issuer)}"
            lines.append(line)
            note = (item["note"] or item["linked_document_note"] or "").strip()
            if note:
                lines.append(f"   Примечание: {note}")
    return "\n".join(lines)


def _format_request_date(value: Any) -> str:
    """Format stored ISO dates for the human-readable request text."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return raw


def _issued_agreement(title: str) -> str:
    """Choose a participle agreeing with the document title."""
    first = (title or "").strip().lower()
    if first.startswith(("технические условия", "технические требования", "сведения", "материалы", "документы")):
        return "выданные"
    if first.startswith(("справка", "копия", "выписка", "ведомость")):
        return "выданная"
    if first.startswith(("решение", "письмо", "свидетельство", "задание", "согласование", "заключение")):
        return "выданное"
    if first.startswith("акт"):
        return "выданный"
    return "выданный"


def _match_case(source: str, replacement: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _organization_instrumental(value: str) -> str:
    """Inflect the issuer phrase while preserving quoted legal names and acronyms."""
    morph = _get_morph_analyzer()
    parts = re.split(r'("[^"\n]*"|«[^»\n]*»)', value)
    for index, part in enumerate(parts):
        if part.startswith(('"', '«')):
            continue
        tokens = re.split(r"([А-Яа-яЁё-]+)", part)
        in_prepositional_phrase = False
        for token_index, token in enumerate(tokens):
            if token == ",":
                in_prepositional_phrase = False
                continue
            if not re.fullmatch(r"[А-Яа-яЁё-]+", token):
                continue
            if token.lower() == "по":
                in_prepositional_phrase = True
                continue
            if in_prepositional_phrase or token.isupper():
                continue
            parsed = morph.parse(token)[0]
            inflected = parsed.inflect({"ablt"})
            if inflected:
                tokens[token_index] = _match_case(token, inflected.word)
        parts[index] = "".join(tokens)
    return "".join(parts)


@lru_cache(maxsize=1)
def _get_morph_analyzer() -> MorphAnalyzer:
    return MorphAnalyzer()


def generate_project_document(
    kind: str,
    project: Any,
    documents: list[Any],
    checklist: list[Any],
    checklist_document_titles: dict[int, str] | None = None,
) -> bytes:
    if kind not in DOCUMENT_TYPES:
        raise ValueError("Неизвестный тип документа.")
    if kind == "handover":
        document = Document(str(HANDOVER_TEMPLATE))
        document.core_properties.title = f"{DOCUMENT_TYPES[kind]['title']} — {project['name']}"
        document.core_properties.subject = f"Проект {project['contract_number']}"
        _populate_handover_template(document, project, documents, checklist_document_titles or {})
        output = BytesIO()
        document.save(output)
        return output.getvalue()

    document = Document()
    _set_document_defaults(document)

    title = DOCUMENT_TYPES[kind]["title"]
    document.core_properties.title = f"{title} — {project['name']}"
    document.core_properties.subject = f"Проект {project['contract_number']}"
    title_paragraph = document.add_paragraph(style="Title")
    title_run = title_paragraph.add_run(title)
    title_run.font.name = "Times New Roman"
    title_run.font.size = Pt(15)
    title_run.font.bold = True
    title_run.font.color.rgb = RGBColor(0, 0, 0)
    _set_run_font(title_run, "Times New Roman")
    _add_project_details(document, project)

    _add_inventory(document, documents)

    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _set_document_defaults(document: Document) -> None:
    section = document.sections[0]
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2)

    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    normal.paragraph_format.space_after = Pt(6)
    _set_style_font(normal, "Times New Roman")

    title = document.styles["Title"]
    title.font.name = "Times New Roman"
    title.font.size = Pt(15)
    title.font.bold = True
    title.font.color.rgb = RGBColor(0, 0, 0)
    title.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(14)
    _set_style_font(title, "Times New Roman")
    if title.element.pPr is not None:
        border = title.element.pPr.find(qn("w:pBdr"))
        if border is not None:
            title.element.pPr.remove(border)

    for style_name in ("Heading 1", "Heading 2"):
        style = document.styles[style_name]
        style.font.name = "Times New Roman"
        style.font.size = Pt(12 if style_name == "Heading 1" else 11)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.bold = True
        style.paragraph_format.keep_with_next = True
        _set_style_font(style, "Times New Roman")


def _set_style_font(style: Any, name: str) -> None:
    r_pr = style.element.get_or_add_rPr()
    fonts = r_pr.rFonts
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        r_pr.append(fonts)
    fonts.set(qn("w:ascii"), name)
    fonts.set(qn("w:hAnsi"), name)
    fonts.set(qn("w:eastAsia"), name)


def _set_run_font(run: Any, name: str) -> None:
    r_pr = run._element.get_or_add_rPr()
    fonts = r_pr.rFonts
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        r_pr.append(fonts)
    fonts.set(qn("w:ascii"), name)
    fonts.set(qn("w:hAnsi"), name)
    fonts.set(qn("w:eastAsia"), name)


def _add_project_details(document: Document, project: Any) -> None:
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run(f"Проект: {project['name']}\nДоговор: {project['contract_number']}\nДата формирования: {date.today():%d.%m.%Y}")
    run.font.size = Pt(11)


def _add_inventory(document: Document, documents: list[Any]) -> None:
    if not documents:
        document.add_paragraph("Документы не добавлены.")
        return

    for number, item in enumerate(documents, start=1):
        title = format_document_title(
            item["working_title"] or item["official_title"] or item["original_filename"],
            item["issuer"], item["issue_date"], item["document_number"],
        )
        paragraph = document.add_paragraph()
        paragraph.add_run(f"{number}. {title}")


def _populate_handover_template(
    document: Document,
    project: Any,
    documents: list[Any],
    checklist_document_titles: dict[int, str],
) -> None:
    """Fill the supplied handover form while retaining its layout and styles."""
    if not document.paragraphs or not document.tables or len(document.tables[0].rows) < 2:
        raise ValueError("Шаблон акта приёма-передачи имеет неожиданную структуру.")
    contract_paragraph = next(
        (paragraph for paragraph in document.paragraphs if paragraph.text.strip().startswith("к договору №")),
        None,
    )
    if contract_paragraph is None:
        raise ValueError("В шаблоне акта не найдено место для реквизитов договора.")
    contract_date = _date_display(project["contract_date"] if "contract_date" in project.keys() else "")
    contract_date_text = contract_date or "__ _____________ 20__"
    _replace_paragraph_text(contract_paragraph, f"к договору № {project['contract_number']} от {contract_date_text} г.")

    object_paragraph = next((paragraph for paragraph in document.paragraphs if paragraph.text.strip() == "{{OBJECT_NAME}}"), None)
    if object_paragraph is None:
        raise ValueError("В шаблоне акта не найдено место для наименования объекта.")
    _replace_paragraph_text(object_paragraph, str(project["name"] or ""))

    table = document.tables[0]
    exemplar = deepcopy(table.rows[1]._tr)
    for row in list(table.rows[1:]):
        table._tbl.remove(row._tr)

    for item in documents:
        title = _customer_letter_title(item, checklist_document_titles.get(item["id"]))
        number = (item["document_number"] or "").strip()
        issue_date = _date_display(item["issue_date"])
        details = ", ".join(part for part in (issue_date, f"№ {number}" if number else "") if part)
        label = f"{title} ({details})" if details else title
        row_xml = deepcopy(exemplar)
        table._tbl.append(row_xml)
        cells = table.rows[-1].cells
        for cell, value in zip(cells, (label, "1", "")):
            _replace_paragraph_text(cell.paragraphs[0], value)


def _customer_letter_title(item: Any, checklist_title: str | None) -> str:
    raw_title = str(item["working_title"] or item["official_title"] or "").strip()
    is_letter = bool(checklist_title) or raw_title.casefold().startswith("письмо заказчика")
    if not is_letter:
        return _document_title(item)
    subject = raw_title
    subject = re.sub(r"^письмо\s+заказчика\s*", "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"^письмо\s*", "", subject, flags=re.IGNORECASE)
    subject = subject.strip(" \t\r\n:—–-«»\"'")
    if not subject and checklist_title:
        subject = re.sub(r"^письмо\s+заказчика\s*", "", checklist_title, flags=re.IGNORECASE).strip(" \t:—–-«»\"'")
    if subject:
        subject = subject[0].upper() + subject[1:]
        return f"Письмо заказчика «{subject}»"
    return "Письмо заказчика"


def _replace_paragraph_text(paragraph: Any, value: str) -> None:
    if paragraph.runs:
        paragraph.runs[0].text = value
        for run in paragraph.runs[1:]:
            run.text = ""
        return
    paragraph.add_run(value)


def _document_title(item: Any) -> str:
    return item["working_title"] or item["official_title"] or item["original_filename"] or "Документ"


def _date_display(value: str | None) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return value
