#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
readonly REVISION=e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f
readonly UPSTREAM=https://github.com/linkerd/linkerd2-proxy.git
CACHE="$ROOT/.cache"
SOURCE="$CACHE/proxy-source"
TARGET_VOLUME=${TARGET_VOLUME:-linkerd-replay-target}
CARGO_VOLUME=${CARGO_VOLUME:-linkerd-replay-cargo}
BUILD_JOBS=${BUILD_JOBS:-2}
RUST_IMAGE=${RUST_IMAGE:-rust:1.90}
RUNTIME_IMAGE=${RUNTIME_IMAGE:-cr.l5d.io/linkerd/proxy:edge-26.8.2}
PROXY_IMAGE=${PROXY_IMAGE:-linkerd-replay-proxy}
mkdir -p "$CACHE" "$ROOT/.artifacts/build"

if [[ ! -d "$CACHE/upstream.git" ]]; then
  git init --bare "$CACHE/upstream.git"
fi
if ! git --git-dir="$CACHE/upstream.git" cat-file -e "${REVISION}^{commit}" 2>/dev/null; then
  git --git-dir="$CACHE/upstream.git" fetch --depth=1 "$UPSTREAM" "$REVISION"
fi
mkdir -p "$SOURCE"
if [[ ! -d "$SOURCE/.git" ]]; then
  git init "$SOURCE"
  git -C "$SOURCE" fetch "$CACHE/upstream.git" "$REVISION"
fi
git -C "$SOURCE" checkout --detach --force "$REVISION"
git -C "$SOURCE" apply --check "$ROOT/patches/replay-unpolled-body.patch"
git -C "$SOURCE" diff --exit-code

build() {
  local variant=$1 output="$CACHE/image-$1"
  mkdir -p "$output"
  docker run --rm \
    -v "$SOURCE:/src:ro" -v "$output:/out" \
    -v "$TARGET_VOLUME:/target" -v "$CARGO_VOLUME:/usr/local/cargo" \
    -e CARGO_TARGET_DIR="/target/$variant" -e CARGO_BUILD_JOBS="$BUILD_JOBS" \
    -e CARGO_PROFILE_RELEASE_LTO=false -e CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
    -e RUSTFLAGS='-D warnings -D deprecated --cfg tokio_unstable -C debuginfo=0' \
    -e LINKERD2_PROXY_VERSION="0.0.0-replay.$variant" \
    -e LINKERD2_PROXY_VENDOR=replay-body-repro \
    -w /src "$RUST_IMAGE" sh -eu -c '
      cargo build --release --locked -p linkerd2-proxy
      cp "$CARGO_TARGET_DIR/release/linkerd2-proxy" /out/linkerd2-proxy
      strip /out/linkerd2-proxy
      sha256sum /out/linkerd2-proxy
    ' 2>&1 | tee "$ROOT/.artifacts/build/$variant.log"
  cp "$ROOT/docker/Proxy.Dockerfile" "$output/Dockerfile"
  docker build --build-arg "RUNTIME_IMAGE=$RUNTIME_IMAGE" \
    --label "org.opencontainers.image.source=https://github.com/cprayer/linkerd-replay-body-repro" \
    --label "repro.upstream.revision=$REVISION" --label "repro.variant=$variant" \
    -t "$PROXY_IMAGE:$variant" "$output"
}

build before
git -C "$SOURCE" apply "$ROOT/patches/replay-unpolled-body.patch"
git -C "$SOURCE" diff --exit-code --no-ext-diff --no-color --full-index --unified=3 \
  --src-prefix=a/ --dst-prefix=b/ -- linkerd/http/retry/src/replay.rs > "$ROOT/.artifacts/build/applied.patch" && {
  printf 'Error: patch did not change ReplayBody\n' >&2
  exit 1
}
cmp "$ROOT/patches/replay-unpolled-body.patch" "$ROOT/.artifacts/build/applied.patch"
build after
docker image inspect "$PROXY_IMAGE:before" "$PROXY_IMAGE:after" > "$ROOT/.artifacts/build/images.json"
printf 'Built %s:before and %s:after from %s\n' "$PROXY_IMAGE" "$PROXY_IMAGE" "$REVISION"
