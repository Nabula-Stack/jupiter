import re
from . import info as vm_info


def _detect_host_cpu_mhz(host):
    """Best-effort host CPU MHz detection for VMX CPU reservation math."""
    probes = [
        "esxcli hardware cpu list | grep -m1 'CPU Speed:'",
        "esxcli hardware cpu global get | grep -E 'Hz|hz|MHz|mhz' | head -1",
    ]
    for cmd in probes:
        out = str(host.run(cmd) or "")
        if out.startswith("Error:"):
            continue
        mhz_match = re.search(r"(\d+)\s*MHz", out, flags=re.IGNORECASE)
        if mhz_match:
            return int(mhz_match.group(1))
        hz_match = re.search(r"(\d{7,})\s*Hz", out, flags=re.IGNORECASE)
        if hz_match:
            return int(int(hz_match.group(1)) / 1_000_000)
    return 1000


def _ds_to_path(ds_path):
    """Convert VMware datastore path '[DATASTORE] relative/path' to /vmfs/volumes/DATASTORE/relative/path."""
    m = re.match(r'^\[(.+?)\]\s+(.+)$', ds_path.strip())
    if m:
        return f"/vmfs/volumes/{m.group(1)}/{m.group(2)}"
    return ds_path


def list_vms_summary(host):
    """
    List all VMs with summary info (vmid, name, vmx path).
    Uses vim-cmd to parse getallvms output.
    """
    raw = host.run("vim-cmd vmsvc/getallvms")
    vms = []
    lines = raw.splitlines()
    if len(lines) > 1:
        for line in lines[1:]:
            parts = re.split(r'\s{2,}', line.strip())
            if len(parts) >= 2:
                vmx = _ds_to_path(parts[2]) if len(parts) > 2 else "Unknown"
                vms.append({
                    "vmid": parts[0],
                    "name": parts[1],
                    "vmx": vmx
                })
    return vms


# ---------------------------------------------------------------------------
#  VMX read helpers (read-only — used to discover next available slot numbers)
# ---------------------------------------------------------------------------

def get_vmx_content(host, vmx_path):
    """Read and parse VMX file. Returns a dict of key-value pairs (read-only use)."""
    content = host.run(f"cat '{vmx_path}'")
    if not content or content.startswith("Error:"):
        raise RuntimeError(f"Failed to read VMX at {vmx_path}: {content}")
    vmx_dict = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            vmx_dict[key.strip()] = value.strip().strip('"')
    if not vmx_dict:
        raise RuntimeError(f"VMX file appears empty or unparseable: {vmx_path}")
    return vmx_dict


# ---------------------------------------------------------------------------
#  VMX write helpers — targeted in-place edits (never rewrites the whole file)
# ---------------------------------------------------------------------------

def _backup_vmx(host, vmx_path):
    """Create a .bak copy before any modification."""
    host.run(f"cp '{vmx_path}' '{vmx_path}.bak'")


def _vmx_set_key(host, vmx_path, key, value):
    """Replace one key in-place with sed.  Appends if the key doesn't exist yet."""
    # Escape for sed basic-regex pattern
    sed_key = key.replace('.', r'\.').replace('[', r'\[')
    # Escape for sed replacement string
    sed_val = str(value).replace('/', r'\/').replace('&', r'\&')

    check = host.run(f"grep -c '^{key} ' '{vmx_path}'")
    exists = not check.startswith("Error") and check.strip() not in ("0", "")

    if exists:
        host.run(
            f"sed 's/^{sed_key} = .*/{sed_key} = \"{sed_val}\"/' "
            f"'{vmx_path}' > '{vmx_path}.tmp' && mv '{vmx_path}.tmp' '{vmx_path}'"
        )
    else:
        host.run(f"printf '%s\\n' '{key} = \"{value}\"' >> '{vmx_path}'")


def _vmx_append_keys(host, vmx_path, entries):
    """Append multiple key = \"value\" lines to the end of the VMX file."""
    for key, value in entries.items():
        host.run(f"printf '%s\\n' '{key} = \"{value}\"' >> '{vmx_path}'")


def _vmx_remove_prefix(host, vmx_path, prefix):
    """Delete every line whose key starts with *prefix* (e.g. 'ethernet1.' or 'scsi0:2.')."""
    sed_pfx = prefix.replace('.', r'\.').replace('[', r'\[')
    host.run(
        f"sed '/^{sed_pfx}/d' '{vmx_path}' > '{vmx_path}.tmp' "
        f"&& mv '{vmx_path}.tmp' '{vmx_path}'"
    )


