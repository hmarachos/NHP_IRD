import re
from datetime import date

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
YEAR_WORDS = {
    "одного": 1,
    "один": 1,
    "года": 1,
    "двух": 2,
    "два": 2,
    "трех": 3,
    "три": 3,
    "четырех": 4,
    "четыре": 4,
    "пяти": 5,
    "пять": 5,
}


class RegexFieldExtractor:
    def extract(self, text: str) -> ParsedDocument:
        normalized = self._normalize(text)
        issue_date = self._extract_issue_date(normalized)
        valid_until = self._extract_valid_until(normalized, issue_date)
        return ParsedDocument(
            official_title=self._extract_title(normalized),
            issuer=self._extract_issuer(text),
            issue_date=issue_date,
            valid_until=valid_until,
            validity_text=self._extract_validity_text(normalized),
            no_expiration=self._is_without_expiration(normalized, valid_until),
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

    def _extract_valid_until(self, text: str, issue_date: str = "") -> str:
        patterns = (
            r"(?:срок действия|действительн[а-я ]*до|действует до)\s*[:\-]?\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})",
            r"(?:до)\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return self._format_date(match.group(1))
        return self._calculate_valid_until_from_duration(text, issue_date)

    def _calculate_valid_until_from_duration(self, text: str, issue_date: str) -> str:
        if not issue_date:
            return ""

        duration_years = self._extract_duration_years(text)
        if not duration_years:
            return ""

        parsed_issue_date = self._parse_date(issue_date)
        if not parsed_issue_date:
            return ""

        return self._format_date_from_date(self._add_years(parsed_issue_date, duration_years))

    def _extract_duration_years(self, text: str) -> int:
        patterns = (
            r"(?:срок[^.\n]{0,120}|действ\w*[^.\n]{0,120})в\s+течение\s+(\d+|[а-яё]+)\s+лет?",
            r"в\s+течение\s+(\d+|[а-яё]+)\s+лет?[^.\n]{0,160}(?:с\s+даты|со\s+дня)\s+(?:их\s+)?выдачи",
            r"(?:с\s+даты|со\s+дня)\s+(?:их\s+)?выдачи[^.\n]{0,160}в\s+течение\s+(\d+|[а-яё]+)\s+лет?",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            value = match.group(1).lower().replace("ё", "е")
            if value.isdigit():
                return int(value)
            if value in YEAR_WORDS:
                return YEAR_WORDS[value]
        return 0

    def _extract_validity_text(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        preferred_markers = ("настоящие технические требования действуют", "в течение", "срок действия")
        for index, line in enumerate(lines):
            lowered = line.lower()
            if any(marker in lowered for marker in preferred_markers):
                return " ".join(lines[index : index + 4])[:500]

        for index, line in enumerate(lines):
            lowered = line.lower()
            if "срок" in lowered or "действ" in lowered:
                return " ".join(lines[index : index + 4])[:500]
        return ""

    def _is_without_expiration(self, text: str, valid_until: str = "") -> bool:
        if valid_until:
            return False
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

    @staticmethod
    def _parse_date(value: str) -> date | None:
        try:
            day, month, year = [int(part) for part in value.replace(".", "-").replace("/", "-").split("-")]
            return date(year, month, day)
        except ValueError:
            return None

    @staticmethod
    def _add_years(value: date, years: int) -> date:
        try:
            return value.replace(year=value.year + years)
        except ValueError:
            return value.replace(month=2, day=28, year=value.year + years)

    @staticmethod
    def _format_date_from_date(value: date) -> str:
        return value.strftime("%d-%m-%Y")
