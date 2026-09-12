# syntax=docker/dockerfile:1
FROM rust:1.90 AS source
WORKDIR /src
RUN git init . \
    && git fetch --depth=1 https://github.com/linkerd/linkerd2-proxy.git e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f \
    && git checkout --detach FETCH_HEAD
COPY harness/replay.rs linkerd/app/integration/examples/replay.rs
ENV CARGO_PROFILE_RELEASE_LTO=false \
    CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
    CARGO_BUILD_JOBS=2 \
    RUSTFLAGS="-D warnings -D deprecated --cfg tokio_unstable -C debuginfo=0"
RUN --mount=type=cache,id=replay-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=replay-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=replay-harness-target,target=/target,sharing=locked \
    touch linkerd/http/retry/src/replay.rs \
    && CARGO_TARGET_DIR=/target cargo build --release --locked -p linkerd-app-integration --example replay \
    && cp /target/release/examples/replay /replay-before \
    && strip /replay-before

FROM source AS patched
COPY patches/replay-unpolled-body.patch /replay.patch
RUN git apply --check /replay.patch && git apply /replay.patch \
    && git diff --no-ext-diff --no-color --full-index --unified=3 \
         --src-prefix=a/ --dst-prefix=b/ -- linkerd/http/retry/src/replay.rs > /applied.patch \
    && cmp /replay.patch /applied.patch
RUN --mount=type=cache,id=replay-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=replay-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=replay-harness-target-after,target=/target,sharing=locked \
    CARGO_TARGET_DIR=/target cargo build --release --locked -p linkerd-app-integration --example replay \
    && cp /target/release/examples/replay /replay-after \
    && strip /replay-after

FROM golang:1.25.1 AS fixture
WORKDIR /fixture
COPY fixture/go.mod fixture/go.sum ./
RUN go mod download
COPY fixture/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /audit-h2 .

FROM python:3.13-slim-trixie
WORKDIR /app
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark tcpdump libcap2-bin \
    && setcap cap_net_raw=ep /usr/bin/tcpdump \
    && rm -rf /var/lib/apt/lists/*
COPY --from=source /replay-before /usr/local/bin/replay-before
COPY --from=patched /replay-after /usr/local/bin/replay-after
COPY --from=fixture /audit-h2 /usr/local/bin/audit-h2
COPY --from=source /src/linkerd/app/integration/src/data /src/linkerd/app/integration/src/data
COPY scripts/run.py scripts/reproduce.py scripts/standalone.py scripts/packets.py ./scripts/
RUN mkdir /results && chown 65532:65532 /results
ENV LINKERD2_PROXY_LOG="linkerd=debug,warn" PYTHONUNBUFFERED=1 NO_COLOR=1
LABEL org.opencontainers.image.source="https://github.com/cprayer/linkerd-replay-body-repro" \
      repro.upstream.revision="e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f"
USER 65532:65532
ENTRYPOINT ["python3", "scripts/standalone.py"]
CMD ["--runs", "5"]
