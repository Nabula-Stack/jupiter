"""
ESXi vSphere API Client — Plug-and-play VMware vSphere SDK integration.

This module provides a high-level client for interacting with ESXi hosts
via the VMware vSphere API (pyVmomi), supporting:
  - Host operations (power, maintenance, hardware, license)
  - VM operations (inventory, power, snapshots, provisioning)
  - Network operations (vSwitches, portgroups, NICs)
  - Storage operations (datastores, file explorer)

Usage:
    client = EsxiApiClient(host="192.168.1.10", username="root", password="secret")
    with client.connect() as api:
        summary = api.get_host_summary()
        vms = api.list_vms()
        print(summary, vms)

Design: Context manager for automatic cleanup; standardized response schemas;
vendoring-agnostic error handling.
"""

from __future__ import annotations

import ssl
import logging
import posixpath
import tempfile
import time
import urllib.parse
import requests
from typing import Any, Optional, List, Dict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from contextlib import contextmanager

try:
    from pyVmomi import vim, vmodl
    from pyVim.connect import SmartConnect, Disconnect
    PYVMOMI_AVAILABLE = True
except ImportError:
    PYVMOMI_AVAILABLE = False

logger = logging.getLogger(__name__)


# ============================================================================
# Response Schemas (UI-friendly, vendor-agnostic)
# ============================================================================

@dataclass
class HostSummary:
    """Host hardware and software summary."""
    name: str
    ip_address: str
    version: str
    build: str
    vendor: str
    model: str
    cpu_count: int
    memory_gb: float
    processor_type: str
    boot_time: Optional[str] = None
    last_sync: Optional[str] = None


@dataclass
class HostHardware:
    """Detailed host hardware configuration."""
    cpu_count: int
    cpu_mhz: int
    cpu_cores: int
    memory_gb: float
    memory_available_gb: float
    nics: int
    hbas: int
    pci_devices: int


@dataclass
class HostPower:
    """Host power state and operations."""
    state: str  # "on", "off", "standby", "unknown"
    uptime_seconds: int
    last_boot: Optional[str] = None


@dataclass
class VMInfo:
    """Virtual machine information."""
    name: str
    uuid: str
    power_state: str  # "poweredOn", "poweredOff", "suspended"
    cpu_count: int
    memory_mb: int
    tools_running: bool
    tools_status: str
    datastorage: Optional[str] = None
    guest_os: Optional[str] = None
    ip_addresses: List[str] = field(default_factory=list)
    dns_name: Optional[str] = None
    dns_servers: List[str] = field(default_factory=list)


@dataclass
class NetworkSwitch:
    """Virtual network switch."""
    name: str
    portgroup_count: int
    nic_count: int
    mtu: int


@dataclass
class Portgroup:
    """Port group (virtual network)."""
    name: str
    vswitch: str
    vlan_id: int
    active_nics: List[str] = field(default_factory=list)
    standby_nics: List[str] = field(default_factory=list)


@dataclass
class Datastore:
    """Storage datastore."""
    name: str
    capacity_gb: float
    free_gb: float
    provisioned_gb: float
    type: str  # "VMFS", "NFS", "vSAN", etc.


@dataclass
class DatastoreEntry:
    """Datastore browser entry for files/folders."""
    name: str
    is_dir: bool
    size: str
    path: str


# ============================================================================
# ESXi vSphere API Client
# ============================================================================

