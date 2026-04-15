# KVM Plugin: Complete Backend-to-UI Mapping

## Executive Summary

The Nebula KVM plugin follows a **pull-based architecture** with live websocket updates:
- **Sync Cycle**: Background workers pull VM/host data from KVM via SSH every ~10 seconds
- **WebSocket Updates**: Real-time broadcasts to all connected clients whenever state changes
- **Direct SSH**: File operations and live commands use direct SSH (no caching)
- **Hypervisor-Agnostic UI**: Same Django admin interface works for KVM, ESXi, and Proxmox

---

## 1. API Routes & HTTP Actions

### 1.1 REST API Endpoints (Django Ninja)

**Base URL**: `/api/v1/`

#### Health & Diagnostics

```
GET /api/v1/kvm/{host_name}/health
├─ Response: { status, vm_count, storage_pools, networks }
├─ Backend: lib.kvm.manage.list_vms_with_stats()
├─ Sync Trigger: No (read-only from cache)
└─ WebSocket: None (direct read)
```

#### System Discovery

```
GET /api/v1/system/hypervisors
├─ Response: ["kvm_libvirt", "vmware_esxi", "proxmox_ve"]
├─ Backend: manager/hypervisors/registry.py::list_adapter_slugs()
├─ Sync Trigger: No
└─ WebSocket: None

GET /api/v1/system/hosts/hypervisors
├─ Response: [{"host_id": 1, "slug": "kvm_libvirt"}, ...]
├─ Backend: Query Host.hypervisor_type
├─ Sync Trigger: No
└─ WebSocket: None
```

### 1.2 Django Admin Routes (Form Submissions)

The admin interface uses Django's built-in action handlers + custom view methods.

#### Host Management

```
POST /admin/manager/host/add/
├─ Form: HostAdmin.form_fields
│  ├─ name: CharField
│  ├─ ip_address: CharField
│  ├─ hypervisor_type: ChoiceField (kvm_libvirt|vmware_esxi|proxmox_ve)
│  ├─ username: CharField (service account)
│  ├─ password: CharField (optional, empty for key-based)
│  ├─ ssh_public_key: TextArea (SSH RSA public key)
│  ├─ is_active: BooleanField (trigger sync)
│  └─ license_key: CharField (ESXi only)
├─ Handler: HostAdmin.save_model()
├─ Sync Trigger: If is_active=True, runs sync_hosts management command
└─ WebSocket: broadcast_host_update() → "host_updates" group

POST /admin/manager/host/{id}/change/
├─ Same form as above
├─ Handler: HostAdmin.save_model()
├─ Sync Trigger: If is_active changed True, starts sync
└─ WebSocket: broadcast_host_update()

GET /admin/manager/host/{id}/
├─ Display: name, ip_address, hypervisor_type, status, vms, last_sync
├─ Read-only: cpu_count, memory_gb, services_status (from last sync)
└─ Sync Trigger: None (display data from cache)
```

#### Virtual Machine Management

**Power Operations** (all POST to Django admin action handler):

```
POST /admin/manager/virtualmachine/{id}/power_on_action/
├─ Action Handler: VirtualMachineAdmin.power_on_action()
├─ Calls: trigger_vm_action(vm_obj, "poweron", {})
├─ Backend Flow:
│  ├─ Get connection: get_conn(host.name)
│  ├─ Execute: virsh start {vmid}
│  ├─ Update DB: vm.power_state = "poweredOn"
│  ├─ Log Action: vm.log_action("Power ON", "success", error="")
│  └─ Broadcast: broadcast_vm_update(vm)
├─ Response: Redirect to change_list with success message
└─ WebSocket Event:
   ├─ Type: "vm_power_state_changed"
   ├─ Data: { vm_id, name, power_state: "poweredOn", timestamp }
   └─ Listeners: vm_realtime_updates.js → update table row

POST /admin/manager/virtualmachine/{id}/shutdown_action/
├─ Action Handler: VirtualMachineAdmin.shutdown_action()
├─ Calls: trigger_vm_action(vm_obj, "shutdown", {})
├─ Backend Flow:
│  ├─ Execute: virsh shutdown {vmid}
│  ├─ Wait: Up to 30 seconds for graceful shutdown
│  ├─ Update DB: vm.power_state = "poweredOff" (once off)
│  └─ Broadcast: broadcast_vm_update(vm)
└─ WebSocket Event: vm_power_state_changed

POST /admin/manager/virtualmachine/{id}/power_off_action/
├─ Action Handler: VirtualMachineAdmin.power_off_action()
├─ Calls: trigger_vm_action(vm_obj, "poweroff", {})
├─ Backend Flow:
│  ├─ Execute: virsh destroy {vmid}  # Force kill
│  ├─ Update DB: vm.power_state = "poweredOff"
│  └─ Broadcast: broadcast_vm_update(vm)
└─ WebSocket Event: vm_power_state_changed

POST /admin/manager/virtualmachine/{id}/reset_action/
├─ Action Handler: VirtualMachineAdmin.reset_action()
├─ Calls: trigger_vm_action(vm_obj, "reset", {})
├─ Backend Flow:
│  ├─ Execute: virsh reset {vmid}
│  ├─ Update DB: vm.power_state = "poweredOn"
│  └─ Broadcast: broadcast_vm_update(vm)
└─ WebSocket Event: vm_power_state_changed

POST /admin/manager/virtualmachine/{id}/reboot_action/
├─ Action Handler: VirtualMachineAdmin.reboot_action()
├─ Calls: trigger_vm_action(vm_obj, "reboot", {})
├─ Backend Flow:
│  ├─ Execute: virsh reboot {vmid}
│  ├─ Update DB: vm.power_state = "poweredOn"
│  └─ Broadcast: broadcast_vm_update(vm)
└─ WebSocket Event: vm_power_state_changed

POST /admin/manager/virtualmachine/{id}/suspend_action/
├─ Action Handler: VirtualMachineAdmin.suspend_action()
├─ Calls: trigger_vm_action(vm_obj, "suspend", {})
├─ Backend Flow:
│  ├─ Execute: virsh suspend {vmid}
│  ├─ Update DB: vm.power_state = "suspended"
│  └─ Broadcast: broadcast_vm_update(vm)
└─ WebSocket Event: vm_power_state_changed
```

