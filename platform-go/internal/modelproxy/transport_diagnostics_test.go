package modelproxy

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func TestTransportDiagnosticLogRedactsPayloadAndCapability(t *testing.T) {
	var buffer bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buffer, nil)))
	defer slog.SetDefault(previous)
	transportDiagnostic("request", &url.Error{Op: "Post", URL: "https://private-host.invalid/prompt-secret", Err: errors.New("private-response")}, context.Background(), nil, Grant{RequestID: "request-fixture", RunID: "run-fixture", TokenHash: "capability-secret"})
	output := buffer.String()
	for _, secret := range []string{"private-host", "prompt-secret", "private-response", "capability-secret"} {
		if strings.Contains(output, secret) {
			t.Fatal("sensitive data in diagnostic")
		}
	}
	if !strings.Contains(output, "request-fixture") || !strings.Contains(output, "other_transport_error") {
		t.Fatal("safe correlation missing")
	}
	if diagnosticTokenCount(nil) != "unknown" {
		t.Fatal("unknown usage became zero")
	}
}

func TestTransportDiagnosticClassificationDoesNotExposeErrorText(t *testing.T) {
	for _, tc := range []struct {
		err  error
		want string
	}{
		{nil, "eof_before_terminal"}, {io.ErrUnexpectedEOF, "unexpected_eof"},
		{syscall.ECONNRESET, "connection_reset"}, {bufio.ErrTooLong, "scanner_line_limit"},
		{&url.Error{Op: "Post", URL: "https://secret.invalid/private", Err: context.DeadlineExceeded}, "context_deadline"},
		{errors.New("private response secret"), "other_transport_error"},
	} {
		if got := transportClass(tc.err); got != tc.want {
			t.Fatalf("class=%s want=%s", got, tc.want)
		}
	}
}

// A successful provider response still needs time for durable accounting. This
// exercises real HTTP socket deadlines, not just arithmetic on configuration.
func TestHTTPWriteDeadlineIncludesLateAccounting(t *testing.T) {
	for _, extra := range []time.Duration{100 * time.Millisecond, 600 * time.Millisecond} {
		t.Run(extra.String(), func(t *testing.T) {
			cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
				time.Sleep(200 * time.Millisecond)
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "data: {\"type\":\"response.completed\",\"response\":{}}\n\n")
			})
			cfg.RequestTimeout = 300 * time.Millisecond
			cfg.RecordUsage = func(ctx context.Context, record UsageRecord) error {
				time.Sleep(400 * time.Millisecond)
				return ctx.Err()
			}
			server := httptest.NewUnstartedServer(build(t, cfg))
			server.Config.WriteTimeout = cfg.RequestTimeout + extra
			server.Start()
			defer server.Close()
			req, _ := http.NewRequest("POST", server.URL+"/v1/responses", strings.NewReader(`{"model":"hy4-test","stream":true}`))
			req.Header.Set("Authorization", "Bearer run-token")
			response, err := server.Client().Do(req)
			var body []byte
			if response != nil {
				body, _ = io.ReadAll(response.Body)
				response.Body.Close()
			}
			completed := err == nil && strings.Contains(string(body), "response.completed")
			if extra < 400*time.Millisecond && completed {
				t.Fatal("short write deadline unexpectedly retained terminal")
			}
			if extra > 400*time.Millisecond && !completed {
				t.Fatal("adequate write deadline lost terminal")
			}
		})
	}
}

// One shared HTTP/2 connection: cancelling one run must leave the other 48
// streams intact. A real connection close must fail all affected streams;
// it must never be converted into a planned cancellation.
func TestSharedHTTP2FortyNineStreams(t *testing.T) {
	for _, dropConnection := range []bool{false, true} {
		t.Run(fmt.Sprintf("drop_connection_%t", dropConnection), func(t *testing.T) {
			entered := make(chan struct{}, 49)
			release := make(chan struct{})
			var once sync.Once
			upstream := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.ProtoMajor != 2 {
					t.Error("HTTP2 not negotiated")
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "data: {\"type\":\"response.created\"}\n\n")
				w.(http.Flusher).Flush()
				entered <- struct{}{}
				select {
				case <-release:
					fmt.Fprint(w, "data: {\"type\":\"response.completed\",\"response\":{}}\n\n")
				case <-r.Context().Done():
					return
				}
			}))
			upstream.EnableHTTP2 = true
			upstream.StartTLS()
			defer upstream.Close()
			defer once.Do(func() { close(release) })
			client := upstream.Client()
			client.Transport.(*http.Transport).MaxConnsPerHost = 1
			var mu sync.Mutex
			records := []UsageRecord{}
			cfg := Config{BaseURL: upstream.URL + "/v1", MasterKey: "local-fixture", Client: client, MaxConcurrent: 49, RequestTimeout: 15 * time.Second,
				Authorize: func(_ context.Context, token, model string) (Grant, error) {
					return Grant{RunID: token, Attempt: 1, Model: model, TokenHash: "fixture"}, nil
				},
				RecordUsage: func(_ context.Context, r UsageRecord) error {
					mu.Lock()
					defer mu.Unlock()
					records = append(records, r)
					return nil
				}}
			p := build(t, cfg)
			done := make(chan struct{}, 49)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			for i := 0; i < 49; i++ {
				go func(i int) {
					c := context.Background()
					if i == 0 {
						c = ctx
					}
					r := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"fixture","stream":true}`)).WithContext(c)
					r.Header.Set("Authorization", fmt.Sprintf("Bearer run-%d", i))
					p.ServeHTTP(httptest.NewRecorder(), r)
					done <- struct{}{}
				}(i)
			}
			for i := 0; i < 49; i++ {
				select {
				case <-entered:
				case <-time.After(10 * time.Second):
					t.Fatal("49 streams did not enter")
				}
			}
			if dropConnection {
				upstream.CloseClientConnections()
			} else {
				cancel()
				select {
				case <-done:
				case <-time.After(5 * time.Second):
					t.Fatal("cancelled stream did not exit")
				}
				once.Do(func() { close(release) })
			}
			remaining := 49
			if !dropConnection {
				remaining = 48
			}
			for i := 0; i < remaining; i++ {
				select {
				case <-done:
				case <-time.After(10 * time.Second):
					t.Fatal("stream did not finish")
				}
			}
			mu.Lock()
			defer mu.Unlock()
			counts := map[int]int{}
			for _, r := range records {
				counts[r.Status]++
			}
			if len(records) != 49 {
				t.Fatalf("accounting records=%d", len(records))
			}
			if dropConnection {
				if counts[502] != 49 {
					t.Fatalf("connection closure misclassified: %v", counts)
				}
			} else {
				if counts[200] != 48 || counts[499] != 1 {
					t.Fatalf("cancellation crossed requests: %v", counts)
				}
			}
		})
	}
}