class EsxiApiClient:
    """
    High-level vSphere API client for ESXi hosts.
    
    Supports both username/password and certificate authentication.
    Automatically handles SSL certificate validation (insecure by default).
    
    Attributes:
        host: ESXi host IP or FQDN
        username: vSphere username (e.g., "root")
        password: vSphere password
        port: vSphere API port (default 443)
        verify_ssl: Whether to verify SSL certificates (default False)
        timeout: Connection timeout in seconds (default 20)
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 443,
        verify_ssl: bool = False,
        timeout: int = 20,
    ):
        if not PYVMOMI_AVAILABLE:
            raise RuntimeError(
                "pyVmomi is not installed. Install with: pip install pyvmomi"
            )

        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.si = None  # ServiceInstance
        self.content = None

    def __enter__(self) -> EsxiApiClient:
        """Context manager entry; establishes connection."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit; closes connection."""
        self.disconnect()

    def _session_cookie_header(self) -> Optional[str]:
        """Return vmware_soap_session cookie header value from pyVmomi stub if available."""
        stub = getattr(self.si, "_stub", None)
        raw_cookie = getattr(stub, "cookie", None)
        if not raw_cookie:
            return None
        return str(raw_cookie).split(";", 1)[0].strip() or None

    @staticmethod
    def _normalize_datastore_path(path: str) -> str:
        """Accept /vmfs/volumes paths and return vSphere [datastore] notation."""
        normalized = posixpath.normpath(str(path or "").strip())
        if not normalized:
            return ""
        if normalized.startswith("["):
            return normalized

        prefix = "/vmfs/volumes/"
        if normalized.startswith(prefix):
            parts = normalized[len(prefix):].split("/", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                ds_name, rel_path = parts
                return f"[{ds_name}] {rel_path}"

        return normalized

    @contextmanager
    def connect(self):
        """
        Establish authenticated connection to the ESXi host.
        
        Returns:
            self (for chaining operations)
            
        Raises:
            ConnectionError: If authentication or connection fails
        """
        # Establish connection: only connection/auth failures are wrapped here.
        try:
            if not self.verify_ssl:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            else:
                context = None

            self.si = SmartConnect(
                host=self.host,
                user=self.username,
                pwd=self.password,
                port=self.port,
                sslContext=context,
            )
            self.content = self.si.RetrieveContent()
            logger.info(f"Connected to ESXi host: {self.host}")
        except vim.fault.InvalidLogin as e:
            raise ConnectionError(f"Authentication failed for {self.username}@{self.host}: {e}")
        except Exception as e:
            raise ConnectionError(f"Connection failed to {self.host}: {e}")

        # Operation errors inside caller code are propagated as-is.
        try:
            yield self
        finally:
            if self.si:
                Disconnect(self.si)
                logger.info(f"Disconnected from {self.host}")
                self.si = None
                self.content = None

    def disconnect(self) -> None:
        """Explicitly disconnect from vSphere (called automatically in context manager)."""
        if self.si:
            try:
                Disconnect(self.si)
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self.si = None
                self.content = None

    # ========================================================================
    # HOST OPERATIONS
    # ========================================================================

    def get_host_summary(self) -> HostSummary:
        """
        Retrieve host summary: name, version, hardware, model.
        
        Returns:
            HostSummary object
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        host = self._get_host_object()
        
        name = host.name
        ip = self._get_host_ipv4(host)
        version = host.config.product.version
        build = host.config.product.build
        vendor = host.hardware.systemInfo.vendor or "Unknown"
        model = host.hardware.systemInfo.model or "Unknown"
        cpu_count = host.hardware.cpuInfo.numCpuCores
        memory_gb = host.hardware.memorySize / (1024**3)
        processor_type = host.hardware.cpuInfo.hz / (10**9)  # Convert Hz to GHz
        
        boot_time = None
        if hasattr(host.runtime, 'bootTime') and host.runtime.bootTime:
            boot_time = host.runtime.bootTime.isoformat()

        return HostSummary(
            name=name,
            ip_address=ip,
            version=version,
            build=build,
            vendor=vendor,
            model=model,
            cpu_count=cpu_count,
            memory_gb=round(memory_gb, 2),
            processor_type=f"{processor_type:.2f} GHz",
            boot_time=boot_time,
            last_sync=datetime.utcnow().isoformat(),
        )

    def get_host_hardware(self) -> HostHardware:
        """
        Retrieve detailed host hardware configuration.
        
        Returns:
            HostHardware object
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        host = self._get_host_object()
        cpuinfo = host.hardware.cpuInfo
        mem_available = host.runtime.healthSystemRuntime.systemHealthInfo.numericSensorInfo
        
        memory_available_mb = 0
        for sensor in mem_available or []:
            if "memory" in sensor.name.lower():
                memory_available_mb += sensor.currentReading / (1024**2)

        nic_count = len(host.config.network.vnic) if host.config.network.vnic else 0
        hba_count = len(host.config.storageDevice.hostBusAdapter) if host.config.storageDevice.hostBusAdapter else 0
        pci_count = len(host.hardware.pciDevice) if host.hardware.pciDevice else 0

        return HostHardware(
            cpu_count=cpuinfo.numCpuPackages,
            cpu_mhz=cpuinfo.hz // (10**6),  # Convert Hz to MHz
            cpu_cores=cpuinfo.numCpuCores,
            memory_gb=round(host.hardware.memorySize / (1024**3), 2),
            memory_available_gb=round(memory_available_mb / 1024, 2),
            nics=nic_count,
            hbas=hba_count,
            pci_devices=pci_count,
        )

    def get_host_power(self) -> HostPower:
        """
        Retrieve host power state and uptime.
        
        Returns:
            HostPower object
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        host = self._get_host_object()
        state_map = {
            "poweredOn": "on",
            "poweredOff": "off",
            "standbyMode": "standby",
        }
        state = state_map.get(host.runtime.powerState, "unknown")
        
        uptime = getattr(host.summary.quickStats, 'uptime', 0) or 0
        boot_time = None
        
        if hasattr(host.runtime, 'bootTime') and host.runtime.bootTime:
            boot_time = host.runtime.bootTime.isoformat()

        return HostPower(
            state=state,
            uptime_seconds=uptime,
            last_boot=boot_time,
        )

    def set_host_maintenance_mode(self, enable: bool) -> Dict[str, Any]:
        """
        Enable or disable host maintenance mode.
        
        Args:
            enable: True to enter maintenance mode, False to exit
            
        Returns:
            Status dict with result
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        host = self._get_host_object()
        try:
            if enable:
                task = host.EnterMaintenanceMode_Task(timeout=300)
                mode_str = "maintenance"
            else:
                task = host.ExitMaintenanceMode_Task(timeout=300)
                mode_str = "operational"
            
            self._wait_for_task(task)
            return {"status": "success", "message": f"Host entered {mode_str} mode"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def reboot_host(self, force: bool = True) -> Dict[str, Any]:
        """Reboot ESXi host via API."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        task = host.RebootHost_Task(force=force)
        self._wait_for_task(task)
        return {"status": "success", "message": "Reboot command sent"}

    def shutdown_host(self, force: bool = True) -> Dict[str, Any]:
        """Shutdown ESXi host via API."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        task = host.ShutdownHost_Task(force=force)
        self._wait_for_task(task)
        return {"status": "success", "message": "Shutdown command sent"}

    def set_lockdown_mode(self, enable: bool) -> Dict[str, Any]:
        """Enable/disable lockdown mode using HostAccessManager."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        access_mgr = host.configManager.hostAccessManager
        mode = "lockdownNormal" if enable else "lockdownDisabled"
        access_mgr.ChangeLockdownMode(mode)
        return {"status": "success", "lockdown_mode": mode}

    def get_lockdown_status(self) -> Dict[str, Any]:
        """Return current lockdown mode for host."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        return {"status": "success", "lockdown_mode": str(getattr(host.config, "lockdownMode", "unknown"))}

    def get_host_permissions(self) -> List[Dict[str, Any]]:
        """Return host permissions via AuthorizationManager."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        auth = self.content.authorizationManager
        host = self._get_host_object()
        roles = {int(role.roleId): role.name for role in (auth.roleList or [])}
        perms = auth.RetrieveEntityPermissions(entity=host, inherited=True) or []

        return [
            {
                "principal": p.principal,
                "is_group": bool(p.group),
                "role": roles.get(int(p.roleId), str(p.roleId)),
                "propagate": bool(p.propagate),
            }
            for p in perms
        ]

    def generate_support_bundle(self) -> Dict[str, Any]:
        """Attempt support bundle generation via diagnostics manager."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        diag = host.configManager.diagnosticSystem
        try:
            task = diag.GenerateLogBundles_Task(includeDefault=True, host=host)
            info = self._wait_for_task(task, timeout=900)
            return {"status": "success", "result": str(getattr(info, "result", ""))}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def list_services(self) -> List[Dict[str, Any]]:
        """List host services and status."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        service_sys = host.configManager.serviceSystem
        service_info = service_sys.serviceInfo.service or []
        rows = []
        for svc in service_info:
            rows.append(
                {
                    "name": str(getattr(svc, "key", "") or ""),
                    "label": str(getattr(svc, "label", "") or ""),
                    "status": "Running" if bool(getattr(svc, "running", False)) else "Stopped",
                    "policy": str(getattr(svc, "policy", "") or ""),
                    "required": bool(getattr(svc, "required", False)),
                }
            )
        return rows

    def control_service(self, service_name: str, action: str) -> Dict[str, Any]:
        """Control host service start/stop/restart."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        service_sys = host.configManager.serviceSystem
        normalized = (action or "").strip().lower()
        if normalized == "start":
            service_sys.StartService(id=service_name)
        elif normalized == "stop":
            service_sys.StopService(id=service_name)
        elif normalized == "restart":
            service_sys.RestartService(id=service_name)
        else:
            raise ValueError(f"Unsupported service action: {action}")
        return {"status": "success", "service": service_name, "action": normalized}

    def set_service_policy(self, service_name: str, enabled: bool) -> Dict[str, Any]:
        """Enable or disable service autostart policy."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        service_sys = host.configManager.serviceSystem
        policy = "on" if enabled else "off"
        service_sys.UpdateServicePolicy(id=service_name, policy=policy)
        return {"status": "success", "service": service_name, "policy": policy}

    def get_service_status(self, service_name: str) -> str:
        """Return service status string for a service key."""
        services = self.list_services()
        for svc in services:
            if svc.get("name") == service_name:
                return str(svc.get("status") or "Unknown")
        return "Unknown"

    # ========================================================================
    # VM OPERATIONS
    # ========================================================================

    def list_vms(self) -> List[VMInfo]:
        """
        List all VMs registered on the ESXi host.

        Uses CreateContainerView with recursive=True so it works on both
        standalone ESXi (rootFolder → ComputeResource → ResourcePool → VM)
        and vCenter (rootFolder → Datacenter → vmFolder → VM).

        Returns:
            List of VMInfo objects
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        view = self.content.viewManager.CreateContainerView(
            self.content.rootFolder,
            [vim.VirtualMachine],
            recursive=True,
        )
        try:
            vms = []
            for vm in view.view:
                try:
                    config = vm.config
                    runtime = vm.runtime
                    guest = vm.guest

                    vm_info = VMInfo(
                        name=config.name if config else "Unknown",
                        uuid=config.uuid if config else "",
                        power_state=str(runtime.powerState) if runtime else "unknown",
                        cpu_count=config.hardware.numCPU if config and config.hardware else 0,
                        memory_mb=config.hardware.memoryMB if config and config.hardware else 0,
                        tools_running=(
                            (guest.toolsRunningStatus == "guestToolsRunning") if guest else False
                        ),
                        tools_status=str(guest.toolsStatus) if guest and guest.toolsStatus else "toolsNotInstalled",
                        datastorage=config.files.vmPathName if config and config.files else "",
                        guest_os=config.guestFullName if config else "",
                        dns_name=guest.hostName if guest else "",
                    )

                    # Extract IP addresses
                    if guest and guest.net:
                        for net in guest.net:
                            if hasattr(net, "ipConfig") and net.ipConfig:
                                for addr in net.ipConfig.ipAddress:
                                    ip = addr.ipAddress
                                    if ip and not ip.startswith("fe80"):
                                        vm_info.ip_addresses.append(ip)

                    # Extract guest DNS servers from ipStack if VMware Tools reports them.
                    if guest and getattr(guest, "ipStack", None):
                        for stack in guest.ipStack or []:
                            dns_cfg = getattr(stack, "dnsConfig", None)
                            if not dns_cfg:
                                continue
                            for dns_ip in (getattr(dns_cfg, "ipAddress", None) or []):
                                if dns_ip and dns_ip not in vm_info.dns_servers:
                                    vm_info.dns_servers.append(str(dns_ip))

                    vms.append(vm_info)
                except Exception:
                    continue
        finally:
            view.Destroy()

        return vms

    def get_vm(self, vm_name: str) -> Optional[VMInfo]:
        """
        Get specific VM by name.
        
        Args:
            vm_name: Name of the VM
            
        Returns:
            VMInfo object or None if not found
        """
        vms = self.list_vms()
        for vm in vms:
            if vm.name == vm_name:
                return vm
        return None

    def get_vm_by_identifier(self, vm_identifier: str) -> Optional[VMInfo]:
        """Get VM by UUID first, then by name."""
        for vm in self.list_vms():
            if vm.uuid == vm_identifier or vm.name == vm_identifier:
                return vm
        return None

    def set_vm_power_state(self, vm_name: str, power_on: bool) -> Dict[str, Any]:
        """
        Power VM on or off.
        
        Args:
            vm_name: Name of the VM
            power_on: True to power on, False to power off
            
        Returns:
            Status dict with result
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        try:
            vm = self._find_vm(vm_name)
            if not vm:
                return {"status": "error", "message": f"VM {vm_name} not found"}

            if power_on:
                task = vm.PowerOnVM_Task()
                action = "powered on"
            else:
                task = vm.PowerOffVM_Task()
                action = "powered off"

            self._wait_for_task(task)
            return {"status": "success", "message": f"VM {action}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def set_vm_power_state_by_identifier(self, vm_identifier: str, state: str) -> Dict[str, Any]:
        """Set VM power state by UUID or name, accepts vim-cmd style power verbs."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        vm = self._find_vm_by_identifier(vm_identifier)
        if not vm:
            return {"status": "error", "message": f"VM not found: {vm_identifier}"}

        verb = (state or "").strip().lower()
        if verb in {"power.on", "on", "poweron"}:
            task = vm.PowerOnVM_Task()
        elif verb in {"power.off", "off", "poweroff"}:
            task = vm.PowerOffVM_Task()
        elif verb in {"power.shutdown", "shutdown"}:
            task = vm.ShutdownGuest()
            return {"status": "success", "message": "Guest shutdown requested"}
        elif verb in {"power.reset", "reset", "reboot", "power.reboot"}:
            task = vm.ResetVM_Task()
        elif verb in {"power.suspend", "suspend"}:
            task = vm.SuspendVM_Task()
        else:
            return {"status": "error", "message": f"Unsupported power verb: {state}"}

        self._wait_for_task(task)
        return {"status": "success", "message": f"Power action applied: {verb}"}

    def vm_snapshot_action(self, vm_identifier: str, op: str, name: Optional[str] = None, description: str = "Admin Snapshot") -> Dict[str, Any]:
        """Snapshot operations by UUID/name: create/list/removeall/revert."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        vm = self._find_vm_by_identifier(vm_identifier)
        if not vm:
            return {"status": "error", "message": f"VM not found: {vm_identifier}"}

        action = (op or "").strip().lower()
        if action == "create":
            snap_name = name or f"Snap-{datetime.utcnow().strftime('%m%d-%H%M')}"
            task = vm.CreateSnapshot_Task(name=snap_name, description=description, memory=False, quiesce=False)
            self._wait_for_task(task)
            return {"status": "success", "message": f"Snapshot created: {snap_name}"}
        if action == "removeall":
            task = vm.RemoveAllSnapshots_Task()
            self._wait_for_task(task)
            return {"status": "success", "message": "All snapshots removed"}
        if action == "list":
            return {"status": "success", "snapshots": self._collect_vm_snapshots(vm)}
        if action == "revert":
            snaps = self._collect_vm_snapshots(vm)
            if not snaps:
                return {"status": "error", "message": "No snapshot available to revert"}
            target = self._find_snapshot_ref(vm.snapshot.rootSnapshotList, name) if name else vm.snapshot.currentSnapshot
            if not target:
                return {"status": "error", "message": f"Snapshot not found: {name}"}
            task = target.RevertToSnapshot_Task()
            self._wait_for_task(task)
            return {"status": "success", "message": "Snapshot reverted"}
        return {"status": "error", "message": f"Unsupported snapshot op: {op}"}

    def get_vm_webmks_ticket(self, vm_identifier: str) -> Dict[str, Any]:
        """Get WebMKS console ticket for browser-based console access."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        
        vm = self._find_vm_by_identifier(vm_identifier)
        if not vm:
            return {"status": "error", "message": f"VM not found: {vm_identifier}"}

        vm_name = ""
        console_id = str(vm_identifier)
        try:
            if vm.config and vm.config.name:
                vm_name = str(vm.config.name)
            console_id = str(getattr(vm, "_moId", "") or vm_identifier)
        except Exception:
            vm_name = str(vm_identifier)
        
        try:
            # Acquire WebMKS ticket for console access
            session_manager = self.content.sessionManager
            ticket = session_manager.AcquireGenericServiceTicket(
                spec=vim.SessionManager.GenericServiceTicket.TicketSpec(
                    host=self.host,
                    protocol="webmks"
                )
            )
            
            # WebMKS requires the ticket ID and VM reference
            return {
                "status": "success",
                "ticket": ticket.id,
                "host": self.host,
                "port": 443,
                "vmid": str(vm_identifier),
                "console_id": console_id,
                "vm_name": vm_name or str(vm_identifier),
            }
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Failed to get WebMKS ticket: {str(exc)}",
                "console_id": console_id,
                "vm_name": vm_name or str(vm_identifier),
            }

    def unregister_vm_by_identifier(self, vm_identifier: str) -> Dict[str, Any]:
        """Unregister VM from inventory by UUID/name."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        vm = self._find_vm_by_identifier(vm_identifier)
        if not vm:
            return {"status": "error", "message": f"VM not found: {vm_identifier}"}
        vm.UnregisterVM()
        return {"status": "success", "message": f"VM unregistered: {vm_identifier}"}

    def destroy_vm(self, vm_identifier: str) -> Dict[str, Any]:
        """Permanently destroy VM from inventory and datastore."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        vm = self._find_vm_by_identifier(vm_identifier)
        if not vm:
            return {"status": "error", "message": f"VM not found: {vm_identifier}"}

        try:
            runtime = getattr(vm, "runtime", None)
            power_state = str(getattr(runtime, "powerState", "") or "")
            if power_state.lower() == "poweredon":
                power_off_task = vm.PowerOffVM_Task()
                self._wait_for_task(power_off_task)

            destroy_task = vm.Destroy_Task()
            self._wait_for_task(destroy_task)
            return {"status": "success", "message": f"VM permanently destroyed: {vm_identifier}"}
        except Exception as exc:
            return {"status": "error", "message": f"Failed to permanently destroy VM: {str(exc)}"}

    def register_vm_from_path(self, vmx_path: str) -> Dict[str, Any]:
        """Register a VMX path to host inventory. Accepts /vmfs/volumes or [datastore] format."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        vmx_path = self._normalize_datastore_path(vmx_path)
        
        host = self._get_host_object()
        folder = host.parent.vmFolder if hasattr(host.parent, "vmFolder") else self._get_datacenter_object().vmFolder
        pool = None
        if hasattr(host.parent, "resourcePool"):
            pool = host.parent.resourcePool
        task = folder.RegisterVM_Task(path=vmx_path, asTemplate=False, host=host, pool=pool)
        self._wait_for_task(task)
        return {"status": "success", "message": f"VM registered: {vmx_path}"}

    # ========================================================================
    # NETWORK OPERATIONS
    # ========================================================================

    def list_vswitches(self) -> List[NetworkSwitch]:
        """
        List all virtual switches on the ESXi host.
        
        Returns:
            List of NetworkSwitch objects
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        host = self._get_host_object()
        switches = []

        for vswitch in host.config.network.vswitch or []:
            try:
                bridge = vswitch.bridge
                nic_count = (
                    len(bridge.nicTeamingPolicy.nicOrder.activeNic)
                    if bridge is not None and hasattr(bridge, 'nicTeamingPolicy')
                    and bridge.nicTeamingPolicy is not None
                    and hasattr(bridge.nicTeamingPolicy, 'nicOrder')
                    and bridge.nicTeamingPolicy.nicOrder is not None
                    else 0
                )
            except Exception:
                nic_count = 0
            sw = NetworkSwitch(
                name=vswitch.name,
                portgroup_count=len(vswitch.portgroup) if vswitch.portgroup else 0,
                nic_count=nic_count,
                mtu=vswitch.mtu or 1500,
            )
            switches.append(sw)

        return switches

    def list_portgroups(self) -> List[Portgroup]:
        """
        List all port groups (virtual networks) on the ESXi host.
        
        Returns:
            List of Portgroup objects
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        host = self._get_host_object()
        portgroups = []

        for pg in host.config.network.portgroup or []:
            try:
                nt = pg.spec.policy.nicTeaming if hasattr(pg.spec.policy, 'nicTeaming') else None
                no = nt.nicOrder if nt is not None and hasattr(nt, 'nicOrder') else None
                active_nics = list(no.activeNic) if no is not None and no.activeNic else []
                standby_nics = list(no.standbyNic) if no is not None and no.standbyNic else []
            except Exception:
                active_nics = []
                standby_nics = []
            pgroup = Portgroup(
                name=pg.spec.name,
                vswitch=pg.spec.vswitchName,
                vlan_id=pg.spec.vlanId or 0,
                active_nics=active_nics,
                standby_nics=standby_nics,
            )
            portgroups.append(pgroup)

        return portgroups

    def create_vswitch(self, name: str, num_ports: int = 128, mtu: int = 1500) -> Dict[str, Any]:
        """Create standard vSwitch."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        net_sys = host.configManager.networkSystem
        spec = vim.host.VirtualSwitch.Specification(numPorts=num_ports, mtu=mtu)
        net_sys.AddVirtualSwitch(vswitchName=name, spec=spec)
        return {"status": "success", "vswitch": name}

    def remove_vswitch(self, name: str) -> Dict[str, Any]:
        """Remove standard vSwitch."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        net_sys = host.configManager.networkSystem
        net_sys.RemoveVirtualSwitch(vswitchName=name)
        return {"status": "success", "vswitch": name}

    def add_portgroup(self, vswitch: str, name: str, vlan: int = 0) -> Dict[str, Any]:
        """Create portgroup on standard vSwitch."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        net_sys = host.configManager.networkSystem
        pg_spec = vim.host.PortGroup.Specification(name=name, vswitchName=vswitch, vlanId=int(vlan or 0))
        net_sys.AddPortGroup(portgrp=pg_spec)
        return {"status": "success", "portgroup": name, "vswitch": vswitch, "vlan": int(vlan or 0)}

    def remove_portgroup(self, name: str) -> Dict[str, Any]:
        """Remove portgroup by name."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        net_sys = host.configManager.networkSystem
        net_sys.RemovePortGroup(pgName=name)
        return {"status": "success", "portgroup": name}

    def set_portgroup_vlan(self, name: str, vlan: int) -> Dict[str, Any]:
        """Update VLAN for existing portgroup."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        net_sys = host.configManager.networkSystem
        existing = None
        for pg in host.config.network.portgroup or []:
            if getattr(pg.spec, "name", "") == name:
                existing = pg.spec
                break
        if existing is None:
            raise ValueError(f"Portgroup not found: {name}")
        new_spec = vim.host.PortGroup.Specification(
            name=existing.name,
            vswitchName=existing.vswitchName,
            vlanId=int(vlan),
            policy=existing.policy,
        )
        net_sys.UpdatePortGroup(pgName=name, portgrp=new_spec)
        return {"status": "success", "portgroup": name, "vlan": int(vlan)}

    def list_physical_nics(self) -> List[Dict[str, Any]]:
        """List host physical NIC metadata."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        rows = []
        for nic in host.config.network.pnic or []:
            rows.append(
                {
                    "interface": str(getattr(nic, "device", "") or ""),
                    "driver": str(getattr(nic, "driver", "") or "--"),
                    "admin_status": "up" if bool(getattr(nic, "linkSpeed", None)) else "down",
                    "link_status": "up" if bool(getattr(nic, "linkSpeed", None)) else "down",
                    "speed": str(getattr(getattr(nic, "linkSpeed", None), "speedMb", "--") or "--"),
                    "duplex": str(getattr(getattr(nic, "linkSpeed", None), "duplex", "--") or "--"),
                    "mac": str(getattr(nic, "mac", "") or "--"),
                    "mtu": str(getattr(nic, "mtu", "") or "--"),
                    "description": str(getattr(nic, "pci", "") or "--"),
                }
            )
        return rows

    def list_vmkernel_nics(self) -> List[Dict[str, Any]]:
        """List VMkernel interfaces."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        rows = []
        for nic in host.config.network.vnic or []:
            ip_spec = getattr(getattr(nic, "spec", None), "ip", None)
            rows.append(
                {
                    "interface": str(getattr(nic, "device", "") or "--"),
                    "ip": str(getattr(ip_spec, "ipAddress", "--") or "--"),
                    "netmask": str(getattr(ip_spec, "subnetMask", "--") or "--"),
                    "type": "static" if bool(getattr(ip_spec, "dhcp", False)) is False else "dhcp",
                    "mtu": str(getattr(getattr(nic, "spec", None), "mtu", "--") or "--"),
                    "enabled": "true",
                }
            )
        return rows

    def list_tcp_ip_stacks(self) -> List[Dict[str, Any]]:
        """List basic TCP/IP stack data."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        return [{"name": "defaultTcpipStack", "enabled": "true", "ccalgo": "--"}]

    def list_firewall_rules(self) -> List[Dict[str, Any]]:
        """List firewall rulesets from firewall system."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        fw = host.configManager.firewallSystem
        rulesets = getattr(getattr(fw, "firewallInfo", None), "ruleset", []) or []
        rows = []
        for rs in rulesets:
            rows.append(
                {
                    "name": str(getattr(rs, "key", "") or "--"),
                    "enabled": "true" if bool(getattr(rs, "enabled", False)) else "false",
                    "allow_incoming": "--",
                    "allow_outgoing": "--",
                    "required": "true" if bool(getattr(rs, "required", False)) else "false",
                }
            )
        return rows

    # ========================================================================
    # STORAGE OPERATIONS
    # ========================================================================

    def list_datastores(self) -> List[Datastore]:
        """
        List all datastores accessible to the ESXi host.
        
        Returns:
            List of Datastore objects
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        host = self._get_host_object()
        datastores = []

        for ds_ref in host.datastore or []:
            try:
                ds = ds_ref
                capacity = float(getattr(getattr(ds, "summary", None), "capacity", 0) or 0)
                free = float(getattr(getattr(ds, "summary", None), "freeSpace", 0) or 0)
                uncommitted = float(getattr(getattr(ds, "summary", None), "uncommitted", 0) or 0)
                
                datastore = Datastore(
                    name=ds.name or "Unknown",
                    capacity_gb=round(capacity / (1024**3), 2) if capacity > 0 else 0,
                    free_gb=round(free / (1024**3), 2) if free > 0 else 0,
                    provisioned_gb=round(uncommitted / (1024**3), 2) if uncommitted > 0 else 0,
                    type=getattr(getattr(ds, "summary", None), "type", "Unknown") or "Unknown",
                )
                datastores.append(datastore)
            except Exception as exc:
                # Skip datastores that can't be accessed
                continue

        return datastores

    def list_available_disks(self) -> List[Dict[str, Any]]:
        """List storage devices visible to ESXi host."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        devices = getattr(getattr(host.config, "storageDevice", None), "scsiLun", []) or []
        rows = []
        for dev in devices:
            rows.append(
                {
                    "device": str(getattr(dev, "canonicalName", "") or ""),
                    "model": str(getattr(dev, "model", "") or ""),
                    "vendor": str(getattr(dev, "vendor", "") or ""),
                    "lun_type": str(getattr(dev, "lunType", "") or ""),
                    "uuid": str(getattr(dev, "uuid", "") or ""),
                }
            )
        return rows

    def rescan_storage(self) -> Dict[str, Any]:
        """Rescan HBAs and VMFS volumes."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        storage_sys = host.configManager.storageSystem
        storage_sys.RescanAllHba()
        storage_sys.RescanVmfs()
        return {"status": "success", "message": "Storage rescan completed"}

    def unmount_datastore(self, ds_name: str) -> Dict[str, Any]:
        """Unmount VMFS datastore by datastore name."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        storage_sys = host.configManager.storageSystem
        target_uuid = None
        for ds in host.datastore or []:
            if getattr(ds, "name", "") != ds_name:
                continue
            vmfs = getattr(getattr(ds, "info", None), "vmfs", None)
            target_uuid = getattr(vmfs, "uuid", None)
            if target_uuid:
                break
        if not target_uuid:
            raise ValueError(f"VMFS datastore not found: {ds_name}")
        storage_sys.UnmountVmfsVolume(uuid=target_uuid)
        return {"status": "success", "datastore": ds_name}

    def list_datastore_directory(self, vmfs_path: str) -> List[DatastoreEntry]:
        """List datastore directory entries for a /vmfs/volumes path."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        normalized = posixpath.normpath(vmfs_path or "/vmfs/volumes")
        if not normalized.startswith("/vmfs/volumes"):
            raise ValueError("Path must be under /vmfs/volumes")

        if normalized == "/vmfs/volumes":
            entries: List[DatastoreEntry] = []
            for ds in self.list_datastores():
                entries.append(
                    DatastoreEntry(
                        name=ds.name,
                        is_dir=True,
                        size="",
                        path=f"/vmfs/volumes/{ds.name}",
                    )
                )
            return sorted(entries, key=lambda item: item.name.lower())

        datastore_name, rel_path, datastore_path = self._vmfs_to_datastore_path(normalized)
        browse_target = datastore_path if datastore_path.endswith("/") else f"{datastore_path}/"

        host = self._get_host_object()
        browser = host.datastoreBrowser
        details = vim.host.DatastoreBrowser.FileInfo.Details()
        details.fileType = True
        details.fileSize = True
        details.modification = True
        details.fileOwner = False
        search_spec = vim.HostDatastoreBrowserSearchSpec()
        search_spec.details = details
        search_spec.matchPattern = ["*"]  # Match all files
        task = browser.SearchDatastore_Task(datastorePath=browse_target, searchSpec=search_spec)
        try:
            info = self._wait_for_task(task)
        except RuntimeError as exc:
            # Datastore search failed; return empty list
            return sorted([], key=lambda item: (not item.is_dir, item.name.lower()))

        files = []
        if info and getattr(info, "result", None) and getattr(info.result, "file", None):
            files = info.result.file

        entries = []
        for file_info in files:
            raw_name = str(getattr(file_info, "path", "") or "").strip()
            if not raw_name:
                continue

            # Check if it's a folder by checking the class type name
            file_type_name = type(file_info).__name__
            is_dir = "FolderInfo" in file_type_name
            clean_name = raw_name.rstrip("/")
            full_rel = clean_name if not rel_path else f"{rel_path.rstrip('/')}/{clean_name}"
            full_vmfs_path = f"/vmfs/volumes/{datastore_name}/{full_rel}".rstrip("/")
            size_value = "" if is_dir else str(int(getattr(file_info, "fileSize", 0) or 0))

            entries.append(
                DatastoreEntry(
                    name=clean_name,
                    is_dir=is_dir,
                    size=size_value,
                    path=full_vmfs_path,
                )
            )

        return sorted(entries, key=lambda item: (not item.is_dir, item.name.lower()))

    def datastore_path_exists(self, vmfs_path: str) -> bool:
        """Check whether a datastore file/folder exists for /vmfs/volumes path."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        normalized = posixpath.normpath(vmfs_path or "/vmfs/volumes")
        if not normalized.startswith("/vmfs/volumes"):
            return False
        if normalized == "/vmfs/volumes":
            return True

        try:
            datastore_name, rel_path, _ = self._vmfs_to_datastore_path(normalized)
        except ValueError:
            return False

        if not rel_path:
            return any(ds.name == datastore_name for ds in self.list_datastores())

        parent_rel = posixpath.dirname(rel_path)
        target_name = posixpath.basename(rel_path).rstrip("/")
        parent_ds_path = f"[{datastore_name}]" if parent_rel in ("", ".") else f"[{datastore_name}] {parent_rel}"
        if not parent_ds_path.endswith("/"):
            parent_ds_path += "/"

        try:
            host = self._get_host_object()
            browser = host.datastoreBrowser
            search_spec = vim.HostDatastoreBrowserSearchSpec()
            task = browser.SearchDatastore_Task(datastorePath=parent_ds_path, searchSpec=search_spec)
            info = self._wait_for_task(task)
            files = []
            if getattr(info, "result", None) and getattr(info.result, "file", None):
                files = info.result.file
            for file_info in files:
                name = str(getattr(file_info, "path", "") or "").rstrip("/")
                if name == target_name:
                    return True
            return False
        except Exception:
            return False

    def make_datastore_directory(self, vmfs_path: str, create_parents: bool = True) -> Dict[str, Any]:
        """Create a datastore directory using vSphere FileManager."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        _, _, datastore_path = self._vmfs_to_datastore_path(vmfs_path)
        file_manager = self.content.fileManager
        datacenter = self._get_datacenter_object()
        try:
            file_manager.MakeDirectory(
                name=datastore_path,
                datacenter=datacenter,
                createParentDirectories=create_parents,
            )
            return {"status": "success", "path": vmfs_path}
        except vim.fault.FileAlreadyExists:
            return {"status": "exists", "path": vmfs_path}

    def delete_datastore_path(self, vmfs_path: str) -> Dict[str, Any]:
        """Delete a datastore file/folder using vSphere FileManager."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        _, _, datastore_path = self._vmfs_to_datastore_path(vmfs_path)
        task = self.content.fileManager.DeleteDatastoreFile_Task(
            name=datastore_path,
            datacenter=self._get_datacenter_object(),
        )
        self._wait_for_task(task)
        return {"status": "success", "path": vmfs_path}

    def move_datastore_path(self, src_vmfs_path: str, dest_vmfs_path: str, force: bool = True) -> Dict[str, Any]:
        """Move/rename datastore file/folder using vSphere FileManager."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        _, _, src_path = self._vmfs_to_datastore_path(src_vmfs_path)
        _, _, dest_path = self._vmfs_to_datastore_path(dest_vmfs_path)
        task = self.content.fileManager.MoveDatastoreFile_Task(
            sourceName=src_path,
            sourceDatacenter=self._get_datacenter_object(),
            destinationName=dest_path,
            destinationDatacenter=self._get_datacenter_object(),
            force=force,
        )
        self._wait_for_task(task)
        return {"status": "success", "src": src_vmfs_path, "dest": dest_vmfs_path}

    def copy_datastore_path(self, src_vmfs_path: str, dest_vmfs_path: str, force: bool = True) -> Dict[str, Any]:
        """Copy datastore file/folder using vSphere FileManager."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        _, _, src_path = self._vmfs_to_datastore_path(src_vmfs_path)
        _, _, dest_path = self._vmfs_to_datastore_path(dest_vmfs_path)
        task = self.content.fileManager.CopyDatastoreFile_Task(
            sourceName=src_path,
            sourceDatacenter=self._get_datacenter_object(),
            destinationName=dest_path,
            destinationDatacenter=self._get_datacenter_object(),
            force=force,
        )
        self._wait_for_task(task)
        return {"status": "success", "src": src_vmfs_path, "dest": dest_vmfs_path}

    def list_pci_devices(self) -> List[Dict[str, Any]]:
        """List host PCI devices for passthrough selection."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        devices = []
        for pci in host.hardware.pciDevice or []:
            bus = str(getattr(pci, "bus", "")).strip()
            slot = str(getattr(pci, "slot", "")).strip()
            function = str(getattr(pci, "function", "")).strip()
            pci_id = f"{bus}:{slot}.{function}" if bus and slot and function else str(getattr(pci, "id", "") or "")
            vendor_name = str(getattr(pci, "vendorName", "") or "").strip()
            device_name = str(getattr(pci, "deviceName", "") or "").strip()
            devices.append({
                "id": pci_id,
                "label": f"{pci_id} - {vendor_name} {device_name}".strip(),
                "vendor": vendor_name,
                "device": device_name,
            })
        return devices

    def list_files_by_suffix(self, suffix: str) -> List[str]:
        """Recursively search datastores and return /vmfs/volumes paths ending with suffix."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        suffix_norm = str(suffix or "").lower()
        if not suffix_norm.startswith("."):
            suffix_norm = f".{suffix_norm}"

        host = self._get_host_object()
        browser = host.datastoreBrowser
        details = vim.host.DatastoreBrowser.FileInfo.Details()
        details.fileType = True
        details.fileSize = False
        details.modification = False
        details.fileOwner = False
        search_spec = vim.HostDatastoreBrowserSearchSpec()
        search_spec.details = details

        results: List[str] = []
        seen = set()
        for ds in self.list_datastores():
            ds_name = ds.name
            root = f"[{ds_name}]"
            ds_prefix = f"[{ds_name}]"
            try:
                task = browser.SearchDatastoreSubFolders_Task(datastorePath=root, searchSpec=search_spec)
                info = self._wait_for_task(task, timeout=900)
            except Exception:
                continue
            folders = getattr(info, "result", None) or []
            for folder in folders:
                folder_path = str(getattr(folder, "folderPath", "") or "")
                # folderPath format: "[dsName] rel/path/" — dsName may contain spaces
                if folder_path.startswith(ds_prefix):
                    folder_rel = folder_path[len(ds_prefix):].strip().rstrip("/")
                else:
                    folder_rel = ""
                for file_info in (getattr(folder, "file", None) or []):
                    name = getattr(file_info, "path", None)
                    if not isinstance(name, str) or not name:
                        continue
                    if not name.lower().endswith(suffix_norm):
                        continue
                    rel = name if not folder_rel else f"{folder_rel}/{name}"
                    vmfs_path = f"/vmfs/volumes/{ds_name}/{rel}".replace("//", "/")
                    if vmfs_path not in seen:
                        seen.add(vmfs_path)
                        results.append(vmfs_path)
        return sorted(results)

    def list_files_by_suffix_under(self, base_vmfs_path: str, suffix: str) -> List[str]:
        """Recursively search only under base_vmfs_path for files ending with suffix."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        base_norm = posixpath.normpath(base_vmfs_path or "/vmfs/volumes")
        if not base_norm.startswith("/vmfs/volumes"):
            raise ValueError("Path must be under /vmfs/volumes")
        if base_norm == "/vmfs/volumes":
            return self.list_files_by_suffix(suffix)

        suffix_norm = str(suffix or "").lower()
        if not suffix_norm.startswith("."):
            suffix_norm = f".{suffix_norm}"

        datastore_name, rel_path, datastore_path = self._vmfs_to_datastore_path(base_norm)
        browse_target = datastore_path if datastore_path.endswith("/") else f"{datastore_path}/"

        host = self._get_host_object()
        browser = host.datastoreBrowser
        details = vim.host.DatastoreBrowser.FileInfo.Details()
        details.fileType = True
        details.fileSize = False
        details.modification = False
        details.fileOwner = False
        search_spec = vim.HostDatastoreBrowserSearchSpec()
        search_spec.details = details

        task = browser.SearchDatastoreSubFolders_Task(datastorePath=browse_target, searchSpec=search_spec)
        info = self._wait_for_task(task, timeout=900)
        folders = getattr(info, "result", None) or []

        ds_prefix = f"[{datastore_name}]"
        results: List[str] = []
        seen = set()
        for folder in folders:
            folder_path = str(getattr(folder, "folderPath", "") or "")
            if folder_path.startswith(ds_prefix):
                folder_rel = folder_path[len(ds_prefix):].strip().rstrip("/")
            else:
                folder_rel = ""
            for file_info in (getattr(folder, "file", None) or []):
                name = getattr(file_info, "path", None)
                if not isinstance(name, str) or not name:
                    continue
                if not name.lower().endswith(suffix_norm):
                    continue
                rel = name if not folder_rel else f"{folder_rel}/{name}"
                vmfs_path = f"/vmfs/volumes/{datastore_name}/{rel}".replace("//", "/")
                if vmfs_path not in seen:
                    seen.add(vmfs_path)
                    results.append(vmfs_path)

        return sorted(results)

    def list_iso_files(self) -> List[str]:
        return self.list_files_by_suffix(".iso")

    def list_vmx_files(self, base_path: str = "/vmfs/volumes") -> List[str]:
        vmx_files = self.list_files_by_suffix(".vmx")
        base_norm = posixpath.normpath(base_path or "/vmfs/volumes")
        return [path for path in vmx_files if path.startswith(base_norm)]

    def upload_datastore_file(self, target_dir_vmfs_path: str, filename: str, file_obj: Any) -> Dict[str, Any]:
        """Upload a file to a datastore path using ESXi HTTP NFC lease/ticket auth."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        normalized_dir = posixpath.normpath(target_dir_vmfs_path or "/vmfs/volumes")
        if not normalized_dir.startswith("/vmfs/volumes"):
            raise ValueError("Upload path must be under /vmfs/volumes")
        safe_filename = posixpath.basename(filename or "upload")
        if not safe_filename or safe_filename in (".", ".."):
            raise ValueError("Invalid upload filename")

        datastore_name, rel_dir, _ = self._vmfs_to_datastore_path(normalized_dir)

        # Build datastore browser URL for direct file upload.
        # Example: https://host/folder/<path>/<file>?dcPath=ha-datacenter&dsName=<datastore>
        rel_parts = [p for p in rel_dir.split("/") if p] if rel_dir else []
        url_rel_path = "/".join(
            urllib.parse.quote(p, safe="") for p in rel_parts + [safe_filename]
        )
        upload_url = (
            f"https://{self.host}/folder/{url_rel_path}"
            f"?dcPath=ha-datacenter&dsName={urllib.parse.quote(datastore_name, safe='')}"
        )

        verify_ssl = self.verify_ssl
        request_timeout = max(60, self.timeout * 6)

        file_size = None
        if hasattr(file_obj, "tell") and hasattr(file_obj, "seek"):
            try:
                cur = file_obj.tell()
                file_obj.seek(0, os.SEEK_END)
                end = file_obj.tell()
                file_obj.seek(cur, os.SEEK_SET)
                file_size = max(0, int(end - cur))
            except Exception:
                file_size = None

        def _attempt_put(req_headers: Dict[str, str], auth=None):
            last_exc = None
            for attempt in range(3):
                try:
                    if hasattr(file_obj, "seek"):
                        file_obj.seek(0)
                    headers = dict(req_headers)
                    headers.setdefault("Connection", "close")
                    if file_size is not None:
                        headers["Content-Length"] = str(file_size)
                    return requests.put(
                        upload_url,
                        data=file_obj,
                        headers=headers,
                        auth=auth,
                        timeout=request_timeout,
                        verify=verify_ssl,
                    )
                except requests.exceptions.SSLError as exc:
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(1 + attempt)
                        continue
                except requests.exceptions.RequestException as exc:
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(1 + attempt)
                        continue
            raise RuntimeError(f"Upload transport failed: {last_exc}")

        # Try auth methods in order, with fresh ticket per ticket attempt.
        response = None
        errors = []

        try:
            session_manager = self.content.sessionManager
            ticket = session_manager.AcquireGenericServiceTicket(
                spec=vim.SessionManager.HttpServiceRequestSpec(
                    method="PUT",
                    url=upload_url,
                )
            )
            response = _attempt_put(
                {
                    "Content-Type": "application/octet-stream",
                    "Authorization": f"vmware_cgi_ticket {ticket.id}",
                }
            )
        except Exception as exc:
            errors.append(str(exc))

        if response is None or response.status_code == 401:
            cookie_header = self._session_cookie_header()
            if cookie_header:
                try:
                    response = _attempt_put(
                        {
                            "Content-Type": "application/octet-stream",
                            "Cookie": cookie_header,
                        }
                    )
                except Exception as exc:
                    errors.append(str(exc))

        if response is None or response.status_code == 401:
            try:
                response = _attempt_put(
                    {"Content-Type": "application/octet-stream"},
                    auth=(self.username, self.password),
                )
            except Exception as exc:
                errors.append(str(exc))
                raise RuntimeError("; ".join(errors[-3:]) or str(exc))

        if response.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Upload failed ({response.status_code}): {response.text[:500]}"
            )

        uploaded_path = f"{normalized_dir.rstrip('/')}/{safe_filename}"
        return {"status": "success", "path": uploaded_path, "filename": safe_filename}

    def read_datastore_file_content(self, vmfs_path: str, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        """Download file bytes from a /vmfs/volumes path using an ESXi session ticket."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        normalized = posixpath.normpath(vmfs_path or "")
        if not normalized.startswith("/vmfs/volumes"):
            raise ValueError("Path must be under /vmfs/volumes")

        datastore_name, rel_path, _ = self._vmfs_to_datastore_path(normalized)
        if not rel_path:
            raise ValueError("vmfs_path must point to a file, not a datastore root")

        rel_parts = [p for p in rel_path.split("/") if p]
        url_path = "/".join(urllib.parse.quote(p, safe="") for p in rel_parts)
        download_url = (
            f"https://{self.host}/folder/{url_path}"
            f"?dcPath=ha-datacenter&dsName={urllib.parse.quote(datastore_name, safe='')}"
        )

        ticket = self.content.sessionManager.AcquireGenericServiceTicket(
            spec=vim.SessionManager.HttpServiceRequestSpec(method="GET", url=download_url)
        )
        headers = {"Authorization": f"vmware_cgi_ticket {ticket.id}"}

        response = requests.get(
            download_url,
            headers=headers,
            timeout=max(60, self.timeout * 3),
            verify=self.verify_ssl,
            stream=True,
        )

        # Some hosts reject ticket header for /folder. Fallback to pyVmomi session cookie.
        if response.status_code == 401:
            response.close()
            cookie_header = self._session_cookie_header()
            if cookie_header:
                response = requests.get(
                    download_url,
                    headers={"Cookie": cookie_header},
                    timeout=max(60, self.timeout * 3),
                    verify=self.verify_ssl,
                    stream=True,
                )

        # Last resort: basic auth.
        if response.status_code == 401:
            response.close()
            response = requests.get(
                download_url,
                auth=(self.username, self.password),
                timeout=max(60, self.timeout * 3),
                verify=self.verify_ssl,
                stream=True,
            )

        response.raise_for_status()

        content = b""
        for chunk in response.iter_content(chunk_size=65536):
            content += chunk
            if len(content) >= max_bytes:
                break
        return content

    def download_datastore_file_to_local(self, vmfs_path: str, local_path: Optional[str] = None) -> str:
        """Stream a datastore file to local disk and return local file path."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        normalized = posixpath.normpath(vmfs_path or "")
        if not normalized.startswith("/vmfs/volumes"):
            raise ValueError("Path must be under /vmfs/volumes")

        datastore_name, rel_path, _ = self._vmfs_to_datastore_path(normalized)
        if not rel_path:
            raise ValueError("vmfs_path must point to a file, not a datastore root")

        rel_parts = [p for p in rel_path.split("/") if p]
        url_path = "/".join(urllib.parse.quote(p, safe="") for p in rel_parts)
        download_url = (
            f"https://{self.host}/folder/{url_path}"
            f"?dcPath=ha-datacenter&dsName={urllib.parse.quote(datastore_name, safe='')}"
        )

        ticket = self.content.sessionManager.AcquireGenericServiceTicket(
            spec=vim.SessionManager.HttpServiceRequestSpec(method="GET", url=download_url)
        )

        if not local_path:
            suffix = os.path.splitext(posixpath.basename(rel_path))[1] or ".bin"
            fd, local_path = tempfile.mkstemp(prefix="nebula-esxi-ds-", suffix=suffix)
            os.close(fd)

        response = requests.get(
            download_url,
            headers={"Authorization": f"vmware_cgi_ticket {ticket.id}"},
            timeout=max(60, self.timeout * 6),
            verify=self.verify_ssl,
            stream=True,
        )

        if response.status_code == 401:
            response.close()
            cookie_header = self._session_cookie_header()
            if cookie_header:
                response = requests.get(
                    download_url,
                    headers={"Cookie": cookie_header},
                    timeout=max(60, self.timeout * 6),
                    verify=self.verify_ssl,
                    stream=True,
                )

        if response.status_code == 401:
            response.close()
            response = requests.get(
                download_url,
                auth=(self.username, self.password),
                timeout=max(60, self.timeout * 6),
                verify=self.verify_ssl,
                stream=True,
            )

        response.raise_for_status()
        with open(local_path, "wb") as out_handle:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    out_handle.write(chunk)

        return local_path

    def import_ovf_from_datastore(
        self,
        ovf_vmfs_path: str,
        vm_name: str,
        datastore_name: str,
        cpu_count: int = 2,
        ram_mb: int = 2048,
        guest_os: str = "other-64",
        network_name: str = "VM Network",
        nic_type: str = "e1000",
        scsi_controller: str = "lsilogic",
        firmware: str = "bios",
        hw_version: str = "13",
        disk_type: str = "thin",
        power_on: bool = False,
        extra_nics: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """
        Deploy an OVF already on the datastore by:
          1. Reading OVF text via HTTP.
          2. Creating a VM shell via CreateVM_Task (no disk copy).
          3. Attaching the existing VMDK files as existing disks.
        Returns {"status": "success", "message": "..."} on success.
        """
        if not self.content:
            raise RuntimeError("Not connected")

        normalized_ovf = posixpath.normpath(ovf_vmfs_path or "")
        if not normalized_ovf.lower().endswith(".ovf"):
            raise ValueError("ovf_vmfs_path must point to a .ovf file")

        ovf_source_dir = posixpath.dirname(normalized_ovf)
        ovf_ds_name, _, _ = self._vmfs_to_datastore_path(normalized_ovf)
        target_ds_name = datastore_name or ovf_ds_name

        # 1. Read OVF text
        ovf_bytes = self.read_datastore_file_content(normalized_ovf, max_bytes=2 * 1024 * 1024)
        ovf_text = ovf_bytes.decode("utf-8", errors="replace")

        # 2. Discover VMDK files in same directory
        vmdk_paths = []
        try:
            dir_entries = self.list_datastore_directory(ovf_source_dir)
            for entry in dir_entries:
                name_l = entry.name.lower()
                if (
                    not entry.is_dir
                    and name_l.endswith(".vmdk")
                    and not name_l.endswith(("-flat.vmdk", "-delta.vmdk", "-sesparse.vmdk", "-ctk.vmdk"))
                ):
                    vmdk_paths.append(entry.path)
        except Exception:
            pass

        # 3. Create the VM shell
        host = self._get_host_object()
        resource_pool = host.parent.resourcePool
        vm_folder = None
        for child in self.content.rootFolder.childEntity:
            if isinstance(child, vim.Datacenter):
                vm_folder = child.vmFolder
                break
        if vm_folder is None:
            vm_folder = self.content.rootFolder

        scsi_cls_map = {
            "lsilogic": vim.vm.device.VirtualLsiLogicController,
            "lsisas1068": vim.vm.device.VirtualLsiLogicSASController,
            "pvscsi": vim.vm.device.ParaVirtualSCSIController,
        }
        scsi_cls = scsi_cls_map.get(str(scsi_controller).lower(), vim.vm.device.VirtualLsiLogicController)

        config = vim.vm.ConfigSpec()
        config.name = vm_name
        config.memoryMB = int(ram_mb)
        config.numCPUs = int(cpu_count)
        config.guestId = self._map_guest_os_to_guest_id(guest_os)
        config.files = vim.vm.FileInfo(vmPathName=f"[{target_ds_name}]")
        try:
            config.version = f"vmx-{int(str(hw_version).strip()):02d}"
        except (ValueError, TypeError):
            config.version = "vmx-13"
        config.firmware = "efi" if str(firmware).lower() in ("efi", "uefi") else "bios"

        devices = []

        scsi_spec = vim.vm.device.VirtualDeviceSpec()
        scsi_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        scsi_ctrl = scsi_cls()
        scsi_ctrl.key = 1000
        scsi_ctrl.busNumber = 0
        scsi_ctrl.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
        scsi_spec.device = scsi_ctrl
        devices.append(scsi_spec)

        # 4. Attach existing VMDKs
        unit_number = 0
        for vmdk_path in vmdk_paths:
            _, vmdk_rel, vmdk_ds_path = self._vmfs_to_datastore_path(vmdk_path)
            disk_spec = vim.vm.device.VirtualDeviceSpec()
            disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            vdisk = vim.vm.device.VirtualDisk()
            vdisk.key = -1
            vdisk.unitNumber = unit_number
            vdisk.controllerKey = 1000
            backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
            backing.fileName = vmdk_ds_path
            backing.diskMode = "persistent"
            backing.thinProvisioned = str(disk_type).lower() == "thin"
            vdisk.backing = backing
            disk_spec.device = vdisk
            devices.append(disk_spec)
            unit_number += 1

        # 5. NIC
        nic_cls_map = {
            "e1000": vim.vm.device.VirtualE1000,
            "e1000e": vim.vm.device.VirtualE1000e,
            "vmxnet3": vim.vm.device.VirtualVmxnet3,
        }
        nic_cls = nic_cls_map.get(str(nic_type).lower(), vim.vm.device.VirtualE1000)
        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        nic = nic_cls()
        nic.key = -1
        nic_backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        nic_backing.deviceName = network_name
        nic.backing = nic_backing
        nic.addressType = "generated"
        nic_spec.device = nic
        devices.append(nic_spec)

        for extra in (extra_nics or []):
            extra_nic_cls = nic_cls_map.get(str(extra.get("type") or nic_type).lower(), vim.vm.device.VirtualE1000)
            en_spec = vim.vm.device.VirtualDeviceSpec()
            en_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            en = extra_nic_cls()
            en.key = -1
            en_back = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
            en_back.deviceName = str(extra.get("network") or network_name)
            en.backing = en_back
            en.addressType = "generated"
            en_spec.device = en
            devices.append(en_spec)

        config.deviceChange = devices

        try:
            task = vm_folder.CreateVM_Task(config=config, pool=resource_pool, host=host)
        except Exception:
            task = vm_folder.CreateVM_Task(config=config, pool=resource_pool)
        self._wait_for_task(task)

        if power_on:
            try:
                vm_obj = self._find_vm(vm_name)
                if vm_obj:
                    self._wait_for_task(vm_obj.PowerOn())
            except Exception:
                return {
                    "status": "success",
                    "warning": "VM created but could not power on automatically.",
                    "message": f"VM '{vm_name}' created from OVF with {len(vmdk_paths)} attached disk(s).",
                }

        return {
            "status": "success",
            "message": f"VM '{vm_name}' created from OVF with {len(vmdk_paths)} attached disk(s).",
        }

    # ========================================================================
    # VM Creation & Reconfiguration (pyVmomi — works in API mode)
    # ========================================================================

    def _map_guest_os_to_guest_id(self, guest_os: str) -> str:
        """Map VMX guestOS values to valid pyVmomi guestId values."""
        vmx_to_guest_id = {
            "other-64": "otherLinux64Guest",
            "other-32": "otherLinuxGuest",
            "ubuntu-64": "ubuntu64Guest",
            "debian12-64": "debian12_64Guest",
            "debian-64": "debian11_64Guest",
            "centos-64": "centos64Guest",
            "rhel9-64": "rhel9_64Guest",
            "rhel8-64": "rhel8_64Guest",
            "sles15-64": "sles15_64Guest",
            "windows2022srvNext-64": "windows2019srv_64Guest",
            "windows2019srv-64": "windows2019srv_64Guest",
            "windows2016srv-64": "windows2016srv_64Guest",
            "windows9-64": "windows11_64Guest",
        }
        return vmx_to_guest_id.get(str(guest_os).lower(), "otherLinux64Guest")

    def _get_host_cpu_mhz(self) -> int:
        """Return host CPU MHz per core when available."""
        try:
            host = self._get_host_object()
            hz = int(getattr(getattr(host.hardware, "cpuInfo", None), "hz", 0) or 0)
            return max(hz // (10**6), 0)
        except Exception:
            return 0

    def create_vm(
        self,
        datastore_name: str,
        vm_name: str,
        ram_mb: int = 2048,
        cpu_count: int = 2,
        disk_size_gb: int = 16,
        disk_type: str = "thin",
        guest_os: str = "other-64",
        network_name: str = "VM Network",
        nic_type: str = "e1000",
        scsi_controller: str = "lsilogic",
        firmware: str = "bios",
        hw_version: str = "13",
        power_on: bool = False,
        cd_iso_path: str = "",
        extra_disks: Optional[List[Dict]] = None,
        extra_nics: Optional[List[Dict]] = None,
        cpu_hotplug: bool = False,
        memory_hotplug: bool = False,
        hardware_virtualization: bool = False,
        pci_passthrough_devices: Optional[List[str]] = None,
        reserve_all_cpu: bool = False,
        reserve_all_memory: bool = False,
        **_kwargs,
    ) -> Dict[str, Any]:
        """Create a VM on standalone ESXi using pyVmomi CreateVM_Task."""
        if not self.content:
            raise RuntimeError("Not connected")

        host = self._get_host_object()
        resource_pool = host.parent.resourcePool

        # On standalone ESXi the VM folder lives under the implicit ha-datacenter.
        vm_folder = None
        for child in self.content.rootFolder.childEntity:
            if isinstance(child, vim.Datacenter):
                vm_folder = child.vmFolder
                break
        if vm_folder is None:
            vm_folder = self.content.rootFolder

        config = vim.vm.ConfigSpec()
        config.name = vm_name
        config.memoryMB = int(ram_mb)
        config.numCPUs = int(cpu_count)
        config.guestId = self._map_guest_os_to_guest_id(guest_os)
        config.files = vim.vm.FileInfo(vmPathName=f"[{datastore_name}]")
        try:
            config.version = f"vmx-{int(str(hw_version).strip()):02d}"
        except (ValueError, TypeError):
            config.version = "vmx-13"
        config.firmware = "efi" if str(firmware).lower() in ("efi", "uefi") else "bios"

        if reserve_all_memory:
            mem_alloc = vim.ResourceAllocationInfo()
            mem_alloc.reservation = int(ram_mb)
            config.memoryAllocation = mem_alloc

        if reserve_all_cpu:
            cpu_mhz = self._get_host_cpu_mhz()
            if cpu_mhz <= 0:
                cpu_mhz = 1000
            cpu_alloc = vim.ResourceAllocationInfo()
            cpu_alloc.reservation = int(cpu_count) * cpu_mhz
            config.cpuAllocation = cpu_alloc

        devices = []

        # SCSI controller
        scsi_cls_map = {
            "lsilogic": vim.vm.device.VirtualLsiLogicController,
            "lsisas1068": vim.vm.device.VirtualLsiLogicSASController,
            "pvscsi": vim.vm.device.ParaVirtualSCSIController,
        }
        scsi_cls = scsi_cls_map.get(str(scsi_controller).lower(), vim.vm.device.VirtualLsiLogicController)
        scsi_spec = vim.vm.device.VirtualDeviceSpec()
        scsi_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        scsi_ctrl = scsi_cls()
        scsi_ctrl.key = 1000
        scsi_ctrl.busNumber = 0
        scsi_ctrl.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
        scsi_spec.device = scsi_ctrl
        devices.append(scsi_spec)

        # Primary disk
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create
        disk = vim.vm.device.VirtualDisk()
        disk.key = 0
        disk.unitNumber = 0
        disk.controllerKey = 1000
        disk_backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        disk_backing.diskMode = "persistent"
        disk_backing.thinProvisioned = str(disk_type).lower() == "thin"
        disk_backing.eagerlyScrub = str(disk_type).lower() == "eagerzeroedthick"
        disk.backing = disk_backing
        disk.capacityInKB = int(disk_size_gb) * 1024 * 1024
        disk_spec.device = disk
        devices.append(disk_spec)

        # NIC
        nic_cls_map = {
            "e1000": vim.vm.device.VirtualE1000,
            "e1000e": vim.vm.device.VirtualE1000e,
            "vmxnet3": vim.vm.device.VirtualVmxnet3,
        }
        nic_cls = nic_cls_map.get(str(nic_type).lower(), vim.vm.device.VirtualE1000)
        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        nic = nic_cls()
        nic.key = 0
        nic_backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        nic_backing.deviceName = network_name
        nic_backing.useAutoDetect = False
        nic.backing = nic_backing
        nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
        nic.connectable.startConnected = True
        nic.connectable.allowGuestControl = True
        nic_spec.device = nic
        devices.append(nic_spec)

        # Extra NICs (ethernet1, ethernet2, ...)
        for _en_i, en in enumerate(extra_nics or [], start=1):
            en_type = str(en.get("type", nic_type)).lower()
            en_net = str(en.get("network", network_name))
            en_cls = nic_cls_map.get(en_type, vim.vm.device.VirtualE1000)
            en_spec = vim.vm.device.VirtualDeviceSpec()
            en_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            en_nic = en_cls()
            en_nic.key = -(100 + _en_i)
            en_backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
            en_backing.deviceName = en_net
            en_backing.useAutoDetect = False
            en_nic.backing = en_backing
            en_nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
            en_nic.connectable.startConnected = True
            en_nic.connectable.allowGuestControl = True
            en_spec.device = en_nic
            devices.append(en_spec)

        # Extra disks on SCSI slots 1, 2, ...
        for _ed_i, ed in enumerate(extra_disks or [], start=1):
            ed_size = int(ed.get("size_gb", 16))
            ed_fmt = str(ed.get("type", disk_type)).lower()
            ex_disk_spec = vim.vm.device.VirtualDeviceSpec()
            ex_disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            ex_disk_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create
            ex_disk = vim.vm.device.VirtualDisk()
            ex_disk.key = -(_ed_i + 10)
            ex_disk.unitNumber = _ed_i
            ex_disk.controllerKey = 1000
            ex_backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
            ex_backing.diskMode = "persistent"
            ex_backing.thinProvisioned = ed_fmt == "thin"
            ex_backing.eagerlyScrub = ed_fmt == "eagerzeroedthick"
            ex_disk.backing = ex_backing
            ex_disk.capacityInKB = ed_size * 1024 * 1024
            ex_disk_spec.device = ex_disk
            devices.append(ex_disk_spec)

        # CD-ROM / ISO
        cdrom_dev = vim.vm.device.VirtualCdrom()
        cdrom_dev.key = 3000
        cdrom_dev.controllerKey = 200  # IDE controller (built-in)
        cdrom_dev.unitNumber = 0
        if cd_iso_path:
            iso_backing = vim.vm.device.VirtualCdrom.IsoBackingInfo()
            iso_backing.fileName = self._normalize_datastore_path(str(cd_iso_path).strip())
            cdrom_dev.backing = iso_backing
            cdrom_dev.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
            cdrom_dev.connectable.startConnected = True
            cdrom_dev.connectable.connected = True
            cdrom_dev.connectable.allowGuestControl = True
        else:
            cdrom_dev.backing = vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo()
            cdrom_dev.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
            cdrom_dev.connectable.startConnected = False
            cdrom_dev.connectable.connected = False
            cdrom_dev.connectable.allowGuestControl = True
        cdrom_spec = vim.vm.device.VirtualDeviceSpec()
        cdrom_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        cdrom_spec.device = cdrom_dev
        devices.append(cdrom_spec)

        # PCI passthrough devices
        for _pci_id in (pci_passthrough_devices or []):
            if not _pci_id:
                continue
            pci_spec = vim.vm.device.VirtualDeviceSpec()
            pci_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            pci_dev = vim.vm.device.VirtualPCIPassthrough()
            pci_dev.key = -1
            pci_backing = vim.vm.device.VirtualPCIPassthrough.DeviceBackingInfo()
            pci_backing.id = str(_pci_id)
            pci_dev.backing = pci_backing
            pci_spec.device = pci_dev
            devices.append(pci_spec)

        config.deviceChange = devices

        # ExtraConfig: hotplug and nested virtualization flags
        extra_config_items = []
        if cpu_hotplug:
            extra_config_items.append(vim.option.OptionValue(key="vcpu.hotadd", value="TRUE"))
        if memory_hotplug:
            extra_config_items.append(vim.option.OptionValue(key="mem.hotadd", value="TRUE"))
        if hardware_virtualization:
            extra_config_items.append(vim.option.OptionValue(key="vhv.enable", value="TRUE"))
        if extra_config_items:
            config.extraConfig = extra_config_items

        task = vm_folder.CreateVM_Task(config=config, pool=resource_pool)
        task_info = self._wait_for_task(task)

        power_on_warning = None
        if power_on and task_info.result:
            try:
                on_task = task_info.result.PowerOnVM_Task()
                self._wait_for_task(on_task)
            except Exception as exc:
                power_on_warning = str(exc)

        return {"status": "success", "message": f"VM '{vm_name}' created", "warning": power_on_warning}

    def reconfigure_vm(self, vm_identifier: str, modification: str, **kwargs) -> Dict[str, Any]:
        """Reconfigure VM hardware via ReconfigVM_Task."""
        vm = self._find_vm_by_identifier(vm_identifier)
        if not vm:
            return {"status": "error", "message": f"VM not found: {vm_identifier}"}

        config = vim.vm.ConfigSpec()
        device_changes = []

        if modification == "cpu":
            config.numCPUs = int(kwargs.get("cpu", 1))

        elif modification == "memory":
            config.memoryMB = int(kwargs.get("memory", 512))

        elif modification == "guest_os":
            config.guestId = self._map_guest_os_to_guest_id(kwargs.get("guest_os", "other-64"))

        elif modification == "add_network":
            nic_cls_map = {
                "e1000": vim.vm.device.VirtualE1000,
                "e1000e": vim.vm.device.VirtualE1000e,
                "vmxnet3": vim.vm.device.VirtualVmxnet3,
            }
            nic_cls = nic_cls_map.get(str(kwargs.get("adapter_type", "e1000")).lower(), vim.vm.device.VirtualE1000)
            nic_spec = vim.vm.device.VirtualDeviceSpec()
            nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            nic = nic_cls()
            nic.key = -1
            nic_backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
            nic_backing.deviceName = str(kwargs.get("network_name", "VM Network"))
            nic_backing.useAutoDetect = False
            nic.backing = nic_backing
            nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
            nic.connectable.startConnected = True
            nic.connectable.allowGuestControl = True
            nic_spec.device = nic
            device_changes.append(nic_spec)

        elif modification == "remove_network":
            nic_number = int(kwargs.get("nic_number", 0))
            nics = [
                d for d in (vm.config.hardware.device or [])
                if isinstance(d, vim.vm.device.VirtualEthernetCard)
            ]
            if nic_number >= len(nics):
                return {"status": "error", "message": f"NIC index {nic_number} not found"}
            rm_spec = vim.vm.device.VirtualDeviceSpec()
            rm_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
            rm_spec.device = nics[nic_number]
            device_changes.append(rm_spec)

        elif modification == "add_disk":
            disk_size_gb = int(kwargs.get("disk_size", 16))
            scsi_ctrls = [
                d for d in (vm.config.hardware.device or [])
                if isinstance(d, vim.vm.device.VirtualSCSIController)
            ]
            if not scsi_ctrls:
                return {"status": "error", "message": "No SCSI controller found on VM"}
            ctrl = scsi_ctrls[0]
            used_units = {d.unitNumber for d in (vm.config.hardware.device or []) if getattr(d, "controllerKey", None) == ctrl.key}
            unit = next((u for u in range(0, 16) if u not in used_units and u != 7), None)
            if unit is None:
                return {"status": "error", "message": "No free SCSI unit number available"}
            disk_spec = vim.vm.device.VirtualDeviceSpec()
            disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            disk_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create
            new_disk = vim.vm.device.VirtualDisk()
            new_disk.key = -1
            new_disk.unitNumber = unit
            new_disk.controllerKey = ctrl.key
            backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
            backing.diskMode = "persistent"
            backing.thinProvisioned = True
            new_disk.backing = backing
            new_disk.capacityInKB = disk_size_gb * 1024 * 1024
            disk_spec.device = new_disk
            device_changes.append(disk_spec)

        elif modification == "remove_disk":
            disk_unit = int(kwargs.get("disk_unit", 0))
            disks = [d for d in (vm.config.hardware.device or []) if isinstance(d, vim.vm.device.VirtualDisk)]
            target = next((d for d in disks if d.unitNumber == disk_unit), None)
            if not target:
                return {"status": "error", "message": f"Disk unit {disk_unit} not found"}
            rm_spec = vim.vm.device.VirtualDeviceSpec()
            rm_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
            rm_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.destroy
            rm_spec.device = target
            device_changes.append(rm_spec)

        elif modification == "resize_disk":
            disk_unit = int(kwargs.get("disk_unit", 0))
            new_size_gb = int(kwargs.get("disk_size", 0))
            disks = [d for d in (vm.config.hardware.device or []) if isinstance(d, vim.vm.device.VirtualDisk)]
            target = next((d for d in disks if d.unitNumber == disk_unit), None)
            if not target:
                return {"status": "error", "message": f"Disk unit {disk_unit} not found"}
            resize_spec = vim.vm.device.VirtualDeviceSpec()
            resize_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            target.capacityInKB = new_size_gb * 1024 * 1024
            resize_spec.device = target
            device_changes.append(resize_spec)

        elif modification == "mount_iso":
            raw_iso_path = str(kwargs.get("iso_path", "")).strip()
            iso_path = self._normalize_datastore_path(raw_iso_path)
            if not iso_path:
                return {"status": "error", "message": "ISO path is required"}
            if not iso_path.lower().endswith(".iso"):
                return {"status": "error", "message": f"Only .iso files are supported (got: {raw_iso_path})"}
            if not iso_path.startswith("["):
                return {
                    "status": "error",
                    "message": (
                        "Invalid ISO path format. Use /vmfs/volumes/<datastore>/path/file.iso "
                        "or [datastore] path/file.iso"
                    ),
                }
            cdroms = [d for d in (vm.config.hardware.device or []) if isinstance(d, vim.vm.device.VirtualCdrom)]
            if not cdroms:
                return {"status": "error", "message": "No CD-ROM device found on VM"}
            cdrom = cdroms[0]
            iso_spec = vim.vm.device.VirtualDeviceSpec()
            iso_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            ds_backing = vim.vm.device.VirtualCdrom.IsoBackingInfo()
            ds_backing.fileName = iso_path
            cdrom.backing = ds_backing
            cdrom.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
            cdrom.connectable.startConnected = True
            cdrom.connectable.connected = True
            cdrom.connectable.allowGuestControl = True
            iso_spec.device = cdrom
            device_changes.append(iso_spec)

        elif modification == "eject_iso":
            cdroms = [d for d in (vm.config.hardware.device or []) if isinstance(d, vim.vm.device.VirtualCdrom)]
            if not cdroms:
                return {"status": "error", "message": "No CD-ROM device found on VM"}
            cdrom = cdroms[0]
            eject_spec = vim.vm.device.VirtualDeviceSpec()
            eject_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            cdrom.backing = vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo()
            cdrom.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
            cdrom.connectable.startConnected = False
            cdrom.connectable.connected = False
            cdrom.connectable.allowGuestControl = True
            eject_spec.device = cdrom
            device_changes.append(eject_spec)

        elif modification == "hw_version":
            hw_ver_raw = str(kwargs.get("hw_version", "13")).strip()
            try:
                version_num = int(hw_ver_raw)
            except ValueError:
                version_num = 13
            # UpgradeVM_Task is a separate task, not part of ReconfigVM
            task = vm.UpgradeVM_Task(version=f"vmx-{version_num:02d}")
            self._wait_for_task(task)
            return {"status": "success", "message": f"VM hardware version upgraded to vmx-{version_num:02d}"}

        elif modification in {"cpu_hotplug", "memory_hotplug", "hardware_virtualization"}:
            enabled_raw = kwargs.get("enabled", kwargs.get("value", "false"))
            if isinstance(enabled_raw, bool):
                is_enabled = enabled_raw
            else:
                is_enabled = str(enabled_raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}
            key_map = {
                "cpu_hotplug": "vcpu.hotadd",
                "memory_hotplug": "mem.hotadd",
                "hardware_virtualization": "vhv.enable",
            }
            extra_key = key_map[modification]
            config.extraConfig = [
                vim.option.OptionValue(key=extra_key, value="TRUE" if is_enabled else "FALSE")
            ]
            # Also set dedicated ConfigSpec flags when available; these are the
            # authoritative fields surfaced by vSphere for hot-add/nested-virt.
            if modification == "cpu_hotplug":
                try:
                    config.cpuHotAddEnabled = bool(is_enabled)
                except Exception:
                    pass
            elif modification == "memory_hotplug":
                try:
                    config.memoryHotAddEnabled = bool(is_enabled)
                except Exception:
                    pass
            elif modification == "hardware_virtualization":
                try:
                    config.nestedHVEnabled = bool(is_enabled)
                except Exception:
                    pass

        elif modification in {"reserve_all_memory", "reserve_all_cpu"}:
            enabled_raw = kwargs.get("enabled", kwargs.get("value", "false"))
            if isinstance(enabled_raw, bool):
                is_enabled = enabled_raw
            else:
                is_enabled = str(enabled_raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}

            if modification == "reserve_all_memory":
                mem_alloc = vm.config.memoryAllocation or vim.ResourceAllocationInfo()
                mem_alloc.reservation = int(vm.config.hardware.memoryMB) if is_enabled else 0
                config.memoryAllocation = mem_alloc
            else:
                cpu_alloc = vm.config.cpuAllocation or vim.ResourceAllocationInfo()
                host_cpu_mhz = self._get_host_cpu_mhz()
                if host_cpu_mhz <= 0:
                    host_cpu_mhz = 1000
                desired = int(vm.config.hardware.numCPU) * host_cpu_mhz if is_enabled else 0
                cpu_alloc.reservation = desired
                config.cpuAllocation = cpu_alloc

        elif modification == "add_pci_passthrough":
            pci_id_val = str(kwargs.get("pci_id", "")).strip()
            if not pci_id_val:
                return {"status": "error", "message": "pci_id is required for add_pci_passthrough"}
            pci_spec = vim.vm.device.VirtualDeviceSpec()
            pci_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            pci_dev = vim.vm.device.VirtualPCIPassthrough()
            pci_dev.key = -1
            pci_backing = vim.vm.device.VirtualPCIPassthrough.DeviceBackingInfo()
            pci_backing.id = pci_id_val
            pci_dev.backing = pci_backing
            pci_spec.device = pci_dev
            device_changes.append(pci_spec)

        elif modification == "remove_pci_passthrough":
            pci_slot_val = int(kwargs.get("pci_slot", kwargs.get("slot", -1)))
            pci_devices_list = [
                d for d in (vm.config.hardware.device or [])
                if isinstance(d, vim.vm.device.VirtualPCIPassthrough)
            ]
            target_pci = next((d for d in pci_devices_list if d.key == pci_slot_val), None)
            if not target_pci:
                return {"status": "error", "message": f"PCI device with key {pci_slot_val} not found"}
            rm_pci_spec = vim.vm.device.VirtualDeviceSpec()
            rm_pci_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
            rm_pci_spec.device = target_pci
            device_changes.append(rm_pci_spec)

        else:
            return {"status": "error", "message": f"Unsupported modification via API: {modification}"}

        if device_changes:
            config.deviceChange = device_changes

        task = vm.ReconfigVM_Task(spec=config)
        self._wait_for_task(task)
        return {"status": "success", "message": f"Applied '{modification}' to VM"}

    def get_vm_hardware_api(self, vm_identifier: str) -> Dict[str, Any]:
        """Return structured VM hardware (NICs, disks, CD-ROM, PCI, hotplug) via vSphere API."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        vm = self._find_vm_by_identifier(vm_identifier)
        if not vm:
            return {"status": "error", "message": f"VM not found: {vm_identifier}"}

        config = vm.config
        runtime = vm.runtime

        nics: List[Dict[str, Any]] = []
        disks: List[Dict[str, Any]] = []
        cdrom: Dict[str, Any] = {
            "present": False, "mounted": False, "iso_path": "", "device_type": "atapi-cdrom"
        }
        pci_passthrough: List[Dict[str, Any]] = []

        nic_type_map = {
            "VirtualE1000": "e1000",
            "VirtualE1000e": "e1000e",
            "VirtualVmxnet3": "vmxnet3",
            "VirtualVmxnet2": "vmxnet2",
        }
        nic_idx = 0

        for device in (config.hardware.device or []):
            type_name = type(device).__name__

            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                backing = device.backing
                network_name_val = ""
                if hasattr(backing, "deviceName"):
                    network_name_val = str(backing.deviceName or "")
                elif hasattr(backing, "port"):
                    port = getattr(backing, "port", None)
                    network_name_val = str(getattr(port, "portgroupKey", "") or "") if port else ""
                adapter_type = nic_type_map.get(type_name, type_name.replace("Virtual", "").lower())
                label = ""
                if device.deviceInfo:
                    label = str(device.deviceInfo.label or "")
                if not label:
                    label = f"NIC {nic_idx}"
                mac = str(getattr(device, "macAddress", "") or "N/A")
                connected = True
                if device.connectable:
                    connected = bool(device.connectable.connected)
                nics.append({
                    "index": nic_idx,
                    "label": label,
                    "network": network_name_val,
                    "type": adapter_type,
                    "mac": mac,
                    "connected": connected,
                    "key": device.key,
                })
                nic_idx += 1

            elif isinstance(device, vim.vm.device.VirtualDisk):
                size_gb = round((device.capacityInKB or 0) / (1024 * 1024), 2)
                unit = device.unitNumber or 0
                ctrl = device.controllerKey or 0
                backing_file = ""
                if hasattr(device.backing, "fileName"):
                    backing_file = str(device.backing.fileName or "")
                is_thin = bool(getattr(device.backing, "thinProvisioned", False))
                label = ""
                if device.deviceInfo:
                    label = str(device.deviceInfo.label or "")
                if not label:
                    label = f"Hard Disk ({unit})"
                disks.append({
                    "controller": ctrl,
                    "unit": unit,
                    "label": label,
                    "file": backing_file,
                    "full_path": backing_file,
                    "size_gb": size_gb,
                    "thin": is_thin,
                    "key": device.key,
                })

            elif isinstance(device, vim.vm.device.VirtualCdrom):
                backing = device.backing
                is_iso = isinstance(backing, vim.vm.device.VirtualCdrom.IsoBackingInfo)
                iso_path_val = str(backing.fileName or "") if (is_iso and hasattr(backing, "fileName")) else ""
                cdrom = {
                    "present": True,
                    "mounted": is_iso and bool(iso_path_val),
                    "iso_path": iso_path_val,
                    "device_type": "cdrom-image" if (is_iso and iso_path_val) else "atapi-cdrom",
                    "key": device.key,
                }

            elif isinstance(device, vim.vm.device.VirtualPCIPassthrough):
                pci_id_str = ""
                if device.backing:
                    pci_id_str = str(getattr(device.backing, "id", "") or "")
                label = ""
                if device.deviceInfo:
                    label = str(device.deviceInfo.label or "")
                pci_passthrough.append({
                    "slot": device.key,
                    "id": pci_id_str,
                    "label": label or pci_id_str or str(device.key),
                    "key": device.key,
                })

        # Hotplug / nested-virt flags from dedicated config fields + extraConfig fallback.
        extra_config_map: Dict[str, str] = {
            item.key: str(item.value) for item in (config.extraConfig or [])
        }
        cpu_hotplug = bool(getattr(config, "cpuHotAddEnabled", False)) or (
            extra_config_map.get("vcpu.hotadd", "FALSE").upper() == "TRUE"
        )
        memory_hotplug = bool(getattr(config, "memoryHotAddEnabled", False)) or (
            extra_config_map.get("mem.hotadd", "FALSE").upper() == "TRUE"
        )
        hw_virt = bool(getattr(config, "nestedHVEnabled", False)) or (
            extra_config_map.get("vhv.enable", "FALSE").upper() == "TRUE"
        )
        memory_mb = int(getattr(getattr(config, "hardware", None), "memoryMB", 0) or 0)
        cpu_count = int(getattr(getattr(config, "hardware", None), "numCPU", 0) or 0)
        memory_reservation = int(getattr(getattr(config, "memoryAllocation", None), "reservation", 0) or 0)
        cpu_reservation = int(getattr(getattr(config, "cpuAllocation", None), "reservation", 0) or 0)
        host_cpu_mhz = self._get_host_cpu_mhz()
        expected_full_cpu_reservation = cpu_count * host_cpu_mhz if host_cpu_mhz > 0 else 0
        reserve_all_memory = memory_reservation >= memory_mb and memory_mb > 0
        reserve_all_cpu = (
            cpu_reservation >= expected_full_cpu_reservation and expected_full_cpu_reservation > 0
        ) or (expected_full_cpu_reservation == 0 and cpu_reservation > 0)

        power_state = str(runtime.powerState) if runtime else "poweredOff"

        nics.sort(key=lambda x: x["index"])
        disks.sort(key=lambda x: x["unit"])
        pci_passthrough.sort(key=lambda x: x["slot"])

        return {
            "status": "success",
            "vmid": vm_identifier,
            "power_state": power_state,
            "nics": nics,
            "disks": disks,
            "cdrom": cdrom,
            "cpu_hotplug": cpu_hotplug,
            "memory_hotplug": memory_hotplug,
            "hardware_virtualization": hw_virt,
            "reserve_all_memory": reserve_all_memory,
            "reserve_all_cpu": reserve_all_cpu,
            "pci_passthrough": pci_passthrough,
        }

    def list_pci_devices(self) -> List[Dict[str, Any]]:
        """List PCI devices on the host available for passthrough."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        host = self._get_host_object()
        devices: List[Dict[str, Any]] = []
        for pci in (host.hardware.pciDevice or []):
            bus = int(getattr(pci, "bus", 0) or 0)
            slot = int(getattr(pci, "slot", 0) or 0)
            func = int(getattr(pci, "function", 0) or 0)
            pci_id = f"{bus:02x}:{slot:02x}.{func}"
            vendor = str(getattr(pci, "vendorName", "") or "")
            device_name = str(getattr(pci, "deviceName", "") or "")
            label = f"{pci_id} - {vendor} {device_name}".strip(" -")
            devices.append({
                "id": pci_id,
                "label": label,
                "vendor": vendor,
                "device": device_name,
            })
        return devices

    def list_iso_files(self) -> List[str]:
        """List ISO files available on all datastores visible to this host."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        import re as _re
        host = self._get_host_object()
        browser = host.datastoreBrowser
        isos: List[str] = []
        for ds in (host.datastore or []):
            try:
                ds_name = ds.name
                search_spec = vim.HostDatastoreBrowserSearchSpec()
                search_spec.matchPattern = ["*.iso"]
                task = browser.SearchDatastoreSubFolders_Task(
                    datastorePath=f"[{ds_name}]",
                    searchSpec=search_spec,
                )
                info = self._wait_for_task(task, timeout=60)
                results = getattr(info, "result", []) or []
                for result in results:
                    folder_path = str(getattr(result, "folderPath", f"[{ds_name}]") or f"[{ds_name}]")
                    for file_info in (getattr(result, "file", []) or []):
                        raw_name = str(getattr(file_info, "path", "") or "").strip()
                        if not raw_name.lower().endswith(".iso"):
                            continue
                        m = _re.match(r'\[([^\]]+)\]\s*(.*)', folder_path)
                        if m:
                            ds_prefix = m.group(1)
                            rel_folder = m.group(2).strip("/")
                            if rel_folder:
                                full_path = f"/vmfs/volumes/{ds_prefix}/{rel_folder}/{raw_name}"
                            else:
                                full_path = f"/vmfs/volumes/{ds_prefix}/{raw_name}"
                        else:
                            full_path = f"/vmfs/volumes/{ds_name}/{raw_name}"
                        isos.append(full_path)
            except Exception:
                continue
        return isos

    def add_license(self, serial_key: str) -> Dict[str, Any]:
        """Assign a license key to this host via vSphere LicenseManager."""
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")
        try:
            lm = self.content.licenseManager
            # Add/validate the license key in the license inventory first
            lm.UpdateLicense(licenseKey=serial_key, labels=None)
            # Assign it to this host entity
            host = self._get_host_object()
            lam = lm.licenseAssignmentManager
            lam.UpdateAssignedLicense(entity=host._moId, licenseKey=serial_key)
            return {"status": "success", "message": f"License assigned: {serial_key}"}
        except Exception as exc:
            return {"status": "error", "message": f"License assignment failed: {exc}"}

    # ========================================================================
    # Helper Methods
    # ========================================================================

    def _get_host_object(self) -> vim.HostSystem:
        """Get the host system object from vSphere.

        Handles both standalone ESXi (rootFolder → ComputeResource → host)
        and vCenter (rootFolder → Datacenter → hostFolder → ComputeResource/Cluster → host).
        """
        if not self.content:
            raise RuntimeError("Not connected")

        def _search_compute(entities):
            for entity in entities:
                if isinstance(entity, vim.HostSystem):
                    return entity
                if isinstance(entity, (vim.ComputeResource, vim.ClusterComputeResource)):
                    for host in entity.host:
                        return host
            return None

        for child in self.content.rootFolder.childEntity:
            # vCenter: Datacenter wraps the host folder
            if isinstance(child, vim.Datacenter):
                result = _search_compute(child.hostFolder.childEntity)
                if result is not None:
                    return result
            # Standalone ESXi: ComputeResource sits directly under rootFolder
            elif isinstance(child, (vim.ComputeResource, vim.ClusterComputeResource)):
                for host in child.host:
                    return host
            elif isinstance(child, vim.HostSystem):
                return child

        raise RuntimeError("No host system found in vSphere inventory")

    def _get_datacenter_object(self) -> vim.Datacenter:
        """Find a datacenter object required by FileManager datastore methods."""
        if not self.content:
            raise RuntimeError("Not connected")

        for child in self.content.rootFolder.childEntity:
            if isinstance(child, vim.Datacenter):
                return child
        raise RuntimeError("No datacenter found in vSphere inventory")

    def _vmfs_to_datastore_path(self, vmfs_path: str) -> tuple[str, str, str]:
        """Convert /vmfs/volumes/... path to [datastore] relative/path format."""
        normalized = posixpath.normpath(vmfs_path or "/vmfs/volumes")
        if not normalized.startswith("/vmfs/volumes"):
            raise ValueError("Path must be under /vmfs/volumes")

        rel = normalized[len("/vmfs/volumes"):].strip("/")
        if not rel:
            raise ValueError("Datastore path must include datastore name")

        parts = rel.split("/", 1)
        datastore_name = parts[0]
        rel_path = parts[1] if len(parts) > 1 else ""
        datastore_path = f"[{datastore_name}]" if not rel_path else f"[{datastore_name}] {rel_path}"
        return datastore_name, rel_path, datastore_path

    def _get_host_ipv4(self, host: vim.HostSystem) -> str:
        """Extract first IPv4 address from host network config."""
        try:
            for vnic in host.config.network.vnic:
                if vnic.spec.ip.ipAddress:
                    return vnic.spec.ip.ipAddress
        except Exception:
            pass
        return "Unknown"

    def _find_vm(self, vm_name: str) -> Optional[vim.VirtualMachine]:
        """Find a VM by name using CreateContainerView (works on standalone ESXi and vCenter)."""
        view = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [vim.VirtualMachine], recursive=True
        )
        try:
            for vm in view.view:
                try:
                    if vm.config and vm.config.name == vm_name:
                        return vm
                except Exception:
                    continue
        finally:
            view.Destroy()
        return None

    def _find_vm_by_identifier(self, vm_identifier: str) -> Optional[vim.VirtualMachine]:
        """Find VM by UUID first, then by display name (works on standalone ESXi and vCenter)."""
        identifier = str(vm_identifier or "")
        view = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [vim.VirtualMachine], recursive=True
        )
        try:
            for vm in view.view:
                try:
                    if vm.config:
                        if str(vm.config.uuid) == identifier or vm.config.name == identifier:
                            return vm
                except Exception:
                    continue
        finally:
            view.Destroy()
        return None

    def _collect_vm_snapshots(self, vm: vim.VirtualMachine) -> List[Dict[str, Any]]:
        """Flatten VM snapshot tree to list."""
        snapshot_root = getattr(getattr(vm, "snapshot", None), "rootSnapshotList", None)
        if not snapshot_root:
            return []

        rows: List[Dict[str, Any]] = []

        def walk(nodes):
            for node in nodes or []:
                rows.append(
                    {
                        "name": str(getattr(node, "name", "") or ""),
                        "description": str(getattr(node, "description", "") or ""),
                        "create_time": str(getattr(node, "createTime", "") or ""),
                        "state": str(getattr(node, "state", "") or ""),
                    }
                )
                walk(getattr(node, "childSnapshotList", None))

        walk(snapshot_root)
        return rows

    def _find_snapshot_ref(self, nodes, target_name: Optional[str]):
        """Find snapshot managed object by name in snapshot tree."""
        if not target_name:
            return None
        for node in nodes or []:
            if str(getattr(node, "name", "")) == target_name:
                return getattr(node, "snapshot", None)
            found = self._find_snapshot_ref(getattr(node, "childSnapshotList", None), target_name)
            if found is not None:
                return found
        return None

    def _wait_for_task(self, task: vim.Task, timeout: int = 300) -> vim.TaskInfo:
        """Wait for a task to complete. Raises RuntimeError if the task errors."""
        import time
        max_wait = timeout
        while max_wait > 0:
            info = task.info
            state_str = str(info.state)
            if state_str == "success":
                return info
            if state_str == "error":
                error_msg = (
                    str(info.error.msg)
                    if info.error and hasattr(info.error, "msg")
                    else str(info.error)
                )
                raise RuntimeError(f"Task failed: {error_msg}")
            time.sleep(1)
            max_wait -= 1
        raise TimeoutError(f"Task did not complete within {timeout} seconds")


# ============================================================================
# Convenience Functions
# ============================================================================

def to_dict(obj: Any) -> Dict[str, Any]:
    """Convert dataclass to dictionary, handling nested objects."""
    if hasattr(obj, '__dataclass_fields__'):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    elif isinstance(obj, list):
        return [to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    else:
        return obj
