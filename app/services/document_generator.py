from __future__ import annotations

from datetime import date
from io import BytesIO
from typing import Any

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


DOCUMENT_TYPES = {
    "inventory": {
        "title": "Опись документов",
        "button": "Создать опись документов",
        "filename": "Opis_dokumentov",
        "description": "Сформировать перечень загруженных документов проекта.",
    },
    "request": {
        "title": "Запрос недостающих исходных данных",
        "button": "Запрос недостающих исходных данных",
        "filename": "Zapros_nedostayushchih_dannyh",
        "description": "Подготовить письмо со списком ожидаемых документов.",
    },
    "handover": {
        "title": "Акт приёма-передачи ИРД",
        "button": "Создать акт приёма-передачи ИРД",
        "filename": "Akt_priema_peredachi_IRD",
        "description": "Составить акт по документам, включённым в проект.",
    },
}


def generate_project_document(
    kind: str,
    project: Any,
    documents: list[Any],
    checklist: list[Any],
    fields: dict[str, str] | None = None,
) -> bytes:
    if kind not in DOCUMENT_TYPES:
        raise ValueError("Неизвестный тип документа.")
    fields = fields or {}
    document = Document()
    _set_document_defaults(document)

    title = DOCUMENT_TYPES[kind]["title"]
    document.core_properties.title = f"{title} — {project['name']}"
    document.core_properties.subject = f"Проект {project['contract_number']}"
    if kind != "request":
        title_paragraph = document.add_paragraph(style="Title")
        title_run = title_paragraph.add_run(title)
        title_run.font.name = "Times New Roman"
        title_run.font.size = Pt(15)
        title_run.font.bold = True
        title_run.font.color.rgb = RGBColor(0, 0, 0)
        _set_run_font(title_run, "Times New Roman")
        _add_project_details(document, project)

    if kind == "inventory":
        _add_inventory(document, documents, checklist)
    elif kind == "request":
        _add_missing_data_request(document, project, checklist, fields)
    else:
        _add_handover_act(document, documents, fields)

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


def _add_inventory(document: Document, documents: list[Any], checklist: list[Any]) -> None:
    document.add_paragraph(
        "Опись содержит документы, находящиеся в активном составе архива проекта на дату формирования."
    )
    if not documents:
        document.add_paragraph("В архив проекта пока не добавлены документы.")
        return

    linked_titles = {
        item["document_id"]: item["title"]
        for item in checklist
        if item["document_id"] is not None
    }
    rows = []
    for number, item in enumerate(documents, start=1):
        title = _document_title(item)
        linked = linked_titles.get(item["id"], "")
        if linked and linked.casefold() != title.casefold():
            title = f"{linked}: {title}"
        rows.append([
            str(number),
            title,
            item["issuer"] or "",
            _date_display(item["issue_date"]),
            _date_display(item["valid_until"]),
            item["original_filename"] or "",
        ])
    _add_table(document, ["№", "Документ", "Кем выдан", "Дата выдачи", "Действителен до", "Имя файла"], rows)
    document.add_paragraph(f"Всего документов: {len(documents)}.")


def _add_missing_data_request(document: Document, project: Any, checklist: list[Any], fields: dict[str, str]) -> None:
    replacements = [item for item in checklist if item["replacement_required"]]
    missing = [item for item in checklist if item["status"] == "missing" and not item["replacement_required"]]
    header = document.add_table(rows=1, cols=2)
    header.alignment = WD_TABLE_ALIGNMENT.CENTER
    header.autofit = False
    header.columns[0].width = Cm(7.5)
    header.columns[1].width = Cm(8.5)
    left, right = header.rows[0].cells
    left.width = Cm(7.5)
    right.width = Cm(8.5)
    left.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    right.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    outgoing_date = _date_display(fields.get("date")) or "«___» __________ 20__ г."
    left.text = f"№ {fields.get('number') or '_______'} от {outgoing_date}\nНа № {fields.get('incoming_number') or '_______'} от {_date_display(fields.get('incoming_date')) or '__________'}"
    recipient = fields.get("recipient") or project["customer"] or ""
    right.text = recipient
    for cell in (left, right):
        _remove_cell_borders(cell)
        for paragraph in cell.paragraphs:
            paragraph.paragraph_format.space_after = Pt(2)
            for run in paragraph.runs:
                _set_run_font(run, "Times New Roman")
                run.font.size = Pt(12)
                run.font.color.rgb = RGBColor(0, 0, 0)
    for paragraph in right.paragraphs:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    subject = document.add_paragraph()
    subject.paragraph_format.space_before = Pt(12)
    subject.paragraph_format.space_after = Pt(18)
    subject.add_run(f"О предоставлении исходных данных\n(объект №{project['contract_number']})")

    greeting_name = fields.get("greeting_name", "").strip()
    if greeting_name:
        greeting = document.add_paragraph()
        greeting.paragraph_format.left_indent = Cm(4)
        greeting.paragraph_format.space_before = Pt(4)
        greeting.paragraph_format.space_after = Pt(12)
        salutation = greeting_name.rstrip("! .")
        if not salutation.casefold().startswith("уважаемый"):
            salutation = f"Уважаемый {salutation}"
        greeting.add_run(f"{salutation}!")

    contract_date = _date_display(fields.get("contract_date"))
    contract_reference = f"договору №{project['contract_number']}"
    if contract_date:
        contract_reference += f" от {contract_date}"
    if missing:
        opening_text = (
            f"В рамках исполнения обязательств по {contract_reference} по объекту "
            f"«{project['name']}» просим Вас предоставить следующие данные:"
        )
    else:
        opening_text = (
            f"В рамках исполнения обязательств по {contract_reference} по объекту "
            f"«{project['name']}» просим Вас заменить следующие документы:"
        )
    opening = document.add_paragraph(opening_text)
    opening.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    opening.paragraph_format.first_line_indent = Cm(1.25)
    opening.paragraph_format.space_after = Pt(5)

    if missing:
        _add_numbered_request_items(document, missing, include_notes=False)
    if replacements:
        heading = document.add_paragraph("Документы, требующие замены:")
        heading.paragraph_format.space_before = Pt(8)
        heading.paragraph_format.space_after = Pt(3)
        _add_numbered_request_items(document, replacements, include_notes=True)

    deadline = _date_display(fields.get("deadline"))
    if deadline:
        paragraph = document.add_paragraph(f"Информацию просим представить не позднее {deadline}.")
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        paragraph.paragraph_format.first_line_indent = Cm(1.25)
        paragraph.paragraph_format.space_before = Pt(8)

    signature = document.add_table(rows=1, cols=2)
    signature.alignment = WD_TABLE_ALIGNMENT.CENTER
    signature.autofit = False
    for cell, width, value in zip(
        signature.rows[0].cells,
        (Cm(9), Cm(7)),
        (fields.get("signatory_position") or "", fields.get("sender_signatory") or ""),
    ):
        cell.width = width
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.BOTTOM
        cell.text = value
        _remove_cell_borders(cell)
        for paragraph in cell.paragraphs:
            paragraph.paragraph_format.space_before = Pt(18)
            paragraph.paragraph_format.space_after = Pt(0)
    for paragraph in signature.rows[0].cells[1].paragraphs:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    if fields.get("contact_phone"):
        phone = document.add_paragraph(fields["contact_phone"])
        phone.paragraph_format.space_before = Pt(10)
        phone.paragraph_format.space_after = Pt(0)
        for run in phone.runs:
            run.font.size = Pt(9)
            _set_run_font(run, "Times New Roman")


