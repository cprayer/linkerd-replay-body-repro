# HTTP/2 fixture

Build with Docker; no local Go toolchain or module cache is needed:

```sh
sh fixture/build.sh
python3 fixture/smoke.py
```

The image is `linkerd-replay-fixture:local` (override with `FIXTURE_IMAGE`). It includes `/audit-h2` and BusyBox, so a client pod can run `/bin/sleep 3600` and invoke `/audit-h2` with `kubectl exec`.

```sh
# First attempt for each x-audit-id is refused; later attempts echo their body.
/audit-h2 -mode server -listen :8080 -failure refused
# Alternatives: -failure http503, grpc14, or none.
# Control: add -consume-first to read the first attempt completely before failing.

/audit-h2 -mode client -addr audit-server:8080 -authority audit-server:8080 \
  -id case-001 -payload 'hello world' -delay-ms 500
# Control: -delay-ms 0. Concurrency: -concurrency 20 -warmup=false.
```

The client **never retries**. It validates the echoed audit ID, byte count, payload, and SHA256. The default warmup uses a separate bodyless stream (`/audit/warmup`, HTTP 204) and does not increment attempt counters. Concurrent clients use separate connections and unique suffixed IDs. Payloads are limited to 16,384 bytes; the socket deadline is 15 seconds.

Server failures are `RST_STREAM(REFUSED_STREAM)`, HTTP 503 with END_STREAM, or HTTP 200 with trailers-only `grpc-status: 14`. Success echoes raw bytes in a JSON response. `-grpc` sets request headers to exercise gRPC status-based policies; this is an HTTP/2 transport fixture, **not a generated gRPC client**. It does not encode Protobuf messages or gRPC DATA envelopes.

JSON logs include UTC timestamps, connection/stream IDs, attempt numbers, cumulative DATA bytes, rejection, end-of-stream, resets, and received GOAWAY. The server does not send GOAWAY. Compare `client_headers`, `reject`, `client_data`, and `request_end` to verify actual timing and byte counts. A delay is an experimental control, not proof by itself that the proxy has not read the body.

Optional server `-initial-window 0` grants capacity to retry/consume-first streams. It does not prevent Hyper from polling a body. Use the default 65,535 window for direct smoke tests: the raw client assumes that default send window. Delay-based reproduction does not require zero window.

`smoke.py` runs nine explicit invocations covering all failures, consumed-body HTTP 503, and immediate success. It checks the exact error, attempt number, 11-byte echo, and rejection-time byte count. It creates uniquely named local containers and removes them afterward. Ignored logs are written to `fixture/results/smoke/`.
