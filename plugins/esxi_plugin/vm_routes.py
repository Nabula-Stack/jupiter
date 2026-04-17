import json
import logging
import os
import posixpath
import re
import shutil
import shlex
import tarfile
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
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
logger = logging.getLogger(__name__)

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
    """Best-effort parser for ESXi PCI devices."""
    try:
        if hasattr(conn, "list_pci_devices"):
            return conn.list_pci_devices() or []

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


def _is_esxi_api_mode(host_obj) -> bool:
    return (
        host_obj.hypervisor_type == Host.HYPERVISOR_VMWARE_ESXI
        and (getattr(host_obj, "esxi_connection_method", "") or Host.CONNECTION_SSH).lower() == Host.CONNECTION_API
    )


def _is_esxi_ssh_mode(host_obj) -> bool:
    return (
        host_obj.hypervisor_type == Host.HYPERVISOR_VMWARE_ESXI
        and (getattr(host_obj, "esxi_connection_method", "") or Host.CONNECTION_SSH).lower() == Host.CONNECTION_SSH
    )


def _build_esxi_ssh_conn(host_obj):
    """Build SSH connection for ESXi operations that are not available via API mode."""
    if not _is_esxi_ssh_mode(host_obj):
        raise RuntimeError(
            "This operation requires ESXi SSH connection mode. "
            "Set the host connection method to SSH to continue."
        )

    from plugins.esxi_ssh_plugin import build_esxi_ssh_connection

    return build_esxi_ssh_connection(host_obj)


def _ova_session_cache_key(session_id: str) -> str:
    return f"ninja:ova_import_session:{session_id}"


def _read_ovf_text_from_local_upload(local_path: str, filename: str) -> str:
    lower_name = str(filename or "").lower()
    if lower_name.endswith(".ovf"):
        with open(local_path, "rb") as fp:
            return fp.read().decode("utf-8", errors="ignore")

    try:
        with tarfile.open(local_path, "r:*") as tar:
            ovf_member = next((m for m in tar.getmembers() if m.name.lower().endswith(".ovf")), None)
            if not ovf_member:
                raise ValueError("Uploaded OVA does not contain an OVF descriptor")
            ovf_file = tar.extractfile(ovf_member)
            if ovf_file is None:
                raise ValueError("Failed to read OVF descriptor from OVA")
            return ovf_file.read().decode("utf-8", errors="ignore")
    except tarfile.ReadError:
        # Some clients mislabel plain OVF files as .ova; gracefully detect XML envelope.
        with open(local_path, "rb") as fp:
            head = fp.read(8192).decode("utf-8", errors="ignore")
        if "<Envelope" in head and "ovf" in head.lower():
            with open(local_path, "rb") as fp:
                return fp.read().decode("utf-8", errors="ignore")
        raise ValueError(
            "Uploaded file is not a valid OVA archive. Ensure you selected the original .ova/.ovf and re-upload."
        )


def _ovf_capacity_to_gb(capacity: str, units: str) -> float:
    try:
        cap = float(capacity)
    except (TypeError, ValueError):
        return 0.0

    u = str(units or "").strip().lower()
    if not u:
        return max(cap, 0.0)

    exp_match = re.search(r"2\^(\d+)", u)
    if "byte" in u and exp_match:
        mult = 2 ** int(exp_match.group(1))
        return round((cap * mult) / (1024 ** 3), 2)
    if "byte" in u:
        return round(cap / (1024 ** 3), 2)
    if "kb" in u:
        return round(cap / (1024 ** 2), 2)
    if "mb" in u:
        return round(cap / 1024, 2)
    if "tb" in u:
        return round(cap * 1024, 2)
    return round(cap, 2)


def _parse_ovf_defaults(ovf_text: str, fallback_name: str) -> dict:
    defaults = {
        "name": fallback_name,
        "cpu": 2,
        "ram_mb": 2048,
        "guest_os": "other-64",
        "firmware": "bios",
        "networks": [],
        "disks": [],
        "disk_files": [],
    }
    if not ovf_text:
        return defaults

    root = ET.fromstring(ovf_text)

    for elem in root.iter():
        if elem.tag.endswith("VirtualSystem"):
            for child in list(elem):
                if child.tag.endswith("Name") and (child.text or "").strip():
                    defaults["name"] = child.text.strip()
                    break
            break

    for elem in root.iter():
        if elem.tag.endswith("Network"):
            for k, v in elem.attrib.items():
                if k.lower().endswith("name") and v and v not in defaults["networks"]:
                    defaults["networks"].append(v)

    cpu_val = None
    mem_val = None
    for item in root.iter():
        if not item.tag.endswith("Item"):
            continue
        rtype = None
        qty = None
        for child in list(item):
            if child.tag.endswith("ResourceType"):
                rtype = (child.text or "").strip()
            elif child.tag.endswith("VirtualQuantity"):
                qty = (child.text or "").strip()
        if rtype == "3" and qty:
            try:
                cpu_val = max(1, int(float(qty)))
            except ValueError:
                pass
        elif rtype == "4" and qty:
            try:
                mem_val = max(256, int(float(qty)))
            except ValueError:
                pass
    if cpu_val:
        defaults["cpu"] = cpu_val
    if mem_val:
        defaults["ram_mb"] = mem_val

    file_refs = {}
    for elem in root.iter():
        if not elem.tag.endswith("File"):
            continue
        file_id = ""
        href = ""
        for key, val in elem.attrib.items():
            key_lower = key.lower()
            if key_lower.endswith("id"):
                file_id = str(val or "").strip()
            elif key_lower.endswith("href"):
                href = posixpath.basename(str(val or "").strip())
        if file_id and href:
            file_refs[file_id] = href

    idx = 0
    for elem in root.iter():
        if not elem.tag.endswith("Disk"):
            continue
        cap = elem.attrib.get("capacity")
        units = elem.attrib.get("capacityAllocationUnits", "")
        file_ref = ""
        for key, val in elem.attrib.items():
            if key.lower().endswith("fileref"):
                file_ref = str(val or "").strip()
                break
        size_gb = _ovf_capacity_to_gb(cap, units)
        if size_gb <= 0:
            size_gb = 16.0
        disk_file = file_refs.get(file_ref, "")
        if disk_file and disk_file not in defaults["disk_files"]:
            defaults["disk_files"].append(disk_file)
        defaults["disks"].append(
            {
                "label": elem.attrib.get("diskId") or f"Disk {idx + 1}",
                "size_gb": max(1, int(round(size_gb))),
                "file": disk_file,
            }
        )
        idx += 1

    if not defaults["disks"]:
        defaults["disks"] = [{"label": "Disk 1", "size_gb": 16}]

    return defaults


def _read_remote_vmdk_size_gb(conn, vmdk_path: str) -> int:
    try:
        header = conn.run(f"head -40 '{vmdk_path}'")
        if not header or str(header).startswith("Error:"):
            return 16
        match = re.search(r"RW\s+(\d+)", str(header))
        if not match:
            return 16
        sectors = int(match.group(1))
        size_gb = round((sectors * 512) / (1024 ** 3))
        return max(1, int(size_gb))
    except Exception:
        return 16


