package modelproxy

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestSlotAndRetryWaitTerminalAccounting(t *testing.T) {
	for _, stage := range []string{"slot", "retry"} {
		for _, cancelled := range []bool{false, true} {
			name := stage + "/deadline"
			if cancelled {
				name = stage + "/cancelled"
			}
			t.Run(name, func(t *testing.T) {
				var calls atomic.Int64
				cfg, rows, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					w.Header().Set("Retry-After", "5")
					w.WriteHeader(503)
				})
				cfg.RequestTimeout = 250 * time.Millisecond
				cfg.MaxRetryAfter = time.Second
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				record := cfg.RecordUsage
				cfg.RecordUsage = func(ctx context.Context, row UsageRecord) error {
					if ctx.Err() != nil {
						t.Error("accounting lost independent context")
					}
					err := record(ctx, row)
					if cancelled && stage == "retry" && row.Attempt == 1 {
						cancel()
					}
					return err
				}
				p := build(t, cfg)
				if stage == "slot" {
					p.slots <- struct{}{}
					defer func() { <-p.slots }()
					if cancelled {
						cancel()
					}
				}
				req := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"hy4-test"}`)).WithContext(ctx)
				req.Header.Set("Authorization", "Bearer run-token")
				req.Header.Set("X-QIQC-Controller-Correlation-ID", strings.Repeat("a", 32))
				out := httptest.NewRecorder()
				p.ServeHTTP(out, req)
				wantStatus, wantClass := 504, "admission_deadline_before_upstream"
				if cancelled {
					wantStatus, wantClass = 499, "client_cancelled_before_upstream"
				}
				wantRows, wantCalls := 1, int64(0)
				if stage == "retry" {
					wantRows, wantCalls = 2, 1
				}
				if out.Code != wantStatus || len(*rows) != wantRows || calls.Load() != wantCalls {
					t.Fatalf("status=%d rows=%d calls=%d", out.Code, len(*rows), calls.Load())
				}
				last := (*rows)[len(*rows)-1]
				if last.Status != wantStatus || last.ErrorClass != wantClass || last.Attempt != 0 ||
					last.Usage.InputTokens == nil || *last.Usage.InputTokens != 0 ||
					last.Usage.OutputTokens == nil || *last.Usage.OutputTokens != 0 ||
					last.Usage.CachedTokens == nil || *last.Usage.CachedTokens != 0 ||
					last.Grant.RequestCorrelation != strings.Repeat("a", 32) {
					t.Fatalf("incorrect terminal accounting: %+v", last)
				}
				if stage == "retry" {
					first := (*rows)[0]
					if first.Status != 503 || first.ErrorClass != "provider_http_error" || first.Attempt != 1 ||
						first.Usage.InputTokens != nil || first.Grant.RequestID != last.Grant.RequestID {
						t.Fatal("previous actual attempt erased or rewritten")
					}
				}
			})
		}
	}
}

func TestWaitCancellationAccountingFailureFailsClosed(t *testing.T) {
	cfg, _, _ := config(t, func(http.ResponseWriter, *http.Request) { t.Error("provider called") })
	cfg.RecordUsage = func(context.Context, UsageRecord) error { return errors.New("storage unavailable") }
	p := build(t, cfg)
	p.slots <- struct{}{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"hy4-test"}`)).WithContext(ctx)
	req.Header.Set("Authorization", "Bearer run-token")
	out := httptest.NewRecorder()
	p.ServeHTTP(out, req)
	if out.Code != 503 || !strings.Contains(out.Body.String(), "usage_record_error") {
		t.Fatal("unpersisted cancellation presented as accounted")
	}
}

func TestCancellationDuringNextAuthorizationKeepsTerminalAccounting(t *testing.T) {
	cfg, rows, _ := config(t, func(http.ResponseWriter, *http.Request) { t.Error("provider called") })
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	authorize, calls := cfg.Authorize, 0
	cfg.Authorize = func(ctx context.Context, token, model string) (Grant, error) {
		calls++
		if calls == 2 {
			cancel()
			return Grant{}, context.Canceled
		}
		return authorize(ctx, token, model)
	}
	req := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"hy4-test"}`)).WithContext(ctx)
	req.Header.Set("Authorization", "Bearer run-token")
	out := httptest.NewRecorder()
	build(t, cfg).ServeHTTP(out, req)
	if out.Code != 499 || len(*rows) != 1 || (*rows)[0].Attempt != 0 {
		t.Fatal("authorization cancellation race lost accounting")
	}
}
