import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings, RequestFactory
from django.urls import reverse
from pypdf import PdfReader, PdfWriter

from app.services.correspondence_attachments import append_correspondence_files
from app.services.document_pages import restore_page_markers
from app.services.document_preparation import run_pending_preparations
from app.services.project_qa import retrieve_project_sources
from migration_portal.models import Correspondence, Project, UserProfile


def pdf_bytes(width):
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=300)
    buffer = io.BytesIO()
    writer.write(buffer)
    writer.close()
    return buffer.getvalue()


class CorrespondenceAttachmentTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.folder.name, SECURE_SSL_REDIRECT=False)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.root = Path(self.folder.name)
        self.project = Project.objects.create(name="Тест", contract_number="TEST")
        self.root.joinpath("original.pdf").write_bytes(pdf_bytes(100))
        self.letter = Correspondence.objects.create(project=self.project, direction="incoming", number="ORIGINAL/42", sender="Отправитель", subject="Исходное письмо", summary="Сохранённое описание", original_filename="original.pdf", stored_filename="original.pdf", mime_type="application/pdf", raw_text="--- page 1 ---\nИсходный текст письма")

    def upload(self, width=200, name="appendix.pdf"):
        return SimpleUploadedFile(name, pdf_bytes(width), content_type="application/pdf")

    def test_append_preserves_page_order_card_and_original_file_and_indexes_new_page(self):
        original_bytes = self.root.joinpath("original.pdf").read_bytes()
        original_text = self.letter.raw_text
        with patch("app.services.pdf_text.PdfTextExtractor.extract", return_value="--- page 1 ---\nПриложение: разрешённая мощность 137,5 кВт"):
            self.assertEqual(append_correspondence_files(self.project.pk, self.letter.pk, [self.upload(200), self.upload(250)]), (2, 3))
        self.letter.refresh_from_db()
        reader = PdfReader(self.root / self.letter.stored_filename)
        self.assertEqual([float(page.mediabox.width) for page in reader.pages], [100, 200, 250])
        self.assertEqual(self.root.joinpath("original.pdf").read_bytes(), original_bytes)
        self.assertTrue(self.letter.raw_text.startswith(original_text))
        self.assertIn("--- page 2 ---", self.letter.raw_text)
        self.assertEqual((self.letter.number, self.letter.sender, self.letter.subject, self.letter.summary), ("ORIGINAL/42", "Отправитель", "Исходное письмо", "Сохранённое описание"))
        run_pending_preparations()
        sources = retrieve_project_sources(self.project.pk, "Какая разрешённая мощность указана в приложении?")
        self.assertTrue(any(source["page"] == 2 and "137,5" in source["text"] for source in sources))

    def test_second_append_uses_latest_document_and_continuous_page_numbers(self):
        with patch("app.services.pdf_text.PdfTextExtractor.extract", return_value="--- page 1 ---\nПервое приложение"):
            append_correspondence_files(self.project.pk, self.letter.pk, [self.upload(200)])
        with patch("app.services.pdf_text.PdfTextExtractor.extract", return_value="--- page 1 ---\nВторое приложение"):
            self.assertEqual(append_correspondence_files(self.project.pk, self.letter.pk, [self.upload(250)]), (1, 3))
        self.letter.refresh_from_db()
        self.assertEqual([float(page.mediabox.width) for page in PdfReader(self.root / self.letter.stored_filename).pages], [100, 200, 250])
        self.assertIn("--- page 3 ---\nВторое приложение", self.letter.raw_text)

    def test_failure_preserves_existing_file_row_and_removes_staging_files(self):
        original = self.root.joinpath("original.pdf").read_bytes()
        with patch("app.services.pdf_text.PdfTextExtractor.extract", return_value="--- page 1 ---\nText"), patch.object(Correspondence, "save", side_effect=RuntimeError("write failure")):
            with self.assertRaises(RuntimeError):
                append_correspondence_files(self.project.pk, self.letter.pk, [self.upload()])
        self.letter.refresh_from_db()
        self.assertEqual(self.letter.stored_filename, "original.pdf")
        self.assertEqual(self.root.joinpath("original.pdf").read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["original.pdf"])

    def test_ocr_failure_keeps_added_pages_and_reports_warning(self):
        notices = []
        with patch("app.services.pdf_text.PdfTextExtractor.extract", side_effect=RuntimeError("OCR failure")):
            with self.assertLogs("app.services.correspondence_attachments", level="WARNING"):
                self.assertEqual(append_correspondence_files(self.project.pk, self.letter.pk, [self.upload()], ocr_warnings=notices), (1, 2))
        self.letter.refresh_from_db()
        self.assertEqual(len(PdfReader(self.root / self.letter.stored_filename).pages), 2)
        self.assertEqual(self.letter.number, "ORIGINAL/42")
        self.assertTrue(notices)

    def test_endpoint_requires_csrf_edit_permission_and_project_scope(self):
        from django.test import Client
        user = get_user_model().objects.create_superuser(username="append-test", password="TestPass123!!")
        self.client.force_login(user)
        page = self.client.get(reverse("web_object_workspace", args=[self.project.pk])+"?tab=correspondence")
        self.assertContains(page, "data-correspondence-append")
        url = reverse("web_append_correspondence", args=[self.project.pk, self.letter.pk])
        secure_client = Client(enforce_csrf_checks=True)
        secure_client.force_login(user)
        self.assertEqual(secure_client.post(url, {"files": self.upload()}).status_code, 403)
        with patch("app.services.pdf_text.PdfTextExtractor.extract", return_value="--- page 1 ---\nПриложение"):
            response = self.client.post(url, {"files": self.upload()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["page_count"], 2)
        self.assertIn("page=2", response.json()["preview"])
        other = Project.objects.create(name="Другой", contract_number="OTHER")
        self.assertEqual(self.client.post(reverse("web_append_correspondence", args=[other.pk, self.letter.pk]), {"files": self.upload()}).status_code, 404)
        user.is_superuser = False
        user.save(update_fields=["is_superuser"])
        profile = UserProfile.objects.create(user=user, access_level="view")
        profile.allowed_projects.add(self.project)
        self.assertEqual(self.client.post(url, {"files": self.upload()}).status_code, 403)

    def test_invalid_file_does_not_modify_original(self):
        with self.assertRaises(ValueError):
            append_correspondence_files(self.project.pk, self.letter.pk, [SimpleUploadedFile("bad.pdf", b"broken")])
        self.letter.refresh_from_db()
        self.assertEqual(self.letter.stored_filename, "original.pdf")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["original.pdf"])

    def test_old_unmarked_prefix_is_mapped_before_new_appendix_pages(self):
        from types import SimpleNamespace
        pages = [SimpleNamespace(extract_text=lambda: "First page"), SimpleNamespace(extract_text=lambda: "Second page")]
        with patch("pypdf.PdfReader", return_value=SimpleNamespace(pages=pages)):
            mapped, _ = restore_page_markers("First page\n\n--- page 2 ---\nSecond page", self.root / "original.pdf")
        self.assertTrue(mapped.startswith("--- page 1 ---\nFirst page"))
        self.assertIn("--- page 2 ---\nSecond page", mapped)
