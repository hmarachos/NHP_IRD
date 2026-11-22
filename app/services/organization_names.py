"""Normalize verbose Russian legal-form names in extracted organization fields."""

from __future__ import annotations

import re


_LEGAL_FORMS = (
    (r"республиканск(?:ое|ого)\s+дочерн(?:ее|его)\s+унитарн(?:ое|ого)\s+предприяти(?:е|я)\s+по\s+обеспечению\s+нефтепродуктами", "РУП"),
    (r"республиканск(?:ое|ого)\s+(?:дочерн(?:ее|его)\s+)?унитарн(?:ое|ого)\s+предприяти(?:е|я)", "РУП"),
    (r"коммунальн(?:ое|ого)\s+унитарн(?:ое|ого)\s+предприяти(?:е|я)", "КУП"),
    (r"частн(?:ое|ого)\s+унитарн(?:ое|ого)\s+предприяти(?:е|я)", "ЧУП"),
    (r"унитарн(?:ое|ого)\s+предприяти(?:е|я)", "УП"),
    (r"обществ(?:о|а|у|ом)\s+с\s+ограниченной\s+ответственностью", "ООО"),
    (r"обществ(?:о|а|у|ом)\s+с\s+дополнительной\s+ответственностью", "ОДО"),
    (r"открыт(?:ое|ого)\s+акционерн(?:ое|ого)\s+общество", "ОАО"),
    (r"закрыт(?:ое|ого)\s+акционерн(?:ое|ого)\s+общество", "ЗАО"),
)


def normalize_organization_forms(value: str) -> str:
    """Abbreviate common Russian organization forms without changing names."""
    normalized = re.sub(r"\s+", " ", value or "").strip()
    for pattern, abbreviation in _LEGAL_FORMS:
        normalized = re.sub(rf"\b{pattern}\b", abbreviation, normalized, flags=re.IGNORECASE)
    return normalized


def normalize_user_text(value: str | None) -> str | None:
    """Apply the shared organization-name and Russian-quote input conventions."""
    if value is None:
        return None
    normalized = re.sub(
        r"(?:РУП|республиканск\w*\s+(?:дочерн\w*\s+)?унитарн\w*\s+предприяти\w*)"
        r"\s+по\s+обеспечению\s+нефтепродуктами",
        "РУП",
        value,
        flags=re.IGNORECASE,
    )

    result: list[str] = []
    quote_depth = 0
    for character in normalized:
        if character == "«":
            quote_depth += 1
            result.append(character)
        elif character == "»":
            quote_depth = max(0, quote_depth - 1)
            result.append(character)
        elif character == '"':
            if quote_depth:
                result.append("»")
                quote_depth -= 1
            else:
                result.append("«")
                quote_depth += 1
        else:
            result.append(character)
    return "".join(result)
