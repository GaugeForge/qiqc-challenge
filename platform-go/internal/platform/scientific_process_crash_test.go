package platform

// These tests kill real independent RunWorker processes. The scientific executor,
// cleanup receipts and object provider are explicit local substitutes: no Harbor,
// task, verifier, E2B, model, R2, deployment or account credential is contacted.
import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

type processCrashObjects struct {
	*rollout.Store
	journal  string
	afterPut func()
	fail     bool
}

func appendCrashEvent(path string, event map[string]any) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	raw, err := json.Marshal(event)
	if err != nil {
		return err
	}
	if _, err = f.Write(append(raw, '\n')); err != nil {
		return err
	}
	return f.Sync()
}
func (o *processCrashObjects) PutFile(ctx context.Context, key, file string) (rollout.Info, error) {
	if o.fail {
		if err := appendCrashEvent(o.journal, map[string]any{"event": "put_failed"}); err != nil {
			return rollout.Info{}, err
		}
		return rollout.Info{}, errors.New("explicit local object outage")
	}
	_, existing := os.Stat(filepath.Join(o.Root, filepath.FromSlash(key)))
	info, err := o.Store.PutFile(ctx, key, file)
	if err != nil {
		return info, err
	}
	if err = appendCrashEvent(o.journal, map[string]any{"event": "put", "created": os.IsNotExist(existing), "sha256": info.SHA256}); err != nil {
		return info, err
	}
	if o.afterPut != nil {
		o.afterPut()
	}
	return info, nil
}

