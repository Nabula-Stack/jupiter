"""
ESXi API Router — Integration examples for using esxi_api.py in routes.

This file demonstrates how to integrate the new vSphere API client (esxi_api.py)
into the existing plugin router structure. Copy patterns from here into your
actual route handlers.

Usage:
    from .esxi_api_routes import ESXiAPIRouter
    router = ESXiAPIRouter.build_host_routes()
    api.add_router("/hosts/api", router, tags=["Host API"])
"""

from ninja import Router, Schema
from typing import Optional, List
import logging

from .esxi_api import (
    EsxiApiClient, HostSummary, VMInfo, NetworkSwitch, Portgroup, 
    Datastore, HostHardware, HostPower, to_dict
)
from manager.utils import get_conn, find_host_obj
from manager.models import Host

logger = logging.getLogger(__name__)


# ============================================================================
# Response Schemas (for API documentation)
# ============================================================================

class HostSummarySchema(Schema):
    """Host summary response."""
    name: str
    ip_address: str
    version: str
    build: str
    vendor: str
    model: str
    cpu_count: int
    memory_gb: float
    processor_type: str
    boot_time: Optional[str] = None
    last_sync: Optional[str] = None


class VMInfoSchema(Schema):
    """Virtual machine information."""
    name: str
    uuid: str
    power_state: str
    cpu_count: int
    memory_mb: int
    tools_running: bool
    tools_status: str
    datastorage: Optional[str] = None
    guest_os: Optional[str] = None
    ip_addresses: List[str] = []
    dns_name: Optional[str] = None


class NetworkSwitchSchema(Schema):
    """Virtual switch information."""
    name: str
    portgroup_count: int
    nic_count: int
    mtu: int


class DatastoreSchema(Schema):
    """Storage datastore information."""
    name: str
    capacity_gb: float
    free_gb: float
    provisioned_gb: float
    type: str


class PowerControlSchema(Schema):
    """Power control request."""
    power_on: bool


class MaintenanceModeSchema(Schema):
    """Maintenance mode control."""
    enable: bool


# ============================================================================
# Router: Host Operations via vSphere API
# ============================================================================

