import json
import posixpath
import re
import threading
from ninja import Router, File, UploadedFile
from django.views.decorators.cache import cache_page
from django.core.cache import cache
from ninja.decorators import decorate_view
from django.shortcuts import get_object_or_404, redirect

from manager.models import VirtualMachine
from manager.models import Host
from manager.utils import get_conn, get_host_obj
from manager.websocket_broadcaster import (
    broadcast_vm_power_state_changed,
    broadcast_vm_created,
    broadcast_vm_modified,
    broadcast_vm_snapshot_operation,
    broadcast_vm_operation,
)
from lib.vms import manage as vm_manage
from lib.vms import info as vm_info
from lib.vms import create as vm_create
from lib.vms import migrate as vm_migrate
from lib.vms import modify as vm_modify
from lib.storage import manage as storage_manage
from lib.network import manage as network_manage
from lib.kvm import manage as kvm_manage
from manager.services import sync_vms_for_host

router = Router(tags=["Virtual Machine Management"])

# VMX guestOS values (short hyphenated format required by ESXi .vmx files).
GUEST_OS_OPTIONS = [
    {"value": "other-64",              "label": "Other Linux / Generic 64-bit"},
    {"value": "ubuntu-64",             "label": "Ubuntu Linux 64-bit"},
    {"value": "debian12-64",           "label": "Debian 12 64-bit"},
    {"value": "debian-64",             "label": "Debian 10/11 64-bit"},
    {"value": "centos-64",             "label": "CentOS 64-bit"},
    {"value": "rhel9-64",              "label": "Red Hat Enterprise Linux 9 64-bit"},
    {"value": "rhel8-64",              "label": "Red Hat Enterprise Linux 8 64-bit"},
    {"value": "sles15-64",             "label": "SUSE Linux Enterprise 15 64-bit"},
    {"value": "windows2022srvNext-64", "label": "Windows Server 2022 64-bit"},
    {"value": "windows2019srv-64",     "label": "Windows Server 2019 64-bit"},
    {"value": "windows2016srv-64",     "label": "Windows Server 2016 64-bit"},
    {"value": "windows9-64",           "label": "Windows 10/11 64-bit"},
]

NIC_OPTIONS = ["e1000", "e1000e", "vmxnet3"]
SCSI_CONTROLLER_OPTIONS = ["lsilogic", "lsisas1068", "pvscsi"]
DISK_TYPE_OPTIONS = ["thin", "zeroedthick", "eagerzeroedthick"]
FIRMWARE_OPTIONS = ["bios", "efi"]
HW_VERSION_OPTIONS = [str(v) for v in range(10, 22)]


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _safe_list_esxi_pci_devices(conn):
    """Best-effort parser for `esxcli hardware pci list` output."""
    try:
        raw = conn.run("esxcli hardware pci list")
        if not raw or str(raw).startswith("Error:"):
            return []

        devices = []
        block = {}
        for line in str(raw).splitlines():
            if not line.strip():
                if block:
                    bus = str(block.get("Bus", "")).strip()
                    slot = str(block.get("Slot", "")).strip()
                    func = str(block.get("Function", "")).strip()
                    vendor_name = str(block.get("Vendor Name", "")).strip()
                    device_name = str(block.get("Device Name", "")).strip()
                    if bus and slot and func:
                        pci_id = f"{bus}:{slot}.{func}"
                        devices.append(
                            {
                                "id": pci_id,
                                "label": f"{pci_id} - {vendor_name} {device_name}".strip(),
                                "vendor": vendor_name,
                                "device": device_name,
                            }
                        )
                    block = {}
                continue

            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            block[key.strip()] = val.strip()

        if block:
            bus = str(block.get("Bus", "")).strip()
            slot = str(block.get("Slot", "")).strip()
            func = str(block.get("Function", "")).strip()
            vendor_name = str(block.get("Vendor Name", "")).strip()
            device_name = str(block.get("Device Name", "")).strip()
            if bus and slot and func:
                pci_id = f"{bus}:{slot}.{func}"
                devices.append(
                    {
                        "id": pci_id,
                        "label": f"{pci_id} - {vendor_name} {device_name}".strip(),
                        "vendor": vendor_name,
                        "device": device_name,
                    }
                )

        return devices
    except Exception:
        return []


def _safe_list_proxmox_pci_devices(conn, node):
    try:
        rows = conn.list_pci_devices(node)
        devices = []
        for row in rows:
            # Typical key names in Proxmox API rows can vary by version.
            pci_id = str(
                row.get("id")
                or row.get("device")
                or row.get("slot")
                or row.get("pciid")
                or ""
            ).strip()
            if not pci_id:
                continue

            vendor = str(row.get("vendor_name") or row.get("vendor") or "").strip()
            device = str(row.get("device_name") or row.get("name") or "").strip()
            label = f"{pci_id} - {vendor} {device}".strip()
            devices.append(
                {
                    "id": pci_id,
                    "label": label,
                    "vendor": vendor,
                    "device": device,
                }
            )
        return devices
    except Exception:
        return []


def bust_vm_cache(host_name, vmid=None):
    if hasattr(cache, "delete_pattern"):
        cache.delete_pattern(f"*{host_name}/list*")
        if vmid:
            cache.delete_pattern(f"*{host_name}/{vmid}/details*")
    else:
        cache.clear()


# =========================================================
# 0. REMOTE ACCESS
# =========================================================

@router.get("/{host_name}/{vmid}/console", summary="Get Browser Console Redirect")
def get_vm_console_redirect(request, host_name: str, vmid: str):
    host_obj = get_host_obj(host_name, require_active=True)
    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        try:
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                console_info = conn.get_vnc_console_ticket(node, vmid)
            vnc_ticket = console_info.get("ticket", "")
            vnc_port = console_info.get("port", 5900)
            if not vnc_ticket:
                return {"status": "error", "message": "Failed to obtain VNC console ticket from Proxmox"}
            console_url = f"https://{host_obj.ip_address}:8006/?console=kvm&novnc=1&vmid={vmid}&node={node}&vncticket={vnc_ticket}"
        except Exception as e:
            return {"status": "error", "message": f"Console error: {str(e)}"}
    elif host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        console_url = f"https://{host_obj.ip_address}:9090"
    else:
        console_url = f"https://{host_obj.ip_address}/ui/#/console/{vmid}"
    print(f"[DEBUG] Console Redirect | Match Found: {host_obj.name} ({host_obj.ip_address}) | VMID: {vmid}")
    return redirect(console_url)

# =========================================================
# 1. INVENTORY & STATS
# =========================================================

@router.get("/{host_name}/list", summary="List All VMs with Stats")
@decorate_view(cache_page(30))
def list_vms_with_stats(request, host_name: str):
    try:
        host_obj = get_host_obj(host_name, require_active=True)
        vms = host_obj.vms.all()
        vm_list = []
        for vm in vms:
            used_gb = _to_float(vm.storage_used_gb, 0.0)
            provisioned_gb = _to_float(vm.storage_provisioned_gb, 0.0)
            vm_list.append({
                "id": vm.id,
                "vmid": vm.vmid,
                "name": vm.name,
                "power_state": vm.power_state,
                "state": vm.power_state,
                "guest_os": vm.guest_os or "Unknown",
                "guest_name": vm.guest_os or "N/A",
                "ip_address": str(vm.ip_address) if vm.ip_address else "N/A",
                "is_running": vm.power_state.lower() == "poweredon",
                "storage_used_gb": round(used_gb, 2),
                "storage_provisioned_gb": round(provisioned_gb, 2),
                "storage_free_gb": round(max(provisioned_gb - used_gb, 0.0), 2),
                "cpu_usage_mhz": int(vm.cpu_usage_mhz or 0),
                "mem_active_mb": int(vm.mem_active_mb or 0),
                "updated_at": vm.updated_at.isoformat() if vm.updated_at else None,
            })
        return {"vms": vm_list, "host": host_name, "count": len(vm_list)}
    except Exception as e:
        return {"error": str(e), "vms": []}