def restore_vmx_backup(host, vmx_path, vmid):
    """Restore a VMX from its .bak copy and reload the VM config."""
    result = host.run(f"test -f '{vmx_path}.bak' && cp '{vmx_path}.bak' '{vmx_path}' && echo OK")
    if "OK" not in (result or ""):
        raise RuntimeError(f"No backup found at {vmx_path}.bak")
    reload_vm_config(host, vmid)
    return {"status": "success", "message": "VMX restored from backup and config reloaded."}


# ---------------------------------------------------------------------------
#  Read structured hardware info from VMX (NICs + Disks)
# ---------------------------------------------------------------------------

def get_vm_hardware(host, vmid, vmx_path):
    """
    Parse the VMX file and return structured NIC and disk information,
    similar to the ESXi UI edit-settings view.
    """
    vmx = get_vmx_content(host, vmx_path)
    state = get_vm_state(host, vmid)

    # --- NICs ---
    nic_ids = sorted(set(
        k.split('.')[0] for k in vmx if k.startswith('ethernet') and '.present' in k
        and vmx[k].upper() == 'TRUE'
    ))
    nics = []
    for eid in nic_ids:
        idx = int(eid.replace('ethernet', ''))
        nics.append({
            "index": idx,
            "label": eid,
            "network": vmx.get(f'{eid}.networkName', 'Unknown'),
            "type": vmx.get(f'{eid}.virtualDev', 'Unknown'),
            "mac": vmx.get(f'{eid}.generatedAddress', vmx.get(f'{eid}.address', 'N/A')),
            "connected": vmx.get(f'{eid}.startConnected', 'TRUE').upper() == 'TRUE',
        })

    # --- Disks ---
    disk_keys = sorted(set(
        k.rsplit('.', 1)[0] for k in vmx
        if re.match(r'scsi\d+:\d+\.fileName', k) and vmx.get(k, '').endswith('.vmdk')
    ))
    disks = []
    for dk in disk_keys:
        # dk is like 'scsi0:0'
        m = re.match(r'scsi(\d+):(\d+)', dk)
        if not m:
            continue
        ctrl = int(m.group(1))
        unit = int(m.group(2))
        file_name = vmx.get(f'{dk}.fileName', '')

        # Resolve the full path for the VMDK
        if file_name.startswith('/'):
            vmdk_full = file_name
        else:
            vm_dir = vmx_path.rsplit('/', 1)[0]
            vmdk_full = f"{vm_dir}/{file_name}"

        # Get disk size from vmkfstools
        size_gb = 0
        try:
            raw = host.run(f"vmkfstools -D '{vmdk_full}' 2>/dev/null | head -2")
            # Output: "Disk ... has N sectors..."  or use du as fallback
            if 'RW' in raw:
                # descriptor: RW <sectors> ...
                sectors_match = re.search(r'RW\s+(\d+)', raw)
                if sectors_match:
                    size_gb = round(int(sectors_match.group(1)) * 512 / (1024**3), 2)
        except Exception:
            pass

        if size_gb == 0:
            # Fallback: read the VMDK descriptor directly
            try:
                desc = host.run(f"head -20 '{vmdk_full}'")
                ext_match = re.search(r'RW\s+(\d+)', desc)
                if ext_match:
                    size_gb = round(int(ext_match.group(1)) * 512 / (1024**3), 2)
            except Exception:
                pass

        disks.append({
            "controller": ctrl,
            "unit": unit,
            "label": f"SCSI ({ctrl}:{unit})",
            "file": file_name,
            "full_path": vmdk_full,
            "size_gb": size_gb,
            "thin": vmx.get(f'{dk}.mode', '') != 'independent',
        })

    cdrom = get_cdrom_info(host, vmx_path)

    return {
        "vmid": vmid,
        "power_state": state,
        "nics": nics,
        "disks": disks,
        "cdrom": cdrom,
    }


# ---------------------------------------------------------------------------
#  Disk resize  (vmkfstools -X)
# ---------------------------------------------------------------------------

