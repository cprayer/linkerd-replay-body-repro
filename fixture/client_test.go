package main

import (
	"fmt"
	"net"
	"testing"
	"time"

	"golang.org/x/net/http2"
)

func TestClientSendsOneBodyAndChecksEcho(t *testing.T) {
	for _, delay := range []int{0, 5} {
		t.Run(fmt.Sprintf("delay-%d", delay), func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			client.SetDeadline(time.Now().Add(2 * time.Second))
			server.SetDeadline(time.Now().Add(2 * time.Second))
			done := make(chan error, 1)
			go func() {
				done <- func() error {
					w := newWire(server)
					frame, err := w.readFrame()
					if err != nil {
						return err
					}
					headers, ok := frame.(*http2.MetaHeadersFrame)
					if !ok || headers.StreamEnded() {
						return fmt.Errorf("expected request headers: %v", frame)
					}
					frame, err = w.readFrame()
					if err != nil {
						return err
					}
					data, ok := frame.(*http2.DataFrame)
					if !ok || !data.StreamEnded() || data.StreamID != headers.StreamID || string(data.Data()) != "ping" {
						return fmt.Errorf("expected one complete ping body: %v", frame)
					}
					if err := echo(w, data.StreamID, &request{id: "example", attempt: 1, data: data.Data()}); err != nil {
						return err
					}
					for i := 0; i < 2; i++ {
						frame, err = w.readFrame()
						if err != nil {
							return err
						}
						if _, ok := frame.(*http2.WindowUpdateFrame); !ok {
							return fmt.Errorf("unexpected extra request frame: %v", frame)
						}
					}
					return nil
				}()
			}()
			if err := exchange(newWire(client), 1, options{id: "example", payload: "ping", delay: delay}, false); err != nil {
				t.Fatal(err)
			}
			if err := <-done; err != nil {
				t.Fatal(err)
			}
		})
	}
}