@router.get("/{host_name}/db/list", summary="Get VM List from Database (No Cache)")
def get_vms_from_db(request, host_name: str):
    try:
        host_obj = get_host_obj(host_name, require_active=True)
        vms = host_obj.vms.all()
        vm_list = []
        for vm in vms:
            used_gb = _to_float(vm.storage_used_gb, 0.0)
            provisioned_gb = _to_float(vm.storage_provisioned_gb, 0.0)
            vm_list.append({
                "id": vm.id,
                "vmid": vm.vmid,
                "name": vm.name,
                "power_state": vm.power_state,
                "state": vm.power_state,
                "guest_os": vm.guest_os or "Unknown",
                "guest_name": vm.guest_os or "N/A",
                "ip_address": str(vm.ip_address) if vm.ip_address else "N/A",
                "is_running": vm.power_state.lower() == "poweredon",
                "storage_used_gb": round(used_gb, 2),
                "storage_provisioned_gb": round(provisioned_gb, 2),
                "storage_free_gb": round(max(provisioned_gb - used_gb, 0.0), 2),
                "updated_at": vm.updated_at.isoformat() if vm.updated_at else None,
            })
        return {"vms": vm_list, "host": host_name, "count": len(vm_list)}
    except Exception as e:
        return {"error": str(e), "vms": []}

@router.get("/{host_name}/{vmid}/details", summary="Get Specific VM Details")
@decorate_view(cache_page(30))
def get_vm_info_details(request, host_name: str, vmid: str):
    host_obj = get_host_obj(host_name, require_active=True)
    vm_obj = get_object_or_404(VirtualMachine, host=host_obj, vmid=vmid)

    used_gb = _to_float(vm_obj.storage_used_gb, 0.0)
    provisioned_gb = _to_float(vm_obj.storage_provisioned_gb, 0.0)
    storage_payload = {
        "used_gb": round(used_gb, 2),
        "provisioned_gb": round(provisioned_gb, 2),
        "free_gb": round(max(provisioned_gb - used_gb, 0.0), 2),
    }

    return {
        "id": vm_obj.id,
        "vmid": vm_obj.vmid,
        "name": vm_obj.name,
        "uuid": vm_obj.uuid,
        "power_state": vm_obj.power_state,
        "overall_status": vm_obj.overall_status,
        "guest_os": vm_obj.guest_os,
        "distro": vm_obj.distro,
        "kernel": vm_obj.kernel,
        "ip_address": str(vm_obj.ip_address) if vm_obj.ip_address else None,
        "dns_name": vm_obj.dns_name,
        "tools_status": vm_obj.tools_status if host_obj.hypervisor_type != Host.HYPERVISOR_PROXMOX_VE else None,
        "tools_running": vm_obj.tools_running if host_obj.hypervisor_type != Host.HYPERVISOR_PROXMOX_VE else None,
        "agent_status": vm_obj.tools_running if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE else None,
        "num_cpu": vm_obj.num_cpu,
        "memory_mb": vm_obj.memory_mb,
        "cpu_usage_mhz": vm_obj.cpu_usage_mhz,
        "mem_active_mb": vm_obj.mem_active_mb,
        "uptime_human": vm_obj.uptime_human,
        "vmx": vm_obj.vmx_path,
        "hw_version": vm_obj.hw_version,
        "networks": vm_obj.networks or [],
        "dns_servers": vm_obj.dns_servers or [],
        "storage": storage_payload,
        "storage_used_gb": storage_payload["used_gb"],
        "storage_provisioned_gb": storage_payload["provisioned_gb"],
        "storage_free_gb": storage_payload["free_gb"],
    }

# =========================================================
# 2. POWER & LIFECYCLE
# =========================================================