**Snapshot Operations**:

```
POST /admin/manager/virtualmachine/{id}/snapshot_create_action/
├─ Action Handler: VirtualMachineAdmin.snapshot_create_action()
├─ Form Dialog: Asks for snapshot_name
├─ Calls: trigger_vm_action(vm_obj, "snapshot.create", {"name": "snapshot_name"})
├─ Backend Flow:
│  ├─ Execute: virsh snapshot-create-as {vmid} {name}
│  ├─ Capture: Snapshot UUID from output
│  ├─ Update DB: vm.snapshots JSON array
│  └─ Broadcast: broadcast_vm_update(vm)
└─ WebSocket Event:
   ├─ Type: "vm_snapshot_operation"
   ├─ Data: { vm_id, operation: "create", name, status: "success" }
   └─ Listeners: Refresh snapshot list in details view

POST /admin/manager/virtualmachine/{id}/snapshot_delete_action/
├─ Action Handler: VirtualMachineAdmin.snapshot_delete_action()
├─ Form Dialog: Select snapshot to delete
├─ Calls: trigger_vm_action(vm_obj, "snapshot.delete", {"snapshot_id": "..."})
├─ Backend Flow:
│  ├─ Execute: virsh snapshot-delete {vmid} {snapshot_id}
│  ├─ Update DB: Remove from vm.snapshots array
│  └─ Broadcast: broadcast_vm_update(vm)
└─ WebSocket Event: vm_snapshot_operation (delete)

POST /admin/manager/virtualmachine/{id}/snapshot_restore_action/
├─ Action Handler: VirtualMachineAdmin.snapshot_restore_action()
├─ Form Dialog: Select snapshot to restore
├─ Calls: trigger_vm_action(vm_obj, "snapshot.restore", {"snapshot_id": "..."})
├─ Backend Flow:
│  ├─ Note: VM will revert to snapshot timestamp
│  ├─ Execute: virsh snapshot-revert {vmid} {snapshot_id}
│  ├─ Update DB: vm.power_state may change
│  └─ Broadcast: broadcast_vm_update(vm)
└─ WebSocket Event: vm_snapshot_operation (restore)
```

**VM Lifecycle**:

```
POST /admin/manager/virtualmachine/add/
├─ Template: vm_create_wizard.html (replaces Django form)
├─ Form Fields:
│  ├─ name: CharField
│  ├─ host: ForeignKey (select target KVM host)
│  ├─ num_cpu: IntegerField
│  ├─ memory_mb: IntegerField
│  ├─ storage_gb: DecimalField
│  ├─ disk_format: ChoiceField (qcow2|raw)
│  ├─ network: ForeignKey (to host's networks)
│  ├─ guest_os: CharField
│  └─ iso_path: CharField (optional, for OS install)
├─ Handler: VirtualMachineAdmin.save_model()
├─ Backend Flow:
│  ├─ Get connection: get_conn(host.name)
│  ├─ Execute: virt-install --name {name} --cpu {num_cpu} --memory {memory_mb} ...
│  ├─ Execute: qemu-img create -f {disk_format} {vm_disk_path} {storage_gb}G
│  ├─ Create DB record: VirtualMachine(vmid, name, uuid, ...)
│  └─ Broadcast: broadcast_vm_update(vm)
├─ Response: Redirect to change_list + success message
└─ WebSocket Event:
   ├─ Type: "vm_status_update"
   ├─ Data: { vm_id, name, power_state: "poweredOff", status: "creating" }
   └─ Duration: ~30-60 seconds depending on disk size

POST /admin/manager/virtualmachine/{id}/delete/
├─ Action Handler: VirtualMachineAdmin.delete_action()
├─ Confirmation Dialog: "Permanently delete VM and all disks?"
├─ Calls: trigger_vm_action(vm_obj, "delete", {})
├─ Backend Flow:
│  ├─ Get connection: get_conn(host.name)
│  ├─ Execute: virsh undefine --remove-all-storage {vmid}
│  ├─ Delete DB record: VirtualMachine
│  ├─ Log Action: vm.log_action("Delete", "success")
│  └─ Broadcast: broadcast_vm_update(vm, deleted=True)
├─ Response: Redirect to change_list + success message
└─ WebSocket Event:
   ├─ Type: "vm_status_update"
   ├─ Data: { vm_id, status: "deleted" }
   └─ Listeners: Remove row from table

POST /admin/manager/virtualmachine/{id}/unregister_action/
├─ Action Handler: VirtualMachineAdmin.unregister_action()
├─ Confirmation Dialog: "Unregister VM (keep disks)?"
├─ Backend Flow:
│  ├─ Execute: virsh undefine {vmid}  # No --remove-all-storage
│  ├─ Delete DB record: VirtualMachine
│  └─ Broadcast: broadcast_vm_update(vm, unregistered=True)
└─ WebSocket Event: vm_status_update (unregistered)
```

---

## 2. WebSocket Implementation

### 2.1 WebSocket Connection

