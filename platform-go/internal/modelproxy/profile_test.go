package modelproxy

import (
	"context"
	"errors"
	"net/http"
	"testing"
)

func TestProfileRejectsUnsupportedEndpointAndModel(t *testing.T) {
	calls := 0
	cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) { calls++ })
	cfg.AllowedEndpoints = []string{"chat/completions"}
	cfg.RequiredModel = "test-model"
	p := build(t, cfg)
	if perform(p, "/v1/responses", `{"model":"test-model"}`).Code != 400 {
		t.Fatal("unsupported endpoint accepted")
	}
	if perform(p, "/v1/chat/completions", `{"model":"paid-fallback"}`).Code != 403 {
		t.Fatal("unapproved model accepted")
	}
	if calls != 0 {
		t.Fatal("request reached provider")
	}
}
func TestCancelledAdmissionRecordsNoProviderAttempt(t *testing.T) {
	calls := 0
	cfg, rows, _ := config(t, func(w http.ResponseWriter, r *http.Request) { calls++ })
	cfg.Reserve = func(context.Context, Grant) error { return context.Canceled }
	p := build(t, cfg)
	if perform(p, "/v1/responses", `{"model":"hy4-test"}`).Code != 499 {
		t.Fatal("wrong cancellation status")
	}
	if calls != 0 || len(*rows) != 1 {
		t.Fatal("unexpected I/O or rows")
	}
	r := (*rows)[0]
	if r.Attempt != 0 || r.ErrorClass != "client_cancelled_before_upstream" || r.Usage.InputTokens == nil || *r.Usage.InputTokens != 0 || r.Usage.OutputTokens == nil || *r.Usage.OutputTokens != 0 {
		t.Fatal("false upstream attempt/unknown usage")
	}
	cfg.Reserve = func(context.Context, Grant) error { return errors.New("safe rejection") }
	if perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test"}`).Code != 503 {
		t.Fatal("other rejection forged as cancellation")
	}
}
