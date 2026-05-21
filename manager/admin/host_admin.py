import json

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin
from unfold.decorators import action, display

from lib.host import manage as host_manage
from manager.utils import get_conn
from manager.models import Host
from manager.services import sync_host_details_to_db

# Host form collects SSH public key for key-based access.
class HostAdminForm(forms.ModelForm):
    class Meta:
        model = Host
        fields = "__all__"
        widgets = {
            'ssh_public_key': forms.PasswordInput(
                render_value=False,
                attrs={'placeholder': 'Paste new SSH public key to replace current one'}
            ),
            'password': forms.PasswordInput(
                render_value=False,
                attrs={'placeholder': 'Password'}
            ),
        }

    def clean_ssh_public_key(self):
        ssh_public_key = self.cleaned_data.get('ssh_public_key', '')
        hypervisor = self.cleaned_data.get('hypervisor_type')
        connection_method = self.cleaned_data.get('esxi_connection_method')

        # Only require SSH key for ESXi + SSH mode
        if hypervisor == Host.HYPERVISOR_VMWARE_ESXI and connection_method == Host.CONNECTION_SSH:
            if not ssh_public_key and self.instance.pk:
                # Preserve current key when editing and field is intentionally left blank
                return self.instance.ssh_public_key
        
        return ssh_public_key

    def clean_password(self):
        password = self.cleaned_data.get('password', '')
        hypervisor = self.cleaned_data.get('hypervisor_type')
        connection_method = self.cleaned_data.get('esxi_connection_method')

        # Only require password for ESXi + API mode (or other hypervisors)
        if hypervisor == Host.HYPERVISOR_VMWARE_ESXI and connection_method == Host.CONNECTION_API:
            if not password and self.instance.pk:
                # Preserve current password when editing
                return self.instance.password
        
        return password

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        hypervisor = self.cleaned_data.get('hypervisor_type')

        # Proxmox REST API requires the user@realm format (e.g. root@pam).
        if hypervisor == Host.HYPERVISOR_PROXMOX_VE:
            if username and '@' not in username:
                from django.core.exceptions import ValidationError
                raise ValidationError(
                    "Proxmox requires the username in user@realm format "
                    "(e.g. root@pam or admin@pve)."
                )
        return username

