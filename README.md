# Linkerd ReplayBody reproduction

Reproduces an HTTP/2 request-body duplication bug in Linkerd using kind. If a retry starts before any body data has been read, `ReplayBody` can send the first chunk twice within the same attempt.

Both proxy images use upstream commit [`e5de317d`](https://github.com/linkerd/linkerd2-proxy/commit/e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f). The `after` image adds only the [one-line fix](patches/replay-unpolled-body.patch).

## Run

Requires Docker with Buildx, kind, kubectl, Python 3.9+, Bash, and Git to clone the repository. If Linkerd CLI `edge-26.8.2` is not installed, the runner uses curl and the official installer to install it under `.cache/linkerd2/`.

```sh
git clone https://github.com/cprayer/linkerd-replay-body-repro.git
cd linkerd-replay-body-repro

python3 scripts/run.py --runs 5
```

The runner builds both proxies and the fixture once, creates or checks the dedicated cluster, then runs the reproduction five times. An `INCONCLUSIVE` run gets up to two extra attempts; `FAIL` is not retried. All attempts are retained, and a failed run makes the overall result fail even if later runs pass. Use `--retries 0` to disable extra attempts.

Repeat using the existing images and cluster:

```sh
python3 scripts/run.py --skip-build --runs 5
```

The terminal shows progress and a compact before/after result table. The same table is saved as `report.md`; full output is saved in `.artifacts/batches/<batch>/runner.log`.

Proxy compilation uses a multi-stage Dockerfile: it fetches the pinned upstream commit, verifies the patch, builds the binary, and copies it directly into the runtime image. Source and binary output do not use host bind mounts. BuildKit manages the Cargo and target caches in a dedicated `linkerd-replay-builder`; the builder uses two CPUs and 4 GiB of memory by default. Override with `BUILD_CPUS`, `BUILD_MEMORY`, and `BUILD_JOBS` if needed. The first build compiles both variants; subsequent builds reuse the cache.

The runner runs on the host and uses Docker for builds and kind nodes. It does not require mounting the host Docker socket into a runner container. For a separate cluster or output directory, use `--cluster <name>` or `--artifacts <directory>`.

The `linkerd-replay-repro` cluster uses Kubernetes `v1.36.1`, Gateway API `v1.5.1`, and Linkerd `edge-26.8.2`.

The individual commands remain available with the matching Linkerd CLI on `PATH` (or set `LINKERD_BIN`):

```sh
bash scripts/build-proxies.sh
bash fixture/build.sh
bash scripts/cluster.sh up
python3 scripts/reproduce.py
```

## Expected results

The client sends each request once. The runner checks body bytes, hashes, server attempts, and Linkerd retry counters.

| Scenario | Before | After |
|---|---|---|
| REFUSED_STREAM before DATA | Duplicated body | Exact body |
| FailFast, then retry a healthy backend | Duplicated body on retry | Exact body |
| HTTP 503 after reading the body | Exact body on retry | Same |
| HTTP 503 before DATA | 503 without retry in this fixture | Same |
| Healthy backend | Exact body, no retry | Same |

The REFUSED_STREAM case delays client DATA by 500ms. FailFast backend selection is probabilistic; the batch runner can retry an `INCONCLUSIVE` result.

Logs, metrics, and `summary.json` are saved under `.artifacts/<run>/`. Exit codes: `0` for expected results, `1` for failure, `2` for an inconclusive run.

## Results

Open **`report.md` at the path printed when the command finishes**. It shows the overall result and one row per run: duplicated request counts before/after the fix, and PASS/FAIL/INCONCLUSIVE.

- **PASS**: the bug was reproduced before the fix and all checks passed after it
- **FAIL**: a check or setup step failed; open that row's **Logs** for the error
- **INCONCLUSIVE**: the probabilistic FailFast case needs another attempt; the runner retries up to `--retries` times

For setup failures, open the **Full log** link. Raw client/server events, proxy logs, metrics, and JSON results are retained alongside the summary. Share the entire result directory if further investigation is needed.

## Cleanup

The runner deletes its test namespace. To also delete the cluster:

```sh
bash scripts/cluster.sh delete
```

For a custom cluster, use the same name: `CLUSTER_NAME=<name> bash scripts/cluster.sh delete`. Batch logs and build caches remain available for later runs.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## License

[Apache-2.0](LICENSE)
