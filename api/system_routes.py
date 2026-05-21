from ninja import Router

from manager.hypervisors import list_adapter_slugs
from manager.models import Host

router = Router(tags=["System"])


@router.get("/hypervisors", summary="List supported hypervisor plugins")
def get_supported_hypervisors(request):
    return {
        "supported": list_adapter_slugs(),
    }


@router.get("/hosts/hypervisors", summary="List host to hypervisor mapping")
def get_host_hypervisor_map(request):
    hosts = Host.objects.filter(is_active=True).values("name", "ip_address", "hypervisor_type")
    return {
        "hosts": list(hosts),
    }