class ESXiAPIRouter:
    """Builder class for vSphere API routes."""

    @staticmethod
    def build_host_routes() -> Router:
        """
        Build host management routes using vSphere API.
        
        Routes (examples):
            GET  /hosts/{host_name}/summary-api     → get_host_summary_api
            GET  /hosts/{host_name}/hardware-api    → get_host_hardware_api
            GET  /hosts/{host_name}/power-api       → get_host_power_api
            POST /hosts/{host_name}/power-api       → set_host_power_api
            POST /hosts/{host_name}/maintenance-api → set_maintenance_mode_api
        """
        router = Router(tags=["Host Management (vSphere API)"])

        @router.get("/{host_name}/summary-api", response=HostSummarySchema)
        def get_host_summary_api(request, host_name: str):
            """
            Get host summary via vSphere API (real-time, not cached).
            
            Returns host info directly from ESXi.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    summary = api.get_host_summary()
                    return to_dict(summary)
                    
            except ConnectionError as e:
                logger.error(f"Failed to connect to {host_name}: {e}")
                return {"status": 503, "error": f"Connection failed: {str(e)}"}
            except Exception as e:
                logger.error(f"Error getting summary for {host_name}: {e}")
                return {"status": 500, "error": str(e)}

        @router.get("/{host_name}/hardware-api")
        def get_host_hardware_api(request, host_name: str):
            """
            Get detailed hardware configuration via vSphere API.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    hardware = api.get_host_hardware()
                    return to_dict(hardware)
                    
            except Exception as e:
                logger.error(f"Error getting hardware for {host_name}: {e}")
                return {"status": 500, "error": str(e)}

        @router.get("/{host_name}/power-api", response=dict)
        def get_host_power_api(request, host_name: str):
            """
            Get host power state and uptime via vSphere API.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    power = api.get_host_power()
                    return to_dict(power)
                    
            except Exception as e:
                logger.error(f"Error getting power state for {host_name}: {e}")
                return {"status": 500, "error": str(e)}

        @router.post("/{host_name}/maintenance-api", response=dict)
        def set_maintenance_mode_api(request, host_name: str, data: MaintenanceModeSchema):
            """
            Enable/disable maintenance mode via vSphere API.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    result = api.set_host_maintenance_mode(data.enable)
                    return result
                    
            except Exception as e:
                logger.error(f"Error setting maintenance mode for {host_name}: {e}")
                return {"status": 500, "error": str(e)}

        return router

    @staticmethod
    def build_vm_routes() -> Router:
        """
        Build VM management routes using vSphere API.
        
        Routes (examples):
            GET  /vms/{host_name}/list-api       → list_vms_api
            GET  /vms/{host_name}/{vm_name}-api  → get_vm_api
            POST /vms/{host_name}/{vm_name}/power-api → set_vm_power_api
        """
        router = Router(tags=["VM Management (vSphere API)"])

        @router.get("/{host_name}/list-api", response=List[VMInfoSchema])
        def list_vms_api(request, host_name: str):
            """
            List all VMs on a host via vSphere API.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    vms = api.list_vms()
                    return [to_dict(vm) for vm in vms]
                    
            except Exception as e:
                logger.error(f"Error listing VMs for {host_name}: {e}")
                return {"status": 500, "error": str(e)}

        @router.get("/{host_name}/{vm_name}-api", response=VMInfoSchema)
        def get_vm_api(request, host_name: str, vm_name: str):
            """
            Get specific VM details via vSphere API.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    vm = api.get_vm(vm_name)
                    if not vm:
                        return {"status": 404, "error": f"VM {vm_name} not found"}
                    return to_dict(vm)
                    
            except Exception as e:
                logger.error(f"Error getting VM {vm_name}: {e}")
                return {"status": 500, "error": str(e)}

        @router.post("/{host_name}/{vm_name}/power-api", response=dict)
        def set_vm_power_api(request, host_name: str, vm_name: str, data: PowerControlSchema):
            """
            Power a VM on or off via vSphere API.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    result = api.set_vm_power_state(vm_name, data.power_on)
                    return result
                    
            except Exception as e:
                logger.error(f"Error controlling VM {vm_name}: {e}")
                return {"status": 500, "error": str(e)}

        return router

    @staticmethod
    def build_network_routes() -> Router:
        """
        Build network management routes using vSphere API.
        
        Routes (examples):
            GET /network/{host_name}/switches-api   → list_vswitches_api
            GET /network/{host_name}/portgroups-api → list_portgroups_api
        """
        router = Router(tags=["Network Management (vSphere API)"])

        @router.get("/{host_name}/switches-api", response=List[NetworkSwitchSchema])
        def list_vswitches_api(request, host_name: str):
            """
            List all virtual switches via vSphere API.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    switches = api.list_vswitches()
                    return [to_dict(sw) for sw in switches]
                    
            except Exception as e:
                logger.error(f"Error listing vSwitches for {host_name}: {e}")
                return {"status": 500, "error": str(e)}

        @router.get("/{host_name}/portgroups-api")
        def list_portgroups_api(request, host_name: str):
            """
            List all port groups (virtual networks) via vSphere API.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    pgroups = api.list_portgroups()
                    return [to_dict(pg) for pg in pgroups]
                    
            except Exception as e:
                logger.error(f"Error listing portgroups for {host_name}: {e}")
                return {"status": 500, "error": str(e)}

        return router

    @staticmethod
    def build_storage_routes() -> Router:
        """
        Build storage management routes using vSphere API.
        
        Routes (examples):
            GET /storage/{host_name}/datastores-api → list_datastores_api
        """
        router = Router(tags=["Storage Management (vSphere API)"])

        @router.get("/{host_name}/datastores-api", response=List[DatastoreSchema])
        def list_datastores_api(request, host_name: str):
            """
            List all datastores via vSphere API.
            """
            try:
                host_obj = find_host_obj(host_name, require_active=True)
                if not host_obj:
                    return {"status": 404, "error": f"Host {host_name} not found"}

                client = EsxiApiClient(
                    host=str(host_obj.ip_address),
                    username=host_obj.username,
                    password=host_obj.password,
                )
                
                with client.connect() as api:
                    datastores = api.list_datastores()
                    return [to_dict(ds) for ds in datastores]
                    
            except Exception as e:
                logger.error(f"Error listing datastores for {host_name}: {e}")
                return {"status": 500, "error": str(e)}

        return router