def resize_disk(host, vmid, vmx_path, disk_unit, new_size_gb):
    """
    Expand (or report error for shrink) a virtual disk.
    ESXi does NOT support shrinking VMDKs — only expanding via vmkfstools -X.
    VM must be powered off.
    """
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    vmx = get_vmx_content(host, vmx_path)
    file_name = vmx.get(f'scsi0:{disk_unit}.fileName', '')
    if not file_name:
        raise RuntimeError(f"No disk found at scsi0:{disk_unit}")

    if file_name.startswith('/'):
        vmdk_full = file_name
    else:
        vm_dir = vmx_path.rsplit('/', 1)[0]
        vmdk_full = f"{vm_dir}/{file_name}"

    result = host.run(f"vmkfstools -X {new_size_gb}G '{vmdk_full}'")
    if isinstance(result, str) and result.startswith("Error"):
        raise RuntimeError(result)

    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modification": "disk_resized",
        "disk_unit": disk_unit,
        "new_size": f"{new_size_gb}GB",
        "message": f"Disk scsi0:{disk_unit} expanded to {new_size_gb}GB.",
    }


# Legacy wrapper kept only for batch_modify_vm (should be phased out)
def set_vmx_content(host, vmx_path, vmx_dict):
    """DEPRECATED — only used by batch_modify_vm. Prefer targeted helpers."""
    _backup_vmx(host, vmx_path)
    lines = []
    for key, value in vmx_dict.items():
        lines.append(f'{key} = "{value}"')
    vmx_content = "\n".join(lines) + "\n"
    cmd = f"cat > '{vmx_path}' << 'VMXEOF'\n{vmx_content}VMXEOF"
    return host.run(cmd)


def power_off_vm(host, vmid):
    """
    Power off a VM gracefully or forcefully.
    """
    try:
        result = host.run(f"vim-cmd vmsvc/power.off {vmid}")
        return result
    except Exception as e:
        raise Exception(f"Failed to power off VM {vmid}: {e}")


def power_on_vm(host, vmid):
    """
    Power on a VM.
    """
    try:
        result = host.run(f"vim-cmd vmsvc/power.on {vmid}")
        return result
    except Exception as e:
        raise Exception(f"Failed to power on VM {vmid}: {e}")


def get_vm_state(host, vmid):
    """
    Get current power state of a VM.
    Returns: 'poweredOn', 'poweredOff', or 'suspended'
    """
    summary = host.run(f"vim-cmd vmsvc/get.summary {vmid}")
    if "poweredOn" in summary:
        return "poweredOn"
    elif "suspended" in summary:
        return "suspended"
    else:
        return "poweredOff"


def reload_vm_config(host, vmid):
    """
    Reload VM configuration in ESXi after modifying VMX file.
    This is required for changes to take effect.
    """
    try:
        result = host.run(f"vim-cmd vmsvc/reload {vmid}")
        return result
    except Exception as e:
        raise Exception(f"Failed to reload VM {vmid} config: {e}")


# ---------------------------------------------------------------------------
#  CD-ROM / ISO management  (ide1:0)
# ---------------------------------------------------------------------------

def get_cdrom_info(host, vmx_path):
    """Read current CD-ROM state from VMX file (ide1:0)."""
    vmx = get_vmx_content(host, vmx_path)
    present = vmx.get('ide1:0.present', 'FALSE').upper() == 'TRUE'
    device_type = vmx.get('ide1:0.deviceType', 'atapi-cdrom')
    file_name = vmx.get('ide1:0.fileName', '')
    mounted = present and device_type == 'cdrom-image' and bool(file_name)
    return {
        "present": present,
        "mounted": mounted,
        "iso_path": file_name if mounted else "",
        "device_type": device_type,
    }


def mount_iso(host, vmid, vmx_path, iso_path):
    """Mount an ISO image to ide1:0 in the VMX. Works while VM is powered on or off."""
    import posixpath
    normalized = posixpath.normpath(iso_path.strip())
    if not normalized.startswith('/vmfs/volumes'):
        raise ValueError(f"ISO path must be under /vmfs/volumes (got: {normalized})")
    if not normalized.lower().endswith('.iso'):
        raise ValueError(f"Only .iso files are supported (got: {normalized})")
    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, 'ide1:0.present', 'TRUE')
    _vmx_set_key(host, vmx_path, 'ide1:0.fileName', normalized)
    _vmx_set_key(host, vmx_path, 'ide1:0.deviceType', 'cdrom-image')
    _vmx_set_key(host, vmx_path, 'ide1:0.startConnected', 'TRUE')
    reload_vm_config(host, vmid)
    return {
        "status": "success",
        "modification": "mount_iso",
        "iso_path": normalized,
        "message": f"ISO mounted: {normalized}",
    }