**Endpoint**: `ws://localhost:8000/ws/vms/updates/` (or wss:// for HTTPS)

**Location**: `manager/consumers.py::VMUpdatesConsumer`

### 2.2 WebSocket Lifecycle

```
1. Browser JavaScript Connects
   ├─ Code: manager/static/js/vm_realtime_updates.js
   └─ Connects to: ws://localhost:8000/ws/vms/updates/

2. Consumer.connect() Handler
   ├─ Increments: Redis counter "active_sync_users"
   ├─ If first client (count == 1):
   │  └─ Starts: _on_demand_sync_loop()
   └─ Sends: { type: "connection_established", user_count: 1 }

3. Sync Loop Runs (Every 10 seconds)
   ├─ Checks: Redis counter > 0
   ├─ If no clients: Exits loop (saves CPU/network)
   ├─ Runs: run_one_sync_cycle()
   │  ├─ Parallel thread pool: Fetch all KVM hosts
   │  ├─ For each host:
   │  │  ├─ SSH connection: get_conn(host.name)
   │  │  ├─ Fetch: virsh list, dominfo, domblklist, etc.
   │  │  ├─ Compare: Hash new state vs. cached state
   │  │  ├─ Update DB: Only changed VMs
   │  │  └─ Broadcast: Changed VMs only (not all VMs)
   │  └─ Return: Sync results
   ├─ Broadcast: broadcast_vm_batch(changed_vms)
   │  └─ Sends to Redis: {"type": "vm_batch_update", "vms": [...]}
   └─ Schedule: Next cycle in 10 seconds

4. Direct Action Triggers Broadcast
   ├─ User clicks: Power ON button
   ├─ Admin action: trigger_vm_action()
   ├─ SSH executes: virsh start {vmid}
   ├─ DB updates: vm.power_state = "poweredOn"
   ├─ Broadcast: broadcast_vm_update(vm) → Immediate
   └─ Result: UI updates ~1 second after click

5. Consumer.disconnect() Handler
   ├─ Decrements: Redis counter "active_sync_users"
   ├─ If count == 0:
   │  └─ Sync loop exits automatically
   └─ Cleans up: Removes consumer from channel groups
```

### 2.3 WebSocket Message Types

**Incoming** (to consumer):

```
{
  "type": "vm_status_update",
  "vm_id": 123,
  "name": "web-server-01",
  "power_state": "poweredOn",
  "memory_active_mb": 1024,
  "cpu_usage_mhz": 2500,
  "uptime_human": "2 days, 3:45:00",
  "timestamp": "2026-04-15T14:30:00Z"
}
```

```
{
  "type": "vm_batch_update",
  "vms": [
    { "id": 123, "power_state": "poweredOn", "cpu_usage_mhz": 2500 },
    { "id": 124, "power_state": "poweredOff", ... }
  ],
  "timestamp": "2026-04-15T14:30:00Z"
}
```

```
{
  "type": "vm_power_state_changed",
  "vm_id": 123,
  "action": "poweron",
  "power_state": "poweredOn",
  "timestamp": "2026-04-15T14:30:00Z"
}
```

```
{
  "type": "vm_snapshot_operation",
  "vm_id": 123,
  "operation": "create|delete|restore",
  "snapshot_name": "backup-2026-04-15",
  "snapshot_id": "uuid-...",
  "status": "success|failed",
  "error": "" | "Snapshot already exists"
}
```

```
{
  "type": "host_updates",
  "host_id": 1,
  "name": "kvm-prod-01",
  "cpu_usage_percent": 45.2,
  "memory_usage_percent": 62.5,
  "services_status": {
    "libvirtd": "active",
    "virtqemud": "inactive"
  },
  "vm_count": 12,
  "timestamp": "2026-04-15T14:30:00Z"
}
```

```
{
  "type": "connection_established",
  "user_count": 1,
  "sync_interval_seconds": 10
}
```

**Outgoing** (from browser to consumer):

```
{
  "action": "request_full_sync"
}
// Triggers: Immediate broadcast of all VMs (not waiting for next cycle)
```

### 2.4 JavaScript Handler

**File**: `manager/static/js/vm_realtime_updates.js`

```javascript
class VMRealtimeUpdates {
  constructor(containerId) {
    this.wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.wsUrl = `${this.wsProtocol}//localhost:8000/ws/vms/updates/`;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 5;
    this.reconnectDelay = 3000;
    this.connect();
  }

  connect() {
    try {
      this.socket = new WebSocket(this.wsUrl);
      
      this.socket.onopen = () => {
        console.log('Connected to VM updates');
        this.reconnectAttempts = 0;
      };

      this.socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        this.handleMessage(message);
      };

      this.socket.onerror = () => {
        console.error('WebSocket error');
        this.disconnect();
      };

      this.socket.onclose = () => {
        console.log('Disconnected from VM updates');
        this.attemptReconnect();
      };
    } catch (error) {
      console.error('Failed to create WebSocket:', error);
      this.attemptReconnect();
    }
  }

  handleMessage(message) {
    switch (message.type) {
      case 'vm_status_update':
      case 'vm_batch_update':
        this.updateVMRows(message);
        break;
      case 'vm_power_state_changed':
        this.updatePowerState(message);
        break;
      case 'vm_snapshot_operation':
        this.showSnapshotNotification(message);
        break;
      case 'host_updates':
        this.updateHostMetrics(message);
        break;
      case 'connection_established':
        console.log(`Connected. Active users: ${message.user_count}`);
        break;
    }
  }

  updateVMRows(message) {
    const vms = message.vms || [message];  // Handle both batch and single
    vms.forEach(vm => {
      const row = document.querySelector(`tr[data-vm-id="${vm.id}"]`);
      if (row) {
        // Update status badge
        const statusCell = row.querySelector('[data-field="power_state"]');
        if (statusCell) {
          statusCell.innerHTML = this.getBadgeHTML(vm.power_state);
        }
        // Update CPU/memory
        const cpuCell = row.querySelector('[data-field="cpu_usage"]');
        if (cpuCell) {
          cpuCell.textContent = `${vm.cpu_usage_mhz} MHz`;
        }
        const memCell = row.querySelector('[data-field="memory_active"]');
        if (memCell) {
          memCell.textContent = `${vm.memory_active_mb} MB`;
        }
      }
    });
  }

  updatePowerState(message) {
    const row = document.querySelector(`tr[data-vm-id="${message.vm_id}"]`);
    if (row) {
      const statusCell = row.querySelector('[data-field="power_state"]');
      const badge = message.power_state === 'poweredOn' 
        ? '<span class="badge bg-success">Powered On</span>'
        : '<span class="badge bg-danger">Powered Off</span>';
      statusCell.innerHTML = badge;
    }
  }

  updateHostMetrics(message) {
    const hostCard = document.querySelector(`[data-host-id="${message.host_id}"]`);
    if (hostCard) {
      hostCard.querySelector('[data-metric="cpu"]').textContent 
        = `${message.cpu_usage_percent.toFixed(1)}%`;
      hostCard.querySelector('[data-metric="memory"]').textContent 
        = `${message.memory_usage_percent.toFixed(1)}%`;
    }
  }

  getBadgeHTML(powerState) {
    const badges = {
      'poweredOn': '<span class="badge bg-success">✓ Powered On</span>',
      'poweredOff': '<span class="badge bg-danger">✕ Powered Off</span>',
      'suspended': '<span class="badge bg-warning">⏸ Suspended</span>'
    };
    return badges[powerState] || '<span class="badge bg-secondary">Unknown</span>';
  }

  attemptReconnect() {
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++;
      setTimeout(() => this.connect(), this.reconnectDelay);
    }
  }

  disconnect() {
    if (this.socket) {
      this.socket.close();
    }
  }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
  new VMRealtimeUpdates('vm-container');
});
```

---

## 3. Sync Cycle Architecture

### 3.1 Sync Cycle Timing

**Location**: `manager/services/sync_cycle.py::run_one_sync_cycle()`

```
Event: WebSocket client connects
  ↓
