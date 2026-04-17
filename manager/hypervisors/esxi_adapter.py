from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from plugins.esxi_ssh_plugin import build_esxi_ssh_connection

from .base import HypervisorAdapter


def _hash_vm_state(state_dict: dict) -> str:
    return hashlib.md5(str(sorted(state_dict.items())).encode()).hexdigest()


class _EsxiApiConnection:
    """Context-manager adapter for EsxiApiClient.connect()."""

    def __init__(self, client: Any):
        self._client = client
        self._ctx = None

    def __enter__(self) -> Any:
        self._ctx = self._client.connect()
        return self._ctx.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._ctx is None:
            return False
        return bool(self._ctx.__exit__(exc_type, exc_val, exc_tb))


class EsxiAdapter(HypervisorAdapter):
    slug = "vmware_esxi"
    display_name = "VMware ESXi"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def build_connection(self, host: Any) -> Any:
        connection_method = (getattr(host, "esxi_connection_method", "") or "ssh").lower()
        if connection_method == "api":
            from plugins.esxi_api_plugin import EsxiApiClient

            client = EsxiApiClient(
                host=host.ip_address,
                username=host.username,
                password=host.password,
                verify_ssl=False,
            )
            return _EsxiApiConnection(client)

        return build_esxi_ssh_connection(host)

    def _sync_host_api(self, host: Any, conn: Any) -> bool:
        try:
            hardware = conn.get_host_hardware()
            summary = conn.get_host_summary()

            host.cpu_count = hardware.cpu_cores or host.cpu_count
            host.memory_gb = int(round(hardware.memory_gb or host.memory_gb or 0))
            host.vendor = summary.vendor or host.vendor or "VMware"
            host.model_name = summary.model or host.model_name or "Unknown Model"
            host.os_version = summary.version or host.os_version or "Unknown"
            host.processor_type = summary.processor_type or host.processor_type or "Unknown Processor"
            if not host.license_key:
                host.license_key = "See ESXi Portal"
            if not host.license_name:
                host.license_name = "Evaluation/Licensed"

            cpu_usage_percent = 0
            memory_usage_percent = 0
            try:
                host_obj = conn._get_host_object()
                quick = host_obj.summary.quickStats
                cpu_used_mhz = int(getattr(quick, "overallCpuUsage", 0) or 0)
                mem_used_mb = int(getattr(quick, "overallMemoryUsage", 0) or 0)
                total_cpu_mhz = int((host_obj.hardware.cpuInfo.hz / (10**6)) * host_obj.hardware.cpuInfo.numCpuCores)
                total_mem_mb = int(host_obj.hardware.memorySize / (1024**2))
                if total_cpu_mhz > 0:
                    cpu_usage_percent = round((cpu_used_mhz / total_cpu_mhz) * 100)
                if total_mem_mb > 0:
                    memory_usage_percent = round((mem_used_mb / total_mem_mb) * 100)
            except Exception:
                pass

            host.services_status = {
                "cpu_usage_percent": max(0, min(100, cpu_usage_percent)),
                "memory_usage_percent": max(0, min(100, memory_usage_percent)),
                "services": [
                    {
                        "name": svc.get("name", ""),
                        "status": svc.get("status", "Unknown"),
                    }
                    for svc in (conn.list_services() if hasattr(conn, "list_services") else [])
                ],
            }
            host.network_data = {
                "vswitches": [asdict(item) for item in conn.list_vswitches()],
                "portgroups": [asdict(item) for item in conn.list_portgroups()],
                "physical_nics": conn.list_physical_nics() if hasattr(conn, "list_physical_nics") else [],
                "vmkernel_nics": conn.list_vmkernel_nics() if hasattr(conn, "list_vmkernel_nics") else [],
                "tcp_ip_stacks": conn.list_tcp_ip_stacks() if hasattr(conn, "list_tcp_ip_stacks") else [],
                "firewall_rules": conn.list_firewall_rules() if hasattr(conn, "list_firewall_rules") else [],
            }
            datastores_ui = []
            for item in conn.list_datastores():
                ds = asdict(item)
                capacity_b = int(float(ds.get("capacity_gb", 0) or 0) * (1024 ** 3))
                free_b = int(float(ds.get("free_gb", 0) or 0) * (1024 ** 3))
                datastores_ui.append(
                    {
                        "name": ds.get("name", ""),
                        "type": ds.get("type", ""),
                        "capacity": capacity_b,
                        "used": max(0, capacity_b - free_b),
                        "free": free_b,
                        "mounted": True,
                    }
                )

            host.storage_data = {
                "datastores": datastores_ui,
                "raw_devices": conn.list_available_disks() if hasattr(conn, "list_available_disks") else [],
            }

            host.save(update_fields=[
                "cpu_count", "memory_gb", "vendor", "model_name", "os_version",
                "processor_type", "license_key", "license_name",
                "services_status", "network_data", "storage_data", "last_sync",
            ])
            print(
                f"✅ ESXi host '{host.name}' synced via API: "
                f"{host.cpu_count} CPUs | {host.memory_gb}GB RAM | "
                f"CPU {host.services_status.get('cpu_usage_percent', 0)}% | "
                f"RAM {host.services_status.get('memory_usage_percent', 0)}%"
            )
            return True
        except Exception as exc:
            print(f"❌ Error syncing ESXi host via API '{host.name}': {exc}")
            return False

    def _sync_vms_api(self, host: Any, conn: Any) -> int:
        from django.core.cache import cache
        from manager.models import VirtualMachine

        try:
            vms_data = conn.list_vms()
        except Exception as exc:
            print(f"❌ Error listing ESXi VMs via API on '{host.name}': {exc}")
            return 0

        if not vms_data:
            print(f"⚠️ No VM data returned for ESXi host {host.name} via API")
            return 0

        count = 0
        changed_count = 0
        deleted_count = 0
        esxi_vmids = set()

        for vm in vms_data:
            vm_dict = asdict(vm)
            vmid = vm_dict.get("uuid") or vm_dict.get("name")
            if not vmid:
                continue

            vmid = str(vmid)
            esxi_vmids.add(vmid)

            ip_addresses = [ip for ip in (vm_dict.get("ip_addresses") or []) if ip and ip != "0.0.0.0"]
            clean_networks = [{
                "network": "Unknown",
                "mac": "N/A",
                "ip": list(set(ip_addresses)),
            }]

            obj, created = VirtualMachine.objects.update_or_create(
                vmid=vmid,
                host=host,
                defaults={
                    "name": vm_dict.get("name") or vmid,
                    "uuid": vm_dict.get("uuid"),
                    "vmx_path": vm_dict.get("datastorage") or "",
                    "hw_version": None,
                    "power_state": str(vm_dict.get("power_state", "Unknown")),
                    "overall_status": "green",
                    "guest_os": vm_dict.get("guest_os") or "Unknown",
                    "distro": "N/A",
                    "kernel": "N/A",
                    "ip_address": next(iter(ip_addresses), None),
                    "dns_name": vm_dict.get("dns_name") or vm_dict.get("name") or "Unknown",
                    "tools_status": vm_dict.get("tools_status"),
                    "tools_running": str(vm_dict.get("tools_running")),
                    "networks": clean_networks,
                    "dns_servers": [
                        ip for ip in (vm_dict.get("dns_servers") or []) if ip
                    ],
                    "num_cpu": int(vm_dict.get("cpu_count", 0) or 0),
                    "memory_mb": int(vm_dict.get("memory_mb", 0) or 0),
                    "storage_used_gb": 0.0,
                    "storage_provisioned_gb": 0.0,
                    "cpu_usage_mhz": 0,
                    "mem_active_mb": 0,
                    "uptime_human": "N/A",
                },
            )

            cache_key = f"ninja:vm_details:{host.ip_address}:{vmid}"
            cache_payload = {
                **vm_dict,
                "networks": clean_networks,
                "dns_name": vm_dict.get("dns_name") or vm_dict.get("name") or "Unknown",
                "uptime_human": "N/A",
                "vmid": vmid,
            }
            new_hash = _hash_vm_state({
                "p": vm_dict.get("power_state"),
                "c": int(vm_dict.get("cpu_count", 0) or 0),
                "m": int(vm_dict.get("memory_mb", 0) or 0),
                "n": str(clean_networks),
            })
            if created or not cache.get(cache_key) or getattr(obj, "_last_hash", None) != new_hash:
                cache.set(cache_key, cache_payload, timeout=120)
                obj._last_hash = new_hash
                changed_count += 1
            count += 1

        for vm_obj in VirtualMachine.objects.filter(host=host).exclude(vmid__in=esxi_vmids):
            cache.delete(f"ninja:vm_details:{host.ip_address}:{vm_obj.vmid}")
            vm_obj.delete()
            deleted_count += 1

        print(
            f"   📊 ESXi VM Sync API [{host.name}]: "
            f"Changed={changed_count} | Deleted={deleted_count} | Total={count}"
        )
        return count

    # ------------------------------------------------------------------
    # Host sync — writes data into the standard Host fields so the UI
    # renders without any knowledge of ESXi specifics.
    # ------------------------------------------------------------------

    def sync_host(self, host: Any, conn: Any) -> bool:
        if (getattr(host, "esxi_connection_method", "") or "ssh").lower() == "api":
            return self._sync_host_api(host, conn)

        from lib import host as host_lib
        from lib import network
        from lib import storage
        from lib.host import services as host_services
        from lib.network import manage as net_manage

        try:
            hardware_data = host_lib.get_host_hardware(conn)
            summary_data = host_lib.get_host_summary(conn)
            usage_stats = host_lib.get_host_usage_stats(conn)
            license_data = host_lib.get_license_details(conn)

            if not hardware_data or not summary_data:
                print(f"⚠️ Incomplete data returned for ESXi host {host.name}")
                return False

            host.cpu_count = (
                hardware_data.get("cpu_count", host.cpu_count) or host.cpu_count
            )
            host.memory_gb = (
                hardware_data.get("memory_total_gb", host.memory_gb) or host.memory_gb
            )
            host.vendor = (
                hardware_data.get("vendor", host.vendor) or host.vendor or "VMware/Intel"
            )

            model = (summary_data.get("model", "") or "").strip()
            if model and model != "Unknown Model":
                host.model_name = model
            elif not host.model_name:
                host.model_name = "Unknown Model"

            os_ver = (summary_data.get("version", "") or "").strip()
            if os_ver:
                host.os_version = os_ver
            elif not host.os_version:
                host.os_version = "Unknown"

            processor = (license_data.get("product", "") or "").strip()
            if processor and processor != "Unknown Processor":
                host.processor_type = processor
            elif not host.processor_type:
                host.processor_type = "Unknown Processor"

            license_key = (license_data.get("key", "") or "").strip()
            if license_key:
                host.license_key = license_key
            elif not host.license_key:
                host.license_key = "See ESXi Portal"

            license_name = (license_data.get("status", "") or "").strip()
            if license_name:
                host.license_name = license_name
            elif not host.license_name:
                host.license_name = "Evaluation/Licensed"

            if host.services_status is None:
                host.services_status = {}
            host.services_status["cpu_usage_percent"] = usage_stats.get(
                "cpu_usage_percent", 0
            )
            host.services_status["memory_usage_percent"] = usage_stats.get(
                "memory_usage_percent", 0
            )
            try:
                host.services_status["services"] = (
                    host_services.list_services_with_status(conn)
                )
            except Exception as exc:
                print(f"⚠️  Error fetching services for {host.name}: {exc}")
                host.services_status["services"] = []

            try:
                host.network_data = {
                    "vswitches": network.list_vswitches(conn),
                    "portgroups": network.list_portgroups(conn),
                    "physical_nics": (
                        network.get_physical_nics(conn)
                        if hasattr(network, "get_physical_nics")
                        else []
                    ),
                    "vmkernel_nics": net_manage.get_vmkernel_nics(conn),
                    "tcp_ip_stacks": net_manage.get_tcp_ip_stacks(conn),
                    "firewall_rules": net_manage.get_firewall_rules(conn),
                }
            except Exception as exc:
                print(f"⚠️  Error fetching network data for {host.name}: {exc}")
                host.network_data = {
                    "vswitches": [],
                    "portgroups": [],
                    "physical_nics": [],
                    "vmkernel_nics": [],
                    "tcp_ip_stacks": [],
                    "firewall_rules": [],
                }

            try:
                host.storage_data = {
                    "datastores": storage.list_datastores(conn),
                    "raw_devices": (
                        storage.list_available_disks(conn)
                        if hasattr(storage, "list_available_disks")
                        else []
                    ),
                }
            except Exception as exc:
                print(f"⚠️  Error fetching storage data for {host.name}: {exc}")
                host.storage_data = {"datastores": [], "raw_devices": []}

            host.save(update_fields=[
                "cpu_count", "memory_gb", "vendor", "model_name", "os_version",
                "processor_type", "license_key", "license_name",
                "services_status", "network_data", "storage_data", "last_sync",
            ])
            print(
                f"✅ ESXi host '{host.name}' synced: "
                f"{host.cpu_count} CPUs | {host.memory_gb}GB RAM | "
                f"CPU {usage_stats.get('cpu_usage_percent', 0)}% | "
                f"RAM {usage_stats.get('memory_usage_percent', 0)}%"
            )
            return True
        except Exception as exc:
            print(f"❌ Error syncing ESXi host '{host.name}': {exc}")
            return False

    # ------------------------------------------------------------------
    # VM sync — writes to standard VirtualMachine fields so the UI
    # renders without any knowledge of ESXi.
    # ------------------------------------------------------------------

    def sync_vms(self, host: Any, conn: Any) -> int:
        if (getattr(host, "esxi_connection_method", "") or "ssh").lower() == "api":
            return self._sync_vms_api(host, conn)

        from django.core.cache import cache
        from manager.models import VirtualMachine
        from lib.vms import manage as vm_manage

        vms_data = vm_manage.list_vms_with_stats(conn)
        if not vms_data:
            print(f"⚠️ No VM data returned for ESXi host {host.name}")
            return 0

        count = 0
        changed_count = 0
        deleted_count = 0
        esxi_vmids = {vm.get("vmid") for vm in vms_data}

        for vm in vms_data:
            try:
                cpu_val = int(vm.get("cpu_usage_mhz", 0) or 0)
                mem_val = int(vm.get("memory_usage_mb", 0) or 0)
                used_gb = float(vm.get("storage_used_gb", 0.0) or 0.0)
                prov_gb = float(vm.get("storage_provisioned_gb", 0.0) or 0.0)
            except (ValueError, TypeError):
                cpu_val, mem_val, used_gb, prov_gb = 0, 0, 0.0, 0.0

            raw_networks = vm.get("networks", []) or []
            clean_networks = []
            for n in raw_networks:
                if not isinstance(n, dict):
                    continue
                ips = n.get("ip") or n.get("ip_address") or []
                if isinstance(ips, str):
                    ips = [ips]
                final_ips = [ip for ip in ips if ip and ip != "0.0.0.0"]
                clean_networks.append({
                    "network": n.get("network") or n.get("name") or "Unknown",
                    "mac": n.get("mac") or "N/A",
                    "ip": list(set(final_ips)),
                })

            dns_name = vm.get("dns_name") or vm.get("vm_name") or "Unknown"

            obj, created = VirtualMachine.objects.update_or_create(
                vmid=vm.get("vmid"),
                host=host,
                defaults={
                    "name": vm.get("vm_name"),
                    "uuid": vm.get("uuid"),
                    "vmx_path": vm.get("vmx", ""),
                    "hw_version": vm.get("hw_version"),
                    "power_state": vm.get("power_state", "Unknown"),
                    "overall_status": vm.get("overall_status", "green"),
                    "guest_os": vm.get("guest_name", "Unknown"),
                    "distro": vm.get("distro", "N/A"),
                    "kernel": vm.get("kernel", "N/A"),
                    "ip_address": (
                        vm.get("ip_address")
                        if vm.get("ip_address") != "N/A"
                        else None
                    ),
                    "dns_name": dns_name,
                    "tools_status": vm.get("tools_status"),
                    "tools_running": vm.get("tools_running"),
                    "networks": clean_networks,
                    "dns_servers": vm.get("dns_servers", []) or [],
                    "num_cpu": int(vm.get("num_cpu", 0) or 0),
                    "memory_mb": int(vm.get("memory_mb", 0) or 0),
                    "storage_used_gb": used_gb,
                    "storage_provisioned_gb": prov_gb,
                    "cpu_usage_mhz": cpu_val,
                    "mem_active_mb": mem_val,
                    "uptime_human": vm.get("uptime_human", "N/A"),
                },
            )

            cache_key = f"ninja:vm_details:{host.ip_address}:{vm.get('vmid')}"
            cache_payload = {
                **vm,
                "networks": clean_networks,
                "dns_name": dns_name,
                "uptime_human": vm.get("uptime_human", "N/A"),
            }
            new_hash = _hash_vm_state({
                "p": vm.get("power_state"),
                "c": cpu_val,
                "m": mem_val,
                "n": str(clean_networks),
            })
            if (
                created
                or not cache.get(cache_key)
                or getattr(obj, "_last_hash", None) != new_hash
            ):
                cache.set(cache_key, cache_payload, timeout=120)
                obj._last_hash = new_hash
                changed_count += 1
            count += 1

        # Remove VMs that no longer exist on the hypervisor.
        for vm_obj in VirtualMachine.objects.filter(host=host).exclude(
            vmid__in=esxi_vmids
        ):
            cache.delete(f"ninja:vm_details:{host.ip_address}:{vm_obj.vmid}")
            vm_obj.delete()
            deleted_count += 1

        print(
            f"   📊 ESXi VM Sync [{host.name}]: "
            f"Changed={changed_count} | Deleted={deleted_count} | Total={count}"
        )
        return count
