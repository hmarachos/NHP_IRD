from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("migration_portal", "0017_prepareddocument_page_mapping")]
    operations = [
        migrations.CreateModel(
            name="PendingExpiryNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("valid_until", models.CharField(max_length=10)),
                ("next_attempt_at", models.DateTimeField()),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="migration_portal.document")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("document", "valid_until"), name="pending_expiry_document_unique")]},
        ),
    ]
