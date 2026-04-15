"""
Enhanced WebSocket Service for Real-Time Updates
Provides comprehensive broadcasting for all VM, Host, Network, and Storage operations.
"""

import asyncio
import json
from datetime import datetime
from channels.layers import get_channel_layer
from enum import Enum


class EventType(Enum):
    """Enum for all supported WebSocket event types"""
    # VM Events
    VM_STATUS_UPDATE = 'vm_status_update'
    VM_POWER_STATE_CHANGED = 'vm_power_state_changed'
    VM_CREATED = 'vm_created'
    VM_MODIFIED = 'vm_modified'
    VM_DELETED = 'vm_deleted'
    VM_SNAPSHOT_CREATED = 'vm_snapshot_created'
    VM_SNAPSHOT_RESTORED = 'vm_snapshot_restored'
    VM_SNAPSHOT_DELETED = 'vm_snapshot_deleted'
    VM_OPERATION_STARTED = 'vm_operation_started'
    VM_OPERATION_COMPLETED = 'vm_operation_completed'
    VM_OPERATION_FAILED = 'vm_operation_failed'
    
    # Host Events
    HOST_STATUS_UPDATE = 'host_status_update'
    HOST_LICENSE_UPDATED = 'host_license_updated'
    HOST_REBOOT_INITIATED = 'host_reboot_initiated'
    HOST_SHUTDOWN_INITIATED = 'host_shutdown_initiated'
    
    # Network Events
    NETWORK_INVENTORY_UPDATED = 'network_inventory_updated'
    NETWORK_PORTGROUP_CREATED = 'network_portgroup_created'
    NETWORK_PORTGROUP_DELETED = 'network_portgroup_deleted'
    NETWORK_VSWITCH_CREATED = 'network_vswitch_created'
    NETWORK_VSWITCH_DELETED = 'network_vswitch_deleted'
    
    # Storage Events
    STORAGE_INVENTORY_UPDATED = 'storage_inventory_updated'
    STORAGE_DATASTORE_CREATED = 'storage_datastore_created'
    STORAGE_RESCAN_INITIATED = 'storage_rescan_initiated'
    STORAGE_RESCAN_COMPLETED = 'storage_rescan_completed'
    STORAGE_DIRECTORY_CREATED = 'storage_directory_created'
    STORAGE_ITEM_DELETED = 'storage_item_deleted'


class WebSocketEventBroadcaster:
    """Main class for broadcasting WebSocket events"""
    
    @staticmethod
    async def broadcast(
        event_type: EventType,
        group: str,
        data: dict,
        additional_fields: dict = None
    ):
        """
        Broadcast an event to a WebSocket group
        
        Args:
            event_type: Type of event from EventType enum
            group: Channel group name (e.g., 'vm_updates', 'host_updates')
            data: Event-specific data
            additional_fields: Additional fields to include in broadcast
        """
        channel_layer = get_channel_layer()
        
        payload = {
            'type': event_type.value,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            **data
        }
        
        if additional_fields:
            payload.update(additional_fields)
        
        # Convert 'type' key to 'message_type' to avoid conflict with Django Channels
        message = {
            'type': group + '_message',  # Must be a valid handler name
            'event_type': event_type.value,
            **payload
        }
        
        try:
            await channel_layer.group_send(group, message)
            print(f"[WebSocket] Broadcast: {event_type.value} to {group}")
        except Exception as e:
            print(f"[WebSocket] Error broadcasting {event_type.value}: {e}")
    
    @staticmethod
    def broadcast_sync(
        event_type: EventType,
        group: str,
        data: dict,
        additional_fields: dict = None
    ):
        """
        Synchronous wrapper for broadcast - use from Django views
        
        Args:
            event_type: Type of event from EventType enum
            group: Channel group name
            data: Event-specific data
            additional_fields: Additional fields to include
        """
        try:
            asyncio.run(WebSocketEventBroadcaster.broadcast(event_type, group, data, additional_fields))
        except RuntimeError:
            # If event loop already running, create task
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(
                        WebSocketEventBroadcaster.broadcast(event_type, group, data, additional_fields)
                    )
            except Exception as e:
                print(f"[WebSocket] Error in sync broadcast: {e}")


