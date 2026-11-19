from django.db import migrations

from migration_portal.full_catalog_notes import ITEM_NOTES, TEMPLATE_DESCRIPTION, TEMPLATE_NAME


def apply_notes(apps, schema_editor):
    Template = apps.get_model("migration_portal", "ChecklistTemplate")
    Item = apps.get_model("migration_portal", "ChecklistTemplateItem")
    template = Template.objects.filter(name=TEMPLATE_NAME).first()
    if template is None:
        return

    template.description = TEMPLATE_DESCRIPTION
    template.save(update_fields=["description"])
    for sort_order, description in ITEM_NOTES.items():
        Item.objects.filter(template_id=template.pk, sort_order=sort_order).update(
            description=description
        )


class Migration(migrations.Migration):
    dependencies = [("migration_portal", "0006_preproject_input_template")]
    operations = [migrations.RunPython(apply_notes, migrations.RunPython.noop)]
