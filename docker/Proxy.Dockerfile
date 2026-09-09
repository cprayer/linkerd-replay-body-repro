# syntax=docker/dockerfile:1
ARG RUST_IMAGE=rust:1.90
ARG RUNTIME_IMAGE=cr.l5d.io/linkerd/proxy:edge-26.8.2

FROM ${RUST_IMAGE} AS source
WORKDIR /src
RUN git init . \
    && git fetch --depth=1 https://github.com/linkerd/linkerd2-proxy.git e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f \
    && git checkout --detach FETCH_HEAD
COPY patches/replay-unpolled-body.patch /replay.patch
RUN git apply --check /replay.patch

FROM source AS build
ARG VARIANT
ARG BUILD_JOBS=2
RUN case "$VARIANT" in \
      before) git diff --exit-code ;; \
      after) git apply /replay.patch \
        && git diff --no-ext-diff --no-color --full-index --unified=3 \
             --src-prefix=a/ --dst-prefix=b/ -- linkerd/http/retry/src/replay.rs > /applied.patch \
        && cmp /replay.patch /applied.patch ;; \
      *) echo 'VARIANT must be before or after' >&2; exit 1 ;; \
    esac
ENV CARGO_PROFILE_RELEASE_LTO=false \
    CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
    RUSTFLAGS="-D warnings -D deprecated --cfg tokio_unstable -C debuginfo=0" \
    LINKERD2_PROXY_VENDOR=replay-body-repro
RUN --mount=type=cache,id=replay-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=replay-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=replay-target-${VARIANT},target=/target,sharing=locked \
    CARGO_TARGET_DIR=/target CARGO_BUILD_JOBS="$BUILD_JOBS" \
      LINKERD2_PROXY_VERSION="0.0.0-replay.$VARIANT" \
      cargo build --release --locked -p linkerd2-proxy \
    && cp /target/release/linkerd2-proxy /linkerd2-proxy \
    && strip /linkerd2-proxy \
    && sha256sum /linkerd2-proxy

FROM ${RUNTIME_IMAGE}
ARG VARIANT
LABEL org.opencontainers.image.source="https://github.com/cprayer/linkerd-replay-body-repro" \
      repro.upstream.revision="e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f" \
      repro.variant="${VARIANT}"
COPY --from=build /linkerd2-proxy /usr/lib/linkerd/linkerd2-proxy
