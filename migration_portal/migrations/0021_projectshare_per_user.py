from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("migration_portal", "0020_project_share"),
    ]

    operations = [
        migrations.AlterField(
            model_name="projectshare",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="shares",
                to="migration_portal.project",
            ),
        ),
    ]
