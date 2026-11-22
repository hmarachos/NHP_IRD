"""Django-owned PostgreSQL schema corresponding to the legacy IRD records."""
from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import Cast, Now
import uuid
from app.services.organization_names import normalize_user_text


TIMESTAMP_TEXT_DEFAULT = Cast(Now(), output_field=models.CharField(max_length=40))


class NormalizedUserTextMixin:
    normalized_text_fields: tuple[str, ...] = ()

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        fields = self.normalized_text_fields if update_fields is None else set(self.normalized_text_fields) & set(update_fields)
        for field_name in fields:
            setattr(self, field_name, normalize_user_text(getattr(self, field_name)))
        return super().save(*args, **kwargs)


class ChecklistTemplate(NormalizedUserTextMixin, models.Model):
    normalized_text_fields = ("name", "description")

    id = models.AutoField(primary_key=True)
    name = models.TextField(unique=True)
    description = models.TextField(default="", db_default="")
    created_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)
    updated_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "checklist_templates"


class Project(NormalizedUserTextMixin, models.Model):
    normalized_text_fields = ("contract_number", "stage", "name", "customer")

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


class Document(NormalizedUserTextMixin, models.Model):
    normalized_text_fields = (
        "contract_number", "object_name", "working_title", "official_title", "issuer", "document_number", "note",
    )

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

    def save(self, *args, **kwargs):
        # Persist the preparation job and cache invalidation with the card.
        with transaction.atomic():
            return super().save(*args, **kwargs)

    class Meta:
        db_table = "documents"
        indexes = [
            models.Index(fields=["project", "created_at"], name="document_project_date_idx"),
            models.Index(fields=["valid_until"], name="document_expiry_idx"),
        ]


class Correspondence(NormalizedUserTextMixin, models.Model):
    normalized_text_fields = ("number", "sender", "recipient", "subject", "summary")

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

    def save(self, *args, **kwargs):
        # Persist the preparation job and cache invalidation with the card.
        with transaction.atomic():
            return super().save(*args, **kwargs)

    class Meta:
        db_table = "correspondence"
        constraints = [models.CheckConstraint(condition=Q(direction__in=["incoming", "outgoing"]), name="correspondence_direction_valid")]
        indexes = [models.Index(fields=["project", "letter_date", "id"], name="correspond_project_date_idx")]


class ProjectNotebookEntry(NormalizedUserTextMixin, models.Model):
    normalized_text_fields = ("title",)

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


class ChecklistTemplateItem(NormalizedUserTextMixin, models.Model):
    normalized_text_fields = ("title", "description")

    id = models.AutoField(primary_key=True)
    template = models.ForeignKey(ChecklistTemplate, on_delete=models.CASCADE, db_column="template_id")
    title = models.TextField()
    description = models.TextField(default="", db_default="")
    sort_order = models.IntegerField(default=0, db_default=0)
    validity_years = models.IntegerField(default=0, db_default=0)

    class Meta:
        db_table = "checklist_template_items"
        ordering = ["sort_order", "id"]


class ProjectChecklistItem(NormalizedUserTextMixin, models.Model):
    normalized_text_fields = ("title", "description", "note")

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


class PendingExpiryNotification(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE)
    valid_until = models.CharField(max_length=10)
    next_attempt_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "valid_until"], name="pending_expiry_document_unique")]


class ChecklistExpiryNotification(models.Model):
    id = models.AutoField(primary_key=True)
    checklist_item = models.ForeignKey(ProjectChecklistItem, on_delete=models.CASCADE, db_column="checklist_item_id")
    valid_until = models.TextField()
    sent_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "checklist_expiry_notifications"
        constraints = [models.UniqueConstraint(fields=["checklist_item", "valid_until"], name="checklist_expiry_unique")]


class ProjectSearchRevision(models.Model):
    project = models.OneToOneField(Project, primary_key=True, on_delete=models.CASCADE, db_column="project_id")
    revision = models.UUIDField(default=uuid.uuid4, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "project_search_revisions"


class ProjectAnswerCache(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, db_column="project_id")
    question_hash = models.CharField(max_length=64)
    normalized_question = models.TextField()
    source_revision = models.UUIDField()
    model_name = models.CharField(max_length=120)
    prompt_version = models.PositiveSmallIntegerField(default=1)
    answer = models.TextField()
    sources = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "project_answer_cache"
        constraints = [models.UniqueConstraint(fields=["project", "question_hash"], name="qa_cache_project_question_uq")]
        indexes = [models.Index(fields=["project", "expires_at"], name="qa_cache_project_exp_idx")]


class LegacyImportState(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    source_path = models.TextField()
    source_sha256 = models.CharField(max_length=64)
    imported_at = models.CharField(max_length=40, default="", db_default=TIMESTAMP_TEXT_DEFAULT)

    class Meta:
        db_table = "migration_import_state"


class Organization(NormalizedUserTextMixin, models.Model):
    normalized_text_fields = ("name",)

    name = models.CharField(max_length=240, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Customer(NormalizedUserTextMixin, models.Model):
    normalized_text_fields = ("name",)

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
    show_ai_analytics = models.BooleanField(default=True)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.PROTECT, related_name="users")
    access_level = models.CharField(max_length=12, choices=AccessLevel.choices, default=AccessLevel.BLOCKED)
    allowed_customers = models.ManyToManyField(Customer, blank=True, related_name="permitted_users")
    allowed_projects = models.ManyToManyField(Project, blank=True, related_name="permitted_users")

    def __str__(self):
        return f"{self.user.get_full_name()} ({self.get_access_level_display()})"


class PreparedDocument(models.Model):
    """Durable preparation job and atomically published search representation."""
    document = models.OneToOneField(Document, null=True, blank=True, on_delete=models.CASCADE, related_name="prepared")
    correspondence = models.OneToOneField(Correspondence, null=True, blank=True, on_delete=models.CASCADE, related_name="prepared")
    normalized_text = models.TextField(default="")
    page_mapping = models.CharField(max_length=16, default="unavailable")
    metadata = models.JSONField(default=dict)
    input_hash = models.CharField(max_length=64, default="")
    text_hash = models.CharField(max_length=64, default="")
    requested_revision = models.UUIDField(default=uuid.uuid4, editable=False)
    pipeline_version = models.PositiveIntegerField(default=0)
    revision = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, default="pending", db_index=True)
    error = models.TextField(default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=(Q(document__isnull=False, correspondence__isnull=True) | Q(document__isnull=True, correspondence__isnull=False)),
            name="prepared_exactly_one_source",
        )]


class DocumentChunk(models.Model):
    prepared = models.ForeignKey(PreparedDocument, on_delete=models.CASCADE, related_name="chunks")
    revision = models.PositiveIntegerField()
    position = models.PositiveIntegerField()
    page = models.PositiveIntegerField(null=True)
    text = models.TextField()

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["prepared", "position"], name="chunk_source_position_uq")]