on_demand_sync_loop() starts
  ↓
Every 10 seconds (while clients connected):
  ├─ Fetch all Host records where is_active=True
  ├─ Create thread pool (max workers = CPU count)
  ├─ For each host, spawn: sync_host_worker(host)
  │  ├─ Get SSH connection: get_conn(host.name)
  │  ├─ Call: KvmLibvirtAdapter.sync_host(host, conn)
  │  │  ├─ Execute SSH commands:
  │  │  │  ├─ nproc → cpu_count
  │  │  │  ├─ /proc/meminfo → memory_gb
  │  │  │  ├─ uname -r → kernel_version
  │  │  │  ├─ hostnamectl → os_version
  │  │  │  ├─ lscpu → processor_type, vendor_id
  │  │  │  ├─ systemctl is-active libvirtd → services_status
  │  │  │  └─ virsh net-list → networks
  │  │  ├─ Update DB: Host record
  │  │  └─ Return: True/False
  │  ├─ Call: KvmLibvirtAdapter.sync_vms(host, conn)
  │  │  ├─ Execute: virsh list --all --name
  │  │  ├─ For each VM:
  │  │  │  ├─ virsh dominfo {vmid} → CPU, memory, UUID, power_state
  │  │  │  ├─ virsh domblklist {vmid} → Disk inventory
  │  │  │  ├─ virsh domifaddr {vmid} → IP address (DHCP)
  │  │  │  ├─ qemu-img info → Disk file size (provisioned GB)
  │  │  │  └─ virsh domstats {vmid} → Live CPU/memory usage
  │  │  ├─ Update/Create DB: VirtualMachine records
  │  │  ├─ Delete orphaned VMs
  │  │  └─ Return: VM count
  │  └─ Broadcast: broadcast_vm_batch(changed_vms)
  │     └─ Send to Redis channel layer → All connected clients
  ├─ Wait for all workers to finish
  ├─ Return: Sync results
  └─ Schedule next cycle
  
Last client disconnects
  ↓
Sync loop exits (saves CPU/network/SSH connections)
```

### 3.2 VM State Hashing

**Purpose**: Only broadcast VMs that actually changed

```python
# Before sync
vm_cache_hash = hashlib.md5(
    f"{vm.name}_{vm.power_state}_{vm.cpu_usage_mhz}_{vm.memory_active_mb}".encode()
).hexdigest()

# After sync
vm_new_hash = hashlib.md5(
    f"{vm.name}_{vm.power_state}_{vm.cpu_usage_mhz}_{vm.memory_active_mb}".encode()
).hexdigest()

# Only broadcast if changed
if vm_cache_hash != vm_new_hash:
    broadcast_vm_update(vm)
```

### 3.3 SSH Connection Pooling

**Location**: `lib.connect.connect.py::get_conn(host_name)`

```python
def get_conn(host_name: str) -> contextmanager(ESXiConnect):
    """
    Returns a context manager for SSH connection.
    
    Usage:
        with get_conn("kvm-prod-01") as conn:
            conn.execute_command("virsh list --all")
    
    Features:
    - Connection pooling via thread-local storage
    - SSH key-based auth (reads from Host.ssh_public_key)
    - Automatic retry (3 attempts)
    - Timeout: 30 seconds
    """
    # Lookup host from DB
    host = Host.objects.get(name=host_name, is_active=True)
    
    # Check if connection exists in thread-local pool
    if hasattr(get_conn._local, 'pool'):
        if host_name in get_conn._local.pool:
            return get_conn._local.pool[host_name]
    
    # Create new connection
    ssh_key_path = f"/ssh_keys/{host.ssh_public_key_filename}"
    conn = ESXiConnect(
        host=host.ip_address,
        user=host.username,
        key=ssh_key_path,
        port=22,
        timeout=30
    )
    conn.connect()
    
    # Cache in thread-local pool
    if not hasattr(get_conn._local, 'pool'):
        get_conn._local.pool = {}
    get_conn._local.pool[host_name] = conn
    
    return conn
```

---

## 4. Direct SSH Operations (File Browsing)

**Purpose**: File operations bypass the sync cycle and execute directly via SSH

**Locations**: 
- `lib.host.services.py` (file browsing)
- `lib.storage.explorer.py` (datastore exploration)

```
1. User clicks: "Browse VM Files" button
   ↓
2. Frontend sends: GET /api/v1/files/?host=kvm-prod-01&path=/var/lib/libvirt/
   ↓
3. Backend handler:
   ├─ Get connection: get_conn("kvm-prod-01")
   ├─ Execute: ls -la /var/lib/libvirt/ (direct SSH)
   ├─ Parse output: File list with permissions, size, date
   └─ Return: JSON { files: [...], directories: [...] }
   ↓