# ============================================================================
# VM OPERATION BROADCASTERS
# ============================================================================

def broadcast_vm_power_state_changed(vm):
    """
    Broadcast when VM power state changes
    
    Args:
        vm (VirtualMachine): VM instance
    """
    data = {
        'vm_id': vm.id,
        'vm_name': vm.name,
        'vmid': vm.vmid,
        'power_state': vm.power_state,
        'host_name': vm.host.name if vm.host else 'Unknown',
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.VM_POWER_STATE_CHANGED,
        'vm_updates',
        data
    )


def broadcast_vm_created(vm):
    """Broadcast when new VM is created"""
    data = {
        'vm_id': vm.id,
        'vm_name': vm.name,
        'vmid': vm.vmid,
        'host_name': vm.host.name if vm.host else 'Unknown',
        'cpu_count': vm.num_cpu,
        'memory_mb': vm.memory_mb,
        'power_state': vm.power_state,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.VM_CREATED,
        'vm_updates',
        data
    )


def broadcast_vm_modified(vm, modification_type: str, old_value=None, new_value=None):
    """
    Broadcast when VM is modified
    
    Args:
        vm (VirtualMachine): VM instance
        modification_type: Type of modification (cpu, memory, disk, nic)
        old_value: Previous value
        new_value: New value
    """
    data = {
        'vm_id': vm.id,
        'vm_name': vm.name,
        'vmid': vm.vmid,
        'modification_type': modification_type,
        'old_value': str(old_value) if old_value else None,
        'new_value': str(new_value) if new_value else None,
        'host_name': vm.host.name if vm.host else 'Unknown',
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.VM_MODIFIED,
        'vm_updates',
        data
    )


def broadcast_vm_snapshot_operation(vm, operation: str, snapshot_name: str = None):
    """
    Broadcast snapshot operations (create, restore, delete)
    
    Args:
        vm (VirtualMachine): VM instance
        operation: create, restore, or delete
        snapshot_name: Name of snapshot
    """
    if operation == 'create':
        event_type = EventType.VM_SNAPSHOT_CREATED
    elif operation == 'restore':
        event_type = EventType.VM_SNAPSHOT_RESTORED
    elif operation == 'delete':
        event_type = EventType.VM_SNAPSHOT_DELETED
    else:
        return
    
    data = {
        'vm_id': vm.id,
        'vm_name': vm.name,
        'vmid': vm.vmid,
        'snapshot_name': snapshot_name,
        'operation': operation,
        'host_name': vm.host.name if vm.host else 'Unknown',
    }
    WebSocketEventBroadcaster.broadcast_sync(event_type, 'vm_updates', data)


def broadcast_vm_operation(
    vm,
    operation: str,
    status: str,
    error: str = None,
    details: dict = None
):
    """
    Broadcast generic VM operation status
    
    Args:
        vm (VirtualMachine): VM instance
        operation: Operation name (power_on, migrate, etc)
        status: started, completed, failed
        error: Error message if failed
        details: Additional operation details
    """
    if status == 'started':
        event_type = EventType.VM_OPERATION_STARTED
    elif status == 'completed':
        event_type = EventType.VM_OPERATION_COMPLETED
    elif status == 'failed':
        event_type = EventType.VM_OPERATION_FAILED
    else:
        return
    
    data = {
        'vm_id': vm.id,
        'vm_name': vm.name,
        'vmid': vm.vmid,
        'operation': operation,
        'status': status,
        'error': error,
        'host_name': vm.host.name if vm.host else 'Unknown',
    }
    
    WebSocketEventBroadcaster.broadcast_sync(event_type, 'vm_updates', data, details)


# ============================================================================
# HOST OPERATION BROADCASTERS
# ============================================================================

