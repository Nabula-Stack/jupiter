from django.http import Http404
from ipaddress import ip_address
from manager.models import Host
from manager.hypervisors import get_adapter


def _is_valid_ip(value):
    try:
        ip_address(value)
        return True
    except ValueError:
        return False


def find_host_obj(identifier, require_active=True):
    """Find host by name first, then by IP only if identifier is a valid IP."""
    filters = {"is_active": True} if require_active else {}

    host_obj = Host.objects.filter(name=identifier, **filters).first()
    if host_obj:
        return host_obj

    if _is_valid_ip(identifier):
        return Host.objects.filter(ip_address=identifier, **filters).first()

    return None


def get_host_obj(identifier, require_active=True):
    """Get host object by name/IP with safe IP validation."""
    host_obj = find_host_obj(identifier, require_active=require_active)
    if not host_obj:
        raise Http404(f"No Host matches '{identifier}'.")
    return host_obj

def get_conn(identifier):
    """
    Fetches credentials from DB using Name OR IP and returns an ESXiConnect instance.
    """
    host_obj = get_host_obj(identifier, require_active=True)

    return get_conn_for_host(host_obj)


def get_conn_for_host(host_obj):
    """Builds a connection using the hypervisor adapter for a host."""
    adapter = get_adapter(host_obj.hypervisor_type)
    return adapter.build_connection(host_obj)