def _is_remote_vmdk_descriptor(conn, vmdk_path: str) -> bool:
    """Return True for attachable VMDK roots (descriptor or monolithic sparse), not split extents."""
    try:
        header = conn.run(f"head -20 '{vmdk_path}'")
        text = str(header or "")
        if text and not text.startswith("Error:"):
            if ("Disk DescriptorFile" in text) or ("ddb." in text):
                return True

        # Fallback: monolithic sparse VMDKs are valid roots but may not have a text descriptor.
        probe = conn.run(f"vmkfstools -q '{vmdk_path}'")
        probe_text = str(probe or "")
        if not probe_text:
            return True
        if probe_text.startswith("Error:"):
            return False
        lowered = probe_text.lower()
        if "not a virtual disk" in lowered or "invalid argument" in lowered:
            return False
        return True
    except Exception:
        return False


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
        # ESXi: use WebMKS ticket if available (API mode), otherwise fall back to direct URL (SSH mode)
        try:
            with get_conn(host_name) as conn:
                if hasattr(conn, "get_vm_webmks_ticket"):
                    ticket_result = conn.get_vm_webmks_ticket(vmid)
                    console_id = str(ticket_result.get("console_id") or vmid)
                    vm_name = str(ticket_result.get("vm_name") or vmid)
                    if ticket_result.get("status") == "success":
                        ticket = ticket_result.get("ticket", "")
                        # WebMKS console URL with ticket auth
                        console_url = f"https://{host_obj.ip_address}/ui/?vmId={console_id}&vmName={vm_name}#/console/{console_id}?ticket={ticket}"
                    else:
                        # Fallback to direct ESXi UI
                        console_url = f"https://{host_obj.ip_address}/ui/#/console/{console_id}"
                else:
                    # SSH mode: direct ESXi UI URL
                    console_url = f"https://{host_obj.ip_address}/ui/#/console/{vmid}"
        except Exception as e:
            # Fallback on error
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
                "reserve_all_cpu": False,
                "reserve_all_memory": False,
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
                "reserve_all_cpu": False,
                "reserve_all_memory": False,
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
                    "reserve_all_cpu": False,
                    "reserve_all_memory": False,
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
                    "reserve_all_cpu": False,
                    "reserve_all_memory": False,
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
                    "reserve_all_cpu": False,
                    "reserve_all_memory": False,
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
                    "reserve_all_cpu": False,
                    "reserve_all_memory": False,
                },
            }

    try:
        with get_conn(host_name) as conn:
            datastores = storage_manage.list_datastores(conn)
            portgroups = network_manage.list_portgroups(conn)
            pci_devices = _safe_list_esxi_pci_devices(conn)
            try:
                if hasattr(conn, "list_iso_files"):
                    isos = conn.list_iso_files() or []
                elif hasattr(conn, "run"):
                    iso_raw = conn.run("find /vmfs/volumes -maxdepth 4 -name '*.iso' 2>/dev/null")
                    isos = [l.strip() for l in iso_raw.splitlines() if l.strip().lower().endswith(".iso")]
                else:
                    isos = []
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
                    "reserve_all_cpu": True,
                    "reserve_all_memory": True,
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
                    "reserve_all_cpu": False,
                    "reserve_all_memory": False,
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
                "reserve_all_cpu": True,
                "reserve_all_memory": True,
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
                "reserve_all_cpu": False,
                "reserve_all_memory": False,
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
    reserve_all_cpu: bool = False,
    reserve_all_memory: bool = False,
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
    reserve_all_cpu = _to_bool(_pick("reserve_all_cpu", reserve_all_cpu), False)
    reserve_all_memory = _to_bool(_pick("reserve_all_memory", reserve_all_memory), False)
    cd_iso_path = str(_pick("cd_iso_path", cd_iso_path) or "")
    extra_disks = payload.get("extra_disks", []) or []
    extra_nics = payload.get("extra_nics", []) or []
    pci_passthrough_devices = payload.get("pci_passthrough_devices", []) or []
    source_ova_session_id = str(payload.get("source_ova_session_id") or "").strip()

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
            if source_ova_session_id:
                if host_obj.hypervisor_type != Host.HYPERVISOR_VMWARE_ESXI:
                    return {"status": "error", "message": "OVA import sessions are currently supported for ESXi hosts only."}

                session = cache.get(_ova_session_cache_key(source_ova_session_id))
                if not session:
                    return {
                        "status": "error",
                        "message": "OVA import session expired or not found. Upload the OVA again.",
                    }

                if str(session.get("host_name")) != str(host_name):
                    return {
                        "status": "error",
                        "message": "OVA import session host mismatch. Re-open the import wizard for this host.",
                    }
                if str(session.get("datastore")) != str(datastore):
                    return {
                        "status": "error",
                        "message": "Datastore differs from OVA import session. Use the pre-filled datastore or upload again.",
                    }

                # ── API mode: OVF already on datastore, use pyVmomi import ──
                if _is_esxi_api_mode(host_obj) and not session.get("api_ovf_source") and session.get("ovf_path"):
                    session["api_ovf_source"] = True

                if session.get("api_ovf_source"):
                    ovf_vmfs_path = str(session.get("ovf_path") or "")
                    if not ovf_vmfs_path:
                        return {"status": "error", "message": "Session missing OVF path."}
                    api_result = conn.import_ovf_from_datastore(
                        ovf_vmfs_path=ovf_vmfs_path,
                        vm_name=name,
                        datastore_name=datastore,
                        cpu_count=cpu,
                        ram_mb=ram,
                        guest_os=guest_os,
                        network_name=network_name,
                        nic_type=nic_type,
                        scsi_controller=scsi_controller,
                        firmware=firmware,
                        hw_version=hw_version,
                        disk_type=disk_type,
                        power_on=power_on,
                        extra_nics=extra_nics,
                    )
                    cache.delete(_ova_session_cache_key(source_ova_session_id))
                    bust_vm_cache(host_name)
                    sync_vms_for_host(host_obj)
                    vm_obj = VirtualMachine.objects.filter(host=host_obj, name=name).order_by("-updated_at").first()
                    if vm_obj:
                        try:
                            broadcast_vm_created(vm_obj)
                        except Exception:
                            pass
                    status_key = "created_with_warning" if api_result.get("warning") else "created"
                    response = {
                        "status": status_key,
                        "output": api_result.get("message", ""),
                        "requested_config": {
                            "name": name, "datastore": datastore, "cpu": cpu, "ram": ram,
                            "disk_type": disk_type, "network_name": network_name,
                            "nic_type": nic_type, "firmware": firmware,
                            "hw_version": hw_version, "power_on": power_on,
                            "source_ova_session_id": source_ova_session_id,
                        },
                    }
                    if api_result.get("warning"):
                        response["warning"] = api_result["warning"]
                    return response

                if _is_esxi_api_mode(host_obj):
                    return {
                        "status": "error",
                        "message": "OVA session deployment is SSH-only. Set ESXi connection method to SSH for this host.",
                    }

                staging_dir = str(session.get("staging_dir") or "").strip()
                if not staging_dir:
                    return {"status": "error", "message": "Invalid OVA import session payload."}

                if not hasattr(conn, "run"):
                    return {
                        "status": "error",
                        "message": "Selected ESXi connection cannot run SSH OVA deployment commands.",
                    }

                try:
                    result = vm_create.deploy_ova_from_session(
                        conn,
                        datastore=datastore,
                        session_dir=staging_dir,
                        disk_files=session.get("disk_files") or [],
                        vm_name=name,
                        cpu_count=cpu,
                        ram_mb=ram,
                        network_name=network_name,
                        nic_type=nic_type,
                        scsi_controller=scsi_controller,
                        guest_os=guest_os,
                        firmware=firmware,
                        hw_version=hw_version,
                        disk_type=disk_type,
                        extra_nics=extra_nics,
                        power_on=power_on,
                    )
                finally:
                    pass

                cache.delete(_ova_session_cache_key(source_ova_session_id))
                try:
                    with _build_esxi_ssh_conn(host_obj) as cleanup_conn:
                        cleanup_conn.run(f"rm -rf '{staging_dir}'", timeout=120)
                except Exception:
                    logger.warning("Failed to cleanup OVA session staging directory: %s", staging_dir)

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
                        "disk_type": disk_type,
                        "network_name": network_name,
                        "nic_type": nic_type,
                        "firmware": firmware,
                        "hw_version": hw_version,
                        "power_on": power_on,
                        "source_ova_session_id": source_ova_session_id,
                    },
                }

            # ESXi API mode: use pyVmomi CreateVM_Task
            if hasattr(conn, "create_vm"):
                api_result = conn.create_vm(
                    datastore_name=datastore, vm_name=name, ram_mb=ram, cpu_count=cpu,
                    disk_size_gb=disk_size_gb, disk_type=disk_type, guest_os=guest_os,
                    network_name=network_name, nic_type=nic_type, scsi_controller=scsi_controller,
                    firmware=firmware, hw_version=hw_version, power_on=power_on,
                    cd_iso_path=cd_iso_path, extra_disks=extra_disks, extra_nics=extra_nics,
                    cpu_hotplug=cpu_hotplug, memory_hotplug=memory_hotplug,
                    hardware_virtualization=hardware_virtualization,
                    pci_passthrough_devices=pci_passthrough_devices,
                    reserve_all_cpu=reserve_all_cpu,
                    reserve_all_memory=reserve_all_memory,
                )
                if (api_result or {}).get("status") == "error":
                    return {"status": "error", "message": api_result.get("message", "Create failed")}
                bust_vm_cache(host_name)
                sync_vms_for_host(host_obj)
                vm_obj = VirtualMachine.objects.filter(host=host_obj, name=name).order_by("-updated_at").first()
                if vm_obj:
                    try:
                        broadcast_vm_created(vm_obj)
                    except Exception:
                        pass
                response = {
                    "status": api_result.get("warning") and "created_with_warning" or "created",
                    "output": api_result.get("message", ""),
                    "requested_config": {
                        "name": name, "datastore": datastore, "cpu": cpu, "ram": ram,
                        "disk_size_gb": disk_size_gb, "disk_type": disk_type, "guest_os": guest_os,
                        "network_name": network_name, "nic_type": nic_type, "firmware": firmware,
                        "hw_version": hw_version, "power_on": power_on,
                        "cd_iso_path": cd_iso_path, "cpu_hotplug": cpu_hotplug,
                        "memory_hotplug": memory_hotplug,
                        "hardware_virtualization": hardware_virtualization,
                        "pci_passthrough_devices": pci_passthrough_devices,
                    },
                }
                if api_result.get("warning"):
                    response["warning"] = f"VM created but could not power on: {api_result['warning']}"
                return response

            result, power_on_warning = vm_create.create_vm(
                conn, datastore=datastore, vm_name=name, ram_mb=ram, cpu_count=cpu,
                disk_size_gb=disk_size_gb, disk_type=disk_type, guest_os=guest_os,
                network_name=network_name, nic_type=nic_type, scsi_controller=scsi_controller,
                firmware=firmware, hw_version=hw_version, power_on=power_on,
                cd_iso_path=cd_iso_path, extra_disks=extra_disks, extra_nics=extra_nics,
                cpu_hotplug=cpu_hotplug, memory_hotplug=memory_hotplug,
                hardware_virtualization=hardware_virtualization,
                pci_passthrough_devices=pci_passthrough_devices,
                reserve_all_cpu=reserve_all_cpu,
                reserve_all_memory=reserve_all_memory,
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
            "reserve_all_cpu": reserve_all_cpu,
            "reserve_all_memory": reserve_all_memory,
        },
    }
    if power_on_warning:
        response["status"] = "created_with_warning"
        response["warning"] = f"VM created successfully but could not power on: {power_on_warning}"
    return response

