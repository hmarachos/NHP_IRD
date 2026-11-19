"""Django-owned PostgreSQL schema corresponding to the legacy IRD records."""
from django.db import models
from django.db.models import Q
from django.db.models.functions import Cast, Now


TIMESTAMP_TEXT_DEFAULT = Cast(Now(), output_field=models.CharField(max_length=40))


class ChecklistTemplate(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.TextField(unique=True)
    description = models.TextField(default="", db_default="")
    created_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)
    updated_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "checklist_templates"


class Project(models.Model):
    id = models.AutoField(primary_key=True)
    contract_number = models.TextField()
    contract_date = models.CharField(max_length=40, default="", db_default="")
    stage = models.TextField(default="", db_default="")
    name = models.TextField()
    customer = models.TextField(default="", db_default="")
    template = models.ForeignKey(ChecklistTemplate, null=True, blank=True, on_delete=models.SET_NULL, db_column="template_id")
    chief_engineer = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="chief_engineer_projects",
    )
    archived_at = models.CharField(max_length=40, null=True, blank=True)
    created_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "objects"
        indexes = [models.Index(fields=["created_at", "id"], name="project_created_idx")]


class Document(models.Model):
    id = models.AutoField(primary_key=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, db_column="object_id")
    contract_number = models.TextField()
    object_name = models.TextField()
    original_filename = models.TextField()
    stored_filename = models.TextField()
    mime_type = models.TextField(default="application/pdf", db_default="application/pdf")
    working_title = models.TextField(null=True, blank=True)
    official_title = models.TextField(null=True, blank=True)
    issuer = models.TextField(null=True, blank=True)
    issue_date = models.TextField(null=True, blank=True)
    valid_until = models.TextField(null=True, blank=True)
    validity_text = models.TextField(null=True, blank=True)
    no_expiration = models.BooleanField(default=False, db_default=False)
    raw_text = models.TextField(null=True, blank=True)
    note = models.TextField(default="", db_default="")
    status = models.TextField(default="processed", db_default="processed")
    error = models.TextField(null=True, blank=True)
    created_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)
    processing_token = models.CharField(max_length=32, default="", blank=True)
    processing_lease_until = models.DateTimeField(null=True, blank=True)
    document_number = models.TextField(default="", db_default="")
    archived_at = models.CharField(max_length=40, null=True, blank=True)

    class Meta:
        db_table = "documents"
        indexes = [
            models.Index(fields=["project", "created_at"], name="document_project_date_idx"),
            models.Index(fields=["valid_until"], name="document_expiry_idx"),
        ]


class Correspondence(models.Model):
    id = models.AutoField(primary_key=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, db_column="object_id")
    direction = models.TextField()
    number = models.TextField(default="", db_default="")
    letter_date = models.TextField(default="", db_default="")
    sender = models.TextField(default="", db_default="")
    recipient = models.TextField(default="", db_default="")
    subject = models.TextField(default="", db_default="")
    summary = models.TextField(default="", db_default="")
    original_filename = models.TextField(default="", db_default="")
    stored_filename = models.TextField(default="", db_default="")
    mime_type = models.TextField(default="application/pdf", db_default="application/pdf")
    raw_text = models.TextField(default="", db_default="")
    status = models.TextField(default="processed", db_default="processed")
    error = models.TextField(default="", db_default="")
    record_type = models.TextField(default="correspondence", db_default="correspondence")
    valid_until = models.TextField(default="", db_default="")
    created_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "correspondence"
        constraints = [models.CheckConstraint(condition=Q(direction__in=["incoming", "outgoing"]), name="correspondence_direction_valid")]
        indexes = [models.Index(fields=["project", "letter_date", "id"], name="correspond_project_date_idx")]


class ProjectNotebookEntry(models.Model):
    id = models.AutoField(primary_key=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, db_column="project_id")
    title = models.TextField(default="", db_default="")
    content = models.TextField(default="", db_default="")
    created_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)
    updated_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "project_notebook_entries"
        ordering = ["-updated_at", "-id"]
        indexes = [models.Index(fields=["project", "updated_at"], name="notebook_project_date_idx")]


