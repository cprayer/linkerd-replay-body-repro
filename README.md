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

The [Go client](fixture/client.go) sends the request once; the [Go server](fixture/server.go) echoes the received body unchanged.
The client sends `ping` and prints its actual response through Linkerd:

```text
ECHO failfast-before-000 SENT "ping" (4 bytes) RECEIVED "pingping" (8 bytes) HTTP 200
ECHO failfast-after-000 SENT "ping" (4 bytes) RECEIVED "ping" (4 bytes) HTTP 200
```

Each `ECHO` line includes the request ID and HTTP status. One HTTP 200 echo per scenario is shown
in the Docker console; the raw client logs contain every request. `report.md` also shows
the sent/received strings with direct links to those logs.

The log ends with one row per run: **before duplicates → after duplicates → PASS/FAIL**. PASS means the original bug was reproduced and the fix passed all checks.

Checks cover REFUSED_STREAM, FailFast, consumed-body 503, early 503, and a healthy backend.
The **FailFast case uses the simple HTTP/2 app without a client delay**: the client sends `ping`,
and the proxy can retry from an empty backend to the echo server. The healthy control uses the same app.
The frame-controlled [REFUSED_STREAM experiment](fixture/frames/README.md) sends HEADERS,
waits 500 ms, then sends `ping`; its original controls and JSON logs remain available.
The echo table in `report.md` shows both cases and their client delays. INCONCLUSIVE results get up to two extra attempts; FAIL is never retried.

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

## License

[Apache-2.0](LICENSE)
