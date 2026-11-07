package modelproxy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func config(t *testing.T, upstream http.HandlerFunc) (Config, *[]UsageRecord, *sync.Mutex) {
	t.Helper()
	server := httptest.NewServer(upstream)
	t.Cleanup(server.Close)
	records := []UsageRecord{}
	mu := &sync.Mutex{}
	return Config{BaseURL: server.URL + "/v1/chat/completions", MasterKey: "provider-secret-not-run-token", AllowInsecureHTTP: true, MaxConcurrent: 1, MaxAttempts: 3, MaxRetryAfter: time.Millisecond, RequestTimeout: time.Second,
		Authorize: func(ctx context.Context, token, model string) (Grant, error) {
			if token != "run-token" || model != "hy4-test" {
				return Grant{}, &HTTPError{Status: 403}
			}
			return Grant{RunID: "run", Attempt: 1, Model: model, TokenHash: "hash"}, nil
		},
		RecordUsage: func(ctx context.Context, r UsageRecord) error {
			mu.Lock()
			defer mu.Unlock()
			records = append(records, r)
			return nil
		},
	}, &records, mu
}
func perform(proxy http.Handler, path, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest("POST", path, strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer run-token")
	req.Header.Set("X-Leak", "must-not-forward")
	out := httptest.NewRecorder()
	proxy.ServeHTTP(out, req)
	return out
}
func build(t *testing.T, cfg Config) *Proxy {
	t.Helper()
	p, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestNormalizeEndpointAndRejectCredentialsQuery(t *testing.T) {
	for _, base := range []string{"https://provider.test/v1", "https://provider.test/v1/chat/completions", "https://provider.test/v1/responses/"} {
		got, err := UpstreamURL(base, "responses")
		if err != nil || got != "https://provider.test/v1/responses" {
			t.Fatalf("%s %v", got, err)
		}
		got, err = UpstreamURL(base, "chat/completions")
		if err != nil || got != "https://provider.test/v1/chat/completions" {
			t.Fatal(got, err)
		}
	}
	for _, bad := range []string{"file:///etc/passwd", "https://secret@provider.test/v1", "https://provider.test/v1?key=secret", "https://provider.test/v1#secret"} {
		if _, err := UpstreamURL(bad, "responses"); err == nil {
			t.Fatal("unsafe base", bad)
		}
	}
}

func TestChatAndResponsesNonStreamUsageAndSecretBoundary(t *testing.T) {
	for _, endpoint := range []string{"chat/completions", "responses"} {
		t.Run(endpoint, func(t *testing.T) {
			cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v1/"+endpoint || r.Header.Get("Authorization") != "Bearer provider-secret-not-run-token" || r.Header.Get("X-Leak") != "" {
					t.Error("forwarding boundary", r.URL.Path)
				}
				w.Header().Set("Set-Cookie", "secret-cookie=provider-secret-not-run-token")
				w.Header().Set("Content-Type", "application/json")
				if endpoint == "responses" {
					fmt.Fprint(w, `{"id":"r","output":[],"usage":{"input_tokens":11,"output_tokens":3,"input_tokens_details":{"cached_tokens":2}}}`)
				} else {
					fmt.Fprint(w, `{"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":2}}}`)
				}
			})
			out := perform(build(t, cfg), "/v1/"+endpoint, `{"model":"hy4-test"}`)
			if out.Code != 200 || out.Header().Get("Set-Cookie") != "" || strings.Contains(out.Body.String(), cfg.MasterKey) {
				t.Fatal(out.Code, out.Body.String())
			}
			if len(*records) != 1 || *(*records)[0].Usage.InputTokens != 11 || *(*records)[0].Usage.CachedTokens != 2 {
				t.Fatal("usage missing", *records)
			}
		})
	}
}

