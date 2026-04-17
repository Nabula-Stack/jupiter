# Nebula Manager

A self-hosted hypervisor management platform built with Django and Daphne (ASGI). Nebula provides a unified admin UI, REST API, and real-time WebSocket dashboard for managing virtual machines across ESXi, KVM/libvirt, and Proxmox environments.

---

## What Nebula Can Do

### Hypervisor Support
- **VMware ESXi** — connect over SSH or the vSphere API
- **KVM / libvirt** — connect over SSH using a private key
- **Proxmox VE** — connect via the Proxmox REST API

### Virtual Machine Management
- List, power on/off/reset/suspend all VMs across all hosts
- Create VMs from scratch (CPU, RAM, disk, network)
- Clone, rename, delete, and unregister VMs
- Deploy OVA/OVF images directly to ESXi hosts
- Snapshot create, revert, and delete
- Live VM status with real-time WebSocket updates in the admin dashboard

### Host & Infrastructure Management
- Add and manage multiple hypervisor hosts from a single interface
- View per-host CPU, memory, storage, and network inventory
- Browse datastores and storage pools
- Manage virtual networks, port groups, and vSwitches
- Per-host SSH connection with encrypted key storage at rest

### Background Sync Worker
- Continuously syncs VM state, host inventory, and metrics into the PostgreSQL database
- Broadcasts live updates over Redis Channels to connected browser clients

### REST API
- Full Django Ninja API at `/api/v1/` with OpenAPI docs at `/api/v1/docs`
- API calls require a logged-in Django admin staff session
- Route groups: `/hosts/`, `/vms/`, `/storage/`, `/network/`, `/system/`, `/proxmox/`, `/kvm/`
- Pluggable hypervisor adapter architecture — external plugins can be loaded via `HYPERVISOR_PLUGIN_MODULES`

### Admin UI
- Django Unfold admin with sidebar navigation, live VM list, and host dashboards
- Encrypted storage of per-host SSH public keys in the database

---

## Requirements

- Docker and Docker Compose (local dev / all-in-one)
- Or: a k3s cluster with Helm 3 and kubectl (production / Rancher)
- Harbor or any OCI-compatible container registry (for k3s image push)
- An SSH key pair for ESXi / KVM host access

---

## Docker Deployment

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

| Variable | Description |
|---|---|
| `DB_PASSWORD` | PostgreSQL password |
| `DJANGO_SECRET_KEY` | Random 50+ character secret |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames / IPs |
| `DJANGO_SUPERUSER_USERNAME` | Admin login username |
| `DJANGO_SUPERUSER_PASSWORD` | Admin login password |
| `SSH_PRIVATE_KEY_B64` | Base64-encoded private key for ESXi/KVM access |

Generate the base64 key value:

```bash
# Linux / macOS
base64 --wrap=0 ~/.ssh/your_key

# PowerShell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\Users\you\.ssh\your_key"))
```

### 2. Start all services

```bash
docker compose up -d --build
```

This starts four containers:

| Container | Purpose |
|---|---|
| `web` | Django + Daphne ASGI on port 8000 |
| `sync-worker` | Background host sync loop |
| `db` | PostgreSQL 16 |
| `redis` | Redis 7 (channels + cache) |

### 3. Open the admin UI

```
http://localhost:8000/admin/
```

Log in with the superuser credentials from `.env`. The API docs are at:

```
http://localhost:8000/api/v1/docs
```

### Useful commands

```bash
# View web logs
docker compose logs -f web

# View sync worker logs
docker compose logs -f sync-worker

# Stop sync worker temporarily
docker compose stop sync-worker

# Run a Django management command
docker compose exec web python manage.py <command>
```

---

## Helm Deployment (k3s / Rancher)

### 1. Build and push images

```bash
REGISTRY=build.home/home TAG=latest ./k8s/build-images.sh
```

This builds and pushes four images: `nebula_web`, `nebula_sync_worker`, `nebula_postgres`, `nebula_redis`.

### 2. Create the image pull secret

```bash
kubectl create namespace nebula --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret docker-registry harbor-regcred \
  --namespace nebula \
  --docker-server build.home \
  --docker-username <HARBOR_USERNAME> \
  --docker-password <HARBOR_PASSWORD>
```

