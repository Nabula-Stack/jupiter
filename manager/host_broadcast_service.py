"""
Service for broadcasting Host updates via WebSocket.
Similar to vm broadcast but for host system data.
"""

import asyncio
import json
from datetime import datetime
from channels.layers import get_channel_layer


def broadcast_host_update(host):
    """
    Send a Host status update to all connected WebSocket clients.
    
    Args:
        host (Host): The Host model instance with updated data
    """
    try:
        asyncio.run(_async_broadcast_host_update(host))
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(_async_broadcast_host_update(host))
        except Exception as e:
            print(f"[WebSocket] Error broadcasting host update: {e}")


async def _async_broadcast_host_update(host):
    """
    Async helper to send host update to channel layer.
    """
    channel_layer = get_channel_layer()
    
    # Extract service status data
    services = host.services_status or {}
    
    update_data = {
        'type': 'host_status_update',
        'host_id': host.id,
        'host_name': host.name,
        'ip_address': str(host.ip_address),
        'cpu_count': host.cpu_count,
        'memory_gb': host.memory_gb,
        'cpu_usage_percent': services.get('cpu_usage_percent', 0),
        'memory_usage_percent': services.get('memory_usage_percent', 0),
        'os_version': host.os_version,
        'model_name': host.model_name,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    
    # Send to 'vm_updates' group (same group for all updates)
    await channel_layer.group_send(
        'vm_updates',
        update_data
    )
    
    print(f"[WebSocket] Broadcast Host: {host.name} - CPU: {services.get('cpu_usage_percent', 0)}%")


def broadcast_host_batch(hosts):
    """
    Send multiple host updates.
    
    Args:
        hosts (QuerySet or list): Host instances
    """
    try:
        hosts_list = list(hosts)
        asyncio.run(_async_broadcast_host_batch(hosts_list))
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                hosts_list = list(hosts)
                asyncio.create_task(_async_broadcast_host_batch(hosts_list))
        except Exception as e:
            print(f"[WebSocket] Error broadcasting host batch: {e}")


async def _async_broadcast_host_batch(hosts_list):
    """
    Async helper to send multiple host updates.
    """
    channel_layer = get_channel_layer()
    
    for host in hosts_list:
        services = host.services_status or {}
        
        update_data = {
            'type': 'host_status_update',
            'host_id': host.id,
            'host_name': host.name,
            'ip_address': str(host.ip_address),
            'cpu_count': host.cpu_count,
            'memory_gb': host.memory_gb,
            'cpu_usage_percent': services.get('cpu_usage_percent', 0),
            'memory_usage_percent': services.get('memory_usage_percent', 0),
            'os_version': host.os_version,
            'model_name': host.model_name,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        
        await channel_layer.group_send(
            'vm_updates',
            update_data
        )
    
    print(f"[WebSocket] Broadcast host batch: {len(hosts_list)} hosts updated")
