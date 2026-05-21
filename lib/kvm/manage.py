from __future__ import annotations

import json
import re
import shlex
from typing import Any


_VIRSH = "virsh -c qemu:///system"


def _virsh(command: str) -> str:
    return f"{_VIRSH} {command}"


def _run_or_raise(conn: Any, command: str) -> str:
    result = conn.run(command)
    if isinstance(result, str) and result.startswith("Error:"):
        raise RuntimeError(result)
    return str(result or "")


def _parse_kv_lines(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        out[key.strip()] = val.strip()
    return out


def _normalize_power_state(state: str) -> str:
    s = (state or "").strip().lower()
    if s == "running":
        return "poweredOn"
    if s in {"paused", "pmsuspended"}:
        return "suspended"
    return "poweredOff"


def _extract_first_ipv4(raw: str) -> str | None:
    for line in raw.splitlines():
        if "ipv4" not in line.lower():
            continue
        cols = [c for c in line.split() if c]
        for col in cols:
            if "/" in col and re.match(r"^\d+\.\d+\.\d+\.\d+/\d+$", col):
                return col.split("/", 1)[0]
    return None


def _safe_int(text: str, default: int = 0) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def _read_disk_sizes_bytes(conn: Any, path: str) -> tuple[int, int]:
    quoted = shlex.quote(path)
    raw = conn.run(f"qemu-img info --output=json {quoted}")
    if isinstance(raw, str) and raw.startswith("Error:"):
        return 0, 0
    try:
        info = json.loads(str(raw or "{}"))
        virtual_size = int(info.get("virtual-size") or 0)
        actual_size = int(info.get("actual-size") or 0)
        return virtual_size, actual_size
    except (ValueError, TypeError, json.JSONDecodeError):
        return 0, 0


def list_networks(conn: Any) -> list[str]:
    raw = conn.run(_virsh("net-list --all --name"))
    if isinstance(raw, str) and raw.startswith("Error:"):
        return []
    return [line.strip() for line in str(raw).splitlines() if line.strip()]


def list_storage_pools(conn: Any) -> list[dict[str, Any]]:
    raw = conn.run(_virsh("pool-list --all --name"))
    if isinstance(raw, str) and raw.startswith("Error:"):
        return []

    pools: list[dict[str, Any]] = []
    for pool in [line.strip() for line in str(raw).splitlines() if line.strip()]:
        info_raw = conn.run(_virsh(f"pool-info {shlex.quote(pool)}"))
        if isinstance(info_raw, str) and info_raw.startswith("Error:"):
            continue
        info = _parse_kv_lines(str(info_raw))
        cap = _safe_int(str(info.get("Capacity", "0")).split()[0])
        alloc = _safe_int(str(info.get("Allocation", "0")).split()[0])
        avail = _safe_int(str(info.get("Available", "0")).split()[0])
        pools.append(
            {
                "name": pool,
                "type": "libvirt-pool",
                "total": cap,
                "used": alloc,
                "free": avail,
                "state": info.get("State", "unknown"),
                "autostart": info.get("Autostart", "no"),
            }
        )
    return pools


def list_vms_with_stats(conn: Any) -> list[dict[str, Any]]:
    raw = conn.run(_virsh("list --all --name"))
    if isinstance(raw, str) and raw.startswith("Error:"):
        return []

    names = [line.strip() for line in str(raw).splitlines() if line.strip()]
    rows: list[dict[str, Any]] = []

    for name in names:
        qname = shlex.quote(name)
        dominfo_raw = conn.run(_virsh(f"dominfo {qname}"))
        if isinstance(dominfo_raw, str) and dominfo_raw.startswith("Error:"):
            continue
        dom = _parse_kv_lines(str(dominfo_raw))

        state = _normalize_power_state(str(dom.get("State", "")))
        num_cpu = _safe_int(dom.get("CPU(s)", "0"), 0)
        memory_mb = int(_safe_int(str(dom.get("Max memory", "0")).split()[0], 0) / 1024)

        ip_raw = conn.run(
            f"sh -lc \"{_VIRSH} domifaddr {qname} --source agent 2>/dev/null || {_VIRSH} domifaddr {qname} --source arp 2>/dev/null\""
        )
        ip_addr = None if isinstance(ip_raw, str) and ip_raw.startswith("Error:") else _extract_first_ipv4(str(ip_raw or ""))

        provisioned_bytes = 0
        used_bytes = 0
        blklist_raw = conn.run(_virsh(f"domblklist --details {qname}"))
        if not (isinstance(blklist_raw, str) and blklist_raw.startswith("Error:")):
            for line in str(blklist_raw).splitlines():
                line = line.strip()
                if not line or line.lower().startswith("type") or line.startswith("-"):
                    continue
                cols = [c for c in line.split() if c]
                if len(cols) < 4:
                    continue
                if cols[1].lower() != "disk":
                    continue
                source_path = cols[3]
                if source_path in {"-", "none"}:
                    continue
                virt_b, used_b = _read_disk_sizes_bytes(conn, source_path)
                provisioned_bytes += virt_b
                used_bytes += used_b

        rows.append(
            {
                "vmid": name,
                "vm_name": name,
                "uuid": str(dom.get("UUID") or ""),
                "vmx": "",
                "hw_version": "kvm",
                "power_state": state,
                "overall_status": "green" if state == "poweredOn" else "gray",
                "guest_name": "KVM Guest",
                "distro": "Linux",
                "kernel": "N/A",
                "ip_address": ip_addr,
                "dns_name": name,
                "tools_status": "n/a",
                "tools_running": "n/a",
                "networks": [],
                "dns_servers": [],
                "num_cpu": num_cpu,
                "memory_mb": memory_mb,
                "storage_used_gb": round(used_bytes / (1024 ** 3), 2),
                "storage_provisioned_gb": round(provisioned_bytes / (1024 ** 3), 2),
                "cpu_usage_mhz": 0,
                "memory_usage_mb": 0,
                "uptime_human": "N/A",
            }
        )

    return rows


def create_vm(
    conn: Any,
    datastore: str,
    vm_name: str,
    ram_mb: int = 2048,
    cpu_count: int = 2,
    disk_size_gb: int = 16,
    network_name: str = "default",
    nic_type: str = "virtio",
    power_on: bool = False,
) -> str:
    name = str(vm_name or "").strip()
    pool = str(datastore or "").strip()
    if not name:
        raise ValueError("VM name is required")
    if not pool:
        raise ValueError("Datastore (libvirt storage pool) is required")

    check_cmd = (
        "sh -lc \"command -v virt-install >/dev/null 2>&1 "
        "&& command -v qemu-img >/dev/null 2>&1 && echo OK || echo MISSING\""
    )
    tools = conn.run(check_cmd)
    if "OK" not in str(tools):
        raise RuntimeError("KVM host is missing virt-install or qemu-img")

    net_names = list_networks(conn)
    if not net_names:
        network = "default"
    elif network_name in net_names:
        network = network_name
    else:
        network = net_names[0]

    pool_path_raw = _run_or_raise(
        conn,
        f"sh -lc \"{_VIRSH} pool-dumpxml {shlex.quote(pool)} | sed -n 's:.*<path>\\(.*\\)</path>.*:\\1:p' | head -n1\"",
    )
    pool_path = str(pool_path_raw).strip()
    if not pool_path:
        raise RuntimeError(f"Could not resolve path for storage pool '{pool}'")

    disk_path = f"{pool_path.rstrip('/')}/{name}.qcow2"
    _run_or_raise(conn, f"qemu-img create -f qcow2 {shlex.quote(disk_path)} {int(disk_size_gb)}G")

    install_cmd = (
        "virt-install "
        f"--name {shlex.quote(name)} "
        f"--memory {int(ram_mb)} "
        f"--vcpus {max(int(cpu_count), 1)} "
        f"--disk path={shlex.quote(disk_path)},format=qcow2,bus=virtio "
        f"--network network={shlex.quote(network)},model={shlex.quote(str(nic_type or 'virtio'))} "
        "--graphics none --noautoconsole --os-variant generic --import"
    )
    _run_or_raise(conn, install_cmd)

    if not power_on:
        conn.run(_virsh(f"destroy {shlex.quote(name)}"))

    return f"KVM VM '{name}' created in pool '{pool}'"


def power_op(conn: Any, vmid: str, state: str) -> str:
    target = shlex.quote(str(vmid))
    action = str(state or "").strip().lower()
    mapping = {
        "power.on": _virsh(f"start {target}"),
        "power.off": _virsh(f"destroy {target}"),
        "power.shutdown": _virsh(f"shutdown {target}"),
        "power.reset": _virsh(f"reset {target}"),
        "power.reboot": _virsh(f"reboot {target}"),
        "power.suspend": _virsh(f"suspend {target}"),
        "resume": _virsh(f"resume {target}"),
    }
    if action not in mapping:
        raise ValueError(f"Unsupported KVM power action: {state}")
    return _run_or_raise(conn, mapping[action])


def snapshot_op(conn: Any, vmid: str, op: str, name: str | None = None, description: str = "Admin Snapshot") -> str:
    target = shlex.quote(str(vmid))
    action = str(op or "").strip().lower()
    snap_name = shlex.quote(name or "snap-admin")

    if action == "create":
        return _run_or_raise(
            conn,
            (
                f"{_VIRSH} snapshot-create-as {target} {snap_name} {shlex.quote(description)} "
                "--atomic --disk-only --quiesce"
            ),
        )
    if action == "list":
        return _run_or_raise(conn, _virsh(f"snapshot-list {target}"))
    if action in {"remove", "delete"}:
        return _run_or_raise(conn, _virsh(f"snapshot-delete {target} --snapshotname {snap_name}"))
    if action in {"revert", "restore"}:
        return _run_or_raise(conn, _virsh(f"snapshot-revert {target} --snapshotname {snap_name}"))
    if action in {"removeall", "delete_all"}:
        return _run_or_raise(conn, f"sh -lc \"for s in $({_VIRSH} snapshot-list {target} --name); do {_VIRSH} snapshot-delete {target} --snapshotname $s; done\"")
    raise ValueError(f"Unsupported KVM snapshot operation: {op}")


def unregister_vm(conn: Any, vmid: str) -> str:
    target = shlex.quote(str(vmid))
    # Keep storage by default for unregister semantics.
    return _run_or_raise(conn, _virsh(f"undefine {target}"))


def delete_vm(conn: Any, vmid: str) -> str:
    target = shlex.quote(str(vmid))

    # Best effort stop first.
    conn.run(_virsh(f"destroy {target}"))

    # Try full cleanup, then graceful fallbacks.
    for cmd in (
        _virsh(f"undefine {target} --nvram --remove-all-storage"),
        _virsh(f"undefine {target} --remove-all-storage"),
        _virsh(f"undefine {target}"),
    ):
        result = conn.run(cmd)
        if not (isinstance(result, str) and result.startswith("Error:")):
            return str(result)

    raise RuntimeError(f"Failed to delete KVM VM {vmid}")


def get_vm_hardware(conn: Any, vmid: str) -> dict[str, Any]:
    target = shlex.quote(str(vmid))
    dominfo_raw = _run_or_raise(conn, _virsh(f"dominfo {target}"))
    dom = _parse_kv_lines(dominfo_raw)

    net_raw = conn.run(_virsh(f"domiflist {target}"))
    nics: list[dict[str, Any]] = []
    if not (isinstance(net_raw, str) and net_raw.startswith("Error:")):
        for line in str(net_raw).splitlines():
            line = line.strip()
            if not line or line.lower().startswith("interface") or line.startswith("-"):
                continue
            cols = [c for c in line.split() if c]
            if len(cols) < 5:
                continue
            nics.append(
                {
                    "index": len(nics),
                    "network": cols[2],
                    "type": cols[1],
                    "mac": cols[4],
                    "connected": True,
                }
            )

    disks: list[dict[str, Any]] = []
    blk_raw = conn.run(_virsh(f"domblklist --details {target}"))
    if not (isinstance(blk_raw, str) and blk_raw.startswith("Error:")):
        for line in str(blk_raw).splitlines():
            line = line.strip()
            if not line or line.lower().startswith("type") or line.startswith("-"):
                continue
            cols = [c for c in line.split() if c]
            if len(cols) < 4 or cols[1].lower() != "disk":
                continue
            source = cols[3]
            virt_b, _ = _read_disk_sizes_bytes(conn, source)
            disks.append(
                {
                    "unit": len(disks),
                    "label": cols[2],
                    "file": source,
                    "size_gb": int(round(virt_b / (1024 ** 3))) if virt_b else 0,
                }
            )

    power_state = _normalize_power_state(str(dom.get("State", "")))
    return {
        "status": "success",
        "power_state": power_state,
        "nics": nics,
        "disks": disks,
        "cdrom": None,
        "cpu_hotplug": False,
        "memory_hotplug": False,
        "hardware_virtualization": True,
        "pci_passthrough": [],
    }