@router.get("/{host_name}/ova/session/{session_id}", summary="Get OVA Import Session Defaults")
def get_ova_session_endpoint(request, host_name: str, session_id: str):
    session = cache.get(_ova_session_cache_key(session_id))
    if not session:
        return {"status": "error", "message": "OVA import session expired or not found."}
    if str(session.get("host_name")) != str(host_name):
        return {"status": "error", "message": "OVA import session host mismatch."}
    return {
        "status": "success",
        "session_id": session_id,
        "host_name": session.get("host_name"),
        "datastore": session.get("datastore"),
        "prefill": session.get("prefill") or {},
        "expires_in_seconds": int(session.get("expires_in_seconds") or 3600),
    }


@router.post("/{host_name}/deploy-ova", summary="Prepare OVA Import Session")
def deploy_ova_endpoint(request, host_name: str, datastore: str, vm_name: str = "", file: UploadedFile = File(...)):
    host_obj = get_host_obj(host_name, require_active=True)
    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        return {"status": "error", "message": "OVA deploy is not supported for Proxmox hosts. Use the Proxmox web UI to import disk images."}
    if host_obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
        return {"status": "error", "message": "OVA deploy is not supported for KVM hosts through this endpoint yet."}

    filename = posixpath.basename(file.name or "upload.ova")
    sidecar_files = request.FILES.getlist("sidecar_files") if hasattr(request, "FILES") else []
    if not filename.lower().endswith((".ova", ".ovf")):
        return {"status": "error", "message": "Only .ova and .ovf files are supported."}
    if filename.lower().endswith(".ovf") and not sidecar_files:
        return {
            "status": "error",
            "message": "For OVF packages, upload the .ovf together with its .vmdk sidecar files. The .mf file is optional.",
        }

    requested_vm_name = (vm_name or "").strip()
    ova_vm_name = requested_vm_name or filename.rsplit(".", 1)[0]
    suffix = ".ovf" if filename.lower().endswith(".ovf") else ".ova"
    fd, local_tmp = tempfile.mkstemp(prefix="nebula-ova-", suffix=suffix)
    os.close(fd)

    try:
        bytes_written = 0
        with open(local_tmp, "wb") as temp_handle:
            for chunk in file.chunks():
                temp_handle.write(chunk)
                bytes_written += len(chunk)
        expected_size = int(getattr(file, "size", 0) or 0)
        if bytes_written <= 0:
            raise ValueError("Upload is empty")
        if expected_size > 0 and bytes_written != expected_size:
            raise ValueError(
                f"Upload appears incomplete ({bytes_written} of {expected_size} bytes). Please retry the upload."
            )
        ovf_text = _read_ovf_text_from_local_upload(local_tmp, filename)
        ovf_defaults = _parse_ovf_defaults(ovf_text, ova_vm_name)
    except Exception as exc:
        try:
            if os.path.exists(local_tmp):
                os.remove(local_tmp)
        except OSError:
            pass
        return {"status": "error", "message": f"Failed to read OVA/OVF metadata: {str(exc)[:400]}"}

    session_id = uuid.uuid4().hex
    staging_dir = f"/vmfs/volumes/{datastore}/.nebula-ovf-staging/{session_id}"
    remote_uploaded_file = f"{staging_dir}/{filename}"

    if _is_esxi_api_mode(host_obj):
        api_temp_files = []
        try:
            from plugins.esxi_api_plugin import EsxiApiClient

            with EsxiApiClient(
                host=host_obj.ip_address,
                username=host_obj.username,
                password=host_obj.password,
                verify_ssl=False,
            ).connect() as api:
                api.make_datastore_directory(staging_dir, create_parents=True)

                def _upload_via_api_only(local_path: str, remote_name: str) -> None:
                    with open(local_path, "rb") as upload_fp:
                        api.upload_datastore_file(staging_dir, remote_name, upload_fp)

                ovf_remote_path = ""
                uploaded_vmdks = {}

                if filename.lower().endswith(".ova"):
                    with tarfile.open(local_tmp, "r:*") as tar:
                        members = [m for m in tar.getmembers() if m.isfile()]
                        ovf_members = [m for m in members if str(m.name).lower().endswith(".ovf")]
                        if not ovf_members:
                            return {"status": "error", "message": "No OVF descriptor found inside uploaded OVA."}

                        primary_ovf = sorted(ovf_members, key=lambda m: str(m.name).lower())[0]
                        seen_names = set()

                        for member in members:
                            member_name = posixpath.basename(str(member.name or "").strip())
                            if not member_name:
                                continue
                            lower_name = member_name.lower()
                            if not (lower_name.endswith(".ovf") or lower_name.endswith(".vmdk") or lower_name.endswith(".mf")):
                                continue
                            if member_name in seen_names:
                                continue
                            seen_names.add(member_name)

                            src = tar.extractfile(member)
                            if src is None:
                                continue

                            suffix = os.path.splitext(member_name)[1] or ".bin"
                            fd2, tmp_member = tempfile.mkstemp(prefix="nebula-ova-api-", suffix=suffix)
                            os.close(fd2)
                            api_temp_files.append(tmp_member)
                            with open(tmp_member, "wb") as out_handle:
                                shutil.copyfileobj(src, out_handle)

                            _upload_via_api_only(tmp_member, member_name)

                            remote_path = f"{staging_dir}/{member_name}"
                            if str(member.name) == str(primary_ovf.name):
                                ovf_remote_path = remote_path
                            if lower_name.endswith(".vmdk") and not lower_name.endswith(("-flat.vmdk", "-delta.vmdk", "-sesparse.vmdk", "-ctk.vmdk")):
                                uploaded_vmdks[member_name.lower()] = remote_path
                else:
                    _upload_via_api_only(local_tmp, filename)
                    ovf_remote_path = f"{staging_dir}/{filename}"

                    for extra in sidecar_files:
                        extra_name = posixpath.basename(getattr(extra, "name", "") or "")
                        if not extra_name or extra_name == filename:
                            continue

                        extra_suffix = os.path.splitext(extra_name)[1] or ".bin"
                        extra_fd, extra_tmp = tempfile.mkstemp(prefix="nebula-ovf-sidecar-api-", suffix=extra_suffix)
                        os.close(extra_fd)
                        api_temp_files.append(extra_tmp)

                        extra_written = 0
                        with open(extra_tmp, "wb") as efp:
                            for chunk in extra.chunks():
                                efp.write(chunk)
                                extra_written += len(chunk)
                        if extra_written <= 0:
                            continue

                        _upload_via_api_only(extra_tmp, extra_name)
                        if extra_name.lower().endswith(".vmdk") and not extra_name.lower().endswith(("-flat.vmdk", "-delta.vmdk", "-sesparse.vmdk", "-ctk.vmdk")):
                            uploaded_vmdks[extra_name.lower()] = f"{staging_dir}/{extra_name}"

                if not ovf_remote_path:
                    return {
                        "status": "error",
                        "message": "OVF descriptor not found in uploaded content after extraction.",
                    }

                referenced_vmdks = []
                for disk_file in ovf_defaults.get("disk_files") or []:
                    matched = uploaded_vmdks.get(posixpath.basename(str(disk_file)).lower())
                    if matched:
                        referenced_vmdks.append(matched)

                candidate_vmdks = referenced_vmdks or list(uploaded_vmdks.values())
                candidate_vmdks = sorted(dict.fromkeys(candidate_vmdks))
                if not candidate_vmdks:
                    hint = ""
                    if filename.lower().endswith(".ovf"):
                        hint = " Ensure the OVF sidecar VMDK files were included in this same upload."
                    return {
                        "status": "error",
                        "message": f"No attachable VMDK files found in extracted OVA/OVF payload.{hint}",
                    }

                ovf_disks = ovf_defaults.get("disks") or []
                disk_defaults = ovf_disks or [{"label": "Disk 1", "size_gb": 16}]
                primary_disk = disk_defaults[0]
                extra_disk_defaults = [
                    {"size_gb": int(d.get("size_gb") or 16), "type": "thin", "datastore": datastore}
                    for d in disk_defaults[1:]
                ]

                ovf_networks = ovf_defaults.get("networks") or []
                primary_network = ovf_networks[0] if ovf_networks else "VM Network"
                extra_nic_defaults = [{"network": n, "type": "e1000"} for n in ovf_networks[1:]]

                prefill = {
                    "source": "ova_import_api",
                    "name": requested_vm_name or ovf_defaults.get("name") or ova_vm_name,
                    "cpu": int(ovf_defaults.get("cpu") or 2),
                    "ram_mb": int(ovf_defaults.get("ram_mb") or 2048),
                    "guest_os": ovf_defaults.get("guest_os") or "other-64",
                    "firmware": ovf_defaults.get("firmware") or "bios",
                    "hw_version": "13",
                    "disk_type": "thin",
                    "disk_size_gb": int(primary_disk.get("size_gb") or 16),
                    "extra_disks": extra_disk_defaults,
                    "network_name": primary_network,
                    "extra_nics": extra_nic_defaults,
                    "ovf_networks": ovf_networks,
                }

                session_payload = {
                    "session_id": session_id,
                    "host_name": host_name,
                    "datastore": datastore,
                    "staging_dir": staging_dir,
                    "filename": filename,
                    "disk_files": ovf_defaults.get("disk_files") or [],
                    "vmdk_paths": candidate_vmdks,
                    "ovf_path": ovf_remote_path,
                    "api_ovf_source": True,
                    "prefill": prefill,
                    "expires_in_seconds": 3600,
                }
                cache.set(_ova_session_cache_key(session_id), session_payload, timeout=3600)

                redirect_url = f"/admin/manager/virtualmachine/add/?host_name={host_name}&ova_session={session_id}"
                return {
                    "status": "ready_for_config",
                    "message": "OVA/OVF uploaded via API. Review and edit VM settings before final creation.",
                    "session_id": session_id,
                    "redirect_url": redirect_url,
                }
        except Exception as exc:
            logger.exception("Failed to prepare OVA import session via API: host=%s datastore=%s filename=%s", host_name, datastore, filename)
            return {"status": "error", "message": f"Failed to prepare OVA import via API: {str(exc)[:500]}"}
        finally:
            for tmp_path in api_temp_files:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass

    try:
        with _build_esxi_ssh_conn(host_obj) as ssh_conn:
            ssh_conn.run(f"mkdir -p '{staging_dir}'", timeout=120)
            with open(local_tmp, "rb") as fp:
                ssh_conn.upload_file(fp, remote_uploaded_file)

            if filename.lower().endswith(".ova"):
                out = ssh_conn.run(f"tar xf '{remote_uploaded_file}' -C '{staging_dir}'", timeout=3600)
                if isinstance(out, str) and out.startswith("Error:"):
                    return {"status": "error", "message": f"Failed to extract OVA on host: {out[:400]}"}
                ssh_conn.run(f"rm -f '{remote_uploaded_file}'", timeout=60)
            else:
                for extra in sidecar_files:
                    extra_name = posixpath.basename(getattr(extra, "name", "") or "")
                    if not extra_name:
                        continue
                    if extra_name == filename:
                        continue
                    extra_tmp_suffix = os.path.splitext(extra_name)[1] or ".bin"
                    extra_fd, extra_tmp = tempfile.mkstemp(prefix="nebula-ovf-sidecar-", suffix=extra_tmp_suffix)
                    os.close(extra_fd)
                    try:
                        extra_written = 0
                        with open(extra_tmp, "wb") as efp:
                            for chunk in extra.chunks():
                                efp.write(chunk)
                                extra_written += len(chunk)
                        if extra_written <= 0:
                            continue
                        extra_remote = f"{staging_dir}/{extra_name}"
                        with open(extra_tmp, "rb") as efp:
                            ssh_conn.upload_file(efp, extra_remote)
                    finally:
                        try:
                            if os.path.exists(extra_tmp):
                                os.remove(extra_tmp)
                        except OSError:
                            pass

            ovf_remote = ssh_conn.run(f"ls -1 '{staging_dir}'/*.ovf 2>/dev/null | head -1")
            if not ovf_remote or str(ovf_remote).startswith("Error:"):
                return {
                    "status": "error",
                    "message": "OVF descriptor not found in uploaded content after extraction.",
                }

            uploaded_vmdks_raw = ssh_conn.run(f"find '{staging_dir}' -maxdepth 1 -type f \\( -iname '*.vmdk' \\) 2>/dev/null")
            uploaded_vmdks = {}
            for line in str(uploaded_vmdks_raw or "").splitlines():
                p = line.strip()
                if not p:
                    continue
                uploaded_vmdks[posixpath.basename(p).lower()] = p

            referenced_vmdks = []
            for disk_file in ovf_defaults.get("disk_files") or []:
                matched = uploaded_vmdks.get(posixpath.basename(str(disk_file)).lower())
                if matched:
                    referenced_vmdks.append(matched)

            candidate_vmdks = referenced_vmdks or list(uploaded_vmdks.values())
            candidate_vmdks = sorted(dict.fromkeys(candidate_vmdks))
            if not candidate_vmdks:
                hint = ""
                if filename.lower().endswith(".ovf"):
                    hint = " Ensure the OVF sidecar VMDK files were included in this same upload."
                return {
                    "status": "error",
                    "message": f"No attachable VMDK files found in extracted OVA/OVF payload.{hint}",
                }

            discovered_disks = []
            for idx, vmdk in enumerate(candidate_vmdks, start=1):
                discovered_disks.append(
                    {
                        "label": f"Disk {idx}",
                        "size_gb": _read_remote_vmdk_size_gb(ssh_conn, vmdk),
                    }
                )

        ovf_disks = ovf_defaults.get("disks") or []
        disk_defaults = discovered_disks if len(discovered_disks) >= len(ovf_disks) else ovf_disks
        if not disk_defaults:
            disk_defaults = [{"label": "Disk 1", "size_gb": 16}]

        primary_disk = disk_defaults[0]
        extra_disk_defaults = [
            {"size_gb": int(d.get("size_gb") or 16), "type": "thin", "datastore": datastore}
            for d in disk_defaults[1:]
        ]

        ovf_networks = ovf_defaults.get("networks") or []
        primary_network = ovf_networks[0] if ovf_networks else "VM Network"
        extra_nic_defaults = [
            {"network": n, "type": "e1000"}
            for n in ovf_networks[1:]
        ]

        prefill = {
            "source": "ova_import",
            "name": requested_vm_name or ovf_defaults.get("name") or ova_vm_name,
            "cpu": int(ovf_defaults.get("cpu") or 2),
            "ram_mb": int(ovf_defaults.get("ram_mb") or 2048),
            "guest_os": ovf_defaults.get("guest_os") or "other-64",
            "firmware": ovf_defaults.get("firmware") or "bios",
            "hw_version": "13",
            "disk_type": "thin",
            "disk_size_gb": int(primary_disk.get("size_gb") or 16),
            "extra_disks": extra_disk_defaults,
            "network_name": primary_network,
            "extra_nics": extra_nic_defaults,
            "ovf_networks": ovf_networks,
        }

        session_payload = {
            "session_id": session_id,
            "host_name": host_name,
            "datastore": datastore,
            "staging_dir": staging_dir,
            "filename": filename,
            "disk_files": ovf_defaults.get("disk_files") or [],
            "prefill": prefill,
            "expires_in_seconds": 3600,
        }
        cache.set(_ova_session_cache_key(session_id), session_payload, timeout=3600)

        redirect_url = f"/admin/manager/virtualmachine/add/?host_name={host_name}&ova_session={session_id}"
        return {
            "status": "ready_for_config",
            "message": "OVA uploaded and extracted. Review and edit VM settings before final creation.",
            "session_id": session_id,
            "redirect_url": redirect_url,
        }
    except Exception as exc:
        logger.exception("Failed to prepare OVA import session: host=%s datastore=%s filename=%s", host_name, datastore, filename)
        return {"status": "error", "message": f"Failed to prepare OVA import: {str(exc)[:500]}"}
    finally:
        try:
            if os.path.exists(local_tmp):
                os.remove(local_tmp)
        except OSError:
            logger.warning("Failed to remove temporary local OVA file: %s", local_tmp)


