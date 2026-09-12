package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net"
	"os"
	"sync"
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
	conn net.Conn
	f    *http2.Framer
	mu   sync.Mutex
}

func newWire(c net.Conn) *wire {
	f := http2.NewFramer(c, c)
	f.ReadMetaHeaders = hpack.NewDecoder(4096, nil)
	return &wire{conn: c, f: f}
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

func (w *wire) readFrame() (http2.Frame, error) {
	for {
		frame, err := w.f.ReadFrame()
		if err != nil {
			return nil, err
		}
		switch f := frame.(type) {
		case *http2.SettingsFrame:
			if !f.IsAck() {
				err = w.ack()
			}
		case *http2.PingFrame:
			if !f.Flags.Has(http2.FlagPingAck) {
				w.mu.Lock()
				err = w.f.WritePing(true, f.Data)
				w.mu.Unlock()
			}
		default:
			return frame, nil
		}
		if err != nil {
			return nil, err
		}
	}
}
