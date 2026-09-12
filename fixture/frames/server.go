package main

import (
	"fmt"
	"io"
	"net"
	"strconv"
	"sync"
	"sync/atomic"

	"golang.org/x/net/http2"
	"golang.org/x/net/http2/hpack"
)

func echo(w *wire, stream uint32, r *request) error {
	if err := w.headers(stream, []hpack.HeaderField{
		{Name: ":status", Value: "200"},
		{Name: "content-type", Value: "application/octet-stream"},
		{Name: "content-length", Value: strconv.Itoa(len(r.data))},
		{Name: "x-audit-id", Value: r.id},
		{Name: "x-audit-attempt", Value: strconv.Itoa(r.attempt)},
	}, false); err != nil {
		return err
	}
	return w.data(stream, r.data, true)
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
		if err := echo(w, stream, r); err != nil {
			return err
		}
		event("success", fields(stream, r))
		return nil
	}
	for {
		frame, err := w.readFrame()
		if err != nil {
			event("connection_end", map[string]any{"conn": conn, "error": err.Error()})
			return
		}
		switch f := frame.(type) {
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
