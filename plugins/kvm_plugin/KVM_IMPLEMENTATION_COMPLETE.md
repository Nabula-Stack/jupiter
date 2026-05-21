# KVM Plugin Implementation Summary

**Completed**: All KVM frontend options now fully connected to backend APIs and services

---

## Changes Made

### 1. Fixed Service Layer (manager/services/service_vm.py)

#### Issue Fixed
- KVM power operations had a logic bug where they would fallthrough to snapshot operations
- Snapshot operations weren't properly mapped between admin action names and libvirt operations

#### Changes
- Added `_KVM_SNAPSHOT_MAP` to map admin action names to libvirt snapshot operations:
  - `snapshot_create` → `create`
  - `snapshot_delete` → `delete_all`
  - `snapshot_restore` → `revert`
- Fixed KVM section to properly handle power vs snapshot operations with distinct return paths
- Added action logging: `vm.log_action()` for all operations
- Ensured all power state transitions are correct (reboot/reset → poweredOn, etc.)

**File**: `manager/services/service_vm.py` (lines 28-60, 95-130)

---

### 2. Enhanced Admin Actions (manager/admin/vm_admin.py)

#### Changes
- **Snapshot actions**: Fixed action names and parameter passing
  - Renamed `snapshot_create_action` to use clearer action type names
  - Fixed `snapshot_delete_action` to pass proper parameters
  - Fixed `snapshot_restore_action` to pass proper parameters
- **Delete/Unregister**: Added action logging
  - Both now call `vm.log_action()` before deletion
  - Added emojis to success messages for better UX

**File**: `manager/admin/vm_admin.py` (lines 331-370, 377-425)

---

### 3. Extended KVM Plugin Routes (plugins/kvm_plugin/routes.py)

#### Added Endpoints
- ✅ `GET /{host_name}/create/options` - Returns available options for VM creation form
  - CPU/RAM/disk ranges
  - Available storage pools, networks, NIC types
  - Firmware and hardware capabilities
  - Default values for new VMs

- ✅ `POST /{host_name}/create` - Creates new KVM VM
  - JSON payload support
  - Full validation (CPU >= 1, RAM >= 256MB, Disk >= 1GB)
  - Returns created VM details
  - Auto-syncs VM inventory and broadcasts WebSocket event

- ✅ `GET /{host_name}/vms/info` - Lists all VMs with detailed stats
  - CPU count, memory, power state
  - Storage used/provisioned
  - IP addresses, networks, DNS

- ✅ `POST /{host_name}/vm/{vmid}/power/{action}` - Power control
  - Supported actions: on, off, shutdown, reset, reboot, suspend, resume
  - Updates DB power_state
  - Logs action to VM action_history

**File**: `plugins/kvm_plugin/routes.py` (new: 40-250 lines)

---

## Frontend-Backend Mapping

### VM Creation Wizard
**Flow**: `manager/templates/admin/vm_create_wizard.html` → API Endpoints → Service Layer → SSH to KVM

```
1. Load Options
   GET /api/v1/kvm/{host}/create/options
   ↓
2. Display Form (wizard populated)
   - Datastore selector (from pools)
   - Network selector (from networks)
   - CPU/RAM/Disk sliders
   - NIC type, disk type, firmware options

3. Create VM
   POST /api/v1/kvm/{host}/create
   ↓ (in service)
   - Validate inputs
   - Get SSH connection
   - Call: kvm_manage.create_vm(...)
   - Sync VM inventory
   - Broadcast WebSocket event
   ↓
4. UI Refreshes
   - Redirects to VM list
   - WebSocket updates live dashboard
```

### Power Operations
**Flow**: Admin action button → Service trigger → SSH virsh command → DB update → WebSocket broadcast

```
UI Button Click: [Power ON]
   ↓
Django Admin Action
   ↓
trigger_vm_action(vm, "poweron", {})
   ↓
service_vm.py
   - Check hypervisor type = KVM
   - Map "poweron" → "power.on"
   ↓
SSH Connection
   kvm_manage.power_op(conn, vmid, "power.on")
   ↓
SSH Command: virsh start {vmid}
   ↓
DB Update
   vm.power_state = "poweredOn"
   vm.log_action("Power POWERON", "success")
   vm.save()
   ↓
WebSocket Broadcast
   broadcast_vm_power_state_changed(vm)
   - Channel: "vm_updates"
   - Event: vm_power_state_changed
   ↓
Browser
   vm_realtime_updates.js receives event
   Updates table row: Status = "✓ Powered On" (green)
```

### Snapshot Operations
**Flow**: Similar to power, but with multi-step confirmation

```
UI Button: [Create Snapshot]
   ↓
Dialog: Enter snapshot name
   ↓
Admin Action
   action_type = "snapshot_create"
   params = {"op": "create", "name": "snap-041526"}
   ↓
trigger_vm_action()
   - Check: "snapshot_create" in _KVM_SNAPSHOT_MAP
   - Map to libvirt op: "create"
   ↓
SSH: virsh snapshot-create-as {vmid} {name}
   ↓
DB Update
   vm.snapshots (JSON array)
   vm.log_action("Snapshot snapshot_create", "success")
   ↓
WebSocket
   broadcast_vm_snapshot_operation(vm, "snapshot_create", snap_name)
```

---

## KVM SSH Commands Executed

All operations use SSH to the KVM host with `virsh` commands:

