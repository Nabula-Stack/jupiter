import re

def get_physical_nics(host):
    """Checks if the physical cables are actually plugged in."""
    raw = host.run("esxcli network nic list")
    nics = []
    for line in raw.splitlines()[2:]:
        parts = re.split(r'\s{2,}', line.strip())
        if len(parts) >= 10:
            nics.append({
                "interface": parts[0],
                "driver": parts[2],
                "admin_status": parts[3],
                "link_status": parts[4],
                "speed": parts[5],
                "duplex": parts[6],
                "mac": parts[7],
                "mtu": parts[8],
                "description": parts[9],
            })
        elif len(parts) >= 7:
            nics.append({
                "interface": parts[0],
                "driver": parts[2] if len(parts) > 2 else "--",
                "admin_status": parts[3] if len(parts) > 3 else "--",
                "link_status": parts[4] if len(parts) > 4 else "--",
                "speed": parts[5] if len(parts) > 5 else "--",
                "duplex": parts[6] if len(parts) > 6 else "--",
                "mac": parts[7] if len(parts) > 7 else "--",
                "mtu": parts[8] if len(parts) > 8 else "--",
                "description": parts[9] if len(parts) > 9 else "--",
            })
    return nics
