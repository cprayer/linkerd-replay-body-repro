package main

import (
	"bytes"
	"net"
	"testing"

	"golang.org/x/net/http2"
)

func TestEchoReturnsReceivedBytesUnchanged(t *testing.T) {
	for _, body := range [][]byte{[]byte("ping"), []byte("pingping"), {0, 255, 1}, {}} {
		t.Run(string(body), func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			done := make(chan error, 1)
			go func() {
				done <- echo(newWire(server), 3, &request{id: "example", attempt: 2, data: body})
			}()
			reader := newWire(client)
			frame, err := reader.f.ReadFrame()
			if err != nil {
				t.Fatal(err)
			}
			headers, ok := frame.(*http2.MetaHeadersFrame)
			if !ok || headers.StreamID != 3 {
				t.Fatalf("unexpected response headers: %v", frame)
			}
			frame, err = reader.f.ReadFrame()
			if err != nil {
				t.Fatal(err)
			}
			data, ok := frame.(*http2.DataFrame)
			if !ok || data.StreamID != 3 || !data.StreamEnded() || !bytes.Equal(data.Data(), body) {
				t.Fatalf("response differs from received body %q: %v", body, frame)
			}
			if err := <-done; err != nil {
				t.Fatal(err)
			}
		})
	}
}
