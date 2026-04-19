from ninja import Router, Schema
from django.views.decorators.cache import cache_page
from django.core.cache import cache
from ninja.decorators import decorate_view

# Import management modules
from lib.host import manage as host_manage   # For POST requests
from lib.host import services as host_services

from manager.models import Host
from manager.utils import get_conn, find_host_obj
from manager.websocket_broadcaster import (
    broadcast_host_license_updated,
    broadcast_host_reboot_initiated,
    broadcast_host_shutdown_initiated,
)

# 1. Define the router
router = Router(tags=["Host Management"])


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_percent_int(value):
    pct = _to_float(value, 0.0)
    if pct < 0:
        pct = 0.0
    return int(round(pct))


def _storage_percent_from_snapshot(storage_data):
    data = storage_data if isinstance(storage_data, dict) else {}
    datastores = data.get("datastores") if isinstance(data.get("datastores"), list) else []

    total_capacity = 0.0
    total_used = 0.0
    fallback_usage_values = []

    for ds in datastores:
        if not isinstance(ds, dict):
            continue

        capacity = _to_float(
            ds.get("capacity_gb", ds.get("capacity", ds.get("total_gb", 0))),
            0.0,
        )
        used = _to_float(
            ds.get("used_gb", ds.get("used", ds.get("used_space_gb", 0))),
            0.0,
        )

        if capacity > 0:
            total_capacity += capacity
            total_used += max(0.0, min(used, capacity))
            continue

        usage_pct = ds.get("usage_percent", ds.get("used_percent"))
        if usage_pct is not None:
            fallback_usage_values.append(_to_float(usage_pct, 0.0))

    if total_capacity > 0:
        return _to_percent_int((total_used / total_capacity) * 100.0)

    if fallback_usage_values:
        avg = sum(fallback_usage_values) / len(fallback_usage_values)
        return _to_percent_int(avg)

    return 0

# --- Schemas ---
class LicenseSchema(Schema):
    serial_key: str

class MaintenanceSchema(Schema):
    enable: bool

class LockdownSchema(Schema):
    enable: bool

class ServiceControlSchema(Schema):
    service_name: str
    action: str  # start, stop, restart, enable, disable

# --- 1. INFO (Pulls from host_info) ---

@router.get("/all", summary="List all managed hosts")
@decorate_view(cache_page(300))
def list_all_managed_hosts(request):
    """Pulls from Postgres and caches in Redis for 5 minutes."""
    hosts = Host.objects.filter(is_active=True)
    return [
        {"name": h.name, "ip": h.ip_address, "status": "active", "hypervisor_type": h.hypervisor_type}
        for h in hosts
    ]

@router.get("/{host_name}/summary", summary="Get hardware/software summary")
@decorate_view(cache_page(60))
def get_summary(request, host_name: str):
    """Returns host summary from DB snapshot."""
    host_obj = find_host_obj(host_name, require_active=True)
    if not host_obj:
        return {"status": "error", "message": f"Host not found: {host_name}"}
    return {
        "summary": {
            "name": host_obj.name,
            "ip": str(host_obj.ip_address),
            "vendor": host_obj.vendor,
            "model": host_obj.model_name,
            "version": host_obj.os_version,
            "last_sync": host_obj.last_sync.isoformat() if host_obj.last_sync else None,
        },
        "hardware": {
            "cpu_count": host_obj.cpu_count,
            "memory_total_gb": host_obj.memory_gb,
            "vendor": host_obj.vendor,
            "processor_type": host_obj.processor_type,
        },
    }


@router.get("/metrics", summary="Get host metrics overview")
@decorate_view(cache_page(15))
def get_host_metrics(request):
    """Return host-keyed CPU/memory/storage utilization percentages."""
    hosts = Host.objects.filter(is_active=True).order_by("name")
    payload = {}

    for host_obj in hosts:
        services_snapshot = host_obj.services_status if isinstance(host_obj.services_status, dict) else {}
        cpu_percent = _to_percent_int(services_snapshot.get("cpu_usage_percent", 0))
        memory_percent = _to_percent_int(services_snapshot.get("memory_usage_percent", 0))
        storage_percent = _storage_percent_from_snapshot(host_obj.storage_data)

        payload[host_obj.name] = {
            "CPU": cpu_percent,
            "memory_percent": memory_percent,
            "storage_percent": storage_percent,
        }

    return payload

@router.get("/{host_name}/license", summary="Get license info")
@decorate_view(cache_page(3600))
def get_license(request, host_name: str):
    """Returns license details from DB snapshot."""
    host_obj = find_host_obj(host_name, require_active=True)
    if not host_obj:
        return {"status": "error", "message": f"Host not found: {host_name}"}
    return {
        "license": {
            "key": host_obj.license_key,
            "status": host_obj.license_name,
            "product": host_obj.processor_type,
            "last_sync": host_obj.last_sync.isoformat() if host_obj.last_sync else None,
        }
    }