def eject_iso(host, vmid, vmx_path):
    """Eject the ISO from ide1:0 and reset to empty virtual CD-ROM."""
    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, 'ide1:0.present', 'TRUE')
    _vmx_set_key(host, vmx_path, 'ide1:0.fileName', '')
    _vmx_set_key(host, vmx_path, 'ide1:0.deviceType', 'atapi-cdrom')
    _vmx_set_key(host, vmx_path, 'ide1:0.startConnected', 'FALSE')
    reload_vm_config(host, vmid)
    return {
        "status": "success",
        "modification": "eject_iso",
        "message": "ISO ejected.",
    }


def modify_cpu(host, vmid, vmx_path, cpu_count):
    """Modify the number of vCPUs. VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, 'numvcpus', str(cpu_count))
    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modification": "cpu",
        "new_value": cpu_count,
        "message": f"vCPU count updated to {cpu_count}. Configuration reloaded."
    }


def modify_memory(host, vmid, vmx_path, memory_mb):
    """Modify the amount of RAM (in MB). VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, 'memsize', str(memory_mb))
    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modification": "memory",
        "new_value": f"{memory_mb} MB",
        "message": f"Memory updated to {memory_mb} MB. Configuration reloaded."
    }


def set_cpu_hotplug(host, vmid, vmx_path, enabled):
    """Enable/disable CPU hotplug (vcpu.hotadd). VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, 'vcpu.hotadd', 'TRUE' if bool(enabled) else 'FALSE')
    reload_vm_config(host, vmid)
    return {
        "status": "success",
        "vmid": vmid,
        "modification": "cpu_hotplug",
        "enabled": bool(enabled),
        "message": f"CPU hotplug {'enabled' if enabled else 'disabled'}. Configuration reloaded.",
    }


def set_memory_hotplug(host, vmid, vmx_path, enabled):
    """Enable/disable memory hotplug (mem.hotadd). VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, 'mem.hotadd', 'TRUE' if bool(enabled) else 'FALSE')
    reload_vm_config(host, vmid)
    return {
        "status": "success",
        "vmid": vmid,
        "modification": "memory_hotplug",
        "enabled": bool(enabled),
        "message": f"Memory hotplug {'enabled' if enabled else 'disabled'}. Configuration reloaded.",
    }


def set_hardware_virtualization(host, vmid, vmx_path, enabled):
    """Enable/disable nested hardware virtualization (vhv.enable). VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, 'vhv.enable', 'TRUE' if bool(enabled) else 'FALSE')
    reload_vm_config(host, vmid)
    return {
        "status": "success",
        "vmid": vmid,
        "modification": "hardware_virtualization",
        "enabled": bool(enabled),
        "message": f"Nested virtualization {'enabled' if enabled else 'disabled'}. Configuration reloaded.",
    }


def set_reserve_all_memory(host, vmid, vmx_path, enabled):
    """Enable/disable full memory reservation (sched.mem.min). VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    vmx = get_vmx_content(host, vmx_path)
    memsize_mb = int(str(vmx.get("memsize", "0") or "0") or 0)

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, "sched.mem.min", str(memsize_mb if bool(enabled) else 0))
    reload_vm_config(host, vmid)
    return {
        "status": "success",
        "vmid": vmid,
        "modification": "reserve_all_memory",
        "enabled": bool(enabled),
        "message": f"Full memory reservation {'enabled' if enabled else 'disabled'}. Configuration reloaded.",
    }


