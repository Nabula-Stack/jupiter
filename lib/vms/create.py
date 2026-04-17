import shlex
import re
import os


def _vmx_escape(value: object) -> str:
    """Escapes VMX string values to avoid malformed .vmx entries."""
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run_or_raise(host, command: str, timeout: int | None = None) -> str:
    """Runs a command on ESXi and raises if shell call fails."""
    result = host.run(command, timeout=timeout)
    if isinstance(result, str) and result.startswith("Error:"):
        raise RuntimeError(result)
    return result


def _detect_host_cpu_mhz(host) -> int:
    """Best-effort detection of host CPU speed in MHz for reservation math."""
    probes = [
        "esxcli hardware cpu list | grep -m1 'CPU Speed:'",
        "esxcli hardware cpu global get | grep -E 'Hz|hz|MHz|mhz' | head -1",
    ]
    for cmd in probes:
        try:
            out = str(host.run(cmd) or "")
        except Exception:
            continue
        if out.startswith("Error:"):
            continue
        mhz_match = re.search(r"(\d+)\s*MHz", out, flags=re.IGNORECASE)
        if mhz_match:
            return int(mhz_match.group(1))
        hz_match = re.search(r"(\d{7,})\s*Hz", out, flags=re.IGNORECASE)
        if hz_match:
            return int(int(hz_match.group(1)) / 1_000_000)
    return 1000


