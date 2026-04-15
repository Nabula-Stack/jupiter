# Generated migration to remove django_cryptography dependency
# This migration converts the encrypted password field to a regular CharField

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manager', '0012_virtualmachine_action_history'),
    ]

    operations = [
        migrations.AlterField(
            model_name='host',
            name='password',
            field=models.CharField(max_length=255),
        ),
    ]
