package main

import (
	"flag"
	"fmt"
	"log"
)

func main() {
	mode := flag.String("mode", "server", "server or client")
	addr := flag.String("addr", "127.0.0.1:8080", "listen address or client target")
	id := flag.String("id", "example", "request ID")
	flag.Parse()
	log.SetFlags(0)
	var err error
	switch *mode {
	case "server":
		err = server(*addr)
	case "client":
		err = client(*addr, *id)
	default:
		err = fmt.Errorf("invalid mode %q", *mode)
	}
	if err != nil {
		log.Fatal(err)
	}
}
