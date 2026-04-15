import time
import re

def cold_migrate(src_host, dest_host, vmid, vm_name, src_ds, dest_ds, dest_ip):
    """
    Performs a full cross-host migration.
    1. Validates VM state.
    2. Powers off if necessary.
    3. Unregisters from Source.
    4. Moves folder via SCP (with key-check bypass).
    5. Registers on Destination.
    """
    
    # --- 1. PRE-CHECK & SHUTDOWN ---
    # Get current state to see if we need to kill it
    state_raw = src_host.run(f"vim-cmd vmsvc/power.getstate {vmid}")
    
    if "Powered on" in state_raw:
        # We try a hard power.off because shutdown (guest) might hang
        src_host.run(f"vim-cmd vmsvc/power.off {vmid}")
        # Give ESXi a few seconds to flush the vmdk locks
        time.sleep(5)

    # --- 2. DESTINATION PREP ---
    # Ensure the destination datastore path exists
    dest_path = f"/vmfs/volumes/{dest_ds}/{vm_name}"
    dest_host.run(f"mkdir -p '{dest_path}'")

    # --- 3. UNREGISTER SOURCE ---
    # We must unregister so the files aren't 'in use' by the management service
    src_host.run(f"vim-cmd vmsvc/unregister {vmid}")

    # --- 4. DATA TRANSFER (THE SCP ENGINE) ---
    # -r: recursive
    # -p: preserve file permissions (Critical for .vmdk descriptors)
    # -o StrictHostKeyChecking=no: Don't ask 'Are you sure' for the SSH fingerprint
    # -o BatchMode=yes: Don't hang asking for a password if keys aren't set up
    
    source_path = f"/vmfs/volumes/{src_ds}/{vm_name}/*"
    
    transfer_cmd = (
        f"scp -r -p -o StrictHostKeyChecking=no -o BatchMode=yes "
        f"{source_path} root@{dest_ip}:{dest_path}/"
    )
    
    # This call will block your API thread until the bytes are moved
    transfer_result = src_host.run(transfer_cmd)
    
    # Check if scp failed (BatchMode returns error if password is required)
    if "Permission denied" in transfer_result or "Lost connection" in transfer_result:
        return {
            "status": "failed",
            "error": "SSH Key authentication failed between hosts. Set up authorized_keys first.",
            "raw": transfer_result
        }

    # --- 5. REGISTRATION ---
    dest_vmx = f"{dest_path}/{vm_name}.vmx"
    
    # Register the VM on the new host
    # This returns the NEW vmid on the destination host
    register_output = dest_host.run(f"vim-cmd solo/registervm '{dest_vmx}'")
    
    # Clean up the output to get just the ID
    new_vmid = register_output.strip()

    return {
        "status": "success",
        "old_vmid": vmid,
        "new_vmid": new_vmid,
        "transferred_to": dest_ip,
        "destination_path": dest_vmx
    }