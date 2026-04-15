from django.db import models
from .host import Host


class VirtualMachine(models.Model):
    # --- Identity ---
    vmid = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    uuid = models.CharField(max_length=100, blank=True, null=True)
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="vms")
    
    # --- Status & Power ---
    power_state = models.CharField(max_length=50, default="Unknown")
    overall_status = models.CharField(max_length=20, default="gray") # green, yellow, red
    
    # --- Guest Details ---
    guest_os = models.CharField(max_length=255, blank=True, null=True)
    distro = models.CharField(max_length=255, blank=True, null=True)
    kernel = models.CharField(max_length=255, blank=True, null=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    dns_name = models.CharField(max_length=255, blank=True, null=True) # Added for Networking tab
    tools_status = models.CharField(max_length=100, blank=True, null=True)
    tools_running = models.CharField(max_length=100, blank=True, null=True) # Added to prevent Admin errors

    # --- Configuration (Hardware) ---
    num_cpu = models.IntegerField(default=0)
    memory_mb = models.BigIntegerField(default=0)
    hw_version = models.CharField(max_length=20, blank=True, null=True)
    vmx_path = models.TextField(blank=True)
    
    # --- Storage ---
    storage_used_gb = models.FloatField(default=0.0)
    storage_provisioned_gb = models.FloatField(default=0.0)
    
    # --- Real-time Stats ---
    cpu_usage_mhz = models.IntegerField(default=0)
    mem_active_mb = models.IntegerField(default=0)
    uptime_human = models.CharField(max_length=50, blank=True, null=True)

    # --- Networking (Stored as JSON) ---
    # Matches JSON structure: [{"network": "Vm-Network", "mac": "00:0c:...", "ip": ["10.0.0.5"]}]
    networks = models.JSONField(default=list, blank=True)
    dns_servers = models.JSONField(default=list, blank=True) # DNS servers list
    
    # --- Action History (Stored as JSON) ---
    # Tracks all actions performed: [{"action": "Power ON", "timestamp": "2024-01-15 10:30:45", "status": "success|failed"}]
    action_history = models.JSONField(default=list, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Virtual Machine"
        verbose_name_plural = "Virtual Machines"
        unique_together = ('vmid', 'host')
    
    def log_action(self, action_name, status="success", error=None):
        """Log an action to action history."""
        from datetime import datetime
        entry = {
            "action": action_name,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "status": status,
            "error": error
        }
        if not isinstance(self.action_history, list):
            self.action_history = []
        # Keep last 50 actions
        self.action_history = [entry] + self.action_history[:49]
        self.save(update_fields=['action_history'])

    def __str__(self):
        return f"{self.name} (ID: {self.vmid} on {self.host.name})"
