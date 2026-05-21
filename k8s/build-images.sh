#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${REGISTRY:-build.home/home}"
TAG="${TAG:-latest}"

cd "${PROJECT_ROOT}"

docker build --file containers/db/Dockerfile --tag "${REGISTRY}/jupiter_postgres:${TAG}" .
docker build --file containers/redis/Dockerfile --tag "${REGISTRY}/jupiter_redis:${TAG}" .
docker build --file containers/web/Dockerfile --tag "${REGISTRY}/jupiter_web:${TAG}" .
docker build --file containers/sync-worker/Dockerfile --tag "${REGISTRY}/jupiter_sync_worker:${TAG}" .

docker push "${REGISTRY}/jupiter_postgres:${TAG}"
docker push "${REGISTRY}/jupiter_redis:${TAG}"
docker push "${REGISTRY}/jupiter_web:${TAG}"
docker push "${REGISTRY}/jupiter_sync_worker:${TAG}"

echo "Built and pushed jupiter db, redis, web, and sync-worker images successfully."