@router.post("/{host_name}/{vmid}/power", summary="Power Operations")
def vm_power_control(request, host_name: str, vmid: str, action: str):
    esxi_action_map = {
        "poweron": "power.on", "poweroff": "power.off",
        "shutdown": "power.shutdown", "reset": "power.reset",
        "reboot": "power.reboot", "suspend": "power.suspend",
        "on": "power.on", "off": "power.off"
    }
    esxi_action = esxi_action_map.get(action.lower(), action)
    try:
        host_obj = get_host_obj(host_name, require_active=True)
        if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
            prox_action_map = {
                "poweron": "start", "on": "start",
                "poweroff": "stop", "off": "stop",
                "shutdown": "shutdown", "reset": "reset",
                "reboot": "reboot", "suspend": "suspend",
            }
            prox_action = prox_action_map.get(action.lower())
            if not prox_action:
                return {"status": "error", "message": f"Unsupported Proxmox action: {action}"}

            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                vm_status = conn.get_vm_status(node, vmid)
                is_running = str(vm_status.get("status", "")).lower() == "running"

                if prox_action in {"reset", "reboot", "shutdown", "suspend"} and not is_running:
                    return {
                        "status": "error",
                        "message": f"VM {vmid} must be running before '{action}'",
                    }

                conn.vm_power(node, vmid, prox_action)

            vm_obj = VirtualMachine.objects.filter(vmid=vmid, host=host_obj).first()
            if vm_obj:
                if action.lower() in ["poweron", "on"]:
                    vm_obj.power_state = "poweredOn"
                elif action.lower() in ["poweroff", "off", "shutdown"]:
                    vm_obj.power_state = "poweredOff"
                elif action.lower() in ["suspend"]:
                    vm_obj.power_state = "suspended"
                vm_obj.save(update_fields=["power_state"])
                broadcast_vm_power_state_changed(vm_obj)
            return {"status": "success", "vmid": vmid, "action": action, "result": "OK"}

        if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
            with get_conn(host_name) as conn:
                kvm_action_map = {
                    "poweron": "power.on", "on": "power.on",
                    "poweroff": "power.off", "off": "power.off",
                    "shutdown": "power.shutdown", "reset": "power.reset",
                    "reboot": "power.reboot", "suspend": "power.suspend",
                }
                kvm_action = kvm_action_map.get(action.lower())
                if not kvm_action:
                    return {"status": "error", "message": f"Unsupported KVM action: {action}"}
                kvm_manage.power_op(conn, vmid, kvm_action)

            vm_obj = VirtualMachine.objects.filter(vmid=vmid, host=host_obj).first()
            if vm_obj:
                if action.lower() in ["poweron", "on"]:
                    vm_obj.power_state = "poweredOn"
                elif action.lower() in ["poweroff", "off", "shutdown"]:
                    vm_obj.power_state = "poweredOff"
                elif action.lower() in ["suspend"]:
                    vm_obj.power_state = "suspended"
                vm_obj.save(update_fields=["power_state"])
                broadcast_vm_power_state_changed(vm_obj)
            return {"status": "success", "vmid": vmid, "action": action, "result": "OK"}

        with get_conn(host_name) as conn:
            result = vm_manage.power_op(conn, vmid, esxi_action)
            bust_vm_cache(host_name, vmid)
            if "Error" in result:
                broadcast_vm_operation(
                    VirtualMachine.objects.filter(vmid=vmid, host=host_obj).first(),
                    f"power_{action}", "failed", error=result.strip()
                )
                return {"status": "error", "vmid": vmid, "result": result.strip()}
            vm_obj = VirtualMachine.objects.filter(vmid=vmid, host=host_obj).first()
            if vm_obj:
                if action.lower() in ["poweron", "on"]:
                    vm_obj.power_state = "poweredOn"
                elif action.lower() in ["poweroff", "off", "shutdown"]:
                    vm_obj.power_state = "poweredOff"
                elif action.lower() in ["suspend"]:
                    vm_obj.power_state = "suspended"
                vm_obj.save()
                broadcast_vm_power_state_changed(vm_obj)
            return {"status": "success", "vmid": vmid, "action": action, "result": "OK"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# 3. SNAPSHOT OPERATIONS
# =========================================================

@router.post("/{host_name}/{vmid}/snapshots", summary="Snapshot Management")
def vm_snapshot_control(request, host_name: str, vmid: str, op: str, name: str = "Admin-Snap"):
    host_obj = get_host_obj(host_name, require_active=True)

    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        try:
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                lib_op = op.lower()
                if lib_op in {"create", "new"}:
                    conn.create(f"/nodes/{node}/qemu/{vmid}/snapshot", data={"snapname": name})
                    return {"status": "success", "output": f"Snapshot '{name}' created"}
                if lib_op in {"restore", "revert"}:
                    conn.create(f"/nodes/{node}/qemu/{vmid}/snapshot/{name}/rollback")
                    return {"status": "success", "output": f"Snapshot '{name}' restored"}
                if lib_op in {"delete", "remove"}:
                    conn.delete(f"/nodes/{node}/qemu/{vmid}/snapshot/{name}")
                    return {"status": "success", "output": f"Snapshot '{name}' deleted"}
                if lib_op in {"delete_all", "removeall"}:
                    return {
                        "status": "error",
                        "message": "Proxmox snapshot API does not provide a single delete_all action via this endpoint.",
                    }
                return {"status": "error", "message": f"Unsupported Proxmox snapshot operation: {op}"}
        except Exception as e:
            vm_obj = VirtualMachine.objects.filter(vmid=vmid).first()
            if vm_obj:
                broadcast_vm_operation(vm_obj, f"snapshot_{op}", "failed", error=str(e))
            return {"status": "error", "message": str(e)}

    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        try:
            with get_conn(host_name) as conn:
                lib_op = op.lower()
                if lib_op == "restore":
                    lib_op = "revert"
                if lib_op == "delete_all":
                    lib_op = "removeall"
                result = kvm_manage.snapshot_op(conn, vmid, lib_op, name=name)
                vm_obj = VirtualMachine.objects.filter(vmid=vmid).first()
                if vm_obj:
                    broadcast_vm_snapshot_operation(vm_obj, op, name)
                return {"status": "success", "output": result.strip() if result else "Success"}
        except Exception as e:
            vm_obj = VirtualMachine.objects.filter(vmid=vmid).first()
            if vm_obj:
                broadcast_vm_operation(vm_obj, f"snapshot_{op}", "failed", error=str(e))
            return {"status": "error", "message": str(e)}

    try:
        with get_conn(host_name) as conn:
            lib_op = op.lower()
            if lib_op == "restore": lib_op = "revert"
            if lib_op == "delete_all": lib_op = "removeall"
            result = vm_manage.snapshot_op(conn, vmid, lib_op, name=name)
            vm_obj = VirtualMachine.objects.filter(vmid=vmid).first()
            if vm_obj:
                broadcast_vm_snapshot_operation(vm_obj, op, name)
            return {"status": "success", "output": result.strip() if result else "Success"}
    except Exception as e:
        vm_obj = VirtualMachine.objects.filter(vmid=vmid).first()
        if vm_obj:
            broadcast_vm_operation(vm_obj, f"snapshot_{op}", "failed", error=str(e))
        return {"status": "error", "message": str(e)}

# =========================================================
# 4. PROVISIONING & MAINTENANCE
# =========================================================

@router.get("/{host_name}/create/options", summary="Get VM Create Form Options")
def get_create_vm_options(request, host_name: str):
    host_obj = get_host_obj(host_name, require_active=True)

    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        with get_conn(host_name) as conn:
            pools = kvm_manage.list_storage_pools(conn)
            networks = kvm_manage.list_networks(conn)

        return {
            "datastores": [
                {
                    "name": p.get("name", ""),
                    "free": int(p.get("free", 0)),
                    "total": int(p.get("total", 0)),
                    "type": p.get("type", "libvirt-pool"),
                }
                for p in pools
            ],
            "networks": networks,
            "network_details": [{"name": n, "type": "libvirt-network"} for n in networks],
            "pci_devices": [],
            "isos": [],
            "capabilities": {
                "cpu_hotplug": False,
                "memory_hotplug": False,
                "hardware_virtualization": True,
                "pci_passthrough": False,
            },
            "guest_os_options": [
                {"value": "linux", "label": "Linux"},
                {"value": "windows", "label": "Windows"},
                {"value": "other", "label": "Other"},
            ],
            "nic_options": ["virtio", "e1000"],
            "scsi_controller_options": ["virtio-scsi"],
            "disk_type_options": ["qcow2"],
            "firmware_options": ["bios", "efi"],
            "hw_version_options": ["kvm"],
            "defaults": {
                "cpu": 2,
                "ram": 2048,
                "disk_size_gb": 16,
                "disk_type": "qcow2",
                "guest_os": "linux",
                "nic_type": "virtio",
                "scsi_controller": "virtio-scsi",
                "firmware": "bios",
                "hw_version": "kvm",
                "power_on": False,
                "cpu_hotplug": False,
                "memory_hotplug": False,
                "hardware_virtualization": True,
                "pci_passthrough_devices": [],
            },
        }

    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        try:
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                storages = conn.list_storage(node)
                networks = conn.list_network(node)
                pci_devices = _safe_list_proxmox_pci_devices(conn, node)

            datastores = []
            for storage_item in storages:
                if str(storage_item.get("enabled", 1)) == "0":
                    continue
                datastores.append(
                    {
                        "name": storage_item.get("storage") or storage_item.get("name") or "",
                        "free": int(storage_item.get("avail") or 0),
                        "total": int(storage_item.get("total") or 0),
                        "type": storage_item.get("type") or "",
                    }
                )

            network_names = []
            for net in networks:
                if net.get("type") in {"bridge", "bond", "vlan", "OVSBridge", "OVSBond"}:
                    network_names.append(net.get("iface") or net.get("name") or "")
            network_names = [name for name in network_names if name]

            return {
                "datastores": datastores,
                "networks": network_names,
                "network_details": networks,
                "pci_devices": pci_devices,
                "isos": [],
                "capabilities": {
                    "cpu_hotplug": True,
                    "memory_hotplug": True,
                    "hardware_virtualization": True,
                    "pci_passthrough": True,
                },
                "guest_os_options": [
                    {"value": "l26", "label": "Linux 2.6+/3.x/4.x/5.x Kernel"},
                    {"value": "l24", "label": "Linux 2.4 Kernel"},
                    {"value": "win11", "label": "Windows 11/2022"},
                    {"value": "win10", "label": "Windows 10/2016/2019"},
                    {"value": "win8", "label": "Windows 8/2012"},
                    {"value": "win7", "label": "Windows 7/2008r2"},
                    {"value": "other", "label": "Other"},
                ],
                "nic_options": ["virtio", "e1000", "rtl8139", "vmxnet3"],
                "scsi_controller_options": ["virtio-scsi-pci", "virtio-scsi-single", "lsi", "megasas", "pvscsi"],
                "disk_type_options": ["raw", "qcow2"],
                "firmware_options": ["seabios", "ovmf"],
                "hw_version_options": ["13"],
                "defaults": {
                    "cpu": 2,
                    "ram": 2048,
                    "disk_size_gb": 16,
                    "disk_type": "raw",
                    "guest_os": "l26",
                    "nic_type": "virtio",
                    "scsi_controller": "virtio-scsi-pci",
                    "firmware": "seabios",
                    "hw_version": "13",
                    "power_on": False,
                    "cpu_hotplug": True,
                    "memory_hotplug": True,
                    "hardware_virtualization": True,
                    "pci_passthrough_devices": [],
                },
            }
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Cannot load Proxmox options for '{host_name}': {str(exc)}",
                "datastores": [],
                "networks": [],
                "network_details": [],
                "pci_devices": [],
                "isos": [],
                "capabilities": {
                    "cpu_hotplug": True,
                    "memory_hotplug": True,
                    "hardware_virtualization": True,
                    "pci_passthrough": True,
                },
                "guest_os_options": [
                    {"value": "l26", "label": "Linux 2.6+/3.x/4.x/5.x Kernel"},
                    {"value": "other", "label": "Other"},
                ],
                "nic_options": ["virtio", "e1000"],
                "scsi_controller_options": ["virtio-scsi-pci"],
                "disk_type_options": ["raw", "qcow2"],
                "firmware_options": ["seabios", "ovmf"],
                "hw_version_options": ["13"],
                "defaults": {
                    "cpu": 2,
                    "ram": 2048,
                    "disk_size_gb": 16,
                    "disk_type": "raw",
                    "guest_os": "l26",
                    "nic_type": "virtio",
                    "scsi_controller": "virtio-scsi-pci",
                    "firmware": "seabios",
                    "hw_version": "13",
                    "power_on": False,
                    "cpu_hotplug": True,
                    "memory_hotplug": True,
                    "hardware_virtualization": True,
                    "pci_passthrough_devices": [],
                },
            }

    try:
        with get_conn(host_name) as conn:
            datastores = storage_manage.list_datastores(conn)
            portgroups = network_manage.list_portgroups(conn)
            pci_devices = _safe_list_esxi_pci_devices(conn)
            try:
                iso_raw = conn.run("find /vmfs/volumes -maxdepth 4 -name '*.iso' 2>/dev/null")
                isos = [l.strip() for l in iso_raw.splitlines() if l.strip().lower().endswith(".iso")]
            except Exception:
                isos = []
            return {
                "datastores": datastores,
                "networks": [pg.get("name") for pg in portgroups if pg.get("name")],
                "network_details": portgroups,
                "pci_devices": pci_devices,
                "isos": isos,
                "capabilities": {
                    "cpu_hotplug": True,
                    "memory_hotplug": True,
                    "hardware_virtualization": True,
                    "pci_passthrough": True,
                },
                "guest_os_options": GUEST_OS_OPTIONS,
                "nic_options": NIC_OPTIONS,
                "scsi_controller_options": SCSI_CONTROLLER_OPTIONS,
                "disk_type_options": DISK_TYPE_OPTIONS,
                "firmware_options": FIRMWARE_OPTIONS,
                "hw_version_options": HW_VERSION_OPTIONS,
                "defaults": {
                    "cpu": 2, "ram": 2048, "disk_size_gb": 16, "disk_type": "thin",
                    "guest_os": "other-64", "nic_type": "e1000", "scsi_controller": "lsilogic",
                    "firmware": "bios", "hw_version": "13", "power_on": False,
                    "cpu_hotplug": False, "memory_hotplug": False,
                    "hardware_virtualization": False,
                    "pci_passthrough_devices": [],
                },
            }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Cannot load ESXi options for '{host_name}': {str(exc)}",
            "datastores": [],
            "networks": [],
            "network_details": [],
            "pci_devices": [],
            "isos": [],
            "capabilities": {
                "cpu_hotplug": True,
                "memory_hotplug": True,
                "hardware_virtualization": True,
                "pci_passthrough": True,
            },
            "guest_os_options": GUEST_OS_OPTIONS,
            "nic_options": NIC_OPTIONS,
            "scsi_controller_options": SCSI_CONTROLLER_OPTIONS,
            "disk_type_options": DISK_TYPE_OPTIONS,
            "firmware_options": FIRMWARE_OPTIONS,
            "hw_version_options": HW_VERSION_OPTIONS,
            "defaults": {
                "cpu": 2,
                "ram": 2048,
                "disk_size_gb": 16,
                "disk_type": "thin",
                "guest_os": "other-64",
                "nic_type": "e1000",
                "scsi_controller": "lsilogic",
                "firmware": "bios",
                "hw_version": "13",
                "power_on": False,
                "cpu_hotplug": False,
                "memory_hotplug": False,
                "hardware_virtualization": False,
                "pci_passthrough_devices": [],
            },
        }

