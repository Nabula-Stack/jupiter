"""
Service module for sending real-time VM updates via Django Channels.
Used by background tasks to broadcast changes to connected WebSocket clients.
"""

import asyncio
import json
from datetime import datetime
from channels.layers import get_channel_layer


def broadcast_vm_update(vm):
    """
    Send a VM status update to all connected WebSocket clients.
    
    This is a synchronous wrapper that can be called from Django views,
    management commands, or background tasks.
    
    Args:
        vm (VirtualMachine): The VirtualMachine model instance with updated data
        
    Example:
        from manager.models import VirtualMachine
        from manager.websocket_service import broadcast_vm_update
        
        vm = VirtualMachine.objects.get(id=1)
        vm.power_state = 'poweredOn'
        vm.save()
        broadcast_vm_update(vm)
    """
    try:
        # Run the async function in a new event loop
        asyncio.run(_async_broadcast_vm_update(vm))
    except RuntimeError:
        # If there's already an event loop running, use it
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(_async_broadcast_vm_update(vm))
        except Exception as e:
            print(f"[WebSocket] Error broadcasting update: {e}")


async def _async_broadcast_vm_update(vm):
    """
    Async helper function to send update to channel layer.
    
    Args:
        vm (VirtualMachine): The VirtualMachine model instance
    """
    channel_layer = get_channel_layer()
    
    # Prepare the update payload
    update_data = {
        'type': 'vm_status_update',  # This maps to the vm_status_update method in the consumer
        'vm_id': vm.id,
        'vm_name': vm.name,
        'vmid': vm.vmid,
        'power_state': vm.power_state,
        'overall_status': vm.overall_status,
        'cpu_usage_mhz': vm.cpu_usage_mhz,
        'memory_mb': vm.mem_active_mb,
        'tools_status': vm.tools_status,
        'storage_used_gb': vm.storage_used_gb,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    
    # Send to all clients in the 'vm_updates' group
    await channel_layer.group_send(
        'vm_updates',
        update_data
    )
    
    print(f"[WebSocket] Broadcast: {vm.name} - Power: {vm.power_state}, Status: {vm.overall_status}")


def broadcast_vm_batch(vms):
    """
    Send multiple VM updates to all connected WebSocket clients.
    More efficient than calling broadcast_vm_update multiple times.
    
    Args:
        vms (QuerySet or list): VirtualMachine instances
    """
    try:
        # Convert QuerySet to list to avoid async context issues
        vms_list = list(vms)
        asyncio.run(_async_broadcast_vm_batch(vms_list))
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Convert QuerySet to list before creating task
                vms_list = list(vms)
                asyncio.create_task(_async_broadcast_vm_batch(vms_list))
        except Exception as e:
            print(f"[WebSocket] Error broadcasting batch: {e}")


async def _async_broadcast_vm_batch(vms_list):
    """
    Async helper to send multiple updates.
    
    Args:
        vms_list (list): List of VirtualMachine instances
    """
    channel_layer = get_channel_layer()
    
    for vm in vms_list:
        update_data = {
            'type': 'vm_status_update',
            'vm_id': vm.id,
            'vm_name': vm.name,
            'vmid': vm.vmid,
            'power_state': vm.power_state,
            'overall_status': vm.overall_status,
            'cpu_usage_mhz': vm.cpu_usage_mhz,
            'memory_mb': vm.mem_active_mb,
            'tools_status': vm.tools_status,
            'storage_used_gb': vm.storage_used_gb,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        
        await channel_layer.group_send(
            'vm_updates',
            update_data
        )
    
    print(f"[WebSocket] Broadcast batch: {len(vms_list)} VMs updated")
