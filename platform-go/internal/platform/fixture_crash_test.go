package platform

import (
	"context"
	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestFixturePreparationFailureDoesNotStrandUploading(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	if _, err := s.Confirm(tok, v.ID, "infra", "simulated", "prepare-failure"); err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("worker", 1)
	if err != nil || l == nil {
		t.Fatal(err)
	}
	cache := filepath.Join(s.Root, "cache")
	if err = os.RemoveAll(cache); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(cache, []byte("prevent directory creation"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = s.CompleteFixture(context.Background(), *l, 0, "1", "fixture"); err == nil {
		t.Fatal("expected preparation failure")
	}
	var state string
	if err = s.DB.QueryRow("SELECT state FROM runs WHERE id=?", l.RunID).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != "running" {
		t.Fatalf("incomplete evidence stranded state=%s", state)
	}
}

func TestFixturePreparedEvidenceRecoveryBeforeUploadTransition(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	objects := &brokenObjects{&rollout.Store{Root: filepath.Join(s.Root, "objects")}, true}
	s.Objects = objects
	if _, err := s.Confirm(tok, v.ID, "infra", "simulated", "prepared-crash"); err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("dead-worker", 1)
	if err != nil || l == nil {
		t.Fatal(err)
	}
	if err = s.CompleteFixture(context.Background(), *l, 1, "1.25", "fixture"); err == nil {
		t.Fatal("expected object outage")
	}
	// Reconstruct the durable boundary: pending.json exists, upload state commit has not happened.
	if _, err = s.DB.Exec("UPDATE runs SET state='running' WHERE id=?", l.RunID); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	objects.broken = false
	reopened.Objects = objects
	reopened.Now = func() time.Time { return time.Now().Add(2 * time.Minute) }
	recovery, err := reopened.Recover()
	if err != nil || recovery.Rescheduled != 0 || recovery.UploadsPreserved != 1 {
		t.Fatalf("prepared evidence lost during recovery: %+v %v", recovery, err)
	}
	count, err := reopened.ResumeUploads(context.Background())
	if err != nil || count != 1 {
		t.Fatalf("resume %d %v", count, err)
	}
	var state string
	var attempts, ledger, notices int
	reopened.DB.QueryRow("SELECT state FROM runs WHERE id=?", l.RunID).Scan(&state)
	reopened.DB.QueryRow("SELECT count(*) FROM attempts WHERE run_id=?", l.RunID).Scan(&attempts)
	reopened.DB.QueryRow("SELECT count(*) FROM ledger WHERE batch_id=?", l.BatchID).Scan(&ledger)
	reopened.DB.QueryRow("SELECT count(*) FROM outbox WHERE event='evaluation_completed'").Scan(&notices)
	if state != "completed" || attempts != 1 || ledger != 1 || notices != 1 {
		t.Fatalf("state=%s attempts=%d ledger=%d notices=%d", state, attempts, ledger, notices)
	}
}
