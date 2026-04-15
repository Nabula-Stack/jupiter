import re

# --- Discovery / Listing ---

def list_datastores(conn):
    """Lists datastores and safely handles the esxcli filesystem output.

    esxcli storage filesystem list columns (split by 2+ spaces):
      [0] Mount Point   [1] Volume Name   [2] UUID
      [3] Mounted       [4] Type           [5] Size (bytes)   [6] Free (bytes)
    """
    raw = conn.run("esxcli storage filesystem list")
    ds_list = []
    lines = raw.splitlines()

    if len(lines) > 2:
        for line in lines[2:]:
            parts = re.split(r'\s{2,}', line.strip())
            if len(parts) < 7:
                continue

            name = parts[1].strip()
            mounted = parts[3].strip().lower() == "true"
            fs_type = parts[4].strip()

            # Skip unmounted or unnamed entries
            if not name or not mounted:
                continue

            try:
                capacity = int(parts[5])
                free = int(parts[6])
            except (ValueError, IndexError):
                continue

            used = capacity - free

            ds_list.append({
                "name": name,
                "type": fs_type,
                "capacity": capacity,
                "used": used,
                "free": free,
                "mounted": mounted,
            })
    return ds_list

def list_available_disks(conn):
    """Lists physical storage devices recognized by the kernel."""
    return conn.run("esxcli storage core device list")


# --- System Operations ---

def rescan_storage(conn):
    """Triggers a rescan of all HBAs and storage devices."""
    conn.run("esxcli storage core adapter rescan --all")
    conn.run("esxcli storage core device rescan")
    return "Storage rescan triggered."

def refresh_vmfs(conn):
    """Probes all adapters for new VMFS volumes."""
    return conn.run("vmkfstools -V")


# --- Datastore Management (The Heavy Lifting) ---

def create_datastore(conn, disk_id, ds_name):
    """
    Formats a physical disk and creates a VMFS6 datastore.
    disk_id is the NAA ID (e.g., naa.600508b1001c...)
    """
    # 1. Create a new GPT partition table
    conn.run(f"partedUtil mklabel /vmfs/devices/disks/{disk_id} gpt")
    
    # 2. Calculate the end sector for full capacity
    # We use a shell pipeline to get the 4th value from the getptbl header
    get_end_sector = "partedUtil getptbl /vmfs/devices/disks/{} | head -1 | awk '{{print $4 - 1}}'".format(disk_id)
    end_sector = conn.run(get_end_sector).strip()
    
    if not end_sector.isdigit():
        return f"Error: Could not determine end sector for {disk_id}"

    # 3. Create the VMFS partition
    # GUID AA310212400F11DB9590000C2911D1B8 is the standard for VMFS
    partition_cmd = f"partedUtil setptbl /vmfs/devices/disks/{disk_id} gpt '1 2048 {end_sector} AA310212400F11DB9590000C2911D1B8 0'"
    conn.run(partition_cmd)
    
    # 4. Format as VMFS6
    return conn.run(f"vmkfstools -C vmfs6 -S {ds_name} /vmfs/devices/disks/{disk_id}:1")

def extend_datastore(conn, ds_name, disk_id):
    """Extends an existing datastore onto a new span/disk."""
    return conn.run(f"vmkfstools -Z /vmfs/devices/disks/{disk_id}:1 /vmfs/volumes/{ds_name}")

def unmount_datastore(conn, ds_name):
    """Safely unmounts a datastore from the host."""
    return conn.run(f"esxcli storage filesystem unmount -p /vmfs/volumes/{ds_name}")