4. Frontend renders: File browser tree view
   ↓
5. User browses or downloads file (direct SFTP)
```

**SSH Commands Used** (No caching, always fresh):

```bash
# List files
ls -la /path/to/directory

# Get file size
stat /path/to/file

# Download file (via SFTP)
sftp -i ~/.ssh/nebula_rsa -b /tmp/batch.txt nebula@host

# Browse VM disk
qemu-img info /var/lib/libvirt/images/vm-disk.qcow2

# Check storage pools
virsh pool-info pool-name
virsh vol-list pool-name
```

---

## 5. UI Components & Their Backend Connections

### 5.1 Host Management Page

**Template**: `manager/templates/admin/host_admin.html` (Unfold base)

```
┌─────────────────────────────────────────┐
│  Host Management (Unfold admin list)    │
├─────────────────────────────────────────┤
│                                         │
│ Name          │ Type    │ Status │ ... │
├─────────────────────────────────────────┤
│ kvm-prod-01   │ KVM     │ 🟢    │     │
│ esxi-lab-01   │ ESXi    │ 🟢    │     │
│ prox-test-01  │ Proxmox │ 🔴    │     │
└─────────────────────────────────────────┘

Buttons:
├─ [Add Host] → POST /admin/manager/host/add/
├─ [Edit] {id} → POST /admin/manager/host/{id}/change/
├─ [Sync Now] → POST /admin/manager/host/{id}/action/sync_now/
├─ [Delete] {id} → POST /admin/manager/host/{id}/delete/
└─ [View VMs] {id} → GET /admin/host_vms/?host_id={id}
```

**Real-time Updates**: 
- CPU/Memory gauges update via WebSocket `host_updates` events
- Refresh rate: Every 10 seconds (or on manual action)

### 5.2 VM List Page

**Template**: `manager/templates/admin/vm_status_realtime.html`

```
┌────────────────────────────────────────────────────────────┐
│  Virtual Machines (Live Status Dashboard)                  │
├────────────────────────────────────────────────────────────┤
│                                                            │
│ Name           │ Host         │ Status │ CPU   │ RAM      │
├────────────────────────────────────────────────────────────┤
│ web-server-01  │ kvm-prod-01  │ ✓ On   │ 2500M │ 2048 MB  │
│ db-server-01   │ kvm-prod-01  │ ✗ Off  │ 0     │ 0        │
│ app-server-02  │ esxi-lab-01  │ ✓ On   │ 1800M │ 1024 MB  │
│ test-vm-01     │ kvm-prod-01  │ ⏸ Sus  │ 0     │ 0        │
└────────────────────────────────────────────────────────────┘
```

**Real-time Updates**:
- Status badges update via WebSocket `vm_power_state_changed` events
- CPU/memory update via WebSocket `vm_status_update` events
- Auto-refresh every 10 seconds
- No manual refresh needed

### 5.3 VM Details Page

**Template**: `manager/templates/admin/vm_detail.html`

```
Tabs:
├─ [Overview]
│  ├─ Name, UUID, Guest OS
│  ├─ Hardware: CPU cores, memory, disk space
│  ├─ Power State: [Power On] [Shutdown] [Power Off] [Reset] [Reboot] [Suspend]
│  └─ Uptime, Last sync
│
├─ [Snapshots]
│  ├─ List: [ name │ created │ size │ action ]
│  ├─ Buttons:
│  │  ├─ [Create Snapshot] → Form (name input)
│  │  ├─ [Delete] → Confirmation
│  │  └─ [Restore] → Confirmation + revert warning
│  └─ Real-time: Refresh on WebSocket `vm_snapshot_operation`
│
├─ [Networking]
│  ├─ Network interfaces (MAC, IP, network name)
│  ├─ DNS servers
│  └─ DNS name
│
├─ [Storage]
│  ├─ Disks: [ path │ format │ provisioned │ used │ action ]
│  ├─ [Browse] → File manager
│  └─ [Detach] → Remove disk
│
├─ [Metrics]
│  ├─ Real-time: CPU %, Memory %, Network I/O, Disk I/O
│  ├─ Chart: Last 1 hour (via time series)
│  └─ Update rate: Every 10 seconds (WebSocket)
│
└─ [Action History]
   ├─ Log: [ action │ timestamp │ status │ error ]
   └─ Examples:
      ├─ Power ON @ 2026-04-15 14:30:00 → success
      ├─ Snapshot "backup-1" @ 2026-04-15 14:25:00 → success
      └─ Shutdown @ 2026-04-15 14:20:00 → success
```

**Actions Available**:
- Power On/Off/Shutdown → POST /admin/manager/virtualmachine/{id}/power_on_action/
- Reset/Reboot/Suspend → Similar endpoints
- Create/Delete/Restore Snapshots → Similar endpoints
- Delete VM → POST /admin/manager/virtualmachine/{id}/delete/
- Edit → POST /admin/manager/virtualmachine/{id}/change/

### 5.4 VM Creation Wizard

**Template**: `manager/templates/admin/vm_create_wizard.html`

```
Step 1: Basic Settings
├─ VM Name (text input)
├─ Target Host (dropdown, select hypervisor)
└─ Guest OS (dropdown, e.g., "Ubuntu 22.04", "CentOS 8", etc.)

Step 2: Hardware
├─ CPU Cores (spinner, 1-64)
├─ Memory (MB) (spinner, 512-262144)
├─ Primary Disk Size (GB) (spinner, 10-5000)
└─ Disk Format (radio: qcow2 | raw)

Step 3: Networking
├─ Network (dropdown, from host.network_data)
├─ MAC Address (auto-generate or manual)
└─ IP Assignment (dhcp | static)

Step 4: Storage
├─ Storage Pool (dropdown, from host.storage_data)
├─ Datastore (for ESXi/Proxmox)
└─ Storage Location Path

