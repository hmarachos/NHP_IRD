"""Locate the source paragraph on a rendered PDF page for analytics citations."""
import re
import unicodedata


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
    return token in target or any(
        len(candidate) >= 4 and candidate.startswith(token[:min(5, len(token))])
        for candidate in target
    )


def highlight_paragraph(pdf_path, page_index, reference_text, *, lang="rus+eng", dpi=240):
    """Return normalized rectangles for the OCR paragraph most related to a citation.

    OCR is performed only for the cited page when the user opens a citation.
    Each rectangle covers one text line, so the browser can draw a translucent
    yellow marker over the original page without modifying the source file.
    """
    query = _tokens(reference_text)
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
            if not token_set:
                continue
            key = (data["block_num"][index], data["par_num"][index])
            entry = paragraphs.setdefault(key, {"words": set(), "lines": {}})
            entry["words"].update(token_set)
            line_key = (key[0], key[1], data["line_num"][index])
            x, y = int(data["left"][index]), int(data["top"][index])
            right = x + int(data["width"][index])
            bottom = y + int(data["height"][index])
            line = entry["lines"].setdefault(line_key, {"rect": [x, y, right, bottom], "words": set()})
            line["rect"][0] = min(line["rect"][0], x)
            line["rect"][1] = min(line["rect"][1], y)
            line["rect"][2] = max(line["rect"][2], right)
            line["rect"][3] = max(line["rect"][3], bottom)
            line["words"].update(token_set)

        scored = []
        for entry in paragraphs.values():
            lines = sorted(entry["lines"].items(), key=lambda item: item[0][2])
            matched_lines = []
            for line_key, line in lines:
                hits = {term for term in query if _matches(term, line["words"])}
                if hits:
                    matched_lines.append((line_key[2], hits))
            # Tesseract may group several numbered requirements into one
            # paragraph. Keep the contiguous line run that best matches the
            # cited answer instead of tinting the entire OCR block.
            clusters = []
            for line_number, hits in matched_lines:
                if clusters and line_number <= clusters[-1]["last"] + 2:
                    clusters[-1]["last"] = line_number
                    clusters[-1]["hits"].update(hits)
                else:
                    clusters.append({"first": line_number, "last": line_number, "hits": set(hits)})
            for cluster in clusters:
                if cluster["hits"]:
                    selected_lines = [
                        line for line_key, line in lines
                        if cluster["first"] <= line_key[2] <= cluster["last"]
                    ]
                    scored.append((len(cluster["hits"]), len(cluster["hits"]) / len(query), selected_lines))
        if not scored:
            return []
        hit_count, coverage, best_lines = max(
            scored, key=lambda item: (item[0], item[1], -len(item[2])),
        )
        if hit_count < min(2, len(query)) or coverage < 0.12:
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
