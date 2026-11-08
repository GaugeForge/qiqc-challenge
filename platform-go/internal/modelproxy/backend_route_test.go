package modelproxy

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func TestProviderBackendsRouteByStableTeamGroup(t *testing.T) {
	var mu sync.Mutex
	seen := map[string]string{}
	server := func(id string) *httptest.Server {
		return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			mu.Lock()
			seen[id] = r.Header.Get("Authorization")
			mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}`)
		}))
	}
	a, b := server("a"), server("b")
	defer a.Close()
	defer b.Close()
	cfg := Config{
		Backends: []Backend{
			{ID: "a", BaseURL: a.URL + "/v1/chat/completions", MasterKey: "provider-key-a"},
			{ID: "b", BaseURL: b.URL + "/v1/chat/completions", MasterKey: "provider-key-b"},
		},
		AllowInsecureHTTP: true, MaxConcurrent: 1, MaxAttempts: 1, MaxRetryAfter: time.Millisecond, RequestTimeout: time.Second,
		Authorize: func(ctx context.Context, token, model string) (Grant, error) {
			if token != "run-token" || model != "hy4-test" {
				return Grant{}, &HTTPError{Status: 403}
			}
			return Grant{RunID: "run", Attempt: 1, Model: model, TokenHash: "hash", RouteGroup: "b"}, nil
		},
		RecordUsage: func(context.Context, UsageRecord) error { return nil },
	}
	out := perform(build(t, cfg), "/v1/chat/completions", `{"model":"hy4-test"}`)
	if out.Code != 200 {
		t.Fatalf("status %d", out.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if seen["b"] != "Bearer provider-key-b" || seen["a"] != "" {
		t.Fatalf("team route crossed backend: %#v", seen)
	}
}
