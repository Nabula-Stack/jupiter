# lib/host/manage.py
import re

def get_host_info(conn):
    """Fetches hardware and version details from the ESXi host."""
    if hasattr(conn, "get_host_summary") and hasattr(conn, "get_host_hardware"):
        try:
            summary = conn.get_host_summary()
            hardware = conn.get_host_hardware()
            return {
                "version": summary.version,
                "vendor": summary.vendor,
                "model": summary.model,
                "cpu_count": int(hardware.cpu_cores or 0),
                "memory_total_gb": float(hardware.memory_gb or 0),
            }
        except Exception as e:
            print(f"❌ API Error fetching host info: {e}")

    try:
        # Get Version/Build
        version_raw = conn.run("esxcli system version get")
        
        # Get Hardware Model/Vendor
        hw_raw = conn.run("esxcli hardware platform get")
        
        # Get CPU Count
        cpu_raw = conn.run("esxcli hardware cpu list | grep 'ID:' | wc -l")

        # Get Memory (returns in Bytes, we convert to GB)
        mem_raw = conn.run("esxcli hardware memory get | grep 'Physical Memory:'")
        
        # Simple parsing logic
        info = {
            "version": version_raw.strip() if version_raw else "Unknown",
            "vendor": "Unknown",
            "model": "Unknown",
            "cpu_count": int(cpu_raw.strip()) if cpu_raw and cpu_raw.strip().isdigit() else 0,
            "memory_total_gb": 0
        }

        if hw_raw:
            # Look for Vendor: and Product Name: in the output
            vendor_match = re.search(r"Vendor Name:\s+(.*)", hw_raw)
            model_match = re.search(r"Product Name:\s+(.*)", hw_raw)
            if vendor_match: info["vendor"] = vendor_match.group(1).strip()
            if model_match: info["model"] = model_match.group(1).strip()

        if mem_raw:
            # Extract numbers from "Physical Memory: 34359738368 Bytes"
            mem_bytes = re.search(r"(\d+)", mem_raw)
            if mem_bytes:
                info["memory_total_gb"] = round(int(mem_bytes.group(1)) / (1024**3), 2)

        return info
    except Exception as e:
        print(f"❌ Library Error fetching host info: {e}")
        return None

# --- Action functions required by lib/host/__init__.py ---

def add_license(conn, serial_key):
    """Assigns a new license key."""
    if hasattr(conn, "add_license"):
        result = conn.add_license(serial_key)
        return str((result or {}).get("message", "License operation completed"))
    if hasattr(conn, "get_host_summary"):
        return "Not supported via vSphere API for standalone ESXi in this implementation."
    result = conn.run(f"esxcli system settings license set --license {serial_key}")
    return result.strip() if result else "Success"

def reboot_host(conn):
    """Reboots the physical ESXi host."""
    if hasattr(conn, "reboot_host"):
        result = conn.reboot_host(force=True)
        return (result or {}).get("message", "Reboot command sent")
    return conn.run("reboot")

def shutdown_host(conn):
    """Shuts down the physical ESXi host."""
    if hasattr(conn, "shutdown_host"):
        result = conn.shutdown_host(force=True)
        return (result or {}).get("message", "Shutdown command sent")
    return conn.run("poweroff")

def set_maintenance_mode(conn, enable: bool):
    """Puts host into maintenance mode."""
    if hasattr(conn, "set_host_maintenance_mode"):
        result = conn.set_host_maintenance_mode(enable)
        return (result or {}).get("message", str(result))
    state = "true" if enable else "false"
    # timeout=0 ensures it waits for VMs to move/shutdown if configured
    return conn.run(f"esxcli system maintenanceMode set --enable {state} --timeout 0")

def set_lockdown_mode(conn, enable: bool):
    """Enables or disables ESXi lockdown mode via vim-cmd.

    WARNING: Enabling lockdown restricts SSH access to DCUI and exception users only.
    Ref: https://docs.vmware.com/en/VMware-vSphere/7.0/com.vmware.vsphere.security.doc/GUID-5899B08D-B82E-40CF-A01E-5EB9F21CE0F2.html
    """
    if hasattr(conn, "set_lockdown_mode"):
        result = conn.set_lockdown_mode(enable)
        return (result or {}).get("lockdown_mode", str(result))

    if enable:
        result = conn.run("vim-cmd hostsvc/enable_lockdown")
    else:
        result = conn.run("vim-cmd hostsvc/disable_lockdown")
    return (result or "").strip() or ("Lockdown enabled" if enable else "Lockdown disabled")

def get_lockdown_status(conn):
    """Returns the current lockdown mode state (lockdownDisabled / lockdownNormal / lockdownStrict)."""
    if hasattr(conn, "get_lockdown_status"):
        result = conn.get_lockdown_status()
        return str((result or {}).get("lockdown_mode", "unknown"))

    result = conn.run(
        "python -c \""
        "import pyVim.connect, ssl; "
        "print('unavailable')"
        "\" 2>/dev/null || "
        "esxcli system settings advanced list -o /Net/BlockGuestBcastNotify 2>/dev/null || "
        "echo 'unknown'"
    )
    return (result or "unknown").strip()

def get_host_permissions(conn):
    """Returns local user permission entries for the ESXi host.

    Ref: https://kb.vmware.com/s/article/1025569
    """
    if hasattr(conn, "get_host_permissions"):
        perms = conn.get_host_permissions() or []
        if not perms:
            return "No permission data returned"
        lines = ["Principal IsGroup Role PropagateToChildren"]
        for p in perms:
            lines.append(
                f"{p.get('principal','')} {str(bool(p.get('is_group', False))).lower()} "
                f"{p.get('role','')} {str(bool(p.get('propagate', False))).lower()}"
            )
        return "\n".join(lines)

    result = conn.run("esxcli system permission list 2>&1")
    return (result or "No permission data returned").strip()

def generate_support_bundle(conn):
    """Runs vm-support to create a diagnostic bundle under /tmp.

    Returns the output containing the generated archive path.
    Ref: https://kb.vmware.com/s/article/2032892
    """
    if hasattr(conn, "generate_support_bundle"):
        result = conn.generate_support_bundle()
        if (result or {}).get("status") == "success":
            return str((result or {}).get("result") or "Support bundle generated")
        return str((result or {}).get("message") or "Support bundle generation failed")

    output = conn.run(
        "vm-support -w /tmp 2>&1 | grep -E '(Saving|Created|esx-|error)' | tail -10"
    )
    return (output or "vm-support ran but produced no output").strip()