@router.post("/{host_name}/create", summary="Provision New VM")
def create_vm_endpoint(
    request, host_name: str, datastore: str = None, name: str = None,
    ram: int = 2048, cpu: int = 2, disk_size_gb: int = 16, disk_type: str = "thin",
    guest_os: str = "other-64", network_name: str = "VM Network", nic_type: str = "e1000",
    scsi_controller: str = "lsilogic", firmware: str = "bios", hw_version: str = "13",
    power_on: bool = False, cd_iso_path: str = "",
    cpu_hotplug: bool = False, memory_hotplug: bool = False,
    hardware_virtualization: bool = False,
):
    payload = {}
    if request.body:
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}

    def _pick(key, current):
        return payload.get(key, current)

    datastore = _pick("datastore", datastore)
    name = _pick("name", name)
    try:
        ram = int(_pick("ram", ram) or 2048)
        cpu = int(_pick("cpu", cpu) or 2)
        disk_size_gb = int(_pick("disk_size_gb", disk_size_gb) or 16)
    except (TypeError, ValueError):
        return {"status": "error", "message": "CPU, RAM, and disk size must be numeric values."}
    disk_type = str(_pick("disk_type", disk_type) or "thin")
    guest_os = str(_pick("guest_os", guest_os) or "other-64")
    network_name = str(_pick("network_name", network_name) or "VM Network")
    nic_type = str(_pick("nic_type", nic_type) or "e1000")
    scsi_controller = str(_pick("scsi_controller", scsi_controller) or "lsilogic")
    firmware = str(_pick("firmware", firmware) or "bios")
    hw_version = str(_pick("hw_version", hw_version) or "13")
    power_on_raw = _pick("power_on", power_on)
    power_on = power_on_raw.lower() in {"1", "true", "yes", "on"} if isinstance(power_on_raw, str) else bool(power_on_raw)
    cpu_hotplug = _to_bool(_pick("cpu_hotplug", cpu_hotplug), False)
    memory_hotplug = _to_bool(_pick("memory_hotplug", memory_hotplug), False)
    hardware_virtualization = _to_bool(_pick("hardware_virtualization", hardware_virtualization), False)
    cd_iso_path = str(_pick("cd_iso_path", cd_iso_path) or "")
    extra_disks = payload.get("extra_disks", []) or []
    extra_nics = payload.get("extra_nics", []) or []
    pci_passthrough_devices = payload.get("pci_passthrough_devices", []) or []

    if not datastore or not name:
        return {"status": "error", "message": "Both datastore and name are required."}
    if cpu < 1 or ram < 256 or disk_size_gb < 1:
        return {"status": "error", "message": "Invalid sizing. CPU>=1, RAM>=256MB, Disk>=1GB required."}

    host_obj = get_host_obj(host_name, require_active=True)

    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        try:
            with get_conn(host_name) as conn:
                result = kvm_manage.create_vm(
                    conn,
                    datastore=str(datastore),
                    vm_name=str(name),
                    ram_mb=int(ram),
                    cpu_count=int(cpu),
                    disk_size_gb=int(disk_size_gb),
                    network_name=str(network_name),
                    nic_type=str(nic_type),
                    power_on=bool(power_on),
                )
        except FileExistsError as exc:
            return {"status": "error", "message": str(exc)}
        except (ValueError, RuntimeError) as exc:
            return {"status": "error", "message": str(exc)[:500]}
        except Exception as exc:
            return {"status": "error", "message": f"Unexpected error creating KVM VM: {str(exc)[:300]}"}

        bust_vm_cache(host_name)
        sync_vms_for_host(host_obj)
        vm_obj = VirtualMachine.objects.filter(host=host_obj, name=name).order_by("-updated_at").first()
        if vm_obj:
            try:
                broadcast_vm_created(vm_obj)
            except Exception:
                pass

        return {
            "status": "created",
            "output": str(result).strip(),
            "requested_config": {
                "name": name,
                "datastore": datastore,
                "cpu": cpu,
                "ram": ram,
                "disk_size_gb": disk_size_gb,
                "disk_type": disk_type,
                "guest_os": guest_os,
                "network_name": network_name,
                "nic_type": nic_type,
                "scsi_controller": scsi_controller,
                "firmware": firmware,
                "hw_version": hw_version,
                "power_on": power_on,
            },
        }

    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        try:
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                nextid_response = conn._request("GET", "/cluster/nextid")
                
                # Handle various response formats: int, str, or dict
                if isinstance(nextid_response, dict):
                    vmid = int(nextid_response.get("data", nextid_response))
                else:
                    vmid = int(nextid_response)

                ostype = str(guest_os or "l26")
                if ostype in {"other-64", "other-32", "otherGuest", "otherGuest64"}:
                    ostype = "other"
                # Validate ostype is valid for Proxmox
                valid_ostypes = {
                    "l26", "l24", "w2k", "w2k3", "w2k8", "wvista", "win7", "win8", 
                    "win10", "win11", "l24_debian", "solaris", "other"
                }
                if ostype not in valid_ostypes:
                    ostype = "l26"  # Default to Linux 6.x

                scsihw_raw = str(scsi_controller or "virtio-scsi-pci").lower()
                if scsihw_raw in {"lsilogic", "lsisas1068", "pvscsi"}:
                    scsihw = "virtio-scsi-pci"
                else:
                    scsihw = scsihw_raw or "virtio-scsi-pci"

                bios_raw = str(firmware or "seabios").lower()
                bios = "ovmf" if bios_raw in {"efi", "uefi", "ovmf"} else "seabios"
                net_model = nic_type or "virtio"

                # Get available networks on the node
                available_networks = conn.list_network(node) or []
                bridges = [
                    str(n.get("iface") or "")
                    for n in available_networks
                    if str(n.get("type") or "").lower() == "bridge"
                ]

                if not network_name or network_name == "VM Network":
                    network_name = bridges[0] if bridges else "vmbr0"
                elif network_name not in bridges:
                    # If specified network doesn't exist, warn and use first bridge
                    print(f"⚠️  Network '{network_name}' not found. Available: {bridges}. Using '{bridges[0] if bridges else 'vmbr0'}'")
                    network_name = bridges[0] if bridges else "vmbr0"

                # Validate storage exists
                available_storages = conn.list_storage(node) or []
                storage_ids = [str(s.get("storage") or "") for s in available_storages]
                if datastore not in storage_ids:
                    return {"status": "error", "message": f"Storage '{datastore}' not found on node '{node}'. Available: {storage_ids}"}

                disk_slot_prefix = "scsi"
                disk_entries = [f"{datastore}:{int(disk_size_gb)}"]
                for idx, extra_disk in enumerate(extra_disks or [], start=1):
                    if idx > 29:
                        break
                    ed_size = int(extra_disk.get("size_gb", 16) or 16)
                    ed_store = str(extra_disk.get("datastore") or datastore)
                    if ed_size < 1:
                        ed_size = 1
                    disk_entries.append(f"{ed_store}:{int(ed_size)}")

                net_entries = [f"{net_model},bridge={network_name}"]
                for idx, extra_nic in enumerate(extra_nics or [], start=1):
                    if idx > 29:
                        break
                    en_model = str(extra_nic.get("type") or net_model)
                    en_bridge = str(extra_nic.get("network") or network_name)
                    net_entries.append(f"{en_model},bridge={en_bridge}")

                create_payload = {
                    "vmid": int(vmid),
                    "name": str(name),
                    "memory": int(ram),
                    "cores": max(int(cpu), 1),
                    "sockets": 1,
                    "ostype": ostype,
                    "scsihw": scsihw,
                    "bios": bios,
                    "agent": "enabled=1",
                }

                # Proxmox hotplug is a comma-separated list of features.
                hotplug_flags = []
                if cpu_hotplug:
                    hotplug_flags.append("cpu")
                if memory_hotplug:
                    hotplug_flags.append("memory")
                if pci_passthrough_devices:
                    hotplug_flags.append("pci")
                if hotplug_flags:
                    create_payload["hotplug"] = ",".join(hotplug_flags)

                for disk_idx, disk_value in enumerate(disk_entries):
                    create_payload[f"scsi{disk_idx}"] = disk_value
                for nic_idx, nic_value in enumerate(net_entries):
                    create_payload[f"net{nic_idx}"] = nic_value

                if cd_iso_path:
                    create_payload["ide2"] = f"{datastore}:iso/{cd_iso_path.split('/')[-1]},media=cdrom"

                for pci_idx, pci_id in enumerate(pci_passthrough_devices):
                    if pci_idx > 15:
                        break
                    if not pci_id:
                        continue
                    create_payload[f"hostpci{pci_idx}"] = str(pci_id)
                # Log the payload for debugging
                print(f"📋 Creating Proxmox VM with payload: {create_payload}")


                conn.vm_create(node, create_payload)

                if power_on:
                    conn.vm_power(node, vmid, "start")

        except FileExistsError as exc:
            return {"status": "error", "message": str(exc)}
        except (ValueError, RuntimeError) as exc:
            return {"status": "error", "message": str(exc)[:500]}
        except Exception as exc:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ Proxmox VM creation failed:\n{error_details}")
            return {"status": "error", "message": f"Unexpected error creating Proxmox VM: {str(exc)}", "debug": error_details[:500]}

        # Give Proxmox a moment to index the new VM before syncing
        import time
        time.sleep(1)
        
        bust_vm_cache(host_name)
        sync_vms_for_host(host_obj)
        vm_obj = VirtualMachine.objects.filter(host=host_obj, name=name).order_by("-updated_at").first()
        if vm_obj:
            try:
                broadcast_vm_created(vm_obj)
            except Exception:
                pass

        return {
            "status": "created",
            "output": f"Proxmox VM '{name}' created on node '{node}'",
            "requested_config": {
                "name": name,
                "datastore": datastore,
                "cpu": cpu,
                "ram": ram,
                "disk_size_gb": disk_size_gb,
                "disk_type": disk_type,
                "guest_os": guest_os,
                "network_name": network_name,
                "nic_type": nic_type,
                "scsi_controller": scsi_controller,
                "firmware": firmware,
                "hw_version": hw_version,
                "power_on": power_on,
                "cd_iso_path": cd_iso_path,
                "cpu_hotplug": cpu_hotplug,
                "memory_hotplug": memory_hotplug,
                "hardware_virtualization": hardware_virtualization,
                "pci_passthrough_devices": pci_passthrough_devices,
            },
        }

    try:
        with get_conn(host_name) as conn:
            result, power_on_warning = vm_create.create_vm(
                conn, datastore=datastore, vm_name=name, ram_mb=ram, cpu_count=cpu,
                disk_size_gb=disk_size_gb, disk_type=disk_type, guest_os=guest_os,
                network_name=network_name, nic_type=nic_type, scsi_controller=scsi_controller,
                firmware=firmware, hw_version=hw_version, power_on=power_on,
                cd_iso_path=cd_iso_path, extra_disks=extra_disks, extra_nics=extra_nics,
                cpu_hotplug=cpu_hotplug, memory_hotplug=memory_hotplug,
                hardware_virtualization=hardware_virtualization,
                pci_passthrough_devices=pci_passthrough_devices,
            )
    except FileExistsError as exc:
        return {"status": "error", "message": str(exc)}
    except (ValueError, RuntimeError) as exc:
        return {"status": "error", "message": str(exc)[:500]}
    except Exception as exc:
        return {"status": "error", "message": f"Unexpected error creating VM: {str(exc)[:300]}"}

    bust_vm_cache(host_name)
    sync_vms_for_host(host_obj)
    vm_obj = VirtualMachine.objects.filter(host=host_obj, name=name).order_by("-updated_at").first()
    if vm_obj:
        try:
            broadcast_vm_created(vm_obj)
        except Exception:
            pass

    response: dict = {
        "status": "created",
        "output": str(result).strip(),
        "requested_config": {
            "name": name, "datastore": datastore, "cpu": cpu, "ram": ram,
            "disk_size_gb": disk_size_gb, "disk_type": disk_type, "guest_os": guest_os,
            "network_name": network_name, "nic_type": nic_type, "scsi_controller": scsi_controller,
            "firmware": firmware, "hw_version": hw_version, "power_on": power_on, "cd_iso_path": cd_iso_path,
            "cpu_hotplug": cpu_hotplug, "memory_hotplug": memory_hotplug,
            "hardware_virtualization": hardware_virtualization,
            "pci_passthrough_devices": pci_passthrough_devices,
        },
    }
    if power_on_warning:
        response["status"] = "created_with_warning"
        response["warning"] = f"VM created successfully but could not power on: {power_on_warning}"
    return response