@router.get("/{host_name}/register/browse-ovf", summary="Browse Datastore for OVF/OVA Files")
@decorate_view(cache_page(60))
def browse_ovf_for_register(request, host_name: str, path: str = "/vmfs/volumes", recursive: bool = False):
    """Browse ESXi datastore for .ovf and .ova files to deploy from datastore."""
    host_obj = get_host_obj(host_name, require_active=True)
    if host_obj.hypervisor_type != Host.HYPERVISOR_VMWARE_ESXI:
        return {"status": "error", "message": "OVF datastore deploy is only supported for ESXi hosts."}

    normalized = posixpath.normpath(path)
    if not normalized.startswith("/vmfs/volumes"):
        return {"status": "error", "message": "Browsing is restricted to /vmfs/volumes"}

    if _is_esxi_api_mode(host_obj):
        # API mode: list the current directory only (fast), mirroring SSH browse behavior.
        try:
            from plugins.esxi_api_plugin import EsxiApiClient
            client = EsxiApiClient(
                host=host_obj.ip_address,
                username=host_obj.username,
                password=host_obj.password,
                verify_ssl=False,
            )
            with client.connect() as api:
                if recursive:
                    ovf_paths = api.list_files_by_suffix_under(normalized, ".ovf")
                    ova_paths = api.list_files_by_suffix_under(normalized, ".ova")
                    entries = []
                    for fpath in sorted(ovf_paths + ova_paths):
                        fname = posixpath.basename(fpath)
                        if not fname:
                            continue
                        lower = fname.lower()
                        entries.append({
                            "name": fname,
                            "path": fpath,
                            "is_dir": False,
                            "kind": lower.rsplit(".", 1)[-1],
                        })
                    return {"status": "success", "path": normalized, "recursive": True, "entries": entries}

                dir_entries = api.list_datastore_directory(normalized)

            entries = []
            for item in dir_entries:
                name = str(getattr(item, "name", "") or "").strip()
                entry_path = str(getattr(item, "path", "") or "").strip()
                is_dir = bool(getattr(item, "is_dir", False))
                if not name or not entry_path:
                    continue

                lower = name.lower()
                if is_dir:
                    entries.append({"name": name, "path": entry_path, "is_dir": True, "kind": "dir"})
                elif lower.endswith(".ovf") or lower.endswith(".ova"):
                    entries.append({
                        "name": name,
                        "path": entry_path,
                        "is_dir": False,
                        "kind": lower.rsplit(".", 1)[-1],
                    })

            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            return {"status": "success", "path": normalized, "entries": entries}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:500]}

    try:
        with _build_esxi_ssh_conn(host_obj) as conn:
            entries = []
            if recursive:
                target = normalized.rstrip("/")
                raw = conn.run(
                    f"find {shlex.quote(target)} -type f \\( -iname '*.ovf' -o -iname '*.ova' \\) 2>/dev/null"
                )
                for line in str(raw or "").splitlines():
                    full_path = line.strip()
                    if not full_path:
                        continue
                    name = posixpath.basename(full_path)
                    lower = name.lower()
                    entries.append({"name": name, "path": full_path, "is_dir": False, "kind": lower.rsplit(".", 1)[-1]})
                entries.sort(key=lambda e: e["path"].lower())
                return {"status": "success", "path": normalized, "recursive": True, "entries": entries}

            target = normalized.rstrip("/") + "/"
            raw = conn.run(f"ls -1Ap {shlex.quote(target)} 2>/dev/null")
            for line in str(raw or "").splitlines():
                name = line.strip()
                if not name or name == "./" or name == "../":
                    continue
                is_dir = name.endswith("/")
                clean_name = name.rstrip("/")
                full_path = f"{normalized.rstrip('/')}/{clean_name}"
                lower = clean_name.lower()
                if is_dir:
                    entries.append({"name": clean_name, "path": full_path, "is_dir": True, "kind": "dir"})
                elif lower.endswith(".ovf") or lower.endswith(".ova"):
                    entries.append({"name": clean_name, "path": full_path, "is_dir": False, "kind": lower.rsplit(".", 1)[-1]})
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"status": "success", "path": normalized, "entries": entries}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:500]}


