#!/usr/bin/env bash
set -euo pipefail

# Override with: IMAGE=yourdockerhubuser/iptv-au ./build.sh
IMAGE="${IMAGE:-matthuisman/iptv-au}"

docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
docker buildx inspect multiarch >/dev/null 2>&1 || docker buildx create --name multiarch --driver docker-container --use
docker buildx use multiarch
docker buildx build --push --platform linux/arm/v7,linux/arm64/v8,linux/amd64 --tag "$IMAGE" .
