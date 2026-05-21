# Nebula Helm Deployment (k3s + Rancher)

This directory is now a complete Helm chart for Nebula.

## What gets deployed

- Web deployment: Django + Daphne
- Sync worker deployment: background host synchronization
- PostgreSQL deployment + PVC (optional)
- Redis deployment (optional)
- Ingress (Traefik by default)
- Secret for app/env variables (or use existing secret)

## Files to edit before deployment

1. `values-rancher.yaml`
2. Optional: `secret-app.example.yaml` if you prefer a pre-created secret

## Build and push images

```bash
REGISTRY=build.home/home TAG=latest ./k8s/build-images.sh
```

## Option A: Helm-managed secret (quickest)

1. Edit `values-rancher.yaml` and set real values under `secrets`.
2. Keep `secrets.create: true` and `secrets.existingSecret: ""`.

Deploy:

```bash
helm upgrade --install nebula ./k8s -n nebula --create-namespace -f ./k8s/values-rancher.yaml
```

## Option B: Pre-create secret and reference it

1. Copy `secret-app.example.yaml` to `secret-app.yaml`.
2. Fill real secret values.
3. Apply it:

```bash
kubectl apply -f ./k8s/secret-app.yaml
```

4. Set in `values-rancher.yaml`:

```yaml
secrets:
  create: false
  existingSecret: nebula-app-secret
```

Deploy:

```bash
helm upgrade --install nebula ./k8s -n nebula --create-namespace -f ./k8s/values-rancher.yaml
```

## Rancher Fleet

`fleet.yaml` is configured to deploy this chart using `values-rancher.yaml`.

### Validation checks

```bash
kubectl get pods -n nebula
kubectl get svc -n nebula
kubectl get ingress -n nebula
kubectl logs deployment/nebula-web -n nebula --tail=100
kubectl logs deployment/nebula-sync-worker -n nebula --tail=100
```

### Redis performance tuning (recommended)

This chart now deploys Redis with a cache-oriented profile:

- Persistence disabled (`save ""`, `appendonly no`) to reduce fork overhead and improve UI responsiveness.
- LRU eviction enabled (`maxmemory-policy allkeys-lru`) with `maxmemory 192mb`.
- Lazy freeing enabled for lower latency during key eviction/deletion.

If your workload needs durable Redis data, override `redis.config` in your values file and enable persistence settings.

### Linux host kernel setting for Redis

On Kubernetes nodes, set `vm.overcommit_memory=1` to avoid Redis fork failures during background operations.

Apply on each Linux node:

```bash
sudo sysctl vm.overcommit_memory=1
echo 'vm.overcommit_memory = 1' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### Access

- If ingress host resolves to your cluster, use: `https://nebula.prod.home` (or your configured host)
- Fallback:

```bash
kubectl port-forward svc/nebula-web -n nebula 8000:8000
```