def create_vm(
    host,
    datastore: str,
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
    extra_disks: list | None = None,
    extra_nics: list | None = None,
    cpu_hotplug: bool = False,
    memory_hotplug: bool = False,
    hardware_virtualization: bool = False,
    pci_passthrough_devices: list | None = None,
    reserve_all_cpu: bool = False,
    reserve_all_memory: bool = False,
) -> str:
    """
    Creates and registers a VM with configurable options (ESXi-style).
    """
    vm_name = str(vm_name).strip()
    datastore = str(datastore).strip()
    if not vm_name:
        raise ValueError("VM name is required")
    if not datastore:
        raise ValueError("Datastore is required")

    vm_path = f"/vmfs/volumes/{datastore}/{vm_name}"
    vmx_path = f"{vm_path}/{vm_name}.vmx"
    vmdk_path = f"{vm_path}/{vm_name}.vmdk"

    vm_path_q = shlex.quote(vm_path)
    vmx_path_q = shlex.quote(vmx_path)
    vmdk_path_q = shlex.quote(vmdk_path)

    disk_format = str(disk_type).lower()
    if disk_format not in {"thin", "zeroedthick", "eagerzeroedthick"}:
        disk_format = "thin"

    nic_model = str(nic_type).lower()
    if nic_model not in {"e1000", "e1000e", "vmxnet3"}:
        nic_model = "e1000"

    scsi_model = str(scsi_controller).lower()
    if scsi_model not in {"lsilogic", "lsisas1068", "pvscsi"}:
        scsi_model = "lsilogic"

    firmware_mode = str(firmware).lower()
    if firmware_mode not in {"bios", "efi"}:
        firmware_mode = "bios"

    hw_version = str(hw_version).strip() or "13"
    host_cpu_mhz = _detect_host_cpu_mhz(host)
    cpu_reservation_mhz = int(cpu_count) * int(host_cpu_mhz)

    # Pre-flight: refuse to clobber an existing VM directory.
    exists_check = host.run(f"[ -d {vm_path_q} ] && echo EXISTS || echo OK")
    if "EXISTS" in str(exists_check):
        raise FileExistsError(
            f"VM directory already exists: {vm_path}. "
            "Choose a different name or delete the existing directory first."
        )

    # 1. Create VM directory.
    _run_or_raise(host, f"mkdir -p {vm_path_q}")

    # 2. Create the primary VMDK.
    _run_or_raise(host, f"vmkfstools -c {int(disk_size_gb)}G -d {disk_format} {vmdk_path_q}")

    # 2b. Create extra disks.
    extra_disk_entries = []
    for i, ed in enumerate(extra_disks or [], start=1):
        ed_size = int(ed.get("size_gb", 16))
        ed_fmt = str(ed.get("type", disk_format)).lower()
        if ed_fmt not in {"thin", "zeroedthick", "eagerzeroedthick"}:
            ed_fmt = "thin"
        ed_ds = str(ed.get("datastore", "")).strip()
        ed_name = f"{vm_name}_{i}"
        if ed_ds and ed_ds != datastore:
            # Disk on a different datastore — create in its own folder there
            ed_dir = f"/vmfs/volumes/{ed_ds}/{vm_name}"
            _run_or_raise(host, f"mkdir -p {shlex.quote(ed_dir)}")
            ed_vmdk = f"{ed_dir}/{ed_name}.vmdk"
            ed_vmx_file = f"/vmfs/volumes/{ed_ds}/{vm_name}/{ed_name}.vmdk"
        else:
            # Same datastore as the VM — keep in VM directory
            ed_vmdk = f"{vm_path}/{ed_name}.vmdk"
            ed_vmx_file = f"{ed_name}.vmdk"
        _run_or_raise(host, f"vmkfstools -c {ed_size}G -d {ed_fmt} {shlex.quote(ed_vmdk)}")
        extra_disk_entries.append((i, ed_vmx_file))

    # 3. Build VMX config.
    vmx_content = [
        '.encoding = "UTF-8"',
        'config.version = "8"',
        f'virtualHW.version = "{_vmx_escape(hw_version)}"',
        f'memsize = "{int(ram_mb)}"',
        f'numvcpus = "{int(cpu_count)}"',
        f'vcpu.hotadd = "{"TRUE" if cpu_hotplug else "FALSE"}"',
        f'mem.hotadd = "{"TRUE" if memory_hotplug else "FALSE"}"',
        f'vhv.enable = "{"TRUE" if hardware_virtualization else "FALSE"}"',
        f'sched.mem.min = "{int(ram_mb) if reserve_all_memory else 0}"',
        f'sched.cpu.min = "{cpu_reservation_mhz if reserve_all_cpu else 0}"',
        f'displayName = "{_vmx_escape(vm_name)}"',
        f'guestOS = "{_vmx_escape(guest_os)}"',
        f'firmware = "{_vmx_escape(firmware_mode)}"',
        'scsi0.present = "TRUE"',
        f'scsi0.virtualDev = "{_vmx_escape(scsi_model)}"',
        'scsi0:0.present = "TRUE"',
        f'scsi0:0.fileName = "{_vmx_escape(vm_name)}.vmdk"',
        'scsi0:0.deviceType = "scsi-hardDisk"',
    ]

    # Extra disks on scsi0:1, scsi0:2, ...
    for unit, ed_file in extra_disk_entries:
        vmx_content += [
            f'scsi0:{unit}.present = "TRUE"',
            f'scsi0:{unit}.fileName = "{_vmx_escape(ed_file)}"',
            f'scsi0:{unit}.deviceType = "scsi-hardDisk"',
        ]

    # Primary NIC (ethernet0)
    vmx_content += [
        'ethernet0.present = "TRUE"',
        f'ethernet0.virtualDev = "{_vmx_escape(nic_model)}"',
        f'ethernet0.networkName = "{_vmx_escape(network_name)}"',
        'ethernet0.addressType = "generated"',
    ]

    # Extra NICs (ethernet1, ethernet2, ...)
    for i, en in enumerate(extra_nics or [], start=1):
        en_type = str(en.get("type", "e1000")).lower()
        if en_type not in {"e1000", "e1000e", "vmxnet3"}:
            en_type = "e1000"
        en_net = str(en.get("network", "VM Network"))
        vmx_content += [
            f'ethernet{i}.present = "TRUE"',
            f'ethernet{i}.virtualDev = "{_vmx_escape(en_type)}"',
            f'ethernet{i}.networkName = "{_vmx_escape(en_net)}"',
            f'ethernet{i}.addressType = "generated"',
        ]
    # CD-ROM: mount ISO if provided, otherwise add empty virtual drive.
    if cd_iso_path:
        vmx_content += [
            'ide1:0.present = "TRUE"',
            f'ide1:0.fileName = "{_vmx_escape(cd_iso_path)}"',
            'ide1:0.deviceType = "cdrom-image"',
            'ide1:0.startConnected = "TRUE"',
        ]
    else:
        vmx_content += [
            'ide1:0.present = "TRUE"',
            'ide1:0.deviceType = "atapi-cdrom"',
            'ide1:0.startConnected = "FALSE"',
        ]

    # Optional initial PCI passthrough assignments.
    for pci_idx, pci_id in enumerate(pci_passthrough_devices or []):
        if not pci_id:
            continue
        vmx_content += [
            f'pciPassthru{pci_idx}.present = "TRUE"',
            f'pciPassthru{pci_idx}.id = "{_vmx_escape(str(pci_id))}"',
        ]

    vmx_body = "\n".join(vmx_content) + "\n"
    _run_or_raise(host, f"cat > {vmx_path_q} <<'EOF'\n{vmx_body}EOF")

    # 4. Register in inventory.
    register_output = _run_or_raise(host, f"vim-cmd solo/registervm {vmx_path_q}")

    # 5. Optional power on — failure here is a warning, not a fatal error.
    power_on_warning = None
    if power_on:
        try:
            vmid_output = _run_or_raise(host, "vim-cmd vmsvc/getallvms")
            created_vmid = None
            for line in str(vmid_output).splitlines()[1:]:
                if vm_name in line and f"{vm_name}.vmx" in line:
                    created_vmid = line.split()[0]
                    break
            if created_vmid:
                _run_or_raise(host, f"vim-cmd vmsvc/power.on {created_vmid}")
        except RuntimeError as exc:
            # VM was created and registered successfully; only power-on failed.
            power_on_warning = str(exc).split("\n")[0]  # first line only

    return register_output, power_on_warning


