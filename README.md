# Linkerd ReplayBody reproduction

Reproduces HTTP/2 request-body duplication and checks the [one-line fix](patches/replay-unpolled-body.patch)
against upstream commit [`e5de317d`](https://github.com/linkerd/linkerd2-proxy/commit/e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f).

## Run

Docker is the only prerequisite. The first build compiles the before/after Linkerd proxies and takes several minutes.

```sh
docker build -t linkerd-replay-repro https://github.com/cprayer/linkerd-replay-body-repro.git
docker run -d --name replay-repro linkerd-replay-repro --runs 5
docker logs -f replay-repro
```

## Check the result

The example below shows **FailFast, the simplest reproduction**: the client sends `ping`
through Linkerd, and the server echoes the received body unchanged.

```text
ECHO failfast-before-000 SENT "ping" (4 bytes) RECEIVED "pingping" (8 bytes) HTTP 200
ECHO failfast-after-000 SENT "ping" (4 bytes) RECEIVED "ping" (4 bytes) HTTP 200
```

`ECHO` is printed by the Go client after reading the response; it is not a Linkerd log.
In this FailFast example, `ping` returns as `pingping` before the fix and as `ping` after it.

For the full run across the scenarios below, **PASS means the bug was reproduced before
the fix and all checks passed after it.**
INCONCLUSIVE runs get up to two extra attempts; FAIL is never retried.

| Scenarios | Application |
|---|---|
| FailFast, Healthy | Simple [client.go](fixture/client.go) / [server.go](fixture/server.go), without added delay or warmup |
| REFUSED_STREAM, consumed-body 503, early 503 | [fixture/frames/](fixture/frames/README.md), with controlled HTTP/2 frames and a 500 ms body delay |

FailFast uses an empty backend with weight **100** and a healthy echo backend with weight **1**,
with at most one proxy retry. The weights favor the failure path; the observed count can vary.

Copy the results:

```sh
docker cp replay-repro:/results ./results
```

Open the latest directory's **report.md** for sent/received payloads, duplicate counts, and links to raw logs.
Follow **Packet captures and DATA comparisons** to compare HTTP/2 DATA before and after the proxy,
independently of application logs. Each case includes a PCAP, TShark decode, and packet comparison.
Failures also produce one **errors.log** with combined diagnostics.

To repeat using the same container: `docker start -a replay-repro`.

## License

[Apache-2.0](LICENSE)
