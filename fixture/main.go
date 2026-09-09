package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/net/http2"
	"golang.org/x/net/http2/hpack"
)

var output sync.Mutex

func event(kind string, fields map[string]any) {
	fields["event"], fields["time"] = kind, time.Now().UTC().Format(time.RFC3339Nano)
	output.Lock()
	defer output.Unlock()
	_ = json.NewEncoder(os.Stdout).Encode(fields)
}

type wire struct {
	f  *http2.Framer
	mu sync.Mutex
}

func newWire(c net.Conn) *wire {
	f := http2.NewFramer(c, c)
	f.ReadMetaHeaders = hpack.NewDecoder(4096, nil)
	return &wire{f: f}
}
func (w *wire) headers(stream uint32, fields []hpack.HeaderField, end bool) error {
	var b bytes.Buffer
	e := hpack.NewEncoder(&b)
	for _, f := range fields {
		if err := e.WriteField(f); err != nil {
			return err
		}
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.f.WriteHeaders(http2.HeadersFrameParam{StreamID: stream, BlockFragment: b.Bytes(), EndHeaders: true, EndStream: end})
}
func (w *wire) data(stream uint32, data []byte, end bool) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.f.WriteData(stream, end, data)
}
func (w *wire) ack() error { w.mu.Lock(); defer w.mu.Unlock(); return w.f.WriteSettingsAck() }
func (w *wire) window(stream, n uint32) error {
	if n == 0 {
		return nil
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.f.WriteWindowUpdate(stream, n)
}
func (w *wire) reset(stream uint32, reason http2.ErrCode) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.f.WriteRSTStream(stream, reason)
}
func digest(b []byte) string { d := sha256.Sum256(b); return hex.EncodeToString(d[:]) }

type options struct {
	mode, listen, addr, authority, failure, id, payload string
	delay, concurrency                                  int
	warmup, grpc, consume                               bool
	initialWindow                                       uint
}

func main() {
	var o options
	flag.StringVar(&o.mode, "mode", "server", "server or client")
	flag.StringVar(&o.listen, "listen", ":8080", "server address")
	flag.StringVar(&o.addr, "addr", "127.0.0.1:8080", "client target")
	flag.StringVar(&o.authority, "authority", "audit.test", "request authority")
	flag.StringVar(&o.failure, "failure", "refused", "first attempt: refused, http503, grpc14, or none")
	flag.StringVar(&o.id, "id", "", "audit ID; generated when omitted")
	flag.StringVar(&o.payload, "payload", "hello world", "request DATA payload")
	flag.IntVar(&o.delay, "delay-ms", 500, "delay between HEADERS and DATA")
	flag.IntVar(&o.concurrency, "concurrency", 1, "parallel clients; each uses a separate connection and unique audit ID")
	flag.BoolVar(&o.warmup, "warmup", true, "send separate bodyless warmup request first")
	flag.BoolVar(&o.grpc, "grpc", false, "set request content-type application/grpc")
	flag.BoolVar(&o.consume, "consume-first", false, "consume complete first attempt body before refusing")
	flag.UintVar(&o.initialWindow, "initial-window", 65535, "server initial stream window; 0 allows only retry/consume-first streams")
	flag.Parse()
	if o.mode == "server" {
		if err := serve(o); err != nil {
			event("fatal", map[string]any{"error": err.Error()})
			os.Exit(1)
		}
		return
	}
	if o.mode != "client" {
		fmt.Fprintln(os.Stderr, "invalid mode")
		os.Exit(2)
	}
	if o.id == "" {
		o.id = fmt.Sprintf("audit-%d", time.Now().UnixNano())
	}
	if len(o.payload) > 16384 || o.delay < 0 || o.concurrency < 1 {
		fmt.Fprintln(os.Stderr, "payload must be <=16384 bytes, delay >=0, concurrency >=1")
		os.Exit(2)
	}
	var wg sync.WaitGroup
	var failed atomic.Int64
	for i := 0; i < o.concurrency; i++ {
		local := o
		if o.concurrency > 1 {
			local.id = fmt.Sprintf("%s-%03d", o.id, i)
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := client(local); err != nil {
				failed.Add(1)
				event("client_failure", map[string]any{"id": local.id, "error": err.Error()})
			}
		}()
	}
	wg.Wait()
	event("client_summary", map[string]any{"total": o.concurrency, "failed": failed.Load(), "succeeded": int64(o.concurrency) - failed.Load()})
	if failed.Load() > 0 {
		os.Exit(1)
	}
}