Step 5: Review & Create
├─ Display all settings
├─ [Create] → POST /admin/manager/virtualmachine/add/
│  ├─ Calls: trigger_vm_action(None, "create", {...})
│  ├─ SSH: virt-install --name {name} --cpu {cores} ...
│  └─ Broadcasts: vm_status_update (status: "creating")
└─ [Cancel] → Back to list
```

---

## 6. Complete Action Flow Example: "Power On VM"

```
┌────────────────────────────────────────────────────────────────┐
│                         USER CLICKS BUTTON                     │
│               [Power On] on web-server-01                      │
└──────────────────────────┬─────────────────────────────────────┘
                           │
        ┌──────────────────▼──────────────────┐
        │   BROWSER SUBMITS FORM              │
        │  POST /admin/.../power_on_action/   │
        │  CSRF Token: [embedded in form]     │
        └──────────────────┬──────────────────┘
                           │
        ┌──────────────────▼──────────────────────────────┐
        │   DJANGO RECEIVES REQUEST                       │
        │   Admin action handler                          │
        │   VirtualMachineAdmin.power_on_action()         │
        └──────────────────┬──────────────────────────────┘
                           │
        ┌──────────────────▼──────────────────────────────────┐
        │   SERVICE LAYER (manager/services/service_vm.py)    │
        │   trigger_vm_action(vm_obj, "poweron", {})          │
        │   ├─ Power map: "poweron" → "power.on"              │
        │   ├─ VM host: "kvm-prod-01"                         │
        │   └─ Resolve action class: KvmPowerOn()             │
        └──────────────────┬──────────────────────────────────┘
                           │
        ┌──────────────────▼──────────────────────────────┐
        │   SSH EXECUTION (lib/connect/connect.py)        │
        │   get_conn("kvm-prod-01")                       │
        │   └─ Returns ESXiConnect context manager        │
        │      ├─ SSH key: /ssh_keys/nebula_rsa           │
        │      ├─ User: nebula                            │
        │      ├─ Host: 192.168.1.100                     │
        │      └─ Timeout: 30s                            │
        └──────────────────┬──────────────────────────────┘
                           │
        ┌──────────────────▼──────────────────────────────┐
        │   EXECUTE VIRSH COMMAND                         │
        │   conn.execute_command("virsh start vm-123")    │
        │   ├─ SSH sends: virsh start vm-123              │
        │   ├─ KVM executes: libvirt API call             │
        │   ├─ QEMU launches: VM boot sequence            │
        │   └─ Returns: "Domain vm-123 started"           │
        └──────────────────┬──────────────────────────────┘
                           │
        ┌──────────────────▼──────────────────────────────────┐
        │   DATABASE UPDATE (manager/models/virtual_machine.py)│
        │   vm.power_state = "poweredOn"                      │
        │   vm.save()                                         │
        │   ├─ Timestamp: Last power action                   │
        │   └─ Log action: vm.log_action("Power ON", "success")
        └──────────────────┬──────────────────────────────────┘
                           │
        ┌──────────────────▼────────────────────────────────┐
        │   WEBSOCKET BROADCAST                             │
        │   broadcast_vm_update(vm)                         │
        │   ├─ Serialize VM state: {id, name, power_state...}
        │   ├─ Channel: "vm_updates"                        │
        │   ├─ Type: "vm_power_state_changed"               │
        │   └─ Redis: Queues message to Redis channel layer │
        └──────────────────┬────────────────────────────────┘
                           │
        ┌──────────────────▼────────────────────────────────┐
        │   REDIS CHANNEL DISTRIBUTION                      │
        │   Channel: "vm_updates"                           │
        │   └─ Broadcasts to all connected consumers        │
        │      (VMUpdatesConsumer instances)                │
        └──────────────────┬────────────────────────────────┘
                           │
        ┌──────────────────▼────────────────────────────────┐
        │   CONSUMERS RECEIVE MESSAGE                       │
        │   Consumer.vm_power_state_changed(event)          │
        │   ├─ Deserializes: vm_id, power_state, timestamp  │
        │   └─ Forwards to browser: await send_json(event)  │
        └──────────────────┬────────────────────────────────┘
                           │
        ┌──────────────────▼────────────────────────────────┐
        │   BROWSER RECEIVES WEBSOCKET EVENT                │
        │   JavaScript listener: socket.onmessage           │
        │   ├─ Parses JSON: {vm_id: 123, power_state: ...}  │
        │   └─ Calls: handleMessage(message)                │
        └──────────────────┬────────────────────────────────┘
                           │
        ┌──────────────────▼────────────────────────────────┐
        │   FRONTEND UPDATES UI                             │
        │   updateVMRows(message)                           │
        │   ├─ Find: tr[data-vm-id="123"]                   │
        │   ├─ Update: Status badge → "✓ Powered On"        │
        │   ├─ Update: Row background → Green               │
        │   └─ Play sound: Success notification             │
        └──────────────────┬────────────────────────────────┘
                           │
        ┌──────────────────▼──────────────────┐
        │  USER SEES RESULT                    │
        │  Status updated in <1 second         │
        │  "web-server-01" shows: ✓ Powered On│
        │  Background: Green (healthy)         │
        └──────────────────────────────────────┘

