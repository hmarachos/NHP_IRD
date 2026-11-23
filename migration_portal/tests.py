import io
import hashlib
import re
import json
import tempfile
import zipfile
from unittest.mock import patch
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model

from migration_portal.models import ChecklistDocument, ChecklistExpiryNotification, ChecklistTemplate, ChecklistTemplateItem, Correspondence, Customer, Document, DocumentVersionGroup, ExpiryNotification, HandoverAct, Organization, Project, ProjectChecklistItem, ProjectNotebookEntry, UserProfile
from pypdf import PdfWriter
from app.models import ParsedDocument
from migration_portal.document_matching import find_duplicate
from migration_portal.checklist_matching import suggest_checklist_items
from types import SimpleNamespace


@override_settings(SECURE_SSL_REDIRECT=False)
class DjangoPostgresProjectFlowTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        username = f"migration-test-{id(self)}"
        self.user = get_user_model().objects.create_superuser(username=username, email=f"{username}@example.test", password="TestPass123!!")
        self.client.force_login(self.user)
        self.addCleanup(self.user.delete)

    def test_portal_requires_login_and_health_check_stays_public(self):
        anonymous = Client()
        response = anonymous.get(reverse("web_index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
        self.assertEqual(anonymous.get(reverse("health")).status_code, 200)
        self.assertEqual(self.client.get(reverse("web_index")).status_code, 200)

    def csrf_token(self):
        response = self.client.get(reverse("web_index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Активные проекты")
        self.assertIn("csrftoken", self.client.cookies)
        import re
        return re.search(r'name="csrf_token" value="([^"]+)"', response.content.decode()).group(1)

    def test_project_can_add_all_missing_items_from_a_selected_template(self):
        template_id = (ChecklistTemplate.objects.order_by("-id").values_list("id", flat=True).first() or 0) + 1
        template = ChecklistTemplate.objects.create(id=template_id, name="Mass add template")
        template_items = [
            ChecklistTemplateItem.objects.create(
                id=(ChecklistTemplateItem.objects.order_by("-id").values_list("id", flat=True).first() or 0) + 1,
                template=template, title=f"Template document {index}", sort_order=index,
            )
            for index in range(3)
        ]
        project_id = (Project.objects.order_by("-id").values_list("id", flat=True).first() or 0) + 1
        project = Project.objects.create(id=project_id, contract_number="MASS-ADD", name="Mass add project")
        self.addCleanup(project.delete)
        existing_item = ProjectChecklistItem.objects.create(
            project=project, source_template_item=template_items[0], title=template_items[0].title,
        )
        response = self.client.get(reverse("web_object_workspace", args=[project.id]))
        self.assertContains(response, "Mass add template")
        token = re.search(r'name="csrf_token" value="([^"]+)"', response.content.decode()).group(1)

        response = self.client.post(reverse("web_add_checklist_item", args=[project.id]), {
            "csrf_token": token, "add_mode": "template", "template_id": str(template.id),
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProjectChecklistItem.objects.filter(project=project).count(), 3)
        self.assertEqual(ProjectChecklistItem.objects.get(pk=existing_item.pk).title, template_items[0].title)
        self.assertEqual(
            set(ProjectChecklistItem.objects.filter(project=project).values_list("source_template_item_id", flat=True)),
            {item.id for item in template_items},
        )

    def test_project_create_update_archive_restore_and_workspace_use_postgres(self):
        token = self.csrf_token()
        blocked = Client(enforce_csrf_checks=True).post(reverse("web_create_object"), {
            "contract_number": "NO-CSRF", "name": "Blocked project",
        })
        self.assertEqual(blocked.status_code, 403)

        response = self.client.post(reverse("web_create_object"), {
            "csrf_token": token,
            "contract_number": "DJANGO-TEST-001",
            "contract_date": "2026-11-19",
            "name": "Проект из Django",
            "customer": "Заказчик",
        })
        project = Project.objects.get(contract_number="DJANGO-TEST-001")
        self.addCleanup(project.delete)
        self.assertRedirects(response, reverse("web_object_workspace", args=[project.id]))
        self.assertContains(self.client.get(response["Location"]), "Проект из Django")

        response = self.client.post(reverse("web_update_object", args=[project.id]), {
            "csrf_token": token, "contract_number": "DJANGO-TEST-002",
            "contract_date": "2026-11-20", "name": "Обновлённый проект", "customer": "Заказчик 2",
        })
        self.assertRedirects(response, reverse("web_index"))
        project.refresh_from_db()
        self.assertEqual(project.name, "Обновлённый проект")

        response = self.client.post(reverse("web_archive_object", args=[project.id]), {"csrf_token": token})
        self.assertRedirects(response, reverse("web_index"))
        project.refresh_from_db()
        self.assertTrue(project.archived_at)
        response = self.client.post(reverse("web_restore_object", args=[project.id]), {"csrf_token": token})
        self.assertRedirects(response, reverse("web_index"))
        project.refresh_from_db()
        self.assertIsNone(project.archived_at)

    def test_project_stage_and_notebook_use_postgres_and_sanitize_html(self):
        token = self.csrf_token()
        response = self.client.post(reverse("web_create_object"), {
            "csrf_token": token,
            "contract_number": "NOTEBOOK-001",
            "name": "Проект с записной книжкой",
            "stage": "Строительный проект",
        })
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(contract_number="NOTEBOOK-001")
        self.addCleanup(project.delete)
        self.assertEqual(project.stage, "Строительный проект")
        self.assertEqual(self.client.get(reverse("web_project_notebook", args=[project.id])).status_code, 200)

        response = self.client.post(reverse("web_save_notebook_entry", args=[project.id]), {
            "csrf_token": token,
            "title": "  Совещание  ",
            "content": '<p><strong>Итог</strong><img src=x onerror="alert(1)"><script>alert(2)</script></p>',
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["title"], "Совещание")
        self.assertIn("<strong>Итог</strong>", payload["content"])
        self.assertNotIn("<script", payload["content"])
        self.assertNotIn("onerror", payload["content"])

        entry_id = payload["id"]
        response = self.client.post(reverse("web_delete_notebook_entry", args=[project.id, entry_id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 204)
        self.assertFalse(ProjectNotebookEntry.objects.filter(pk=entry_id).exists())

        response = self.client.post(reverse("web_update_object", args=[project.id]), {
            "csrf_token": token, "contract_number": project.contract_number, "name": project.name,
            "stage": "Неизвестная стадия",
        })
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.stage, "")

    def test_template_items_are_copied_to_project_checklist(self):
        token = self.csrf_token()
        template = ChecklistTemplate.objects.create(name="Test checklist")
        self.addCleanup(template.delete)
        ChecklistTemplateItem.objects.create(template=template, title="Исходные данные", description="Описание", sort_order=2, validity_years=3)
        response = self.client.post(reverse("web_create_object"), {
            "csrf_token": token, "contract_number": "DJANGO-TEMPLATE-001",
            "name": "Проект с чек-листом", "template_id": str(template.id),
        })
        project = Project.objects.get(contract_number="DJANGO-TEMPLATE-001")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProjectChecklistItem.objects.filter(project=project).count(), 1)
        item = ProjectChecklistItem.objects.get(project=project)
        self.assertEqual((item.title, item.description, item.validity_years), ("Исходные данные", "Описание", 3))

    def test_deleting_template_keeps_existing_project_checklist_snapshot(self):
        token = self.csrf_token()
        template = ChecklistTemplate.objects.create(name="Template snapshot")
        ChecklistTemplateItem.objects.create(template=template, title="Сохранённый пункт", sort_order=0)
        project = Project.objects.create(contract_number="TEMPLATE-SNAPSHOT-001", name="Template snapshot project", template=template)
        item = ProjectChecklistItem.objects.create(
            project=project, source_template_item_id=ChecklistTemplateItem.objects.get(template=template).id,
            title="Сохранённый пункт", sort_order=0,
        )
        response = self.client.post(reverse("web_delete_template", args=[template.id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        item.refresh_from_db()
        self.assertIsNone(project.template_id)
        self.assertEqual(item.title, "Сохранённый пункт")
        self.assertTrue(ProjectChecklistItem.objects.filter(pk=item.pk).exists())

    def test_template_management_crud_uses_django_and_postgres(self):
        token = self.csrf_token()
        response = self.client.get(reverse("web_manage_templates"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Шаблоны")

        response = self.client.post(reverse("web_create_template"), {
            "csrf_token": token, "name": "Migration template", "description": "Created in Django",
        })
        self.assertEqual(response.status_code, 302)
        template = ChecklistTemplate.objects.get(name="Migration template")
        self.addCleanup(template.delete)

        response = self.client.post(reverse("web_add_template_item", args=[template.id]), {
            "csrf_token": token, "title": "Исходные данные", "description": "Description",
        })
        self.assertEqual(response.status_code, 302)
        item = ChecklistTemplateItem.objects.get(template=template)

        response = self.client.post(reverse("web_update_template_item", args=[item.id]), {
            "csrf_token": token, "title": "Обновлённый пункт", "description": "Updated",
        })
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual((item.title, item.description), ("Обновлённый пункт", "Updated"))

        response = self.client.post(reverse("web_update_template", args=[template.id]), {
            "csrf_token": token, "name": "Migration template updated", "description": "Updated",
        })
        self.assertEqual(response.status_code, 302)
        template.refresh_from_db()
        self.assertEqual((template.name, template.description), ("Migration template updated", "Updated"))

        response = self.client.post(reverse("web_copy_template", args=[template.id]), {
            "csrf_token": token, "name": "Migration template copy",
        })
        self.assertEqual(response.status_code, 302)
        copied = ChecklistTemplate.objects.get(name="Migration template copy")
        self.addCleanup(copied.delete)
        self.assertEqual(list(ChecklistTemplateItem.objects.filter(template=copied).values_list("title", flat=True)), ["Обновлённый пункт"])

        response = self.client.post(reverse("web_delete_template_item", args=[item.id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ChecklistTemplateItem.objects.filter(pk=item.id).exists())
        response = self.client.post(reverse("web_delete_template", args=[copied.id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ChecklistTemplate.objects.filter(pk=copied.id).exists())

    def test_project_workspace_reads_documents_from_postgres(self):
        project = Project.objects.create(contract_number="DOC-001", name="Документы PostgreSQL")
        self.addCleanup(project.delete)
        Document.objects.create(
            project=project,
            contract_number=project.contract_number,
            object_name=project.name,
            original_filename="source.pdf",
            stored_filename="source.pdf",
            working_title="Исходный документ",
        )
        document = Document.objects.get(project=project)
        ProjectChecklistItem.objects.create(
            project=project,
            title="Пункт со связанным документом",
            status="received",
            document=document,
        )
        response = self.client.get(reverse("web_object_workspace", args=[project.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get("X-Migration-Route"))
        self.assertContains(response, "Исходный документ")
        self.assertContains(response, "Документы проекта")
        self.assertContains(response, f'href="/objects/{project.id}/acts"')
        self.assertContains(response, f'href="/objects/{project.id}/export"')
        self.assertContains(response, "/static/styles.css")

    def test_checklist_crud_is_native_csrf_protected_and_scoped_to_project(self):
        token = self.csrf_token()
        project = Project.objects.create(contract_number="CHECKLIST-001", name="Checklist project")
        other_project = Project.objects.create(contract_number="CHECKLIST-002", name="Other project")
        self.addCleanup(project.delete)
        self.addCleanup(other_project.delete)
        item = ProjectChecklistItem.objects.create(project=project, title="Договор аренды")

        denied = Client(enforce_csrf_checks=True).post(
            reverse("web_update_checklist_replacement", args=[project.id, item.id]),
            {"required": "1"},
        )
        self.assertEqual(denied.status_code, 403)

        response = self.client.post(
            reverse("web_update_checklist_replacement", args=[other_project.id, item.id]),
            {"csrf_token": token, "required": "1"},
        )
        self.assertEqual(response.status_code, 404)
        item.refresh_from_db()
        self.assertFalse(item.replacement_required)

        response = self.client.post(
            reverse("web_add_checklist_item", args=[project.id]),
            {"csrf_token": token, "title": "Документ заказчика", "note": "note"},
        )
        self.assertEqual(response.status_code, 302)
        custom = ProjectChecklistItem.objects.get(project=project, title="Документ заказчика")

        response = self.client.post(
            reverse("web_update_checklist_properties", args=[project.id, custom.id]),
            {"csrf_token": token, "title": "Документ заказчика 2", "description": "description",
             "note": "updated", "issue_date": "2026-01-01", "validity_years": "2"},
            HTTP_X_REQUESTED_WITH="fetch",
        )
        self.assertEqual(response.status_code, 200)
        custom.refresh_from_db()
        self.assertEqual(custom.valid_until, "2028-01-01")

        response = self.client.post(
            reverse("web_delete_checklist_item", args=[project.id, custom.id]),
            {"csrf_token": token}, HTTP_X_REQUESTED_WITH="fetch",
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(ProjectChecklistItem.objects.filter(pk=custom.id).exists())

    def test_native_pdf_upload_validates_file_and_queues_in_postgres(self):
        token = self.csrf_token()
        project = Project.objects.create(contract_number="UPLOAD-NATIVE-001", name="Native upload")
        self.addCleanup(project.delete)
        with tempfile.TemporaryDirectory(prefix="django-native-upload-") as upload_dir:
            with override_settings(MEDIA_ROOT=upload_dir):
                stream = io.BytesIO()
                writer = PdfWriter()
                writer.add_blank_page(width=100, height=100)
                writer.write(stream)
                response = self.client.post(
                    reverse("web_upload_documents", args=[project.id]),
                    {"csrf_token": token, "files": SimpleUploadedFile("native.pdf", stream.getvalue(), content_type="application/pdf")},
                )
                self.assertEqual(response.status_code, 302)
                document = Document.objects.get(project=project)
                self.assertEqual(document.status, "queued")
                self.assertTrue(Path(upload_dir, document.stored_filename).is_file())

                response = self.client.post(
                    reverse("web_upload_documents", args=[project.id]),
                    {"csrf_token": token, "files": SimpleUploadedFile("fake.pdf", b"not a pdf", content_type="application/pdf")},
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Document.objects.filter(project=project).count(), 1)

                stream = io.BytesIO()
                writer = PdfWriter()
                writer.add_blank_page(width=100, height=100)
                writer.write(stream)
                response = self.client.post(
                    reverse("web_upload_documents", args=[project.id]),
                    {"csrf_token": token, "files": [
                        SimpleUploadedFile("accepted.pdf", stream.getvalue(), content_type="application/pdf"),
                        SimpleUploadedFile("rejected.txt", b"unsupported", content_type="text/plain"),
                    ]},
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Document.objects.filter(project=project).count(), 2)
                self.assertTrue(Path(upload_dir, Document.objects.get(project=project, original_filename="accepted.pdf").stored_filename).is_file())

    def test_native_document_fields_archive_restore_and_delete(self):
        token = self.csrf_token()
        project = Project.objects.create(contract_number="DOCUMENT-001", name="Document lifecycle")
        self.addCleanup(project.delete)
        document = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="source.pdf", stored_filename="source.pdf",
        )
        item = ProjectChecklistItem.objects.create(project=project, title="Source document")
        Document.objects.filter(pk=document.pk).update(working_title="Source document")

        response = self.client.post(reverse("web_update_document_fields", args=[document.id]), {
            "csrf_token": token, "working_title": "Source document", "official_title": "Official",
            "issuer": "Issuer", "document_number": "N-1", "issue_date": "2026-01-01",
            "valid_until": "2027-01-01",
        }, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 204)
        document.refresh_from_db()
        self.assertEqual(document.document_number, "N-1")
        item.refresh_from_db()
        self.assertEqual(item.document_id, document.id)

        response = self.client.post(reverse("web_archive_document", args=[document.id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        document.refresh_from_db()
        item.refresh_from_db()
        self.assertTrue(document.archived_at)
        self.assertIsNone(item.document_id)
        self.assertEqual(item.status, "missing")

        response = self.client.post(reverse("web_restore_document", args=[document.id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        document.refresh_from_db()
        self.assertIsNone(document.archived_at)
        response = self.client.post(reverse("web_delete_document", args=[document.id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Document.objects.filter(pk=document.id).exists())

    def test_workspace_matches_legacy_upload_and_document_delete_promotes_next_link(self):
        project = Project.objects.create(contract_number="MULTI-DELETE-001", name="Multi document workflow")
        self.addCleanup(project.delete)
        single_item = ProjectChecklistItem.objects.create(project=project, title="Справка")
        legacy_document = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="legacy.pdf", stored_filename="legacy.pdf", working_title="Справка",
        )
        response = self.client.get(reverse("web_object_workspace", args=[project.id]))
        self.assertEqual(response.status_code, 200)
        single_item.refresh_from_db()
        self.assertEqual(single_item.document_id, legacy_document.id)

        multi_item = ProjectChecklistItem.objects.create(project=project, title="Задание на проектирование")
        first = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="first.pdf", stored_filename="first.pdf",
        )
        second = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="second.pdf", stored_filename="second.pdf",
        )
        multi_item.document = first
        multi_item.status = "received"
        multi_item.save()
        ChecklistDocument.objects.create(checklist_item=multi_item, document=first, sort_order=0)
        ChecklistDocument.objects.create(checklist_item=multi_item, document=second, sort_order=1)
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get(reverse("web_index")).content.decode()).group(1)
        response = self.client.post(reverse("web_delete_document", args=[first.id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        multi_item.refresh_from_db()
        self.assertEqual(multi_item.document_id, second.id)
        self.assertEqual(multi_item.status, "received")

    def test_template_conflict_reports_ajax_error(self):
        first = ChecklistTemplate.objects.create(name="Unique template A")
        second = ChecklistTemplate.objects.create(name="Unique template B")
        self.addCleanup(first.delete)
        self.addCleanup(second.delete)
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get(reverse("web_index")).content.decode()).group(1)
        response = self.client.post(
            reverse("web_update_template", args=[second.id]),
            {"csrf_token": token, "name": first.name, "description": ""},
            HTTP_X_REQUESTED_WITH="fetch",
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])

    def test_archive_and_restore_keep_remaining_multi_document_link(self):
        project = Project.objects.create(contract_number="MULTI-ARCHIVE-001", name="Archive multiple")
        self.addCleanup(project.delete)
        item = ProjectChecklistItem.objects.create(project=project, title="Задание на проектирование")
        first = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="first.pdf", stored_filename="first.pdf",
        )
        second = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="second.pdf", stored_filename="second.pdf",
        )
        item.document, item.status = first, "received"
        item.save()
        ChecklistDocument.objects.create(checklist_item=item, document=first, sort_order=0)
        ChecklistDocument.objects.create(checklist_item=item, document=second, sort_order=1)
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get(reverse("web_index")).content.decode()).group(1)
        response = self.client.post(reverse("web_archive_document", args=[first.id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.document_id, second.id)
        self.assertEqual(item.status, "received")
        response = self.client.post(reverse("web_restore_document", args=[first.id]), {"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.document_id, second.id)
        self.assertEqual(ChecklistDocument.objects.filter(checklist_item=item).count(), 1)

    def test_batch_documents_can_be_grouped_as_versions_and_archived_individually(self):
        project = Project.objects.create(contract_number="VERSION-GROUP-001", name="Version group project")
        self.addCleanup(project.delete)
        other_project = Project.objects.create(contract_number="VERSION-GROUP-002", name="Other project")
        self.addCleanup(other_project.delete)
        item = ProjectChecklistItem.objects.create(project=project, title="Технические требования")
        target = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="Технические требования старые.pdf", stored_filename="old-version.pdf",
            working_title="Технические требования", document_number="08-02/3868",
        )
        source = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="Технические требования новые.pdf", stored_filename="new-version.pdf",
            working_title="Технические требования", document_number="08-02/4410",
        )
        foreign = Document.objects.create(
            project=other_project, contract_number=other_project.contract_number, object_name=other_project.name,
            original_filename="foreign.pdf", stored_filename="foreign.pdf",
        )
        token = self.csrf_token()
        response = self.client.post(reverse("web_add_document_version", args=[target.id]), {
            "csrf_token": token, "source_document_id": source.id, "checklist_item_id": item.id,
        }, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 200)
        target.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(target.version_group_id, source.version_group_id)
        self.assertEqual((target.version_order, source.version_order), (1, 2))
        group = DocumentVersionGroup.objects.get(pk=target.version_group_id)
        self.assertEqual(group.checklist_item_id, item.id)
        foreign_merge = self.client.post(reverse("web_add_document_version", args=[target.id]), {
            "csrf_token": token, "source_document_id": foreign.id,
        }, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(foreign_merge.status_code, 404)

        for document in (target, source):
            response = self.client.post(reverse("web_update_document_fields", args=[document.id]), {
                "csrf_token": token, "checklist_item_id": item.id,
                "working_title": "Технические требования", "document_number": document.document_number,
            }, HTTP_X_REQUESTED_WITH="fetch", HTTP_X_PRESERVE_CHECKLIST_LINK="1")
            self.assertEqual(response.status_code, 204)
        self.assertEqual(ChecklistDocument.objects.filter(checklist_item=item).count(), 2)
        item.refresh_from_db()
        self.assertEqual(item.document_id, target.id)

        response = self.client.post(
            reverse("web_archive_document", args=[target.id]), {"csrf_token": token},
            HTTP_X_PRESERVE_CHECKLIST_LINK="1",
        )
        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        source.refresh_from_db()
        item.refresh_from_db()
        self.assertTrue(target.archived_at)
        self.assertFalse(source.archived_at)
        self.assertEqual(item.document_id, source.id)
        self.assertTrue(DocumentVersionGroup.objects.filter(pk=group.pk).exists())

    def test_checklist_picker_suggests_only_items_without_active_documents(self):
        project = Project.objects.create(contract_number="PICKER-AVAILABLE-001", name="Picker project")
        self.addCleanup(project.delete)
        loaded_item = ProjectChecklistItem.objects.create(project=project, title="Письмо о согласовании")
        available_item = ProjectChecklistItem.objects.create(project=project, title="Технические условия")
        active_document = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="loaded.pdf", stored_filename="loaded.pdf", working_title="Письмо о согласовании",
        )
        loaded_item.document = active_document
        loaded_item.status = "received"
        loaded_item.save()
        ChecklistDocument.objects.create(checklist_item=loaded_item, document=active_document)
        parsed = SimpleNamespace(
            working_title="Технические условия", official_title="", raw_text="Технические условия",
        )

        matches, suggested_id = suggest_checklist_items(project.id, parsed)

        loaded_match = next(match for match in matches if match["id"] == loaded_item.id)
        available_match = next(match for match in matches if match["id"] == available_item.id)
        self.assertTrue(loaded_match["has_document"])
        self.assertFalse(available_match["has_document"])
        self.assertEqual(suggested_id, available_item.id)

    def test_saving_cannot_link_to_another_active_checklist_document(self):
        project = Project.objects.create(contract_number="PICKER-GUARD-001", name="Picker guard project")
        self.addCleanup(project.delete)
        item = ProjectChecklistItem.objects.create(project=project, title="Технические требования")
        existing = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="existing.pdf", stored_filename="existing.pdf",
        )
        item.document = existing
        item.status = "received"
        item.save()
        ChecklistDocument.objects.create(checklist_item=item, document=existing)
        incoming = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="incoming.pdf", stored_filename="incoming.pdf",
        )

        response = self.client.post(reverse("web_update_document_fields", args=[incoming.id]), {
            "csrf_token": self.csrf_token(), "checklist_item_id": item.id,
            "working_title": "Технические требования", "document_number": "",
        }, HTTP_X_REQUESTED_WITH="fetch")

        self.assertEqual(response.status_code, 409)
        self.assertIn("только пункты без загруженных документов", response.json()["error"])

    def test_project_document_request_has_linked_document_details(self):
        project = Project.objects.create(contract_number="REQUEST-DETAIL-001", name="Request details")
        self.addCleanup(project.delete)
        document = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="replacement.pdf", stored_filename="replacement.pdf", issuer="Issuer",
            document_number="N-42", issue_date="2026-03-04", note="Заменяет прежнюю редакцию",
        )
        ProjectChecklistItem.objects.create(
            project=project, title="Разрешение", status="received", document=document,
            replacement_required=True,
        )
        response = self.client.get(f"/objects/{project.id}/documents/request?partial=1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Issuer")
        self.assertContains(response, "N-42")
        self.assertContains(response, "Заменяет прежнюю редакцию")

    def test_checklist_upload_analyze_returns_review_json_and_links_document(self):
        token = self.csrf_token()
        project = Project.objects.create(contract_number="CHECKLIST-UPLOAD-001", name="Checklist upload")
        self.addCleanup(project.delete)
        item = ProjectChecklistItem.objects.create(project=project, title="Задание на проектирование")
        stream = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(stream)
        with tempfile.TemporaryDirectory(prefix="django-checklist-upload-") as upload_dir:
            with override_settings(MEDIA_ROOT=upload_dir):
                response = self.client.post(
                    reverse("web_upload_checklist_document", args=[project.id, item.id]),
                    {"csrf_token": token, "files": SimpleUploadedFile("assignment.pdf", stream.getvalue(), content_type="application/pdf")},
                    HTTP_X_IRD_ANALYZE="1",
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertIn("working_title", payload["fields"])
                document = Document.objects.get(pk=payload["id"], project=project)
                self.assertEqual(document.status, "processed")
                item.refresh_from_db()
                self.assertEqual(item.document_id, document.id)
                self.assertTrue(Path(upload_dir, document.stored_filename).is_file())

    def test_native_document_viewer_renders_pages_and_rejects_out_of_range(self):
        project = Project.objects.create(contract_number="VIEWER-001", name="Viewer project")
        self.addCleanup(project.delete)
        with tempfile.TemporaryDirectory(prefix="django-viewer-") as upload_dir:
            stream = io.BytesIO()
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.write(stream)
            Path(upload_dir, "viewer.pdf").write_bytes(stream.getvalue())
            with override_settings(MEDIA_ROOT=upload_dir):
                document = Document.objects.create(
                    project=project, contract_number=project.contract_number, object_name=project.name,
                    original_filename="viewer.pdf", stored_filename="viewer.pdf",
                )
                response = self.client.get(reverse("web_document_viewer", args=[document.id]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
                self.assertContains(response, 'data-page-count="1"')
                response = self.client.get(reverse("web_document_viewer_page", args=[document.id, 0]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/jpeg")
                response = self.client.get(reverse("web_document_viewer_page", args=[document.id, 1]))
                self.assertEqual(response.status_code, 404)

    def test_correspondence_upload_edit_preview_and_delete_use_postgres(self):
        token = self.csrf_token()
        project = Project.objects.create(contract_number="LETTER-001", name="Correspondence project")
        self.addCleanup(project.delete)
        stream = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(stream)
        with tempfile.TemporaryDirectory(prefix="django-correspondence-") as upload_dir:
            with override_settings(MEDIA_ROOT=upload_dir), patch(
                "app.services.pdf_text.PdfTextExtractor.extract", return_value="Исходящий номер 12 от 01.02.2026\nПисьмо"
            ):
                response = self.client.post(
                    reverse("web_upload_correspondence", args=[project.id]),
                    {"csrf_token": token, "record_type": "approval", "direction": "outgoing",
                     "files": SimpleUploadedFile("approval.pdf", stream.getvalue(), content_type="application/pdf")},
                    HTTP_X_CORRESPONDENCE_ANALYZE="1",
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                letter = Correspondence.objects.get(pk=payload["id"], project=project)
                self.assertEqual(letter.record_type, "approval")
                self.assertEqual(letter.direction, "outgoing")
                stored_path = Path(upload_dir, letter.stored_filename)
                self.assertTrue(stored_path.is_file())

                response = self.client.post(
                    reverse("web_update_correspondence", args=[project.id, letter.id]),
                    {"csrf_token": token, "direction": "incoming", "number": "№ 22", "letter_date": "05.03.2026",
                     "sender": "РУП Минскэнерго", "recipient": "Заказчик", "subject": "Согласование",
                     "summary": "Кратко", "valid_until": "2027-01-01"},
                    HTTP_X_REQUESTED_WITH="fetch",
                )
                self.assertEqual(response.status_code, 204)
                letter.refresh_from_db()
                self.assertEqual((letter.direction, letter.number, letter.letter_date), ("incoming", "№ 22", "2026-03-05"))

                preview = self.client.get(reverse("web_correspondence_preview", args=[letter.id]))
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview["Content-Type"], "application/pdf")
                self.assertEqual(preview["X-Frame-Options"], "SAMEORIGIN")

                response = self.client.post(
                    reverse("web_delete_correspondence", args=[project.id, letter.id]),
                    {"csrf_token": token}, HTTP_X_REQUESTED_WITH="fetch",
                )
                self.assertEqual(response.status_code, 204)
                self.assertFalse(Correspondence.objects.filter(pk=letter.id).exists())
                self.assertFalse(stored_path.exists())


@override_settings(SECURE_SSL_REDIRECT=False)
class DjangoRemainingFlowTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        username = f"migration-test-{id(self)}"
        self.user = get_user_model().objects.create_superuser(username=username, email=f"{username}@example.test", password="TestPass123!!")
        self.client.force_login(self.user)
        self.addCleanup(self.user.delete)

    def test_duplicate_detection_matches_legacy_number_and_archived_rules(self):
        project = Project.objects.create(contract_number="DUPLICATE-001", name="Duplicate checks")
        self.addCleanup(project.delete)
        existing = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="existing.pdf", stored_filename="existing.pdf",
            working_title="Технические требования", issuer='РУП «Минскэнерго»',
            document_number="12/34", issue_date="2026-02-01", raw_text="Исходящий № 12/34",
        )
        parsed = ParsedDocument(
            working_title="Технические требования", issuer='РУП «Минскэнерго»',
            document_number="12/34", issue_date="2026-02-01", raw_text="без номера в тексте",
        )
        self.assertEqual(find_duplicate(project.id, parsed).id, existing.id)
        parsed.document_number = "99/99"
        self.assertIsNone(find_duplicate(project.id, parsed))
        parsed.document_number = "12/34"
        Document.objects.filter(pk=existing.pk).update(archived_at="2026-11-01 00:00:00")
        self.assertEqual(find_duplicate(project.id, parsed).id, existing.id)

    @override_settings(MAIL_TIMEZONE="Europe/Minsk", EXPIRY_WARNING_DAYS=30)
    def test_expiry_reminders_use_postgres_once_and_include_checklist_terms(self):
        from datetime import date, timedelta
        from unittest.mock import patch

        project = Project.objects.create(contract_number="EXPIRY-001", name="Expiry reminders")
        self.addCleanup(project.delete)
        expiration = (date.today() + timedelta(days=10)).isoformat()
        Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="expiring.pdf", stored_filename="expiring.pdf",
            working_title="Истекающий документ", issuer="Организация", valid_until=expiration,
        )
        checklist = ProjectChecklistItem.objects.create(
            project=project, title="Изыскания", validity_years=2,
            issue_date=(date.today() - timedelta(days=720)).isoformat(), valid_until=expiration,
        )
        with patch("app.services.email_notifications._send_email") as send_email:
            from app.services.email_notifications import send_expiry_reminders
            send_expiry_reminders()
            send_expiry_reminders()
        self.assertEqual(send_email.call_count, 1)
        message = send_email.call_args.args[0]
        self.assertIn("Истекающий документ", message.get_content())
        self.assertIn("Изыскания", message.get_content())
        self.assertEqual(ExpiryNotification.objects.filter(document__project=project).count(), 1)
        self.assertEqual(ChecklistExpiryNotification.objects.filter(checklist_item=checklist).count(), 1)
    def test_document_request_and_project_acts_are_native_django(self):
        client = self.client
        project = Project.objects.create(contract_number="FLOW-001", name="Проверка документов Django")
        self.addCleanup(project.delete)
        ProjectChecklistItem.objects.create(project=project, title="Недостающий документ")

        response = client.get(f"/objects/{project.id}/documents/request?partial=1")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get("X-Migration-Route"))
        self.assertContains(response, "Недостающий документ")

        response = client.get(reverse("web_project_acts", args=[project.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Запрос недостающих данных")

        response = client.get(reverse("web_object_workspace", args=[project.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get("X-Migration-Route"))

    def test_native_handover_docx_generation_history_download_and_delete(self):
        project = Project.objects.create(contract_number="ACT-001", name="Handover generation")
        self.addCleanup(project.delete)
        document = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="source.pdf", stored_filename="source.pdf",
            working_title="Исходный документ", status="processed",
        )
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get(reverse("web_index")).content.decode()).group(1)
        with tempfile.TemporaryDirectory(prefix="django-acts-") as acts_dir:
            with override_settings(ACTS_DIR=acts_dir):
                response = self.client.post(f"/objects/{project.id}/documents/handover", {"csrf_token": token})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                act = HandoverAct.objects.get(project=project, source="manual")
                stored = Path(acts_dir, str(project.id), act.stored_filename)
                self.assertTrue(stored.is_file())
                self.assertGreater(stored.stat().st_size, 1000)

                response = self.client.get(reverse("web_download_handover_act", args=[project.id, act.id]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(b"".join(response.streaming_content), stored.read_bytes())

                response = self.client.post(reverse("web_delete_handover_act", args=[project.id, act.id]), {"csrf_token": token})
                self.assertEqual(response.status_code, 302)
                self.assertFalse(stored.exists())
                self.assertFalse(HandoverAct.objects.filter(pk=act.id).exists())

    def test_native_project_export_contains_json_files_and_verified_manifest(self):
        project = Project.objects.create(contract_number="EXPORT-001", name="Project export")
        self.addCleanup(project.delete)
        document = Document.objects.create(
            project=project, contract_number=project.contract_number, object_name=project.name,
            original_filename="source.pdf", stored_filename="export-source.pdf",
            working_title="Исходный документ",
        )
        item = ProjectChecklistItem.objects.create(project=project, title="Документ")
        ChecklistDocument.objects.create(checklist_item=item, document=document)
        with tempfile.TemporaryDirectory(prefix="django-export-") as upload_dir:
            Path(upload_dir, "export-source.pdf").write_bytes(b"%PDF-1.4 test")
            with override_settings(MEDIA_ROOT=upload_dir):
                response = self.client.get(reverse("web_export_object", args=[project.id]))
                self.assertEqual(response.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content))) as archive:
                    names = set(archive.namelist())
                    self.assertTrue({
                        "Проект.json", "Перечень_ИРД.json", "Переписка.json",
                        "Согласования.json", "Комплектность.json", "Акты_приёма-передачи.json",
                        "manifest.json",
                    }.issubset(names))
                    self.assertTrue(any(name.startswith("01_ИРД/") for name in names))
                    manifest = json.loads(archive.read("manifest.json"))
                    document_entry = next(entry for entry in manifest["files"] if entry.get("document_id") == document.id)
                    content = archive.read(document_entry["path"])
                    self.assertEqual(document_entry["sha256"], hashlib.sha256(content).hexdigest())
                    exported_checklist = json.loads(archive.read("Комплектность.json"))
                    self.assertEqual(exported_checklist[0]["document_ids"], [document.id])

    def test_django_worker_processes_queue_without_flask_context(self):
        project = Project.objects.create(contract_number="WORKER-001", name="Worker test")
        self.addCleanup(project.delete)
        item = ProjectChecklistItem.objects.create(project=project, title="Справка")
        with tempfile.TemporaryDirectory(prefix="django-worker-") as upload_dir:
            source = Path(upload_dir, "queued.pdf")
            source.write_bytes(b"queued file")
            with override_settings(MEDIA_ROOT=upload_dir):
                document = Document.objects.create(
                    project=project, contract_number=project.contract_number, object_name=project.name,
                    original_filename="queued.pdf", stored_filename="queued.pdf", status="queued",
                )
                parsed = ParsedDocument(
                    working_title="Справка", issuer="Организация", issue_date="2026-01-01",
                    raw_text="OCR text",
                )
                with patch("app.services.document_jobs.DocumentProcessingPipeline") as pipeline_class:
                    pipeline_class.return_value.process.return_value = parsed
                    from app.services.document_jobs import run_pending_document_jobs
                    self.assertEqual(run_pending_document_jobs(limit=1), 1)
                document.refresh_from_db()
                item.refresh_from_db()
                self.assertEqual(document.status, "processed")
                self.assertEqual(document.raw_text, "OCR text")
                self.assertEqual(item.document_id, document.id)
                self.assertEqual(item.status, "received")

    @override_settings(MEDIA_ROOT="/tmp/nhp-ird-django-test-uploads")
    def test_legacy_project_delete_cleanup_use_postgres(self):
        with tempfile.TemporaryDirectory(prefix="nhp-ird-django-test-") as upload_dir:
            from django.conf import settings
            settings.MEDIA_ROOT = upload_dir
            client = self.client
            home = client.get(reverse("web_index"))
            token = re.search(r'name="csrf_token" value="([^"]+)"', home.content.decode()).group(1)
            project = Project.objects.create(contract_number="UPLOAD-001", name="Загрузка через Django")

            document = Document.objects.create(
                project=project, contract_number=project.contract_number, object_name=project.name,
                original_filename="migration-smoke.pdf", stored_filename="migration-smoke.pdf",
            )
            stored_path = Path(upload_dir, document.stored_filename)
            stored_path.write_bytes(b"test")

            response = client.post(reverse("web_delete_object", args=[project.id]), {"csrf_token": token})
            self.assertEqual(response.status_code, 302)
            self.assertFalse(Project.objects.filter(pk=project.id).exists())
            self.assertFalse(Document.objects.filter(pk=document.id).exists())
            self.assertFalse(stored_path.exists())


@override_settings(SECURE_SSL_REDIRECT=False)
class GuestProjectAccessTests(TransactionTestCase):
    def setUp(self):
        username = f"guest-scope-{id(self)}"
        self.user = get_user_model().objects.create_user(
            username=username, email=f"{username}@example.test", password="TestPass123!!",
        )
        self.profile, _ = UserProfile.objects.get_or_create(user=self.user)
        self.profile.access_level = UserProfile.AccessLevel.VIEW
        self.profile.save(update_fields=["access_level"])
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)
        self.addCleanup(self.user.delete)

    def test_guest_customer_and_individual_project_scope_is_enforced_on_pages_and_direct_urls(self):
        customer = Customer.objects.create(name="Allowed customer")
        allowed_by_customer = Project.objects.create(
            contract_number="GUEST-CUSTOMER-1", name="Customer project one", customer=customer.name,
        )
        allowed_by_customer_2 = Project.objects.create(
            contract_number="GUEST-CUSTOMER-2", name="Customer project two", customer=customer.name,
        )
        explicitly_allowed = Project.objects.create(
            contract_number="GUEST-PROJECT", name="Explicit project", customer="Other customer",
        )
        forbidden = Project.objects.create(
            contract_number="GUEST-FORBIDDEN", name="Private project", customer="Other customer",
        )
        self.profile.allowed_customers.add(customer)
        self.profile.allowed_projects.add(explicitly_allowed)
        organization = Organization.objects.create(name="Employer organization")
        organization_project = Project.objects.create(
            contract_number="GUEST-ORG", name="Employer organization project", customer="Employer customer",
        )
        self.profile.organization = organization
        self.profile.save(update_fields=["organization"])
        forbidden_document = Document.objects.create(
            project=forbidden, contract_number=forbidden.contract_number, object_name=forbidden.name,
            original_filename="private.pdf", stored_filename="private.pdf",
        )

        home = self.client.get(reverse("web_index"))
        self.assertContains(home, "Customer project one")
        self.assertContains(home, "Customer project two")
        self.assertContains(home, "Explicit project")
        self.assertNotContains(home, "Employer organization project")
        self.assertNotContains(home, "Private project")
        self.assertEqual(self.client.get(reverse("web_object_workspace", args=[allowed_by_customer.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("web_object_workspace", args=[allowed_by_customer_2.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("web_object_workspace", args=[explicitly_allowed.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("web_object_workspace", args=[organization_project.id])).status_code, 403)
        self.assertEqual(self.client.get(reverse("web_object_workspace", args=[forbidden.id])).status_code, 403)
        self.assertEqual(self.client.get(reverse("web_document_detail", args=[forbidden_document.id])).status_code, 403)
        self.assertEqual(self.client.post(reverse("web_save_notebook_entry", args=[forbidden.id]), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse("web_save_notebook_entry", args=[explicitly_allowed.id]), {}).status_code, 403)

    def test_directory_edit_prefills_record_and_deletion_protects_referenced_entries(self):
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        customer = Customer.objects.create(name="Customer to edit", is_active=False)
        response = self.client.get(reverse("admin_organizations"), {"edit_customer": customer.id})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'value="{customer.name}"')

        response = self.client.post(reverse("admin_organizations"), {
            "kind": "customer", "id": customer.id, "name": "Updated customer", "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        customer.refresh_from_db()
        self.assertEqual(customer.name, "Updated customer")
        self.assertTrue(customer.is_active)

        organization = Organization.objects.create(name="Standalone workplace")
        response = self.client.get(reverse("admin_organizations"), {"edit_org": organization.id})
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("admin_organizations"), {
            "kind": "organization", "id": organization.id, "name": organization.name,
            "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)

        project = Project.objects.create(contract_number="REFERENCED", name="Referenced", customer=customer.name)
        response = self.client.post(reverse("admin_organizations"), {"kind": "delete_customer", "id": customer.id})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Customer.objects.filter(pk=customer.id).exists())

        unused = Customer.objects.create(name="Unused customer")
        response = self.client.post(reverse("admin_organizations"), {"kind": "delete_customer", "id": unused.id})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Customer.objects.filter(pk=unused.id).exists())

        organization = Organization.objects.create(name="Assigned organization")
        self.profile.organization = organization
        self.profile.save(update_fields=["organization"])
        response = self.client.post(reverse("admin_organizations"), {"kind": "delete_organization", "id": organization.id})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Organization.objects.filter(pk=organization.id).exists())
        self.profile.organization = None
        self.profile.save(update_fields=["organization"])
        response = self.client.post(reverse("admin_organizations"), {"kind": "delete_organization", "id": organization.id})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Organization.objects.filter(pk=organization.id).exists())
