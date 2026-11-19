from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("migration_portal", "0010_project_chief_engineer"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="organization",
            name="customers",
        ),
    ]