### 3. Configure deployment values

Edit [`k8s/values-rancher.yaml`](k8s/values-rancher.yaml) and replace every `REPLACE_ME_*` placeholder:

```yaml
secrets:
  dbPassword: "your-db-password"
  djangoSecretKey: "your-50-char-random-secret"
  djangoSuperuserUsername: "admin"
  djangoSuperuserPassword: "your-admin-password"
  sshPrivateKeyB64: "base64-encoded-private-key"

ingress:
  hosts:
    - host: nebula.your-domain.home
      paths:
        - path: /
          pathType: Prefix
```

> **Option B — pre-created secret:** copy `k8s/secret-app.example.yaml` to `k8s/secret-app.yaml`, fill it in, apply it with `kubectl apply -f k8s/secret-app.yaml`, then set `secrets.create: false` and `secrets.existingSecret: nebula-app-secret` in `values-rancher.yaml`.

### 4. Deploy with Helm

```bash
helm upgrade --install nebula ./k8s \
  --namespace nebula \
  --create-namespace \
  -f ./k8s/values-rancher.yaml
```

### 5. Deploy via Rancher Fleet (GitOps)

Commit and push the `k8s/` folder to your Fleet Git repository. `k8s/fleet.yaml` is already configured to deploy the chart using `values-rancher.yaml`. Fleet will apply the chart automatically.

### 6. Verify

```bash
kubectl get pods      -n nebula
kubectl get svc       -n nebula
kubectl get ingress   -n nebula

kubectl logs deployment/nebula-web          -n nebula --tail=100
kubectl logs deployment/nebula-sync-worker  -n nebula --tail=100
```

### 7. Access

Browse to the host you set in `ingress.hosts`, e.g.:

```
http://nebula.your-domain.home/admin/
http://nebula.your-domain.home/api/v1/docs
```

Port-forward fallback (no ingress):

```bash
kubectl port-forward svc/nebula-web -n nebula 8000:8000
```

---

## Project Layout

```
containers/          Per-service Dockerfiles (web, sync-worker, db, redis)
core/                Django project settings, ASGI/WSGI, routing
manager/             Main Django app — models, admin, hypervisor adapters, services
  hypervisors/       ESXi, KVM, Proxmox adapter implementations
  services/          VM and host business logic
  consumers.py       WebSocket consumers (Django Channels)
lib/                 Low-level SSH/API wrappers for vSphere, KVM, Proxmox, SSH
plugins/             Pluggable hypervisor API route sets (esxi, kvm, proxmox)
api/                 Django Ninja API entrypoint and system routes
k8s/                 Helm chart (Chart.yaml, templates/, values.yaml, values-rancher.yaml)
ssh_keys/            Local SSH key files (git-ignored)
```

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `DB_NAME` | `mydatabase` | PostgreSQL database name |
| `DB_USER` | `admin` | PostgreSQL user |
| `DB_PASSWORD` | — | PostgreSQL password (**required**) |
| `DJANGO_SECRET_KEY` | insecure default | Django secret key (**change in production**) |
| `DJANGO_DEBUG` | `False` | Enable debug mode |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts |
| `DJANGO_SUPERUSER_USERNAME` | — | Auto-created superuser login |
| `DJANGO_SUPERUSER_PASSWORD` | — | Auto-created superuser password |
| `DJANGO_SUPERUSER_EMAIL` | — | Auto-created superuser email |
| `SSH_PRIVATE_KEY_B64` | — | Base64-encoded SSH private key for host connections |
| `SSH_KEY_CONTAINER_PATH` | `/run/secrets/ssh_key` | Path where the entrypoint writes the decoded key |
| `SSH_PUBLIC_KEY_ENCRYPTION_KEY` | uses `DJANGO_SECRET_KEY` | Dedicated key for encrypting host SSH public keys at rest |
| `HYPERVISOR_PLUGIN_MODULES` | — | Comma-separated Python module paths for external hypervisor plugins |
| `PROXMOX_VERIFY_SSL` | `false` | Verify Proxmox TLS certificate |
