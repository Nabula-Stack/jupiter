from django.db import migrations

import manager.fields


def encrypt_existing_ssh_public_keys(apps, schema_editor):
    Host = apps.get_model('manager', 'Host')
    for host in Host.objects.exclude(ssh_public_key='').iterator():
        value = host.ssh_public_key or ''
        if isinstance(value, str) and value.startswith('enc::'):
            continue
        # Triggers field preparation so plaintext rows are rewritten encrypted.
        host.save(update_fields=['ssh_public_key'])


class Migration(migrations.Migration):

    dependencies = [
        ('manager', '0015_host_network_data_host_storage_data'),
    ]

    operations = [
        migrations.AlterField(
            model_name='host',
            name='ssh_public_key',
            field=manager.fields.EncryptedTextField(blank=True, default=''),
        ),
        migrations.RunPython(encrypt_existing_ssh_public_keys, migrations.RunPython.noop),
    ]