def broadcast_host_license_updated(host):
    """Broadcast when host license is updated"""
    data = {
        'host_id': host.id,
        'host_name': host.name,
        'license_name': host.license_name,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.HOST_LICENSE_UPDATED,
        'host_updates',
        data
    )


def broadcast_host_reboot_initiated(host):
    """Broadcast when host reboot is initiated"""
    data = {
        'host_id': host.id,
        'host_name': host.name,
        'ip_address': host.ip_address,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.HOST_REBOOT_INITIATED,
        'host_updates',
        data
    )


def broadcast_host_shutdown_initiated(host):
    """Broadcast when host shutdown is initiated"""
    data = {
        'host_id': host.id,
        'host_name': host.name,
        'ip_address': host.ip_address,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.HOST_SHUTDOWN_INITIATED,
        'host_updates',
        data
    )


# ============================================================================
# NETWORK OPERATION BROADCASTERS
# ============================================================================

def broadcast_network_portgroup_created(host_name: str, portgroup_name: str, vswitch: str):
    """Broadcast when port group is created"""
    data = {
        'host_name': host_name,
        'portgroup_name': portgroup_name,
        'vswitch_name': vswitch,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.NETWORK_PORTGROUP_CREATED,
        'network_updates',
        data
    )


def broadcast_network_portgroup_deleted(host_name: str, portgroup_name: str):
    """Broadcast when port group is deleted"""
    data = {
        'host_name': host_name,
        'portgroup_name': portgroup_name,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.NETWORK_PORTGROUP_DELETED,
        'network_updates',
        data
    )


def broadcast_network_vswitch_created(host_name: str, vswitch_name: str):
    """Broadcast when vSwitch is created"""
    data = {
        'host_name': host_name,
        'vswitch_name': vswitch_name,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.NETWORK_VSWITCH_CREATED,
        'network_updates',
        data
    )


def broadcast_network_vswitch_deleted(host_name: str, vswitch_name: str):
    """Broadcast when vSwitch is deleted"""
    data = {
        'host_name': host_name,
        'vswitch_name': vswitch_name,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.NETWORK_VSWITCH_DELETED,
        'network_updates',
        data
    )


# ============================================================================
# STORAGE OPERATION BROADCASTERS
# ============================================================================

def broadcast_storage_datastore_created(
    host_name: str,
    datastore_name: str,
    size_gb: float = None
):
    """Broadcast when datastore is created"""
    data = {
        'host_name': host_name,
        'datastore_name': datastore_name,
        'size_gb': size_gb,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.STORAGE_DATASTORE_CREATED,
        'storage_updates',
        data
    )


def broadcast_storage_rescan_initiated(host_name: str):
    """Broadcast when storage rescan is initiated"""
    data = {
        'host_name': host_name,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.STORAGE_RESCAN_INITIATED,
        'storage_updates',
        data
    )


def broadcast_storage_rescan_completed(host_name: str, new_devices: int = 0):
    """Broadcast when storage rescan completes"""
    data = {
        'host_name': host_name,
        'new_devices_found': new_devices,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.STORAGE_RESCAN_COMPLETED,
        'storage_updates',
        data
    )


def broadcast_storage_directory_created(host_name: str, path: str):
    """Broadcast when directory is created"""
    data = {
        'host_name': host_name,
        'path': path,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.STORAGE_DIRECTORY_CREATED,
        'storage_updates',
        data
    )


def broadcast_storage_item_deleted(host_name: str, path: str, item_type: str = 'file'):
    """
    Broadcast when file or folder is deleted
    
    Args:
        host_name: Name of host
        path: Path to deleted item
        item_type: 'file' or 'directory'
    """
    data = {
        'host_name': host_name,
        'path': path,
        'item_type': item_type,
    }
    WebSocketEventBroadcaster.broadcast_sync(
        EventType.STORAGE_ITEM_DELETED,
        'storage_updates',
        data
    )
