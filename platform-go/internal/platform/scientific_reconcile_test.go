package platform

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestScientificReconcileRequiresCheckedCurrentDestroyedReceipt(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	_, err := s.Confirm(tok, v.ID, "infra", "scientific-smoke", "reconcile")
	if err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("lost-controller", 1)
	if err != nil {
		t.Fatal(err)
	}
	s.DB.Exec("UPDATE runs SET state='attention',error=?,lease_until=NULL WHERE id=?", lostWorkerError, l.RunID)
	s.DB.Exec("UPDATE batches SET status='attention' WHERE id=?", l.BatchID)
	dir := filepath.Join(s.Root, "scientific")
	os.MkdirAll(dir, 0700)
	p := filepath.Join(dir, l.RunID+"_1.json")
	receipt := map[string]any{"run_id": l.RunID, "attempt": 1, "mode": "scientific-smoke", "provider_checked": false, "resources": map[string]string{"sandbox": "created"}}
	save := func() {
		b, _ := json.Marshal(receipt)
		if err := os.WriteFile(p, b, 0600); err != nil {
			t.Fatal(err)
		}
	}
	save()
	if s.ReconcileScientificResources(l.RunID, 1, "operator checked") == nil {
		t.Fatal("unchecked receipt accepted")
	}
	receipt["provider_checked"] = true
	save()
	if s.ReconcileScientificResources(l.RunID, 1, "operator checked") == nil {
		t.Fatal("live sandbox accepted")
	}
	receipt["resources"] = map[string]string{"sandbox": "destroyed"}
	receipt["attempt"] = 2
	save()
	if s.ReconcileScientificResources(l.RunID, 1, "operator checked") == nil {
		t.Fatal("other attempt accepted")
	}
	receipt["attempt"] = 1
	save()
	if err = s.ReconcileScientificResources(l.RunID, 1, "positive E2B 404 after kill"); err != nil {
		t.Fatal(err)
	}
	var why string
	s.DB.QueryRow("SELECT error FROM runs WHERE id=?", l.RunID).Scan(&why)
	if why != "platform:reconciled" {
		t.Fatal("fence not reconciled")
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action='reconcile_resources'").Scan(&n)
	if n != 1 {
		t.Fatal("audit missing")
	}
	if s.ReconcileScientificResources(l.RunID, 1, "repeat") == nil {
		t.Fatal("stale reconciliation accepted")
	}
}

func TestReconcilePromotesStagedEvidenceWithoutRequeue(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	_, err := s.Confirm(tok, v.ID, "infra", "scientific-smoke", "reconcile-staged")
	if err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("old-worker", 1)
	if err != nil {
		t.Fatal(err)
	}
	s.DB.Exec("UPDATE runs SET state='attention',error=?,lease_until=NULL WHERE id=?", lostWorkerError, l.RunID)
	s.DB.Exec("UPDATE batches SET status='attention' WHERE id=?", l.BatchID)
	dir := filepath.Join(s.Root, "scientific")
	os.MkdirAll(dir, 0700)
	receipt := map[string]any{"run_id": l.RunID, "attempt": 1, "mode": "scientific-smoke", "provider_checked": true, "resources": map[string]string{"main": "destroyed", "qsim": "destroyed", "verifier": "destroyed"}}
	raw, _ := json.Marshal(receipt)
	os.WriteFile(filepath.Join(dir, l.RunID+"_1.json"), raw, 0600)
	cache := filepath.Join(s.Root, "cache", l.RunID, "1")
	os.MkdirAll(cache, 0700)
	data := []byte("same scientific evidence")
	os.WriteFile(filepath.Join(cache, "rollout.tar.gz"), data, 0600)
	pending := Pending{Lease: *l, Reward: 0, Seconds: "1.000000", Provenance: "scientific-smoke", SHA: shaBytes(data)}
	raw, _ = json.Marshal(pending)
	os.WriteFile(filepath.Join(cache, "staged.json"), raw, 0600)
	if err = s.ReconcileScientificResources(l.RunID, 1, "provider confirmed all destroyed"); err != nil {
		t.Fatal(err)
	}
	var state string
	var attempt int
	s.DB.QueryRow("SELECT state,attempt FROM runs WHERE id=?", l.RunID).Scan(&state, &attempt)
	if state != "uploading" || attempt != 1 {
		t.Fatal("scientific evidence requeued", state, attempt)
	}
	if _, err = os.Stat(filepath.Join(cache, "pending.json")); err != nil {
		t.Fatal("upload not promoted")
	}
	if _, err = os.Stat(filepath.Join(cache, "staged.json")); !os.IsNotExist(err) {
		t.Fatal("staging receipt not replaced")
	}
}

func TestScientificReconcilePreflightRejectsHealthyAndStaleAttempt(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	if _, err := s.Confirm(tok, v.ID, "infra", "scientific-smoke", "preflight"); err != nil {
		t.Fatal(err)
	}
	lease, err := s.Claim("healthy-worker", 1)
	if err != nil || lease == nil {
		t.Fatal(err)
	}
	if s.CheckScientificReconciliation(lease.RunID, 1) == nil {
		t.Fatal("healthy worker eligible for provider deletion")
	}
	if _, err = s.DB.Exec("UPDATE runs SET state='attention',error=?,lease_until=NULL WHERE id=?", lostWorkerError, lease.RunID); err != nil {
		t.Fatal(err)
	}
	if s.CheckScientificReconciliation(lease.RunID, 2) == nil {
		t.Fatal("stale operator attempt eligible for provider deletion")
	}
	if err = s.CheckScientificReconciliation(lease.RunID, 1); err != nil {
		t.Fatal("current lost worker not eligible", err)
	}
	if _, err = s.DB.Exec("UPDATE runs SET error='platform:reconciled' WHERE id=?", lease.RunID); err != nil {
		t.Fatal(err)
	}
	if s.CheckScientificReconciliation(lease.RunID, 1) == nil {
		t.Fatal("already reconciled attempt eligible for provider deletion")
	}
}