def deploy_ova(host, datastore: str, ova_local_path: str, vm_name: str = "") -> str:
    """
    Deploy an OVA/OVF to an ESXi host.

    Uploads the OVA to the datastore, extracts it, locates the OVF,
    converts any VMDK stream-optimized disks, and registers the VM.
    """
    import os
    datastore = str(datastore).strip().strip("/")
    if not datastore:
        raise ValueError("Datastore is required")
    ds_path = f"/vmfs/volumes/{datastore}"

    # Determine VM name from filename if not given
    ova_basename = os.path.basename(ova_local_path)
    if not vm_name:
        vm_name = ova_basename.rsplit(".", 1)[0]
    # Always strip .ova/.ovf extension if user included it
    for ext in (".ova", ".ovf", ".OVA", ".OVF"):
        if vm_name.endswith(ext):
            vm_name = vm_name[:-len(ext)]
            break
    vm_name = vm_name.strip().replace(" ", "_")

    vm_dir = f"{ds_path}/{vm_name}"
    vm_dir_q = shlex.quote(vm_dir)

    # Check if VM dir already exists
    exists_check = host.run(f"[ -d {vm_dir_q} ] && echo EXISTS || echo OK")
    if "EXISTS" in str(exists_check):
        raise FileExistsError(f"Directory already exists: {vm_dir}")

    _run_or_raise(host, f"mkdir -p {vm_dir_q}", timeout=120)

    # The OVA should already be uploaded to the datastore by the API layer.
    # ova_local_path here is the remote path on ESXi.
    remote_ova = ova_local_path

    # Extract OVA (it's a tar archive)
    # OVA extraction can be large and slow; use a generous timeout.
    _run_or_raise(host, f"tar xf {shlex.quote(remote_ova)} -C {vm_dir_q}", timeout=3600)

    # Find the OVF file
    ovf_find = host.run(f"ls {vm_dir_q}/*.ovf 2>/dev/null")
    if not ovf_find or ovf_find.startswith("Error:"):
        raise RuntimeError("No .ovf file found in OVA archive")
    ovf_path = ovf_find.strip().splitlines()[0]

    # Find all VMDKs and convert from stream-optimized to flat/thin
    vmdk_find = host.run(f"ls {vm_dir_q}/*.vmdk 2>/dev/null")
    vmdk_files = [f.strip() for f in (vmdk_find or "").splitlines() if f.strip().endswith(".vmdk")]

    for vmdk in vmdk_files:
        vmdk_q = shlex.quote(vmdk)
        converted = vmdk.replace(".vmdk", "-converted.vmdk")
        converted_q = shlex.quote(converted)
        # Clone/convert to thin (handles stream-optimized → flat)
        result = host.run(f"vmkfstools -i {vmdk_q} -d thin {converted_q}", timeout=7200)
        if isinstance(result, str) and result.startswith("Error:"):
            # If conversion fails, the disk may already be flat — skip
            continue
        # Replace original with converted
        _run_or_raise(host, f"rm -f {vmdk_q}")
        _run_or_raise(host, f"mv {converted_q} {vmdk_q}")
        # Also move the flat file if created
        flat_file = converted.replace(".vmdk", "-flat.vmdk")
        orig_flat = vmdk.replace(".vmdk", "-flat.vmdk")
        host.run(f"[ -f {shlex.quote(flat_file)} ] && mv {shlex.quote(flat_file)} {shlex.quote(orig_flat)}")

    # Find VMX if present, otherwise create a minimal one from OVF
    vmx_find = host.run(f"ls {vm_dir_q}/*.vmx 2>/dev/null")
    if vmx_find and not vmx_find.startswith("Error:") and vmx_find.strip():
        vmx_path = vmx_find.strip().splitlines()[0]
    else:
        # No VMX — create a basic one referencing the OVF's disks
        vmx_path = f"{vm_dir}/{vm_name}.vmx"
        vmdk_refs = []
        for idx, vmdk in enumerate(vmdk_files):
            vmdk_name = os.path.basename(vmdk)
            vmdk_refs.append(
                f'scsi0:{idx}.present = "TRUE"\n'
                f'scsi0:{idx}.fileName = "{_vmx_escape(vmdk_name)}"\n'
                f'scsi0:{idx}.deviceType = "scsi-hardDisk"'
            )
        vmx_body = "\n".join([
            '.encoding = "UTF-8"',
            'config.version = "8"',
            'virtualHW.version = "13"',
            'memsize = "2048"',
            'numvcpus = "2"',
            f'displayName = "{_vmx_escape(vm_name)}"',
            'guestOS = "other-64"',
            'scsi0.present = "TRUE"',
            'scsi0.virtualDev = "lsilogic"',
        ] + vmdk_refs + [
            'ethernet0.present = "TRUE"',
            'ethernet0.virtualDev = "e1000"',
            'ethernet0.networkName = "VM Network"',
            'ethernet0.addressType = "generated"',
        ]) + "\n"
        _run_or_raise(host, f"cat > {shlex.quote(vmx_path)} <<'EOF'\n{vmx_body}EOF")

    # Register the VM
    register_output = _run_or_raise(host, f"vim-cmd solo/registervm {shlex.quote(vmx_path)}", timeout=180)

    # Clean up the uploaded OVA tar to save space
    host.run(f"rm -f {shlex.quote(remote_ova)}", timeout=120)

    return f"OVA deployed as '{vm_name}'. {register_output}"


