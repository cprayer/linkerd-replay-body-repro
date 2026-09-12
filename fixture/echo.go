package main

import (
	"strconv"

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
