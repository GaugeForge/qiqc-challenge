package platform

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

func scientificRecoveryEvidence(t *testing.T, s *Store, l Lease, pending, clean bool) string {
	t.Helper()
	cache := filepath.Join(s.Root, "cache", l.RunID, strconv.Itoa(l.Attempt))
	if err := os.MkdirAll(cache, 0700); err != nil {
		t.Fatal(err)
	}
	// Only local state-machine evidence. No scientific task or verifier executes.
	archive := []byte("unchanged mocked scientific evidence")
	if err := syncWrite(filepath.Join(cache, "rollout.tar.gz"), archive); err != nil {
		t.Fatal(err)
	}
	metadata := Pending{Lease: l, Reward: 0, Seconds: "1.000000", Provenance: "scientific-smoke", SHA: shaBytes(archive)}
	raw, _ := json.Marshal(metadata)
	name := "staged.json"
	if pending {
		name = "pending.json"
	}
	if err := syncWrite(filepath.Join(cache, name), raw); err != nil {
		t.Fatal(err)
	}
	scientificRecoveryReceipt(t, s, l, clean)
	return cache
}

func scientificRecoveryReceipt(t *testing.T, s *Store, l Lease, clean bool) {
	t.Helper()
	state := "created"
	if clean {
		state = "destroyed"
	}
	receipt := map[string]any{"run_id": l.RunID, "attempt": l.Attempt, "mode": "scientific-smoke", "provider_checked": clean, "resources": map[string]string{"mock-main": state, "mock-qsim": state, "mock-verifier": state}}
	raw, _ := json.Marshal(receipt)
	directory := filepath.Join(s.Root, "scientific")
	if err := os.MkdirAll(directory, 0700); err != nil {
		t.Fatal(err)
	}
	if err := syncWrite(filepath.Join(directory, l.RunID+"_"+strconv.Itoa(l.Attempt)+".json"), raw); err != nil {
		t.Fatal(err)
	}
}

func TestExpiredScientificUploadingWithoutPendingRequiresReconciliation(t *testing.T) {
	for _, staged := range []bool{false, true} {
		name := "no_evidence"
		if staged {
			name = "staged_evidence"
		}
		t.Run(name, func(t *testing.T) {
			s, l := leaseIdentityFixture(t, "scientific-smoke")
			s.Objects = &rollout.Store{Root: filepath.Join(s.Root, "objects")}
			if err := s.BeginUpload(l); err != nil {
				t.Fatal(err)
			}
			cache := ""
			if staged {
				cache = scientificRecoveryEvidence(t, s, l, false, false)
			} else {
				scientificRecoveryReceipt(t, s, l, false)
			}
			s.Now = func() time.Time { return time.Now().Add(2 * time.Minute) }
			recovered, err := s.Recover()
			if err != nil || recovered.Attention != 1 || recovered.Rescheduled != 0 || recovered.UploadsPreserved != 0 {
				t.Fatalf("expired scientific uploading stayed unreconcilable: %+v %v", recovered, err)
			}
			if err = s.CheckScientificReconciliation(l.RunID, l.Attempt); err != nil {
				t.Fatal("operator CLI preflight still refuses orphan", err)
			}
			if err = s.ValidateLease(l); status(err) != 409 {
				t.Fatal("old controller may publish")
			}
			if err = s.ResumeUploadLease(l); status(err) != 409 {
				t.Fatal("unconfirmed scientific upload revived")
			}
			if staged {
				if _, err = os.Stat(filepath.Join(cache, "staged.json")); err != nil {
					t.Fatal("staged evidence lost")
				}
				if _, err = os.Stat(filepath.Join(cache, "pending.json")); !os.IsNotExist(err) {
					t.Fatal("staged evidence exposed before cleanup")
				}
			}
			queueBatch(t, s, 1, "simulated")
			if next, e := s.Claim("new-worker", 1); e != nil || next != nil {
				t.Fatal("unconfirmed sandbox slot reused")
			}
			// Mock provider confirmation solely tests the existing trusted operator
			// transition. Real SDK confirmation is covered by the separate opt-in drill.
			scientificRecoveryReceipt(t, s, l, true)
			if err = s.ReconcileScientificResources(l.RunID, l.Attempt, "unit-test-only mocked cleanup confirmation"); err != nil {
				t.Fatal(err)
			}
			if staged {
				count, e := s.ResumeUploads(context.Background())
				if e != nil || count != 1 {
					t.Fatal("same staged attempt did not resume upload", count, e)
				}
			}
			next, e := s.Claim("new-worker", 1)
			if e != nil || next == nil {
				t.Fatal("slot remained blocked after reconciliation")
			}
			var attempts, ledger int
			if err = s.DB.QueryRow("SELECT COUNT(*) FROM attempts WHERE run_id=?", l.RunID).Scan(&attempts); err != nil {
				t.Fatal(err)
			}
			if err = s.DB.QueryRow("SELECT COUNT(*) FROM ledger WHERE batch_id=?", l.BatchID).Scan(&ledger); err != nil {
				t.Fatal(err)
			}
			if attempts != 1 || ledger != 1 {
				t.Fatal("recovery reran or charged original request", attempts, ledger)
			}
		})
	}
}

