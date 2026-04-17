def control_service(client, service_name, action):
    """action: start, stop, restart"""
    if hasattr(client, "control_service"):
        result = client.control_service(service_name, action)
        if str((result or {}).get("status", "success")).lower() == "error":
            return f"Error: {(result or {}).get('message', 'Service action failed')}"
        return str((result or {}).get("message") or (result or {}).get("status", "success"))
    return client.run(f"/etc/init.d/{service_name} {action}")

def list_services(client):
    """Returns a list of all services."""
    if hasattr(client, "list_services"):
        rows = client.list_services() or []
        return [str(r.get("name", "")) for r in rows if r.get("name")]
    raw = client.run("chkconfig --list | awk '{print $1}'")
    return raw.splitlines()


def list_services_with_status(client):
    """Return service name + status using a single remote shell command.

    This avoids many SSH round-trips (one status call per service), which can
    otherwise block request cancellation during ASGI shutdown.
    """
    if hasattr(client, "list_services"):
        rows = []
        for svc in client.list_services() or []:
            rows.append({"name": str(svc.get("name", "")).strip(), "status": str(svc.get("status", "Unknown")).strip()})
        return rows

    cmd = (
        "for s in $(chkconfig --list | awk '{print $1}'); do "
        "st=$(/etc/init.d/$s status 2>/dev/null | head -n1); "
        "if [ -z \"$st\" ]; then st='Unknown'; fi; "
        "printf '%s\t%s\n' \"$s\" \"$st\"; "
        "done"
    )
    raw = client.run(cmd)
    rows = []
    for line in (raw or "").splitlines():
        if not line.strip() or line.startswith("Error:"):
            continue
        if "\t" in line:
            name, status = line.split("\t", 1)
        else:
            parts = line.split(None, 1)
            name = parts[0]
            status = parts[1] if len(parts) > 1 else "Unknown"
        rows.append({"name": name.strip(), "status": status.strip()})
    return rows

def get_service_status(client, service_name):
    """Returns status of a specific service."""
    if hasattr(client, "get_service_status"):
        return str(client.get_service_status(service_name))
    return client.run(f"/etc/init.d/{service_name} status")


def set_service_policy(client, service_name, enabled):
    """Enable/disable service startup policy."""
    if hasattr(client, "set_service_policy"):
        result = client.set_service_policy(service_name, bool(enabled))
        if str((result or {}).get("status", "success")).lower() == "error":
            return f"Error: {(result or {}).get('message', 'Service policy update failed')}"
        return str((result or {}).get("policy", ""))
    state = "on" if enabled else "off"
    return client.run(f"chkconfig {service_name} {state}")