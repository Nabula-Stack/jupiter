"""
ESXi Plugin — mounts all ESXi-specific API routers onto the Nebula API.

Usage (nebula_api.py):
    from plugins.esxi_plugin import register
    register(api)

Route prefixes registered:
    /hosts    — host info, power, maintenance, license
    /network  — vswitches, portgroups, NICs
    /storage  — datastores, file explorer, rescan
    /vms      — inventory, power, snapshots, provisioning, migration
"""

from ninja import NinjaAPI

from .host_routes import router as host_router
from .network_routes import router as network_router
from .storage_routes import router as storage_router
from .vm_routes import router as vm_router


def register(api: NinjaAPI) -> None:
    """Mount all ESXi routers onto the provided NinjaAPI instance."""
    api.add_router("/hosts", host_router, tags=["Host Management"])
    api.add_router("/network", network_router, tags=["Network Management"])
    api.add_router("/storage", storage_router, tags=["Storage Management"])
    api.add_router("/vms", vm_router, tags=["Virtual Machine Management"])
