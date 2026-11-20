import tempfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from app.models import ParsedDocument
from app.services.document_jobs import run_pending_document_jobs
from migration_portal.models import Document


class DocumentLeaseTests(TestCase):
    def document(self, **kwargs):
        return Document.objects.create(contract_number='', object_name='', original_filename='test.pdf', stored_filename='test.pdf', **kwargs)

    def test_old_upload_with_active_lease_is_not_claimed(self):
        d=self.document(status='processing', created_at='2000-01-01', processing_token='active', processing_lease_until=timezone.now()+timedelta(minutes=10))
        self.assertEqual(run_pending_document_jobs(1),0)
        d.refresh_from_db();self.assertEqual(d.processing_token,'active')

    def test_expired_lease_is_recovered_and_cleared(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            Path(directory,'test.pdf').touch()
            d=self.document(status='processing', processing_token='dead', processing_lease_until=timezone.now()-timedelta(seconds=1))
            with patch('app.services.document_jobs.DocumentProcessingPipeline') as pipeline:
                pipeline.return_value.process.return_value=ParsedDocument(document_number='240')
                self.assertEqual(run_pending_document_jobs(1),1)
            d.refresh_from_db();self.assertEqual((d.status,d.document_number,d.processing_token,d.processing_lease_until),('processed','240','',None))

    def test_previous_owner_cannot_save_after_reclaim(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            Path(directory,'test.pdf').touch();d=self.document(status='queued')
            def process(_):
                Document.objects.filter(pk=d.pk).update(processing_token='new-owner',document_number='new-result')
                return ParsedDocument(document_number='stale-result')
            with patch('app.services.document_jobs.DocumentProcessingPipeline') as pipeline:
                pipeline.return_value.process.side_effect=process
                run_pending_document_jobs(1)
            d.refresh_from_db();self.assertEqual((d.processing_token,d.document_number),('new-owner','new-result'))

    def test_legacy_processing_record_without_lease_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            Path(directory, 'test.pdf').touch()
            d = self.document(status='processing')
            with patch('app.services.document_jobs.DocumentProcessingPipeline') as pipeline:
                pipeline.return_value.process.return_value = ParsedDocument(document_number='legacy')
                self.assertEqual(run_pending_document_jobs(1), 1)
            d.refresh_from_db()
            self.assertEqual((d.status, d.document_number), ('processed', 'legacy'))

    def test_previous_owner_error_cannot_overwrite_new_owner(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            Path(directory, 'test.pdf').touch()
            d = self.document(status='queued')
            def process(_):
                Document.objects.filter(pk=d.pk).update(processing_token='replacement', error='new-owner-state')
                raise RuntimeError('stale-worker-error')
            with patch('app.services.document_jobs.DocumentProcessingPipeline') as pipeline:
                pipeline.return_value.process.side_effect = process
                with self.assertLogs('app.services.document_jobs', level='ERROR'):
                    run_pending_document_jobs(1)
            d.refresh_from_db()
            self.assertEqual((d.status, d.processing_token, d.error), ('processing', 'replacement', 'new-owner-state'))


from django.test import TransactionTestCase


class ConcurrentDocumentLeaseTests(TransactionTestCase):
    def test_second_worker_skips_active_first_worker(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connections
        started, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            Path(directory, 'test.pdf').touch()
            document = DocumentLeaseTests.document(self, status='queued', created_at='2000-01-01')
            def process(_):
                started.set()
                if not release.wait(10):
                    raise RuntimeError('Test worker timeout')
                return ParsedDocument(document_number='one-call')
            def worker():
                try:
                    return run_pending_document_jobs(1)
                finally:
                    connections.close_all()
            with patch('app.services.document_jobs.DocumentProcessingPipeline') as pipeline, ThreadPoolExecutor(max_workers=1) as pool:
                pipeline.return_value.process.side_effect = process
                future = pool.submit(worker)
                try:
                    self.assertTrue(started.wait(10))
                    self.assertEqual(run_pending_document_jobs(1), 0)
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=10), 1)
                self.assertEqual(pipeline.return_value.process.call_count, 1)
            document.refresh_from_db()
            self.assertEqual((document.status, document.document_number), ('processed', 'one-call'))