@admin.register(Host)
class HostAdmin(ModelAdmin):
    form = HostAdminForm
    list_after_template = "admin/host_tabs_content.html"
    actions = (
        "action_host_overview",
        "action_create_register_vm",
        "action_shutdown_selected",
        "action_reboot_selected",
        "action_open_services_tab",
        "action_enter_maintenance_mode",
        "action_lockdown_mode",
        "action_permissions",
        "action_generate_support_bundle",
        "action_get_ssh_for_chrome",
    )
    
    # --- UI Configuration ---
    list_display = (
        'name', 
        'ip_address', 
        'display_web_ui_link',
        'hypervisor_type',
        'os_version', 
        'model_name', 
        'display_cpu_usage', 
        'display_mem_usage', 
        'display_vms_link',
        'is_active', 
        'last_sync'
    )
    
    fieldsets = (
        ('Connection', {
            'fields': ('name', 'ip_address', 'hypervisor_type', 'esxi_connection_method', 'username', 'password', 'ssh_public_key', 'is_active'),
            'description': 'For ESXi with SSH: provide SSH public key. For ESXi with vSphere API: provide password.',
        }),
        ('System Summary', {
            'fields': (('vendor', 'model_name'), 'os_version', 'processor_type'),
            'classes': ('tab',),
        }),
        ('Hardware Capacity', {
            'fields': ('cpu_count', 'memory_gb', 'services_status'),
            'classes': ('tab',),
        }),
        ('Licensing', {
            'fields': ('license_name', 'license_key'),
            'classes': ('tab',),
        }),
    )

    readonly_fields = [
        'vendor', 'model_name', 'processor_type', 'os_version',
        'license_name', 'license_key', 'services_status',
        'cpu_count', 'memory_gb', 'display_vms_link'
    ]

    # --- Real-Time Assets ---
    class Media:
        js = ('js/vm_live_updates.js', 'js/host_admin_fields.js')

    # --- Real-Time Display Hooks ---
    @display(description="CPU Usage", label=True)
    def display_cpu_usage(self, obj):
        usage = (obj.services_status or {}).get('cpu_usage_percent', '0')
        # We wrap in a span with a unique ID for the WebSocket to target
        return format_html('<span id="host-cpu-{}">{}%</span>', obj.pk, usage)

    @display(description="RAM Usage", label=True)
    def display_mem_usage(self, obj):
        usage = (obj.services_status or {}).get('memory_usage_percent', '0')
        return format_html('<span id="host-mem-{}">{}%</span>', obj.pk, usage)
    
    @display(description="VMs")
    def display_vms_link(self, obj):
        vm_count = obj.vms.count()
        btn_html = (
            f'<a href="/admin/manager/virtualmachine/?host__id__exact={obj.pk}" '
            'class="text-white px-2 py-1 rounded text-xs font-bold no-underline inline-block" '
            'style="background: #2563eb; margin-right: 4px;">'
            f'{vm_count} VMs</a>'
        )
        btn_html += (
            f'<a href="/admin/manager/host/?tab=network&host={obj.pk}#main-tab-bar" '
            f'onclick="return window.nebulaHostTabs ? window.nebulaHostTabs.open(\'network\', {obj.pk}) : true;" '
            'class="text-white px-2 py-1 rounded text-xs font-bold no-underline inline-block" '
            'style="background: #06b6d4; margin-right: 4px;">Network</a>'
        )
        btn_html += (
            f'<a href="/admin/manager/host/?tab=storage&host={obj.pk}#main-tab-bar" '
            f'onclick="return window.nebulaHostTabs ? window.nebulaHostTabs.open(\'storage\', {obj.pk}) : true;" '
            'class="text-white px-2 py-1 rounded text-xs font-bold no-underline inline-block" '
            'style="background: #7c3aed;">Storage</a>'
        )
        return mark_safe(btn_html)

    @display(description="Web UI")
    def display_web_ui_link(self, obj):
        if obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE:
            url = f"https://{obj.ip_address}:8006"
        elif obj.hypervisor_type == Host.HYPERVISOR_KVM_LIBVIRT:
            url = f"https://{obj.ip_address}:9090"
        else:
            url = f"https://{obj.ip_address}/ui"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer" '
            'title="Open ESXi Web UI" '
            'style="display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:8px;border:1px solid #cbd5e1;color:#0ea5e9;text-decoration:none;">'
            '<span class="material-symbols-outlined" style="font-size:18px">language</span>'
            '</a>',
            url,
        )

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        # 'tab' and 'host' are UI routing params used by the custom tabs bar,
        # not model filters. Remove them before Django admin processes changelist
        # filters to avoid redirecting to ?e=1 on first load.
        query = request.GET.copy()
        for key in ("tab", "host"):
            if key in query:
                query.pop(key)
        request.GET = query

        hosts = Host.objects.filter(is_active=True)
        extra_context['all_hosts_json'] = json.dumps([
            {'pk': h.pk, 'name': h.name, 'ip': str(h.ip_address)}
            for h in hosts
        ])
        return super().changelist_view(request, extra_context=extra_context)

    # --- Admin Actions ---
    def _single_selected_host(self, queryset):
        if queryset.count() != 1:
            return None
        return queryset.first()

    def action_host_overview(self, request, queryset):
        """Open selected host in Host Management view."""
        host = self._single_selected_host(queryset)
        if host:
            return redirect(f"/admin/manager/host/?tab=hosts&host={host.pk}#main-tab-bar")
        self.message_user(request, "Select exactly one host to open Host view.", messages.WARNING)
    action_host_overview.short_description = "Host"

    def action_create_register_vm(self, request, queryset):
        """Open VM create/register workflow."""
        host = self._single_selected_host(queryset)
        if host:
            return redirect(f"/admin/manager/virtualmachine/add/?host={host.pk}")
        return redirect("/admin/manager/virtualmachine/add/")
    action_create_register_vm.short_description = "Create/Register VM"

    def action_shutdown_selected(self, request, queryset):
        """Shutdown selected hosts."""
        ok = 0
        for host in queryset:
            if self._do_shutdown(request, host):
                ok += 1
        self.message_user(request, f"Shutdown command sent to {ok} host(s).")
    action_shutdown_selected.short_description = "Shut down"

    def action_reboot_selected(self, request, queryset):
        """Reboot selected hosts."""
        ok = 0
        for host in queryset:
            if self._do_reboot(request, host):
                ok += 1
        self.message_user(request, f"Reboot command sent to {ok} host(s).")
    action_reboot_selected.short_description = "Reboot"

    def action_open_services_tab(self, request, queryset):
        """Open Services tab for selected host."""
        host = self._single_selected_host(queryset)
        if host:
            return redirect(f"/admin/manager/host/?tab=services&host={host.pk}#main-tab-bar")
        self.message_user(request, "Select exactly one host to open Services tab.", messages.WARNING)
    action_open_services_tab.short_description = "Services"

    def action_enter_maintenance_mode(self, request, queryset):
        """Enter maintenance mode on selected hosts."""
        ok = 0
        for host in queryset:
            try:
                with get_conn(host.name) as conn:
                    host_manage.set_maintenance_mode(conn, True)
                ok += 1
            except Exception as exc:
                self.message_user(request, f"Maintenance mode failed on {host.name}: {exc}", messages.ERROR)
        self.message_user(request, f"Maintenance mode enabled on {ok} host(s).")
    action_enter_maintenance_mode.short_description = "Enter maintenance mode"

    def action_lockdown_mode(self, request, queryset):
        """Enable ESXi lockdown mode on selected hosts.

        WARNING: This restricts management to DCUI and exception users only.
        Only SSH sessions already open at the time will remain active.
        """
        ok = 0
        for host in queryset:
            try:
                with get_conn(host.name) as conn:
                    host_manage.set_lockdown_mode(conn, True)
                ok += 1
            except Exception as exc:
                self.message_user(
                    request,
                    f"Lockdown failed on {host.name}: {exc}",
                    messages.ERROR,
                )
        if ok:
            self.message_user(
                request,
                f"Lockdown mode enabled on {ok} host(s). "
                "SSH access is now restricted — use DCUI or an exception account to disable.",
                messages.WARNING,
            )
    action_lockdown_mode.short_description = "Lockdown mode"

    def action_permissions(self, request, queryset):
        """Display local user permission assignments for selected host(s)."""
        host = self._single_selected_host(queryset)
        if host is None:
            self.message_user(
                request, "Select exactly one host to view permissions.", messages.WARNING
            )
            return
        try:
            with get_conn(host.name) as conn:
                output = host_manage.get_host_permissions(conn)
            # Trim to a safe displayable length
            display = output[:800] + ("…" if len(output) > 800 else "")
            self.message_user(
                request,
                f"Permissions on {host.name}:\n{display}",
                messages.INFO,
            )
        except Exception as exc:
            self.message_user(request, f"Could not fetch permissions for {host.name}: {exc}", messages.ERROR)
    action_permissions.short_description = "Permissions"

    def action_generate_support_bundle(self, request, queryset):
        """Run vm-support on selected host(s) to generate a diagnostic bundle.

        The bundle is written to /tmp on the ESXi host.
        Ref: https://kb.vmware.com/s/article/2032892
        """
        for host in queryset:
            try:
                with get_conn(host.name) as conn:
                    output = host_manage.generate_support_bundle(conn)
                trimmed = output[:600] + ("…" if len(output) > 600 else "")
                self.message_user(
                    request,
                    f"Support bundle generated on {host.name}: {trimmed}",
                    messages.SUCCESS,
                )
            except Exception as exc:
                self.message_user(
                    request,
                    f"Support bundle failed on {host.name}: {exc}",
                    messages.ERROR,
                )
    action_generate_support_bundle.short_description = "Generate support bundle"

    def action_get_ssh_for_chrome(self, request, queryset):
        """Display SSH connection strings for use with Chrome's Secure Shell extension.

        Connection strings can be pasted directly into the Secure Shell App
        (chrome-extension://iodihamcpbpeioajjeobimgagajmlibd/).
        """
        host = self._single_selected_host(queryset)
        if host is None:
            self.message_user(
                request, "Select exactly one host to get SSH details.", messages.WARNING
            )
            return
        username = host.username or "root"
        ip = str(host.ip_address)
        ssh_string = f"{username}@{ip}"
        self.message_user(
            request,
            format_html(
                "SSH connection for <strong>{}</strong>: "
                "<code style='font-family:monospace;background:#f3f4f6;padding:2px 6px;border-radius:3px;'>{}</code> "
                "— paste into <em>Chrome Secure Shell</em> (connection/host field).",
                host.name,
                ssh_string,
            ),
            messages.INFO,
        )
    action_get_ssh_for_chrome.short_description = "Get SSH for Chrome"

    @action(description="Sync All Hosts", icon="sync")
    def sync_all_hosts_action(self, request):
        for h in Host.objects.all():
            sync_host_details_to_db(h)
        self.message_user(request, "Host metadata updated.")
        return redirect(".")

    @action(description="Reboot Host", icon="restart_alt")
    def reboot_host_single(self, request, object_id=None):
        obj = Host.objects.get(pk=object_id)
        if self._do_reboot(request, obj):
            self.message_user(request, f"Reboot signal sent to {obj.name}")
        return redirect(".")

    @action(description="Shutdown Host", icon="power_settings_new")
    def shutdown_host_single(self, request, object_id=None):
        obj = Host.objects.get(pk=object_id)
        if self._do_shutdown(request, obj):
            self.message_user(request, f"Shutdown signal sent to {obj.name}")
        return redirect(".")

    # --- Private Connection Logic ---
    def _do_reboot(self, request, host):
        try:
            # Host connection uses SSH key auth from environment settings.
            with get_conn(host.name) as conn:
                host_manage.reboot_host(conn)
            return True
        except Exception as e:
            self.message_user(request, f"Error on {host.name}: {e}", messages.ERROR)
            return False

    def _do_shutdown(self, request, host):
        try:
            with get_conn(host.name) as conn:
                host_manage.shutdown_host(conn)
            return True
        except Exception as e:
            self.message_user(request, f"Error on {host.name}: {e}", messages.ERROR)
            return False