@router.post("/{host_name}/deploy-ovf-from-datastore", summary="Deploy OVF/OVA Already on Datastore")
def deploy_ovf_from_datastore_endpoint(request, host_name: str, ovf_path: str, vm_name: str = "", datastore: str = ""):
    """
    Read an OVF (or OVA) that already exists on the ESXi datastore and create an
    import session so the user can review settings before final deployment.
    ovf_path must point to a .ovf or .ova under /vmfs/volumes.
    """
    host_obj = get_host_obj(host_name, require_active=True)
    if host_obj.hypervisor_type != Host.HYPERVISOR_VMWARE_ESXI:
        return {"status": "error", "message": "OVF datastore deploy is only supported for ESXi hosts."}

    ovf_path = posixpath.normpath(str(ovf_path or "").strip())
    if not ovf_path.startswith("/vmfs/volumes"):
        return {"status": "error", "message": "ovf_path must be under /vmfs/volumes"}
    lower_path = ovf_path.lower()
    if not (lower_path.endswith(".ovf") or lower_path.endswith(".ova")):
        return {"status": "error", "message": "ovf_path must point to a .ovf or .ova file"}

    is_ova = lower_path.endswith(".ova")
    source_dir = posixpath.dirname(ovf_path)
    filename = posixpath.basename(ovf_path)
    requested_vm_name = (vm_name or "").strip()
    ova_vm_name = requested_vm_name or filename.rsplit(".", 1)[0]

    # Auto-detect datastore from path when not supplied
    if not datastore:
        parts = ovf_path.lstrip("/").split("/")
        datastore = parts[2] if len(parts) > 2 else ""
    if not datastore:
        return {"status": "error", "message": "Could not determine target datastore from ovf_path."}

    # ── API mode: read OVF text via HTTP, discover VMDKs via datastore browser ──
    if _is_esxi_api_mode(host_obj):
        try:
            from plugins.esxi_api_plugin import EsxiApiClient
            client = EsxiApiClient(
                host=host_obj.ip_address,
                username=host_obj.username,
                password=host_obj.password,
                verify_ssl=False,
            )

            session_id = uuid.uuid4().hex
            local_tmp_ova = None
            local_extract_dir = None
            with client.connect() as api:
                session_staging_dir = source_dir
                ovf_path_for_import = ovf_path

                if is_ova:
                    session_staging_dir = f"/vmfs/volumes/{datastore}/.nebula-ovf-staging/{session_id}"
                    api.make_datastore_directory(session_staging_dir, create_parents=True)

                    local_tmp_ova = api.download_datastore_file_to_local(ovf_path)
                    local_extract_dir = tempfile.mkdtemp(prefix="nebula-ds-ova-api-")

                    with tarfile.open(local_tmp_ova, "r:*") as tar:
                        tar.extractall(local_extract_dir)

                    ovf_candidates = []
                    for root_dir, _, files in os.walk(local_extract_dir):
                        for fname in files:
                            if fname.lower().endswith(".ovf"):
                                ovf_candidates.append(os.path.join(root_dir, fname))

                    if not ovf_candidates:
                        return {"status": "error", "message": "No OVF descriptor found after extracting datastore OVA."}

                    primary_ovf_local = sorted(ovf_candidates)[0]
                    with open(primary_ovf_local, "rb") as ovf_file_handle:
                        ovf_text = ovf_file_handle.read().decode("utf-8", errors="replace")

                    uploaded_names = set()
                    for root_dir, _, files in os.walk(local_extract_dir):
                        for fname in files:
                            lower_name = fname.lower()
                            if not (lower_name.endswith(".ovf") or lower_name.endswith(".vmdk") or lower_name.endswith(".mf")):
                                continue
                            if fname in uploaded_names:
                                continue
                            uploaded_names.add(fname)

                            local_file_path = os.path.join(root_dir, fname)
                            with open(local_file_path, "rb") as upload_handle:
                                api.upload_datastore_file(session_staging_dir, fname, upload_handle)

                    ovf_path_for_import = f"{session_staging_dir}/{posixpath.basename(primary_ovf_local)}"
                else:
                    ovf_bytes = api.read_datastore_file_content(ovf_path, max_bytes=2 * 1024 * 1024)
                    ovf_text = ovf_bytes.decode("utf-8", errors="replace")

                ovf_defaults = _parse_ovf_defaults(ovf_text, ova_vm_name)

                # Discover VMDKs next to the OVF in the same directory
                dir_entries = api.list_datastore_directory(session_staging_dir)
                vmdk_paths = [
                    e.path for e in dir_entries
                    if (
                        not e.is_dir
                        and e.name.lower().endswith(".vmdk")
                        and not e.name.lower().endswith(("-flat.vmdk", "-delta.vmdk", "-sesparse.vmdk", "-ctk.vmdk"))
                    )
                ]

            ovf_disks = ovf_defaults.get("disks") or []
            disk_defaults = [{"label": f"Disk {i+1}", "size_gb": int(d.get("size_gb") or 16)} for i, d in enumerate(ovf_disks)] or [{"label": "Disk 1", "size_gb": 16}]
            primary_disk = disk_defaults[0]
            extra_disk_defaults = [
                {"size_gb": int(d.get("size_gb") or 16), "type": "thin", "datastore": datastore}
                for d in disk_defaults[1:]
            ]
            ovf_networks = ovf_defaults.get("networks") or []
            primary_network = ovf_networks[0] if ovf_networks else "VM Network"
            extra_nic_defaults = [{"network": n, "type": "e1000"} for n in ovf_networks[1:]]

            prefill = {
                "source": "api_ovf_datastore",
                "name": requested_vm_name or ovf_defaults.get("name") or ova_vm_name,
                "cpu": int(ovf_defaults.get("cpu") or 2),
                "ram_mb": int(ovf_defaults.get("ram_mb") or 2048),
                "guest_os": ovf_defaults.get("guest_os") or "other-64",
                "firmware": ovf_defaults.get("firmware") or "bios",
                "hw_version": "13",
                "disk_type": "thin",
                "disk_size_gb": int(primary_disk.get("size_gb") or 16),
                "extra_disks": extra_disk_defaults,
                "network_name": primary_network,
                "extra_nics": extra_nic_defaults,
                "ovf_networks": ovf_networks,
            }
            session_payload = {
                "session_id": session_id,
                "host_name": host_name,
                "datastore": datastore,
                "staging_dir": session_staging_dir,
                "ovf_path": ovf_path_for_import,
                "disk_files": ovf_defaults.get("disk_files") or [],
                "vmdk_paths": vmdk_paths,
                "api_ovf_source": True,
                "prefill": prefill,
                "expires_in_seconds": 3600,
            }
            cache.set(_ova_session_cache_key(session_id), session_payload, timeout=3600)
            return {
                "status": "ready_for_config",
                "message": "OVF/OVA prepared from datastore via API. Review and edit VM settings before final creation.",
                "session_id": session_id,
                "redirect_url": f"/admin/manager/virtualmachine/add/?host_name={host_name}&ova_session={session_id}",
            }
        except Exception as exc:
            logger.exception("API OVF-from-datastore failed: host=%s ovf=%s", host_name, ovf_path)
            return {"status": "error", "message": f"Failed to read OVF via API: {str(exc)[:500]}"}
        finally:
            if local_tmp_ova:
                try:
                    if os.path.exists(local_tmp_ova):
                        os.remove(local_tmp_ova)
                except OSError:
                    pass
            if local_extract_dir:
                try:
                    shutil.rmtree(local_extract_dir, ignore_errors=True)
                except Exception:
                    pass

    try:
        with _build_esxi_ssh_conn(host_obj) as ssh_conn:
            # For .ova: extract into a staging dir on the same datastore
            if is_ova:
                session_id = uuid.uuid4().hex
                staging_dir = f"/vmfs/volumes/{datastore}/.nebula-ovf-staging/{session_id}"
                ssh_conn.run(f"mkdir -p {shlex.quote(staging_dir)}", timeout=60)
                out = ssh_conn.run(f"tar xf {shlex.quote(ovf_path)} -C {shlex.quote(staging_dir)}", timeout=3600)
                if isinstance(out, str) and out.startswith("Error:"):
                    return {"status": "error", "message": f"Failed to extract OVA on host: {out[:400]}"}
                ovf_remote = ssh_conn.run(f"ls -1 {shlex.quote(staging_dir)}/*.ovf 2>/dev/null | head -1")
                if not ovf_remote or str(ovf_remote).startswith("Error:"):
                    return {"status": "error", "message": "No OVF descriptor found after extracting OVA."}
                staging_source = staging_dir
                ovf_text_raw = ssh_conn.run(f"cat {shlex.quote(str(ovf_remote).strip())}")
            else:
                # .ovf already on datastore – use its directory as staging source
                session_id = uuid.uuid4().hex
                staging_dir = f"/vmfs/volumes/{datastore}/.nebula-ovf-staging/{session_id}"
                ssh_conn.run(f"mkdir -p {shlex.quote(staging_dir)}", timeout=60)
                # Copy the whole OVF folder into staging so we can safely convert disks later
                ssh_conn.run(f"cp -R {shlex.quote(source_dir)}/. {shlex.quote(staging_dir)}/", timeout=3600)
                staging_source = staging_dir
                ovf_remote_path = f"{staging_dir}/{filename}"
                ovf_text_raw = ssh_conn.run(f"cat {shlex.quote(ovf_remote_path)}")

            ovf_text = str(ovf_text_raw or "")
            if not ovf_text or ovf_text.startswith("Error:"):
                return {"status": "error", "message": "Could not read OVF descriptor from datastore."}

            ovf_defaults = _parse_ovf_defaults(ovf_text, ova_vm_name)

            # Discover VMDKs in staging dir
            vmdk_list_raw = ssh_conn.run(f"find {shlex.quote(staging_dir)} -maxdepth 1 -type f -iname '*.vmdk' 2>/dev/null")
            uploaded_vmdks = {}
            for line in str(vmdk_list_raw or "").splitlines():
                p = line.strip()
                if p:
                    uploaded_vmdks[posixpath.basename(p).lower()] = p

            referenced_vmdks = []
            for disk_file in ovf_defaults.get("disk_files") or []:
                matched = uploaded_vmdks.get(posixpath.basename(str(disk_file)).lower())
                if matched:
                    referenced_vmdks.append(matched)
            candidate_vmdks = referenced_vmdks or list(uploaded_vmdks.values())
            candidate_vmdks = sorted(dict.fromkeys(candidate_vmdks))

            discovered_disks = []
            for idx, vmdk in enumerate(candidate_vmdks, start=1):
                discovered_disks.append({
                    "label": f"Disk {idx}",
                    "size_gb": _read_remote_vmdk_size_gb(ssh_conn, vmdk),
                })

        ovf_disks = ovf_defaults.get("disks") or []
        disk_defaults = discovered_disks if len(discovered_disks) >= len(ovf_disks) else ovf_disks
        if not disk_defaults:
            disk_defaults = [{"label": "Disk 1", "size_gb": 16}]

        primary_disk = disk_defaults[0]
        extra_disk_defaults = [
            {"size_gb": int(d.get("size_gb") or 16), "type": "thin", "datastore": datastore}
            for d in disk_defaults[1:]
        ]
        ovf_networks = ovf_defaults.get("networks") or []
        primary_network = ovf_networks[0] if ovf_networks else "VM Network"
        extra_nic_defaults = [{"network": n, "type": "e1000"} for n in ovf_networks[1:]]

        prefill = {
            "source": "ovf_datastore",
            "name": requested_vm_name or ovf_defaults.get("name") or ova_vm_name,
            "cpu": int(ovf_defaults.get("cpu") or 2),
            "ram_mb": int(ovf_defaults.get("ram_mb") or 2048),
            "guest_os": ovf_defaults.get("guest_os") or "other-64",
            "firmware": ovf_defaults.get("firmware") or "bios",
            "hw_version": "13",
            "disk_type": "thin",
            "disk_size_gb": int(primary_disk.get("size_gb") or 16),
            "extra_disks": extra_disk_defaults,
            "network_name": primary_network,
            "extra_nics": extra_nic_defaults,
            "ovf_networks": ovf_networks,
        }

        session_payload = {
            "session_id": session_id,
            "host_name": host_name,
            "datastore": datastore,
            "staging_dir": staging_dir,
            "filename": filename,
            "disk_files": ovf_defaults.get("disk_files") or [],
            "prefill": prefill,
            "expires_in_seconds": 3600,
        }
        cache.set(_ova_session_cache_key(session_id), session_payload, timeout=3600)

        redirect_url = f"/admin/manager/virtualmachine/add/?host_name={host_name}&ova_session={session_id}"
        return {
            "status": "ready_for_config",
            "message": "OVF read from datastore. Review and edit VM settings before final creation.",
            "session_id": session_id,
            "redirect_url": redirect_url,
        }
    except Exception as exc:
        logger.exception("Failed to prepare OVF-from-datastore session: host=%s ovf_path=%s", host_name, ovf_path)
        return {"status": "error", "message": f"Failed to prepare OVF from datastore: {str(exc)[:500]}"}


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
            entries = []
            if hasattr(conn, "list_datastore_directory"):
                for item in conn.list_datastore_directory(normalized):
                    clean_name = str(getattr(item, "name", "") or "")
                    full_path = str(getattr(item, "path", "") or "")
                    is_dir = bool(getattr(item, "is_dir", False))
                    if is_dir:
                        entries.append({"name": clean_name, "path": full_path, "is_dir": True, "kind": "dir"})
                    elif clean_name.lower().endswith(".vmx"):
                        entries.append({"name": clean_name, "path": full_path, "is_dir": False, "kind": "vmx"})
            else:
                target = normalized.rstrip("/") + "/"
                raw = conn.run(f"ls -1Ap '{target}'")
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
            # ESXi API mode: use pyVmomi RegisterVM_Task
            if hasattr(conn, "register_vm_from_path"):
                api_result = conn.register_vm_from_path(normalized_vmx)
                if api_result.get("status") == "error":
                    return api_result
                bust_vm_cache(host_name)
                sync_vms_for_host(host_obj)
                vm_obj = VirtualMachine.objects.filter(host=host_obj).order_by("-updated_at").first()
                if vm_obj:
                    try:
                        broadcast_vm_created(vm_obj)
                    except Exception:
                        pass
                return {"status": "registered", "message": f"Registered VM from {normalized_vmx}", "vmx_path": normalized_vmx}

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
            # API mode: read hardware directly from vm.config via vSphere API
            if hasattr(conn, "get_vm_hardware_api"):
                return conn.get_vm_hardware_api(vmid)

            vms = vm_modify.list_vms_summary(conn)
            vmx_path = next((vm["vmx"] for vm in vms if vm["vmid"] == vmid), None)
            if not vmx_path:
                return {"status": "error", "message": f"VM {vmid} not found"}
            data = vm_modify.get_vm_hardware(conn, vmid, vmx_path)

            vmx = vm_modify.get_vmx_content(conn, vmx_path)
            data["cpu_hotplug"] = str(vmx.get("vcpu.hotadd", "FALSE")).upper() == "TRUE"
            data["memory_hotplug"] = str(vmx.get("mem.hotadd", "FALSE")).upper() == "TRUE"
            data["hardware_virtualization"] = str(vmx.get("vhv.enable", "FALSE")).upper() == "TRUE"
            memsize_mb = int(str(vmx.get("memsize", "0") or "0") or 0)
            mem_reservation_mb = int(str(vmx.get("sched.mem.min", "0") or "0") or 0)
            cpu_reservation_mhz = int(str(vmx.get("sched.cpu.min", "0") or "0") or 0)
            data["reserve_all_memory"] = memsize_mb > 0 and mem_reservation_mb >= memsize_mb
            data["reserve_all_cpu"] = cpu_reservation_mhz > 0

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
    reserve_all_cpu: bool = None, reserve_all_memory: bool = None,
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
            # ESXi API mode: use pyVmomi ReconfigVM_Task
            if hasattr(conn, "reconfigure_vm"):
                api_result = conn.reconfigure_vm(
                    vm_identifier=vmid, modification=modification,
                    cpu=cpu, memory=memory, disk_size=disk_size, disk_unit=disk_unit,
                    disk_name=disk_name, datastore=datastore, network_name=network_name,
                    adapter_type=adapter_type, guest_os=guest_os, hw_version=hw_version,
                    nic_number=nic_number, iso_path=iso_path, enabled=enabled,
                    pci_id=pci_id, pci_slot=pci_slot, value=value,
                    reserve_all_cpu=reserve_all_cpu, reserve_all_memory=reserve_all_memory,
                )
                bust_vm_cache(host_name, vmid)
                return api_result

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
            elif modification == "reserve_all_cpu":
                result = vm_modify.set_reserve_all_cpu(conn, vmid, vmx_path, _to_bool(enabled if enabled is not None else value, False))
            elif modification == "reserve_all_memory":
                result = vm_modify.set_reserve_all_memory(conn, vmid, vmx_path, _to_bool(enabled if enabled is not None else value, False))
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
            # API mode has no VMX backup concept — use snapshots for config rollback
            if hasattr(conn, "get_vm_hardware_api"):
                return {
                    "status": "error",
                    "message": "VMX backup/restore is SSH-only. In API mode, use VM snapshots to roll back configuration changes.",
                }
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

    # ESXi (SSH or API mode)
    try:
        with get_conn(host_name) as conn:
            # ESXi API mode: use pyVmomi UnregisterVM + DeleteDatastoreFile
            if hasattr(conn, "destroy_vm"):
                result = conn.destroy_vm(vmid)
                if result.get("status") == "error":
                    return result
            else:
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
