from django.db import models
from django.core.exceptions import ValidationError
from manager.fields import EncryptedTextField


class Host(models.Model):
    HYPERVISOR_VMWARE_ESXI = "vmware_esxi"
    HYPERVISOR_MICROSOFT_HYPERV = "microsoft_hyperv"
    HYPERVISOR_PROXMOX_VE = "proxmox_ve"
    HYPERVISOR_NUTANIX_AHV = "nutanix_ahv"
    HYPERVISOR_KVM_LIBVIRT = "kvm_libvirt"
    HYPERVISOR_CUSTOM = "custom"

    HYPERVISOR_CHOICES = (
        (HYPERVISOR_VMWARE_ESXI, "VMware ESXi"),
        (HYPERVISOR_MICROSOFT_HYPERV, "Microsoft Hyper-V"),
        (HYPERVISOR_PROXMOX_VE, "Proxmox VE"),
        (HYPERVISOR_NUTANIX_AHV, "Nutanix AHV"),
        (HYPERVISOR_KVM_LIBVIRT, "KVM/libvirt"),
        (HYPERVISOR_CUSTOM, "Custom Plugin"),
    )

    # ESXi-specific connection method choices
    CONNECTION_SSH = "ssh"
    CONNECTION_API = "api"
    
    ESXI_CONNECTION_CHOICES = (
        (CONNECTION_SSH, "SSH"),
        (CONNECTION_API, "vSphere API"),
    )

    # Identity & Connection
    name = models.CharField(max_length=100)
    ip_address = models.GenericIPAddressField(unique=True)
    hypervisor_type = models.CharField(
        max_length=50,
        choices=HYPERVISOR_CHOICES,
        default=HYPERVISOR_VMWARE_ESXI,
    )
    esxi_connection_method = models.CharField(
        max_length=10,
        choices=ESXI_CONNECTION_CHOICES,
        default=CONNECTION_SSH,
        help_text="ESXi only: SSH uses the SSH plugin, vSphere API uses pyVmomi for direct API access",
    )
    username = models.CharField(max_length=50, default="root")
    password = models.CharField(max_length=255, blank=True, default="")
    ssh_public_key = EncryptedTextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    # --- Hardware/Software Details ---
    vendor = models.CharField(max_length=100, blank=True, null=True)
    model_name = models.CharField(max_length=100, blank=True, null=True)
    processor_type = models.CharField(max_length=200, blank=True, null=True)
    os_version = models.CharField(max_length=100, blank=True, null=True)
    
    # --- Capacity ---
    cpu_count = models.IntegerField(default=0)
    memory_gb = models.IntegerField(default=0)
    
    # --- License Info ---
    license_name = models.CharField(max_length=200, blank=True, null=True)
    license_key = models.CharField(max_length=100, blank=True, null=True)
    
    # --- Service Status (JSON) ---
    services_status = models.JSONField(default=dict, blank=True)
    
    # --- Network Data (JSON) ---
    network_data = models.JSONField(default=dict, blank=True)
    
    # --- Storage Data (JSON) ---
    storage_data = models.JSONField(default=dict, blank=True)
    
    last_sync = models.DateTimeField(auto_now=True)

    def get_connection(self):
        from manager.utils import get_conn 
        return get_conn(self.name)

    def clean(self):
        super().clean()
        if self.ssh_public_key and not self.ssh_public_key.strip().startswith("ssh-"):
            raise ValidationError({"ssh_public_key": "SSH public key must start with 'ssh-'"})

    def __str__(self):
        return f"{self.name} ({self.ip_address})"