func TestRequestNegativeContractsNeverReachProvider(t *testing.T) {
	var calls atomic.Int64
	cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1) })
	cfg.MaxRequestBytes = 128
	p := build(t, cfg)
	for _, test := range []struct {
		path, body string
		status     int
	}{
		{"/v1/models", `{}`, 404}, {"/v1/responses", `[1]`, 400}, {"/v1/responses", `{"model":"hy4-test","stream":"yes"}`, 400},
		{"/v1/responses", `{"model":"other"}`, 403}, {"/v1/responses", `{"model":"hy4-test","previous_response_id":"other-team"}`, 400},
		{"/v1/responses", `{"model":"hy4-test","conversation":"other-team"}`, 400}, {"/v1/responses", `{"model":"hy4-test","input":[{"type":"item_reference","id":"foreign"}]}`, 400},
		{"/v1/chat/completions", strings.Repeat("x", 129), 413},
	} {
		if out := perform(p, test.path, test.body); out.Code != test.status {
			t.Errorf("%s status=%d wanted=%d body=%s", test.body, out.Code, test.status, out.Body.String())
		}
	}
	if calls.Load() != 0 {
		t.Fatal("invalid requests reached provider")
	}
}

func TestRetriesBoundedAndRawProviderErrorsNeverLeak(t *testing.T) {
	var calls atomic.Int64
	cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Header().Set("Retry-After", "99999999")
		w.Header().Set("Location", "https://secret.example")
		w.WriteHeader(429)
		fmt.Fprint(w, "provider-secret-not-run-token hidden debug")
	})
	started := time.Now()
	out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test"}`)
	if out.Code != 429 || calls.Load() != 3 || len(*records) != 3 || time.Since(started) > time.Second || strings.Contains(out.Body.String(), "hidden debug") || out.Header().Get("Location") != "" {
		t.Fatalf("retry/error contract: %d %d %d", out.Code, calls.Load(), len(*records))
	}
	for _, r := range *records {
		if r.Usage.InputTokens != nil || r.Status != 429 {
			t.Fatal("invented usage")
		}
	}
}

func TestRedirectNotFollowed(t *testing.T) {
	var second atomic.Int64
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { second.Add(1) }))
	defer destination.Close()
	cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, destination.URL, 307) })
	out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test"}`)
	if out.Code != 502 || second.Load() != 0 {
		t.Fatal("redirect leaked credentials")
	}
}

func TestStreamProtocolsCaptureUsageAndTerminal(t *testing.T) {
	for _, endpoint := range []string{"chat/completions", "responses"} {
		t.Run(endpoint, func(t *testing.T) {
			cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				if endpoint == "chat/completions" {
					fmt.Fprint(w, "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":5,\"completion_tokens\":2}}\r\n\r\ndata: [DONE]\r\n\r\n")
				} else {
					fmt.Fprint(w, "event: response.completed\ndata:{\"type\":\"response.completed\",\"response\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":2}}}\n\n")
				}
			})
			out := perform(build(t, cfg), "/v1/"+endpoint, `{"model":"hy4-test","stream":true}`)
			if out.Code != 200 || !strings.Contains(out.Header().Get("Content-Type"), "text/event-stream") || len(*records) != 1 || *(*records)[0].Usage.InputTokens != 5 {
				t.Fatal("stream usage", out.Code, *records)
			}
		})
	}
}

func TestStreamMalformedOversizedAndPrematureEOF(t *testing.T) {
	for _, payload := range []string{"data: not-json\n\n", "data: " + strings.Repeat("x", 256) + "\n\n", "data: {\"choices\":[]}\n\n"} {
		cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, payload)
		})
		cfg.MaxEventBytes = 128
		out := perform(build(t, cfg), "/v1/chat/completions", `{"model":"hy4-test","stream":true}`)
		if len(*records) != 1 || (*records)[0].Status != 502 || !strings.Contains(out.Body.String(), "upstream_error") {
			t.Fatal("bad stream accepted", out.Body.String(), *records)
		}
	}
}

