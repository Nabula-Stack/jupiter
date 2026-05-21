
import re

# --- READ / LIST FUNCTIONS ---
def list_vswitches(conn):
    """Lists all virtual switches with detailed information."""
    if hasattr(conn, "list_vswitches"):
        out = []
        for sw in conn.list_vswitches() or []:
            out.append(
                {
                    "name": str(getattr(sw, "name", "") or ""),
                    "num_ports": str(getattr(sw, "portgroup_count", 0) or 0),
                    "mtu": str(getattr(sw, "mtu", "--") or "--"),
                    "uplinks": [],
                }
            )
        return out

    raw = conn.run("esxcli network vswitch standard list")
    vswitches = []
    current_vswitch = None
    for line in raw.splitlines():
        if line.startswith("Name:"):
            if current_vswitch:
                vswitches.append(current_vswitch)
            name = line.split(":", 1)[1].strip()
            current_vswitch = {"name": name, "num_ports": "0", "mtu": "--", "uplinks": []}
        elif line.startswith("Num Ports:") and current_vswitch:
            current_vswitch["num_ports"] = line.split(":", 1)[1].strip()
        elif line.startswith("MTU:") and current_vswitch:
            current_vswitch["mtu"] = line.split(":", 1)[1].strip()
        elif "Uplinks:" in line and current_vswitch:
            uplinks_str = line.split(":", 1)[1].strip()
            if uplinks_str:
                current_vswitch["uplinks"] = [u.strip() for u in uplinks_str.split(",")]
    if current_vswitch:
        vswitches.append(current_vswitch)
    return vswitches


def list_portgroups(conn):
    """Lists all port groups with detailed information."""
    if hasattr(conn, "list_portgroups"):
        out = []
        for pg in conn.list_portgroups() or []:
            out.append(
                {
                    "name": str(getattr(pg, "name", "") or ""),
                    "vswitch": str(getattr(pg, "vswitch", "") or ""),
                    "vlan": str(getattr(pg, "vlan_id", 0) or 0),
                    "mtu": "--",
                }
            )
        return out

    raw = conn.run("esxcli network vswitch standard portgroup list")
    groups = []
    lines = raw.splitlines()
    if len(lines) > 2:
        for line in lines[2:]:
            parts = re.split(r'\s{2,}', line.strip())
            if len(parts) >= 4:
                groups.append({
                    "name": parts[0],
                    "vswitch": parts[1],
                    "vlan": parts[2],
                    "mtu": parts[3] if len(parts) > 3 else "--"
                })
    return groups


# --- WRITE / ACTION FUNCTIONS ---
def create_vswitch(conn, name):
    if hasattr(conn, "create_vswitch"):
        result = conn.create_vswitch(name)
        return str((result or {}).get("status", "success"))
    return conn.run(f"esxcli network vswitch standard add -v '{name}'")

def remove_vswitch(conn, name):
    if hasattr(conn, "remove_vswitch"):
        result = conn.remove_vswitch(name)
        return str((result or {}).get("status", "success"))
    return conn.run(f"esxcli network vswitch standard remove -v '{name}'")

def add_portgroup(conn, vswitch, name):
    if hasattr(conn, "add_portgroup"):
        result = conn.add_portgroup(vswitch, name, vlan=0)
        return str((result or {}).get("status", "success"))
    return conn.run(f"esxcli network vswitch standard portgroup add -p '{name}' -v '{vswitch}'")

def set_portgroup_vlan(conn, name, vlan):
    if hasattr(conn, "set_portgroup_vlan"):
        result = conn.set_portgroup_vlan(name, int(vlan))
        return str((result or {}).get("status", "success"))
    return conn.run(f"esxcli network vswitch standard portgroup set -p '{name}' -v {vlan}")

def remove_portgroup(conn, name):
    if hasattr(conn, "remove_portgroup"):
        result = conn.remove_portgroup(name)
        return str((result or {}).get("status", "success"))
    return conn.run(f"esxcli network vswitch standard portgroup remove -p '{name}'")


# --- ADVANCED NETWORK INFO ---

def _parse_esxcli_table(raw):
    """Generic column-split parser for esxcli tabular output (header + dash-line + data rows)."""
    rows = []
    lines = (raw or "").strip().splitlines()
    if len(lines) < 3:
        return rows
    header_line = lines[0]
    dash_line = lines[1]
    col_starts = [0]
    in_dash = False
    for i, ch in enumerate(dash_line):
        if ch == '-' and not in_dash:
            in_dash = True
        elif ch == ' ' and in_dash:
            in_dash = False
            if i + 1 < len(dash_line) and dash_line[i + 1] == '-':
                col_starts.append(i + 1)
    col_starts.append(len(header_line) + 200)
    headers = []
    for i in range(len(col_starts) - 1):
        h = header_line[col_starts[i]:col_starts[i + 1]].strip()
        headers.append(h.lower().replace(' ', '_'))
    for line in lines[2:]:
        if not line.strip():
            continue
        row = {}
        for i in range(len(col_starts) - 1):
            val = line[col_starts[i]:col_starts[i + 1]] if col_starts[i] < len(line) else ''
            row[headers[i]] = val.strip()
        rows.append(row)
    return rows


def _rows_have_key(rows, candidates):
    """Return True when at least one row contains any candidate key."""
    if not rows:
        return False
    for row in rows:
        for key in candidates:
            if key in row:
                return True
    return False


