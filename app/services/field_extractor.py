import re
from datetime import date

from app.models import ParsedDocument
from app.services.organization_names import normalize_organization_forms


DATE_RE = re.compile(r"(?<!\d)([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})(?!\d)")
MONTHS_RU = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
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
    "общество с ограниченной ответственностью",
    "совместное общество с ограниченной ответственностью",
    "открытое акционерное общество",
    "закрытое акционерное общество",
    "общество с дополнительной ответственностью",
    "частное предприятие",
    "ооо",
    "оао",
    "зао",
    "белорусский народный банк",
)
BELARUSIAN_MARKERS = ("дзяржаў", "рэспубл", "установа", "маладзеч", "гігіен", "эпідэмі", "аховы здар")
YEAR_WORDS = {
    "одного": 1,
    "один": 1,
    "года": 1,
    "двух": 2,
    "два": 2,
    "две": 2,
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
        for line in lines[:35]:
            if re.match(r"^архитектурно[\s-]+планировочное\s+задание\b", line, re.IGNORECASE):
                return "Архитектурно-планировочное задание"
        for line in lines[:25]:
            if re.match(r"^(?:технические условия|технические требования|решение)\b", line, re.IGNORECASE) and 10 <= len(line) <= 220:
                return line
        for marker in ("ТЕХНИЧЕСКИЕ УСЛОВИЯ", "ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ", "РЕШЕНИЕ"):
            if marker.lower() in text.lower():
                return marker
        for line in lines[:25]:
            if 8 <= len(line) <= 160 and line.upper() == line:
                return line
        return lines[0] if lines else ""

    def _extract_issuer(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("--- page")]
        first_page = text.split("--- page 2 ---", 1)[0]
        environmental_examiner = re.search(
            r"Государственное\s+учреждение\s+образования\s+[«\"]?Республиканский\s+центр\s+государственной\s+экологической\s+экспертизы\s*,?\s*подготовки\s*,?\s*повышения\s+квалификации\s+и\s+переподготовки\s+кадров[»\"]?\s+Министерства\s+природных\s+ресурсов\s+и\s+охраны\s+окружающей\s+среды\s+Республики\s+Беларусь",
            first_page,
            re.IGNORECASE | re.DOTALL,
        )
        if environmental_examiner:
            return self._clean_issuer(environmental_examiner.group(0))
        if re.search(r"архитектурно[\s-]+планировочное\s+задание", first_page, re.IGNORECASE):
            approved_block = first_page.split("УТВЕРЖДАЮ", 1)[-1].split("АРХИТЕКТУРНО", 1)[0].replace("|", " ")
            apz_issuer = re.search(
                r"\b([А-ЯЁ][а-яё-]+ского)\s+райис\w*кома\b",
                approved_block,
                re.IGNORECASE,
            )
            if apz_issuer:
                district = apz_issuer.group(1).lower()
                return f"{district[:-3].capitalize()}ий райисполком"
        district_committee = re.search(
            r"\b([А-ЯЁ][а-яё-]+\s+районный\s+исполнительный\s+комитет)\s+(?:решил|решила)\b",
            text,
            re.IGNORECASE,
        )
        if district_committee:
            value = district_committee.group(1).lower()
            return self._clean_issuer(value[:1].upper() + value[1:])
        correspondence_issuer = self._extract_correspondence_issuer(text)
        if correspondence_issuer:
            return correspondence_issuer

        organization = self._extract_russian_organization_from_header(lines[:12])
        if organization:
            return organization

        for line in lines[:12]:
            candidate = self._prefer_russian_text(line)
            lowered = candidate.lower()
            if any(word in lowered for word in ORG_KEYWORDS):
                return self._clean_issuer(candidate)

        match = re.search(r"(?:выдан[оы]?|кем выдан[оы]?)\s*[:\-]\s*(.+)", text, re.IGNORECASE)
        return self._clean_issuer(self._prefer_russian_text(match.group(1))) if match else ""

    @staticmethod
    def _extract_ministry_technical_header(text: str) -> dict[str, str]:
        first_page = text.split("--- page 2 ---", 1)[0]
        if not re.search(r"учреждение\s+«?Узденский\s+районный\s+центр\s+гигиены\s+и\s+эпидемиологии", first_page, re.IGNORECASE) and "республиканский центр государственной экологической экспертизы" not in first_page.lower():
            return {}
        lines = [line.strip() for line in first_page.splitlines() if line.strip()]
        for line in lines[:18]:
            normalized = line.replace("—", "-").replace("–", "-")
            date_match = re.search(r"(?<!\d)([0-3]?\d)[./-]([01]?\d)[./-]((?:19|20)\d{2})(?!\d)", normalized)
            number_match = re.search(r"(?:№\s*)?(\d{1,2}[-/]\d{1,3}[-/]\d{1,4})\b", normalized)
            if number_match or date_match:
                result = {
                    "document_number": number_match.group(1) if number_match else "",
                    "issue_date": f"{int(date_match.group(3)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(1)):02d}" if date_match else "",
                }
                if result["document_number"] and result["issue_date"]:
                    return result
        return {}

    @staticmethod
    def _is_ministry_technical_requirements(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", text.lower())
        return "технические требования" in normalized and (
            "республиканский центр государственной экологической экспертизы" in normalized
            or "узденский районный центр гигиены и эпидемиологии" in normalized
        )

    @staticmethod
    def _is_gai_technical_requirements(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", text.lower())
        return "технические требования" in normalized and "инспекции" in normalized and "внутренних дел" in normalized

    @staticmethod
    def _is_beltelecom_technical_conditions(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", text.lower())
        return (
            "технические услови" in normalized
            and "инженерно-техническое обеспечение" in normalized
            and "белтелеком" in normalized
        )

    @staticmethod
    def _extract_beltelecom_number(text: str) -> str:
        first_page = text.split("--- page 2 ---", 1)[0]
        for line in first_page.splitlines():
            if "№" not in line:
                continue
            # OCR frequently attaches the form underline to the final digit
            # (for example ``19-1-5/335_``), so a word boundary after the
            # number is too strict here.
            match = re.search(r"(?<!\d)(\d{1,2}-\d-\d/\d{2,})(?!\d)", line)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _is_minskenergo_electrical_conditions(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", text.lower())
        return (
            "технические условия" in normalized
            and "электроустановок потребителя" in normalized
            and ("минскэнерго" in normalized or "столбцовские электрические сети" in normalized)
        )

    def _extract_correspondence_issuer(self, text: str) -> str:
        organization_start = (
            r"(?:РУП|Республиканское\s+унитарное\s+предприятие|"
            r"КУП|Коммунальное\s+унитарное\s+предприятие|"
            r"УП|Унитарное\s+предприятие|ОАО|ЗАО|ООО|ОДО|"
            r"Государственное\s+предприятие|Частное\s+предприятие)"
        )
        action = (
            r"(?:просит|просим|сообщает|сообщаем|информирует|информируем|"
            r"направляет|направляем|уведомляет|уведомляем|заявляет|заявляем|"
            r"обращается|представляет|подтверждает)"
        )
        match = re.search(
            rf"(?im)^\s*({organization_start}\b[^\n]{{0,180}}?)\s+{action}\b",
            text,
        )
        return self._clean_issuer(match.group(1)) if match else ""

    def _extract_russian_organization_from_header(self, lines: list[str]) -> str:
        candidates = []
        for start, line in enumerate(lines):
            segment = self._prefer_russian_text(line)
            if not self._looks_like_organization_start(segment):
                continue

            if any(marker in segment.lower() for marker in ("р/с", "банк", "код ", "унп", "окпо")):
                continue
            parts = [segment]
            for following in lines[start + 1 : start + 5]:
                following_segment = self._prefer_russian_text(following)
                if any(marker in following_segment.lower() for marker in ("р/с", "банк", "код ", "унп", "окпо", "юридический адрес", "почтовый адрес", "тел.")):
                    break
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
        value = re.sub(
            r"Республиканское\s+дочернее\s+унитарное\s+предприятие\s+по\s+обеспечению\s+нефтепродуктами",
            "РУП",
            value,
            flags=re.IGNORECASE,
        )
        return normalize_organization_forms(value.strip(" .,:;-"))

    def _extract_issue_date(self, text: str) -> str:
        first_page = text.split("--- page 2 ---", 1)[0]
        lines = [line.strip() for line in first_page.splitlines() if line.strip()]
        if re.search(r"районный\s+исполнительный\s+комитет", first_page, re.IGNORECASE) and re.search(r"\bрешение\b", first_page, re.IGNORECASE):
            month_pattern = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
            # Header text appears directly below the bilingual title (OCR may
            # merge РАШЭННЕ and РЕШЕНИЕ on one line). Never use dates cited in
            # the body as the issuance date.
            for line in lines[:8]:
                if re.search(rf"(?<!\d)([0-3]?\d)\s+({month_pattern})\s+((?:19|20)\d{{2}})", line, re.IGNORECASE):
                    date_match = re.search(rf"(?<!\d)([0-3]?\d)\s+({month_pattern})\s+((?:19|20)\d{{2}})", line, re.IGNORECASE)
                    month = MONTHS_RU[date_match.group(2).lower()]
                    return f"{int(date_match.group(3)):04d}-{month:02d}-{int(date_match.group(1)):02d}"
            for line in lines[:15]:
                dated_header = re.search(
                    rf"(?<!\d)([0-3]?\d)\s+({month_pattern})\s+((?:19|20)\d{{2}})\s*г\w*",
                    line,
                    re.IGNORECASE,
                )
                if dated_header:
                    month = MONTHS_RU[dated_header.group(2).lower()]
                    return f"{int(dated_header.group(3)):04d}-{month:02d}-{int(dated_header.group(1)):02d}"
        for line in lines[:45]:
            # "На № ... от ..." is a blank incoming-reference field; an
            # outgoing letterhead line "№ ... от DD.MM.YYYY" is a real date.
            if re.match(r"^на\s*№", line, re.IGNORECASE):
                continue
            if "№" in line or re.search(r"\bN(?:o)?\b", line):
                match = DATE_RE.search(line)
                if match:
                    return self._format_date(match.group(1))
                textual = re.search(r"[«\"]?([0-3]?\d)[»\"]?\s+([а-яё]+)\s+((?:19|20)\d{2})\s*г", line, re.I)
                if textual and textual.group(2).lower() in MONTHS_RU:
                    return f"{int(textual.group(3)):04d}-{MONTHS_RU[textual.group(2).lower()]:02d}-{int(textual.group(1)):02d}"

        for pattern in (
            r"(?:^|\n)\s*(?:от|выдан[ао]?)\s*[:№\-]*\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})",
            r"(?:дата выдачи)\s*[:\-]?\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})",
        ):
            match = re.search(pattern, first_page, re.IGNORECASE)
            if match:
                return self._format_date(match.group(1))
        # Some scans put the issuance date in a standalone footer.
        # Some scanned utilities place the actual issuance date in a standalone
        # footer. Prefer the final standalone date over dates cited in regulations.
        standalone_dates = list(re.finditer(
            r"(?m)^\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})\s*:?\s*$",
            first_page,
        ))
        if standalone_dates:
            return self._format_date(standalone_dates[-1].group(1))
        return ""

    @staticmethod
    def _extract_committee_decision_number(text: str) -> str:
        first_page = text.split("--- page 2 ---", 1)[0]
        if not re.search(r"районный\s+исполнительный\s+комитет", first_page, re.IGNORECASE) or not re.search(r"\bрешение\b", first_page, re.IGNORECASE):
            return ""
        lines = [line.strip() for line in first_page.splitlines() if line.strip()]
        header_date = re.compile(r"(?<!\d)[0-3]?\d\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(?:19|20)\d{2}", re.IGNORECASE)
        for index, line in enumerate(lines[:8]):
            if header_date.search(line):
                match = re.search(r"\b(?:г\w*\s*)?(\d{1,6})\b", line[header_date.search(line).end():], re.IGNORECASE)
                if match:
                    return match.group(1)
                # In some OCR layouts the number lands on the next line.
                if index + 1 < len(lines):
                    next_number = re.fullmatch(r"\s*(?:№\s*)?(\d{1,6})\s*[_—-]*\s*", lines[index + 1])
                    if next_number:
                        return next_number.group(1)
        return ""

    @staticmethod
    def _extract_header_document_number(text: str) -> str:
        """Read a slash-delimited number from the outgoing header, when OCR has it."""
        first_page = text.split("--- page 2 ---", 1)[0]
        lines = [line.strip() for line in first_page.splitlines() if line.strip()]
        number_pattern = re.compile(
            r"(?:исх(?:одящий)?\.?\s*(?:№|N\s*)|(?<![\w])№\s*|(?<![\w])N(?:o|º|°)?\s*)"
            r"([A-ZА-ЯЁ0-9]+(?:\s*[-./]\s*[A-ZА-ЯЁ0-9]+){1,5})",
            re.IGNORECASE,
        )
        candidates = []
        for line in lines[:30]:
            if re.match(r"^на\s*(?:№|N\b)", line, re.IGNORECASE):
                continue
            match = number_pattern.search(line)
            if not match:
                continue
            number = re.sub(r"\s*([./-])\s*", r"\1", match.group(1)).strip(" .,:;-_")
            if not re.search(r"\d", number):
                continue
            priority = 2 if re.search(r"\bисх(?:одящий)?\b", line, re.IGNORECASE) else 0
            if re.search(r"\bот\s*[«\"]?\d", line, re.IGNORECASE) or DATE_RE.search(line):
                priority += 3
            candidates.append((priority, lines.index(line), number))
        if not candidates:
            return ""
        return max(candidates, key=lambda item: (item[0], -item[1]))[2]

    def _extract_valid_until(self, text: str, issue_date: str = "") -> str:
        explicit_expiry = self._extract_explicit_valid_until(text)
        if explicit_expiry:
            return explicit_expiry
        patterns = (
            r"(?:срок действия|действительн[а-я ]*до|действует до)\s*[:\-]?\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})",
            r"(?:до)\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return self._format_date(match.group(1))
        return self._calculate_valid_until_from_duration(text, issue_date)

    @staticmethod
    def _extract_explicit_valid_until(text: str) -> str:
        """Find an explicitly stated expiry, including dates split by OCR noise."""
        date_pattern = r"([0-3]?\d[./-][01]?\d[./-](?:19|20)\d{2})"
        patterns = (
            # Some scans place stamps or OCR noise between "действительны до"
            # and the date, sometimes moving the date to the next line.
            rf"\bдействительн[а-яё]*\s+до\b[\s\S]{{0,180}}?{date_pattern}",
            rf"\bсрок\s+действия\s+до\b[\s\S]{{0,100}}?{date_pattern}",
            rf"\bдействует\s+до\b[\s\S]{{0,100}}?{date_pattern}",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return RegexFieldExtractor._format_date(match.group(1))
        return ""

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
        # OCR commonly puts a short validity statement at the very bottom of
        # a letter and splits it across visual lines. Match against a flattened
        # copy so phrases such as "согласование действительно\nдва года" stay
        # intact, while sentence boundaries still limit unrelated matches.
        duration_text = re.sub(r"\s+", " ", (text or "").replace("\xa0", " "))
        patterns = (
            r"срок\s+действия[^.!?]{0,160}?\s+(\d+|[а-яё]+)\s+(?:календарн\w*\s+)?(?:год(?:а|у|ов)?|лет)",
            r"(?:срок[^.!?]{0,160}|действ\w*[^.!?]{0,160})в\s+течение\s+(\d+|[а-яё]+)\s*[-‑–—]?\s*(?:х|ых|ти)?\s+лет?",
            r"(?:срок[^.!?]{0,160}|действ\w*[^.!?]{0,160})в\s+течение\s+(\d+)\s*[-‑–—]?\s*(?:х|ых|ти)?\s+лет?",
            r"в\s+течение\s+(\d+)\s*[-‑–—]?\s*(?:х|ых|ти)?\s+лет?[^.!?]{0,220}(?:с\s+даты|со\s+дня)\s+(?:их\s+)?выдачи",
            r"(?:срок[^.!?]{0,160}|действ\w*[^.!?]{0,160})в\s+течение\s+(\d+|[а-яё]+)\s+лет?",
            r"в\s+течение\s+(\d+|[а-яё]+)\s+лет?[^.!?]{0,220}(?:с\s+даты|со\s+дня)\s+(?:их\s+)?выдачи",
            r"(?:согласован\w*|разрешени\w*|письм\w*|документ\w*|услови\w*)\s+(?:является\s+)?действительн\w*[^.!?]{0,120}?\s+(\d+|[а-яё]+)\s+(?:календарн\w*\s+)?(?:год(?:а|у|ов)?|лет)",
            r"\bдействительн\w*[^.!?]{0,100}?\s+(\d+|[а-яё]+)\s+(?:календарн\w*\s+)?(?:год(?:а|у|ов)?|лет)",
            r"(?:срок[^.!?]{0,160}|действ\w*[^.!?]{0,160})в\s+течение\s+(\d+|[а-яё]+)\s+лет?",
            r"в\s+течение\s+(\d+|[а-яё]+)\s+лет?",
            r"(?:с\s+даты|со\s+дня)\s+(?:их\s+)?выдачи[^.!?]{0,220}в\s+течение\s+(\d+|[а-яё]+)\s+лет?",
        )
        for pattern in patterns:
            match = re.search(pattern, duration_text, re.IGNORECASE)
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
                return " ".join(lines[index : index + 8])[:500]

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
            if re.fullmatch(r"(?:19|20)\d{2}-[01]?\d-[0-3]?\d", value.strip()):
                return date.fromisoformat(value.strip())
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
