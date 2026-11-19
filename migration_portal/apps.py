from django.apps import AppConfig

class MigrationPortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "migration_portal"

    def ready(self):
        from migration_portal import signals  # noqa: F401