func TestExpiredScientificPendingStillRequiresConfirmedCleanup(t *testing.T) {
	s, l := leaseIdentityFixture(t, "scientific-smoke")
	if err := s.BeginUpload(l); err != nil {
		t.Fatal(err)
	}
	cache := scientificRecoveryEvidence(t, s, l, true, false)
	s.Now = func() time.Time { return time.Now().Add(2 * time.Minute) }
	recovered, err := s.Recover()
	if err != nil || recovered.Attention != 1 || recovered.UploadsPreserved != 0 {
		t.Fatalf("unconfirmed cleanup accepted as upload-only: %+v %v", recovered, err)
	}
	if err = s.ResumeUploadLease(l); status(err) != 409 {
		t.Fatal("unconfirmed resources released by upload recovery")
	}
	if _, err = os.Stat(filepath.Join(cache, "pending.json")); err != nil {
		t.Fatal("unconfirmed evidence deleted")
	}
}

func TestExpiredScientificConfirmedPendingOnlyResumesSameEvidence(t *testing.T) {
	s, l := leaseIdentityFixture(t, "scientific-smoke")
	s.Objects = &rollout.Store{Root: filepath.Join(s.Root, "objects")}
	if err := s.BeginUpload(l); err != nil {
		t.Fatal(err)
	}
	scientificRecoveryEvidence(t, s, l, true, true)
	s.Now = func() time.Time { return time.Now().Add(2 * time.Minute) }
	recovered, err := s.Recover()
	if err != nil || recovered.Attention != 0 || recovered.Rescheduled != 0 || recovered.UploadsPreserved != 1 {
		t.Fatalf("confirmed pending was interrupted: %+v %v", recovered, err)
	}
	count, err := s.ResumeUploads(context.Background())
	if err != nil || count != 1 {
		t.Fatal("confirmed evidence not uploaded", count, err)
	}
	var state string
	var attempt, ledger int
	if err = s.DB.QueryRow("SELECT state,attempt FROM runs WHERE id=?", l.RunID).Scan(&state, &attempt); err != nil {
		t.Fatal(err)
	}
	if err = s.DB.QueryRow("SELECT COUNT(*) FROM ledger WHERE batch_id=?", l.BatchID).Scan(&ledger); err != nil {
		t.Fatal(err)
	}
	if state != "completed" || attempt != 1 || ledger != 1 {
		t.Fatal("confirmed evidence reran or recharged", state, attempt, ledger)
	}
}

func TestScientificResumeBeforeRecoverCannotRenewUnconfirmedLease(t *testing.T) {
	s, l := leaseIdentityFixture(t, "scientific-smoke")
	s.Objects = &rollout.Store{Root: filepath.Join(s.Root, "objects")}
	if err := s.BeginUpload(l); err != nil {
		t.Fatal(err)
	}
	scientificRecoveryEvidence(t, s, l, true, false)
	s.Now = func() time.Time { return time.Now().Add(2 * time.Minute) }
	var before float64
	if err := s.DB.QueryRow("SELECT lease_until FROM runs WHERE id=?", l.RunID).Scan(&before); err != nil {
		t.Fatal(err)
	}
	// Match RunWorker's actual maintenance order, not just direct Recover calls.
	n, err := s.ResumeUploads(context.Background())
	if err != nil || n != 0 {
		t.Fatalf("maintenance published before cleanup: uploads=%d err=%v", n, err)
	}
	var after float64
	if err = s.DB.QueryRow("SELECT lease_until FROM runs WHERE id=?", l.RunID).Scan(&after); err != nil {
		t.Fatal(err)
	}
	if after != before {
		t.Fatal("unconfirmed upload extended lease and hid orphan from Recover")
	}
	recovered, err := s.Recover()
	if err != nil || recovered.Attention != 1 {
		t.Fatal("maintenance order still hides expired scientific upload", recovered, err)
	}
}
