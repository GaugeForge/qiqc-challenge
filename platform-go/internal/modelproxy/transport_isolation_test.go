package modelproxy

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type connectionKey struct{}

// The same intervention closes exactly ONE real TLS connection in both
// experiments. No mocked responses, no retry, no lowered 49-request admission.
func TestSingleConnectionResetBlastRadiusAtFortyNine(t *testing.T) {
	for _, isolated := range []bool{false, true} {
		t.Run(fmt.Sprintf("http1_isolated_%t", isolated), func(t *testing.T) {
			entered := make(chan net.Conn, 49)
			release := make(chan struct{})
			var once sync.Once
			upstream := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				want := 2
				if isolated {
					want = 1
				}
				if r.ProtoMajor != want {
					t.Errorf("protocol=%d want=%d", r.ProtoMajor, want)
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "data: {\"type\":\"response.created\"}\n\n")
				w.(http.Flusher).Flush()
				entered <- r.Context().Value(connectionKey{}).(net.Conn)
				select {
				case <-release:
					fmt.Fprint(w, "data: {\"type\":\"response.completed\",\"response\":{}}\n\n")
				case <-r.Context().Done():
					return
				}
			}))
			upstream.Config.ConnContext = func(ctx context.Context, c net.Conn) context.Context {
				return context.WithValue(ctx, connectionKey{}, c)
			}
			upstream.EnableHTTP2 = true
			upstream.StartTLS()
			defer upstream.Close()
			defer once.Do(func() { close(release) })
			client := upstream.Client()
			base := client.Transport.(*http.Transport)
			if isolated {
				client = nil // Exercise the actual production-default transport.
			} else {
				base.MaxConnsPerHost = 1
			}
			var mu sync.Mutex
			records := []UsageRecord{}
			cfg := Config{BaseURL: upstream.URL, MasterKey: "synthetic-fixture", Client: client, MaxConcurrent: 49, RequestTimeout: 20 * time.Second,
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
			if isolated {
				transport := p.client.Transport.(*http.Transport)
				if transport.ForceAttemptHTTP2 || transport.TLSNextProto == nil || !transport.DisableKeepAlives || transport.MaxConnsPerHost != 49 {
					t.Fatal("production default is not isolated HTTP1")
				}
				// Trust only the local test CA; preserve production ALPN and
				// all isolation settings instead of substituting a custom client.
				transport.TLSClientConfig.RootCAs = base.TLSClientConfig.RootCAs
			}
			done := make(chan struct{}, 49)
			for i := 0; i < 49; i++ {
				go func(i int) {
					r := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"fixture","stream":true}`))
					r.Header.Set("Authorization", fmt.Sprintf("Bearer fixture-%d", i))
					p.ServeHTTP(httptest.NewRecorder(), r)
					done <- struct{}{}
				}(i)
			}
			connections := map[net.Conn]bool{}
			for i := 0; i < 49; i++ {
				select {
				case c := <-entered:
					connections[c] = true
				case <-time.After(12 * time.Second):
					t.Fatal("49 concurrent streams not ready")
				}
			}
			wantConns := 1
			if isolated {
				wantConns = 49
			}
			if len(connections) != wantConns {
				t.Fatalf("connections=%d want=%d", len(connections), wantConns)
			}
			for c := range connections {
				_ = c.Close()
				break
			}
			failedBeforeRelease := 49
			if isolated {
				failedBeforeRelease = 1
			}
			for i := 0; i < failedBeforeRelease; i++ {
				select {
				case <-done:
				case <-time.After(5 * time.Second):
					t.Fatal("reset stream not observed")
				}
			}
			if isolated {
				select {
				case <-done:
					t.Fatal("another stream affected by one connection reset")
				case <-time.After(100 * time.Millisecond):
				}
			}
			once.Do(func() { close(release) })
			for i := failedBeforeRelease; i < 49; i++ {
				select {
				case <-done:
				case <-time.After(5 * time.Second):
					t.Fatal("unrelated stream did not finish")
				}
			}
			mu.Lock()
			defer mu.Unlock()
			counts := map[int]int{}
			ids := map[string]bool{}
			for _, r := range records {
				counts[r.Status]++
				ids[r.Grant.RequestID] = true
			}
			if len(records) != 49 || len(ids) != 49 {
				t.Fatal("usage count or identity mismatch")
			}
			if counts[502] != failedBeforeRelease || counts[200] != 49-failedBeforeRelease || counts[499] != 0 {
				t.Fatalf("unexpected classification: %v", counts)
			}
			t.Logf("active_requests=49 actual_connections=%d closed_connections=1 failed=%d completed=%d", len(connections), counts[502], counts[200])
		})
	}
}

func TestDefaultTransportDoesNotReuseConnectionsAcrossRequests(t *testing.T) {
	var mu sync.Mutex
	addresses := map[string]bool{}
	cfg, records, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		addresses[r.RemoteAddr] = true
		mu.Unlock()
		if r.ProtoMajor != 1 || !r.Close {
			t.Error("request does not use dedicated HTTP1 connection")
		}
		fmt.Fprint(w, `{"status":"completed"}`)
	})
	cfg.MaxConcurrent = 49
	p := build(t, cfg)
	for i := 0; i < 3; i++ {
		result := perform(p, "/v1/responses", `{"model":"hy4-test"}`)
		if result.Code != 200 {
			t.Fatal("successful request failed")
		}
	}
	if len(addresses) != 3 || len(*records) != 3 {
		t.Fatalf("connections=%d records=%d", len(addresses), len(*records))
	}
	transport := p.client.Transport.(*http.Transport)
	if transport.Proxy != nil || transport.TLSClientConfig.InsecureSkipVerify {
		t.Fatal("upstream trust boundary changed")
	}
}
