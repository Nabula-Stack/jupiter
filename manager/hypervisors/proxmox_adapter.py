from __future__ import annotations

import hashlib
import os
from typing import Any

from lib.proxmox import ProxmoxClient

from .base import HypervisorAdapter


def _hash_vm_state(state_dict: dict) -> str:
    return hashlib.md5(str(sorted(state_dict.items())).encode()).hexdigest()


class ProxmoxAdapter(HypervisorAdapter):
    slug = "proxmox_ve"
    display_name = "Proxmox VE"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def build_connection(self, host: Any) -> ProxmoxClient:
        verify_ssl = (
            os.getenv("PROXMOX_VERIFY_SSL", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        timeout = int(os.getenv("PROXMOX_TIMEOUT", "15"))
        return ProxmoxClient(
            host=str(host.ip_address),
            username=host.username,
            password=host.password,
            verify_ssl=verify_ssl,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Host sync — writes data into the standard Host fields so the UI
    # can render it without any knowledge of Proxmox.
    # ------------------------------------------------------------------

    def sync_host(self, host: Any, conn: Any) -> bool:
        try:
            node = conn.resolve_node(host.name)
            status = conn.get_node_status(node)
            version = conn.get_version()

            max_cpu = int(
                status.get("cpuinfo", {}).get("cpus")
                or status.get("maxcpu")
                or host.cpu_count
                or 0
            )
            max_mem = int(
                status.get("memory", {}).get("total") or status.get("maxmem") or 0
            )
            cpu_usage = float(status.get("cpu") or 0.0)
            mem_used = int(
                status.get("memory", {}).get("used") or status.get("mem") or 0
            )
            mem_gb = int(max_mem / (1024 ** 3)) if max_mem else int(host.memory_gb or 0)
            cpu_usage_percent = (
                round(cpu_usage * 100, 2) if cpu_usage <= 1 else round(cpu_usage, 2)
            )
            mem_usage_percent = (
                round((mem_used / max_mem) * 100, 2) if max_mem else 0
            )

            # Services
            services = []
            for svc in conn.list_services(node):
                name = str(svc.get("name") or svc.get("service") or "").strip()
                if not name:
                    continue
                state = str(svc.get("state") or svc.get("status") or "unknown").strip()
                services.append({"name": name, "status": state})

            # Network — map Proxmox iface types to the standard UI schema
            physical_nics: list[dict] = []
            vswitches: list[dict] = []
            portgroups: list[dict] = []
            vmkernel_nics: list[dict] = []
            for net in conn.list_network(node):
                iface = str(net.get("iface") or net.get("name") or "").strip()
                if not iface:
                    continue
                net_type = str(net.get("type") or "").lower()
                active = bool(net.get("active", False))
                mtu = net.get("mtu") or "--"
                address = net.get("address") or net.get("cidr") or "--"

                physical_nics.append({
                    "interface": iface,
                    "driver": net.get("method") or "--",
                    "admin_status": "up" if active else "down",
                    "link_status": "Up" if active else "Down",
                    "speed": "--",
                    "duplex": "--",
                    "mac": net.get("hwaddr") or "--",
                    "mtu": str(mtu),
                })
                if net_type == "bridge":
                    vswitches.append({
                        "name": iface,
                        "num_ports": "--",
                        "mtu": str(mtu),
                        "uplinks": [
                            p.strip()
                            for p in str(net.get("bridge_ports") or "").split()
                            if p.strip()
                        ],
                    })
                    vmkernel_nics.append({
                        "interface": iface,
                        "ip": address,
                        "netmask": str(net.get("netmask") or "--"),
                        "type": "management",
                        "mtu": str(mtu),
                        "enabled": "true" if active else "false",
                    })
                else:
                    portgroups.append({
                        "name": iface,
                        "vswitch": str(net.get("bridge") or "--"),
                        "vlan": str(net.get("vlan-id") or net.get("vlan") or "0"),
                        "mtu": str(mtu),
                    })

            # Storage
            datastores: list[dict] = []
            for ds in conn.list_storage(node):
                total = int(ds.get("total") or 0)
                used = int(ds.get("used") or 0)
                avail = int(ds.get("avail") or max(total - used, 0))
                datastores.append({
                    "name": str(ds.get("storage") or ds.get("name") or "unknown"),
                    "type": str(ds.get("type") or "storage"),
                    "capacity": total,
                    "used": used,
                    "free": avail,
                })

            # Persist — using the same field names as EsxiAdapter so the UI
            # renders identically regardless of vendor.
            host.cpu_count = max_cpu
            host.memory_gb = mem_gb
            host.vendor = "Proxmox"
            host.model_name = str(
                status.get("kversion") or host.model_name or "Proxmox VE Node"
            )
            host.os_version = str(version.get("version") or host.os_version or "Unknown")
            host.processor_type = str(
                status.get("cpuinfo", {}).get("model")
                or host.processor_type
                or "Unknown Processor"
            )
            host.license_name = "Community / Subscription"
            host.license_key = "Managed in Proxmox"
            host.services_status = {
                "cpu_usage_percent": cpu_usage_percent,
                "memory_usage_percent": mem_usage_percent,
                "services": services,
            }
            host.network_data = {
                "vswitches": vswitches,
                "portgroups": portgroups,
                "physical_nics": physical_nics,
                "vmkernel_nics": vmkernel_nics,
                "tcp_ip_stacks": [{
                    "name": "defaultTcpipStack",
                    "enabled": str(status.get("state") or "unknown"),
                    "ccalgo": "n/a",
                }],
                "firewall_rules": [],
            }
            host.storage_data = {
                "datastores": datastores,
                "raw_devices": "",
            }
            host.save(update_fields=[
                "cpu_count", "memory_gb", "vendor", "model_name", "os_version",
                "processor_type", "license_key", "license_name",
                "services_status", "network_data", "storage_data", "last_sync",
            ])
            print(
                f"✅ Proxmox host '{host.name}' synced: "
                f"{max_cpu} CPUs | {mem_gb}GB RAM | {len(datastores)} datastores"
            )
            return True
        except Exception as exc:
            print(f"❌ Error syncing Proxmox host '{host.name}': {exc}")
            return False

    # ------------------------------------------------------------------
    # VM sync — writes to the standard VirtualMachine fields so the UI
    # renders without any knowledge of Proxmox.
    # ------------------------------------------------------------------

    def sync_vms(self, host: Any, conn: Any) -> int:
        from django.core.cache import cache
        from manager.models import VirtualMachine

        node = conn.resolve_node(host.name)
        vm_rows = conn.list_vms(node)
        count = 0
        changed_count = 0
        deleted_count = 0
        remote_vmids: set[str] = set()

        for vm in vm_rows:
            vmid = str(vm.get("vmid") or "")
            if not vmid:
                continue
            remote_vmids.add(vmid)

            status_str = vm.get("status") or "stopped"
            power_state = (
                "poweredOn" if str(status_str).lower() == "running" else "poweredOff"
            )
            cpu_usage = float(vm.get("cpu") or 0.0)
            cpu_usage_mhz = (
                int(cpu_usage * 1000) if cpu_usage <= 1 else int(cpu_usage)
            )
            mem_active_mb = int(int(vm.get("mem") or 0) / (1024 ** 2))
            memory_mb = int(int(vm.get("maxmem") or 0) / (1024 ** 2))
            used_gb = float(int(vm.get("disk") or 0) / (1024 ** 3))
            provisioned_gb = float(int(vm.get("maxdisk") or 0) / (1024 ** 3))

            vm_status = conn.get_vm_status(node, vmid)
            vm_cfg = conn.get_vm_config(node, vmid)

            vm_name = vm.get("name") or vm_cfg.get("name") or f"vm-{vmid}"
            ip_address = (
                vm_status.get("ip")
                if isinstance(vm_status.get("ip"), str)
                else None
            )
            if ip_address in {"0.0.0.0", "N/A", ""}:
                ip_address = None

            obj, created = VirtualMachine.objects.update_or_create(
                vmid=vmid,
                host=host,
                defaults={
                    "name": vm_name,
                    "uuid": str(vm.get("digest") or vm_cfg.get("smbios1") or ""),
                    "vmx_path": str(vm_cfg.get("vmgenid") or ""),
                    "hw_version": str(vm_cfg.get("machine") or ""),
                    "power_state": power_state,
                    "overall_status": "green" if power_state == "poweredOn" else "gray",
                    "guest_os": str(vm_cfg.get("ostype") or "Unknown"),
                    "distro": "Proxmox Guest",
                    "kernel": str(vm_cfg.get("bios") or "N/A"),
                    "ip_address": ip_address,
                    "dns_name": vm_name,
                    "tools_status": conn.get_agent_status(node, vmid),
                    "tools_running": conn.get_agent_status(node, vmid),
                    "networks": [],
                    "dns_servers": [],
                    "num_cpu": int(vm.get("cpus") or vm_cfg.get("cores") or 0),
                    "memory_mb": memory_mb,
                    "storage_used_gb": round(used_gb, 2),
                    "storage_provisioned_gb": round(provisioned_gb, 2),
                    "cpu_usage_mhz": cpu_usage_mhz,
                    "mem_active_mb": mem_active_mb,
                    "uptime_human": str(vm.get("uptime") or "N/A"),
                },
            )

            cache_key = f"ninja:vm_details:{host.ip_address}:{vmid}"
            cache_payload = {
                "vmid": vmid,
                "vm_name": vm_name,
                "power_state": power_state,
                "cpu_usage_mhz": cpu_usage_mhz,
                "memory_usage_mb": mem_active_mb,
                "storage_used_gb": round(used_gb, 2),
                "storage_provisioned_gb": round(provisioned_gb, 2),
                "ip_address": ip_address or "N/A",
                "networks": [],
                "dns_name": vm_name,
            }
            new_hash = _hash_vm_state(
                {"p": power_state, "c": cpu_usage_mhz, "m": mem_active_mb}
            )
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
            vmid__in=remote_vmids
        ):
            cache.delete(f"ninja:vm_details:{host.ip_address}:{vm_obj.vmid}")
            vm_obj.delete()
            deleted_count += 1

        print(
            f"   📊 Proxmox VM Sync [{host.name}]: "
            f"Changed={changed_count} | Deleted={deleted_count} | Total={count}"
        )
        return count
