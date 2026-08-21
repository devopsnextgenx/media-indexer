#!/usr/bin/env bash
set -e

IMAGE_NAME="amitkshirsagar13/media-indexer:latest"

echo "=== 1. Enabling Docker BuildKit ==="
export DOCKER_BUILDKIT=1

echo "=== 2. Building Docker Image with Cache ==="
docker build \
  --build-arg BUILDKIT_INLINE_CACHE=1 \
  --tag "$IMAGE_NAME" .

echo "=== 3. Cleaning Stopped Containers ==="
docker container prune -f

echo "=== 4. Removing Dangling/Orphaned Images ==="
docker image prune -f

echo "=== 5. Purging Old Build Cache (Preserving Recent Cache) ==="
# Purges build cache older than 24 hours to prevent multi-GB disk leaks while keeping quick rebuilds
docker builder prune -f --filter "until=24h"

echo "=== Build Complete ==="
docker image ls "$IMAGE_NAME"