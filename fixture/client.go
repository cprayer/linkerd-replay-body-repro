package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"golang.org/x/net/http2"
)

func client(addr, id string) error {
	transport := &http2.Transport{
		AllowHTTP: true,
		DialTLSContext: func(ctx context.Context, network, addr string, _ *tls.Config) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, addr)
		},
	}
	defer transport.CloseIdleConnections()
	httpClient := &http.Client{Transport: transport, Timeout: 15 * time.Second}
	// A streaming body lets the server read all bytes even if the proxy duplicates them
	req, err := http.NewRequest(http.MethodPost, "http://"+addr+"/", io.NopCloser(strings.NewReader("ping")))
	if err != nil {
		return err
	}
	req.Header.Set("x-audit-id", id)
	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	fmt.Printf("ECHO %s SENT %q (%d bytes) RECEIVED %q (%d bytes) HTTP %d\n", id, "ping", len("ping"), body, len(body), resp.StatusCode)
	if resp.ProtoMajor != 2 || resp.StatusCode != http.StatusOK || resp.Header.Get("x-audit-id") != id || string(body) != "ping" {
		return fmt.Errorf("unexpected echo: protocol=%s status=%d body=%q", resp.Proto, resp.StatusCode, body)
	}
	return nil
}
