package platform

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

func nativeCleanupFixture(t *testing.T) (*Store, *Lease, string) {
	t.Helper()
	s := testStore(t)
	if err := s.ConfigureScientificCandidate("native-candidate-" + strings.Repeat("a", 64)); err != nil {
		t.Fatal(err)
	}
	team, version := approved(t, s)
	admin := readyUser(t, s, "native-cleanup@example.test", "admin")
	s.ScienceHashes = map[string]bool{version.SHA256: true}
	if err := s.AdminAction(admin, targetAction(version.ID, 48), "local"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Confirm(team, version.ID, "infra", "scientific-smoke", "native-cleanup"); err != nil {
		t.Fatal(err)
	}
	return s, mustClaim(t, s, "native-controller", 1), team
}

func writeNativeCleanupReceipt(t *testing.T, s *Store, l Lease, resources map[string]string) {
	t.Helper()
	dir := filepath.Join(s.Root, "scientific")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(map[string]any{"run_id": l.RunID, "attempt": l.Attempt, "mode": l.Mode, "resources": resources})
	if err != nil {
		t.Fatal(err)
	}
	if err = syncWrite(filepath.Join(dir, l.RunID+"_1.json"), raw); err != nil {
		t.Fatal(err)
	}
}

func TestNativeCleanupUsesRegisteredBatchTopology(t *testing.T) {
	s, lease, _ := nativeCleanupFixture(t)
	for _, tc := range []struct {
		name      string
		resources map[string]string
		accepted  bool
	}{
		{"one_destroyed_host", map[string]string{"host": "destroyed"}, true},
		{"missing", map[string]string{}, false},
		{"still_running", map[string]string{"host": "created"}, false},
		{"unknown_extra", map[string]string{"host": "destroyed", "extra": "created"}, false},
		{"wrong_three_vm_topology", map[string]string{"main": "destroyed", "qsim": "destroyed", "verifier": "destroyed"}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			writeNativeCleanupReceipt(t, s, *lease, tc.resources)
			if got := s.scientificCleanupConfirmed(*lease) == nil; got != tc.accepted {
				t.Fatalf("accepted=%v want=%v", got, tc.accepted)
			}
		})
	}
	writeNativeCleanupReceipt(t, s, *lease, map[string]string{"host": "destroyed"})
	for _, statement := range []string{
		"UPDATE batch_scientific_targets SET release_id='native-release-" + strings.Repeat("b", 64) + "'",
		"DELETE FROM batch_scientific_targets",
	} {
		if _, err := s.DB.Exec(statement); err != nil {
			t.Fatal(err)
		}
		if s.scientificCleanupConfirmed(*lease) == nil {
			t.Fatal("missing or unregistered target accepted")
		}
	}
}

func TestLegacyCleanupStillRequiresThreeVMs(t *testing.T) {
	s := testStore(t)
	l := Lease{RunID: "legacy-attempt", Attempt: 1, Mode: "scientific-smoke"}
	writeNativeCleanupReceipt(t, s, l, map[string]string{"host": "destroyed"})
	if s.scientificCleanupConfirmed(l) == nil {
		t.Fatal("legacy partial cleanup accepted")
	}
	writeNativeCleanupReceipt(t, s, l, map[string]string{"main": "destroyed", "qsim": "destroyed", "verifier": "destroyed"})
	if err := s.scientificCleanupConfirmed(l); err != nil {
		t.Fatal(err)
	}
}

func TestNativeCleanupPromotesAndRecoversSameArchive(t *testing.T) {
	s, l, team := nativeCleanupFixture(t)
	if err := s.BeginUpload(*l); err != nil {
		t.Fatal(err)
	}
	cache := filepath.Join(s.Root, "cache", l.RunID, "1")
	if err := os.MkdirAll(cache, 0700); err != nil {
		t.Fatal(err)
	}
	archive := []byte("trusted native scientific rollout fixture")
	if err := syncWrite(filepath.Join(cache, "rollout.tar.gz"), archive); err != nil {
		t.Fatal(err)
	}
	p := Pending{Lease: *l, Reward: 0, Seconds: "300.116000", Provenance: "scientific-smoke", SHA: shaBytes(archive)}
	raw, _ := json.Marshal(p)
	if err := syncWrite(filepath.Join(cache, "staged.json"), raw); err != nil {
		t.Fatal(err)
	}
	writeNativeCleanupReceipt(t, s, *l, map[string]string{"host": "destroyed"})
	if err := s.PromoteScientificPending(*l); err != nil {
		t.Fatal(err)
	}
	// Recover uses an existing SQL transaction. Its cleanup check must read
	// the native binding through that same transaction and preserve upload work.
	s.Now = func() time.Time { return time.Now().Add(5 * time.Minute) }
	if _, err := s.Recover(); err != nil {
		t.Fatal(err)
	}
	s.Objects = &rollout.Store{Root: filepath.Join(s.Root, "objects")}
	if n, err := s.ResumeUploads(context.Background()); err != nil || n != 1 {
		t.Fatalf("resume=%d err=%v", n, err)
	}
	view, err := s.GetBatch(team, l.BatchID)
	if err != nil || view["status"] != "completed" {
		t.Fatal("native result unavailable", err)
	}
	var attempts, debits int
	s.DB.QueryRow("SELECT count(*) FROM attempts WHERE run_id=?", l.RunID).Scan(&attempts)
	s.DB.QueryRow("SELECT count(*) FROM ledger WHERE batch_id=? AND action='accepted'", l.BatchID).Scan(&debits)
	if attempts != 1 || debits != 1 {
		t.Fatal("re-executed or double charged")
	}
	if _, err := os.Stat(cache); !os.IsNotExist(err) {
		t.Fatal("upload cache retained", err)
	}
}

