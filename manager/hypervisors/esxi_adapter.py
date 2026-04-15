from __future__ import annotations

import hashlib
import os
from typing import Any

from lib.connect.connect import ESXiConnect

from .base import HypervisorAdapter


def _hash_vm_state(state_dict: dict) -> str:
    return hashlib.md5(str(sorted(state_dict.items())).encode()).hexdigest()


class EsxiAdapter(HypervisorAdapter):
    slug = "vmware_esxi"
    display_name = "VMware ESXi"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def build_connection(self, host: Any) -> ESXiConnect:
        ssh_key_path = (
            os.getenv("ESXI_SSH_KEY_PATH")
            or os.getenv("SSH_KEY_PATH")
            or os.getenv("SSH_KEY_CONTAINER_PATH")
            or ("/app/nebula_rsa" if os.path.exists("/app/nebula_rsa") else None)
        )
        ssh_key_passphrase = os.getenv("ESXI_SSH_KEY_PASSPHRASE") or os.getenv("SSH_KEY_PASSPHRASE")
        return ESXiConnect(
            host=host.ip_address,
            user=host.username,
            key_filename=ssh_key_path,
            key_passphrase=ssh_key_passphrase,
        )

    # ------------------------------------------------------------------
    # Host sync — writes data into the standard Host fields so the UI
    # renders without any knowledge of ESXi specifics.
    # ------------------------------------------------------------------

    def sync_host(self, host: Any, conn: Any) -> bool:
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
