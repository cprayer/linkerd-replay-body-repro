# Linkerd ReplayBody reproduction

Reproduces an HTTP/2 request-body duplication bug and verifies the [one-line fix](patches/replay-unpolled-body.patch), using upstream commit [`e5de317d`](https://github.com/linkerd/linkerd2-proxy/commit/e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f).

## Run

Docker is the only prerequisite. Build once, then run the image:

```sh
docker build -t linkerd-replay-repro https://github.com/cprayer/linkerd-replay-body-repro.git
docker run -d --name replay-repro linkerd-replay-repro --runs 5
docker logs -f replay-repro
```

The image contains prebuilt before/after proxies and an HTTP/2 fixture using upstream's integration harness. The first image build compiles Rust and takes several minutes.

## Results

The [Go echo application](fixture/echo.go) returns the received body unchanged.
The client sends `ping` and prints its actual response through Linkerd:

```text
ECHO refused-before-000 SENT "ping" (4 bytes) RECEIVED "pingping" (8 bytes) HTTP 200
ECHO refused-after-000 SENT "ping" (4 bytes) RECEIVED "ping" (4 bytes) HTTP 200
```

`ECHO` is printed by the Go client's `exchange()` function in [fixture/main.go](fixture/main.go)
after receiving the complete HTTP 200 response. `SENT` is the original client payload;
`RECEIVED` is the actual response body. This is client output, not a Linkerd or server log.
Each line includes the request ID and HTTP status. The runner forwards the `-000` request's
echo from each scenario to the Docker console; the raw client logs contain every request.
`report.md` also shows the sent/received strings with direct links to those logs.

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

The existing summaries and fixture/proxy logs are also accompanied by packet evidence.
Follow **Packet captures and DATA comparisons → Run → a duplicate count** to see request IDs,
input/output body lengths and contents, and packet/TCP/HTTP/2 stream numbers.
Each backend retry is compared separately, so a normal retry is not counted as body duplication.

Each scenario saves a `.pcap` captured by `tcpdump` and an `.http2.txt` decoded by TShark.
The comparison reads captured HTTP/2 headers and DATA, independently of fixture JSON logs.
Open the PCAP in Wireshark or use the exact TShark command and filters included in the comparison report.
Capture errors, reported packet drops, incomplete evidence, or disagreement with fixture counts fail the run.
Only failed batches create `errors.log`, combining console, fixture, and proxy logs with failure details.
Capture statistics, TShark diagnostic logs, `.stderr` files, and intermediate PDML files are not saved separately.

Capture is enabled by default in the standalone Docker image and uses its isolated loopback interface.
The image grants only `tcpdump` the `NET_RAW` file capability; the runner stays non-root.
Docker's default capabilities suffice. If your runtime drops `NET_RAW` or disables file capabilities,
allow capture for this container before running it.

## How the reproduction works

```text
Go client → Linkerd outbound proxy → Go echo server
```

The [harness](harness/replay.rs) runs actual Linkerd proxy code with test destination and
policy gRPC services in the same process. The proxy receives backend addresses and an HTTP/2
route with at most one retry, a 64 KiB replay limit, and HTTP 503 as a retryable status.
The Docker image builds the same harness against the original source and the one-line patch.

The [Go client](fixture/main.go) uses HTTP/2 frames directly to control when the body arrives.
Each measured request has its own connection and ID. It sends HEADERS, then schedules one DATA
frame containing `ping` with END_STREAM; an early completed response can cancel that write.
Retries are performed by Linkerd. The client collects the response body, prints it in the
`ECHO` line, and compares its bytes with the original payload.

The [runner](scripts/standalone.py) executes these cases against both proxy versions:

| Case | Concurrent requests | HEADERS → DATA delay | Backend behavior |
|---|---|---|---|
| REFUSED_STREAM | 10 | 500 ms | Rejects the first attempt on HEADERS; echoes the retry |
| Consumed-body 503 | 10 | 500 ms | Reads the entire first body before returning 503; echoes the retry |
| Early 503 | 10 | 500 ms | Returns 503 on the first attempt's HEADERS |
| Healthy | 10 | 0 ms | Echoes the received body |
| FailFast | 20 | 0 ms | Routes between an empty backend and a healthy echo backend |

Cases other than FailFast first send a separate bodyless warmup request on each connection.
The REFUSED_STREAM delay gives the rejection time to arrive before the client sends DATA.

FailFast uses `RandomAvailable` with **empty backend weight 100 : healthy backend weight 1**.
The empty backend is advertised with no endpoints, making it possible for a request to fail
before its body is read. The weights favor that path while retaining a healthy backend for
the retry; they do not guarantee a fixed failure percentage because backend availability
also affects selection. FailFast skips warmup to exercise backend startup. If the required
retry/duplication is not observed, the run is INCONCLUSIVE and can be repeated by the runner.

## License

[Apache-2.0](LICENSE)