@router.post("/{host_name}/deploy-ova", summary="Deploy OVA to ESXi")
def deploy_ova_endpoint(request, host_name: str, datastore: str, vm_name: str = "", file: UploadedFile = File(...)):
    host_obj = get_host_obj(host_name, require_active=True)
    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        return {"status": "error", "message": "OVA deploy is not supported for Proxmox hosts. Use the Proxmox web UI to import disk images."}
    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        return {"status": "error", "message": "OVA deploy is not supported for KVM hosts through this endpoint yet."}

    filename = posixpath.basename(file.name or "upload.ova")
    if not filename.lower().endswith((".ova", ".ovf")):
        return {"status": "error", "message": "Only .ova and .ovf files are supported."}

    ova_vm_name = (vm_name or "").strip() or filename.rsplit(".", 1)[0]
    remote_tmp = f"/vmfs/volumes/{datastore}/{filename}"

    # Phase 1 (in-request): SFTP-upload the file to the host
    try:
        with get_conn(host_name) as upload_conn:
            upload_conn.upload_file(file, remote_tmp)
    except FileExistsError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return {"status": "error", "message": f"Upload to host failed: {str(exc)[:400]}"}

    # Phase 2 (background): extract TAR, convert VMDKs, register VM
    def _do_deploy() -> None:
        try:
            with get_conn(host_name) as deploy_conn:
                from lib.vms.create import deploy_ova
                deploy_ova(deploy_conn, datastore, remote_tmp, vm_name=ova_vm_name)
            bust_vm_cache(host_name)
            sync_vms_for_host(host_obj)
            vm_obj = VirtualMachine.objects.filter(host=host_obj, name=ova_vm_name).order_by("-updated_at").first()
            if vm_obj:
                try:
                    broadcast_vm_created(vm_obj)
                except Exception:
                    pass
        except Exception:
            pass  # Errors visible in server logs; VM list stays unchanged on failure

    threading.Thread(target=_do_deploy, daemon=True).start()

    return {
        "status": "processing",
        "message": f"'{filename}' transferred to {host_name}. Extraction and registration is running in the background — the VM list will update automatically when complete.",
        "remote_path": remote_tmp,
        "vm_name": ova_vm_name,
    }


