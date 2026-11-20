import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler
from django.conf import settings
from django.core.management.base import BaseCommand
from app.services.document_jobs import run_pending_document_jobs
from app.services.document_preparation import run_pending_preparations


class Command(BaseCommand):
    help = "Process background document jobs against Django's PostgreSQL database."

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        scheduler = BlockingScheduler(timezone=settings.MAIL_TIMEZONE)

        def process_uploads():
            count = run_pending_document_jobs()
            prepared_count = run_pending_preparations()
            if prepared_count:
                logging.getLogger(__name__).info("Подготовлено документов для RAG: %d", prepared_count)
            if count:
                logging.getLogger(__name__).info("Обработано Django-загрузок: %d", count)

        scheduler.add_job(process_uploads, "interval", seconds=3, id="django-document-upload-processing", max_instances=1, coalesce=True)

        # Email delivery is controlled explicitly by deployment configuration.
        if os.getenv("DJANGO_ENABLE_EMAIL_NOTIFICATIONS", "false").lower() in {"1", "true", "yes", "on"}:
            from app.services.email_notifications import run_daily_notifications, send_pending_document_notifications

            scheduler.add_job(
                send_pending_document_notifications, "interval", seconds=15,
                id="django-saved-document-expiry-notifications", max_instances=1, coalesce=True,
            )

            def notifications():
                run_daily_notifications()

            scheduler.add_job(
                notifications,
                "cron",
                hour=settings.MAIL_SEND_HOUR,
                minute=0,
                id="django-daily-document-notifications",
                max_instances=1,
                coalesce=True,
            )
            self.stdout.write("Django email notifications enabled.")
        else:
            self.stdout.write("Django email notifications remain disabled by configuration.")
        scheduler.start()