# --- 2. CONFIG & POWER (Pulls from host_manage) ---

@router.post("/{host_name}/license")
def set_license(request, host_name: str, data: LicenseSchema):
    """Assigns a new license key via host_manage."""
    try:
        with get_conn(host_name) as conn:
            result = host_manage.add_license(conn, data.serial_key)
            # Clear specific cache
            cache.delete_pattern(f"*{host_name}/license*")
            
            # Broadcast license update
            host_obj = Host.objects.filter(name=host_name).first()
            if host_obj:
                host_obj.license_name = data.serial_key
                host_obj.save(update_fields=['license_name', 'last_sync'])
                broadcast_host_license_updated(host_obj)
            
            return {"output": result}
    except Exception as e:
        return {"error": str(e)}

@router.post("/{host_name}/reboot")
def reboot(request, host_name: str):
    """Reboots host and busts all related caches."""
    try:
        with get_conn(host_name) as conn:
            host_manage.reboot_host(conn)
            cache.delete_pattern(f"*{host_name}*")
            
            # Broadcast reboot event
            host_obj = Host.objects.filter(name=host_name).first()
            if host_obj:
                broadcast_host_reboot_initiated(host_obj)
            
            return {"message": f"Reboot command sent to {host_name}"}
    except Exception as e:
        return {"error": str(e)}

@router.post("/{host_name}/shutdown")
def shutdown(request, host_name: str):
    """Powers off host and busts all related caches."""
    try:
        with get_conn(host_name) as conn:
            host_manage.shutdown_host(conn)
            cache.delete_pattern(f"*{host_name}*")
            
            # Broadcast shutdown event
            host_obj = Host.objects.filter(name=host_name).first()
            if host_obj:
                broadcast_host_shutdown_initiated(host_obj)
            
            return {"message": f"Poweroff command sent to {host_name}"}
    except Exception as e:
        return {"error": str(e)}

@router.post("/{host_name}/maintenance")
def maintenance_mode(request, host_name: str, data: MaintenanceSchema):
    """Toggles maintenance mode via host_manage."""
    with get_conn(host_name) as conn:
        result = host_manage.set_maintenance_mode(conn, data.enable)
        # Clear summary cache so UI updates immediately
        cache.delete_pattern(f"*{host_name}/summary*")
        return {"status": "Success", "details": result}

@router.post("/{host_name}/lockdown")
def lockdown_mode(request, host_name: str, data: LockdownSchema):
    """Enable or disable ESXi lockdown mode.

    WARNING: Enabling lockdown restricts SSH to DCUI and exception users.
    """
    try:
        with get_conn(host_name) as conn:
            result = host_manage.set_lockdown_mode(conn, data.enable)
            cache.delete_pattern(f"*{host_name}/summary*")
            return {
                "status": "success",
                "host": host_name,
                "lockdown_enabled": data.enable,
                "output": result,
            }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

@router.get("/{host_name}/permissions")
def get_permissions(request, host_name: str):
    """Return local user permission assignments for the host."""
    try:
        with get_conn(host_name) as conn:
            output = host_manage.get_host_permissions(conn)
        # Parse tabular output: Principal  IsGroup  Role  PropagateToChildren
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        entries = []
        for line in lines[1:]:  # skip header row
            parts = line.split()
            if len(parts) >= 3:
                entries.append({
                    "principal": parts[0],
                    "is_group": parts[1].lower() == "true",
                    "role": parts[2],
                    "propagate": parts[3].lower() == "true" if len(parts) > 3 else False,
                })
        return {"status": "success", "host": host_name, "permissions": entries, "raw": output}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

@router.post("/{host_name}/support-bundle")
def generate_support_bundle(request, host_name: str):
    """Trigger vm-support diagnostic bundle generation on the host."""
    try:
        with get_conn(host_name) as conn:
            output = host_manage.generate_support_bundle(conn)
        return {"status": "success", "host": host_name, "output": output}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

# --- 3. SERVICES ---

