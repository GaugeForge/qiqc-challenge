package modelproxy

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// This is a local HTTP concurrency and accounting test, not evidence that the
// external model provider accepts 49 simultaneous scientific requests.
func TestFortyNineConcurrentRunsAndCancelledOverflow(t *testing.T) {
	entered := make(chan struct{}, 50)
	release := make(chan struct{})
	var unblock sync.Once
	var calls, reservations atomic.Int32
	cfg, records, mu := config(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		entered <- struct{}{}
		select {
		case <-release:
		case <-r.Context().Done():
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"status":"completed","usage":{"input_tokens":1,"output_tokens":1}}`)
	})
	// Release handlers before httptest.Server.Close, including failure paths.
	t.Cleanup(func() { unblock.Do(func() { close(release) }) })
	cfg.MaxConcurrent = 49
	cfg.RequestTimeout = 10 * time.Second
	cfg.Authorize = func(_ context.Context, token, model string) (Grant, error) {
		if !strings.HasPrefix(token, "run-") || model != "hy4-test" {
			return Grant{}, &HTTPError{Status: 403}
		}
		return Grant{RunID: token, Attempt: 1, Model: model, TokenHash: "hash-" + token}, nil
	}
	cfg.Reserve = func(context.Context, Grant) error { reservations.Add(1); return nil }
	proxy := build(t, cfg)
	request := func(ctx context.Context, token string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"hy4-test","input":"local concurrency fixture"}`)).WithContext(ctx)
		r.Header.Set("Authorization", "Bearer "+token)
		out := httptest.NewRecorder()
		proxy.ServeHTTP(out, r)
		return out
	}
	results := make(chan *httptest.ResponseRecorder, 49)
	for i := 0; i < 49; i++ {
		go func(i int) { results <- request(context.Background(), fmt.Sprintf("run-%02d", i)) }(i)
	}
	deadline := time.NewTimer(8 * time.Second)
	defer deadline.Stop()
	for i := 0; i < 49; i++ {
		select {
		case <-entered:
		case <-deadline.C:
			t.Fatalf("only %d of 49 upstream requests entered concurrently", i)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if out := request(ctx, "run-overflow"); out.Code != 499 {
		t.Fatalf("cancelled queued request: status=%d", out.Code)
	}
	if calls.Load() != 49 || reservations.Load() != 49 {
		t.Fatalf("overflow consumed provider work: calls=%d reservations=%d", calls.Load(), reservations.Load())
	}
	unblock.Do(func() { close(release) })
	for i := 0; i < 49; i++ {
		if out := <-results; out.Code != http.StatusOK {
			t.Fatalf("accepted concurrent request failed: %d", out.Code)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*records) != 50 {
		t.Fatalf("usage rows=%d, want 49 provider outcomes and one queued cancellation", len(*records))
	}
	runs, requests := map[string]bool{}, map[string]bool{}
	for _, row := range *records {
		if row.Grant.RunID == "run-overflow" {
			if row.Status != 499 || row.Attempt != 0 || row.ErrorClass != "client_cancelled_before_upstream" ||
				row.Usage.InputTokens == nil || *row.Usage.InputTokens != 0 ||
				row.Usage.OutputTokens == nil || *row.Usage.OutputTokens != 0 ||
				row.Usage.CachedTokens == nil || *row.Usage.CachedTokens != 0 {
				t.Fatal("cancelled overflow invented provider work")
			}
			continue
		}
		if row.Status != 200 || row.Attempt != 1 || runs[row.Grant.RunID] || requests[row.Grant.RequestID] || row.Grant.RequestID == "" || row.Grant.RunID == "run-overflow" {
			t.Fatal("incorrect per-run usage attribution or duplicate accounting")
		}
		runs[row.Grant.RunID], requests[row.Grant.RequestID] = true, true
	}
	if len(runs) != 49 {
		t.Fatal("missing real provider outcomes")
	}
}
