from ninja import Router
from lib import network
from lib.network import manage as net_manage
from manager.utils import get_conn 
from django.views.decorators.cache import cache_page
from django.core.cache import cache
from ninja.decorators import decorate_view
from manager.utils import get_host_obj
from manager.websocket_broadcaster import (
    broadcast_network_portgroup_created,
    broadcast_network_portgroup_deleted,
    broadcast_network_vswitch_created,
    broadcast_network_vswitch_deleted,
)

router = Router(tags=["Network Management"])


def _get_host_obj(host_name: str):
    return get_host_obj(host_name, require_active=True)


def _looks_placeholder_only(items, keys):
    if not items:
        return True
    meaningful = False
    for item in items:
        for key in keys:
            val = str((item or {}).get(key, "")).strip()
            if val and val != "--":
                meaningful = True
                break
        if meaningful:
            break
    return not meaningful

# --- 1. DISCOVERY (GET - CACHED) ---

@router.get("/{host_name}/inventory", summary="Get Full Network Inventory")
@decorate_view(cache_page(600))
def get_network_inventory(request, host_name: str):
    host_obj = _get_host_obj(host_name)
    data = host_obj.network_data or {}
    return {
        "vswitches": data.get("vswitches", []),
        "portgroups": data.get("portgroups", []),
        "physical_nics": data.get("physical_nics", []),
    }

@router.get("/{host_name}/vswitches", summary="List All vSwitches")
@decorate_view(cache_page(3600))
def get_switches(request, host_name: str):
    host_obj = _get_host_obj(host_name)
    data = host_obj.network_data or {}
    return {"vswitches": data.get("vswitches", [])}

@router.get("/{host_name}/portgroups", summary="List All Port Groups")
@decorate_view(cache_page(600))
def get_groups(request, host_name: str):
    host_obj = _get_host_obj(host_name)
    data = host_obj.network_data or {}
    return {"portgroups": data.get("portgroups", [])}


# --- 2. MANAGEMENT (POST/DELETE - IMMEDIATE + BUSTING) ---

@router.post("/{host_name}/portgroups", summary="Create a New Port Group")
def create_portgroup(request, host_name: str, pg_name: str, vswitch: str, vlan: int = 0):
    try:
        with get_conn(host_name) as conn:
            network.add_portgroup(conn, vswitch, pg_name)
            if vlan > 0:
                network.set_portgroup_vlan(conn, pg_name, vlan)
            cache.delete_pattern(f"*{host_name}/inventory*")
            cache.delete_pattern(f"*{host_name}/portgroups*")
            broadcast_network_portgroup_created(host_name, pg_name, vswitch)
            return {"status": "success", "message": f"PortGroup '{pg_name}' created."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.delete("/{host_name}/portgroups/{pg_name}", summary="Delete a Port Group")
def delete_portgroup(request, host_name: str, pg_name: str):
    try:
        with get_conn(host_name) as conn:
            result = network.remove_portgroup(conn, pg_name)
            cache.delete_pattern(f"*{host_name}/inventory*")
            cache.delete_pattern(f"*{host_name}/portgroups*")
            broadcast_network_portgroup_deleted(host_name, pg_name)
            return {"status": "deleted", "portgroup": pg_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/{host_name}/vswitches", summary="Create a New vSwitch")
def create_vswitch(request, host_name: str, vswitch_name: str):
    try:
        with get_conn(host_name) as conn:
            network.create_vswitch(conn, vswitch_name)
            cache.delete_pattern(f"*{host_name}*")
            broadcast_network_vswitch_created(host_name, vswitch_name)
            return {"status": "created", "vswitch": vswitch_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.delete("/{host_name}/vswitches/{vswitch_name}", summary="Remove a vSwitch")
def remove_vswitch(request, host_name: str, vswitch_name: str):
    try:
        with get_conn(host_name) as conn:
            result = network.remove_vswitch(conn, vswitch_name)
            cache.delete_pattern(f"*{host_name}*")
            broadcast_network_vswitch_deleted(host_name, vswitch_name)
            return {"status": "removed", "vswitch": vswitch_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/{host_name}/advanced", summary="Get Advanced Network Info (VMkernel, TCP/IP, Firewall)")
def get_advanced_network(request, host_name: str):
    """Returns VMkernel NICs, TCP/IP stacks, and firewall ruleset entries from DB snapshot."""
    host_obj = _get_host_obj(host_name)
    data = host_obj.network_data or {}

    vmkernel_nics = data.get("vmkernel_nics", [])
    tcp_ip_stacks = data.get("tcp_ip_stacks", [])
    firewall_rules = data.get("firewall_rules", [])

    needs_refresh = (
        _looks_placeholder_only(vmkernel_nics, ["interface", "ip"]) or
        _looks_placeholder_only(tcp_ip_stacks, ["name", "enabled", "ccalgo"]) or
        _looks_placeholder_only(firewall_rules, ["name", "enabled", "allow_incoming", "allow_outgoing", "required"])
    )

    if needs_refresh:
        try:
            with get_conn(host_name) as conn:
                vmkernel_nics = net_manage.get_vmkernel_nics(conn)
                tcp_ip_stacks = net_manage.get_tcp_ip_stacks(conn)
                firewall_rules = net_manage.get_firewall_rules(conn)

            updated = dict(data)
            updated["vmkernel_nics"] = vmkernel_nics
            updated["tcp_ip_stacks"] = tcp_ip_stacks
            updated["firewall_rules"] = firewall_rules
            host_obj.network_data = updated
            host_obj.save(update_fields=["network_data", "last_sync"])
        except Exception:
            # Keep serving snapshot data if live refresh fails.
            pass

    return {
        "status": "success",
        "host": host_name,
        "vmkernel_nics": vmkernel_nics,
        "tcp_ip_stacks": tcp_ip_stacks,
        "firewall_rules": firewall_rules,
    }
