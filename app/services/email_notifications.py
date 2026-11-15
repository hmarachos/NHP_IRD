from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import current_app
from werkzeug.utils import secure_filename

from app.repositories import (
    AppSettingsRepository,
    ChecklistRepository,
    DocumentRepository,
    HandoverActRepository,
    ObjectRepository,
)
from app.services.document_generator import generate_project_document


logger = logging.getLogger(__name__)


def run_daily_notifications() -> None:
    """Send expiry reminders and accumulated project handover acts."""
    try:
        send_expiry_reminders()
    except Exception:
        logger.exception("Не удалось обработать напоминания об истечении срока.")
    try:
        send_pending_handover_acts()
    except Exception:
        logger.exception("Не удалось обработать ежедневные акты приёма-передачи.")


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


def send_pending_handover_acts() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = _utc_stamp(now)
    app_started_at = AppSettingsRepository().get("mail_notifications_start_at") or end
    acts = HandoverActRepository()
    documents_repo = DocumentRepository()
    checklist_repo = ChecklistRepository()

    for project in ObjectRepository().list_all():
        project_id = project["id"]
        pending = acts.pending(project_id)
        last_sent = acts.last_sent(project_id)
        if pending:
            period_start = pending["period_start"]
        elif last_sent:
            period_start = last_sent["period_end"]
        else:
            period_start = max(app_started_at, project["created_at"])

        documents = documents_repo.list_received_in_period(project_id, period_start, end)
        if not documents:
            # Empty periods neither create an act nor trigger an email.
            continue

        project_dir = Path(current_app.config["ACTS_DIR"]) / str(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        safe_project_name = secure_filename(project["name"]) or f"project-{project_id}"
        start_date = _local_date(period_start)
        end_date = _local_date(end)
        filename = f"Akt_IRD_{safe_project_name}_{start_date}_{end_date}.docx"
        final_path = project_dir / filename
        temp_path = project_dir / f".{filename}.tmp"

        content = generate_project_document(
            "handover",
            project,
            documents,
            checklist_repo.list_project_items(project_id),
            {
                "date": end_date,
                "number": f"{project_id}-{end_date}",
            },
        )
        temp_path.write_bytes(content)
        temp_path.replace(final_path)
        act = acts.save_pending(project_id, period_start, end, filename)

        message = EmailMessage()
        message["Subject"] = f"Архив ИРД: акт за период {start_date} — {end_date} · {project['name']}"
        message.set_content(
            "Во вложении акт приёма-передачи документов, поступивших в проект за период "
            f"с {start_date} по {end_date}.\n\n"
            f"Проект: {project['name']}\n"
            f"Договор: {project['contract_number']}\n"
            f"Количество документов: {len(documents)}."
        )
        message.add_attachment(
            content,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=filename,
        )
        try:
            _send_email(message)
        except Exception as exc:
            acts.mark_error(act["id"], str(exc))
            logger.exception("Не удалось отправить акт проекта %s.", project_id)
            continue

        acts.mark_sent(act["id"], _utc_stamp(datetime.now(timezone.utc).replace(microsecond=0)))
        logger.info("Отправлен акт проекта %s; документов: %s.", project_id, len(documents))


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


def _utc_stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local_date(value: str) -> str:
    timestamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return timestamp.astimezone(ZoneInfo(current_app.config["MAIL_TIMEZONE"])).strftime("%Y-%m-%d")
