package platform

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/gaugeforge/qiqc-platform-go/internal/modelproxy"
)

func TestModelAdmissionConcurrentOutstandingRequestsSurviveRestart(t *testing.T) {
	s := testStore(t)
	queueBatch(t, s, 1, "scientific-smoke")
	l := mustClaim(t, s, "worker", 4)
	token, err := s.MintModelToken(*l, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	grant, err := s.AuthorizeModel(context.Background(), token, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	// Two gateway processes share SQLite; none of these requests has recorded
	// usage yet. The old completed-usage count admitted all 160 callers.
	other, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	var admitted, rejected, unexpected atomic.Int32
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < 160; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			store := s
			if i%2 == 0 {
				store = other
			}
			err := store.ReserveModelRequest(context.Background(), grant)
			if err == nil {
				admitted.Add(1)
				return
			}
			var http *modelproxy.HTTPError
			if errors.As(err, &http) && http.Status == 429 {
				rejected.Add(1)
			} else {
				unexpected.Add(1)
			}
		}(i)
	}
	close(start)
	wg.Wait()
	if admitted.Load() != 100 || rejected.Load() != 60 || unexpected.Load() != 0 {
		t.Fatalf("admissions=%d rejected=%d unexpected=%d", admitted.Load(), rejected.Load(), unexpected.Load())
	}
	// Token rotation cannot reset a run-attempt budget, including after restart.
	token, err = other.MintModelToken(*l, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	grant, err = other.AuthorizeModel(context.Background(), token, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	var http *modelproxy.HTTPError
	if err = other.ReserveModelRequest(context.Background(), grant); !errors.As(err, &http) || http.Status != 429 {
		t.Fatal("budget reset", err)
	}
	var usage int
	s.DB.QueryRow("SELECT COUNT(*) FROM usage").Scan(&usage)
	if usage != 0 {
		t.Fatal("reservation fabricated provider consumption")
	}
}

func TestModelAdmissionRejectsGrantAfterCancellationAndSeedsPastUsage(t *testing.T) {
	s := testStore(t)
	queueBatch(t, s, 1, "scientific-smoke")
	l := mustClaim(t, s, "worker", 4)
	token, err := s.MintModelToken(*l, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	g, err := s.AuthorizeModel(context.Background(), token, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 99; i++ {
		if err = s.RecordModelUsage(context.Background(), modelproxy.UsageRecord{Grant: g, Status: 200}); err != nil {
			t.Fatal(err)
		}
	}
	if err = s.ReserveModelRequest(context.Background(), g); err != nil {
		t.Fatal(err)
	}
	if err = s.ReserveModelRequest(context.Background(), g); err == nil {
		t.Fatal("historical usage ignored")
	}
	if _, err = s.DB.Exec("UPDATE batches SET status='cancelling' WHERE id=?", l.BatchID); err != nil {
		t.Fatal(err)
	}
	var http *modelproxy.HTTPError
	if err = s.ReserveModelRequest(context.Background(), g); !errors.As(err, &http) || http.Status != 403 {
		t.Fatal("cancelled capability admitted", err)
	}
}

func TestModelAdmissionFrom99AllowsOnlyOneConcurrentAttempt(t *testing.T) {
	s := testStore(t)
	queueBatch(t, s, 1, "scientific-smoke")
	l := mustClaim(t, s, "worker", 4)
	token, err := s.MintModelToken(*l, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	g, err := s.AuthorizeModel(context.Background(), token, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 99; i++ {
		if err = s.ReserveModelRequest(context.Background(), g); err != nil {
			t.Fatal(err)
		}
	}
	var allowed atomic.Int32
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < 24; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			if s.ReserveModelRequest(context.Background(), g) == nil {
				allowed.Add(1)
			}
		}()
	}
	close(start)
	wg.Wait()
	if allowed.Load() != 1 {
		t.Fatal("concurrent cap bypass", allowed.Load())
	}
	if _, err = s.DB.Exec("UPDATE run_tokens SET expires=0"); err != nil {
		t.Fatal(err)
	}
	var http *modelproxy.HTTPError
	if err = s.ReserveModelRequest(context.Background(), g); !errors.As(err, &http) || http.Status != 403 {
		t.Fatal("expired grant admitted", err)
	}
}

func TestLocalPacingRejectionsDoNotConsumeProviderAdmissions(t *testing.T) {
	s := testStore(t)
	queueBatch(t, s, 1, "scientific-smoke")
	l := mustClaim(t, s, "worker", 4)
	token, e := s.MintModelToken(*l, "tencent/hy4-preview")
	if e != nil {
		t.Fatal(e)
	}
	g, e := s.AuthorizeModel(context.Background(), token, "tencent/hy4-preview")
	if e != nil {
		t.Fatal(e)
	}
	zero := int64(0)
	for i := 0; i < 100; i++ {
		if e = s.RecordModelUsage(context.Background(), modelproxy.UsageRecord{Grant: g, Status: 499, Attempt: 0, Usage: modelproxy.Usage{InputTokens: &zero, OutputTokens: &zero}, ErrorClass: "client_cancelled_before_upstream"}); e != nil {
			t.Fatal(e)
		}
	}
	if _, e = s.AuthorizeModel(context.Background(), token, "tencent/hy4-preview"); e != nil {
		t.Fatal("local waits exhausted upstream allowance", e)
	}
	if e = s.ReserveModelRequest(context.Background(), g); e != nil {
		t.Fatal(e)
	}
	var used int
	if e = s.DB.QueryRow("SELECT used FROM model_admissions WHERE run_id=? AND attempt=?", g.RunID, g.Attempt).Scan(&used); e != nil || used != 1 {
		t.Fatal("local waits seeded false provider usage", used, e)
	}
	s.DB.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", g.RunID)
	if e = s.ReserveModelRequest(context.Background(), g); e == nil {
		t.Fatal("revoked capability admitted after wait")
	}
}
