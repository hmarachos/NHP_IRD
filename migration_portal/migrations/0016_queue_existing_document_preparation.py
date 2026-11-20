from django.db import migrations


def queue_existing(apps, schema_editor):
    Prepared = apps.get_model("migration_portal", "PreparedDocument")
    database = schema_editor.connection.alias
    for name, field in (("Document", "document_id"), ("Correspondence", "correspondence_id")):
        Source = apps.get_model("migration_portal", name)
        batch = []
        for source_id in Source.objects.using(database).exclude(raw_text__isnull=True).exclude(raw_text="").values_list("pk", flat=True).iterator(chunk_size=500):
            batch.append(Prepared(**{field: source_id}))
            if len(batch) == 500:
                Prepared.objects.using(database).bulk_create(batch, ignore_conflicts=True)
                batch.clear()
        if batch:
            Prepared.objects.using(database).bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("migration_portal", "0015_prepareddocument_documentchunk_and_more")]
    operations = [migrations.RunPython(queue_existing, migrations.RunPython.noop)]
