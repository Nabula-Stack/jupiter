import json
import datetime
import requests
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.core.cache import cache
from unfold.admin import ModelAdmin
from unfold.decorators import action, display

from manager.models import VirtualMachine, Host
from manager.services import trigger_vm_action, sync_vms_for_host

@admin.register(VirtualMachine)
class VirtualMachineAdmin(ModelAdmin):
    # Unfold native hook: inject wizard below the form on /add/ page only.
    change_form_outer_after_template = "admin/vm_create_wizard.html"
    change_form_template = "admin/manager/virtualmachine/change_form.html"
    list_after_template = "admin/vm_list_extras.html"

    def get_fieldsets(self, request, obj=None):
        # On the add page the wizard handles everything — hide all Django form fields.
        if obj is None:
            return []
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return []
        return super().get_readonly_fields(request, obj)

    def has_add_permission(self, request):
        # Keep the add URL accessible so the wizard template renders.
        return True

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    # --- LIST VIEW CONFIG ---
    list_display = [
        "name",
        "display_status_live",
        "display_tools_status_live",
        "console_access",
        "used_space",
        "guest_os",
        "display_esxi_host",
        "host_cpu",
        "host_memory",
    ]

    list_filter = ["power_state", "tools_status", "host"]
    search_fields = ["name", "vmid", "host__name"]
    
    # Load WebSocket script for live updates
    class Media:
        js = ('js/vm_live_updates.js',)

    # --- DETAIL VIEW LAYOUT ---
    fieldsets = (
        ("VM Identity & Quick Launch", {
            "fields": (("name", "vmid"), ("host", "console_access"), "uuid", "vmx_path")
        }),
        ("System Info", {
            "classes": ["tab"],
            "fields": ("display_guest_os", ("display_distro", "display_kernel"), ("hw_version", "display_tools_status_live")),
        }),
        ("Resource Allocation", {
            "classes": ["tab"],
            "fields": (("num_cpu", "memory_mb"), ("storage_used_text", "storage_provisioned_text")),
        }),
        ("Live Runtime Stats", {
            "classes": ["tab"],
            "fields": (("display_status_live", "get_live_ip"), ("host_cpu", "host_memory"), "get_live_uptime", "display_overall_status"),
        }),
        ("Networking", {
            "classes": ["tab"],
            "fields": ("display_dns_name", "display_networks", "display_dns_servers"),
        }),
        ("Action History", {
            "classes": ["collapse"],
            "fields": ("display_action_history",),
        }),
    )

    readonly_fields = [
        "vmid", "uuid", "vmx_path", "guest_os", "distro", "kernel",
        "display_guest_os", "display_distro", "display_kernel", "display_overall_status",
        "hw_version", "display_tools_status_live", "num_cpu", "memory_mb",
        "storage_used_gb", "storage_provisioned_gb", "cpu_usage_mhz", 
        "mem_active_mb", "overall_status", "display_networks", "display_dns_servers",
        "display_dns_name", "display_status_live", "get_live_ip", "get_live_uptime", "networks",
        "dns_servers", "display_action_history", "display_esxi_host",
        "used_space", "host_cpu", "host_memory", "console_access",
        "display_storage_used", "display_storage_provisioned",
        "storage_used_text", "storage_provisioned_text",
    ]

    actions_list = ["sync_inventory_action", "refresh_vms_action", "register_vm_modal_action", "deploy_ova_modal_action"]
    actions_detail = [
        {
            "title": "VM Control",
            "icon": "settings_power",
            "items": [
                "power_on_action",
                "shutdown_action",
                "power_off_action",
                "reset_action",
                "suspend_action",
                "snapshot_create_action",
                "snapshot_delete_action",
                "snapshot_restore_action",
                "delete_vm_action",
                "unregister_action",
            ],
        }
    ]
    actions = [
        "power_on_action",
        "shutdown_action",
        "power_off_action",
        "reset_action",
        "suspend_action",
        "snapshot_create_action",
        "snapshot_delete_action",
        "snapshot_restore_action",
        "delete_vm_action",
        "unregister_action",
    ]

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        # Strip UI-routing params so Django doesn't treat them as model filters.
        query = request.GET.copy()
        for key in ("tab", "host"):
            if key in query:
                query.pop(key)
        request.GET = query
        return super().changelist_view(request, extra_context=extra_context)

    # --- UI Formatting Helpers ---

    def _to_float(self, value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @display(description="Console", label=True)
    def console_access(self, obj):
        """ESXi style console launcher icon-only button."""
        if not obj.host: return "No Host"
        host_id = obj.host.ip_address or obj.host.name
        api_url = f"/api/v1/vms/{host_id}/{obj.vmid}/console"

        return format_html(
            '<div style="display: flex; gap: 8px; align-items: center;">'
            '<a href="{}" target="_blank" title="Launch Console" style="background: #2563eb; color: #ffffff; padding: 8px; border-radius: 6px; display: flex; align-items: center; justify-content: center; text-decoration: none; transition: all 0.2s;" onmouseover="this.style.background=\'#1d4ed8\'" onmouseout="this.style.background=\'#2563eb\'">'
            '<span class="material-symbols-outlined" style="font-size: 20px;">monitor</span>'
            '</a>'
            '</div>', 
            api_url
        )

    @display(description="DNS Name (Hostname)")
    def display_dns_name(self, obj):
        name = getattr(obj, 'dns_name', None)
        if not name:
            live_data = self._get_cached_data(obj)
            name = live_data.get("vm_name") or live_data.get("dns_name")
        return format_html(
            '<span style="background: rgba(59, 130, 246, 0.1); color: #3b82f6; padding: 2px 10px; border-radius: 6px; font-size: 12px; font-family: monospace; border: 1px solid rgba(59, 130, 246, 0.2); font-weight: bold;">{}</span>',
            name or "N/A"
        )

    @display(description="Guest OS")
    def display_guest_os(self, obj):
        live_data = self._get_cached_data(obj)
        return obj.guest_os or live_data.get("guest_name") or "N/A"

    @display(description="Distro")
    def display_distro(self, obj):
        live_data = self._get_cached_data(obj)
        return obj.distro or live_data.get("distro") or "N/A"

    @display(description="Kernel")
    def display_kernel(self, obj):
        live_data = self._get_cached_data(obj)
        return obj.kernel or live_data.get("kernel") or "N/A"

    @display(description="Network Interfaces")
    def display_networks(self, obj):
        nets = obj.networks or []
        if not nets:
            return mark_safe('<span style="color: #94a3b8;">None</span>')
        html = '<div style="display: flex; flex-wrap: wrap; gap: 6px;">'
        for n in nets:
            ips = n.get('ip', [])
            if isinstance(ips, str): ips = [ips]
            for ip in ips:
                html += f'<span style="background: rgba(148, 163, 184, 0.1); color: inherit; padding: 2px 10px; border-radius: 6px; font-size: 12px; font-family: monospace; border: 1px solid rgba(148, 163, 184, 0.2);">{ip}</span>'
        return mark_safe(html + '</div>')

    @display(description="DNS Servers")
    def display_dns_servers(self, obj):
        servers = obj.dns_servers or []
        if not servers:
            return mark_safe('<span style="color: #94a3b8;">None</span>')
        if isinstance(servers, str):
            try: servers = json.loads(servers.replace("'", '"'))
            except: servers = [servers]
        html = '<div style="display: flex; flex-wrap: wrap; gap: 6px;">'
        for srv in servers:
            html += f'<span style="background: rgba(16, 185, 129, 0.1); color: #10b981; padding: 2px 10px; border-radius: 6px; font-size: 12px; font-family: monospace; border: 1px solid rgba(16, 185, 129, 0.2);">{srv}</span>'
        return mark_safe(html + '</div>')

    @display(description="Storage Used")
    def display_storage_used(self, obj):
        """Displays used storage with live update support."""
        try:
            live_data = self._get_cached_data(obj)
            used_gb = obj.storage_used_gb
            if used_gb in (None, ""):
                used_gb = live_data.get("storage_used_gb", 0)
            used_gb = self._to_float(used_gb, 0.0)
            return format_html(
                '<span id="vm-storage-used-{}" style="background: rgba(239, 68, 68, 0.1); color: #dc2626; padding: 4px 8px; border-radius: 4px; font-weight: bold;">{:.2f} GB</span>',
                obj.id, used_gb
            )
        except Exception:
            return format_html(
                '<span id="vm-storage-used-{}" style="background: rgba(239, 68, 68, 0.1); color: #dc2626; padding: 4px 8px; border-radius: 4px; font-weight: bold;">{:.2f} GB</span>',
                obj.id, 0.0
            )
    
    @display(description="Storage Provisioned")
    def display_storage_provisioned(self, obj):
        """Displays total provisioned storage capacity."""
        try:
            live_data = self._get_cached_data(obj)
            prov_gb = obj.storage_provisioned_gb
            if prov_gb in (None, ""):
                prov_gb = live_data.get("storage_provisioned_gb", 0)
            prov_gb = self._to_float(prov_gb, 0.0)
            return format_html(
                '<span id="vm-storage-prov-{}" style="background: rgba(59, 130, 246, 0.1); color: #3b82f6; padding: 4px 8px; border-radius: 4px; font-weight: bold;">{:.2f} GB</span>',
                obj.id, prov_gb
            )
        except Exception:
            return format_html(
                '<span id="vm-storage-prov-{}" style="background: rgba(59, 130, 246, 0.1); color: #3b82f6; padding: 4px 8px; border-radius: 4px; font-weight: bold;">{:.2f} GB</span>',
                obj.id, 0.0
            )

    @admin.display(description="Storage Used")
    def storage_used_text(self, obj):
        # Plain text fallback for readonly form rendering in Unfold tabs.
        try:
            value = obj.storage_used_gb
            if value in (None, ""):
                value = self._get_cached_data(obj).get("storage_used_gb", 0)
            return f"{self._to_float(value, 0.0):.2f} GB"
        except Exception:
            return "0.00 GB"

    @admin.display(description="Storage Provisioned")
    def storage_provisioned_text(self, obj):
        # Plain text fallback for readonly form rendering in Unfold tabs.
        try:
            value = obj.storage_provisioned_gb
            if value in (None, ""):
                value = self._get_cached_data(obj).get("storage_provisioned_gb", 0)
            return f"{self._to_float(value, 0.0):.2f} GB"
        except Exception:
            return "0.00 GB"

    # --- Core VM Actions ---
    def _handle_action(self, request, queryset, object_id, action_key, success_msg, params=None):
        objs = [VirtualMachine.objects.get(pk=object_id)] if object_id else queryset
        for obj in objs:
            ok, msg = trigger_vm_action(obj, action_key, params=params)
            if ok:
                self.message_user(request, f"{success_msg} ({obj.name})")
            else:
                self.message_user(request, f"{obj.name}: {msg}", messages.ERROR)
        return redirect(request.META.get('HTTP_REFERER', request.path))

    @action(description="Power ON", icon="play_arrow")
    def power_on_action(self, request, queryset=None, object_id=None):
        return self._handle_action(request, queryset, object_id, "poweron", "Power ON signal sent.")

    @action(description="Guest Shutdown", icon="stop")
    def shutdown_action(self, request, queryset=None, object_id=None):
        return self._handle_action(request, queryset, object_id, "shutdown", "Shutdown signal sent.")

    @action(description="Power OFF", icon="power_settings_new")
    def power_off_action(self, request, queryset=None, object_id=None):
        """Immediately powers off the VM (hard shutdown). VM will not gracefully shutdown."""
        print(f"[Admin Action] POWER OFF called with action_type='poweroff'")
        return self._handle_action(request, queryset, object_id, "poweroff", "🔴 Hard Power OFF executed.")

    @action(description="Guest Shutdown", icon="stop")
    def shutdown_action(self, request, queryset=None, object_id=None):
        return self._handle_action(request, queryset, object_id, "shutdown", "Shutdown signal sent.")

    @action(description="Reset", icon="refresh")
    def reset_action(self, request, queryset=None, object_id=None):
        return self._handle_action(request, queryset, object_id, "reset", "Reset signal sent.")

    @action(description="Suspend", icon="pause")
    def suspend_action(self, request, queryset=None, object_id=None):
        """Pauses the VM and saves state to memory. VM state is preserved."""
        print(f"[Admin Action] SUSPEND called with action_type='suspend'")
        return self._handle_action(request, queryset, object_id, "suspend", "⏸️ VM Suspend signal sent.")

    @action(description="Snapshot", icon="add_a_photo")
    def snapshot_create_action(self, request, queryset=None, object_id=None):
        name = f"Snap-{datetime.datetime.now().strftime('%m%d-%H%M')}"
        return self._handle_action(request, queryset, object_id, "create", f"Snapshot {name} started.", params={"op": "create", "name": name})

    @action(description="Delete Snap", icon="delete_sweep")
    def snapshot_delete_action(self, request, queryset=None, object_id=None):
        return self._handle_action(
            request,
            queryset,
            object_id,
            "delete_all",
            "Deleted all snapshots.",
            params={"op": "delete_all"},
        )

    @action(description="Restore Snap", icon="history")
    def snapshot_restore_action(self, request, queryset=None, object_id=None):
        return self._handle_action(request, queryset, object_id, "restore", "Restoring snapshot...", params={"op": "restore"})

    @action(description="Delete VM", icon="delete_forever")
    def delete_vm_action(self, request, queryset=None, object_id=None):
        objs = [VirtualMachine.objects.get(pk=object_id)] if object_id else queryset
        deleted = []
        for obj in objs:
            try:
                from manager.utils import get_conn
                from lib.kvm import manage as kvm_manage
                import lib.vms.manage as vm_manage

                with get_conn(obj.host.name) as conn:
                    if obj.host.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
                        node = conn.resolve_node(obj.host.name)
                        conn.vm_delete(node, obj.vmid, purge=False, destroy_unreferenced_disks=True)
                    elif obj.host.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
                        kvm_manage.delete_vm(conn, obj.vmid)
                    else:
                        if hasattr(conn, "destroy_vm"):
                            result = conn.destroy_vm(obj.vmid)
                            if (result or {}).get("status") == "error":
                                raise RuntimeError((result or {}).get("message") or "Delete failed")
                        else:
                            result = vm_manage.destroy_vm(conn, obj.vmid)
                            if isinstance(result, str) and result.startswith("Error:"):
                                raise RuntimeError(result)
                obj.delete()
                deleted.append(obj.name)
            except Exception as e:
                self.message_user(request, f"Error deleting {obj.name}: {e}", messages.ERROR)
        if deleted:
            self.message_user(request, f"Deleted: {', '.join(deleted)}")
        return redirect("/admin/manager/virtualmachine/")

    @action(description="Unregister VM", icon="link_off")
    def unregister_action(self, request, queryset=None, object_id=None):
        objs = [VirtualMachine.objects.get(pk=object_id)] if object_id else queryset
        unregistered = []
        for obj in objs:
            try:
                from manager.utils import get_conn
                from lib.kvm import manage as kvm_manage
                import lib.vms.manage as vm_manage

                with get_conn(obj.host.name) as conn:
                    if obj.host.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
                        kvm_manage.unregister_vm(conn, obj.vmid)
                    elif obj.host.hypervisor_type != Host.HYPERVISOR_PROXMOX_VE:
                        if hasattr(conn, "unregister_vm_by_identifier"):
                            result = conn.unregister_vm_by_identifier(obj.vmid)
                            if (result or {}).get("status") == "error":
                                raise RuntimeError((result or {}).get("message") or "Unregister failed")
                        else:
                            result = vm_manage.unregister_vm(conn, obj.vmid)
                            if isinstance(result, str) and result.startswith("Error:"):
                                raise RuntimeError(result)
                # Proxmox unregister means remove from Nebula inventory only.
                obj.delete()
                unregistered.append(obj.name)
            except Exception as e:
                self.message_user(request, f"Error unregistering {obj.name}: {e}", messages.ERROR)
        if unregistered:
            self.message_user(request, f"Unregistered (files kept): {', '.join(unregistered)}")
        return redirect("/admin/manager/virtualmachine/")

    @action(description="Register VM", icon="app_registration")
    def register_vm_modal_action(self, request, queryset=None):
        return redirect(request.META.get('HTTP_REFERER', "/admin/manager/virtualmachine/"))

    @action(description="Deploy OVA", icon="cloud_upload")
    def deploy_ova_modal_action(self, request, queryset=None):
        return redirect(request.META.get('HTTP_REFERER', "/admin/manager/virtualmachine/"))

    @action(description="Sync Inventory")
    def sync_inventory_action(self, request, queryset=None):
        for host in Host.objects.all(): 
            sync_vms_for_host(host)
        self.message_user(request, "Inventory sync complete.")
        return redirect(request.META.get('HTTP_REFERER', "/admin/manager/virtualmachine/"))
    
    @action(description="Refresh VM States")
    def refresh_vms_action(self, request, queryset=None):
        """Refresh VM states from database for selected host or all hosts."""
        if queryset:
            # Get unique hosts from selected VMs
            hosts = set(vm.host for vm in queryset)
            for host in hosts:
                sync_vms_for_host(host)
            self.message_user(request, f"Refreshed VMs for {len(hosts)} host(s).")
        else:
            # Refresh all hosts if none selected
            for host in Host.objects.filter(is_active=True):
                sync_vms_for_host(host)
            self.message_user(request, "Refreshed all VMs.")
        return redirect(request.META.get('HTTP_REFERER', "/admin/manager/virtualmachine/"))

    # --- Standard Display Helpers ---

    @display(description="Status")
    def display_status_live(self, obj):
        s = (obj.power_state or "").lower()
        if "on" in s:
            color, text = "#10b981", "Running"
        elif "suspend" in s:
            color, text = "#f59e0b", "Suspended"
        else:
            color, text = "#ef4444", "Stopped"
        return format_html(
            '<span id="vm-status-{}" data-vm-id="{}" style="color:{}; font-weight:bold;">● {}</span>',
            obj.id, obj.id, color, text
        )

    @display(description="Tools")
    def display_tools_status_live(self, obj):
        if obj.host and obj.host.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
            state = (obj.tools_running or "not running").lower()
            color = "#10b981" if state == "running" else "#ef4444"
            text = "running" if state == "running" else "not running"
            return format_html(
                '<span id="vm-tools-{}" data-vm-id="{}" style="color:{};">{}</span>',
                obj.id, obj.id, color, text
            )

        s = (obj.tools_status or "").lower()
        color = "#10b981" if "ok" in s else "#ef4444"
        return format_html(
            '<span id="vm-tools-{}" data-vm-id="{}" style="color:{};">{}</span>',
            obj.id, obj.id, color, obj.tools_status or "Unknown"
        )

    @display(description="Action History")
    def display_action_history(self, obj):
        if not obj.action_history: return "No entries"
        return format_html('<small>{} records</small>', len(obj.action_history))

    def _get_cached_data(self, obj):
        try:
            return cache.get(f"ninja:vm_details:{obj.host.ip_address}:{obj.vmid}") or {}
        except Exception:
            return {}

    @display(description="Live IP")
    def get_live_ip(self, obj): return self._get_cached_data(obj).get("ip_address") or obj.ip_address or "N/A"

    @display(description="Uptime")
    def get_live_uptime(self, obj): return self._get_cached_data(obj).get("uptime_human") or "0s"

    @display(description="Overall Status")
    def display_overall_status(self, obj):
        return (obj.overall_status or "unknown").upper()

    @display(description="Used Space")
    def used_space(self, obj): return f"{obj.storage_used_gb or 0} GB"

    @display(description="Used Space")
    def used_space(self, obj): return format_html(
        '<span id="vm-storage-{}">{} GB</span>',
        obj.id, obj.storage_used_gb or 0
    )

    @display(description="Guest OS")
    def guest_os(self, obj): return obj.guest_os or "Unknown"

    @display(description="Host")
    def display_esxi_host(self, obj):
        if not obj.host:
            return "N/A"
        host_name = obj.host.name
        return format_html('<span style="font-weight: bold;">{}</span>', host_name)

    @display(description="CPU")
    def host_cpu(self, obj): return format_html(
        '<span id="vm-cpu-{}">{} MHz</span>',
        obj.id, obj.cpu_usage_mhz or 0
    )

    @display(description="RAM")
    def host_memory(self, obj): return format_html(
        '<span id="vm-memory-{}">{} MB</span>',
        obj.id, obj.mem_active_mb or 0
    )