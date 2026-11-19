from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("migration_portal", "0004_projectnotebookentry")]

    operations = [
        migrations.AddField(
            model_name="project",
            name="stage",
            field=models.TextField(db_default="", default=""),
        ),
    ]
