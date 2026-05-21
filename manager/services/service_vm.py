# manager/services/service_vm.py
#
# Thin dispatcher — vendor-specific VM sync logic lives in each HypervisorAdapter.
# sync_vms_for_host() delegates to the registered adapter; no vendor switches here.
import datetime
from django.core.cache import cache
from manager.utils import get_conn
from manager.models import VirtualMachine, Host
from manager.hypervisors import get_adapter
from lib.kvm import manage as kvm_manage
from lib.vms import manage as vm_manage
from manager.websocket_broadcaster import (
    broadcast_vm_power_state_changed,
    broadcast_vm_snapshot_operation,
    broadcast_vm_operation,
)

# =========================================================
# 1. VM ACTION TRIGGER — direct SSH (no HTTP self-call)
# =========================================================

# Maps admin action keys to vim-cmd sub-commands
_POWER_MAP = {
    "poweron": "power.on",
    "poweroff": "power.off",
    "shutdown": "power.shutdown",
    "guest_restart": "power.reboot",
    "reset": "power.reset",
    "reboot": "power.reboot",
    "suspend": "power.suspend",
}

# Maps admin action keys to DB power_state values
_STATE_MAP = {
    "poweron": "poweredOn",
    "poweroff": "poweredOff",
    "shutdown": "poweredOff",
    "guest_restart": "poweredOn",
    "suspend": "suspended",
}

_PROXMOX_POWER_MAP = {
    "poweron": "start",
    "on": "start",
    "poweroff": "stop",
    "off": "stop",
    "shutdown": "shutdown",
    "guest_restart": "reboot",
    "reset": "reset",
    "reboot": "reboot",
    "suspend": "suspend",
}

_KVM_POWER_MAP = {
    "poweron": "power.on",
    "on": "power.on",
    "poweroff": "power.off",
    "off": "power.off",
    "shutdown": "power.shutdown",
    "guest_restart": "power.reboot",
    "reset": "power.reset",
    "reboot": "power.reboot",
    "suspend": "power.suspend",
}




def trigger_vm_action(vm_obj, action_type, params=None):
    """
    Execute a power or snapshot operation directly via SSH.
    Updates the DB and broadcasts WebSocket events.
    Returns: (success_boolean, message_string)
    """
    host_name = vm_obj.host.name
    vmid = vm_obj.vmid

    try:
        if vm_obj.host.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
            with get_conn(host_name) as conn:
                if action_type in _PROXMOX_POWER_MAP:
                    node = conn.resolve_node(host_name)
                    prox_action = _PROXMOX_POWER_MAP[action_type]
                    conn.vm_power(node, vmid, prox_action)

                    if action_type.lower() in ["poweron", "on"]:
                        vm_obj.power_state = "poweredOn"
                    elif action_type.lower() in ["poweroff", "off", "shutdown"]:
                        vm_obj.power_state = "poweredOff"
                    elif action_type.lower() in ["suspend"]:
                        vm_obj.power_state = "suspended"
                    vm_obj.save(update_fields=["power_state"])
                    broadcast_vm_power_state_changed(vm_obj)
                    return True, "Success"

                return False, f"Unsupported Proxmox action: {action_type}"

        if vm_obj.host.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
            with get_conn(host_name) as conn:
                if action_type in _KVM_POWER_MAP:
                    kvm_manage.power_op(conn, vmid, _KVM_POWER_MAP[action_type])

                    if action_type.lower() in ["poweron", "on"]:
                        vm_obj.power_state = "poweredOn"
                    elif action_type.lower() in ["poweroff", "off", "shutdown"]:
                        vm_obj.power_state = "poweredOff"
                    elif action_type.lower() in ["suspend"]:
                        vm_obj.power_state = "suspended"
                    vm_obj.save(update_fields=["power_state"])
                    broadcast_vm_power_state_changed(vm_obj)
                    return True, "Success"

                op = (params or {}).get("op", action_type)
                snap_name = (params or {}).get("name", f"Snap-{datetime.datetime.now().strftime('%m%d-%H%M')}")
                kvm_manage.snapshot_op(conn, vmid, op, name=snap_name)
                broadcast_vm_snapshot_operation(vm_obj, op, snap_name)
                return True, "Success"

        with get_conn(host_name) as conn:
            if action_type in _POWER_MAP:
                esxi_cmd = _POWER_MAP[action_type]
                result = vm_manage.power_op(conn, vmid, esxi_cmd)

                if isinstance(result, str) and result.startswith("Error"):
                    broadcast_vm_operation(vm_obj, f"power_{action_type}", "failed", error=result.strip())
                    return False, result.strip()

                # Update DB power state
                new_state = _STATE_MAP.get(action_type)
                if new_state:
                    vm_obj.power_state = new_state
                    vm_obj.save(update_fields=["power_state"])
                broadcast_vm_power_state_changed(vm_obj)
                return True, "Success"

            else:
                # Snapshot operations
                op = (params or {}).get("op", action_type)
                snap_name = (params or {}).get("name", f"Snap-{datetime.datetime.now().strftime('%m%d-%H%M')}")

                lib_op = op.lower()
                if lib_op == "restore":
                    lib_op = "revert"
                if lib_op == "delete_all":
                    lib_op = "removeall"

                result = vm_manage.snapshot_op(conn, vmid, lib_op, name=snap_name)
                broadcast_vm_snapshot_operation(vm_obj, op, snap_name)
                return True, result.strip() if result else "Success"

    except Exception as e:
        broadcast_vm_operation(vm_obj, action_type, "failed", error=str(e))
        return False, f"Failed: {str(e)}"


# =========================================================
# VM SYNC — thin dispatcher, delegates to HypervisorAdapter
# =========================================================

def sync_vms_for_host(host, conn=None) -> int:
    """Sync the VM inventory for *host* using the registered hypervisor adapter.

    If *conn* is provided (shared-session mode from run_sync.py) it is passed
    directly to the adapter.  Otherwise a fresh connection is opened.
    """
    adapter = get_adapter(host.hypervisor_type)
    try:
        if conn is not None:
            return adapter.sync_vms(host, conn)
        with get_conn(host.name) as fresh_conn:
            return adapter.sync_vms(host, fresh_conn)
    except Exception as exc:
        print(f"❌ sync_vms_for_host failed for '{host.name}': {exc}")
        return 0