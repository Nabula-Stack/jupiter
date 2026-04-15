# KVM/libvirt Plugin Setup Guide

## Overview

The KVM plugin connects to Linux KVM hosts over **SSH** using the same key-based auth as the ESXi plugin. It talks to `libvirt` via `virsh` commands over the SSH session — no extra daemon or API port is needed on the KVM host.

### What gets synced

| Data | Source |
|---|---|
| CPU count, RAM, kernel, OS | `nproc`, `/proc/meminfo`, `uname`, `hostnamectl` |
| Services | `systemctl is-active libvirtd / virtqemud` |
| Network bridges | `virsh net-list` |
| Storage pools | `virsh pool-list` + `virsh pool-info` |
| VM inventory | `virsh list --all` + `virsh dominfo` |
| VM disk sizes | `qemu-img info` (actual + provisioned GB) |
| VM IP address | `virsh domifaddr` (agent then ARP fallback) |
| Power state | Running → `poweredOn`, Paused → `suspended`, else `poweredOff` |

---

## KVM Host Requirements

### 1. Install required packages on the KVM host

```bash
# Debian / Ubuntu
sudo apt install --yes \
    libvirt-daemon-system \
    libvirt-clients \
    qemu-kvm \
    qemu-utils \
    virtinst

# RHEL / Rocky / AlmaLinux
sudo dnf install -y \
    libvirt \
    libvirt-client \
    qemu-kvm \
    qemu-img \
    virt-install
```

### 2. Enable and start libvirt

```bash
sudo systemctl enable --now libvirtd
# On newer distros the socket-based unit may be used instead:
sudo systemctl enable --now virtqemud.socket
```

### 3. Create a Nebula service account

```bash
sudo useradd --system --shell /bin/bash --home /home/nebula nebula
sudo usermod --append --groups libvirt,kvm nebula
sudo mkdir --parents /home/nebula/.ssh
sudo chmod 700 /home/nebula/.ssh
```

### 4. Authorise the Nebula SSH public key

Paste the content of your Nebula RSA **public** key (the same key used for ESXi hosts, or a new one) into the authorised keys file on the KVM host:

```bash
echo "ssh-rsa AAAA... nebula@nebula" \
  | sudo tee /home/nebula/.ssh/authorized_keys
sudo chmod 600 /home/nebula/.ssh/authorized_keys
sudo chown --recursive nebula:nebula /home/nebula/.ssh
```

### 5. Verify qemu-img is accessible

The sync worker calls `qemu-img info` to read disk sizes. Confirm the nebula user can run it:

```bash
sudo -u nebula qemu-img --version
```

If it prints a version string, you are ready. If not, add the binary's directory to the user's `PATH` in `/home/nebula/.bashrc`.

---

## Nebula Admin Setup

### 1. Open the Host Management page

Navigate to **Admin → Host Management → Add Host**.

### 2. Fill in the form

| Field | Value |
|---|---|
| **Name** | Any label — e.g. `kvm-prod-01` |
| **IP Address** | The KVM host's IP or FQDN |
| **Hypervisor Type** | `KVM/libvirt` |
| **Username** | The service account created above — e.g. `nebula` |
| **SSH Public Key** | Paste the full public key (`ssh-rsa AAAA...`) |
| **Is Active** | ✅ checked |

> The **Password** and **ESXi Connection Method** fields are automatically hidden when KVM/libvirt is selected.

### 3. Save and wait for the first sync

The background sync worker picks up new hosts within 5 seconds. Watch the logs:

```bash
docker compose logs -f sync-worker
```

A successful sync looks like:

```
✅ KVM host 'kvm-prod-01' synced: 16 CPUs | 64GB RAM | kernel 6.8.0-57-generic
   📊 KVM VM Sync [kvm-prod-01]: Changed=4 | Deleted=0 | Total=4
```

---

## API Endpoint

The KVM plugin registers a health summary route:

```
GET /api/v1/kvm/{host_name}/health
```

Example response:

```json
{
  "status": "success",
  "host": "kvm-prod-01",
  "hypervisor": "kvm_libvirt",
  "last_sync": "2026-04-15T14:30:00Z",
  "vm_count": 4,
  "storage_pools": 2,
  "networks": 3
}
```

Access the full interactive docs at **Admin → Development → KVM API Docs** or directly at `/api/v1/docs`.

---

## Troubleshooting

### Auth failure in sync logs

```
❌ sync_vms_for_host failed for 'kvm-prod-01': Authentication failed
```

- Confirm the public key in the Nebula admin matches the private key loaded in the container (`SSH_PRIVATE_KEY_B64` env var or `/app/nebula_rsa`).
- Test manually: `ssh -i /path/to/nebula_rsa nebula@<kvm-host-ip> virsh list --all`

### `virsh` permission denied

```
error: Failed to connect to the hypervisor ... Permission denied
```

The `nebula` user is not in the `libvirt` group. Run on the KVM host:

```bash
sudo usermod --append --groups libvirt,kvm nebula
# Then log out and back in, or restart the SSH session
```

### Disk sizes show 0 GB

`qemu-img` is not in the `nebula` user's `PATH`. Add to `/home/nebula/.bashrc`:

```bash
export PATH="$PATH:/usr/bin:/usr/local/bin"
```

### VM IP shows N/A

The QEMU guest agent is not running inside the VM. Install it in the guest:

```bash
# Inside the VM (Debian/Ubuntu)
sudo apt install --yes qemu-guest-agent
sudo systemctl enable --now qemu-guest-agent
```

Without the agent, Nebula falls back to the ARP table which may not always resolve.

---

## Adding KVM API Docs to the sidebar

The sidebar already has an **ESXi API Docs** and **Proxmox API Docs** entry. To add a dedicated KVM entry, edit `core/settings.py`:

```python
{"title": "KVM API Docs", "icon": "api", "link": "/api/v1/docs#KVM%20Plugin"},
```
