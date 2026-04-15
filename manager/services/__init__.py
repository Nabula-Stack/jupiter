from .service_host import sync_host_details_to_db
from .service_vm import sync_vms_for_host, trigger_vm_action
from ..models import Host 

def sync_all_data():
    """Main entry point for background sync."""
    hosts = Host.objects.filter(is_active=True)
    total_vms = 0
    failed_hosts = []

    for h in hosts:
        try:
            # 1. Sync host metadata
            sync_host_details_to_db(h)

            # 2. Sync all VMs on that host
            vm_count = sync_vms_for_host(h)
            
            if vm_count is None:
                vm_count = 0
                
            total_vms += vm_count
            print(f"✅ [{h.name}] Sync Complete (VMs: {vm_count})")

        except Exception as e:
            failed_hosts.append(h.name)
            print(f"❌ Sync failed for {h.name}: {e}")

    if failed_hosts:
        print(f"⚠️ Failed hosts: {', '.join(failed_hosts)}")

    return total_vms

# --- Wrappers for Admin/Tasks ---

def sync_vms_to_db():
    """Legacy wrapper for the VM Admin button"""
    return sync_all_data()

def sync_host_details_to_db_wrapper():
    """Wrapper for Host-only sync tasks"""
    hosts = Host.objects.filter(is_active=True)
    for h in hosts:
        try:
            sync_host_details_to_db(h)
        except Exception as e:
            print(f"❌ Host sync failed for {h.name}: {e}")