def _add_numbered_request_items(document: Document, items: list[Any], *, include_notes: bool) -> None:
    for index, item in enumerate(items, start=1):
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        paragraph.paragraph_format.left_indent = Cm(0.8)
        paragraph.paragraph_format.first_line_indent = Cm(-0.8)
        paragraph.paragraph_format.space_after = Pt(3)
        paragraph.add_run(f"{index}. {item['title']}")
        if include_notes:
            note = (item["note"] or item["linked_document_note"] or "").strip()
            if note:
                comment = document.add_paragraph()
                comment.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                comment.paragraph_format.left_indent = Cm(1.4)
                comment.paragraph_format.space_after = Pt(5)
                comment.add_run(f"Примечание: {note}")


def _add_handover_act(document: Document, documents: list[Any], fields: dict[str, str]) -> None:
    act_date = _form_date(fields.get("date"))
    act_number = fields.get("number") or "________"
    document.add_paragraph(f"№ {act_number} от {act_date}")
    document.add_paragraph(
        f"Передающая сторона: {fields.get('sender') or '________________________________________'}"
    )
    document.add_paragraph(
        f"Принимающая сторона: {fields.get('recipient') or '________________________________________'}"
    )
    document.add_paragraph(
        "Стороны составили настоящий акт о том, что передающая сторона передала, а принимающая сторона приняла перечисленные ниже исходные данные по проекту."
    )
    if documents:
        _add_table(
            document,
            ["№", "Наименование документа", "Кем выдан", "Дата документа", "Файл"],
            [[
                str(i),
                _document_title(item),
                item["issuer"] or "",
                _date_display(item["issue_date"]),
                item["original_filename"] or "",
            ] for i, item in enumerate(documents, start=1)],
        )
    else:
        document.add_paragraph("Документы для передачи не указаны.")
    document.add_paragraph("Замечания при приёме: __________________________________________________________")
    document.add_paragraph("Настоящий акт составлен в двух экземплярах, имеющих равную силу.")
    _add_signature_blocks(document, fields)


def _add_signature_blocks(document: Document, fields: dict[str, str]) -> None:
    table = document.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for cell, party in zip(table.rows[0].cells, (fields.get("sender") or "Передающая сторона", fields.get("recipient") or "Принимающая сторона")):
        cell.width = Cm(7.5)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
        cell.text = f"{party}\n\n________________ / __________________\n       подпись                 Ф.И.О."
        _remove_cell_borders(cell)


def _add_table(document: Document, headings: list[str], rows: list[list[str]]) -> None:
    table = document.add_table(rows=1, cols=len(headings))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = True
    header = table.rows[0]
    _repeat_table_header(header)
    for cell, heading in zip(header.cells, headings):
        cell.text = heading
        _shade_cell(cell, "E7F1EF")
        _format_cell(cell, bold=True)
    for values in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, values):
            cell.text = str(value or "")
            _format_cell(cell)
    document.add_paragraph()


def _format_cell(cell: Any, bold: bool = False) -> None:
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    for paragraph in cell.paragraphs:
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.space_before = Pt(2)
        for run in paragraph.runs:
            run.font.name = "Times New Roman"
            run.font.size = Pt(9)
            run.font.bold = bold
            run.font.color.rgb = RGBColor(0, 0, 0)


def _shade_cell(cell: Any, fill: str) -> None:
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shading)


def _repeat_table_header(row: Any) -> None:
    properties = row._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    properties.append(repeat)


def _remove_cell_borders(cell: Any) -> None:
    properties = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "nil")
        borders.append(element)
    properties.append(borders)


def _document_title(item: Any) -> str:
    return item["working_title"] or item["official_title"] or item["original_filename"] or "Документ"


def _date_display(value: str | None) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return value


def _form_date(value: str | None) -> str:
    if not value:
        return date.today().strftime("%d.%m.%Y")
    try:
        return date.fromisoformat(value[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return value
