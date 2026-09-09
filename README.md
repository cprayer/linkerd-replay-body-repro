# Linkerd ReplayBody reproduction

Reproduces an HTTP/2 request-body duplication bug in Linkerd using kind. If a retry starts before any body data has been read, `ReplayBody` can send the first chunk twice within the same attempt.

Both proxy images use upstream commit [`e5de317d`](https://github.com/linkerd/linkerd2-proxy/commit/e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f). The `after` image adds only the [one-line fix](patches/replay-unpolled-body.patch).

## Run

Requires Docker, kind, kubectl, Git, Python 3.9+, Bash, and Linkerd CLI `edge-26.8.2`. Builds run in Docker; proxy builds use two CPUs and 4 GiB of memory by default.

Install the matching CLI if needed:

```sh
curl --proto '=https' --tlsv1.2 -sSfL https://run.linkerd.io/install-edge \
  | LINKERD2_VERSION=edge-26.8.2 sh
export PATH="$HOME/.linkerd2/bin:$PATH"
```

```sh
git clone https://github.com/cprayer/linkerd-replay-body-repro.git
cd linkerd-replay-body-repro

bash scripts/build-proxies.sh
bash fixture/build.sh
bash scripts/cluster.sh up
python3 scripts/reproduce.py
```

The `linkerd-replay-repro` cluster uses Kubernetes `v1.36.1`, Gateway API `v1.5.1`, and Linkerd `edge-26.8.2`.

## Expected results

The client sends each request once. The runner checks body bytes, hashes, server attempts, and Linkerd retry counters.

| Scenario | Before | After |
|---|---|---|
| REFUSED_STREAM before DATA | Duplicated body | Exact body |
| FailFast, then retry a healthy backend | Duplicated body on retry | Exact body |
| HTTP 503 after reading the body | Exact body on retry | Same |
| HTTP 503 before DATA | 503 without retry in this fixture | Same |
| Healthy backend | Exact body, no retry | Same |

The REFUSED_STREAM case delays client DATA by 500ms. FailFast backend selection is probabilistic; rerun if the result is `INCONCLUSIVE`.

Logs, metrics, and `summary.json` are saved under `.artifacts/<run>/`. Exit codes: `0` for expected results, `1` for failure, `2` for an inconclusive run.

## Cleanup

The runner deletes its test namespace. To also delete the cluster:

```sh
bash scripts/cluster.sh delete
```

## License

[Apache-2.0](LICENSE)
