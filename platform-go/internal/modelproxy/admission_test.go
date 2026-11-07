package modelproxy

import (
	"context"
	"net/http"
	"sync/atomic"
	"testing"
)

func TestProviderAttemptReservationCountsRetriesWithoutUsageFabrication(t *testing.T) {
	var calls, admissions atomic.Int32
	cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(503) })
	cfg.Reserve = func(context.Context, Grant) error {
		if admissions.Add(1) > 2 {
			return &HTTPError{Status: 429}
		}
		return nil
	}
	out := perform(build(t, cfg), "/v1/chat/completions", `{"model":"hy4-test"}`)
	if out.Code != 429 || calls.Load() != 2 || admissions.Load() != 3 || len(*records) != 3 {
		t.Fatalf("status=%d provider=%d admissions=%d records=%d", out.Code, calls.Load(), admissions.Load(), len(*records))
	}
	for _, r := range (*records)[:2] {
		if r.Status != 503 || r.Usage.InputTokens != nil {
			t.Fatal("reservation created fake consumption", r)
		}
	}
	r := (*records)[2]
	if r.Attempt != 0 || r.Status != 429 || r.ErrorClass != "admission_rejected_before_upstream" || r.Usage.InputTokens == nil || *r.Usage.InputTokens != 0 {
		t.Fatal("rejection audit fabricated upstream consumption")
	}
}

func TestReservationRejectionRecordsOnlyLocalAudit(t *testing.T) {
	var calls atomic.Int32
	cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1) })
	cfg.Reserve = func(context.Context, Grant) error { return &HTTPError{Status: 403} }
	out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test","input":"test"}`)
	if out.Code != 403 || calls.Load() != 0 || len(*records) != 1 {
		t.Fatal("revoked request reached upstream", out.Code, calls.Load(), len(*records))
	}
	r := (*records)[0]
	if r.Attempt != 0 || r.ErrorClass != "admission_rejected_before_upstream" || r.Usage.InputTokens == nil || *r.Usage.InputTokens != 0 {
		t.Fatal("local audit is not explicit")
	}
}
