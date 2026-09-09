# Linkerd ReplayBody reproduction

Reproduces an HTTP/2 request-body duplication bug and verifies the [one-line fix](patches/replay-unpolled-body.patch), using upstream commit [`e5de317d`](https://github.com/linkerd/linkerd2-proxy/commit/e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f).

## Run

Docker is the only prerequisite. Build once, then run the image:

```sh
docker build -t linkerd-replay-repro https://github.com/cprayer/linkerd-replay-body-repro.git
docker run -d --name replay-repro linkerd-replay-repro --runs 5
docker logs -f replay-repro
```

The image contains prebuilt before/after proxies and an HTTP/2 fixture. It runs as a non-root user using upstream's integration harness for local routing and policy, without Kubernetes or additional container privileges. The first image build compiles Rust and takes several minutes.

## Results

The log ends with one row per run: **before duplicates → after duplicates → PASS/FAIL**. PASS means the original bug was reproduced and the fix passed all checks.

Checks cover REFUSED_STREAM, FailFast, consumed-body 503, early 503, and a healthy backend. INCONCLUSIVE results get up to two extra attempts; FAIL is never retried.

Repeat the same run:

```sh
docker start -a replay-repro
```

Copy the summary table and raw logs if needed:

```sh
docker cp replay-repro:/results ./results
```

Open the latest directory's `report.md`; its **Logs** links lead directly to details.

## License

[Apache-2.0](LICENSE)