@router.post("/{host_name}/services/control")
def service_control(request, host_name: str, data: ServiceControlSchema):
    """Control host services (start/stop/restart/enable/disable)."""
    action = (data.action or "").strip().lower()
    service_name = (data.service_name or "").strip()
    if not service_name:
        return {"status": "error", "message": "service_name is required"}

    host_obj = find_host_obj(host_name, require_active=True)
    if not host_obj:
        return {"status": "error", "message": f"Host not found: {host_name}"}

    if host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
        try:
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                result = conn.control_systemd_service(node, service_name, action)
                services = conn.list_systemd_services(node)
                service_status = next(
                    (s.get("status", "unknown") for s in services if s.get("service") == service_name),
                    "unknown",
                )
                return {
                    "status": "success",
                    "host": host_name,
                    "service_name": service_name,
                    "action": action,
                    "output": str(result or ""),
                    "service_status": service_status,
                }
        except Exception as exc:
            return {"status": "error", "message": f"Proxmox service control error: {str(exc)}"}

    try:
        with get_conn(host_name) as conn:
            # API-mode: preserve detailed faults instead of collapsing to generic success/error strings.
            if hasattr(conn, "control_service") and hasattr(conn, "set_service_policy"):
                if action in {"start", "stop", "restart"}:
                    result = conn.control_service(service_name, action) or {}
                elif action in {"enable", "disable"}:
                    result = conn.set_service_policy(service_name, action == "enable") or {}
                else:
                    return {"status": "error", "message": f"Unsupported action: {action}"}

                service_status = conn.get_service_status(service_name) if hasattr(conn, "get_service_status") else "Unknown"
                if str(result.get("status", "success")).lower() == "error":
                    msg = str(result.get("message") or "Service control failed")
                    if "cannot start ntpd without one server defined" in msg.lower():
                        msg += " Configure at least one NTP server first, then retry starting ntpd."
                    return {
                        "status": "error",
                        "host": host_name,
                        "service_name": service_name,
                        "action": action,
                        "message": msg,
                        "service_status": str(service_status or "").strip(),
                    }

                return {
                    "status": "success",
                    "host": host_name,
                    "service_name": service_name,
                    "action": action,
                    "output": str(result.get("message") or result.get("policy") or result.get("action") or "success").strip(),
                    "service_status": str(service_status or "").strip(),
                }

            # SSH-mode (legacy path)
            if action in {"start", "stop", "restart"}:
                output = host_services.control_service(conn, service_name, action)
            elif action in {"enable", "disable"}:
                output = host_services.set_service_policy(conn, service_name, action == "enable")
            else:
                return {"status": "error", "message": f"Unsupported action: {action}"}

            status = host_services.get_service_status(conn, service_name)
            if isinstance(output, str) and output.startswith("Error:"):
                return {
                    "status": "error",
                    "host": host_name,
                    "service_name": service_name,
                    "action": action,
                    "message": output,
                    "service_status": (status or "").strip(),
                }
            return {
                "status": "success",
                "host": host_name,
                "service_name": service_name,
                "action": action,
                "output": (output or "").strip(),
                "service_status": (status or "").strip(),
            }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@router.get("/{host_name}/services", summary="List host services")
@decorate_view(cache_page(30))
def list_host_services(request, host_name: str):
    """Return service names and current status for a host from DB snapshot."""
    try:
        host_obj = find_host_obj(host_name, require_active=True)
        if not host_obj:
            return {"status": "error", "message": f"Host not found: {host_name}", "services": []}

        snapshot = host_obj.services_status or {}
        services = snapshot.get("services", []) if isinstance(snapshot, dict) else []
        services = sorted(
            [{"name": (s.get("name") or "").strip(), "status": (s.get("status") or "").strip()} for s in services if s.get("name")],
            key=lambda x: x["name"].lower(),
        )
        return {
            "status": "success",
            "host": host_name,
            "services": services,
            "cpu_usage_percent": snapshot.get("cpu_usage_percent", 0) if isinstance(snapshot, dict) else 0,
            "memory_usage_percent": snapshot.get("memory_usage_percent", 0) if isinstance(snapshot, dict) else 0,
            "last_sync": host_obj.last_sync.isoformat() if host_obj.last_sync else None,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc), "services": []}

# --- 4. CACHE MANAGEMENT ---

@router.post("/cache/clear-all", summary="Clear all cached data")
def clear_all_cache(request):
    """Clears all cache entries across the system."""
    try:
        cache.clear()
        return {
            "status": "success",
            "message": "All cache cleared successfully",
            "timestamp": str(__import__('datetime').datetime.now())
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to clear cache: {str(e)}"
        }

@router.post("/cache/clear-host", summary="Clear cache for specific host")
def clear_host_cache(request, host_name: str):
    """Clears all cache entries for a specific host."""
    try:
        cache.delete_pattern(f"*{host_name}*")
        return {
            "status": "success",
            "message": f"Cache cleared for host: {host_name}",
            "timestamp": str(__import__('datetime').datetime.now())
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to clear cache for {host_name}: {str(e)}"
        }
