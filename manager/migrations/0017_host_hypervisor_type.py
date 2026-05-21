from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manager", "0016_encrypt_host_ssh_public_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="host",
            name="hypervisor_type",
            field=models.CharField(
                choices=[
                    ("vmware_esxi", "VMware ESXi"),
                    ("microsoft_hyperv", "Microsoft Hyper-V"),
                    ("proxmox_ve", "Proxmox VE"),
                    ("nutanix_ahv", "Nutanix AHV"),
                    ("kvm_libvirt", "KVM/libvirt"),
                    ("custom", "Custom Plugin"),
                ],
                default="vmware_esxi",
                max_length=50,
            ),
        ),
    ]
