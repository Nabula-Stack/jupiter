# vCenter Manager — Setup

## Prerequisites

- Docker & Docker Compose
- k3s cluster with kubectl access (for Kubernetes deployment)
- SSH key pair for ESXi host access

## Quick Start

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env with your database password, secret key, and host settings

# 2. Start core services
docker compose up -d --build

# 3. (Optional) Start sync worker profile
docker compose --profile always-sync up -d sync-worker

# 4. Create admin user
docker compose exec web python manage.py createsuperuser

# 5. Open browser
# http://localhost:8000/admin/
```

### Background Sync Worker

The `sync-worker` service runs under the `always-sync` profile and continuously syncs:
- Host metadata (CPU, memory, storage, network)
- VMs list and state for each host  
- WebSocket broadcast for live updates

Start it:
```bash
docker compose --profile always-sync up -d sync-worker
```

View logs:
```bash
docker compose logs -f sync-worker
```

Stop sync worker temporarily:
```bash
docker compose stop sync-worker
```

## Container Build Layout (4 Containers)

- `containers/web/Dockerfile`
- `containers/sync-worker/Dockerfile`
- `containers/db/Dockerfile`
- `containers/redis/Dockerfile`

All services in `docker-compose.yml` now build from these container-specific Dockerfiles.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_NAME` | `mydatabase` | PostgreSQL database name |
| `DB_USER` | `admin` | PostgreSQL user |
| `DB_PASSWORD` | `mypassword` | PostgreSQL password |
| `DB_HOST` | `db` | PostgreSQL hostname inside compose network |
| `DB_PORT` | `5432` | PostgreSQL port |
| `REDIS_HOST` | `redis` | Redis hostname (set by compose) |
| `REDIS_PORT` | `6379` | Redis port |
| `DJANGO_SECRET_KEY` | insecure default | Change in production |
| `DJANGO_DEBUG` | `False` | Debug mode |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,0.0.0.0` | Comma-separated hosts |
| `SSH_PUBLIC_KEY_ENCRYPTION_KEY` | uses `DJANGO_SECRET_KEY` | Optional dedicated key for encrypting `Host.ssh_public_key` at rest |
| `SSH_KEY_PATH` | `/app/ssh_keys/rsa` | Default SSH key path for host operations |
| `ESXI_SSH_KEY_PATH` | `/app/ssh_keys/nebula_rsa` | ESXi key path inside container |
| `KVM_SSH_KEY_PATH` | `/app/ssh_keys/rsa` | KVM key path inside container |
| `HYPERVISOR_PLUGIN_MODULES` | empty | Comma-separated Python modules to auto-register external hypervisor plugins |
| `WEB_PORT` | `8000` | Exposed web port |
| `WEB_INTERNAL_PORT` | `8000` | Daphne bind port inside container |

## SSH Key Setup

Generate a key pair and copy to your ESXi hosts:

```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/vcnter_rsa -C "vcenter-manager"
cat ~/.ssh/vcnter_rsa.pub | ssh root@<ESXI_IP> "cat >> /etc/ssh/keys-root/authorized_keys"
```

For Docker Compose, place private keys in the local `ssh_keys` folder (mounted read-only into containers):

```bash
mkdir -p ssh_keys
cp ~/.ssh/vcnter_rsa ssh_keys/rsa
cp ~/.ssh/vcnter_rsa ssh_keys/nebula_rsa
```

Then set key env paths in `.env` only if you need non-default locations.

## Architecture

```
docker compose up -d
├── web      — Django + Daphne (ASGI) on :8000
├── db       — PostgreSQL 16
└── redis    — Redis 7 (channels + cache)
```

## API

All endpoints are under `/api/v1/`. Interactive docs at `/api/v1/docs`.

| Route Prefix | Purpose |
|-------------|---------|
| `/api/v1/hosts/` | Host management |
| `/api/v1/vms/` | VM lifecycle, power, snapshots, OVA deploy |
| `/api/v1/storage/` | Datastores, file browser, upload |
| `/api/v1/network/` | Port groups, vSwitches, NICs |
| `/api/v1/system/` | Supported plugin list and host-to-hypervisor mapping |

## Development (without Docker)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # set DB_HOST/REDIS_HOST to your local instances
python manage.py migrate
python manage.py createsuperuser
```

**Terminal 1** — Web server:
```bash
daphne -b 0.0.0.0 -p 8000 core.asgi:application
```

**Terminal 2** — Background sync worker:
```bash
python manage.py sync_hosts
```

## k3s Deployment (Rancher Fleet)

1. Build all 4 images and push to Harbor:
```bash
REGISTRY=build.home/home TAG=latest ./k8s/build-images.sh
```

2. Create image pull secret in k3s namespace:
```bash
kubectl create secret docker-registry harbor-regcred \
	--namespace nebula \
	--docker-server build.home \
	--docker-username <HARBOR_USERNAME> \
	--docker-password <HARBOR_PASSWORD>
```

3. Choose one secret approach:

- Helm-managed secret: set values in `k8s/values-rancher.yaml` under `secrets`.
- Pre-created secret:

```bash
cp k8s/secret-app.example.yaml k8s/secret-app.yaml
kubectl apply -f k8s/secret-app.yaml
```

4. If using a pre-created secret, set this in `k8s/values-rancher.yaml`:

```bash
secrets:
  create: false
  existingSecret: nebula-app-secret
```

5. Commit and push the `k8s/` folder to your Fleet Git repo. Fleet uses `fleet.yaml` and `values-rancher.yaml` automatically.

6. Access web app via ingress host from `values-rancher.yaml`:
```bash
https://nebula.prod.home/admin/
```