def _parse_split_table(raw):
    """Fallback parser for space-separated table outputs."""
    lines = [l.rstrip() for l in (raw or "").splitlines() if l.strip()]
    if len(lines) < 3:
        return []

    header_index = None
    for i in range(len(lines) - 1):
        if set(lines[i + 1].strip()) <= {"-", " "} and "-" in lines[i + 1]:
            header_index = i
            break
    if header_index is None:
        return []

    headers = [h.strip().lower().replace(" ", "_") for h in re.split(r"\s{2,}", lines[header_index].strip()) if h.strip()]
    if not headers:
        return []

    rows = []
    for line in lines[header_index + 2:]:
        parts = [p.strip() for p in re.split(r"\s{2,}", line.strip())]
        if not parts:
            continue
        row = {}
        for idx, key in enumerate(headers):
            row[key] = parts[idx] if idx < len(parts) else ""
        rows.append(row)
    return rows


def _parse_key_value_blocks(raw):
    """Parse outputs that look like grouped key:value sections."""
    blocks = []
    current = {}
    for line in (raw or "").splitlines():
        original = line.rstrip()
        text = original.strip()
        if not text:
            if current:
                blocks.append(current)
                current = {}
            continue
        # In some ESXi outputs each block starts with an unindented label line
        # like: defaultTcpipStack
        if ":" not in text and original == text:
            if current:
                blocks.append(current)
                current = {}
            current["name"] = text
            continue
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        current[key] = value.strip()
    if current:
        blocks.append(current)
    return blocks


def get_vmkernel_nics(conn):
    """Lists VMkernel (vmk) adapters with IPv4 configuration."""
    if hasattr(conn, "list_vmkernel_nics"):
        return conn.list_vmkernel_nics() or []

    ipv4_raw = conn.run("esxcli network ip interface ipv4 get 2>/dev/null") or ""
    intf_raw = conn.run("esxcli network ip interface list 2>/dev/null") or ""
    intf_rows = _parse_esxcli_table(intf_raw)
    ipv4_rows = _parse_esxcli_table(ipv4_raw)
    intf_map = {r.get('name', r.get('interface', '')): r for r in intf_rows}
    nics = []
    for row in ipv4_rows:
        iface = row.get('name', row.get('interface', ''))
        if not iface:
            continue
        extra = intf_map.get(iface, {})
        nics.append({
            "interface": iface,
            "ip":        row.get('ipv4_address', row.get('ipv4address', '--')),
            "netmask":   row.get('ipv4_netmask', row.get('ipv4netmask', '--')),
            "type":      row.get('address_type', row.get('addresstype', row.get('type', '--'))),
            "mtu":       extra.get('mtu', '--'),
            "enabled":   extra.get('enabled', extra.get('enable', '--')),
        })
    return nics


def get_tcp_ip_stacks(conn):
    """Lists TCP/IP network stack instances."""
    if hasattr(conn, "list_tcp_ip_stacks"):
        return conn.list_tcp_ip_stacks() or []

    raw = conn.run("esxcli network ip netstack list 2>/dev/null") or ""
    rows = _parse_esxcli_table(raw)
    if not _rows_have_key(rows, ["key", "name", "state"]):
        rows = _parse_split_table(raw)
    if not _rows_have_key(rows, ["key", "name", "state"]):
        rows = _parse_key_value_blocks(raw)
    stacks = []
    for row in rows:
        name = row.get('key') or row.get('name') or row.get('netstack_instance') or '--'
        enabled = row.get('enabled') or row.get('is_enabled') or row.get('active') or row.get('state') or '--'
        ccalgo = (
            row.get('congestion_control_algorithm')
            or row.get('default_congestion_control_algorithm')
            or row.get('cca')
            or row.get('ccalgo')
            or '--'
        )
        stacks.append({
            "name": name,
            "enabled": enabled,
            "ccalgo": ccalgo,
        })
    return stacks


def get_firewall_rules(conn):
    """Lists firewall ruleset entries (enabled/disabled state)."""
    if hasattr(conn, "list_firewall_rules"):
        return conn.list_firewall_rules() or []

    raw = conn.run("esxcli network firewall ruleset list 2>/dev/null") or ""
    rows = _parse_esxcli_table(raw)
    if not _rows_have_key(rows, ["name", "enabled"]):
        rows = _parse_split_table(raw)
    rules = []
    for row in rows:
        name = row.get('name') or row.get('ruleset') or row.get('ruleset_name') or '--'
        enabled = row.get('enabled') or row.get('is_enabled') or '--'
        allow_in = (
            row.get('allow_incoming')
            or row.get('allowincoming')
            or row.get('allow_in')
            or row.get('enable/disable_configurable')
            or row.get('enable_disable_configurable')
            or row.get('allowed_all_ip')
            or row.get('allowedallip')
            or '--'
        )
        allow_out = (
            row.get('allow_outgoing')
            or row.get('allowoutgoing')
            or row.get('allow_out')
            or row.get('allowed_ip_configurable')
            or row.get('allowed_ip_addresses')
            or row.get('allowedipaddresses')
            or '--'
        )
        required = row.get('required') or row.get('mandatory') or '--'
        rules.append({
            "name": name,
            "enabled": enabled,
            "allow_incoming": allow_in,
            "allow_outgoing": allow_out,
            "required": required,
        })
    return rules