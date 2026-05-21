from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manager", "0017_host_hypervisor_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="host",
            name="esxi_connection_method",
            field=models.CharField(
                choices=[("ssh", "SSH"), ("api", "vSphere API")],
                default="ssh",
                help_text="ESXi only: SSH uses the SSH plugin, vSphere API uses pyVmomi for direct API access",
                max_length=10,
            ),
        ),
    ]