def set_reserve_all_cpu(host, vmid, vmx_path, enabled):
    """Enable/disable full CPU reservation (sched.cpu.min). VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    vmx = get_vmx_content(host, vmx_path)
    cpu_count = int(str(vmx.get("numvcpus", "1") or "1") or 1)
    host_cpu_mhz = _detect_host_cpu_mhz(host)
    reservation_mhz = cpu_count * host_cpu_mhz if bool(enabled) else 0

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, "sched.cpu.min", str(reservation_mhz))
    reload_vm_config(host, vmid)
    return {
        "status": "success",
        "vmid": vmid,
        "modification": "reserve_all_cpu",
        "enabled": bool(enabled),
        "message": f"Full CPU reservation {'enabled' if enabled else 'disabled'}. Configuration reloaded.",
    }


def add_pci_passthrough(host, vmid, vmx_path, pci_id):
    """Attach a PCI passthrough device. VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    if not pci_id:
        raise ValueError("pci_id is required")

    vmx_dict = get_vmx_content(host, vmx_path)
    used = []
    for key in vmx_dict.keys():
        m = re.match(r'^pciPassthru(\d+)\.present$', key)
        if m and str(vmx_dict.get(key, '')).upper() == 'TRUE':
            used.append(int(m.group(1)))

    slot = 0
    while slot in used:
        slot += 1

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, f'pciPassthru{slot}.present', 'TRUE')
    _vmx_set_key(host, vmx_path, f'pciPassthru{slot}.id', str(pci_id))
    reload_vm_config(host, vmid)
    return {
        "status": "success",
        "vmid": vmid,
        "modification": "add_pci_passthrough",
        "slot": slot,
        "pci_id": str(pci_id),
        "message": f"PCI device {pci_id} attached at pciPassthru{slot}.",
    }


def remove_pci_passthrough(host, vmid, vmx_path, slot):
    """Detach a PCI passthrough device by slot index. VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_remove_prefix(host, vmx_path, f'pciPassthru{int(slot)}.')
    reload_vm_config(host, vmid)
    return {
        "status": "success",
        "vmid": vmid,
        "modification": "remove_pci_passthrough",
        "slot": int(slot),
        "message": f"PCI passthrough slot pciPassthru{int(slot)} removed.",
    }


def add_disk(host, vmid, vmx_path, disk_size_gb, disk_name=None, datastore=None):
    """Add a new virtual disk. VM must be powered off."""
    if not disk_name:
        disk_name = f"disk_{vmid}"

    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    # Determine where to place the VMDK
    if datastore:
        vm_name = vmx_path.rsplit('/', 1)[0].rsplit('/', 1)[-1]
        disk_dir = f"/vmfs/volumes/{datastore}/{vm_name}"
        host.run(f"mkdir -p '{disk_dir}'")
        vmdk_path = f"{disk_dir}/{disk_name}.vmdk"
    else:
        vm_dir = vmx_path.rsplit('/', 1)[0]
        vmdk_path = f"{vm_dir}/{disk_name}.vmdk"

    # Read VMX (read-only) to find next available SCSI slot
    vmx_dict = get_vmx_content(host, vmx_path)
    existing_disks = [k for k in vmx_dict if 'scsi' in k and ':' in k and 'fileName' in k]
    next_unit = len(existing_disks)

    # Create the VMDK file on the host
    host.run(f"vmkfstools -c {disk_size_gb}G -d thin '{vmdk_path}'")

    # Append disk entries to VMX (never rewrites the file)
    _backup_vmx(host, vmx_path)
    _vmx_append_keys(host, vmx_path, {
        f'scsi0:{next_unit}.present': 'TRUE',
        f'scsi0:{next_unit}.fileName': vmdk_path,
    })
    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modification": "disk_added",
        "disk_name": disk_name,
        "disk_size": f"{disk_size_gb}GB",
        "disk_path": vmdk_path,
        "message": f"Disk {disk_name} ({disk_size_gb}GB) added. Configuration reloaded."
    }


def add_network(host, vmid, vmx_path, network_name="VM Network", adapter_type="e1000"):
    """Add a new network adapter. VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    # Read VMX (read-only) to find next available ethernet slot
    vmx_dict = get_vmx_content(host, vmx_path)
    existing_nics = [k for k in vmx_dict if k.startswith('ethernet') and '.present' in k]
    next_nic = len(existing_nics)

    # Append NIC entries to VMX
    _backup_vmx(host, vmx_path)
    _vmx_append_keys(host, vmx_path, {
        f'ethernet{next_nic}.present': 'TRUE',
        f'ethernet{next_nic}.virtualDev': adapter_type,
        f'ethernet{next_nic}.networkName': network_name,
        f'ethernet{next_nic}.addressType': 'generated',
    })
    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modification": "network_added",
        "network_name": network_name,
        "adapter_type": adapter_type,
        "nic_number": next_nic,
        "message": f"Network adapter {next_nic} ({adapter_type}) added to network '{network_name}'. Configuration reloaded."
    }


