# HTTP/2 ping and echo

[client.go](client.go) makes one HTTP request with `ping`, reads the response, and prints both strings.
[server.go](server.go) reads the request body and writes those bytes back.
[main.go](main.go) only selects the role and address.

```sh
sh fixture/build.sh

# Start the echo server, then run a client in its network namespace.
docker run -d --name echo-server --entrypoint /echo-h2 linkerd-replay-fixture:local
docker run --rm --network container:echo-server --entrypoint /echo-h2 \
  linkerd-replay-fixture:local -mode client -id example
```

```text
SERVER example RECEIVED "ping" (4 bytes)
ECHO example SENT "ping" (4 bytes) RECEIVED "ping" (4 bytes) HTTP 200
```

The app uses Go's HTTP client and `golang.org/x/net/http2` for plaintext HTTP/2 (h2c).
The request body is streamed without Content-Length, so the server can echo duplicated bytes
instead of rejecting them for exceeding a declared length. The app has no delay or warmup;
Python starts concurrent clients. Packet capture checks one input request per ID.

In the standalone reproduction, `failfast` and `healthy` use this app. FailFast retries are
triggered by the proxy route's empty backend; the app always echoes the body it receives.
The client exits nonzero on an unexpected response, including `pingping`.

The existing 500 ms delay, REFUSED_STREAM, 503, and gRPC controls remain in
[frames/](frames/README.md), built as `/audit-h2`. Their CLI and JSON logs are unchanged.
`python3 fixture/smoke.py` checks those nine direct invocations; `go test -race ./...`
checks both apps, including the simple client's rejection of duplicated responses.
