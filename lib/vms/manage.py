import re
from . import info as vm_info
from .modify import _ds_to_path

def list_vms_summary(host):
    if hasattr(host, "list_vms"):
        vms = []
        for vm in host.list_vms() or []:
            vmid = str(getattr(vm, "uuid", "") or getattr(vm, "name", ""))
            vms.append({
                "vmid": vmid,
                "name": getattr(vm, "name", "Unknown"),
                "vmx": getattr(vm, "datastorage", "Unknown") or "Unknown",
            })
        return vms

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

def list_vms_with_stats(host):
    if hasattr(host, "list_vms"):
        out = []
        for vm in host.list_vms() or []:
            ip_list = [ip for ip in (getattr(vm, "ip_addresses", []) or []) if ip and ip != "0.0.0.0"]
            out.append({
                "vmid": str(getattr(vm, "uuid", "") or getattr(vm, "name", "")),
                "vm_name": getattr(vm, "name", "Unknown"),
                "vmx": getattr(vm, "datastorage", "") or "",
                "uuid": getattr(vm, "uuid", None),
                "hw_version": None,
                "power_state": str(getattr(vm, "power_state", "unknown")),
                "overall_status": "green",
                "guest_name": getattr(vm, "guest_os", None) or "Unknown",
                "distro": "N/A",
                "kernel": "N/A",
                "ip_address": next(iter(ip_list), None),
                "dns_name": getattr(vm, "dns_name", None) or getattr(vm, "name", "Unknown"),
                "tools_status": getattr(vm, "tools_status", None),
                "tools_running": getattr(vm, "tools_running", False),
                "networks": [{"network": "Unknown", "mac": "N/A", "ip": list(set(ip_list))}],
                "dns_servers": [],
                "num_cpu": int(getattr(vm, "cpu_count", 0) or 0),
                "memory_mb": int(getattr(vm, "memory_mb", 0) or 0),
                "storage_used_gb": 0.0,
                "storage_provisioned_gb": 0.0,
                "cpu_usage_mhz": 0,
                "memory_usage_mb": 0,
                "uptime_human": "N/A",
            })
        return out

    from concurrent.futures import ThreadPoolExecutor, as_completed
    vms = list_vms_summary(host)
    enriched_vms = []

    def get_full_vm_data(vm):
        details = vm_info.get_vm_details(host, vm['vmid'])
        stats = vm_info.get_vm_runtime_stats(host, vm['vmid'])
        return {**vm, **details, **stats}

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(get_full_vm_data, vm) for vm in vms]
        for future in as_completed(futures):
            try:
                enriched_vms.append(future.result())
            except Exception:
                continue
    return enriched_vms

def power_op(host, vmid, state):
    """Expects state like 'power.on' or 'power.shutdown'"""
    if hasattr(host, "set_vm_power_state_by_identifier"):
        result = host.set_vm_power_state_by_identifier(str(vmid), state)
        if (result or {}).get("status") == "error":
            return f"Error: {result.get('message', 'Power operation failed')}"
        return str((result or {}).get("message", "Success"))
    return host.run(f"vim-cmd vmsvc/{state} {vmid}")

def snapshot_op(host, vmid, op, name=None, description="Admin Snapshot"):
    """Handles snapshot lifecycle"""
    if hasattr(host, "vm_snapshot_action"):
        result = host.vm_snapshot_action(str(vmid), op, name=name, description=description)
        if (result or {}).get("status") == "error":
            return f"Error: {result.get('message', 'Snapshot operation failed')}"
        if op == "list":
            return str((result or {}).get("snapshots", []))
        return str((result or {}).get("message", "Success"))

    if op == "create":
        return host.run(f'vim-cmd vmsvc/snapshot.create {vmid} "{name}" "{description}" 1 0')
    if op == "list":
        return host.run(f"vim-cmd vmsvc/snapshot.get {vmid}")
    if op == "removeall":
        return host.run(f"vim-cmd vmsvc/snapshot.removeall {vmid}")
    if op == "revert":
        return host.run(f"vim-cmd vmsvc/snapshot.revert {vmid} 0 0")

def unregister_vm(host, vmid):
    """Removes VM from inventory only. Files remain on datastore."""
    if hasattr(host, "unregister_vm_by_identifier"):
        result = host.unregister_vm_by_identifier(str(vmid))
        if (result or {}).get("status") == "error":
            return f"Error: {result.get('message', 'Unregister failed')}"
        return str((result or {}).get("message", "Success"))
    return host.run(f"vim-cmd vmsvc/unregister {vmid}")


