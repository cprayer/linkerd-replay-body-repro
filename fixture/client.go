package main

import (
	"bytes"
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
	fields := []hpack.HeaderField{
		{Name: ":method", Value: "POST"},
		{Name: ":scheme", Value: "http"},
		{Name: ":authority", Value: o.authority},
		{Name: ":path", Value: path},
		{Name: "x-audit-id", Value: o.id},
		{Name: "content-type", Value: content},
		{Name: "te", Value: "trailers"},
	}
	if err := w.headers(stream, fields, warmup); err != nil {
		return err
	}
	event("client_headers", map[string]any{"id": o.id, "stream": stream, "warmup": warmup, "delay_ms": o.delay})
	if !warmup && o.delay == 0 {
		if err := sendBody(w, stream, o); err != nil {
			return err
		}
	} else if !warmup {
		cancel := make(chan struct{})
		defer close(cancel)
		go func() {
			timer := time.NewTimer(time.Duration(o.delay) * time.Millisecond)
			defer timer.Stop()
			select {
			case <-cancel:
				return
			case <-timer.C:
				if err := sendBody(w, stream, o); err != nil {
					w.conn.Close()
				}
			}
		}()
	}
	var body []byte
	status, grpcStatus := "", ""
	responseID, attempt := "", 0
	for {
		frame, err := w.readFrame()
		if err != nil {
			return err
		}
		ended := false
		switch f := frame.(type) {
		case *http2.MetaHeadersFrame:
			if f.StreamID != stream {
				continue
			}
			for _, h := range f.Fields {
				switch h.Name {
				case ":status":
					status = h.Value
				case "grpc-status":
					grpcStatus = h.Value
				case "x-audit-id":
					responseID = h.Value
				case "x-audit-attempt":
					attempt, err = strconv.Atoi(h.Value)
					if err != nil {
						return err
					}
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
		event("client_response", map[string]any{"id": o.id, "status": status, "grpc_status": grpcStatus,
			"response_id": responseID, "attempt": attempt, "body": string(body), "bytes": len(body), "sha256": digest(body)})
		if status != "200" || (grpcStatus != "" && grpcStatus != "0") {
			return fmt.Errorf("status=%s grpc-status=%s", status, grpcStatus)
		}
		output.Lock()
		fmt.Printf("ECHO %s SENT %q (%d bytes) RECEIVED %q (%d bytes) HTTP %s\n", o.id, o.payload, len(o.payload), body, len(body), status)
		output.Unlock()
		if responseID != o.id || !bytes.Equal(body, []byte(o.payload)) {
			return fmt.Errorf("payload mismatch: %s", body)
		}
		event("client_success", map[string]any{"id": o.id, "attempt": attempt, "bytes": len(body), "sha256": digest(body)})
		return nil
	}
}

func sendBody(w *wire, stream uint32, o options) error {
	err := w.data(stream, []byte(o.payload), true)
	m := map[string]any{"id": o.id, "stream": stream, "bytes": len(o.payload), "sha256": digest([]byte(o.payload))}
	if err != nil {
		m["error"] = err.Error()
	}
	event("client_data", m)
	return err
}

func runClients(o options) int {
	if o.id == "" {
		o.id = fmt.Sprintf("audit-%d", time.Now().UnixNano())
	}
	if len(o.payload) > 16384 || o.delay < 0 || o.concurrency < 1 {
		fmt.Fprintln(os.Stderr, "payload must be <=16384 bytes, delay >=0, concurrency >=1")
		return 2
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
		return 1
	}
	return 0
}
