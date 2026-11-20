from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from migration_portal.models import (
    ChecklistExpiryNotification,
    Document,
    ExpiryNotification,
    PendingExpiryNotification,
    ProjectChecklistItem,
)

logger = logging.getLogger(__name__)


def queue_saved_document_notification(document) -> None:
    """Notify only after the user has confirmed the document card."""
    today = datetime.now(ZoneInfo(settings.MAIL_TIMEZONE)).date()
    expiration = _date(document.valid_until)
    if (not document.project_id or document.archived_at or document.no_expiration
            or not expiration or expiration >= today + timedelta(days=30)):
        return
    key = f"expiry_30_days:{document.valid_until}"
    if not ExpiryNotification.objects.filter(document=document, notification_type=key).exists():
        PendingExpiryNotification.objects.get_or_create(
            document=document, valid_until=document.valid_until,
            defaults={"next_attempt_at": timezone.now()},
        )


def send_pending_document_notifications() -> None:
    for job_id in PendingExpiryNotification.objects.filter(
        next_attempt_at__lte=timezone.now(),
    ).values_list("pk", flat=True)[:20]:
        try:
            with transaction.atomic():
                job = PendingExpiryNotification.objects.select_for_update(skip_locked=True).filter(pk=job_id).first()
                if job is None:
                    continue
                document = Document.objects.select_related("project").get(pk=job.document_id)
                key = f"expiry_30_days:{job.valid_until}"
                if (not document.project_id or document.project.archived_at or document.archived_at
                        or document.no_expiration or document.valid_until != job.valid_until
                        or ExpiryNotification.objects.filter(document=document, notification_type=key).exists()):
                    job.delete()
                    continue
                today = datetime.now(ZoneInfo(settings.MAIL_TIMEZONE)).date()
                expiration = _date(job.valid_until)
                expired = expiration <= today
                title = document.working_title or document.official_title or document.original_filename
                status = "Срок действия истёк" if expired else "Срок действия заканчивается менее чем через месяц"
                message = EmailMessage()
                message["Subject"] = f"Архив ИРД: {status.lower()}"
                message.set_content(
                    f"{status}.\n\nДоговор: {document.project.contract_number}\n"
                    f"Объект: {document.project.name}\nДокумент: {title}\n"
                    f"Номер документа: {document.document_number or 'не указан'}\n"
                    f"Организация: {document.issuer or 'не указана'}\n"
                    f"Срок действия: {expiration.strftime('%d.%m.%Y')}\n"
                )
                message.add_alternative(_expiry_html([{
                    "working_title": title,
                    "document_number": document.document_number,
                    "issuer": document.issuer,
                    "valid_until": job.valid_until,
                    "notification_project_name": document.project.name,
                    "notification_contract_number": document.project.contract_number,
                }], heading=status), subtype="html")
                _send_email(message)
                ExpiryNotification.objects.get_or_create(document=document, notification_type=key)
                job.delete()
        except Exception:
            logger.exception("Не удалось отправить уведомление о сроке документа, очередь %s.", job_id)
            PendingExpiryNotification.objects.filter(pk=job_id).update(
                next_attempt_at=timezone.now() + timedelta(minutes=5),
            )


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
        if PendingExpiryNotification.objects.filter(document=row, valid_until=row.valid_until).exists():
            continue
        notification_key = f"expiry_30_days:{row.valid_until}"
        if ExpiryNotification.objects.filter(document=row, notification_type=notification_key).exists():
            continue
        documents.append({
            "id": row.id, "document_id": row.id, "valid_until": row.valid_until,
            "working_title": row.working_title, "official_title": row.official_title,
            "original_filename": row.original_filename, "issuer": row.issuer,
            "document_number": row.document_number,
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
    message.add_alternative(_expiry_html(
        all_rows, heading="Документы с истекающим сроком действия",
    ), subtype="html")
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


def _expiry_html(documents: list[Any], *, heading: str) -> str:
    """Email-safe table layout; escape all document and project data."""
    today = datetime.now(ZoneInfo(settings.MAIL_TIMEZONE)).date()
    cards = []
    for row in documents:
        expiration = _date(row.get("valid_until"))
        days_left = (expiration - today).days if expiration else None
        if days_left is not None and days_left < 0:
            status, color, background = "Срок истёк", "#b42318", "#fef3f2"
        elif days_left == 0:
            status, color, background = "Срок истекает сегодня", "#b42318", "#fef3f2"
        else:
            status, color, background = "Срок скоро истекает", "#b54708", "#fffaeb"
        title = row.get("working_title") or row.get("official_title") or row.get("original_filename") or "Документ"
        details = [
            ("Договор", row.get("notification_contract_number") or "Не указан"),
            ("Объект", row.get("notification_project_name") or "Не указан"),
        ]
        if row.get("document_number"):
            details.append(("Номер документа", row["document_number"]))
        if row.get("issuer"):
            details.append(("Организация", row["issuer"]))
        details.append(("Действует до", expiration.strftime("%d.%m.%Y") if expiration else "Не указано"))
        detail_rows = "".join(
            '<tr><td style="padding:7px 12px 7px 0;width:135px;vertical-align:top;color:#667085;">'
            + escape(label) + '</td><td style="padding:7px 0;vertical-align:top;overflow-wrap:anywhere;">'
            + escape(str(value)) + '</td></tr>'
            for label, value in details
        )
        cards.append(
            '<tr><td style="padding:0 0 18px;">'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="border:1px solid #e4e7ec;border-radius:12px;border-collapse:separate;">'
            '<tr><td style="padding:20px;">'
            f'<span style="display:inline-block;padding:6px 10px;border-radius:6px;font-size:12px;font-weight:bold;color:{color};background:{background};">'
            + escape(status) + '</span>'
            '<h2 style="margin:16px 0 12px;font-size:18px;line-height:1.4;color:#101828;overflow-wrap:anywhere;">'
            + escape(str(title)) + '</h2>'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="font-size:14px;line-height:1.5;">'
            + detail_rows + '</table></td></tr></table></td></tr>'
        )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f2f4f7;font-family:Arial,Helvetica,sans-serif;color:#344054;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f2f4f7;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;">'
        '<tr><td style="padding:24px;background:#101828;border-radius:12px 12px 0 0;">'
        '<p style="margin:0 0 10px;color:#fdb022;font-size:12px;letter-spacing:2px;font-weight:bold;">АРХИВ ИРД</p>'
        '<h1 style="margin:0;font-size:24px;line-height:1.35;color:#ffffff;">'
        + escape(heading) + '</h1></td></tr>'
        '<tr><td style="padding:24px 20px 6px;background:#ffffff;border-radius:0 0 12px 12px;">'
        '<p style="margin:0 0 20px;font-size:14px;line-height:1.6;">'
        'Проверьте сроки и при необходимости запросите актуальные документы.</p>'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
        + "".join(cards) + '</table></td></tr>'
        '<tr><td style="padding:18px 12px;text-align:center;font-size:12px;line-height:1.5;color:#667085;">'
        'Автоматическое уведомление системы «Архив ИРД».</td></tr>'
        '</table></td></tr></table></body></html>'
    )


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
