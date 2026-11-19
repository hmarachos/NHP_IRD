from django.contrib import admin

from migration_portal import models


@admin.register(models.Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_at")
    search_fields = ("name",)
    list_filter = ("is_active",)


@admin.register(models.Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)
    list_filter = ("is_active",)


@admin.register(models.UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "access_level", "is_gip")
    search_fields = ("user__email", "user__first_name", "user__last_name", "middle_name")
    list_filter = ("access_level", "is_gip", "organization")
    filter_horizontal = ("allowed_customers", "allowed_projects")


@admin.register(models.Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "contract_number", "name", "customer", "chief_engineer", "archived_at")
    search_fields = ("contract_number", "name", "customer")
    list_filter = ("archived_at",)


@admin.register(models.Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "working_title", "document_number", "status", "archived_at")
    search_fields = ("contract_number", "object_name", "working_title", "official_title", "document_number", "issuer")
    list_filter = ("status", "archived_at", "no_expiration")


@admin.register(models.Correspondence)
class CorrespondenceAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "record_type", "direction", "number", "letter_date", "subject")
    search_fields = ("number", "sender", "recipient", "subject", "summary")
    list_filter = ("record_type", "direction", "status")


@admin.register(models.ChecklistTemplate)
class ChecklistTemplateAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(models.ChecklistTemplateItem)
class ChecklistTemplateItemAdmin(admin.ModelAdmin):
    list_display = ("id", "template", "title", "sort_order", "validity_years")
    search_fields = ("title", "template__name")
    list_filter = ("template",)


@admin.register(models.ProjectChecklistItem)
class ProjectChecklistItemAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "title", "status", "validity_years", "valid_until")
    search_fields = ("project__contract_number", "project__name", "title")
    list_filter = ("status", "validity_years")


@admin.register(models.ChecklistDocument)
class ChecklistDocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "checklist_item", "document", "sort_order")


@admin.register(models.HandoverAct)
class HandoverActAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "period_start", "period_end", "source", "sent_at")
    list_filter = ("source",)


@admin.register(models.AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value")
    search_fields = ("key", "value")


@admin.register(models.ExpiryNotification)
class ExpiryNotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "notification_type", "sent_at")


@admin.register(models.ChecklistExpiryNotification)
class ChecklistExpiryNotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "checklist_item", "valid_until", "sent_at")