@router.get("/{host_name}/register/browse", summary="Browse Datastore for VMX Files")
def browse_vmx_for_register(request, host_name: str, path: str = "/vmfs/volumes"):
    host_obj = get_host_obj(host_name, require_active=True)
    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        try:
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                vms = conn.list_vms(node)

            entries = []
            for vm in vms:
                vmid = str(vm.get("vmid") or "").strip()
                name = str(vm.get("name") or f"VM {vmid}").strip()
                if not vmid:
                    continue
                entries.append(
                    {
                        "name": f"{name} (VMID {vmid})",
                        "path": vmid,
                        "is_dir": False,
                        "kind": "vmid",
                    }
                )

            entries.sort(key=lambda e: e["name"].lower())
            return {"status": "success", "path": "/proxmox/vms", "entries": entries}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:500]}

    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        try:
            with get_conn(host_name) as conn:
                rows = kvm_manage.list_vms_with_stats(conn)

            entries = [
                {
                    "name": f"{row.get('vm_name')} ({row.get('vmid')})",
                    "path": str(row.get("vmid") or ""),
                    "is_dir": False,
                    "kind": "domain",
                }
                for row in rows
                if row.get("vmid")
            ]
            entries.sort(key=lambda e: e["name"].lower())
            return {"status": "success", "path": "/kvm/domains", "entries": entries}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:500]}

    normalized = posixpath.normpath(path)
    if not normalized.startswith("/vmfs/volumes"):
        return {"status": "error", "message": "Browsing is restricted to /vmfs/volumes"}
    try:
        with get_conn(host_name) as conn:
            target = normalized.rstrip("/") + "/"
            raw = conn.run(f"ls -1Ap '{target}'")
            entries = []
            for line in raw.splitlines():
                name = line.strip()
                if not name:
                    continue
                is_dir = name.endswith("/")
                clean_name = name.rstrip("/")
                full_path = f"{normalized.rstrip('/')}/{clean_name}"
                if is_dir:
                    entries.append({"name": clean_name, "path": full_path, "is_dir": True, "kind": "dir"})
                elif clean_name.lower().endswith(".vmx"):
                    entries.append({"name": clean_name, "path": full_path, "is_dir": False, "kind": "vmx"})
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            return {"status": "success", "path": normalized, "entries": entries}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:500]}


@router.post("/{host_name}/register", summary="Register VM from Existing VMX")
def register_vm_from_vmx_endpoint(request, host_name: str, vmx_path: str):
    host_obj = get_host_obj(host_name, require_active=True)
    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        vmid = str(vmx_path or "").strip()
        if not vmid or not vmid.isdigit():
            return {"status": "error", "message": "For Proxmox registration, vmx_path must be a numeric VMID."}

        try:
            sync_vms_for_host(host_obj)
            vm_obj = VirtualMachine.objects.filter(host=host_obj, vmid=vmid).order_by("-updated_at").first()
            if not vm_obj:
                return {
                    "status": "error",
                    "message": f"VMID {vmid} not found on host after sync.",
                }
            bust_vm_cache(host_name)
            return {
                "status": "registered",
                "message": f"Registered Proxmox VM '{vm_obj.name}' (VMID {vmid})",
                "vmid": vmid,
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:500]}

    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        vmid = str(vmx_path or "").strip()
        if not vmid:
            return {"status": "error", "message": "For KVM registration, vmx_path must be a libvirt domain name."}
        try:
            sync_vms_for_host(host_obj)
            vm_obj = VirtualMachine.objects.filter(host=host_obj, vmid=vmid).order_by("-updated_at").first()
            if not vm_obj:
                return {"status": "error", "message": f"Domain '{vmid}' not found on host after sync."}
            bust_vm_cache(host_name)
            return {"status": "registered", "message": f"Registered KVM VM '{vm_obj.name}'", "vmid": vmid}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:500]}

    normalized_vmx = posixpath.normpath(vmx_path)
    if not normalized_vmx.startswith("/vmfs/volumes"):
        return {"status": "error", "message": "vmx_path must be under /vmfs/volumes"}
    if not normalized_vmx.lower().endswith(".vmx"):
        return {"status": "error", "message": "vmx_path must point to a .vmx file"}

    try:
        with get_conn(host_name) as conn:
            result = vm_manage.register_vm(conn, normalized_vmx)
            if isinstance(result, str) and result.startswith("Error"):
                return {"status": "error", "message": result[:500]}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:500]}

    bust_vm_cache(host_name)
    sync_vms_for_host(host_obj)
    vm_obj = VirtualMachine.objects.filter(host=host_obj, vmx_path=normalized_vmx).order_by("-updated_at").first()
    if vm_obj:
        try:
            broadcast_vm_created(vm_obj)
        except Exception:
            pass
    return {"status": "registered", "message": f"Registered VM from {normalized_vmx}", "vmx_path": normalized_vmx}


