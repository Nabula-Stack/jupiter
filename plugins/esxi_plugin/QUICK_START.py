"""
QUICK START: Integrate ESXi vSphere API routes into Nebula.

This shows how to mount the new vSphere API routes into your existing
Django Ninja API. Add this to your nebula_api.py or route registration code.
"""

# ============================================================================
# Option 1: Add routes to existing plugin registration
# ============================================================================

# In nebula_api.py or your API initialization:
from plugins.esxi_plugin import register
from plugins.esxi_plugin.esxi_api_routes import ESXiAPIRouter

def setup_api():
    # ... existing setup ...
    
    # Register existing ESXi routes (SSH-based)
    from plugins.esxi_plugin import register
    register(api)
    
    # Mount new vSphere API routes ALONGSIDE existing routes
    # These will be at /api/v1/hosts/*-api, /api/v1/vms/*-api, etc.
    
    api.add_router(
        "/hosts",
        ESXiAPIRouter.build_host_routes(),
        tags=["Host Management (vSphere API)"]
    )
    
    api.add_router(
        "/vms",
        ESXiAPIRouter.build_vm_routes(),
        tags=["VM Management (vSphere API)"]
    )
    
    api.add_router(
        "/network",
        ESXiAPIRouter.build_network_routes(),
        tags=["Network Management (vSphere API)"]
    )
    
    api.add_router(
        "/storage",
        ESXiAPIRouter.build_storage_routes(),
        tags=["Storage Management (vSphere API)"]
    )


# ============================================================================
# Option 2: Use vSphere API client standalone (e.g., in Celery tasks)
# ============================================================================

from plugins.esxi_plugin.esxi_api import EsxiApiClient, to_dict
from manager.models import Host

def sync_host_data_async(host_id):
    """Background task to sync host data via vSphere API."""
    host = Host.objects.get(id=host_id)
    
    try:
        client = EsxiApiClient(
            host=str(host.ip_address),
            username=host.username,
            password=host.password,
        )
        
        with client.connect() as api:
            # Get fresh data from vSphere
            summary = api.get_host_summary()
            hardware = api.get_host_hardware()
            
            # Update Host model
            host.os_version = summary.version
            host.model_name = summary.model
            host.cpu_count = summary.cpu_count
            host.memory_gb = summary.memory_gb
            host.vendor = summary.vendor
            host.processor_type = summary.processor_type
            host.last_sync = summary.last_sync
            host.save()
            
            # Log success
            print(f"✓ Synced {host.name} via vSphere API")
            
    except ConnectionError as e:
        print(f"✗ Failed to connect to {host.name}: {e}")
    except Exception as e:
        print(f"✗ Error syncing {host.name}: {e}")


# ============================================================================
# Option 3: Add a single route to existing router
# ============================================================================

# In plugins/esxi_plugin/host_routes.py or elsewhere:
from ninja import Router
from .esxi_api import EsxiApiClient, HostSummarySchema, to_dict
from manager.utils import find_host_obj

# Extend your existing router
router = Router(tags=["Host Management"])

@router.get("/{host_name}/summary-live", response=dict)
def get_host_summary_live(request, host_name: str):
    """
    Get LIVE host summary directly from ESXi via vSphere API.
    
    Compare with:
    - GET /hosts/{host_name}/summary  ← cached (DB snapshot)
    - GET /hosts/{host_name}/summary-live  ← real-time (vSphere API)
    """
    host_obj = find_host_obj(host_name, require_active=True)
    if not host_obj:
        return {"error": f"Host not found: {host_name}"}
    
    try:
        client = EsxiApiClient(
            host=str(host_obj.ip_address),
            username=host_obj.username,
            password=host_obj.password,
        )
        
        with client.connect() as api:
            summary = api.get_host_summary()
            return to_dict(summary)
            
    except ConnectionError as e:
        return {"error": f"Connection failed: {e}", "status": 503}
    except Exception as e:
        return {"error": str(e), "status": 500}


# ============================================================================
# API Endpoints Created
# ============================================================================

"""
After integrating, you'll have these new endpoints:

HOST MANAGEMENT (vSphere API):
  GET  /api/v1/hosts/{host_name}/summary-api
       → Get real-time host summary (name, version, model, CPU, RAM)
  GET  /api/v1/hosts/{host_name}/hardware-api
       → Get detailed hardware (CPU MHz, cores, available memory, NICs, HBAs)
  GET  /api/v1/hosts/{host_name}/power-api
       → Get power state and uptime
  POST /api/v1/hosts/{host_name}/maintenance-api
       → Enable/disable maintenance mode

VM MANAGEMENT (vSphere API):
  GET  /api/v1/vms/{host_name}/list-api
       → List all VMs with power state, CPU, RAM, tools status, IP addresses
  GET  /api/v1/vms/{host_name}/{vm_name}-api
       → Get specific VM details
  POST /api/v1/vms/{host_name}/{vm_name}/power-api
       → Power VM on/off

NETWORK MANAGEMENT (vSphere API):
  GET  /api/v1/network/{host_name}/switches-api
       → List virtual switches (vSwitches)
  GET  /api/v1/network/{host_name}/portgroups-api
       → List port groups (virtual networks) with VLAN info

STORAGE MANAGEMENT (vSphere API):
  GET  /api/v1/storage/{host_name}/datastores-api
       → List datastores with capacity, free space, type
"""

# ============================================================================
# UI Integration (JavaScript/Fetch)
# ============================================================================

"""
Example: Add a "Sync via API" button to the host detail page:

<button onclick="syncHostViaAPI('my-esxi-host')">
  🔄 Sync via vSphere API
</button>

<script>
async function syncHostViaAPI(hostname) {
    try {
        const response = await fetch(`/api/v1/hosts/${hostname}/summary-api`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        
        const data = await response.json();
        document.getElementById("host-version").textContent = data.version;
        document.getElementById("host-memory").textContent = `${data.memory_gb} GB`;
        document.getElementById("host-cpus").textContent = data.cpu_count;
        document.getElementById("last-sync").textContent = data.last_sync;
        
        alert("✓ Host data synced successfully!");
    } catch (error) {
        alert(`✗ Sync failed: ${error.message}`);
    }
}
</script>
"""
