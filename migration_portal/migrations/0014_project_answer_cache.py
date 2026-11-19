import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("migration_portal", "0013_userprofile_show_ai_analytics"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectSearchRevision",
            fields=[
                ("project", models.OneToOneField(db_column="project_id", on_delete=django.db.models.deletion.CASCADE, primary_key=True, serialize=False, to="migration_portal.project")),
                ("revision", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "project_search_revisions"},
        ),
        migrations.CreateModel(
            name="ProjectAnswerCache",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("question_hash", models.CharField(max_length=64)),
                ("normalized_question", models.TextField()),
                ("source_revision", models.UUIDField()),
                ("model_name", models.CharField(max_length=120)),
                ("prompt_version", models.PositiveSmallIntegerField(default=1)),
                ("answer", models.TextField()),
                ("sources", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("project", models.ForeignKey(db_column="project_id", on_delete=django.db.models.deletion.CASCADE, to="migration_portal.project")),
            ],
            options={"db_table": "project_answer_cache"},
        ),
        migrations.AddConstraint(
            model_name="projectanswercache",
            constraint=models.UniqueConstraint(fields=("project", "question_hash"), name="qa_cache_project_question_uq"),
        ),
        migrations.AddIndex(
            model_name="projectanswercache",
            index=models.Index(fields=["project", "expires_at"], name="qa_cache_project_exp_idx"),
        ),
    ]
