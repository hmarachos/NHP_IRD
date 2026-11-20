import io
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfWriter

from app.models import ParsedDocument
from migration_portal.models import Document, Project, ProjectChecklistItem


@override_settings(SECURE_SSL_REDIRECT=False)
class GeneralDocumentUploadTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('upload-test', password='test'))
        self.project = Project.objects.create(contract_number='UPLOAD', name='Upload project')
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def upload(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buffer = io.BytesIO()
        writer.write(buffer)
        with patch('app.services.pipeline.DocumentProcessingPipeline') as pipeline:
            pipeline.return_value.process.return_value = ParsedDocument(working_title='Новый документ')
            response = self.client.post(reverse('web_upload_documents', args=[self.project.pk]), {
                'files': SimpleUploadedFile('new.pdf', buffer.getvalue(), content_type='application/pdf'),
            }, HTTP_X_IRD_ANALYZE='1')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_toolbar_button_opens_shared_upload_modal(self):
        response = self.client.get(reverse('web_object_workspace', args=[self.project.pk]))
        self.assertContains(response, 'data-item-title="Загрузить документ"')
        html = response.content.decode()
        self.assertLess(html.index('data-item-title="Загрузить документ"'), html.index('data-checklist-lock='))

    def test_analyze_then_save_creates_visible_checklist_document(self):
        result = self.upload()
        self.assertTrue(result['general_upload'])
        self.assertEqual(result['fields']['working_title'], 'Новый документ')
        self.assertFalse(ProjectChecklistItem.objects.filter(project=self.project).exists())
        with patch('app.services.email_notifications.queue_saved_document_notification'):
            response = self.client.post(reverse('web_update_document_fields', args=[result['id']]), {
                'working_title': 'Проверенное название',
            }, HTTP_X_REQUESTED_WITH='fetch', HTTP_X_ADD_TO_CHECKLIST='1', HTTP_X_PRESERVE_CHECKLIST_LINK='1')
        self.assertEqual(response.status_code, 204)
        item = ProjectChecklistItem.objects.get(project=self.project)
        self.assertEqual((item.title, item.document_id, item.status), ('Проверенное название', result['id'], 'received'))

    def test_empty_upload_returns_json_error(self):
        response = self.client.post(reverse('web_upload_documents', args=[self.project.pk]), HTTP_X_IRD_ANALYZE='1')
        self.assertEqual(response.status_code, 422)
        self.assertIn('error', response.json())
        self.assertFalse(Document.objects.filter(project=self.project).exists())
