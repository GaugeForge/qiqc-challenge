package platform

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestNativePrecreateReconciliationRequiresBoundFreshEvidence(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	if _, err := s.Confirm(tok, v.ID, "infra", "scientific-smoke", "precreate"); err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("failed-controller", 1)
	if err != nil || l == nil {
		t.Fatal(err)
	}
	s.DB.Exec("UPDATE runs SET state='attention',error=?,lease_until=NULL WHERE id=?", lostWorkerError, l.RunID)
	s.DB.Exec("UPDATE batches SET status='attention' WHERE id=?", l.BatchID)
	proof, _ := json.Marshal(map[string]any{"schema": "qiqc-native-startup-import-diagnostic/v1", "run_id": l.RunID, "attempt": 1, "exit_code": 1, "stdin_empty": true, "cloud_calls": false, "failure_before_main": true})
	sha := sha256.Sum256(proof)
	check := map[string]any{"kind": "native_precreate_import_failure", "purpose": "qiqc-native-candidate", "states": []string{"running", "paused"}, "matches": 0, "checked_at": int64(s.now()), "proof_sha256": hex.EncodeToString(sha[:])}
	receipt := map[string]any{"run_id": l.RunID, "attempt": 1, "mode": "scientific-smoke", "resources": map[string]string{}, "provider_checked": true, "precreate_proof": string(proof)}
	dir := filepath.Join(s.Root, "scientific")
	os.MkdirAll(dir, 0700)
	path := filepath.Join(dir, l.RunID+"_1.json")
	save := func() {
		t.Helper()
		raw, _ := json.Marshal(receipt)
		if err := os.WriteFile(path, raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	reject := func(label string) {
		t.Helper()
		save()
		if s.ReconcileScientificResources(l.RunID, 1, label) == nil {
			t.Fatal("accepted", label)
		}
	}
	reject("empty list alone")
	receipt["no_resources_created"] = check
	check["matches"] = 1
	reject("provider has resource")
	check["matches"] = 0
	check["checked_at"] = int64(s.now()) - 600
	reject("stale provider observation")
	check["checked_at"] = int64(s.now())
	check["proof_sha256"] = "wrong"
	reject("changed import proof")
	check["proof_sha256"] = hex.EncodeToString(sha[:])
	check["states"] = []string{"running"}
	reject("paused sandboxes unchecked")
	check["states"] = []string{"running", "paused"}
	receipt["resources"] = map[string]string{"sandbox": "destroyed"}
	reject("created resource contradicts precreate")
	receipt["resources"] = map[string]string{}
	save()
	if err := s.ReconcileScientificResources(l.RunID, 1, "exact import failure and provider empty"); err != nil {
		t.Fatal(err)
	}
	var reason string
	s.DB.QueryRow("SELECT error FROM runs WHERE id=?", l.RunID).Scan(&reason)
	if reason != "platform:reconciled" {
		t.Fatal(reason)
	}
	var after string
	if err := s.DB.QueryRow("SELECT after_json FROM audit WHERE action='reconcile_resources'").Scan(&after); err != nil {
		t.Fatal(err)
	}
	var event map[string]any
	json.Unmarshal([]byte(after), &event)
	if event["resource_count"] != float64(0) {
		t.Fatal("fabricated destroyed resource", event)
	}
	if s.ReconcileScientificResources(l.RunID, 1, "repeat") == nil {
		t.Fatal("duplicate reconciliation accepted")
	}
}
