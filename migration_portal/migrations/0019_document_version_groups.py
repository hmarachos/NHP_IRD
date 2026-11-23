from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("migration_portal", "0018_pending_expiry_notification"),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentVersionGroup",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("created_at", models.CharField(db_default=django.db.models.functions.comparison.Cast(django.db.models.functions.datetime.Now(), output_field=models.CharField(max_length=40)), default="", max_length=40)),
                ("checklist_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="document_version_groups", to="migration_portal.projectchecklistitem")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="document_version_groups", to="migration_portal.project")),
            ],
            options={"db_table": "document_version_groups"},
        ),
        migrations.AddField(
            model_name="document",
            name="version_group",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="versions", to="migration_portal.documentversiongroup"),
        ),
        migrations.AddField(
            model_name="document",
            name="version_order",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AddIndex(
            model_name="document",
            index=models.Index(fields=["version_group", "version_order"], name="document_version_order_idx"),
        ),
    ]