def deploy_ova_from_session(
    host,
    datastore: str,
    session_dir: str,
    vm_name: str,
    disk_files: list | None = None,
    cpu_count: int = 2,
    ram_mb: int = 2048,
    network_name: str = "VM Network",
    nic_type: str = "e1000",
    scsi_controller: str = "lsilogic",
    guest_os: str = "other-64",
    firmware: str = "bios",
    hw_version: str = "13",
    disk_type: str = "thin",
    extra_nics: list | None = None,
    power_on: bool = False,
) -> str:
    """Create/register VM from a prepared OVA session directory using user-edited settings."""
    def _is_descriptor_file(vmdk_path: str) -> bool:
        try:
            header = host.run(f"head -20 {shlex.quote(vmdk_path)}")
            text = str(header or "")
            if text and not text.startswith("Error:"):
                if ("Disk DescriptorFile" in text) or ("ddb." in text):
                    return True

            probe = host.run(f"vmkfstools -q {shlex.quote(vmdk_path)}")
            probe_text = str(probe or "")
            if not probe_text:
                return True
            if probe_text.startswith("Error:"):
                return False
            lowered = probe_text.lower()
            if "not a virtual disk" in lowered or "invalid argument" in lowered:
                return False
            return True
        except Exception:
            return False

    vm_name = str(vm_name or "").strip().replace(" ", "_")
    datastore = str(datastore or "").strip().strip("/")
    session_dir = str(session_dir or "").strip().rstrip("/")
    if not vm_name:
        raise ValueError("VM name is required")
    if not datastore:
        raise ValueError("Datastore is required")
    if not session_dir.startswith("/vmfs/volumes/"):
        raise ValueError("Invalid OVA staging directory")

    vm_dir = f"/vmfs/volumes/{datastore}/{vm_name}"
    vm_dir_q = shlex.quote(vm_dir)
    session_dir_q = shlex.quote(session_dir)

    exists_check = host.run(f"[ -d {vm_dir_q} ] && echo EXISTS || echo OK")
    if "EXISTS" in str(exists_check):
        raise FileExistsError(f"Directory already exists: {vm_dir}")

    _run_or_raise(host, f"mkdir -p {vm_dir_q}")
    _run_or_raise(host, f"cp -R {session_dir_q}/. {vm_dir_q}/", timeout=3600)

    requested_disk_files = []
    for disk_file in disk_files or []:
        disk_name = os.path.basename(str(disk_file or "").strip())
        if disk_name:
            requested_disk_files.append(f"{vm_dir}/{disk_name}")

    descriptors = []
    if requested_disk_files:
        for p in requested_disk_files:
            exists = host.run(f"[ -f {shlex.quote(p)} ] && echo EXISTS || echo MISSING")
            if "EXISTS" in str(exists):
                descriptors.append(p)
    else:
        list_vmdk = host.run(f"find {vm_dir_q} -maxdepth 1 -type f \\( -iname '*.vmdk' \\) 2>/dev/null")
        for path in (list_vmdk or "").splitlines():
            p = path.strip()
            if not p:
                continue
            lower = p.lower()
            if lower.endswith("-flat.vmdk") or lower.endswith("-ctk.vmdk") or lower.endswith("-delta.vmdk") or lower.endswith("-sesparse.vmdk"):
                continue
            if not _is_descriptor_file(p):
                continue
            descriptors.append(p)
    descriptors = sorted(set(descriptors))
    if not descriptors:
        raise RuntimeError("No attachable VMDK files found in prepared OVA session")

    wanted_disk_type = str(disk_type or "thin").lower()
    if wanted_disk_type not in {"thin", "zeroedthick", "eagerzeroedthick"}:
        wanted_disk_type = "thin"

    converted_descriptors = []
    for src in descriptors:
        src_q = shlex.quote(src)
        conv = src.replace(".vmdk", "-import.vmdk")
        conv_q = shlex.quote(conv)
        out = host.run(f"vmkfstools -i {src_q} -d {wanted_disk_type} {conv_q}", timeout=7200)
        if isinstance(out, str) and out.startswith("Error:"):
            converted_descriptors.append(src)
            continue
        converted_descriptors.append(conv)

    nic_model = str(nic_type).lower()
    if nic_model not in {"e1000", "e1000e", "vmxnet3"}:
        nic_model = "e1000"

    scsi_model = str(scsi_controller).lower()
    if scsi_model not in {"lsilogic", "lsisas1068", "pvscsi"}:
        scsi_model = "lsilogic"

    firmware_mode = str(firmware).lower()
    if firmware_mode not in {"bios", "efi"}:
        firmware_mode = "bios"

    vmx_path = f"{vm_dir}/{vm_name}.vmx"
    vmx_entries = [
        '.encoding = "UTF-8"',
        'config.version = "8"',
        f'virtualHW.version = "{_vmx_escape(str(hw_version or "13"))}"',
        f'memsize = "{int(ram_mb)}"',
        f'numvcpus = "{int(cpu_count)}"',
        f'displayName = "{_vmx_escape(vm_name)}"',
        f'guestOS = "{_vmx_escape(guest_os)}"',
        f'firmware = "{_vmx_escape(firmware_mode)}"',
        'scsi0.present = "TRUE"',
        f'scsi0.virtualDev = "{_vmx_escape(scsi_model)}"',
    ]

    for idx, path in enumerate(converted_descriptors):
        vmdk_name = os.path.basename(path)
        vmx_entries.extend([
            f'scsi0:{idx}.present = "TRUE"',
            f'scsi0:{idx}.fileName = "{_vmx_escape(vmdk_name)}"',
            f'scsi0:{idx}.deviceType = "scsi-hardDisk"',
        ])

    vmx_entries.extend([
        'ethernet0.present = "TRUE"',
        f'ethernet0.virtualDev = "{_vmx_escape(nic_model)}"',
        f'ethernet0.networkName = "{_vmx_escape(network_name)}"',
        'ethernet0.addressType = "generated"',
    ])

    for idx, nic in enumerate(extra_nics or [], start=1):
        en_type = str(nic.get("type", nic_model)).lower()
        if en_type not in {"e1000", "e1000e", "vmxnet3"}:
            en_type = "e1000"
        en_net = str(nic.get("network", network_name))
        vmx_entries.extend([
            f'ethernet{idx}.present = "TRUE"',
            f'ethernet{idx}.virtualDev = "{_vmx_escape(en_type)}"',
            f'ethernet{idx}.networkName = "{_vmx_escape(en_net)}"',
            f'ethernet{idx}.addressType = "generated"',
        ])

    vmx_body = "\n".join(vmx_entries) + "\n"
    _run_or_raise(host, f"cat > {shlex.quote(vmx_path)} <<'EOF'\n{vmx_body}EOF")

    register_output = _run_or_raise(host, f"vim-cmd solo/registervm {shlex.quote(vmx_path)}", timeout=180)

    if power_on:
        try:
            vmid_output = _run_or_raise(host, "vim-cmd vmsvc/getallvms")
            created_vmid = None
            for line in str(vmid_output).splitlines()[1:]:
                if vm_name in line and f"{vm_name}.vmx" in line:
                    created_vmid = line.split()[0]
                    break
            if created_vmid:
                _run_or_raise(host, f"vim-cmd vmsvc/power.on {created_vmid}")
        except Exception:
            pass

    return f"OVA session deployed as '{vm_name}'. {register_output}"