package platform

import (
	"context"
	"encoding/json"
	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
	"os"
	"path/filepath"
	"testing"
)

func TestCompletedEvidenceCleanupRequiresMatchingDatabaseAndObject(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	objects := &brokenObjects{&rollout.Store{Root: filepath.Join(s.Root, "objects")}, true}
	s.Objects = objects
	if _, err := s.Confirm(tok, v.ID, "infra", "simulated", "post-commit-crash"); err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("worker", 1)
	if err != nil || l == nil {
		t.Fatal(err)
	}
	if err = s.CompleteFixture(context.Background(), *l, 0, "1.25", "fixture"); err == nil {
		t.Fatal("expected outage")
	}
	cache := filepath.Join(s.Root, "cache", l.RunID, "1")
	raw, err := os.ReadFile(filepath.Join(cache, "pending.json"))
	if err != nil {
		t.Fatal(err)
	}
	bundle, err := os.ReadFile(filepath.Join(cache, "rollout.tar.gz"))
	if err != nil {
		t.Fatal(err)
	}
	objects.broken = false
	if n, err := s.ResumeUploads(context.Background()); err != nil || n != 1 {
		t.Fatalf("finish %d %v", n, err)
	}
	var key string
	if err = s.DB.QueryRow("SELECT object_key FROM runs WHERE id=?", l.RunID).Scan(&key); err != nil {
		t.Fatal(err)
	}
	objectFile := filepath.Join(objects.Root, filepath.FromSlash(key))
	for _, scenario := range []string{"changed_reward", "changed_bundle", "missing_object", "valid"} {
		t.Run(scenario, func(t *testing.T) {
			if err := os.MkdirAll(cache, 0700); err != nil {
				t.Fatal(err)
			}
			defer os.RemoveAll(cache)
			metadata := append([]byte(nil), raw...)
			bytes := append([]byte(nil), bundle...)
			if scenario == "changed_reward" {
				var p Pending
				json.Unmarshal(metadata, &p)
				p.Reward = 1
				metadata, _ = json.Marshal(p)
			}
			if scenario == "changed_bundle" {
				bytes = []byte("tampered")
			}
			if err := syncWrite(filepath.Join(cache, "rollout.tar.gz"), bytes); err != nil {
				t.Fatal(err)
			}
			if err := syncWrite(filepath.Join(cache, "pending.json"), metadata); err != nil {
				t.Fatal(err)
			}
			if scenario == "missing_object" {
				if err := os.Remove(objectFile); err != nil {
					t.Fatal(err)
				}
				defer os.WriteFile(objectFile, bundle, 0600)
			}
			n, err := s.ResumeUploads(context.Background())
			if scenario == "valid" {
				if err != nil || n != 1 {
					t.Fatalf("completed evidence not cleaned: %d %v", n, err)
				}
				if _, err = os.Stat(cache); !os.IsNotExist(err) {
					t.Fatal("cache still present")
				}
			} else {
				if n != 0 {
					t.Fatal("mismatched evidence cleaned")
				}
				if _, err = os.Stat(filepath.Join(cache, "pending.json")); err != nil {
					t.Fatal("evidence removed without checks")
				}
			}
		})
	}
	var attempts, ledger, notices int
	s.DB.QueryRow("SELECT count(*) FROM attempts WHERE run_id=?", l.RunID).Scan(&attempts)
	s.DB.QueryRow("SELECT count(*) FROM ledger WHERE batch_id=?", l.BatchID).Scan(&ledger)
	s.DB.QueryRow("SELECT count(*) FROM outbox WHERE event='evaluation_completed'").Scan(&notices)
	if attempts != 1 || ledger != 1 || notices != 1 {
		t.Fatalf("duplicate state %d/%d/%d", attempts, ledger, notices)
	}
}
