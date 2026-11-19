"""Invalidate project QA answers whenever searchable project data changes."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from app.services.project_answer_cache import invalidate_project_answer_cache
from migration_portal.models import (
    ChecklistDocument,
    Correspondence,
    Document,
    ProjectChecklistItem,
)


@receiver((post_save, post_delete), sender=Document, dispatch_uid="project_qa_document_changed")
def document_changed(sender, instance, **kwargs):
    invalidate_project_answer_cache(instance.project_id)


@receiver((post_save, post_delete), sender=Correspondence, dispatch_uid="project_qa_correspondence_changed")
def correspondence_changed(sender, instance, **kwargs):
    invalidate_project_answer_cache(instance.project_id)


@receiver((post_save, post_delete), sender=ProjectChecklistItem, dispatch_uid="project_qa_checklist_changed")
def checklist_item_changed(sender, instance, **kwargs):
    invalidate_project_answer_cache(instance.project_id)


def _linked_project_id(instance):
    project_id = ProjectChecklistItem.objects.filter(
        pk=instance.checklist_item_id,
    ).values_list("project_id", flat=True).first()
    if project_id:
        return project_id
    return Document.objects.filter(
        pk=instance.document_id,
    ).values_list("project_id", flat=True).first()


@receiver((post_save, post_delete), sender=ChecklistDocument, dispatch_uid="project_qa_checklist_document_changed")
def checklist_document_changed(sender, instance, **kwargs):
    invalidate_project_answer_cache(_linked_project_id(instance))
