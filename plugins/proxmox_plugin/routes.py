from ninja import Router

from manager.utils import get_host_obj

router = Router(tags=["Proxmox Plugin"])


@router.get("/{host_name}/health", summary="Proxmox host health summary")
def proxmox_health(request, host_name: str):
    host = get_host_obj(host_name, require_active=True)
    if host.hypervisor_type != "proxmox_ve":
        return {"status": "error", "message": f"Host '{host_name}' is not Proxmox"}

    return {
        "status": "success",
        "host": host.name,
        "hypervisor": host.hypervisor_type,
        "last_sync": host.last_sync.isoformat() if host.last_sync else None,
        "services_count": len((host.services_status or {}).get("services", [])),
        "network_interfaces": len((host.network_data or {}).get("physical_nics", [])),
        "storage_items": len((host.storage_data or {}).get("datastores", [])),
        "vm_count": host.vms.count(),
    }