func TestWaitingRevokedTokenCannotCallProvider(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int64
	var revoked atomic.Bool
	waitAuthorized := make(chan struct{})
	var authCalls atomic.Int64
	cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			close(entered)
			<-release
		}
		fmt.Fprint(w, `{"usage":{}}`)
	})
	original := cfg.Authorize
	cfg.Authorize = func(ctx context.Context, token, model string) (Grant, error) {
		if authCalls.Add(1) == 3 {
			close(waitAuthorized)
		}
		if revoked.Load() {
			return Grant{}, &HTTPError{Status: 403}
		}
		return original(ctx, token, model)
	}
	p := build(t, cfg)
	done := make(chan struct{})
	go func() { perform(p, "/v1/responses", `{"model":"hy4-test"}`); close(done) }()
	<-entered
	waiting := make(chan *httptest.ResponseRecorder)
	go func() { waiting <- perform(p, "/v1/responses", `{"model":"hy4-test"}`) }()
	<-waitAuthorized
	revoked.Store(true)
	close(release)
	<-done
	if out := <-waiting; out.Code != 403 || calls.Load() != 1 {
		t.Fatal("revoked queued request reached provider", out.Code, calls.Load())
	}
}

func TestCancelledStreamReleasesSlotAndRecordsUnknownUsage(t *testing.T) {
	entered := make(chan struct{})
	fixtureDone := make(chan struct{})
	defer close(fixtureDone)
	var calls atomic.Int64
	cfg, records, mu := config(t, func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, ": started\n\n")
			w.(http.Flusher).Flush()
			close(entered)
			// Connection: close need not run a server-side background socket
			// reader. Bound fixture cleanup independently of server ctx delivery.
			select {
			case <-r.Context().Done():
			case <-fixtureDone:
			}
			return
		}
		fmt.Fprint(w, `{"usage":{"input_tokens":1,"output_tokens":1}}`)
	})
	p := build(t, cfg)
	ctx, cancel := context.WithCancel(context.Background())
	req := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"hy4-test","stream":true}`)).WithContext(ctx)
	req.Header.Set("Authorization", "Bearer run-token")
	done := make(chan struct{})
	go func() { p.ServeHTTP(httptest.NewRecorder(), req); close(done) }()
	<-entered
	cancel()
	<-done
	if out := perform(p, "/v1/responses", `{"model":"hy4-test"}`); out.Code != 200 {
		t.Fatal("slot leaked", out.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*records) != 2 || (*records)[0].Status != 499 || (*records)[0].Usage.InputTokens != nil {
		t.Fatal("cancel usage misclassified", *records)
	}
}

func TestLedgerFailureFailsClosedAndReleasesSlot(t *testing.T) {
	cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `{"usage":{"input_tokens":1}}`) })
	cfg.RecordUsage = func(context.Context, UsageRecord) error { return errors.New("secret database path") }
	p := build(t, cfg)
	for i := 0; i < 2; i++ {
		out := perform(p, "/v1/responses", `{"model":"hy4-test"}`)
		if out.Code != 503 || strings.Contains(out.Body.String(), "database path") {
			t.Fatal("ledger failure leaked or accepted", out.Code, out.Body.String())
		}
	}
}

func TestResponseLimitAndMissingUsage(t *testing.T) {
	cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"output":"`+strings.Repeat("x", 512)+`"}`)
	})
	cfg.MaxResponseBytes = 128
	if out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test"}`); out.Code != 502 || len(*records) != 1 || (*records)[0].Usage.InputTokens != nil {
		t.Fatal("oversized response")
	}
}

// Ensure downstream disconnects are represented independently of provider timeouts.
func TestHTTPErrorAndUsageJSONShapes(t *testing.T) {
	var _ error = &HTTPError{Status: 403, Message: "denied"}
	r := UsageRecord{Grant: Grant{RunID: "r", Attempt: 1, Model: "m", TokenHash: "h"}, Status: 200}
	raw, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	var value map[string]any
	if json.Unmarshal(raw, &value) != nil {
		t.Fatal("bad record")
	}
	_, _ = io.Discard.Write(raw)
}

