package main

import (
	"io"
	"log"
	"net/http"

	"golang.org/x/net/http2"
	"golang.org/x/net/http2/h2c"
)

func server(addr string) error {
	return http.ListenAndServe(addr, h2c.NewHandler(http.HandlerFunc(echo), &http2.Server{}))
}

func echo(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	id := r.Header.Get("x-audit-id")
	log.Printf("SERVER %s RECEIVED %q (%d bytes)", id, body, len(body))
	w.Header().Set("x-audit-id", id)
	if _, err := w.Write(body); err != nil {
		log.Printf("SERVER %s write error: %v", id, err)
	}
}