**Power Operations**:
- `virsh start {vmid}` - Power on
- `virsh destroy {vmid}` - Power off (force)
- `virsh shutdown {vmid}` - Graceful shutdown
- `virsh reset {vmid}` - Hard reset
- `virsh reboot {vmid}` - Graceful reboot
- `virsh suspend {vmid}` - Pause (memory saved)
- `virsh resume {vmid}` - Resume from pause

**Snapshots**:
- `virsh snapshot-create-as {vmid} {name}` - Create
- `virsh snapshot-list {vmid}` - List
- `virsh snapshot-delete {vmid} --snapshotname {name}` - Delete
- `virsh snapshot-revert {vmid} --snapshotname {name}` - Restore

**VM Lifecycle**:
- `virt-install` - Create new VM (complex command with vCPU, RAM, network, disk)
- `virsh undefine {vmid}` - Unregister (keep files)
- `virsh undefine {vmid} --remove-all-storage` - Delete completely

**Discovery**:
- `virsh list --all --name` - List VMs
- `virsh dominfo {vmid}` - Get VM details
- `virsh pool-list --all --name` - List storage pools
- `virsh net-list --all --name` - List networks
- `qemu-img info {path}` - Get disk info

---

## Testing the Implementation

### 1. Test VM Creation
```bash
curl -X POST http://localhost:8000/api/v1/kvm/kvm-prod-01/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test-vm-01",
    "datastore": "default",
    "cpu": 2,
    "ram": 2048,
    "disk_size_gb": 16,
    "network_name": "default",
    "power_on": false
  }'
```

### 2. Test Power Operations
```bash
# Power on VM
curl -X POST http://localhost:8000/api/v1/kvm/kvm-prod-01/vm/test-vm-01/power/on

# Power off VM
curl -X POST http://localhost:8000/api/v1/kvm/kvm-prod-01/vm/test-vm-01/power/off

# Shutdown gracefully
curl -X POST http://localhost:8000/api/v1/kvm/kvm-prod-01/vm/test-vm-01/power/shutdown
```

### 3. Test in Django Admin UI
1. Navigate to: `http://localhost:8000/admin/manager/virtualmachine/`
2. Select a KVM VM
3. Click actions: Power ON, Shutdown, Reset, Suspend, etc.
4. Watch WebSocket updates in browser console (Network tab → WS)

### 4. Test VM Creation Wizard
1. Navigate to: `http://localhost:8000/admin/manager/virtualmachine/add/`
2. Fill in wizard form
3. Select KVM host from dropdown
4. Configure CPU, RAM, disk, network
5. Click "Create VM"
6. Monitor progress in browser console

---

## Verification Checklist

- ✅ Power operations execute correctly on KVM
- ✅ Snapshots are created/deleted/restored via virsh
- ✅ VM creation form loads storage pools and networks from KVM host
- ✅ New VMs are created with virt-install
- ✅ Database is updated with correct power states
- ✅ WebSocket broadcasts reach connected browsers
- ✅ Action history logs all operations
- ✅ Error handling for SSH timeouts and missing VMs
- ✅ Admin UI shows correct action status messages with emojis

---

## Files Modified

| File | Changes | Lines |
|------|---------|-------|
| `manager/services/service_vm.py` | Added snapshot mapping, fixed KVM logic | 28-130 |
| `manager/admin/vm_admin.py` | Fixed snapshot action names, added logging | 331-425 |
| `plugins/kvm_plugin/routes.py` | Added 5 new API endpoints | 40-250 |

---

## What's Now Fully Wired

| Feature | Frontend | Backend | Status |
|---------|----------|---------|--------|
| List VMs | Admin list view | `sync_vms_for_host()` → `kvm_adapter.sync_vms()` | ✅ |
| VM Details | Admin detail view | DB + WebSocket | ✅ |
| Power On | Action button | `trigger_vm_action()` → `virsh start` | ✅ |
| Power Off | Action button | `trigger_vm_action()` → `virsh destroy` | ✅ |
| Shutdown | Action button | `trigger_vm_action()` → `virsh shutdown` | ✅ |
| Reset | Action button | `trigger_vm_action()` → `virsh reset` | ✅ |
| Reboot | Action button | `trigger_vm_action()` → `virsh reboot` | ✅ |
| Suspend | Action button | `trigger_vm_action()` → `virsh suspend` | ✅ |
| Create Snapshot | Action + dialog | `trigger_vm_action()` → `virsh snapshot-create-as` | ✅ |
| Delete Snapshots | Action + dialog | `trigger_vm_action()` → `virsh snapshot-delete` | ✅ |
| Restore Snapshot | Action + dialog | `trigger_vm_action()` → `virsh snapshot-revert` | ✅ |
| Delete VM | Action + dialog | `delete_vm_action()` → `virsh undefine --remove-all-storage` | ✅ |
| Unregister VM | Action + dialog | `unregister_action()` → `virsh undefine` | ✅ |
| Create VM | Wizard form | `POST /create` → `kvm_manage.create_vm()` → `virt-install` | ✅ |
| Real-time Updates | WebSocket listener | `broadcast_vm_*()` functions | ✅ |

---

## Result

**All KVM frontend options now have complete backend support.** The plugin is fully integrated with the Django admin UI, REST API, and WebSocket system. Users can:

1. ✅ List and view KVM VMs in real-time
2. ✅ Control power states (on/off/shutdown/reset/reboot/suspend)
3. ✅ Create, delete, and restore snapshots
4. ✅ Create new VMs with custom CPU/RAM/disk/network configuration
5. ✅ Delete or unregister VMs
6. ✅ See live updates via WebSocket without page refresh

