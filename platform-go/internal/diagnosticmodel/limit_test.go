package diagnosticmodel

import (
	"context"
	"database/sql"
	"errors"
	_ "github.com/mattn/go-sqlite3"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestDurableSharedBudget(t *testing.T) {
	path := filepath.Join(t.TempDir(), "limits.db") + "?_busy_timeout=5000&_txlock=immediate"
	db, e := sql.Open("sqlite3", path)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	other, e := sql.Open("sqlite3", path)
	if e != nil {
		t.Fatal(e)
	}
	defer other.Close()
	now := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
	ctx := context.Background()
	if e = Reserve(ctx, db, now, 10, 12); e != nil {
		t.Fatal(e)
	}
	var mu sync.Mutex
	good := 1
	limited := 0
	var wg sync.WaitGroup
	for i := 0; i < 29; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			d := db
			if i%2 == 0 {
				d = other
			}
			err := Reserve(ctx, d, now, 10, 12)
			mu.Lock()
			defer mu.Unlock()
			if err == nil {
				good++
			} else if errors.Is(err, ErrLimited) {
				limited++
			} else {
				t.Error(err)
			}
		}(i)
	}
	wg.Wait()
	if good != 10 || limited != 20 {
		t.Fatalf("accepted=%d limited=%d", good, limited)
	}
	for i := 0; i < 2; i++ {
		if e = Reserve(ctx, other, now.Add(61*time.Second), 10, 12); e != nil {
			t.Fatal(e)
		}
	}
	if !errors.Is(Reserve(ctx, db, now.Add(122*time.Second), 10, 12), ErrLimited) {
		t.Fatal("daily cap bypassed")
	}
	db.Close()
	db, e = sql.Open("sqlite3", path)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	if !errors.Is(Reserve(ctx, db, now.Add(180*time.Second), 10, 12), ErrLimited) {
		t.Fatal("restart erased budget")
	}
	if e = Reserve(ctx, db, now.Add(24*time.Hour), 10, 12); e != nil {
		t.Fatal(e)
	}
}
func TestInvalidProfileAndCancelledContext(t *testing.T) {
	for _, v := range []struct {
		m    string
		r, d int
	}{{"tencent/hy4-preview", 10, 50}, {Model, 21, 50}, {Model, 10, 1001}, {Model, 0, 50}} {
		if Validate(v.m, v.r, v.d) == nil {
			t.Fatal("invalid profile accepted")
		}
	}
	db, _ := sql.Open("sqlite3", ":memory:")
	defer db.Close()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if Reserve(ctx, db, time.Now(), 10, 50) == nil {
		t.Fatal("cancel accepted")
	}
}