type attempts struct {
	sync.Mutex
	ids  map[string]int
	conn atomic.Uint64
}

func (a *attempts) next(id string) int { a.Lock(); defer a.Unlock(); a.ids[id]++; return a.ids[id] }

type request struct {
	id                 string
	attempt            int
	data               []byte
	failed, done, grpc bool
}

func serve(o options) error {
	if o.failure != "refused" && o.failure != "http503" && o.failure != "grpc14" && o.failure != "none" {
		return fmt.Errorf("invalid failure %q", o.failure)
	}
	if o.initialWindow > 2147483647 {
		return fmt.Errorf("invalid initial-window")
	}
	listener, err := net.Listen("tcp", o.listen)
	if err != nil {
		return err
	}
	event("listening", map[string]any{"addr": o.listen, "failure": o.failure, "consume_first": o.consume, "initial_window": o.initialWindow})
	a := &attempts{ids: make(map[string]int)}
	for {
		c, err := listener.Accept()
		if err != nil {
			return err
		}
		conn := a.conn.Add(1)
		go serverConn(c, conn, a, o)
	}
}
func serverConn(c net.Conn, conn uint64, a *attempts, o options) {
	defer c.Close()
	fields := func(stream uint32, r *request) map[string]any {
		m := map[string]any{"conn": conn, "stream": stream}
		if r != nil {
			m["id"] = r.id
			m["attempt"] = r.attempt
			m["bytes"] = len(r.data)
		}
		return m
	}
	event("connection", map[string]any{"conn": conn, "remote": c.RemoteAddr().String()})
	preface := make([]byte, len(http2.ClientPreface))
	if _, err := io.ReadFull(c, preface); err != nil {
		return
	}
	if string(preface) != http2.ClientPreface {
		return
	}
	w := newWire(c)
	if err := w.f.WriteSettings(http2.Setting{ID: http2.SettingInitialWindowSize, Val: uint32(o.initialWindow)}); err != nil {
		return
	}
	requests := make(map[uint32]*request)
	defer func() {
		for stream, r := range requests {
			m := fields(stream, r)
			m["sha256"] = digest(r.data)
			m["failed"] = r.failed
			m["done"] = r.done
			event("stream_summary", m)
		}
	}()
	fail := func(stream uint32, r *request) error {
		r.failed = true
		m := fields(stream, r)
		m["failure"] = o.failure
		event("reject", m)
		switch o.failure {
		case "refused":
			return w.reset(stream, http2.ErrCodeRefusedStream)
		case "http503":
			return w.headers(stream, []hpack.HeaderField{{Name: ":status", Value: "503"}, {Name: "content-length", Value: "0"}}, true)
		case "grpc14":
			return w.headers(stream, []hpack.HeaderField{{Name: ":status", Value: "200"}, {Name: "content-type", Value: "application/grpc"}, {Name: "grpc-status", Value: "14"}, {Name: "grpc-message", Value: "audit refusal"}}, true)
		}
		return nil
	}
	finish := func(stream uint32, r *request) error {
		r.done = true
		m := fields(stream, r)
		m["sha256"] = digest(r.data)
		event("request_end", m)
		if r.failed {
			return nil
		}
		if r.attempt == 1 && o.failure != "none" {
			return fail(stream, r)
		}
		payload, _ := json.Marshal(map[string]any{"id": r.id, "attempt": r.attempt, "bytes": len(r.data), "payload": string(r.data), "sha256": digest(r.data)})
		if err := w.headers(stream, []hpack.HeaderField{{Name: ":status", Value: "200"}, {Name: "content-type", Value: "application/json"}, {Name: "content-length", Value: strconv.Itoa(len(payload))}}, false); err != nil {
			return err
		}
		if err := w.data(stream, payload, true); err != nil {
			return err
		}
		event("success", fields(stream, r))
		return nil
	}
	for {
		frame, err := w.f.ReadFrame()
		if err != nil {
			event("connection_end", map[string]any{"conn": conn, "error": err.Error()})
			return
		}
		switch f := frame.(type) {
		case *http2.SettingsFrame:
			if !f.IsAck() {
				if err = w.ack(); err != nil {
					return
				}
			}
		case *http2.PingFrame:
			if !f.Flags.Has(http2.FlagPingAck) {
				w.mu.Lock()
				err = w.f.WritePing(true, f.Data)
				w.mu.Unlock()
				if err != nil {
					return
				}
			}
		case *http2.MetaHeadersFrame:
			stream := f.StreamID
			if r := requests[stream]; r != nil {
				event("request_trailers", fields(stream, r))
				if f.StreamEnded() {
					if err = finish(stream, r); err != nil {
						return
					}
				}
				continue
			}
			id, path, content := "", "", ""
			for _, h := range f.Fields {
				switch h.Name {
				case "x-audit-id":
					id = h.Value
				case ":path":
					path = h.Value
				case "content-type":
					content = h.Value
				}
			}
			if path == "/audit/warmup" {
				event("warmup", map[string]any{"conn": conn, "stream": stream})
				if err = w.headers(stream, []hpack.HeaderField{{Name: ":status", Value: "204"}}, true); err != nil {
					return
				}
				continue
			}
			if id == "" {
				id = fmt.Sprintf("missing-id-%d-%d", conn, stream)
			}
			r := &request{id: id, attempt: a.next(id), grpc: content == "application/grpc"}
			requests[stream] = r
			m := fields(stream, r)
			m["end_stream"] = f.StreamEnded()
			event("headers", m)
			if r.attempt == 1 && o.failure != "none" && !o.consume {
				if err = fail(stream, r); err != nil {
					return
				}
			} else if o.initialWindow == 0 {
				if err = w.window(stream, 65535); err != nil {
					return
				}
			}
			if f.StreamEnded() {
				if err = finish(stream, r); err != nil {
					return
				}
			}
		case *http2.DataFrame:
			r := requests[f.StreamID]
			if r == nil {
				event("unexpected_data", map[string]any{"conn": conn, "stream": f.StreamID})
				continue
			}
			r.data = append(r.data, f.Data()...)
			m := fields(f.StreamID, r)
			m["frame_bytes"] = len(f.Data())
			m["end_stream"] = f.StreamEnded()
			event("data", m)
			if err = w.window(0, uint32(f.Length)); err != nil {
				return
			}
			if !r.failed {
				if err = w.window(f.StreamID, uint32(f.Length)); err != nil {
					return
				}
			}
			if f.StreamEnded() {
				if err = finish(f.StreamID, r); err != nil {
					return
				}
			}
		case *http2.RSTStreamFrame:
			m := fields(f.StreamID, requests[f.StreamID])
			m["code"] = f.ErrCode.String()
			event("reset", m)
		case *http2.GoAwayFrame:
			event("goaway", map[string]any{"conn": conn, "last_stream": f.LastStreamID, "code": f.ErrCode.String()})
		}
	}
}

