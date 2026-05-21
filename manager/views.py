import json
from django.shortcuts import render, get_object_or_404
from .models import Host

def dashboard(request):
    # Read hosts and VMs from the database — no live connection needed here.
    # The background sync worker (run_sync.py) keeps the data current.
    hosts = Host.objects.filter(is_active=True)
    inventory = []

    for host_obj in hosts:
        vm_qs = host_obj.vms.all() if hasattr(host_obj, 'vms') else []
        inventory.append({
            'host': host_obj,
            'vms': vm_qs,
            'status': 'synced',
        })

    return render(request, 'manager/dashboard.html', {'inventory': inventory})


def host_vms(request, host_id):
    """View to display VMs for a specific host with auto-refresh."""
    host = get_object_or_404(Host, pk=host_id)
    vms = host.vms.all().order_by('name')
    
    context = {
        'title': f'VMs on {host.name}',
        'host': host,
        'vms': [
            {
                'id': vm.id,
                'vmid': vm.vmid,
                'name': vm.name,
                'state': vm.power_state,
                'power_state': vm.power_state,
                'guest_name': vm.guest_os or 'Unknown',
                'guest_os': vm.guest_os or 'Unknown',
                'ip_address': str(vm.ip_address) if vm.ip_address else 'N/A',
                'is_running': vm.power_state.lower() == 'poweredon',
            }
            for vm in vms
        ]
    }
    return render(request, 'admin/host_vms.html', context)


def vm_status_realtime(request):
    """
    Real-time VM status dashboard with WebSocket updates.
    Displays all VMs with live status updates via Django Channels.
    
    URL: /admin/vm-status-realtime/
    """
    from .models import VirtualMachine
    
    vms = VirtualMachine.objects.select_related('host').all().order_by('name')
    
    context = {
        'title': 'VM Status - Real-Time Updates',
        'vms': vms,
        'total_vms': vms.count(),
        'powered_on': vms.filter(power_state='poweredOn').count(),
        'powered_off': vms.filter(power_state='poweredOff').count(),
    }
    
    return render(request, 'admin/vm_status_realtime.html', context)


def all_hosts_network(request):
    """Display network configuration for all active hosts."""
    hosts = Host.objects.filter(is_active=True)
    hosts_list = [{'pk': h.pk, 'name': h.name, 'ip_address': str(h.ip_address)} for h in hosts]
    
    context = {
        'hosts_json': json.dumps(hosts_list),
        'title': 'Network Management - All Hosts',
    }
    return render(request, 'admin/all_hosts_network.html', context)


def all_hosts_storage(request):
    """Display storage configuration for all active hosts."""
    hosts = Host.objects.filter(is_active=True)
    hosts_list = [{'pk': h.pk, 'name': h.name, 'ip_address': str(h.ip_address)} for h in hosts]
    
    context = {
        'hosts_json': json.dumps(hosts_list),
        'title': 'Storage Management - All Hosts',
    }
    return render(request, 'admin/all_hosts_storage.html', context)


def all_hosts_vms(request):
    """Display VMs for all active hosts."""
    hosts = Host.objects.filter(is_active=True)
    
    context = {
        'hosts': hosts,
        'title': 'Virtual Machines - All Hosts',
    }
    return render(request, 'admin/all_hosts_vms.html', context)