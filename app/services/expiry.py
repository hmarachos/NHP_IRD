from datetime import date, datetime


DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y")


def parse_document_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def iso_date(value: str | None) -> str:
    parsed = parse_document_date(value)
    return parsed.isoformat() if parsed else ""


def expiry_state(document, warning_days: int = 30, today: date | None = None) -> dict:
    if document["no_expiration"]:
        return {"key": "unlimited", "label": "Без срока", "days_left": None}
    expiration = parse_document_date(document["valid_until"])
    if expiration is None:
        return {"key": "unknown", "label": "Срок не указан", "days_left": None}
    days_left = (expiration - (today or date.today())).days
    if days_left < 0:
        return {"key": "expired", "label": f"Просрочен на {abs(days_left)} дн.", "days_left": days_left}
    if days_left <= warning_days:
        return {"key": "expiring", "label": f"Истекает через {days_left} дн.", "days_left": days_left}
    return {"key": "valid", "label": "Действует", "days_left": days_left}


def attach_expiry(document, warning_days: int = 30) -> dict:
    result = dict(document)
    result["expiry"] = expiry_state(document, warning_days)
    return result
