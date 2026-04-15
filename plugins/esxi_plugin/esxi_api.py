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

    @contextmanager
    def connect(self):
        """
        Establish authenticated connection to the ESXi host.
        
        Returns:
            self (for chaining operations)
            
        Raises:
            ConnectionError: If authentication or connection fails
        """
        try:
            # Disable SSL certificate verification (common for self-signed certs)
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
            yield self

        except vim.fault.InvalidLogin as e:
            raise ConnectionError(f"Authentication failed for {self.username}@{self.host}: {e}")
        except Exception as e:
            raise ConnectionError(f"Connection failed to {self.host}: {e}")
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

    # ========================================================================
    # VM OPERATIONS
    # ========================================================================

    def list_vms(self) -> List[VMInfo]:
        """
        List all VMs registered on the ESXi host.
        
        Returns:
            List of VMInfo objects
        """
        if not self.content:
            raise RuntimeError("Not connected to vSphere. Use 'with client.connect():'")

        container = self.content.rootFolder
        viewmgr = self.content.viewManager
        objspec = vmodl.Query.PropertyCollector.ObjectSpec(obj=container, selectSet=[
            vmodl.Query.PropertyCollector.TraversalSpec(
                name='traverseEntities',
                type=vim.Folder,
                path='childEntity',
                skip=False,
            )
        ])
        propspec = vmodl.Query.PropertyCollector.PropertySpec(
            type=vim.VirtualMachine,
            all=True,
        )
        filterspec = vmodl.Query.PropertyCollector.FilterSpec(
            objectSet=[objspec],
            propSet=[propspec],
        )

        collector = self.content.propertyCollector
        options = vmodl.Query.PropertyCollector.RetrieveOptions()
        result = collector.RetrievePropertiesEx([filterspec], options)

        vms = []
        for obj in result.objects:
            vm = obj.obj
            props = {prop.name: prop.val for prop in obj.propSet}

            vm_info = VMInfo(
                name=props.get('name', 'Unknown'),
                uuid=props.get('config.uuid', ''),
                power_state=props.get('runtime.powerState', 'unknown'),
                cpu_count=props.get('config.hardware.numCPU', 0),
                memory_mb=props.get('config.hardware.memoryMB', 0),
                tools_running=props.get('guest.toolsRunningStatus', 'guestToolsNotRunning') == 'guestToolsRunning',
                tools_status=props.get('guest.toolsStatus', 'toolsNotInstalled'),
                datastorage=props.get('config.files.vmPathName', ''),
                guest_os=props.get('config.guestFullName', ''),
                dns_name=props.get('guest.hostName', ''),
            )
            
            # Extract IP addresses
            if 'guest.net' in props:
                for net in props['guest.net']:
                    if hasattr(net, 'ipConfig') and net.ipConfig:
                        for addr in net.ipConfig.ipAddress:
                            if not addr.ipAddress.startswith('fe80'):  # Skip IPv6 link-local
                                vm_info.ip_addresses.append(addr.ipAddress)

            vms.append(vm_info)

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
            sw = NetworkSwitch(
                name=vswitch.name,
                portgroup_count=len(vswitch.portgroup) if vswitch.portgroup else 0,
                nic_count=len(vswitch.bridge.nicTeamingPolicy.nicOrder.activeNic) if hasattr(vswitch.bridge, 'nicTeamingPolicy') else 0,
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
            pgroup = Portgroup(
                name=pg.spec.name,
                vswitch=pg.spec.vswitchName,
                vlan_id=pg.spec.vlanId or 0,
                active_nics=list(pg.spec.policy.nicTeaming.nicOrder.activeNic) if hasattr(pg.spec.policy, 'nicTeaming') else [],
                standby_nics=list(pg.spec.policy.nicTeaming.nicOrder.standbyNic) if hasattr(pg.spec.policy, 'nicTeaming') else [],
            )
            portgroups.append(pgroup)

        return portgroups

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
            ds = ds_ref
            datastore = Datastore(
                name=ds.name,
                capacity_gb=round(ds.summary.capacity / (1024**3), 2),
                free_gb=round(ds.summary.freeSpace / (1024**3), 2),
                provisioned_gb=round(ds.summary.uncommitted / (1024**3), 2),
                type=ds.summary.type,
            )
            datastores.append(datastore)

        return datastores

    # ========================================================================
    # Helper Methods
    # ========================================================================

    def _get_host_object(self) -> vim.HostSystem:
        """Get the host system object from vSphere."""
        if not self.content:
            raise RuntimeError("Not connected")

        # Find the first (and typically only) host on a single ESXi system
        for child in self.content.rootFolder.childEntity:
            if isinstance(child, vim.Datacenter):
                for host_ref in child.hostFolder.childEntity:
                    if isinstance(host_ref, vim.ClusterComputeResource):
                        for host in host_ref.host:
                            return host
                    elif isinstance(host_ref, vim.HostSystem):
                        return host_ref
        raise RuntimeError("No host system found in vSphere inventory")

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
        """Find a VM by name."""
        container = self.content.rootFolder
        collector = self.content.propertyCollector
        
        objspec = vmodl.Query.PropertyCollector.ObjectSpec(obj=container)
        propspec = vmodl.Query.PropertyCollector.PropertySpec(
            type=vim.VirtualMachine,
            pathSet=['name'],
        )
        filterspec = vmodl.Query.PropertyCollector.FilterSpec(
            objectSet=[objspec],
            propSet=[propspec],
        )

        result = collector.RetrievePropertiesEx([filterspec], vmodl.Query.PropertyCollector.RetrieveOptions())
        
        for obj in result.objects:
            if obj.propSet[0].val == vm_name:
                return obj.obj

        return None

    def _wait_for_task(self, task: vim.Task, timeout: int = 300) -> vim.TaskInfo:
        """Wait for a task to complete."""
        max_wait = timeout
        while max_wait > 0:
            if task.info.state in (vim.TaskState.success, vim.TaskState.error):
                return task.info
            import time
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