func client(o options) error {
	c, err := net.DialTimeout("tcp", o.addr, 5*time.Second)
	if err != nil {
		return err
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(15 * time.Second))
	if _, err = io.WriteString(c, http2.ClientPreface); err != nil {
		return err
	}
	w := newWire(c)
	if err = w.f.WriteSettings(); err != nil {
		return err
	}
	stream := uint32(1)
	if o.warmup {
		if err = exchange(w, stream, o, true); err != nil {
			return fmt.Errorf("warmup: %w", err)
		}
		stream += 2
	}
	return exchange(w, stream, o, false)
}
func exchange(w *wire, stream uint32, o options, warmup bool) error {
	path := "/audit.Service/Call"
	if warmup {
		path = "/audit/warmup"
	}
	content := "application/octet-stream"
	if o.grpc {
		content = "application/grpc"
	}
	fields := []hpack.HeaderField{{Name: ":method", Value: "POST"}, {Name: ":scheme", Value: "http"}, {Name: ":authority", Value: o.authority}, {Name: ":path", Value: path}, {Name: "x-audit-id", Value: o.id}, {Name: "content-type", Value: content}, {Name: "te", Value: "trailers"}}
	if err := w.headers(stream, fields, warmup); err != nil {
		return err
	}
	event("client_headers", map[string]any{"id": o.id, "stream": stream, "warmup": warmup, "delay_ms": o.delay})
	cancel := make(chan struct{})
	defer close(cancel)
	if !warmup {
		go func() {
			timer := time.NewTimer(time.Duration(o.delay) * time.Millisecond)
			defer timer.Stop()
			select {
			case <-cancel:
				return
			case <-timer.C:
			}
			// Fixtures use payloads below the default 65,535-byte stream window.
			err := w.data(stream, []byte(o.payload), true)
			m := map[string]any{"id": o.id, "stream": stream, "bytes": len(o.payload), "sha256": digest([]byte(o.payload))}
			if err != nil {
				m["error"] = err.Error()
			}
			event("client_data", m)
		}()
	}
	var body []byte
	status, grpcStatus := "", ""
	for {
		frame, err := w.f.ReadFrame()
		if err != nil {
			return err
		}
		ended := false
		switch f := frame.(type) {
		case *http2.SettingsFrame:
			if !f.IsAck() {
				if err = w.ack(); err != nil {
					return err
				}
			}
		case *http2.PingFrame:
			if !f.Flags.Has(http2.FlagPingAck) {
				w.mu.Lock()
				err = w.f.WritePing(true, f.Data)
				w.mu.Unlock()
				if err != nil {
					return err
				}
			}
		case *http2.MetaHeadersFrame:
			if f.StreamID != stream {
				continue
			}
			for _, h := range f.Fields {
				if h.Name == ":status" {
					status = h.Value
				}
				if h.Name == "grpc-status" {
					grpcStatus = h.Value
				}
			}
			ended = f.StreamEnded()
			event("client_response_headers", map[string]any{"id": o.id, "stream": stream, "status": status, "grpc_status": grpcStatus, "end_stream": ended})
		case *http2.DataFrame:
			if f.StreamID != stream {
				continue
			}
			body = append(body, f.Data()...)
			ended = f.StreamEnded()
			_ = w.window(0, uint32(f.Length))
			_ = w.window(stream, uint32(f.Length))
		case *http2.RSTStreamFrame:
			if f.StreamID == stream {
				return fmt.Errorf("RST_STREAM(%s)", f.ErrCode)
			}
		case *http2.GoAwayFrame:
			event("client_goaway", map[string]any{"last_stream": f.LastStreamID, "code": f.ErrCode.String()})
			if stream > f.LastStreamID {
				return fmt.Errorf("GOAWAY excludes stream %d", stream)
			}
		}
		if !ended {
			continue
		}
		if warmup {
			if status != "204" {
				return fmt.Errorf("warmup status %s", status)
			}
			event("client_warmup_done", map[string]any{"id": o.id, "stream": stream})
			return nil
		}
		event("client_response", map[string]any{"id": o.id, "status": status, "grpc_status": grpcStatus, "body": string(body)})
		if status != "200" || (grpcStatus != "" && grpcStatus != "0") {
			return fmt.Errorf("status=%s grpc-status=%s", status, grpcStatus)
		}
		var received struct {
			ID      string `json:"id"`
			Attempt int    `json:"attempt"`
			Bytes   int    `json:"bytes"`
			Payload string `json:"payload"`
			SHA256  string `json:"sha256"`
		}
		if err = json.Unmarshal(body, &received); err != nil {
			return err
		}
		if received.ID != o.id || received.Bytes != len(o.payload) || received.Payload != o.payload || received.SHA256 != digest([]byte(o.payload)) {
			return fmt.Errorf("payload mismatch: %s", body)
		}
		event("client_success", map[string]any{"id": o.id, "attempt": received.Attempt, "bytes": received.Bytes, "sha256": received.SHA256})
		return nil
	}
}
