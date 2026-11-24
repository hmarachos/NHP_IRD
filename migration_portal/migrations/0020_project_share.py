from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("migration_portal", "0019_document_version_groups"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectShare",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_digest", models.CharField(max_length=64, unique=True)),
                ("password_hash", models.CharField(max_length=128)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ("project", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="share", to="migration_portal.project")),
            ],
            options={"db_table": "project_shares"},
        ),
        migrations.AddIndex(
            model_name="projectshare",
            index=models.Index(fields=["expires_at", "revoked_at"], name="project_share_active_idx"),
        ),
    ]
