package main

import (
	"flag"
	"fmt"
	"os"
)

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
	os.Exit(runClients(o))
}
