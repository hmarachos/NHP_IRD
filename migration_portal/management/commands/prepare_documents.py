"""Backfill or repair search representations without OCR or external AI."""
from django.core.management.base import BaseCommand
from app.services.document_preparation import queue_document_preparation, run_pending_preparations
from migration_portal.models import Correspondence, Document, PreparedDocument


class Command(BaseCommand):
    help = "Prepare stored OCR for RAG, including existing uploads. Does not re-run OCR."

    def add_arguments(self, parser):
        parser.add_argument("--rebuild", action="store_true", help="Rebuild all normalized texts and chunks.")
        parser.add_argument("--retry-errors", action="store_true")

    def handle(self, *args, **options):
        for model in (Document, Correspondence):
            for source in model.objects.exclude(raw_text__isnull=True).exclude(raw_text="").exclude(status__in=["queued", "processing"]).iterator():
                queue_document_preparation(source)
        if options["rebuild"]:
            PreparedDocument.objects.update(status="pending", pipeline_version=0)
        if options["retry_errors"]:
            PreparedDocument.objects.filter(status="error").update(status="pending", error="")
        completed = 0
        while True:
            count = run_pending_preparations(limit=100)
            completed += count
            if not count:
                break
        self.stdout.write(f"Prepared: {completed}; ready: {PreparedDocument.objects.filter(status='ready').count()}; errors: {PreparedDocument.objects.filter(status='error').count()}")
        if PreparedDocument.objects.filter(status="error").exists():
            from django.core.management.base import CommandError
            raise CommandError("Some documents could not be prepared; use --retry-errors after fixing the cause.")