func TestScientificCrashWorkerChild(t *testing.T) {
	if os.Getenv("QIQC_TEST_SCIENCE_CRASH_CHILD") != "1" {
		t.Skip("internal process helper")
	}
	root := os.Getenv("QIQC_TEST_SCIENCE_CRASH_ROOT")
	phase := os.Getenv("QIQC_TEST_SCIENCE_CRASH_PHASE")
	if root == "" || (phase != "unknown_cleanup" && phase != "after_object_put" && phase != "resume") {
		t.Fatal("invalid isolated helper configuration")
	}
	s, err := Open(filepath.Join(root, "platform.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	objects := &processCrashObjects{Store: &rollout.Store{Root: filepath.Join(root, "objects")}, journal: filepath.Join(root, "events.jsonl")}
	s.Objects = objects
	stopAtBoundary := func() {
		f := os.NewFile(3, "parent-ready")
		if f == nil {
			t.Fatal("missing readiness pipe")
		}
		if _, err := f.Write([]byte{1}); err != nil {
			t.Fatal(err)
		}
		f.Close()
		// RunWorker's heartbeat still runs until the parent sends real SIGKILL.
		select {}
	}
	if phase == "after_object_put" {
		objects.afterPut = stopAtBoundary
	}
	executor := func(_ context.Context, l Lease) error {
		if err := appendCrashEvent(objects.journal, map[string]any{"event": "synthetic_executor", "mode": l.Mode}); err != nil {
			return err
		}
		if phase == "resume" {
			return errors.New("scientific executor must not rerun during archive recovery")
		}
		raw, _ := json.Marshal(l)
		if err := syncWrite(filepath.Join(root, "lease.json"), raw); err != nil {
			return err
		}
		if err := s.BeginUpload(l); err != nil {
			return err
		}
		cache := scientificRecoveryEvidence(t, s, l, false, phase != "unknown_cleanup")
		_ = cache // Synthetic executor publishes staged bytes exactly once, never executes Harness.
		if phase == "unknown_cleanup" {
			stopAtBoundary()
		}
		if err := s.PromoteScientificPending(l); err != nil {
			return err
		}
		raw, err = os.ReadFile(filepath.Join(cache, "pending.json"))
		if err != nil {
			return err
		}
		var p Pending
		if err = json.Unmarshal(raw, &p); err != nil {
			return err
		}
		return s.CompleteScientificPending(context.Background(), l, p.Reward, p.Seconds, p.SHA)
	}
	if err = s.RunWorker(context.Background(), 1, executor); err != nil {
		t.Fatal(err)
	}
}

func startScientificCrashChild(t *testing.T, root, phase string, ready bool) *exec.Cmd {
	t.Helper()
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(binary, "-test.run=^TestScientificCrashWorkerChild$", "-test.count=1")
	// Deliberately do not inherit cloud credentials or real service configuration.
	cmd.Env = []string{"QIQC_TEST_SCIENCE_CRASH_CHILD=1", "QIQC_TEST_SCIENCE_CRASH_ROOT=" + root, "QIQC_TEST_SCIENCE_CRASH_PHASE=" + phase, "GOTRACEBACK=none"}
	log, err := os.OpenFile(filepath.Join(root, "child-"+phase+".log"), os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0600)
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stdout = log
	cmd.Stderr = log
	t.Cleanup(func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
		}
		log.Close()
	})
	var reader, writer *os.File
	if ready {
		reader, writer, err = os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		cmd.ExtraFiles = []*os.File{writer}
		defer reader.Close()
	}
	if err = cmd.Start(); err != nil {
		if writer != nil {
			writer.Close()
		}
		t.Fatal(err)
	}
	if ready {
		writer.Close()
		received := make(chan error, 1)
		go func() { var b [1]byte; _, e := reader.Read(b[:]); received <- e }()
		select {
		case e := <-received:
			if e != nil {
				t.Fatalf("worker failed before requested crash boundary: %v", e)
			}
		case <-time.After(20 * time.Second):
			t.Fatal("worker did not reach crash boundary")
		}
	}
	return cmd
}
func killScientificCrashChild(t *testing.T, cmd *exec.Cmd) {
	t.Helper()
	if err := cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	err := cmd.Wait()
	var exit *exec.ExitError
	if !errors.As(err, &exit) {
		t.Fatalf("expected SIGKILL, received %v", err)
	}
	status, ok := exit.Sys().(syscall.WaitStatus)
	if !ok || !status.Signaled() || status.Signal() != syscall.SIGKILL {
		t.Fatalf("wrong child termination: %v", exit)
	}
}
func crashCounts(t *testing.T, root string) map[string]int {
	t.Helper()
	result := map[string]int{}
	raw, err := os.ReadFile(filepath.Join(root, "events.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var event map[string]any
		if json.Unmarshal([]byte(line), &event) != nil {
			t.Fatal("invalid crash journal")
		}
		name := event["event"].(string)
		result[name]++
		if name == "put" && event["created"] == true {
			result["objects_created"]++
		}
	}
	return result
}

func TestScientificWorkerSIGKILLPendingRecoveryCombination(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("requires Linux SIGKILL")
	}
	started := time.Now()
	rows := []map[string]any{}
	for _, phase := range []string{"unknown_cleanup", "after_object_put"} {
		t.Run(phase, func(t *testing.T) {
			s := testStore(t)
			root, path := s.Root, s.Path
			token, version := approved(t, s)
			s.ScienceHashes = map[string]bool{version.SHA256: true}
			if _, err := s.Confirm(token, version.ID, "infra", "scientific-smoke", "independent-worker-crash"); err != nil {
				t.Fatal(err)
			}
			if err := s.Close(); err != nil {
				t.Fatal(err)
			}
			child := startScientificCrashChild(t, root, phase, true)
			killScientificCrashChild(t, child)
			raw, err := os.ReadFile(filepath.Join(root, "lease.json"))
			if err != nil {
				t.Fatal(err)
			}
			var l Lease
			if json.Unmarshal(raw, &l) != nil {
				t.Fatal("missing worker lease")
			}
			s, err = Open(path)
			if err != nil {
				t.Fatal(err)
			}
			defer s.Close()
			objects := &processCrashObjects{Store: &rollout.Store{Root: filepath.Join(root, "objects")}, journal: filepath.Join(root, "events.jsonl")}
			s.Objects = objects
			cache := filepath.Join(root, "cache", l.RunID, strconv.Itoa(l.Attempt))
			archiveBefore, _, err := rollout.Digest(filepath.Join(cache, "rollout.tar.gz"))
			if err != nil {
				t.Fatal(err)
			}
			waitSeconds := 0.0
			if phase == "unknown_cleanup" {
				// No clock manipulation: the original child's real sixty-second lease expires.
				var expires float64
				if err = s.DB.QueryRow("SELECT lease_until FROM runs WHERE id=?", l.RunID).Scan(&expires); err != nil {
					t.Fatal(err)
				}
				waitStarted := time.Now()
				for s.now() <= expires {
					time.Sleep(100 * time.Millisecond)
					if time.Since(waitStarted) > 75*time.Second {
						t.Fatal("real lease failed to expire")
					}
				}
				waitSeconds = time.Since(waitStarted).Seconds()
				if n, e := s.ResumeUploads(context.Background()); e != nil || n != 0 {
					t.Fatal("unknown cleanup uploaded", n, e)
				}
				recovered, e := s.Recover()
				if e != nil || recovered.Attention != 1 {
					t.Fatal("unknown cleanup not fenced", recovered, e)
				}
				if status(s.ValidateLease(l)) != 409 || status(s.ResumeUploadLease(l)) != 409 {
					t.Fatal("old worker not fenced")
				}
				if e = s.ReconcileScientificResources(l.RunID, l.Attempt, "test receipt remains unconfirmed"); e == nil {
					t.Fatal("unknown provider state reconciled")
				}
				blockedBatch := queueBatch(t, s, 1, "simulated")
				if claim, e := s.Claim("slot-check", 1); e != nil || claim != nil {
					t.Fatal("unknown remote resource slot released", e)
				}
				// Remove only the separate synthetic queued slot-check fixture before recovery worker starts.
				if _, e = s.DB.Exec("DELETE FROM runs WHERE batch_id=?", blockedBatch); e != nil {
					t.Fatal(e)
				}
				if _, e = s.DB.Exec("DELETE FROM batches WHERE id=?", blockedBatch); e != nil {
					t.Fatal(e)
				}
				scientificRecoveryReceipt(t, s, l, true) // Explicit fake provider confirmation, not E2B proof.
				if e = s.ReconcileScientificResources(l.RunID, l.Attempt, "local test fake provider confirms cleanup"); e != nil {
					t.Fatal(e)
				}
				objects.fail = true
				if n, e := s.ResumeUploads(context.Background()); e == nil || n != 0 {
					t.Fatal("object outage not preserved", n, e)
				}
				if _, e = os.Stat(filepath.Join(cache, "pending.json")); e != nil {
					t.Fatal("outage lost pending", e)
				}
				objects.fail = false
			}
			resumed := startScientificCrashChild(t, root, "resume", false)
			deadline := time.Now().Add(15 * time.Second)
			for {
				var state string
				if err = s.DB.QueryRow("SELECT state FROM runs WHERE id=?", l.RunID).Scan(&state); err != nil {
					t.Fatal(err)
				}
				_, cacheErr := os.Stat(cache)
				if state == "completed" && os.IsNotExist(cacheErr) {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("restarted worker did not finish same pending evidence", state)
				}
				time.Sleep(50 * time.Millisecond)
			}
			killScientificCrashChild(t, resumed)
			var attempts, ledger, notices int
			if err = s.DB.QueryRow("SELECT COUNT(*) FROM attempts WHERE run_id=?", l.RunID).Scan(&attempts); err != nil {
				t.Fatal(err)
			}
			if err = s.DB.QueryRow("SELECT COUNT(*) FROM ledger WHERE batch_id=?", l.BatchID).Scan(&ledger); err != nil {
				t.Fatal(err)
			}
			if err = s.DB.QueryRow("SELECT COUNT(*) FROM outbox WHERE event='evaluation_completed' AND payload LIKE ?", "%"+l.BatchID+"%").Scan(&notices); err != nil {
				t.Fatal(err)
			}
			counts := crashCounts(t, root)
			if attempts != 1 || ledger != 1 || notices != 1 || counts["synthetic_executor"] != 1 || counts["objects_created"] != 1 {
				t.Fatal("duplicate execution/result/object", attempts, ledger, notices, counts)
			}
			var storedSHA, key string
			if err = s.DB.QueryRow("SELECT object_sha,object_key FROM runs WHERE id=?", l.RunID).Scan(&storedSHA, &key); err != nil {
				t.Fatal(err)
			}
			if storedSHA != archiveBefore {
				t.Fatal("recovery changed the original staged evidence")
			}
			if err = s.Objects.WithFile(context.Background(), key, storedSHA, func(string) error { return nil }); err != nil {
				t.Fatal(err)
			}
			if n, e := s.ResumeUploads(context.Background()); e != nil || n != 0 {
				t.Fatal("completed restart not idempotent", n, e)
			}
			if status(s.ValidateLease(l)) != 409 {
				t.Fatal("dead lease accepted after completion")
			}
			rows = append(rows, map[string]any{"phase": phase, "passed": true, "independent_runworker_sigkill": true, "restarted_independent_runworker": true, "clock_modified": false, "lease_wait_seconds": waitSeconds, "attempts": attempts, "ledger_debits": ledger, "completion_notifications": notices, "journal": counts, "cache_removed_after_completion": true})
		})
	}
	if t.Failed() {
		return
	}
	if output := os.Getenv("QIQC_SCIENCE_CRASH_REPORT"); output != "" {
		binary, _ := os.Executable()
		binarySHA, _, err := rollout.Digest(binary)
		if err != nil {
			t.Fatal(err)
		}
		sources := map[string]string{}
		for _, name := range []string{"scientific_process_crash_test.go", "worker.go", "queue.go", "completion.go", "scientific_completion.go", "scientific_pending_guard.go", "scientific_reconcile.go"} {
			sha, _, e := rollout.Digest(name)
			if e != nil {
				t.Fatal(e)
			}
			sources[name] = sha
		}
		report := map[string]any{"schema": "qiqc-scientific-process-crash/v1", "checked_at": time.Now().UTC().Format(time.RFC3339), "passed": true, "level": "real-local-worker-process-crash-with-fake-science-and-local-object-store", "cloud_calls": false, "harness_executed": false, "real_scientific_verifier": false, "real_provider_cleanup": false, "binary_sha256": binarySHA, "sources_sha256": sources, "seconds": time.Since(started).Seconds(), "scenarios": rows, "completed_cache_evidence": "Separate existing TestCompletedEvidenceCleanupRequiresMatchingDatabaseAndObject; not an actual post-commit SIGKILL window in this test.", "limitations": []string{"No real E2B/R2/Harbor/model; not formal scientific cloud crash acceptance.", "Two PutFile calls may occur after crash-after-object-write, but only one immutable object is created.", "Synthetic staged bytes and provider cleanup receipts are explicitly fake."}}
		data, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(output, append(data, '\n'), 0600); err != nil {
			t.Fatal(err)
		}
		fmt.Printf("scientific crash report: %s\n", filepath.Base(output))
	}
}
