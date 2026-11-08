package platform

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// RunWorker owns local fixture execution. Real executors are injected explicitly;
// neither an archive entrypoint nor a user-provided plugin is loaded on this host.
func (s *Store) RunWorker(ctx context.Context, slots int, realExecutor func(context.Context, Lease) error) error {
	if slots < 1 {
		return fmt.Errorf("invalid slots")
	}
	pool := strings.TrimSpace(os.Getenv("QIQC_WORKER_POOL"))
	if pool != "" && pool != "key1" && pool != "key2" {
		return fmt.Errorf("invalid QIQC_WORKER_POOL")
	}
	// Persist the operator's initial limit once so API and dispatch agree.
	// Subsequent workers read the same setting; raising local parallelism
	// requires a controlled restart, while lowering the global cap is immediate.
	globalSlots := slots
	if pool != "" {
		// Pool-specific workers have independent E2B ceilings (20 + 100). The
		// persisted global ceiling must therefore cover both pools; each worker's
		// own `slots` value remains its local concurrency cap.
		globalSlots = 120
	}
	if err := s.write(func(tx *sql.Tx) error {
		if _, e := tx.Exec("INSERT OR IGNORE INTO settings(key,value) VALUES('global_task_slots',?)", strconv.Itoa(globalSlots)); e != nil {
			return e
		}
		// Separate ceilings are used by Claim for the two E2B credentials. A
		// deployment may lower them transactionally without restarting workers.
		if _, e := tx.Exec("INSERT OR IGNORE INTO settings(key,value) VALUES('e2b_key2_slots','20'),('e2b_key1_slots','100')"); e != nil {
			return e
		}
		var raw string
		if e := tx.QueryRow("SELECT value FROM settings WHERE key='global_task_slots'").Scan(&raw); e != nil {
			return e
		}
		n, e := strconv.Atoi(raw)
		if e != nil || n < 1 || n > 10000 {
			return fmt.Errorf("invalid persisted task slots")
		}
		slots = n
		return nil
	}); err != nil {
		return err
	}
	var wg sync.WaitGroup
	defer wg.Wait()
	ticker := time.NewTicker(200 * time.Millisecond)
	defer ticker.Stop()
	owner := "go-worker-" + id()
	inflight := make(chan struct{}, slots)
	lastMaintenance := time.Time{}
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
		if time.Since(lastMaintenance) > 5*time.Second {
			_, _ = s.ResumeUploads(ctx)
			_, _ = s.ResumeScientificFailureUploads(ctx)
			// A controller can finish and upload its private failure archive
			// before the bridge process returns. Close that proven terminal
			// failure so sibling runs are not left running until worker shutdown.
			_, _ = s.FinalizeScientificFailureArtifacts()
			if _, err := s.Recover(); err != nil {
				// A short database lock (for example while a read-heavy staff
				// snapshot is in flight) must not terminate the only worker.
				slog.Error("worker recovery deferred", "error", err)
			}
			lastMaintenance = time.Now()
		}
		for len(inflight) < slots {
			lease, err := s.ClaimPool(owner, slots, pool)
			if err != nil {
				slog.Error("worker claim deferred", "error", err)
				time.Sleep(500 * time.Millisecond)
				break
			}
			if lease == nil {
				break
			}
			inflight <- struct{}{}
			wg.Add(1)
			go func(l Lease) {
				defer wg.Done()
				defer func() { <-inflight }()
				done := make(chan struct{})
				defer close(done)
				go func() {
					tick := time.NewTicker(10 * time.Second)
					defer tick.Stop()
					for {
						select {
						case <-done:
							return
						case <-tick.C:
							_ = s.Heartbeat(l)
						}
					}
				}()
				var err error
				if err = s.CheckVersion(l.SHA256); err == nil {
					if l.Mode == "simulated" {
						hash := sha256.Sum256([]byte(l.SHA256 + strconv.Itoa(l.Ordinal)))
						seed, _ := strconv.ParseUint(hex.EncodeToString(hash[:])[:8], 16, 64)
						reward := 0
						if seed%10 < 8 {
							reward = 1
						}
						seconds := fmt.Sprintf("%.6f", 10+float64(seed%20000000)/1e6)
						err = s.CompleteFixture(context.Background(), l, reward, seconds, "[Simulation / submitted code was not executed]\nFixture task; no scientific score.\n")
					} else if realExecutor != nil {
						err = realExecutor(ctx, l)
					} else {
						err = fmt.Errorf("real executor unavailable")
					}
				}
				if err != nil {
					// Upload-only errors keep their durable bundle; never turn them into a rerun.
					if _, e := os.Stat(filepath.Join(s.Root, "cache", l.RunID, strconv.Itoa(l.Attempt), "pending.json")); os.IsNotExist(e) {
						category, retryable := "platform", false
						failure, _ := AsExecutionFailure(err)
						if failure != nil {
							category = failure.Category
							retryable = failure.Retryable && category == "platform"
						}
						diagnostic := RunFailure{Category: category}
						if failure != nil {
							diagnostic.Phase, diagnostic.Code = failure.Phase, failure.Code
						}
						slog.Warn("worker execution failed", "run", l.RunID, "attempt", l.Attempt, "category", category, "error", err)
						// A transient SQLite sidecar/lock error must not orphan a
						// running row until its long scientific lease expires.
						var failErr error
						for attempt := 0; attempt < 3; attempt++ {
							if failErr = s.Fail(l, category); failErr == nil {
								if diagErr := s.RecordRunDiagnostic(l.RunID, l.Attempt, diagnostic); diagErr != nil {
									slog.Error("run diagnostic deferred", "run", l.RunID, "attempt", l.Attempt, "error", diagErr)
								}
								if retryable {
									_ = s.RetryPlatformFailure(l, "Infrastructure failure; one bounded automatic retry scheduled")
								}
								break
							}
							time.Sleep(time.Duration(attempt+1) * 250 * time.Millisecond)
						}
						if failErr != nil {
							// The failure-artifact finalizer may have closed the same
							// attempt while the executor was returning. Upgrade its
							// generic marker instead of leaving a stale-lease error.
							var state string
							var current int
							if rowErr := s.DB.QueryRow("SELECT state,attempt FROM runs WHERE id=?", l.RunID).Scan(&state, &current); rowErr == nil && state == "attention" && current == l.Attempt {
								_, _ = s.UpgradeRunDiagnostic(l.RunID, l.Attempt, diagnostic)
								failErr = nil
							}
						}
						if failErr != nil {
							slog.Error("worker fail transition deferred", "run", l.RunID, "attempt", l.Attempt, "error", failErr)
						}
					}
				}
			}(*lease)
		}
	}
}
