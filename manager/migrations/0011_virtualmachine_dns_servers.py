# Generated migration for dns_servers field

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manager', '0010_virtualmachine_dns_name_virtualmachine_tools_running'),
    ]

    operations = [
        migrations.AddField(
            model_name='virtualmachine',
            name='dns_servers',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
