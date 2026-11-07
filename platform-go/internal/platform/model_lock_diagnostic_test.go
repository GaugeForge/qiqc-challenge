package platform

import (
	"context"
	"fmt"
	"github.com/gaugeforge/qiqc-platform-go/internal/modelproxy"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// Real SQLite writer contention reproduces the old two-second deadline gap;
// the new bounded window must retain exactly one outcome after lock release.
func TestModelUsageWriterLockAccountingWindow(t *testing.T) {
	for _, oldWindow := range []bool{true, false} {
		s := testStore(t)
		queueBatch(t, s, 1, "scientific-smoke")
		l := mustClaim(t, s, "worker", 4)
		g := modelproxy.Grant{RunID: l.RunID, Attempt: l.Attempt, Model: "fixture", RequestID: "0123456789abcdef0123456789abcdef"}
		tx, err := s.DB.Begin()
		if err != nil {
			t.Fatal(err)
		}
		released := make(chan struct{})
		go func() { time.Sleep(2300 * time.Millisecond); _ = tx.Rollback(); close(released) }()
		window := 35 * time.Second
		if oldWindow {
			window = 2 * time.Second
		}
		ctx, cancel := context.WithTimeout(context.Background(), window)
		err = s.RecordModelUsage(ctx, modelproxy.UsageRecord{Grant: g, Attempt: 1, Status: 502, ErrorClass: "incomplete_stream"})
		cancel()
		<-released
		var count int
		if e := s.DB.QueryRow("SELECT COUNT(*) FROM usage WHERE request_id=?", g.RequestID).Scan(&count); e != nil {
			t.Fatal(e)
		}
		if oldWindow {
			if err == nil || count != 0 {
				t.Fatalf("old gap not reproduced: err=%v rows=%d", err, count)
			}
		} else {
			if err != nil || count != 1 {
				t.Fatalf("recoverable writer lock lost outcome: err=%v rows=%d", err, count)
			}
		}
	}
}

func TestFortyNineStreamOutcomesSurviveSQLiteWriterContention(t *testing.T) {
	s := testStore(t)
	queueBatch(t, s, 1, "scientific-smoke")
	l := mustClaim(t, s, "worker", 4)
	entered := make(chan struct{}, 49)
	release := make(chan struct{})
	var once sync.Once
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		entered <- struct{}{}
		<-release
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"type\":\"response.completed\",\"response\":{}}\n\n")
	}))
	defer upstream.Close()
	defer once.Do(func() { close(release) })
	proxy, err := modelproxy.New(modelproxy.Config{BaseURL: upstream.URL, MasterKey: "local-fixture", AllowInsecureHTTP: true,
		MaxConcurrent: 49, RequestTimeout: 10 * time.Second,
		Authorize: func(_ context.Context, _, model string) (modelproxy.Grant, error) {
			return modelproxy.Grant{RunID: l.RunID, Attempt: l.Attempt, Model: model, TokenHash: "fixture"}, nil
		}, RecordUsage: s.RecordModelUsage})
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan bool, 49)
	for i := 0; i < 49; i++ {
		go func() {
			r := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(`{"model":"fixture","stream":true}`))
			r.Header.Set("Authorization", "Bearer fixture")
			w := httptest.NewRecorder()
			proxy.ServeHTTP(w, r)
			done <- strings.Contains(w.Body.String(), "response.completed") && !strings.Contains(w.Body.String(), "upstream_error")
		}()
	}
	for i := 0; i < 49; i++ {
		select {
		case <-entered:
		case <-time.After(5 * time.Second):
			t.Fatal("streams not ready")
		}
	}
	tx, err := s.DB.Begin()
	if err != nil {
		t.Fatal(err)
	}
	go func() { time.Sleep(2300 * time.Millisecond); _ = tx.Rollback() }()
	once.Do(func() { close(release) })
	for i := 0; i < 49; i++ {
		select {
		case success := <-done:
			if !success {
				t.Error("terminal lost")
			}
		case <-time.After(40 * time.Second):
			t.Fatal("accounting hung")
		}
	}
	var count, identities int
	if err := s.DB.QueryRow("SELECT COUNT(*),COUNT(DISTINCT request_id) FROM usage WHERE status=200").Scan(&count, &identities); err != nil || count != 49 || identities != 49 {
		t.Fatalf("outcomes=%d identities=%d error=%v", count, identities, err)
	}
}
