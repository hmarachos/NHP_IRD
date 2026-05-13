import re

from app.models import ParsedDocument


DATE_RE = re.compile(r"\b([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})\b")
ORG_KEYWORDS = (
    "государственное учреждение",
    "республиканское унитарное предприятие",
    "коммунальное унитарное предприятие",
    "унитарное предприятие",
    "учреждение",
    "управление",
    "комитет",
    "исполком",
    "предприятие",
    "центр",
)
BELARUSIAN_MARKERS = ("дзяржаў", "рэспубл", "установа", "маладзеч", "гігіен", "эпідэмі", "аховы здар")


class RegexFieldExtractor:
    def extract(self, text: str) -> ParsedDocument:
        normalized = self._normalize(text)
        return ParsedDocument(
            official_title=self._extract_title(normalized),
            issuer=self._extract_issuer(text),
            issue_date=self._extract_issue_date(normalized),
            valid_until=self._extract_valid_until(normalized),
            validity_text=self._extract_validity_text(normalized),
            no_expiration=self._is_without_expiration(normalized),
            raw_text=text,
        )

    def _extract_title(self, text: str) -> str:
        lines = [line.strip(" .,:;") for line in text.splitlines() if line.strip()]
        for marker in ("ТЕХНИЧЕСКИЕ УСЛОВИЯ", "ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ", "РЕШЕНИЕ"):
            if marker.lower() in text.lower():
                return marker
        for line in lines[:25]:
            if 8 <= len(line) <= 160 and line.upper() == line:
                return line
        return lines[0] if lines else ""

    def _extract_issuer(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("--- page")]
        organization = self._extract_russian_organization_from_header(lines[:25])
        if organization:
            return organization

        for line in lines[:25]:
            candidate = self._prefer_russian_text(line)
            lowered = candidate.lower()
            if any(word in lowered for word in ORG_KEYWORDS):
                return self._clean_issuer(candidate)

        match = re.search(r"(?:выдан[оы]?|кем выдан[оы]?)\s*[:\-]\s*(.+)", text, re.IGNORECASE)
        return self._clean_issuer(self._prefer_russian_text(match.group(1))) if match else ""

    def _extract_russian_organization_from_header(self, lines: list[str]) -> str:
        candidates = []
        for start, line in enumerate(lines):
            segment = self._prefer_russian_text(line)
            if not self._looks_like_organization_start(segment):
                continue

            parts = [segment]
            for following in lines[start + 1 : start + 5]:
                following_segment = self._prefer_russian_text(following)
                if self._looks_like_organization_continuation(following_segment):
                    parts.append(following_segment)
                    if "»" in following_segment or ")" in following_segment:
                        break
                    continue
                if parts and len(parts) > 1:
                    break

            candidate = self._clean_issuer(" ".join(parts))
            if candidate:
                candidates.append(candidate)

        if not candidates:
            return ""

        quoted = [candidate for candidate in candidates if "«" in candidate or '"' in candidate]
        return max(quoted or candidates, key=len)

    def _prefer_russian_text(self, value: str) -> str:
        value = re.sub(r".*?(?=ГОСУДАРСТВЕННОЕ УЧРЕЖДЕНИЕ)", "", value)
        value = re.sub(r".*?(?=РЕСПУБЛИКАНСКОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ)", "", value)
        value = re.sub(r".*?(?=КОММУНАЛЬНОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ)", "", value)
        segments = [part.strip(" -—\t") for part in re.split(r"\s{2,}|—(?=«?[А-ЯЁ])", value) if part.strip(" -—\t")]
        if not segments:
            return value.strip()

        russian_segments = [segment for segment in segments if self._is_russian_segment(segment)]
        if russian_segments:
            return max(russian_segments, key=self._russian_score)

        return max(segments, key=self._russian_score)

    def _is_russian_segment(self, value: str) -> bool:
        lowered = value.lower()
        return self._russian_score(value) > 0 and not any(marker in lowered for marker in BELARUSIAN_MARKERS)

    def _russian_score(self, value: str) -> int:
        lowered = value.lower()
        score = sum(3 for keyword in ORG_KEYWORDS if keyword in lowered)
        score += sum(1 for word in ("россии", "республики беларусь", "гигиены", "эпидемиологии", "молодечненский") if word in lowered)
        score -= sum(3 for marker in BELARUSIAN_MARKERS if marker in lowered)
        score -= sum(1 for char in ("ў", "і", "ґ")) if any(char in lowered for char in ("ў", "і", "ґ")) else 0
        return score

    def _looks_like_organization_start(self, value: str) -> bool:
        lowered = value.lower()
        return any(keyword in lowered for keyword in ORG_KEYWORDS)

    def _looks_like_organization_continuation(self, value: str) -> bool:
        cleaned = value.strip()
        lowered = cleaned.lower()
        if any(marker in lowered for marker in ("ул.", "тел", "e-mail", "р/счет", "унп", "окпо", "№", " no ")):
            return False
        return (
            "«" in cleaned
            or "»" in cleaned
            or cleaned.startswith("(")
            or cleaned.endswith(")")
            or cleaned.upper() == cleaned
            or any(word in lowered for word in ("гигиены", "эпидемиологии", "центр"))
        )

    @staticmethod
    def _clean_issuer(value: str) -> str:
        value = re.sub(r"\s+", " ", value)
        value = re.sub(r"\s+([»),.;:])", r"\1", value)
        value = re.sub(r"([«(])\s+", r"\1", value)
        return value.strip(" .,:;-")

    def _extract_issue_date(self, text: str) -> str:
        for line in [line.strip() for line in text.splitlines()[:45] if line.strip()]:
            if re.search(r"^(?:на\s*)?№", line, re.IGNORECASE):
                continue
            if "№" in line or re.search(r"\bN(?:o)?\b", line):
                match = DATE_RE.search(line)
                if match:
                    return self._format_date(match.group(1))

        for pattern in (
            r"([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})\s*(?:г\.?)?\s*(?:№|N|No)",
            r"(?:дата выдачи)\s*[:\-]?\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})",
            r"\b([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})\b",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return self._format_date(match.group(1))
        return ""

    def _extract_valid_until(self, text: str) -> str:
        patterns = (
            r"(?:срок действия|действительн[а-я ]*до|действует до)\s*[:\-]?\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})",
            r"(?:до)\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return self._format_date(match.group(1))
        return ""

    def _extract_validity_text(self, text: str) -> str:
        for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
            if "срок" in line.lower() or "действ" in line.lower():
                return line[:500]
        return ""

    def _is_without_expiration(self, text: str) -> bool:
        lowered = text.lower()
        if any(marker in lowered for marker in ("бессрочно", "без срока", "срок действия не установлен")):
            return True
        return not self._extract_valid_until(text)

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[ \t]+", " ", text.replace("\r", "\n"))

    @staticmethod
    def _format_date(value: str) -> str:
        return value.replace(".", "-").replace("/", "-")
