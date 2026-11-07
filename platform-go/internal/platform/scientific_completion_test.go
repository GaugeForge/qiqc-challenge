package platform

import (
	"context"
	"encoding/json"
	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestScientificUploadOutageRestartsUploadWithoutScienceRerun(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	b, err := s.Confirm(token, v.ID, "infra", "scientific-smoke", "real-smoke")
	if err != nil {
		t.Fatal(err)
	}
	lease, err := s.Claim("scientific-controller", 1)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.BeginUpload(*lease); err != nil {
		t.Fatal(err)
	}
	cache := filepath.Join(s.Root, "cache", lease.RunID, "1")
	if err = os.MkdirAll(cache, 0700); err != nil {
		t.Fatal(err)
	}
	data := []byte("immutable real scientific evidence bytes supplied by trusted controller")
	if err = syncWrite(filepath.Join(cache, "rollout.tar.gz"), data); err != nil {
		t.Fatal(err)
	}
	pending := Pending{Lease: *lease, Reward: 0, Seconds: "12.345678", Provenance: "scientific-smoke", SHA: shaBytes(data)}
	raw, _ := json.Marshal(pending)
	if err = syncWrite(filepath.Join(cache, "pending.json"), raw); err != nil {
		t.Fatal(err)
	}
	scientificRecoveryReceipt(t, s, *lease, true)
	objects := &brokenObjects{&rollout.Store{Root: filepath.Join(s.Root, "objects")}, true}
	s.Objects = objects
	if err = s.CompleteScientificPending(context.Background(), *lease, 0, "12.345678", pending.SHA); err == nil {
		t.Fatal("R2 outage hidden")
	}
	if _, err = os.Stat(filepath.Join(cache, "pending.json")); err != nil {
		t.Fatal("recovery metadata lost")
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	objects.broken = false
	reopened.Objects = objects
	reopened.Now = func() time.Time { return time.Now().Add(5 * time.Minute) }
	n, err := reopened.ResumeUploads(context.Background())
	if err != nil || n != 1 {
		t.Fatalf("resume %d %v", n, err)
	}
	var attempts, ledger int
	reopened.DB.QueryRow("SELECT COUNT(*) FROM attempts").Scan(&attempts)
	reopened.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&ledger)
	if attempts != 1 || ledger != 1 {
		t.Fatal("science rerun or duplicate debit")
	}
	view, err := reopened.GetBatch(token, b["id"].(string))
	if err != nil || view["status"] != "completed" {
		t.Fatal("not complete", err)
	}
	if err = reopened.CompleteScientificPending(context.Background(), *lease, 0, "12.345678", pending.SHA); err != nil {
		t.Fatal("same receipt not idempotent", err)
	}
	if err = reopened.CompleteScientificPending(context.Background(), *lease, 1, "12.345678", pending.SHA); err == nil {
		t.Fatal("changed scientific reward accepted")
	}
}

func TestScientificStagedArchiveCannotPublishBeforeAllSandboxesDestroyed(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	_, err := s.Confirm(tok, v.ID, "infra", "scientific-smoke", "stage")
	if err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("worker", 1)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.BeginUpload(*l); err != nil {
		t.Fatal(err)
	}
	cache := filepath.Join(s.Root, "cache", l.RunID, "1")
	os.MkdirAll(cache, 0700)
	data := []byte("scientific evidence")
	os.WriteFile(filepath.Join(cache, "rollout.tar.gz"), data, 0600)
	p := Pending{Lease: *l, Reward: 0, Seconds: "1.000000", Provenance: "scientific-smoke", SHA: shaBytes(data)}
	raw, _ := json.Marshal(p)
	os.WriteFile(filepath.Join(cache, "staged.json"), raw, 0600)
	s.Objects = &rollout.Store{Root: filepath.Join(s.Root, "objects")}
	if n, err := s.ResumeUploads(context.Background()); err != nil || n != 0 {
		t.Fatal("maintenance published unconfirmed evidence")
	}
	dir := filepath.Join(s.Root, "scientific")
	os.MkdirAll(dir, 0700)
	receipt := map[string]any{"run_id": l.RunID, "attempt": 1, "mode": "scientific-smoke", "resources": map[string]string{"main": "destroyed", "qsim": "created", "verifier": "destroyed"}}
	raw, _ = json.Marshal(receipt)
	os.WriteFile(filepath.Join(dir, l.RunID+"_1.json"), raw, 0600)
	if s.PromoteScientificPending(*l) == nil {
		t.Fatal("live qsim released")
	}
	receipt["resources"].(map[string]string)["qsim"] = "destroyed"
	raw, _ = json.Marshal(receipt)
	os.WriteFile(filepath.Join(dir, l.RunID+"_1.json"), raw, 0600)
	if err = s.PromoteScientificPending(*l); err != nil {
		t.Fatal(err)
	}
	if n, err := s.ResumeUploads(context.Background()); err != nil || n != 1 {
		t.Fatal("confirmed cleanup did not permit upload", err)
	}
}
