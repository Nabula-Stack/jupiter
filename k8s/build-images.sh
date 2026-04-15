#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${REGISTRY:-build.home/home}"
TAG="${TAG:-latest}"

cd "${PROJECT_ROOT}"

docker build --file containers/db/Dockerfile --tag "${REGISTRY}/nebula_postgres:${TAG}" .
docker build --file containers/redis/Dockerfile --tag "${REGISTRY}/nebula_redis:${TAG}" .
docker build --file containers/web/Dockerfile --tag "${REGISTRY}/nebula_web:${TAG}" .
docker build --file containers/sync-worker/Dockerfile --tag "${REGISTRY}/nebula_sync_worker:${TAG}" .

docker push "${REGISTRY}/nebula_postgres:${TAG}"
docker push "${REGISTRY}/nebula_redis:${TAG}"
docker push "${REGISTRY}/nebula_web:${TAG}"
docker push "${REGISTRY}/nebula_sync_worker:${TAG}"

echo "Built and pushed Nebula db, redis, web, and sync-worker images successfully."