#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_JOBS=${BUILD_JOBS:-2}
BUILD_CPUS=${BUILD_CPUS:-2}
BUILD_MEMORY=${BUILD_MEMORY:-4g}
RUST_IMAGE=${RUST_IMAGE:-rust:1.90}
RUNTIME_IMAGE=${RUNTIME_IMAGE:-cr.l5d.io/linkerd/proxy:edge-26.8.2}
PROXY_IMAGE=${PROXY_IMAGE:-linkerd-replay-proxy}
BUILDER=${BUILDER:-linkerd-replay-builder}
BUILD_ARTIFACTS=${BUILD_ARTIFACTS:-"$ROOT/.artifacts/build"}
mkdir -p "$BUILD_ARTIFACTS"

if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  docker buildx create --name "$BUILDER" --node "${BUILDER}0" --driver docker-container
fi
docker buildx inspect --bootstrap "$BUILDER"
docker update --cpus "$BUILD_CPUS" --memory "$BUILD_MEMORY" --memory-swap "$BUILD_MEMORY" \
  "buildx_buildkit_${BUILDER}0"

for variant in before after; do
  docker buildx build --builder "$BUILDER" --load --progress=plain \
    --build-arg "VARIANT=$variant" --build-arg "BUILD_JOBS=$BUILD_JOBS" \
    --build-arg "RUST_IMAGE=$RUST_IMAGE" --build-arg "RUNTIME_IMAGE=$RUNTIME_IMAGE" \
    -f "$ROOT/docker/Proxy.Dockerfile" -t "$PROXY_IMAGE:$variant" "$ROOT" \
    2>&1 | tee "$BUILD_ARTIFACTS/$variant.log"
done
docker image inspect "$PROXY_IMAGE:before" "$PROXY_IMAGE:after" > "$BUILD_ARTIFACTS/images.json"
cp "$ROOT/patches/replay-unpolled-body.patch" "$BUILD_ARTIFACTS/applied.patch"
printf 'Built %s:before and %s:after\n' "$PROXY_IMAGE" "$PROXY_IMAGE"
