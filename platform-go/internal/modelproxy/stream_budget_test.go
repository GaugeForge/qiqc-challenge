package modelproxy

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"syscall"
	"testing"
)

const budgetUsageEvent = "data: {\"usage\":{\"input_tokens\":7,\"output_tokens\":3}}\n\n"
const budgetTerminalEvent = "data: {\"type\":\"response.completed\",\"response\":{}}\n\n"

func TestStreamTotalBudgetAcrossEventsAndWireEncoding(t *testing.T) {
	for _, tc := range []struct {
		name, payload string
		limit         int64
	}{
		{"many_small_events", budgetUsageEvent + strings.Repeat(": heartbeat\n\n", 30) + budgetTerminalEvent, 128},
		{"one_large_event", budgetUsageEvent + "data: {\"delta\":\"" + strings.Repeat("x", 512) + "\"}\n\n" + budgetTerminalEvent, 128},
		{"utf8_bytes_not_characters", budgetUsageEvent + "data: {\"delta\":\"" + strings.Repeat("中", 20) + "\"}\n\n" + budgetTerminalEvent, 128},
		{"crlf_bytes", strings.ReplaceAll(budgetUsageEvent+budgetTerminalEvent, "\n", "\r\n"), int64(len(budgetUsageEvent + budgetTerminalEvent))},
		{"terminal_final_delimiter_over_limit", budgetUsageEvent + budgetTerminalEvent, int64(len(budgetUsageEvent+budgetTerminalEvent) - 1)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, tc.payload)
			})
			cfg.MaxResponseBytes = tc.limit
			out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test","stream":true}`)
			if len(*records) != 1 {
				t.Fatalf("usage records=%d", len(*records))
			}
			r := (*records)[0]
			if r.Status != 502 || r.ErrorClass != "stream_response_too_large" || r.Usage.InputTokens == nil || *r.Usage.InputTokens != 7 || r.Usage.OutputTokens == nil || *r.Usage.OutputTokens != 3 {
				t.Fatalf("limit/previous usage lost: %+v", r)
			}
			if strings.Contains(out.Body.String(), "response.completed") || !strings.Contains(out.Body.String(), "upstream_error") {
				t.Fatal("over-budget stream released terminal or omitted error")
			}
		})
	}
}

// A complete terminal is sufficient: never wait for an infinite suffix, or
// relabel successful completion because the peer resets after the terminal.
type terminalThenErrorReader struct {
	payload string
	err     error
	reads   int
	closed  bool
}

func (r *terminalThenErrorReader) Read(b []byte) (int, error) {
	r.reads++
	if len(r.payload) > 0 {
		n := copy(b, r.payload)
		r.payload = r.payload[n:]
		return n, nil
	}
	return 0, r.err
}
func (r *terminalThenErrorReader) Close() error { r.closed = true; return nil }

func TestStreamTotalBudgetStopsAtExactTerminalWithoutReadingTail(t *testing.T) {
	for _, suffix := range []string{"", strings.Repeat("unbounded-tail", 1000)} {
		cfg, records, _ := config(t, func(http.ResponseWriter, *http.Request) {})
		cfg.MaxResponseBytes = int64(len(budgetTerminalEvent))
		p := build(t, cfg)
		body := &terminalThenErrorReader{payload: budgetTerminalEvent + suffix, err: syscall.ECONNRESET}
		response := &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: body}
		r := httptest.NewRequest("POST", "/v1/responses", nil)
		out := httptest.NewRecorder()
		p.forwardStream(out, r, r.Context(), response, Grant{RunID: "fixture"}, 1)
		if len(*records) != 1 || (*records)[0].Status != 200 || (*records)[0].ErrorClass != "" || body.reads != 1 || !body.closed || !strings.Contains(out.Body.String(), "response.completed") {
			t.Fatalf("terminal not accepted independently of tail: %+v reads=%d", *records, body.reads)
		}
	}
}

func TestStreamEventLimitKeepsDistinctClassification(t *testing.T) {
	for _, event := range []string{"data: " + strings.Repeat("x", 256) + "\n\n", strings.Repeat(": short line\n", 32) + "\n"} {
		cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, budgetUsageEvent+event)
		})
		cfg.MaxEventBytes = 128
		cfg.MaxResponseBytes = 4096
		perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test","stream":true}`)
		if len(*records) != 1 || (*records)[0].Status != 502 || (*records)[0].ErrorClass != "stream_event_too_large" || (*records)[0].Usage.InputTokens == nil || *(*records)[0].Usage.InputTokens != 7 {
			t.Fatalf("event limit/usage classification: %+v", *records)
		}
	}
}

type cancelBeforeRead struct{ cancel context.CancelFunc }

func (r cancelBeforeRead) Read([]byte) (int, error) { r.cancel(); return 0, context.Canceled }
func (cancelBeforeRead) Close() error               { return nil }

func TestStreamTotalBudgetDoesNotMaskCancellationOrTruncatedTerminal(t *testing.T) {
	for _, cancelled := range []bool{false, true} {
		cfg, records, _ := config(t, func(http.ResponseWriter, *http.Request) {})
		cfg.MaxResponseBytes = 4096
		p := build(t, cfg)
		ctx, cancel := context.WithCancel(context.Background())
		var reader io.ReadCloser = &terminalThenErrorReader{payload: strings.TrimSuffix(budgetTerminalEvent, "\n\n"), err: syscall.ECONNRESET}
		if cancelled {
			reader = cancelBeforeRead{cancel}
		}
		response := &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: reader}
		r := httptest.NewRequest("POST", "/v1/responses", nil).WithContext(ctx)
		out := httptest.NewRecorder()
		p.forwardStream(out, r, ctx, response, Grant{RunID: "fixture"}, 1)
		cancel()
		wantStatus, wantClass := 502, "incomplete_stream"
		if cancelled {
			wantStatus, wantClass = 499, "client_cancelled"
		}
		if len(*records) != 1 || (*records)[0].Status != wantStatus || (*records)[0].ErrorClass != wantClass || strings.Contains(out.Body.String(), "response.completed") {
			t.Fatalf("cancellation/truncated terminal misclassified: %+v", *records)
		}
	}
}
