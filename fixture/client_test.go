package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"golang.org/x/net/http2"
	"golang.org/x/net/http2/h2c"
)

func TestClientChecksActualHTTP2Echo(t *testing.T) {
	for _, response := range []string{"ping", "pingping", "wrong"} {
		t.Run(response, func(t *testing.T) {
			var calls atomic.Int64
			handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.ProtoMajor != 2 || r.ContentLength != -1 {
					t.Errorf("expected streaming HTTP/2 request: %s, length %d", r.Proto, r.ContentLength)
				}
				if response == "ping" {
					echo(w, r)
					return
				}
				body, err := io.ReadAll(r.Body)
				if err != nil || string(body) != "ping" {
					t.Errorf("unexpected request: %q, %v", body, err)
				}
				w.Header().Set("x-audit-id", r.Header.Get("x-audit-id"))
				io.WriteString(w, response)
			})
			server := httptest.NewServer(h2c.NewHandler(handler, &http2.Server{}))
			defer server.Close()
			err := client(strings.TrimPrefix(server.URL, "http://"), "example")
			if (err == nil) != (response == "ping") {
				t.Fatalf("response %q: unexpected result %v", response, err)
			}
			if calls.Load() != 1 {
				t.Fatalf("expected one request, got %d", calls.Load())
			}
		})
	}
}