func TestNativeStagedPublicationOperatorRecovery(t *testing.T) {
	s, l, team := nativeCleanupFixture(t)
	if _, err := s.DB.Exec("UPDATE runs SET state='attention',error='platform:operator_review_required' WHERE id=?", l.RunID); err != nil {
		t.Fatal(err)
	}
	if _, err := s.DB.Exec("UPDATE batches SET status='attention' WHERE id=?", l.BatchID); err != nil {
		t.Fatal(err)
	}
	if s.CheckScientificReconciliation(l.RunID, 1) == nil {
		t.Fatal("generic failure eligible without completed evidence")
	}
	cache := filepath.Join(s.Root, "cache", l.RunID, "1")
	if err := os.MkdirAll(cache, 0700); err != nil {
		t.Fatal(err)
	}
	archive := []byte("completed native rollout, unchanged through recovery")
	p := Pending{Lease: *l, Reward: 0, Seconds: "300.116000", Provenance: "scientific-smoke", SHA: shaBytes(archive)}
	raw, _ := json.Marshal(p)
	if err := syncWrite(filepath.Join(cache, "staged.json"), raw); err != nil {
		t.Fatal(err)
	}
	if err := syncWrite(filepath.Join(cache, "rollout.tar.gz"), archive); err != nil {
		t.Fatal(err)
	}
	writeNativeCleanupReceipt(t, s, *l, map[string]string{"host": "destroyed"})
	receiptPath := filepath.Join(s.Root, "scientific", l.RunID+"_1.json")
	receipt := map[string]any{"run_id": l.RunID, "attempt": 1, "mode": l.Mode, "phase": "native_harbor_trial", "resources": map[string]string{"host": "destroyed"}}
	writeReceipt := func() {
		t.Helper()
		data, _ := json.Marshal(receipt)
		if err := syncWrite(receiptPath, data); err != nil {
			t.Fatal(err)
		}
	}
	writeReceipt()
	if s.CheckScientificReconciliation(l.RunID, 1) == nil {
		t.Fatal("unfinished scientific phase accepted")
	}
	receipt["phase"] = "uploading_rollout"
	receipt["controller_error_class"] = "NativeFailure"
	writeReceipt()
	if s.CheckScientificReconciliation(l.RunID, 1) == nil {
		t.Fatal("failed controller accepted")
	}
	delete(receipt, "controller_error_class")
	writeReceipt()
	if err := syncWrite(filepath.Join(cache, "rollout.tar.gz"), []byte("changed")); err != nil {
		t.Fatal(err)
	}
	if s.CheckScientificReconciliation(l.RunID, 1) == nil {
		t.Fatal("changed archive accepted")
	}
	if err := syncWrite(filepath.Join(cache, "rollout.tar.gz"), archive); err != nil {
		t.Fatal(err)
	}
	if err := s.CheckScientificReconciliation(l.RunID, 1); err != nil {
		t.Fatal(err)
	}
	if s.ReconcileScientificResources(l.RunID, 1, "same archive recovery") == nil {
		t.Fatal("provider confirmation missing")
	}
	receipt["provider_checked"] = true
	writeReceipt()
	if err := s.ReconcileScientificResources(l.RunID, 1, "same archive recovery"); err != nil {
		t.Fatal(err)
	}
	s.Objects = &rollout.Store{Root: filepath.Join(s.Root, "objects")}
	if n, err := s.ResumeUploads(context.Background()); err != nil || n != 1 {
		t.Fatal(n, err)
	}
	view, err := s.GetBatch(team, l.BatchID)
	if err != nil || view["status"] != "completed" {
		t.Fatal("same run not completed", err)
	}
	var attempts, audits int
	s.DB.QueryRow("SELECT count(*) FROM attempts WHERE run_id=?", l.RunID).Scan(&attempts)
	s.DB.QueryRow("SELECT count(*) FROM audit WHERE action='reconcile_upload' AND object_id=?", l.RunID).Scan(&audits)
	if attempts != 1 || audits != 1 {
		t.Fatal("recovery reran science or lost audit", attempts, audits)
	}
	if s.CheckScientificReconciliation(l.RunID, 1) == nil {
		t.Fatal("finished run eligible twice")
	}
}
