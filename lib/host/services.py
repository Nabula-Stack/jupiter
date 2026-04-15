def control_service(client, service_name, action):
    """action: start, stop, restart"""
    return client.run(f"/etc/init.d/{service_name} {action}")

def list_services(client):
    """Returns a list of all services."""
    raw = client.run("chkconfig --list | awk '{print $1}'")
    return raw.splitlines()


def list_services_with_status(client):
    """Return service name + status using a single remote shell command.

    This avoids many SSH round-trips (one status call per service), which can
    otherwise block request cancellation during ASGI shutdown.
    """
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
    return client.run(f"/etc/init.d/{service_name} status")