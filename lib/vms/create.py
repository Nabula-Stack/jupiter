import shlex


def _vmx_escape(value: object) -> str:
    """Escapes VMX string values to avoid malformed .vmx entries."""
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run_or_raise(host, command: str) -> str:
    """Runs a command on ESXi and raises if shell call fails."""
    result = host.run(command)
    if isinstance(result, str) and result.startswith("Error:"):
        raise RuntimeError(result)
    return result


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
    ds_path = f"/vmfs/volumes/{shlex.quote(datastore)}"

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

    _run_or_raise(host, f"mkdir -p {vm_dir_q}")

    # The OVA should already be uploaded to the datastore by the API layer.
    # ova_local_path here is the remote path on ESXi.
    remote_ova = ova_local_path

    # Extract OVA (it's a tar archive)
    _run_or_raise(host, f"tar xf {shlex.quote(remote_ova)} -C {vm_dir_q}")

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
        result = host.run(f"vmkfstools -i {vmdk_q} -d thin {converted_q}")
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
    register_output = _run_or_raise(host, f"vim-cmd solo/registervm {shlex.quote(vmx_path)}")

    # Clean up the uploaded OVA tar to save space
    host.run(f"rm -f {shlex.quote(remote_ova)}")

    return f"OVA deployed as '{vm_name}'. {register_output}"