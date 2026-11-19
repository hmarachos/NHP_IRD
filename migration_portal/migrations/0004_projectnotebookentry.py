from django.db import migrations, models
import django.db.models.deletion
import django.db.models.functions.comparison
import django.db.models.functions.datetime


class Migration(migrations.Migration):
    dependencies = [("migration_portal", "0003_legacyimportstate")]

    operations = [
        migrations.CreateModel(
            name="ProjectNotebookEntry",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("title", models.TextField(db_default="", default="")),
                ("content", models.TextField(db_default="", default="")),
                ("created_at", models.CharField(db_default=django.db.models.functions.comparison.Cast(django.db.models.functions.datetime.Now(), output_field=models.CharField(max_length=40)), default="", max_length=40)),
                ("updated_at", models.CharField(db_default=django.db.models.functions.comparison.Cast(django.db.models.functions.datetime.Now(), output_field=models.CharField(max_length=40)), default="", max_length=40)),
                ("project", models.ForeignKey(db_column="project_id", on_delete=django.db.models.deletion.CASCADE, to="migration_portal.project")),
            ],
            options={"db_table": "project_notebook_entries", "ordering": ["-updated_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="projectnotebookentry",
            index=models.Index(fields=["project", "updated_at"], name="notebook_project_date_idx"),
        ),
    ]
