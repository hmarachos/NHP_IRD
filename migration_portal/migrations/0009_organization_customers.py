from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("migration_portal", "0008_customer_organization_userprofile"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="customers",
            field=models.ManyToManyField(blank=True, related_name="organizations", to="migration_portal.customer"),
        ),
    ]