Total Time: ~1 second from click to UI update
Parallel: SSH execution happens while DB and WebSocket run
Fault Tolerance: If broadcast fails, next sync cycle (10s) will refresh
```

---

## 7. Data Model Reference

### 7.1 Host Model

```python
class Host(models.Model):
    # Identity
    name = CharField(max_length=255)
    ip_address = CharField(max_length=255)
    hypervisor_type = CharField(
        choices=[
            ('kvm_libvirt', 'KVM/libvirt'),
            ('vmware_esxi', 'VMware ESXi'),
            ('proxmox_ve', 'Proxmox VE'),
        ]
    )
    
    # Authentication
    username = CharField(max_length=255)
    password = CharField(max_length=255, blank=True)  # Encrypted
    ssh_public_key = TextField()  # RSA public key
    license_key = CharField(max_length=255, blank=True)  # ESXi only
    
    # Hardware (Synced)
    cpu_count = IntegerField(null=True)
    memory_gb = IntegerField(null=True)
    processor_type = CharField(max_length=255, blank=True)
    os_version = CharField(max_length=255, blank=True)
    vendor = CharField(max_length=255, blank=True)  # Manufacturer
    model_name = CharField(max_length=255, blank=True)
    
    # Status (JSON)
    services_status = JSONField(default=dict)
    # Example:
    # {
    #   "cpu_usage_percent": 45.2,
    #   "memory_usage_percent": 62.5,
    #   "services": [
    #     {"name": "libvirtd", "status": "active"},
    #     {"name": "virtqemud", "status": "inactive"}
    #   ]
    # }
    
    # Network (JSON)
    network_data = JSONField(default=dict)
    # Example:
    # {
    #   "bridges": ["virbr0", "br0"],
    #   "networks": ["default", "management"],
    #   "interfaces": [...]
    # }
    
    # Storage (JSON)
    storage_data = JSONField(default=dict)
    # Example:
    # {
    #   "pools": [
    #     {
    #       "name": "default",
    #       "type": "dir",
    #       "path": "/var/lib/libvirt/images",
    #       "total_gb": 1024,
    #       "used_gb": 512,
    #       "free_gb": 512
    #     }
    #   ]
    # }
    
    # Control
    is_active = BooleanField(default=False)  # Trigger sync
    last_sync = DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.name
```

### 7.2 VirtualMachine Model

```python
class VirtualMachine(models.Model):
    # Identity
    vmid = CharField(max_length=255)  # virsh ID or UUID
    name = CharField(max_length=255)
    uuid = CharField(max_length=255, unique=True)
    host = ForeignKey(Host, on_delete=CASCADE)
    
    # State
    power_state = CharField(
        choices=[
            ('poweredOn', 'Powered On'),
            ('poweredOff', 'Powered Off'),
            ('suspended', 'Suspended'),
        ]
    )
    overall_status = CharField(
        choices=[
            ('green', 'Healthy'),
            ('yellow', 'Warning'),
            ('red', 'Error'),
        ],
        default='yellow'
    )
    
    # Hardware
    num_cpu = IntegerField()
    memory_mb = IntegerField()
    hw_version = CharField(max_length=255, blank=True)
    storage_used_gb = DecimalField(max_digits=10, decimal_places=2, default=0)
    storage_provisioned_gb = DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Live Statistics
    cpu_usage_mhz = IntegerField(default=0)
    mem_active_mb = IntegerField(default=0)
    uptime_human = CharField(max_length=255, blank=True)
    
    # Guest OS
    guest_os = CharField(max_length=255, blank=True)
    distro = CharField(max_length=255, blank=True)  # Detected OS
    kernel = CharField(max_length=255, blank=True)
    
    # Networking (JSON)
    networks = JSONField(default=list)
    # Example:
    # [
    #   {
    #     "network": "default",
    #     "mac": "00:0c:29:a2:b8:c0",
    #     "ip": ["192.168.1.100", "fe80::20c:29ff:fea2:b8c0"]
    #   }
    # ]
    dns_servers = JSONField(default=list)
    ip_address = CharField(max_length=255, blank=True)  # Primary
    dns_name = CharField(max_length=255, blank=True)
    
    # Snapshots (JSON)
    snapshots = JSONField(default=list)
    # Example:
    # [
    #   {
    #     "id": "1617906600",
    #     "name": "backup-2026-04-15",
    #     "created": "2026-04-15T14:30:00Z",
    #     "size_mb": 512,
    #     "description": "Before OS update"
    #   }
    # ]
    
    # Audit
    action_history = JSONField(default=list)
    # Example:
    # [
    #   {
    #     "action": "Power ON",
    #     "timestamp": "2026-04-15T14:30:00Z",
    #     "status": "success",
    #     "error": ""
    #   }
    # ]
    
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.name} ({self.host.name})"
    
    def log_action(self, action_name: str, status: str, error: str = ""):
        """Log an action to action_history"""
        entry = {
            "action": action_name,
            "timestamp": datetime.now().isoformat() + "Z",
            "status": status,
            "error": error
        }
        self.action_history.append(entry)
        self.save()
```

---

## 8. Summary: Request/Response Mapping Table

| Operation | HTTP Method | URL | Handler | SSH Command | WebSocket Event | Response |
|-----------|-------------|-----|---------|-------------|-----------------|----------|
| List Hosts | GET | /admin/manager/host/ | HostAdmin.list | None | None | Host list (Unfold table) |
| Add Host | POST | /admin/manager/host/add/ | HostAdmin.save_model() | SSH test (connect test) | host_updates | Redirect to list |
| Sync Host | POST | /admin/.../sync_now/ | HostAdmin.action_sync_host_now() | All sync commands | host_updates, vm_updates | Success message |
| List VMs | GET | /admin/manager/virtualmachine/ | VirtualMachineAdmin.list | None (from cache) | vm_status_update | VM list (Unfold table) |
| Power On | POST | /admin/.../power_on_action/ | power_on_action() | virsh start {vmid} | vm_power_state_changed | Redirect + message |
| Power Off | POST | /admin/.../power_off_action/ | power_off_action() | virsh destroy {vmid} | vm_power_state_changed | Redirect + message |
| Shutdown | POST | /admin/.../shutdown_action/ | shutdown_action() | virsh shutdown {vmid} | vm_power_state_changed | Redirect + message |
| Reset | POST | /admin/.../reset_action/ | reset_action() | virsh reset {vmid} | vm_power_state_changed | Redirect + message |
| Reboot | POST | /admin/.../reboot_action/ | reboot_action() | virsh reboot {vmid} | vm_power_state_changed | Redirect + message |
| Suspend | POST | /admin/.../suspend_action/ | suspend_action() | virsh suspend {vmid} | vm_power_state_changed | Redirect + message |
| Create Snapshot | POST | /admin/.../snapshot_create/ | snapshot_create_action() | virsh snapshot-create-as {vmid} {name} | vm_snapshot_operation | Redirect + message |
| Delete Snapshot | POST | /admin/.../snapshot_delete/ | snapshot_delete_action() | virsh snapshot-delete {vmid} {snap_id} | vm_snapshot_operation | Redirect + message |
| Restore Snapshot | POST | /admin/.../snapshot_restore/ | snapshot_restore_action() | virsh snapshot-revert {vmid} {snap_id} | vm_snapshot_operation | Redirect + message |
| Create VM | POST | /admin/manager/virtualmachine/add/ | VirtualMachineAdmin.save_model() | virt-install ... | vm_status_update | Redirect + message |
| Delete VM | POST | /admin/.../delete_action/ | delete_action() | virsh undefine --remove-all-storage | vm_status_update (deleted) | Redirect + message |
| Edit VM | POST | /admin/manager/virtualmachine/{id}/change/ | VirtualMachineAdmin.save_model() | (config update, VM must be off) | vm_status_update | Redirect + message |

---

## 9. Security & Best Practices

### 9.1 SSH Key Management

```
Location: /ssh_keys/ (in Docker volume)
├─ nebula_rsa (private key)
├─ nebula_rsa.pub (public key)
├─ rsa (private key, legacy)
└─ rsa.pub (public key, legacy)