func TestWaitingRequestDeadlineAndCancellationDoNotLeakSlots(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	var unblock sync.Once
	var calls atomic.Int64
	cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			close(entered)
			<-release
		}
		fmt.Fprint(w, `{"output":[]}`)
	})
	t.Cleanup(func() { unblock.Do(func() { close(release) }) })
	p := build(t, cfg)
	first := make(chan struct{})
	go func() { perform(p, "/v1/responses", `{"model":"hy4-test"}`); close(first) }()
	<-entered
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	req := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"hy4-test"}`)).WithContext(ctx)
	req.Header.Set("Authorization", "Bearer run-token")
	out := httptest.NewRecorder()
	p.ServeHTTP(out, req)
	if out.Code != 499 || calls.Load() != 1 {
		t.Fatal("waiting deadline not enforced", out.Code, calls.Load())
	}
	unblock.Do(func() { close(release) })
	<-first
	if out = perform(p, "/v1/responses", `{"model":"hy4-test"}`); out.Code != 200 {
		t.Fatal("slot leaked")
	}
}

func TestRetryWaitObeysTotalDeadline(t *testing.T) {
	var calls atomic.Int64
	cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Header().Set("Retry-After", "1")
		w.WriteHeader(429)
	})
	cfg.MaxRetryAfter = time.Second
	// Allow a real local TCP handshake under race instrumentation while
	// remaining well below the upstream one-second Retry-After.
	cfg.RequestTimeout = 250 * time.Millisecond
	out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test"}`)
	if out.Code != 504 || calls.Load() != 1 || len(*records) != 2 {
		t.Fatal("retry wait escaped total deadline", out.Code, calls.Load())
	}
	if (*records)[0].Status != 429 || (*records)[0].Attempt != 1 || (*records)[1].Status != 504 ||
		(*records)[1].Attempt != 0 || (*records)[1].ErrorClass != "admission_deadline_before_upstream" {
		t.Fatal("retry/deadline accounting lost actual provider outcome")
	}
}

func TestMissingUsageRemainsUnknownAndIncompleteResponseHasUsage(t *testing.T) {
	cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `{"output":[]}`) })
	if out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test"}`); out.Code != 200 || (*records)[0].Usage.InputTokens != nil {
		t.Fatal("missing usage became zero")
	}
	cfg, records, _ = config(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"type\":\"response.incomplete\",\"response\":{\"usage\":{\"input_tokens\":3,\"output_tokens\":4}}}\n\n")
	})
	out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test","stream":true}`)
	if out.Code != 200 || len(*records) != 1 || (*records)[0].Status != 200 || *(*records)[0].Usage.OutputTokens != 4 {
		t.Fatal("provider max-token completion misclassified")
	}
}

func TestLedgerFailureSuppressesSuccessfulStreamTerminal(t *testing.T) {
	cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: [DONE]\n\n")
	})
	cfg.RecordUsage = func(context.Context, UsageRecord) error { return errors.New("private ledger error") }
	out := perform(build(t, cfg), "/v1/chat/completions", `{"model":"hy4-test","stream":true}`)
	if strings.Contains(out.Body.String(), "[DONE]") || !strings.Contains(out.Body.String(), "upstream_error") || strings.Contains(out.Body.String(), "private ledger") {
		t.Fatal("stream pretended usage was persisted")
	}
}

func TestHTTP200ErrorPayloadIsRedacted(t *testing.T) {
	cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"error":{"message":"provider-secret-not-run-token hidden debug"}}`)
	})
	out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test"}`)
	if out.Code != 502 || strings.Contains(out.Body.String(), cfg.MasterKey) || len(*records) != 1 || (*records)[0].ErrorClass != "provider_body_error" {
		t.Fatal("200 error leaked", out.Code, out.Body.String())
	}
}