def remove_disk(host, vmid, vmx_path, disk_unit):
    """Remove a virtual disk by SCSI unit number. VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_remove_prefix(host, vmx_path, f'scsi0:{disk_unit}.')
    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modification": "disk_removed",
        "disk_unit": disk_unit,
        "message": f"Disk unit scsi0:{disk_unit} removed. Configuration reloaded."
    }


def remove_network(host, vmid, vmx_path, nic_number):
    """Remove a network adapter by NIC number. VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_remove_prefix(host, vmx_path, f'ethernet{nic_number}.')
    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modification": "network_removed",
        "nic_number": nic_number,
        "message": f"Network adapter ethernet{nic_number} removed. Configuration reloaded."
    }


def modify_vm_hardware_version(host, vmid, vmx_path, hw_version="13"):
    """Upgrade the virtual hardware version. VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, 'virtualHW.version', str(hw_version))
    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modification": "hardware_version",
        "new_version": hw_version,
        "message": f"Hardware version updated to {hw_version}. Configuration reloaded."
    }


def modify_guest_os(host, vmid, vmx_path, guest_os):
    """Modify the guest OS type. VM must be powered off."""
    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)
    _vmx_set_key(host, vmx_path, 'guestOS', guest_os)
    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modification": "guest_os",
        "new_os": guest_os,
        "message": f"Guest OS type updated to {guest_os}. Configuration reloaded."
    }


def batch_modify_vm(host, vmid, vmx_path, modifications):
    """
    Apply multiple modifications to a VM at once.
    Uses targeted sed/append — never rewrites the whole VMX.
    """
    results = []

    state = get_vm_state(host, vmid)
    if state == "poweredOn":
        power_off_vm(host, vmid)

    _backup_vmx(host, vmx_path)

    # Simple key modifications via sed
    if "cpu" in modifications:
        _vmx_set_key(host, vmx_path, 'numvcpus', str(modifications["cpu"]))
        results.append({"modification": "cpu", "value": modifications["cpu"]})

    if "memory" in modifications:
        _vmx_set_key(host, vmx_path, 'memsize', str(modifications["memory"]))
        results.append({"modification": "memory", "value": modifications["memory"]})

    if "hw_version" in modifications:
        _vmx_set_key(host, vmx_path, 'virtualHW.version', str(modifications["hw_version"]))
        results.append({"modification": "hw_version", "value": modifications["hw_version"]})

    if "guest_os" in modifications:
        _vmx_set_key(host, vmx_path, 'guestOS', modifications["guest_os"])
        results.append({"modification": "guest_os", "value": modifications["guest_os"]})

    # Disks — need to read VMX once to find next slot, then append
    if "disks" in modifications:
        vmx_dict = get_vmx_content(host, vmx_path)
        vm_dir = vmx_path.rsplit('/', 1)[0]
        existing_disks = [k for k in vmx_dict if 'scsi' in k and ':' in k and 'fileName' in k]
        next_unit = len(existing_disks)

        for disk in modifications["disks"]:
            disk_name = disk.get("name", f"disk_{next_unit}")
            disk_size = disk.get("size", 50)
            vmdk_path = f"{vm_dir}/{disk_name}.vmdk"

            host.run(f"vmkfstools -c {disk_size}G -d thin '{vmdk_path}'")
            _vmx_append_keys(host, vmx_path, {
                f'scsi0:{next_unit}.present': 'TRUE',
                f'scsi0:{next_unit}.fileName': vmdk_path,
            })
            results.append({"modification": "disk_added", "name": disk_name, "size": disk_size})
            next_unit += 1

    # Networks — same pattern
    if "networks" in modifications:
        vmx_dict = get_vmx_content(host, vmx_path)
        existing_nics = [k for k in vmx_dict if k.startswith('ethernet') and '.present' in k]
        next_nic = len(existing_nics)

        for network in modifications["networks"]:
            net_name = network.get("network", "VM Network")
            adapter_type = network.get("adapter_type", "e1000")

            _vmx_append_keys(host, vmx_path, {
                f'ethernet{next_nic}.present': 'TRUE',
                f'ethernet{next_nic}.virtualDev': adapter_type,
                f'ethernet{next_nic}.networkName': net_name,
                f'ethernet{next_nic}.addressType': 'generated',
            })
            results.append({"modification": "network_added", "network": net_name, "adapter": adapter_type})
            next_nic += 1

    reload_vm_config(host, vmid)

    return {
        "status": "success",
        "vmid": vmid,
        "modifications_applied": results,
        "message": f"Applied {len(results)} modifications. Configuration reloaded."
    }