@router.get("/{host_name}/{vmid}/hardware", summary="Get VM Hardware (NICs + Disks from VMX)")
def get_vm_hardware_endpoint(request, host_name: str, vmid: str):
    host_obj = get_host_obj(host_name, require_active=True)

    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        try:
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                status = conn.get_vm_status(node, vmid)
                config = conn.get_vm_config(node, vmid)

            if not status:
                return {"status": "error", "message": f"VM {vmid} not found"}

            nics = []
            disks = []
            cdrom = None

            for key, value in config.items():
                if key.startswith("net"):
                    idx = int(key.replace("net", "") or 0)
                    raw = str(value)
                    nic_type = raw.split(",", 1)[0] if raw else "virtio"
                    network = "vmbr0"
                    for token in raw.split(","):
                        token = token.strip()
                        if token.startswith("bridge="):
                            network = token.split("=", 1)[1]
                            break
                    nics.append({
                        "index": idx,
                        "network": network,
                        "type": nic_type,
                        "mac": "--",
                        "connected": True,
                    })
                elif key.startswith(("scsi", "virtio", "sata", "ide")):
                    unit_digits = "".join(ch for ch in key if ch.isdigit())
                    unit = int(unit_digits) if unit_digits else 0
                    raw = str(value)
                    if "media=cdrom" in raw:
                        cdrom = {"device": key, "iso": raw}
                        continue
                    size_gb = 0
                    for token in raw.split(","):
                        token = token.strip().lower()
                        if token.startswith("size="):
                            sz = token.split("=", 1)[1]
                            if sz.endswith("g"):
                                size_gb = int(float(sz[:-1]))
                            elif sz.endswith("m"):
                                size_gb = max(1, int(float(sz[:-1]) / 1024))
                    disks.append({
                        "unit": unit,
                        "label": key,
                        "file": raw,
                        "size_gb": size_gb,
                    })

            hotplug_raw = str(config.get("hotplug") or "")
            hotplug_parts = {p.strip().lower() for p in hotplug_raw.split(",") if p.strip()}
            pci_passthrough = []
            for key, value in config.items():
                if not key.startswith("hostpci"):
                    continue
                slot_str = key.replace("hostpci", "")
                try:
                    slot = int(slot_str)
                except ValueError:
                    continue
                pci_passthrough.append({
                    "slot": slot,
                    "id": str(value),
                    "label": str(value),
                })
            pci_passthrough.sort(key=lambda x: x["slot"])

            nics.sort(key=lambda x: x["index"])
            disks.sort(key=lambda x: x["unit"])
            power_state = "poweredOn" if str(status.get("status", "")).lower() == "running" else "poweredOff"

            return {
                "status": "success",
                "power_state": power_state,
                "nics": nics,
                "disks": disks,
                "cdrom": cdrom,
                "cpu_hotplug": "cpu" in hotplug_parts,
                "memory_hotplug": "memory" in hotplug_parts,
                "hardware_virtualization": str(config.get("kvm", "1")) not in {"0", "false", "False"},
                "pci_passthrough": pci_passthrough,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        try:
            with get_conn(host_name) as conn:
                return kvm_manage.get_vm_hardware(conn, vmid)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    try:
        with get_conn(host_name) as conn:
            vms = vm_modify.list_vms_summary(conn)
            vmx_path = next((vm["vmx"] for vm in vms if vm["vmid"] == vmid), None)
            if not vmx_path:
                return {"status": "error", "message": f"VM {vmid} not found"}
            data = vm_modify.get_vm_hardware(conn, vmid, vmx_path)

            vmx = vm_modify.get_vmx_content(conn, vmx_path)
            data["cpu_hotplug"] = str(vmx.get("vcpu.hotadd", "FALSE")).upper() == "TRUE"
            data["memory_hotplug"] = str(vmx.get("mem.hotadd", "FALSE")).upper() == "TRUE"
            data["hardware_virtualization"] = str(vmx.get("vhv.enable", "FALSE")).upper() == "TRUE"

            pci_passthrough = []
            for key, value in vmx.items():
                match = re.match(r"^pciPassthru(\d+)\.id$", key)
                if not match:
                    continue
                slot = int(match.group(1))
                present = str(vmx.get(f"pciPassthru{slot}.present", "FALSE")).upper() == "TRUE"
                if not present:
                    continue
                pci_passthrough.append({
                    "slot": slot,
                    "id": str(value),
                    "label": str(value),
                })
            pci_passthrough.sort(key=lambda x: x["slot"])
            data["pci_passthrough"] = pci_passthrough

            return data
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/{host_name}/{vmid}/modify", summary="Modify VM Hardware")
def modify_vm_endpoint(
    request, host_name: str, vmid: str, modification: str, value=None,
    cpu: int = None, memory: int = None, disk_size: int = None, disk_name: str = None,
    datastore: str = None, network_name: str = "VM Network", adapter_type: str = "e1000",
    guest_os: str = None, hw_version: str = None, disk_unit: int = None,
    nic_number: int = None, iso_path: str = None,
    enabled: str = None, pci_id: str = None, pci_slot: int = None,
):
    host_obj = get_host_obj(host_name, require_active=True)

    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        # Keep UI stable: return explicit unsupported instead of falling into ESXi-only logic.
        return {
            "status": "error",
            "message": "KVM hardware modification is not yet implemented in this endpoint.",
        }

    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        try:
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                vm_status = conn.get_vm_status(node, vmid)
                if not vm_status:
                    return {"status": "error", "message": f"VM {vmid} not found"}

                def _parse_proxmox_hotplug(raw_value):
                    raw = str(raw_value or "").strip().lower()
                    if raw == "1":
                        return {"network", "disk", "usb", "cpu", "memory", "pci"}
                    return {p.strip() for p in raw.split(",") if p.strip() and p.strip() != "0"}

                update_payload = None
                if modification == "cpu":
                    if cpu is None:
                        return {"status": "error", "message": "cpu parameter required"}
                    update_payload = {"cores": int(cpu)}
                elif modification == "memory":
                    if memory is None:
                        return {"status": "error", "message": "memory parameter required"}
                    update_payload = {"memory": int(memory)}
                elif modification == "guest_os":
                    if guest_os is None:
                        return {"status": "error", "message": "guest_os parameter required"}
                    update_payload = {"ostype": str(guest_os)}
                elif modification == "cpu_hotplug":
                    is_enabled = _to_bool(enabled if enabled is not None else value, False)
                    cfg = conn.get_vm_config(node, vmid)
                    parts = _parse_proxmox_hotplug(cfg.get("hotplug"))
                    if is_enabled:
                        parts.add("cpu")
                    else:
                        parts.discard("cpu")
                    update_payload = {"hotplug": ",".join(sorted(parts)) if parts else "0"}
                elif modification == "memory_hotplug":
                    is_enabled = _to_bool(enabled if enabled is not None else value, False)
                    cfg = conn.get_vm_config(node, vmid)
                    parts = _parse_proxmox_hotplug(cfg.get("hotplug"))
                    if is_enabled:
                        parts.add("memory")
                    else:
                        parts.discard("memory")
                    update_payload = {"hotplug": ",".join(sorted(parts)) if parts else "0"}
                elif modification == "hardware_virtualization":
                    is_enabled = _to_bool(enabled if enabled is not None else value, False)
                    update_payload = {"kvm": 1 if is_enabled else 0}
                elif modification == "hw_version":
                    if hw_version is None:
                        return {"status": "error", "message": "hw_version parameter required"}
                    update_payload = {"machine": f"pc-q35-{hw_version}"}
                elif modification == "add_network":
                    cfg = conn.get_vm_config(node, vmid)
                    free_slot = None
                    for idx in range(0, 32):
                        if f"net{idx}" not in cfg:
                            free_slot = idx
                            break
                    if free_slot is None:
                        return {"status": "error", "message": "No free network adapter slot available"}
                    net_model = adapter_type or "virtio"
                    bridge = network_name or "vmbr0"
                    update_payload = {f"net{free_slot}": f"{net_model},bridge={bridge}"}
                elif modification == "remove_network":
                    if nic_number is None:
                        return {"status": "error", "message": "nic_number parameter required"}
                    update_payload = {"delete": f"net{int(nic_number)}"}
                elif modification == "add_disk":
                    if disk_size is None:
                        return {"status": "error", "message": "disk_size parameter required"}
                    cfg = conn.get_vm_config(node, vmid)
                    free_slot = None
                    for idx in range(0, 32):
                        if f"scsi{idx}" not in cfg:
                            free_slot = idx
                            break
                    if free_slot is None:
                        return {"status": "error", "message": "No free disk slot available"}
                    disk_store = datastore or "local-lvm"
                    update_payload = {f"scsi{free_slot}": f"{disk_store}:{int(disk_size)}"}
                elif modification == "remove_disk":
                    if disk_unit is None:
                        return {"status": "error", "message": "disk_unit parameter required"}
                    update_payload = {"delete": f"scsi{int(disk_unit)}"}
                elif modification == "resize_disk":
                    if disk_unit is None or disk_size is None:
                        return {"status": "error", "message": "disk_unit and disk_size parameters required"}
                    conn.create(
                        f"/nodes/{node}/qemu/{vmid}/resize",
                        data={"disk": f"scsi{int(disk_unit)}", "size": f"{int(disk_size)}G"},
                    )
                    bust_vm_cache(host_name, vmid)
                    return {
                        "status": "success",
                        "message": f"Disk scsi{int(disk_unit)} resized to {int(disk_size)}G",
                    }
                elif modification == "mount_iso":
                    if not iso_path:
                        return {"status": "error", "message": "iso_path parameter required"}
                    iso_name = posixpath.basename(str(iso_path))
                    cfg = conn.get_vm_config(node, vmid)
                    cdrom_slot = "ide2" if "ide2" not in cfg else "ide0"
                    update_payload = {cdrom_slot: f"local:iso/{iso_name},media=cdrom"}
                elif modification == "eject_iso":
                    cfg = conn.get_vm_config(node, vmid)
                    target_cdrom = "ide2" if "ide2" in cfg else "ide0"
                    update_payload = {"delete": target_cdrom}
                elif modification == "add_pci_passthrough":
                    if not pci_id:
                        return {"status": "error", "message": "pci_id parameter required"}
                    cfg = conn.get_vm_config(node, vmid)
                    slot = None
                    for idx in range(0, 16):
                        if f"hostpci{idx}" not in cfg:
                            slot = idx
                            break
                    if slot is None:
                        return {"status": "error", "message": "No free hostpci slot available"}
                    update_payload = {f"hostpci{slot}": str(pci_id)}
                elif modification == "remove_pci_passthrough":
                    if pci_slot is None:
                        return {"status": "error", "message": "pci_slot parameter required"}
                    update_payload = {"delete": f"hostpci{int(pci_slot)}"}
                else:
                    return {"status": "error", "message": f"Unsupported Proxmox modification: {modification}"}

                if update_payload:
                    conn.vm_update_config(node, vmid, update_payload)

            bust_vm_cache(host_name, vmid)
            return {"status": "success", "message": f"Applied '{modification}' to VM {vmid}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    try:
        with get_conn(host_name) as conn:
            vms = vm_modify.list_vms_summary(conn)
            vmx_path = next((vm["vmx"] for vm in vms if vm["vmid"] == vmid), None)
            if not vmx_path:
                return {"status": "error", "message": f"VM {vmid} not found"}

            result = None
            if modification == "cpu":
                if cpu is None: return {"status": "error", "message": "cpu parameter required"}
                result = vm_modify.modify_cpu(conn, vmid, vmx_path, cpu)
            elif modification == "memory":
                if memory is None: return {"status": "error", "message": "memory parameter required"}
                result = vm_modify.modify_memory(conn, vmid, vmx_path, memory)
            elif modification == "add_disk":
                if disk_size is None: return {"status": "error", "message": "disk_size parameter required"}
                result = vm_modify.add_disk(conn, vmid, vmx_path, disk_size, disk_name, datastore)
            elif modification == "remove_disk":
                if disk_unit is None: return {"status": "error", "message": "disk_unit parameter required"}
                result = vm_modify.remove_disk(conn, vmid, vmx_path, disk_unit)
            elif modification == "resize_disk":
                if disk_unit is None or disk_size is None: return {"status": "error", "message": "disk_unit and disk_size parameters required"}
                result = vm_modify.resize_disk(conn, vmid, vmx_path, disk_unit, disk_size)
            elif modification == "add_network":
                result = vm_modify.add_network(conn, vmid, vmx_path, network_name, adapter_type)
            elif modification == "remove_network":
                if nic_number is None: return {"status": "error", "message": "nic_number parameter required"}
                result = vm_modify.remove_network(conn, vmid, vmx_path, nic_number)
            elif modification == "hw_version":
                if hw_version is None: return {"status": "error", "message": "hw_version parameter required"}
                result = vm_modify.modify_vm_hardware_version(conn, vmid, vmx_path, hw_version)
            elif modification == "guest_os":
                if guest_os is None: return {"status": "error", "message": "guest_os parameter required"}
                result = vm_modify.modify_guest_os(conn, vmid, vmx_path, guest_os)
            elif modification == "cpu_hotplug":
                result = vm_modify.set_cpu_hotplug(conn, vmid, vmx_path, _to_bool(enabled if enabled is not None else value, False))
            elif modification == "memory_hotplug":
                result = vm_modify.set_memory_hotplug(conn, vmid, vmx_path, _to_bool(enabled if enabled is not None else value, False))
            elif modification == "hardware_virtualization":
                result = vm_modify.set_hardware_virtualization(conn, vmid, vmx_path, _to_bool(enabled if enabled is not None else value, False))
            elif modification == "mount_iso":
                if not iso_path: return {"status": "error", "message": "iso_path parameter required"}
                result = vm_modify.mount_iso(conn, vmid, vmx_path, iso_path)
            elif modification == "eject_iso":
                result = vm_modify.eject_iso(conn, vmid, vmx_path)
            elif modification == "add_pci_passthrough":
                if not pci_id: return {"status": "error", "message": "pci_id parameter required"}
                result = vm_modify.add_pci_passthrough(conn, vmid, vmx_path, pci_id)
            elif modification == "remove_pci_passthrough":
                if pci_slot is None: return {"status": "error", "message": "pci_slot parameter required"}
                result = vm_modify.remove_pci_passthrough(conn, vmid, vmx_path, pci_slot)
            else:
                return {"status": "error", "message": f"Unknown modification type: {modification}"}

            bust_vm_cache(host_name, vmid)
            return {**result, "vmx_path": vmx_path}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.delete("/{host_name}/{vmid}/unregister", summary="Remove from Inventory")
def unregister_vm_endpoint(request, host_name: str, vmid: str):
    host_obj = get_host_obj(host_name, require_active=True)

    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        # Proxmox has no ESXi-style inventory unregister; keep VM/config/disks on host.
        VirtualMachine.objects.filter(host=host_obj, vmid=vmid).delete()
        bust_vm_cache(host_name, vmid)
        return {"status": "unregistered", "vmid": vmid}

    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        try:
            with get_conn(host_name) as conn:
                kvm_manage.unregister_vm(conn, vmid)
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:500]}
        VirtualMachine.objects.filter(host=host_obj, vmid=vmid).delete()
        bust_vm_cache(host_name, vmid)
        return {"status": "unregistered", "vmid": vmid}

    with get_conn(host_name) as conn:
        vm_manage.unregister_vm(conn, vmid)
        bust_vm_cache(host_name, vmid)
        return {"status": "unregistered", "vmid": vmid}