func TestProviderHostedToolsRequireExplicitPolicy(t *testing.T) {
	var calls atomic.Int64
	cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); fmt.Fprint(w, `{"output":[]}`) })
	p := build(t, cfg)
	for _, tool := range []string{"web_search", "mcp", "file_search", "computer_use_preview"} {
		body := fmt.Sprintf(`{"model":"hy4-test","tools":[{"type":%q}]}`, tool)
		if out := perform(p, "/v1/responses", body); out.Code != 400 {
			t.Fatal("provider network tool allowed", tool, out.Code)
		}
	}
	if calls.Load() != 0 {
		t.Fatal("unapproved tools reached provider")
	}
	if out := perform(p, "/v1/responses", `{"model":"hy4-test","tools":[{"type":"function","name":"local_tool"}]}`); out.Code != 200 {
		t.Fatal("local tool disabled")
	}
}

func TestGatewayRequestIdentityLinksRetriesAndIgnoresCaller(t *testing.T) {
	var calls atomic.Int64
	cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-QIQC-Request-ID") != "" {
			t.Error("caller identity leaked upstream")
		}
		if calls.Add(1) == 1 {
			w.WriteHeader(503)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"status":"completed","usage":{}}`)
	})
	p := build(t, cfg)
	req := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"hy4-test"}`))
	req.Header.Set("Authorization", "Bearer run-token")
	req.Header.Set("X-QIQC-Request-ID", strings.Repeat("f", 32))
	req.Header.Set("X-QIQC-Controller-Correlation-ID", strings.Repeat("a", 32))
	out := httptest.NewRecorder()
	p.ServeHTTP(out, req)
	identity := out.Header().Get("X-QIQC-Request-ID")
	if out.Code != 200 || len(identity) != 32 || identity == strings.Repeat("f", 32) || len(*records) != 2 {
		t.Fatal("missing trusted request identity")
	}
	for i, r := range *records {
		if r.Grant.RequestID != identity || r.Grant.RequestCorrelation != strings.Repeat("a", 32) || r.Attempt != i+1 {
			t.Fatal("retry identity lost")
		}
	}
	next := perform(p, "/v1/responses", `{"model":"hy4-test"}`)
	if next.Header().Get("X-QIQC-Request-ID") == identity {
		t.Fatal("identity reused")
	}
}

func TestControllerCancellationRetainsActualProviderAttemptBeforeHeaders(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprint(stream), func(t *testing.T) {
			entered := make(chan struct{})
			release := make(chan struct{})
			defer close(release)
			cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
				if stream {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprint(w, "data: {\"type\":\"response.created\"}\n\n")
					w.(http.Flusher).Flush()
				}
				close(entered)
				select {
				case <-r.Context().Done():
				case <-release:
				}
			})
			recorded := make(chan UsageRecord, 1)
			cfg.RecordUsage = func(ctx context.Context, r UsageRecord) error { recorded <- r; return nil }
			server := httptest.NewServer(build(t, cfg))
			defer server.Close()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			req, _ := http.NewRequestWithContext(ctx, "POST", server.URL+"/v1/responses", strings.NewReader(fmt.Sprintf(`{"model":"hy4-test","stream":%v}`, stream)))
			req.Header.Set("Authorization", "Bearer run-token")
			req.Header.Set("X-QIQC-Controller-Correlation-ID", strings.Repeat("c", 32))
			finished := make(chan struct{})
			go func() {
				defer close(finished)
				response, _ := http.DefaultClient.Do(req)
				if response != nil {
					io.Copy(io.Discard, response.Body)
					response.Body.Close()
				}
			}()
			<-entered
			cancel()
			<-finished
			select {
			case record := <-recorded:
				if record.Status != 499 || record.ErrorClass != "client_cancelled" || record.Grant.RequestCorrelation != strings.Repeat("c", 32) || len(record.Grant.RequestID) != 32 || record.Attempt != 1 {
					t.Fatalf("cancel ledger missing: %+v", record)
				}
			case <-time.After(time.Second):
				t.Fatal("cancelled provider attempt disappeared")
			}
		})
	}
}
