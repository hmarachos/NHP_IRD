from unittest.mock import patch

from django.db import transaction
from django.test import TestCase

from app.services.document_preparation import run_pending_preparations
from app.services.document_text import _sanitize_ocr_text
from app.services.project_qa import retrieve_project_sources
from migration_portal.models import Correspondence, Document, PreparedDocument, Project, ProjectChecklistItem


class DocumentPreparationTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Объект", contract_number="42")

    def document(self, **values):
        return Document.objects.create(project=self.project, contract_number="42", object_name="Объект", original_filename="test.pdf", stored_filename="test.pdf", **values)

    def test_ocr_is_prepared_before_search_and_preserves_original_pages_paragraphs(self):
        raw = "--- page 1 ---\nТребования\u00a0 к  электроснабжению\n\nПервый абзац\n|||\n--- page 2 ---\nЭлектроснабжение площадки"
        document = self.document(raw_text=raw)
        self.assertEqual(document.prepared.status, "pending")
        self.assertEqual(retrieve_project_sources(self.project.pk, "электроснабжение"), [])
        self.assertEqual(run_pending_preparations(), 1)
        document.refresh_from_db()
        prepared = document.prepared
        self.assertEqual(document.raw_text, raw)
        self.assertIn("Требования к электроснабжению\n\nПервый", prepared.normalized_text)
        self.assertNotIn("|||", prepared.normalized_text)
        self.assertEqual(list(prepared.chunks.values_list("page", flat=True)), [1, 2])
        with patch("app.services.document_preparation._sanitize_ocr_text", side_effect=AssertionError("Query must not normalize OCR")):
            sources = retrieve_project_sources(self.project.pk, "электроснабжение")
        self.assertTrue(sources)
        self.assertTrue(all(source["page"] in (1, 2) for source in sources))
        self.assertEqual(_sanitize_ocr_text(prepared.normalized_text), prepared.normalized_text)

    def test_card_edit_updates_metadata_without_changing_ocr_or_chunks(self):
        document = self.document(raw_text="--- page 1 ---\nЭлектроснабжение по письму № OLD", document_number="OLD")
        run_pending_preparations()
        before = list(document.prepared.chunks.values_list("id", "text"))
        # Saving only the number must not use unsaved raw_text from the instance.
        document.document_number = "NEW"
        document.raw_text = "unsaved text"
        document.save(update_fields=["document_number"])
        run_pending_preparations()
        prepared = PreparedDocument.objects.get(document=document)
        self.assertEqual(prepared.metadata["document_number"], "NEW")
        self.assertEqual(list(prepared.chunks.values_list("id", "text")), before)
        sources = retrieve_project_sources(self.project.pk, "электроснабжение")
        self.assertIn("NEW", sources[0]["metadata"]["Реквизиты карточки"])
        self.assertIn("OLD", sources[0]["text"])

    def test_queue_survives_rollback_and_does_not_process_unfinished_ocr(self):
        document = self.document(status="queued")
        self.assertEqual(run_pending_preparations(), 0)
        try:
            with transaction.atomic():
                document.raw_text = "Текст"
                document.status = "processed"
                document.save()
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        document.refresh_from_db()
        self.assertEqual(document.status, "queued")
        self.assertEqual(run_pending_preparations(), 0)
        document.status = "processed"
        document.raw_text = "Текст"
        document.save()
        self.assertEqual(run_pending_preparations(), 1)

    def test_failed_publication_retains_old_complete_chunks_and_can_retry(self):
        document = self.document(raw_text="--- page 1 ---\nЭлектроснабжение старое")
        run_pending_preparations()
        before = list(document.prepared.chunks.values_list("id", "text"))
        document.raw_text = "--- page 2 ---\nЭлектроснабжение новое"
        document.save()
        with patch("app.services.document_preparation.DocumentChunk.objects.bulk_create", side_effect=RuntimeError("write failed")):
            with self.assertLogs("app.services.document_preparation", level="ERROR"):
                run_pending_preparations()
        prepared = PreparedDocument.objects.get(document=document)
        self.assertEqual(prepared.status, "error")
        self.assertEqual(list(prepared.chunks.values_list("id", "text")), before)
        self.assertEqual(retrieve_project_sources(self.project.pk, "электроснабжение"), [])
        document.save()
        run_pending_preparations()
        prepared.refresh_from_db()
        self.assertEqual(prepared.status, "ready")
        self.assertEqual(list(prepared.chunks.values_list("page", flat=True)), [2])
        self.assertEqual(prepared.revision, 2)

    def test_correspondence_and_archive_use_same_preparation(self):
        letter = Correspondence.objects.create(project=self.project, direction="incoming", raw_text="--- page 1 ---\nСогласование электроснабжения", number="42")
        run_pending_preparations()
        self.assertEqual(letter.prepared.status, "ready")
        self.assertTrue(retrieve_project_sources(self.project.pk, "электроснабжение"))
        self.project.archived_at = "2026-11-20"
        self.project.save()
        self.assertEqual(retrieve_project_sources(self.project.pk, "электроснабжение"), [])

    def test_latest_card_wins_and_current_checklist_state_is_read_live(self):
        document = self.document(raw_text="Электроснабжение объекта", document_number="one")
        document.document_number = "two"
        document.save(update_fields=["document_number"])
        document.document_number = "three"
        document.save(update_fields=["document_number"])
        self.assertEqual(run_pending_preparations(), 1)
        self.assertEqual(PreparedDocument.objects.get(document=document).metadata["document_number"], "three")
        item = ProjectChecklistItem.objects.create(project=self.project, title="Технические условия электроснабжения", document=document, status="received", replacement_required=True)
        before = list(document.prepared.chunks.values_list("id", flat=True))
        item.replacement_required = False
        item.save()
        sources = retrieve_project_sources(self.project.pk, "электроснабжение")
        self.assertIn("требует замены: нет", sources[0]["metadata"]["Текущее состояние комплектности"])
        self.assertEqual(list(document.prepared.chunks.values_list("id", flat=True)), before)

    def test_error_document_is_not_searchable_even_with_recognized_text(self):
        self.document(raw_text="Электроснабжение объекта", status="error")
        run_pending_preparations()
        self.assertEqual(retrieve_project_sources(self.project.pk, "электроснабжение"), [])

    def test_answer_receives_prepared_requisites_and_current_checklist_state(self):
        from types import SimpleNamespace
        from app.services.project_qa import answer_project_question
        document = self.document(raw_text="Электроснабжение объекта", document_number="CURRENT")
        run_pending_preparations()
        ProjectChecklistItem.objects.create(project=self.project, title="Технические условия", document=document, status="received", replacement_required=False)
        sources = retrieve_project_sources(self.project.pk, "электроснабжение")
        with patch("app.services.project_qa.ai_client") as client:
            client.return_value.responses.create.return_value = SimpleNamespace(output_text='{"answer":"Получен [1]", "used_sources":[1]}')
            answer_project_question("Электроснабжение?", sources)
            payload = client.return_value.responses.create.call_args.kwargs["input"]
        self.assertIn("CURRENT", payload)
        self.assertIn("требует замены: нет", payload)
        self.assertIn("статус: документ получен", payload)

    def test_superseded_job_cannot_publish_over_a_newer_request(self):
        from app.services.document_preparation import prepare_document
        document = self.document(raw_text="Первый текст")
        old_ticket = document.prepared.requested_revision
        document.raw_text = "Новый текст"
        document.save()
        self.assertFalse(prepare_document(document.prepared.pk, old_ticket))
        run_pending_preparations()
        self.assertEqual(PreparedDocument.objects.get(document=document).normalized_text, "Новый текст")

    def test_deleting_project_does_not_recreate_cache_revisions(self):
        from django.db import connection
        from migration_portal.models import ProjectSearchRevision
        self.document(raw_text="Электроснабжение")
        run_pending_preparations()
        self.project.delete()
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        # Project deletion preserves uploaded documents (SET_NULL).
        self.assertEqual(PreparedDocument.objects.count(), 1)
        self.assertEqual(ProjectSearchRevision.objects.count(), 0)


