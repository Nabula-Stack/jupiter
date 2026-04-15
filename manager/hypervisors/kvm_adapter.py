from __future__ import annotations

import hashlib
import os
from typing import Any

from lib.connect.connect import ESXiConnect
from lib.kvm import manage as kvm_manage

from .base import HypervisorAdapter


def _hash_vm_state(state_dict: dict[str, Any]) -> str:
    return hashlib.md5(str(sorted(state_dict.items())).encode()).hexdigest()


class KvmLibvirtAdapter(HypervisorAdapter):
    slug = "kvm_libvirt"
    display_name = "KVM/libvirt"

    def build_connection(self, host: Any) -> ESXiConnect:
        ssh_key_path = (
            os.getenv("SSH_KEY_PATH")
            or os.getenv("SSH_KEY_CONTAINER_PATH")
            or ("/app/nebula_rsa" if os.path.exists("/app/nebula_rsa") else None)
        )
        ssh_key_passphrase = os.getenv("SSH_KEY_PASSPHRASE")
        return ESXiConnect(
            host=host.ip_address,
            user=host.username,
            key_filename=ssh_key_path,
            key_passphrase=ssh_key_passphrase,
        )

    def sync_host(self, host: Any, conn: Any) -> bool:
        try:
            cpus_raw = conn.run("nproc")
            mem_raw = conn.run("awk '/MemTotal/ {print $2}' /proc/meminfo")
            kernel = conn.run("uname -r")
            os_info = conn.run("sh -lc \"hostnamectl 2>/dev/null | sed -n 's/^Operating System: //p' | head -n1\"")
            cpu_model = conn.run("sh -lc \"lscpu | sed -n 's/^Model name:\\s*//p' | head -n1\"")

            cpu_count = int(str(cpus_raw).strip() or 0)
            memory_gb = int(int(str(mem_raw).strip() or 0) / (1024 ** 2))

            services = []
            libvirtd_state = conn.run("sh -lc \"systemctl is-active libvirtd 2>/dev/null || systemctl is-active virtqemud 2>/dev/null || echo unknown\"")
            services.append({"name": "libvirt", "status": str(libvirtd_state).strip()})

            pools = kvm_manage.list_storage_pools(conn)
            networks = kvm_manage.list_networks(conn)

            host.cpu_count = cpu_count
            host.memory_gb = memory_gb
            host.vendor = "KVM/libvirt"
            host.model_name = "Linux KVM Host"
            host.os_version = str(os_info).strip() or "Unknown"
            host.processor_type = str(cpu_model).strip() or "Unknown Processor"
            host.license_name = "Open Source"
            host.license_key = "N/A"
            host.services_status = {
                "cpu_usage_percent": 0,
                "memory_usage_percent": 0,
                "services": services,
            }
            host.network_data = {
                "vswitches": [{"name": net, "num_ports": "--", "mtu": "--", "uplinks": []} for net in networks],
                "portgroups": [{"name": net, "vswitch": net, "vlan": "0", "mtu": "--"} for net in networks],
                "physical_nics": [],
                "vmkernel_nics": [],
                "tcp_ip_stacks": [{"name": "default", "enabled": "active", "ccalgo": "n/a"}],
                "firewall_rules": [],
            }
            host.storage_data = {
                "datastores": pools,
                "raw_devices": "",
            }
            host.save(update_fields=[
                "cpu_count", "memory_gb", "vendor", "model_name", "os_version",
                "processor_type", "license_key", "license_name",
                "services_status", "network_data", "storage_data", "last_sync",
            ])
            print(
                f"KVM host '{host.name}' synced: "
                f"{cpu_count} CPUs | {memory_gb}GB RAM | kernel {str(kernel).strip()}"
            )
            return True
        except Exception as exc:
            print(f"Error syncing KVM host '{host.name}': {exc}")
            return False

    def sync_vms(self, host: Any, conn: Any) -> int:
        from django.core.cache import cache
        from manager.models import VirtualMachine

        rows = kvm_manage.list_vms_with_stats(conn)
        count = 0
        changed_count = 0
        deleted_count = 0
        remote_ids: set[str] = set()

        for vm in rows:
            vmid = str(vm.get("vmid") or "")
            if not vmid:
                continue
            remote_ids.add(vmid)

            obj, created = VirtualMachine.objects.update_or_create(
                vmid=vmid,
                host=host,
                defaults={
                    "name": vm.get("vm_name") or vmid,
                    "uuid": vm.get("uuid") or "",
                    "vmx_path": vm.get("vmx") or "",
                    "hw_version": vm.get("hw_version") or "kvm",
                    "power_state": vm.get("power_state") or "poweredOff",
                    "overall_status": vm.get("overall_status") or "gray",
                    "guest_os": vm.get("guest_name") or "KVM Guest",
                    "distro": vm.get("distro") or "Linux",
                    "kernel": vm.get("kernel") or "N/A",
                    "ip_address": vm.get("ip_address"),
                    "dns_name": vm.get("dns_name") or vmid,
                    "tools_status": vm.get("tools_status") or "n/a",
                    "tools_running": vm.get("tools_running") or "n/a",
                    "networks": vm.get("networks") or [],
                    "dns_servers": vm.get("dns_servers") or [],
                    "num_cpu": int(vm.get("num_cpu") or 0),
                    "memory_mb": int(vm.get("memory_mb") or 0),
                    "storage_used_gb": float(vm.get("storage_used_gb") or 0.0),
                    "storage_provisioned_gb": float(vm.get("storage_provisioned_gb") or 0.0),
                    "cpu_usage_mhz": int(vm.get("cpu_usage_mhz") or 0),
                    "mem_active_mb": int(vm.get("memory_usage_mb") or 0),
                    "uptime_human": vm.get("uptime_human") or "N/A",
                },
            )

            cache_key = f"ninja:vm_details:{host.ip_address}:{vmid}"
            cache_payload = {
                **vm,
                "vmid": vmid,
                "vm_name": vm.get("vm_name") or vmid,
            }
            new_hash = _hash_vm_state(
                {
                    "p": vm.get("power_state"),
                    "c": int(vm.get("cpu_usage_mhz") or 0),
                    "m": int(vm.get("memory_usage_mb") or 0),
                }
            )
            if created or not cache.get(cache_key) or getattr(obj, "_last_hash", None) != new_hash:
                cache.set(cache_key, cache_payload, timeout=120)
                obj._last_hash = new_hash
                changed_count += 1
            count += 1

        for vm_obj in VirtualMachine.objects.filter(host=host).exclude(vmid__in=remote_ids):
            cache.delete(f"ninja:vm_details:{host.ip_address}:{vm_obj.vmid}")
            vm_obj.delete()
            deleted_count += 1

        print(
            f"KVM VM Sync [{host.name}]: "
            f"Changed={changed_count} | Deleted={deleted_count} | Total={count}"
        )
        return count
