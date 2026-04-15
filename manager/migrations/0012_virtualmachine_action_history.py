# Generated migration for action_history field

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manager', '0011_virtualmachine_dns_servers'),
    ]

    operations = [
        migrations.AddField(
            model_name='virtualmachine',
            name='action_history',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
