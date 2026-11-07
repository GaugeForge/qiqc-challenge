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

func TestSharedPacingAndCancellation(t *testing.T) {
	db, _ := sql.Open("sqlite3", filepath.Join(t.TempDir(), "p.db")+"?_busy_timeout=5000&_txlock=immediate")
	defer db.Close()
	ctx := context.Background()
	window := 120 * time.Millisecond
	var wg sync.WaitGroup
	begin := time.Now()
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if e := wait(ctx, db, "same-account", 2, 50, time.Now, window); e != nil {
				t.Error(e)
			}
		}()
	}
	wg.Wait()
	if time.Since(begin) < window {
		t.Fatal("burst bypassed pacing")
	}
	cancelled, cancel := context.WithTimeout(ctx, 10*time.Millisecond)
	defer cancel()
	if e := wait(cancelled, db, "same-account", 2, 50, time.Now, time.Minute); !errors.Is(e, context.DeadlineExceeded) {
		t.Fatal("wait not cancellation aware", e)
	}
	var n int
	db.QueryRow("SELECT COUNT(*) FROM provider_pacing_admissions").Scan(&n)
	if n != 3 {
		t.Fatal("cancelled wait consumed provider slot")
	}
}
func TestSharedKeyGroupDailyAndIndependentGroup(t *testing.T) {
	db, _ := sql.Open("sqlite3", ":memory:")
	db.SetMaxOpenConns(1)
	defer db.Close()
	now := time.Now()
	ctx := context.Background()
	if d, e := pacedReserve(ctx, db, "account", 20, 1, now, time.Minute); e != nil || d != 0 {
		t.Fatal(e)
	}
	if _, e := pacedReserve(ctx, db, "account", 20, 1, now.Add(time.Minute), time.Minute); !errors.Is(e, ErrLimited) {
		t.Fatal("second key would bypass account daily cap")
	}
	if _, e := pacedReserve(ctx, db, "different-account", 1000, 100000, now, time.Minute); e != nil {
		t.Fatal(e)
	}
}