def destroy_vm(host, vmid):
    """Permanently destroy VM. Prefer native destroy; fall back to unregister + datastore cleanup."""
    import os

    # 1. Get the VMX path so we know the VM folder
    vms = list_vms_summary(host)
    vmx_path = None
    for vm in vms:
        if vm["vmid"] == vmid:
            vmx_path = vm["vmx"]
            break

    # 2. Power off if running
    try:
        state_raw = host.run(f"vim-cmd vmsvc/power.getstate {vmid}")
        if "Powered on" in state_raw:
            host.run(f"vim-cmd vmsvc/power.off {vmid}")
    except Exception:
        pass

    # 3. Prefer hard destroy semantics in one operation.
    destroy_out = host.run(f"vim-cmd vmsvc/destroy {vmid}")
    if not (isinstance(destroy_out, str) and destroy_out.startswith("Error:")):
        return f"VM {vmid} permanently destroyed."

    # Fall back to unregister path when destroy is unavailable/fails.
    unregister_out = host.run(f"vim-cmd vmsvc/unregister {vmid}")
    if isinstance(unregister_out, str) and unregister_out.startswith("Error:"):
        return unregister_out

    # 4. Delete the entire VM folder from the datastore
    if vmx_path:
        vm_dir = os.path.dirname(vmx_path)
        # Safety: only allow deletion under /vmfs/volumes
        if vm_dir and vm_dir.startswith("/vmfs/volumes/") and len(vm_dir.split("/")) >= 5:
            result = host.run(f"rm -rf '{vm_dir}'")
            if isinstance(result, str) and result.startswith("Error:"):
                return result
            return f"VM {vmid} destroyed after unregister fallback. Folder deleted: {vm_dir}"

    return (
        "Error: VM was unregistered but full delete could not be confirmed because VM folder path "
        "could not be determined."
    )

def register_vm(host, vmx_path):
    if hasattr(host, "register_vm_from_path"):
        result = host.register_vm_from_path(vmx_path)
        if (result or {}).get("status") == "error":
            return f"Error: {result.get('message', 'Register failed')}"
        return str((result or {}).get("message", "Success"))
    return host.run(f"vim-cmd solo/registervm '{vmx_path}'")

def get_vm_network_info(host, vmid):
    if hasattr(host, "get_vm_by_identifier"):
        vm = host.get_vm_by_identifier(str(vmid))
        if vm is None:
            return [{"network": "Disconnected", "mac": "N/A", "ip": []}]
        ips = [ip for ip in (getattr(vm, "ip_addresses", []) or []) if ip and ip != "0.0.0.0"]
        return [{"network": "Unknown", "mac": "N/A", "ip": list(set(ips))}]

    results = []
    try:
        guest = host.run(f"vim-cmd vmsvc/get.guest {vmid}")
    except Exception:
        guest = ""

    nic_blocks = re.findall(r'\(vim\.vm\.GuestInfo\.NicInfo\)\s*\{(.+?)\}', guest, re.DOTALL)
    for block in nic_blocks:
        network = re.search(r'network\s*=\s*"([^"]+)"', block)
        mac = re.search(r'macAddress\s*=\s*"([^"]+)"', block)
        ips = re.findall(r'(\d+\.\d+\.\d+\.\d+)', block)
        clean_ips = [ip for ip in ips if ip and ip != "0.0.0.0"]
        results.append({
            "network": network.group(1) if network else "Unknown",
            "mac": mac.group(1) if mac else "N/A",
            "ip": list(set(clean_ips))
        })

    if not results:
        try:
            config = host.run(f"vim-cmd vmsvc/get.config {vmid}")
            dev_blocks = re.findall(r'(vim\.vm\.device\.VirtualEthernetCard.*?\})', config, re.DOTALL)
            for dev in dev_blocks:
                network = re.search(r'networkName\s*=\s*"([^"]+)"', dev)
                mac = re.search(r'macAddress\s*=\s*"([^"]+)"', dev)
                results.append({"network": network.group(1) if network else "Unknown", "mac": mac.group(1) if mac else "N/A", "ip": []})
        except:
            return [{"network": "Disconnected", "mac": "N/A", "ip": []}]
    return results