class ChecklistTemplateItem(models.Model):
    id = models.AutoField(primary_key=True)
    template = models.ForeignKey(ChecklistTemplate, on_delete=models.CASCADE, db_column="template_id")
    title = models.TextField()
    description = models.TextField(default="", db_default="")
    sort_order = models.IntegerField(default=0, db_default=0)
    validity_years = models.IntegerField(default=0, db_default=0)

    class Meta:
        db_table = "checklist_template_items"
        ordering = ["sort_order", "id"]


class ProjectChecklistItem(models.Model):
    id = models.AutoField(primary_key=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, db_column="object_id")
    source_template_item = models.ForeignKey(ChecklistTemplateItem, null=True, blank=True, on_delete=models.SET_NULL, db_column="source_template_item_id")
    title = models.TextField()
    description = models.TextField(default="", db_default="")
    sort_order = models.IntegerField(default=0, db_default=0)
    status = models.TextField(default="missing", db_default="missing")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.SET_NULL, db_column="document_id")
    validity_years = models.IntegerField(default=0, db_default=0)
    issue_date = models.TextField(default="", db_default="")
    valid_until = models.TextField(default="", db_default="")
    note = models.TextField(default="", db_default="")
    replacement_required = models.BooleanField(default=False, db_default=False)
    created_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)
    updated_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "project_checklist_items"
        constraints = [models.CheckConstraint(condition=Q(status__in=["missing", "received", "not_required"]), name="project_checklist_status_valid")]
        ordering = ["sort_order", "id"]
        indexes = [models.Index(fields=["project", "sort_order", "id"], name="project_checklist_order_idx")]


class ChecklistDocument(models.Model):
    id = models.BigAutoField(primary_key=True)
    checklist_item = models.ForeignKey(ProjectChecklistItem, on_delete=models.CASCADE, db_column="checklist_item_id")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, db_column="document_id")
    sort_order = models.IntegerField(default=0, db_default=0)
    created_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "project_checklist_documents"
        constraints = [models.UniqueConstraint(fields=["checklist_item", "document"], name="checklist_document_pair_unique")]


class AppSetting(models.Model):
    key = models.TextField(primary_key=True)
    value = models.TextField()

    class Meta:
        db_table = "app_settings"


class HandoverAct(models.Model):
    id = models.AutoField(primary_key=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, db_column="object_id")
    period_start = models.TextField()
    period_end = models.TextField()
    stored_filename = models.TextField()
    source = models.TextField(default="automatic", db_default="automatic")
    created_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)
    sent_at = models.CharField(max_length=40, null=True, blank=True)
    email_error = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "handover_acts"
        constraints = [models.UniqueConstraint(fields=["project"], condition=Q(sent_at__isnull=True, source="automatic"), name="handover_pending_project_unique")]


class ExpiryNotification(models.Model):
    id = models.AutoField(primary_key=True)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, db_column="document_id")
    notification_type = models.TextField()
    sent_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "expiry_notifications"
        constraints = [models.UniqueConstraint(fields=["document", "notification_type"], name="expiry_document_type_unique")]


class ChecklistExpiryNotification(models.Model):
    id = models.AutoField(primary_key=True)
    checklist_item = models.ForeignKey(ProjectChecklistItem, on_delete=models.CASCADE, db_column="checklist_item_id")
    valid_until = models.TextField()
    sent_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "checklist_expiry_notifications"
        constraints = [models.UniqueConstraint(fields=["checklist_item", "valid_until"], name="checklist_expiry_unique")]


class LegacyImportState(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    source_path = models.TextField()
    source_sha256 = models.CharField(max_length=64)
    imported_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "migration_import_state"


class Organization(models.Model):
    name = models.CharField(max_length=240, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Customer(models.Model):
    name = models.CharField(max_length=240, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    class AccessLevel(models.TextChoices):
        BLOCKED = "blocked", "Заблокирован"
        VIEW = "view", "Просмотр"
        EDIT = "edit", "Редактирование"

    user = models.OneToOneField("auth.User", on_delete=models.CASCADE, related_name="profile")
    middle_name = models.CharField(max_length=150, blank=True)
    is_gip = models.BooleanField(default=False)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.PROTECT, related_name="users")
    access_level = models.CharField(max_length=12, choices=AccessLevel.choices, default=AccessLevel.BLOCKED)
    allowed_customers = models.ManyToManyField(Customer, blank=True, related_name="permitted_users")
    allowed_projects = models.ManyToManyField(Project, blank=True, related_name="permitted_users")

    def __str__(self):
        return f"{self.user.get_full_name()} ({self.get_access_level_display()})"
