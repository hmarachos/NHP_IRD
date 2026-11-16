from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from flask import current_app
from app.repositories import (
    ChecklistRepository,
    DocumentRepository,
)


logger = logging.getLogger(__name__)


def run_daily_notifications() -> None:
    """Send scheduled document expiry reminders."""
    try:
        send_expiry_reminders()
    except Exception:
        logger.exception("Не удалось обработать напоминания об истечении срока.")


def send_expiry_reminders() -> None:
    today = datetime.now(ZoneInfo(current_app.config["MAIL_TIMEZONE"])).date()
    cutoff = today + timedelta(days=current_app.config["EXPIRY_WARNING_DAYS"])
    documents = DocumentRepository().list_expiring_within(today.isoformat(), cutoff.isoformat())
    checklist_items = ChecklistRepository().list_expiring_within(today.isoformat(), cutoff.isoformat())
    expiring_items = sorted(
        [*documents, *checklist_items],
        key=lambda row: (row["valid_until"], row["notification_project_name"], row["working_title"] or ""),
    )
    if not expiring_items:
        return

    message = EmailMessage()
    message["Subject"] = f"Архив ИРД: сроки действия документов в ближайшие {current_app.config['EXPIRY_WARNING_DAYS']} дней"
    message.set_content(_expiry_message(expiring_items))
    _send_email(message)
    DocumentRepository().mark_expiry_notifications_sent(documents)
    ChecklistRepository().mark_expiry_notifications_sent(checklist_items)
    logger.info("Отправлено напоминание по документам: %s.", len(expiring_items))


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
    config = current_app.config
    username = config["MAIL_USERNAME"].strip()
    password = "".join(config["MAIL_APP_PASSWORD"].split())
    sender = config["MAIL_FROM"].strip() or username
    recipient = config["MAIL_RECIPIENT"].strip()
    if not username or not password or not sender or not recipient:
        raise RuntimeError(
            "Почта не настроена: укажите MAIL_USERNAME и MAIL_APP_PASSWORD в .env."
        )

    message["From"] = sender
    message["To"] = recipient
    context = ssl.create_default_context()
    port = config["MAIL_PORT"]
    if port == 465:
        with smtplib.SMTP_SSL(config["MAIL_SERVER"], port, timeout=30, context=context) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(config["MAIL_SERVER"], port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(message)
