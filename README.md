# Linkerd ReplayBody reproduction

Reproduce duplicate request-body bytes when Linkerd retries a request **before reading any of its body**. Compare an unmodified upstream proxy with the same source plus a one-line fix.

This repository runs real HTTP/2 traffic through Linkerd in a dedicated kind cluster. The fixture client makes one call per request ID; only Linkerd retries. The server records the exact bytes it receives and echoes their length and SHA256.

## The bug

`ReplayBody` caches bytes as it reads the original body. A retry first returns cached bytes, then continues reading the original body.

If the retry starts with an empty cache, the old implementation leaves `replay_body` set to `true`. It reads and caches the first original chunk, returns it, and then returns the newly cached chunk again on the next poll **of the same attempt**.

```text
Expected: hello world
Before:   hello worldhello world
After:    hello world
```

The [one-line patch](patches/replay-unpolled-body.patch) finishes the empty-cache replay phase before reading the original body. It preserves the existing buffer-limit check. This is independent of GOAWAY detection or a new retry policy.

## Quick start

Requirements: Docker, kind, kubectl, Git, Python 3.9+, Bash, and Linkerd CLI `edge-26.8.2`. The proxy build uses a Rust container; no host Rust or Go installation is needed. Allow several minutes and adequate Docker disk space for the initial Rust build and cluster images.

Install the pinned Linkerd CLI if needed:

```sh
curl --proto '=https' --tlsv1.2 -sSfL https://run.linkerd.io/install-edge \
  | LINKERD2_VERSION=edge-26.8.2 sh
export PATH="$HOME/.linkerd2/bin:$PATH"
```

Then:

```sh
git clone https://github.com/cprayer/linkerd-replay-body-repro.git
cd linkerd-replay-body-repro

bash scripts/cluster.sh up
bash scripts/build-proxies.sh
bash fixture/build.sh
python3 scripts/reproduce.py
```

The default cluster is `linkerd-replay-repro`. Installation pins Kubernetes `v1.36.1`, Gateway API `v1.5.1`, and Linkerd `edge-26.8.2`. All Kubernetes commands select the named cluster explicitly. Existing clusters are checked rather than reinstalled.

The two proxy images are built from upstream commit [`e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f`](https://github.com/linkerd/linkerd2-proxy/commit/e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f). `before` uses that source unchanged; `after` applies only the included patch. Builds use `Cargo.lock` and native container architecture. No fork checkout, private registry, prebuilt investigation image, or personal filesystem path is required.

## Scenarios and expected results

| Scenario | Before | After |
|---|---|---|
| REFUSED_STREAM before DATA arrives | First chunk duplicated on the retry | Exact body |
| FailFast on an unavailable backend, then retry a healthy backend | Body duplication for requests that took this retry path | Exact body |
| HTTP 503 after consuming the complete body | Retry with an exact body | Same |
| HTTP 503 before DATA arrives | In this fixture, the response is returned without retry | Same |
| Healthy backend | Exact body without retry | Same |

The REFUSED_STREAM fixture sends HEADERS, waits 500ms before client DATA, and refuses the first upstream attempt. The delay creates a reproducible unread-body state; it is not a required production delay.

The FailFast case sends DATA immediately. A route selects between an empty Service and a healthy Service. Requests queued on the empty backend fail fast and may retry the healthy backend. Backend selection is probabilistic: the runner must observe the retry and corruption before calling this a successful reproduction. A run that does not exercise the required condition is inconclusive, not a pass.

An HTTP 200 or gRPC OK alone does not prove body integrity. The runner compares request IDs, body bytes, payload hashes, server attempts, and actual Linkerd retry counters. Early 503 is a control, not proof that every early response is unaffected.

The fixture's optional `-grpc` flag tests gRPC status headers over raw HTTP/2; it is not a generated Protobuf client. The main reproduction does not require it. See [fixture details](fixture/README.md).

## Results and cleanup

Each run saves commands, client/server logs, proxy logs, metrics, and a machine-readable summary under `.artifacts/`. These generated files are ignored by Git. Each run uses its own namespace and removes its own test workloads unless preservation is requested by the runner.

Delete the dedicated cluster explicitly when finished:

```sh
bash scripts/cluster.sh delete
```

Build caches are named Docker volumes. `TARGET_VOLUME`, `CARGO_VOLUME`, `BUILD_JOBS`, `PROXY_IMAGE`, `RUST_IMAGE`, and `RUNTIME_IMAGE` can be overridden for the proxy build. `FIXTURE_IMAGE` selects the fixture build tag. The runner accepts `CLUSTER_NAME`, `LINKERD_BIN`, `FIXTURE_IMAGE`, `BEFORE_IMAGE`, and `AFTER_IMAGE` overrides. Use matching image names between build and run.

The fixture can also be checked without Kubernetes:

```sh
python3 fixture/smoke.py
```

This reproduces request-body corruption in a local test environment. It does not measure production incidence, application-side duplicate execution, or a multi-cluster service-mirror topology.

## License

Apache-2.0. The proxy patch applies to the Apache-2.0-licensed Linkerd proxy; the upstream repository retains its own copyright and license notices.
