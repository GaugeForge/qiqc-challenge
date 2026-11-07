package diagnosticmodel

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// Independent review: two service connections sharing an account cannot each
// obtain a slot at the same instant; closing all handles does not reset the cap.
func TestIndependentTwoConnectionsPacingSurvivesReopen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "shared.db") + "?_busy_timeout=5000&_txlock=immediate"
	first, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	second, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	defer second.Close()
	now := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
	var wg sync.WaitGroup
	var mu sync.Mutex
	accepted := 0
	waiting := 0
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			handle := first
			if i%2 == 1 {
				handle = second
			}
			delay, e := pacedReserve(context.Background(), handle, "same-provider-account", 2, 3, now, time.Minute)
			mu.Lock()
			defer mu.Unlock()
			if e != nil {
				t.Error(e)
			} else if delay == 0 {
				accepted++
			} else {
				waiting++
			}
		}(i)
	}
	wg.Wait()
	if accepted != 1 || waiting != 19 {
		t.Fatalf("independent services bypass: accepted=%d waiting=%d", accepted, waiting)
	}
	for i := 1; i <= 2; i++ {
		if d, e := pacedReserve(context.Background(), second, "same-provider-account", 2, 3, now.Add(time.Duration(i)*time.Minute), time.Minute); e != nil || d != 0 {
			t.Fatal(d, e)
		}
	}
	first.Close()
	second.Close()
	reopened, e := sql.Open("sqlite3", path)
	if e != nil {
		t.Fatal(e)
	}
	defer reopened.Close()
	if _, e = pacedReserve(context.Background(), reopened, "same-provider-account", 2, 3, now.Add(3*time.Minute), time.Minute); !errors.Is(e, ErrLimited) {
		t.Fatal("reopen erased shared daily budget", e)
	}
	var n int
	if e = reopened.QueryRow("SELECT COUNT(*) FROM provider_pacing_admissions WHERE quota_group=?", "same-provider-account").Scan(&n); e != nil || n != 3 {
		t.Fatal(n, e)
	}
}
