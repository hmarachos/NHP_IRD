from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q

from migration_portal.models import (
    ChecklistExpiryNotification,
    Document,
    ExpiryNotification,
    ProjectChecklistItem,
)

logger = logging.getLogger(__name__)


def run_daily_notifications() -> None:
    try:
        send_expiry_reminders()
    except Exception:
        logger.exception("Не удалось обработать напоминания об истечении срока.")


def send_expiry_reminders() -> None:
    today = datetime.now(ZoneInfo(settings.MAIL_TIMEZONE)).date()
    cutoff = today + timedelta(days=settings.EXPIRY_WARNING_DAYS)
    documents, doc_ids = [], []
    for row in Document.objects.filter(project__archived_at__isnull=True, archived_at__isnull=True, no_expiration=False).select_related("project"):
        expiration = _date(row.valid_until)
        if not expiration or not today < expiration <= cutoff:
            continue
        notification_key = f"expiry_30_days:{row.valid_until}"
        if ExpiryNotification.objects.filter(document=row, notification_type=notification_key).exists():
            continue
        documents.append({
            "id": row.id, "document_id": row.id, "valid_until": row.valid_until,
            "working_title": row.working_title, "official_title": row.official_title,
            "original_filename": row.original_filename, "issuer": row.issuer,
            "notification_project_name": row.project.name,
            "notification_contract_number": row.project.contract_number,
            "notification_type": notification_key,
        })
        doc_ids.append(row.id)

    checklist_rows, checklist_keys = [], []
    for row in ProjectChecklistItem.objects.filter(
        project__archived_at__isnull=True, validity_years__gt=0,
    ).select_related("project"):
        expiration = _date(row.valid_until)
        if not row.issue_date or not expiration or not today < expiration <= cutoff:
            continue
        if ChecklistExpiryNotification.objects.filter(checklist_item=row, valid_until=row.valid_until).exists():
            continue
        checklist_rows.append({
            "id": row.id, "checklist_item_id": row.id, "valid_until": row.valid_until,
            "working_title": row.title, "official_title": row.title, "original_filename": row.title,
            "issuer": "", "notification_project_name": row.project.name,
            "notification_contract_number": row.project.contract_number,
        })
        checklist_keys.append((row.id, row.valid_until))

    all_rows = sorted([*documents, *checklist_rows], key=lambda row: (
        row["valid_until"], row["notification_project_name"], row["working_title"] or "",
    ))
    if not all_rows:
        return
    message = EmailMessage()
    message["Subject"] = f"Архив ИРД: сроки действия документов в ближайшие {settings.EXPIRY_WARNING_DAYS} дней"
    message.set_content(_expiry_message(all_rows))
    _send_email(message)
    ExpiryNotification.objects.bulk_create([
        ExpiryNotification(document_id=row["document_id"], notification_type=row["notification_type"])
        for row in documents
    ], ignore_conflicts=True)
    ChecklistExpiryNotification.objects.bulk_create([
        ChecklistExpiryNotification(checklist_item_id=item_id, valid_until=valid_until)
        for item_id, valid_until in checklist_keys
    ], ignore_conflicts=True)
    logger.info("Отправлено напоминание по документам: %s.", len(all_rows))


def _date(value):
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _expiry_message(documents: list[Any]) -> str:
    lines = ["Срок действия следующих документов заканчивается в течение месяца:", ""]
    current_project = None
    for row in documents:
        project_label = f"{row['notification_project_name']} (договор {row['notification_contract_number']})"
        if project_label != current_project:
            current_project = project_label
            lines.extend((current_project,))
        title = row["working_title"] or row["official_title"] or row["original_filename"]
        issuer = f" — {row['issuer']}" if row["issuer"] else ""
        lines.append(f"• {title}{issuer}; действует до {row['valid_until']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _send_email(message: EmailMessage) -> None:
    username = settings.MAIL_USERNAME.strip()
    password = "".join(settings.MAIL_APP_PASSWORD.split())
    sender = settings.MAIL_FROM.strip() or username
    recipient = settings.MAIL_RECIPIENT.strip()
    if not username or not password or not sender or not recipient:
        raise RuntimeError("Почта не настроена: укажите MAIL_USERNAME и MAIL_APP_PASSWORD.")
    message["From"] = sender
    message["To"] = recipient
    context = ssl.create_default_context()
    if settings.MAIL_PORT == 465:
        with smtplib.SMTP_SSL(settings.MAIL_SERVER, settings.MAIL_PORT, timeout=30, context=context) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.MAIL_SERVER, settings.MAIL_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(message)
