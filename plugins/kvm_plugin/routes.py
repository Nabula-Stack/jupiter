from ninja import Router

from manager.models import Host
from manager.utils import get_conn, get_host_obj
from lib.kvm import manage as kvm_manage

router = Router(tags=["KVM Plugin"])


@router.get("/{host_name}/health", summary="KVM host health summary")
def kvm_health(request, host_name: str):
    host = get_host_obj(host_name, require_active=True)
    if host.hypervisor_type != Host.HYPERVISOR_KVM_LIBVIRT:
        return {"status": "error", "message": f"Host '{host_name}' is not KVM/libvirt"}

    try:
        with get_conn(host_name) as conn:
            vm_rows = kvm_manage.list_vms_with_stats(conn)
            pools = kvm_manage.list_storage_pools(conn)
            nets = kvm_manage.list_networks(conn)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    return {
        "status": "success",
        "host": host.name,
        "hypervisor": host.hypervisor_type,
        "last_sync": host.last_sync.isoformat() if host.last_sync else None,
        "vm_count": len(vm_rows),
        "storage_pools": len(pools),
        "networks": len(nets),
    }
