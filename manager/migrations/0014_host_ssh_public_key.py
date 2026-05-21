from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manager', '0013_remove_django_cryptography'),
    ]

    operations = [
        migrations.AlterField(
            model_name='host',
            name='password',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='host',
            name='ssh_public_key',
            field=models.TextField(blank=True, default=''),
        ),
    ]
