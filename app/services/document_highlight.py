"""Locate the source paragraph on a rendered PDF page for analytics citations."""
import re
import unicodedata


HIGHLIGHT_VERSION = 2


_WORDS = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+", re.UNICODE)
_CONFUSABLES = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к",
    "m": "м", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
})
_COMMON = {
    "этот", "этого", "также", "который", "которые", "должен", "должны",
    "необходимо", "предусмотреть", "объект", "проект", "стадии", "работ",
}


def _tokens(value):
    result = set()
    for raw in _WORDS.findall(unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")):
        token = raw.translate(_CONFUSABLES) if re.search(r"[а-я]", raw) else raw
        if len(token) >= 4 and token not in _COMMON:
            result.add(token)
    return result


def _matches(token, target):
    if token.startswith("@number:"):
        return token in target
    return token in target or any(
        len(candidate) >= 4 and candidate.startswith(token[:min(5, len(token))])
        for candidate in target
    )


_NUMBER_RE = re.compile(r"(?<![\w.])([+-]?\d+(?:[.,]\d+)?)(?![\w.])")
_UNIT_NUMBER_RE = re.compile(
    r"(?<!\w)([+-]?\d+(?:[.,]\d+)?)\s*(?:[°º]\s*[CС]|кВт|кВА|ВА|VA|м[²³23]|%)(?!\w)",
    re.IGNORECASE,
)


def _number_token(value):
    return "@number:" + value.replace(",", ".").lstrip("+")


def _unit_numbers(text):
    return {_number_token(match.group(1)) for match in _UNIT_NUMBER_RE.finditer(text or "")}


def _select_lines(paragraphs, query):
    """Prefer physical values when the cited sentence contains explicit units."""
    scored = []
    for entry in paragraphs.values():
        lines = sorted(entry["lines"].items(), key=lambda item: item[0][2])
        matched_lines = []
        for line_key, line in lines:
            # OCR can split a value and its unit into separate words. Assemble
            # the complete line before matching numbers to the cited quantity.
            line_text = " ".join(line.get("text", []))
            numeric = _unit_numbers(line_text)
            numeric.update(
                _number_token(match.group(1)) for match in _NUMBER_RE.finditer(line_text)
                if match.group(1).startswith(("+", "-")) or re.search(r"[.,]", match.group(1))
            )
            words = line["words"] | numeric
            hits = {term for term in query if _matches(term, words)}
            if hits:
                matched_lines.append((line_key[2], hits))
        clusters = []
        for line_number, hits in matched_lines:
            if clusters and line_number <= clusters[-1]["last"] + 2:
                clusters[-1]["last"] = line_number
                clusters[-1]["hits"].update(hits)
            else:
                clusters.append({"first": line_number, "last": line_number, "hits": set(hits)})
        for cluster in clusters:
            selected = [line for key, line in lines if cluster["first"] <= key[2] <= cluster["last"]]
            numeric_hits = sum(term.startswith("@number:") for term in cluster["hits"])
            scored.append((numeric_hits, len(cluster["hits"]), len(cluster["hits"]) / len(query), selected))
    if not scored:
        return []
    numeric_hits, hit_count, coverage, lines = max(scored, key=lambda item: (item[0], item[1], item[2], -len(item[3])))
    if not numeric_hits and (hit_count < min(2, len(query)) or coverage < 0.12):
        return []
    return lines


def highlight_paragraph(pdf_path, page_index, reference_text, *, lang="rus+eng", dpi=240):
    """Return normalized rectangles for the OCR paragraph most related to a citation.

    OCR is performed only for the cited page when the user opens a citation.
    Each rectangle covers one text line, so the browser can draw a translucent
    yellow marker over the original page without modifying the source file.
    """
    query = _tokens(reference_text) | _unit_numbers(reference_text)
    if not query:
        return []

    import pypdfium2 as pdfium
    import pytesseract
    from PIL import ImageFilter, ImageOps

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_index < 0 or page_index >= len(pdf):
            return []
        page = pdf[page_index]
        scale = max(1.5, min(float(dpi) / 72, 3.5))
        image = page.render(scale=scale).to_pil().convert("RGB")
        image = ImageOps.autocontrast(ImageOps.grayscale(image))
        image = image.filter(ImageFilter.MedianFilter(size=3)).filter(ImageFilter.SHARPEN)
        image = image.point(lambda value: 255 if value > 175 else 0)
        data = pytesseract.image_to_data(
            image, lang=lang,
            config="--oem 1 --psm 6 -c preserve_interword_spaces=1",
            output_type=pytesseract.Output.DICT,
        )
        paragraphs = {}
        for index, raw in enumerate(data.get("text", [])):
            word = raw.strip()
            if not word:
                continue
            token_set = _tokens(word)
            key = (data["block_num"][index], data["par_num"][index])
            entry = paragraphs.setdefault(key, {"words": set(), "lines": {}})
            entry["words"].update(token_set)
            line_key = (key[0], key[1], data["line_num"][index])
            x, y = int(data["left"][index]), int(data["top"][index])
            right = x + int(data["width"][index])
            bottom = y + int(data["height"][index])
            line = entry["lines"].setdefault(line_key, {"rect": [x, y, right, bottom], "words": set(), "text": []})
            line["rect"][0] = min(line["rect"][0], x)
            line["rect"][1] = min(line["rect"][1], y)
            line["rect"][2] = max(line["rect"][2], right)
            line["rect"][3] = max(line["rect"][3], bottom)
            line["words"].update(token_set)
            line["text"].append(word)

        best_lines = _select_lines(paragraphs, query)
        if not best_lines:
            return []

        width, height = image.size
        rects = []
        for line in best_lines:
            x0, y0, x1, y1 = line["rect"]
            pad_x, pad_y = max(2, int(width * 0.002)), max(2, int(height * 0.0015))
            left = max(0, x0 - pad_x)
            top = max(0, y0 - pad_y)
            right = min(width, x1 + pad_x)
            bottom = min(height, y1 + pad_y)
            rects.append({
                "x": left / width, "y": top / height,
                "width": (right - left) / width, "height": (bottom - top) / height,
            })
        return rects
    finally:
        pdf.close()
