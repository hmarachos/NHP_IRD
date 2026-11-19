from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("migration_portal", "0012_document_processing_lease"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="show_ai_analytics",
            field=models.BooleanField(default=True),
        ),
    ]