Permissions:
├─ Private keys: 0600 (root only)
├─ Public keys: 0644 (readable)
└─ Directory: 0700 (root only)

Usage:
├─ Host.ssh_public_key: Stores full "ssh-rsa AAAA..." string
├─ get_conn(): Reads private key from /ssh_keys/nebula_rsa
└─ SSH command: ssh -i /ssh_keys/nebula_rsa nebula@{host}
```

### 9.2 Command Injection Prevention

```
BAD (vulnerable):
cmd = f"virsh start {vm_name}"  # If vm_name = "test; rm -rf /", disaster!

GOOD (safe):
cmd = ["virsh", "start", vm_name]  # Parameterized, no shell injection
subprocess.run(cmd, check=True)
```

### 9.3 WebSocket Authentication

```
Current: Django Channels + ASGI
├─ Uses Django session/auth
├─ CSRF token required for POST operations
├─ WebSocket inherits user permission from session
└─ Is admin check: connect() checks user.is_staff

Enhancement (recommended):
├─ Add token-based auth for API endpoints
├─ Validate WebSocket sender is authenticated admin
└─ Rate limit: Max 10 concurrent WebSocket connections per user
```

---

## 10. Troubleshooting Guide

### Connection Issues

```
Error: "SSH connection timed out"
Solution:
  ├─ Check: ping {host_ip}
  ├─ Check: ssh -i /ssh_keys/nebula_rsa nebula@{host} "virsh list"
  ├─ Check: KVM host firewall allows port 22
  └─ Check: nebula user exists on KVM host

Error: "Permission denied (publickey)"
Solution:
  ├─ Check: Public key copied to /home/nebula/.ssh/authorized_keys
  ├─ Check: Permissions: 700 on dir, 600 on file
  ├─ Check: user nebula exists: id nebula
  └─ Check: nebula in libvirt group: groups nebula
```

### Sync Issues

```
Error: "No update for 30+ minutes"
Solution:
  ├─ Check: Redis connection: docker exec redis redis-cli ping
  ├─ Check: At least one WebSocket client connected
  ├─ Check: Check container logs: docker logs sync-worker
  └─ Check: Manual sync: python manage.py sync_hosts

Error: "VM state out of sync"
Solution:
  ├─ Check: virsh list --all on KVM host shows correct state
  ├─ Check: Run manual sync: python manage.py sync_hosts
  └─ Check: Check action_history for errors
```

### WebSocket Issues

```
Error: "WebSocket connection failed"
Solution:
  ├─ Check: Browser console for error details
  ├─ Check: Daphne server running: docker logs web (look for "listening")
  ├─ Check: Redis running: docker exec redis redis-cli ping
  └─ Check: Check ws:// vs wss:// (must match page protocol)

Error: "Live updates not showing"
Solution:
  ├─ Check: Open browser DevTools > Network > WS tab
  ├─ Check: WebSocket shows "101 Switching Protocols"
  ├─ Check: At least 1 sync client connected
  └─ Check: Manual trigger: Press F5 to refresh
```

---

## 11. Quick Reference

**Admin URLs**:
- Host Management: `http://localhost:8000/admin/manager/host/`
- VM Management: `http://localhost:8000/admin/manager/virtualmachine/`
- Add Host: `http://localhost:8000/admin/manager/host/add/`
- Add VM: `http://localhost:8000/admin/manager/virtualmachine/add/`

**WebSocket Endpoints**:
- `ws://localhost:8000/ws/vms/updates/` (Daphne, HTTP upgrade)
- Automatic reconnect: 5 attempts, 3 second delay
- Auto-disconnect: On page unload or browser close

**Management Commands**:
- `python manage.py sync_hosts` - Manual sync (once)
- `python manage.py test_vm_broadcast` - Test WebSocket broadcast

**SSH Access** (for debugging):
```bash
# Connect directly to KVM host
ssh -i /ssh_keys/nebula_rsa nebula@{host}

# List all VMs
virsh list --all

# Get VM info
virsh dominfo {vmid}

# Monitor VM live stats
virsh domstats --raw {vmid}

# Check storage pools
virsh pool-list --all
virsh pool-info {pool_name}

# Check networks
virsh net-list --all
virsh net-info {network_name}
```

**Database Queries** (Django shell):
```python
# List all hosts
from manager.models import Host
hosts = Host.objects.all()

# Get specific VM
from manager.models import VirtualMachine
vm = VirtualMachine.objects.get(name="web-server-01")

# View action history
print(vm.action_history)

# Force update
vm.power_state = "poweredOn"
vm.log_action("Manual update", "success")
vm.save()

# Broadcast update
from manager.websocket_service import broadcast_vm_update
broadcast_vm_update(vm)
```

