from django.contrib import admin
from django.urls import path
from django.views.generic.base import RedirectView 
from api.nebula_api import api
from manager.models import VirtualMachine
from manager.views import host_vms, vm_status_realtime, all_hosts_network, all_hosts_storage

# Inject custom views directly into the admin site's URL resolver.
# This is the only reliable way to add views under /admin/ without
# being swallowed by admin.site.urls catch-all.
_original_get_urls = admin.AdminSite.get_urls

def _patched_get_urls(self):
    vm_admin = self._registry[VirtualMachine]
    custom_urls = [
        path('manager/host/virtualmachine/', self.admin_view(vm_admin.changelist_view), name='all_hosts_vms_host'),
        path('manager/network/', self.admin_view(all_hosts_network), name='all_hosts_network'),
        path('manager/storage/', self.admin_view(all_hosts_storage), name='all_hosts_storage'),
        path('manager/vms/', self.admin_view(vm_admin.changelist_view), name='all_hosts_vms'),
    ]
    return custom_urls + _original_get_urls(self)

admin.AdminSite.get_urls = _patched_get_urls

urlpatterns = [
    # 1. Redirect root to admin
    path('', RedirectView.as_view(url='admin/', permanent=False)),

    # 2. Custom host VM views
    path('admin/hosts/<int:host_id>/vms/', host_vms, name='host_vms'),
    path('admin/vm-status-realtime/', vm_status_realtime, name='vm-status-realtime'),

    # 3. The standard Django Admin (now includes our injected network/storage/vms routes)
    path('admin/', admin.site.urls),

    # 4. Your Ninja API
    path('api/v1/', api.urls),       
]