@router.post("/{host_name}/{vmid}/restore-vmx", summary="Restore VMX from Backup")
def restore_vmx_endpoint(request, host_name: str, vmid: str):
    host_obj = get_host_obj(host_name, require_active=True)
    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        return {"status": "error", "message": "VMX restore is ESXi-only."}
    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        return {"status": "error", "message": "VMX restore is ESXi-only."}

    try:
        with get_conn(host_name) as conn:
            vms = vm_modify.list_vms_summary(conn)
            vmx_path = next((vm["vmx"] for vm in vms if vm["vmid"] == vmid), None)
            if not vmx_path:
                return {"status": "error", "message": f"VM {vmid} not found"}
            result = vm_modify.restore_vmx_backup(conn, vmx_path, vmid)
            bust_vm_cache(host_name, vmid)
            return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.delete("/{host_name}/{vmid}/delete", summary="Delete VM (Destroy All Files)")
def delete_vm_endpoint(request, host_name: str, vmid: str):
    host_obj = get_host_obj(host_name, require_active=True)

    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        try:
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                conn.vm_delete(node, vmid, purge=False, destroy_unreferenced_disks=True)
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:500]}

        VirtualMachine.objects.filter(host=host_obj, vmid=vmid).delete()
        bust_vm_cache(host_name, vmid)
        return {"status": "deleted", "vmid": vmid}

    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        try:
            with get_conn(host_name) as conn:
                kvm_manage.delete_vm(conn, vmid)
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:500]}

        VirtualMachine.objects.filter(host=host_obj, vmid=vmid).delete()
        bust_vm_cache(host_name, vmid)
        return {"status": "deleted", "vmid": vmid}

    try:
        with get_conn(host_name) as conn:
            result = vm_manage.destroy_vm(conn, vmid)
            if isinstance(result, str) and result.startswith("Error:"):
                return {"status": "error", "message": result[:500]}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:500]}

    VirtualMachine.objects.filter(host=host_obj, vmid=vmid).delete()
    bust_vm_cache(host_name, vmid)
    return {"status": "deleted", "vmid": vmid}

@router.post("/migrate", summary="Cross-Host Cold Migration")
def migrate_vm_endpoint(request, src_host: str, dest_host: str, vmid: str, vm_name: str, src_ds: str, dest_ds: str):
    src_host_obj = get_host_obj(src_host, require_active=True)
    dest_host_obj = get_host_obj(dest_host, require_active=True)
    if (
        src_host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE
        or dest_host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE
        or src_host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT
        or dest_host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT
    ):
        return {
            "status": "error",
            "message": "This migration endpoint is ESXi-only. Use vendor-native workflows for Proxmox/KVM hosts.",
        }

    with get_conn(src_host) as src_conn:
        with get_conn(dest_host) as dest_conn:
            result = vm_migrate.cold_migrate(src_conn, dest_conn, vmid, vm_name, src_ds, dest_ds, dest_conn.host)
            bust_vm_cache(src_host)
            bust_vm_cache(dest_host)
            return result
