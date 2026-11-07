package platform

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestSubmissionRestartRetainsFrozenReviewAndRejectsTamper(t *testing.T) {
	s := testStore(t)
	withScanner(s)
	token := readyUser(t, s, "recovery@example.test", "team")
	admin := readyUser(t, s, "reviewer@example.test", "admin")
	v, err := s.Upload(token, []byte("scanned immutable archive"))
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Review(admin, v.ID, "approve", "reviewer", "checked bytes", "local"); err != nil {
		t.Fatal(err)
	}
	path := s.Path
	s.Close()
	s, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	withScanner(s)
	versions, err := s.Versions(token)
	if err != nil || len(versions) != 1 || versions[0].Status != "approved" {
		t.Fatal(versions, err)
	}
	var raw string
	if err = s.DB.QueryRow("SELECT report FROM versions WHERE id=?", v.ID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var report Report
	if err = json.Unmarshal([]byte(raw), &report); err != nil {
		t.Fatal(err)
	}
	if report.Source != "authenticated_team_upload" || report.SourceSHA256 != v.SHA256 {
		t.Fatal("lost submission provenance")
	}
	// A crash after the durable hard-link but before the DB transaction leaves a
	// harmless orphan. A resubmission must reuse exactly those bytes.
	orphan := []byte("crash before database commit")
	if err = os.WriteFile(filepath.Join(s.Root, "uploads", shaBytes(orphan)+".zip"), orphan, 0600); err != nil {
		t.Fatal(err)
	}
	recovered, err := s.Upload(token, orphan)
	if err != nil || recovered.SHA256 != shaBytes(orphan) {
		t.Fatal(err)
	}
	// Stale quarantine bytes remain private and never become a version implicitly.
	if err = os.WriteFile(filepath.Join(s.Root, "quarantine", "incoming-123.zip"), []byte("partial crash upload"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(s.Root, "uploads", v.SHA256+".zip"), []byte("tampered"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Confirm(token, v.ID, "infra", "simulated", "after-restart"); err == nil {
		t.Fatal("tampered approved bytes dispatched")
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&n)
	if n != 0 {
		t.Fatal("failed dispatch consumed quota")
	}
}

func TestSubmissionVersionLimitDoesNotAccumulateCanonicalOrphans(t *testing.T) {
	s := testStore(t)
	withScanner(s)
	token := readyUser(t, s, "limit@example.test", "team")
	for i := 0; i < 25; i++ {
		if _, err := s.Upload(token, []byte(fmt.Sprint("accepted-", i))); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < 10; i++ {
		if _, err := s.Upload(token, []byte(fmt.Sprint("over-limit-", i))); status(err) != 429 {
			t.Fatal(err)
		}
	}
	files, err := os.ReadDir(filepath.Join(s.Root, "uploads"))
	if err != nil || len(files) != 25 {
		t.Fatalf("canonical archive leak: %d %v", len(files), err)
	}
	files, err = os.ReadDir(filepath.Join(s.Root, "quarantine"))
	if err != nil || len(files) != 0 {
		t.Fatal("quarantine leak", err)
	}
}
