package platform

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

func TestScientificFailureEvidenceR2OutageResumesWithoutResultOrRerun(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	b, err := s.Confirm(token, v.ID, "infra", "scientific-smoke", "failure-evidence")
	if err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("trusted-controller", 1)
	if err != nil {
		t.Fatal(err)
	}
	cache := filepath.Join(s.Root, "failure-evidence", l.RunID, "1")
	if err = os.MkdirAll(cache, 0700); err != nil {
		t.Fatal(err)
	}
	data := []byte("complete failed Harbor trial archive, private and non-scoring")
	if err = syncWrite(filepath.Join(cache, "rollout.tar.gz"), data); err != nil {
		t.Fatal(err)
	}
	p := FailedScientificEvidence{Lease: *l, SHA: shaBytes(data), Format: "harbor-trial-0.23", ErrorClass: "ProbeError"}
	raw, _ := json.Marshal(p)
	if err = syncWrite(filepath.Join(cache, "pending.json"), raw); err != nil {
		t.Fatal(err)
	}
	objects := &brokenObjects{&rollout.Store{Root: filepath.Join(s.Root, "objects")}, true}
	s.Objects = objects
	n, err := s.ResumeScientificFailureUploads(context.Background())
	if err == nil || n != 0 {
		t.Fatal("outage hidden", n, err)
	}
	if _, err = os.Stat(filepath.Join(cache, "rollout.tar.gz")); err != nil {
		t.Fatal("failure evidence lost")
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	objects.broken = false
	reopened.Objects = objects
	n, err = reopened.ResumeScientificFailureUploads(context.Background())
	if err != nil || n != 1 {
		t.Fatal("failure resume", n, err)
	}
	var attempts, ledger, artifacts int
	var state string
	var reward any
	reopened.DB.QueryRow("SELECT COUNT(*) FROM attempts").Scan(&attempts)
	reopened.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&ledger)
	reopened.DB.QueryRow("SELECT COUNT(*) FROM run_artifacts WHERE object_sha=?", p.SHA).Scan(&artifacts)
	reopened.DB.QueryRow("SELECT state,reward FROM runs WHERE id=?", l.RunID).Scan(&state, &reward)
	if attempts != 1 || ledger != 1 || artifacts != 1 || state != "running" || reward != nil {
		t.Fatalf("failure upload changed execution/scoring: %d %d %d %s %v", attempts, ledger, artifacts, state, reward)
	}
	view, err := reopened.GetBatch(token, b["id"].(string))
	if err != nil || view["status"] == "completed" {
		t.Fatal("failure wrongly completed", err)
	}
	if _, err = reopened.Trace(context.Background(), token, b["id"].(string), 0); err == nil {
		t.Fatal("private failed evidence exposed")
	}
	n, err = reopened.ResumeScientificFailureUploads(context.Background())
	if err != nil || n != 0 {
		t.Fatal("duplicate recovery", n, err)
	}
}

func TestFinalizeScientificFailureArtifactsClosesFinishedController(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	b, err := s.Confirm(token, v.ID, "infra", "scientific-smoke", "terminal-failure")
	if err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("trusted-controller", 1)
	if err != nil || l == nil {
		t.Fatal("claim", err)
	}
	if err = s.initFailureArtifacts(); err != nil {
		t.Fatal(err)
	}
	if _, err = s.DB.Exec("INSERT INTO run_artifacts(run_id,attempt,kind,object_key,object_sha,format,error_class,created) VALUES(?,?,?,?,?,?,?,?)", l.RunID, l.Attempt, "failed-rollout", "private/failure.tar.gz", strings.Repeat("a", 64), "controller-diagnostic", "NativeCandidateFailure", s.now()); err != nil {
		t.Fatal(err)
	}
	receipt := map[string]any{"run_id": l.RunID, "phase": "download_complete_failure_rollout", "controller_error_class": "native_runner_failed", "resources": map[string]string{"sandbox": "destroyed"}}
	if err = os.MkdirAll(filepath.Join(s.Root, "scientific"), 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(s.Root, "scientific", l.RunID+"_"+strconv.Itoa(l.Attempt)+".json")
	rawReceipt, _ := json.Marshal(receipt)
	if err = os.WriteFile(path, rawReceipt, 0600); err != nil {
		t.Fatal(err)
	}
	n, err := s.FinalizeScientificFailureArtifacts()
	if err != nil || n != 1 {
		t.Fatalf("finalize=%d err=%v", n, err)
	}
	var state, why, batchState, category, phase, code string
	if err = s.DB.QueryRow("SELECT r.state,r.error,b.status,COALESCE(r.failure_category,''),COALESCE(r.failure_phase,''),COALESCE(r.failure_code,'') FROM runs r JOIN batches b ON b.id=? WHERE r.id=?", b["id"], l.RunID).Scan(&state, &why, &batchState, &category, &phase, &code); err != nil {
		t.Fatal(err)
	}
	if state != "attention" || why != "harness:operator_review_required" || batchState != "attention" || category != "harness" || phase != "download_complete_failure_rollout" || code != "native_runner_failed" {
		t.Fatalf("unexpected terminal failure state: %s %s %s %s %s %s", state, why, batchState, category, phase, code)
	}
}