from django.test import TransactionTestCase


class ConcurrentPreparationTests(TransactionTestCase):
    def test_card_edit_during_preparation_is_queued_after_publication(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connections
        from app.services.document_preparation import prepare_document

        project = Project.objects.create(name="Объект", contract_number="42")
        document = Document.objects.create(project=project, contract_number="42", object_name="Объект", original_filename="test.pdf", stored_filename="test.pdf", raw_text="Электроснабжение", document_number="old")
        prepared_id = document.prepared.pk
        started, release, editing = threading.Event(), threading.Event(), threading.Event()

        def normalize(text):
            started.set()
            if not release.wait(10):
                raise RuntimeError("test timeout")
            return _sanitize_ocr_text(text)

        def prepare():
            try:
                return prepare_document(prepared_id)
            finally:
                connections.close_all()

        def edit():
            try:
                source = Document.objects.get(pk=document.pk)
                source.document_number = "new"
                editing.set()
                source.save(update_fields=["document_number"])
            finally:
                connections.close_all()

        with patch("app.services.document_preparation._sanitize_ocr_text", side_effect=normalize), ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(prepare)
            try:
                self.assertTrue(started.wait(10))
                changed = pool.submit(edit)
                self.assertTrue(editing.wait(10))
            finally:
                release.set()
            self.assertTrue(future.result(timeout=10))
            changed.result(timeout=10)
        self.assertEqual(PreparedDocument.objects.get(pk=prepared_id).status, "pending")
        run_pending_preparations()
        prepared = PreparedDocument.objects.get(pk=prepared_id)
        self.assertEqual(prepared.metadata["document_number"], "new")
        self.assertEqual(prepared.revision, 1)
