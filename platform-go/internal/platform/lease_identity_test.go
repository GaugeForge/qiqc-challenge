package platform

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

func leaseIdentityFixture(t *testing.T, mode string) (*Store, Lease) {
	t.Helper()
	s := testStore(t)
	token, version := approved(t, s)
	s.ScienceHashes = map[string]bool{version.SHA256: true}
	if _, err := s.Confirm(token, version.ID, "infra", mode, "lease-identity"); err != nil {
		t.Fatal(err)
	}
	lease, err := s.Claim("identity-test-controller", 1)
	if err != nil || lease == nil {
		t.Fatal("lease acquisition failed", err)
	}
	return s, *lease
}

func semanticLeaseMutations() []struct {
	name   string
	mutate func(*Lease)
} {
	return []struct {
		name   string
		mutate func(*Lease)
	}{
		{"mode", func(l *Lease) {
			if l.Mode == "simulated" {
				l.Mode = "scientific-smoke"
			} else {
				l.Mode = "simulated"
			}
		}},
		{"kind", func(l *Lease) { l.Kind = "public" }},
		{"ordinal", func(l *Lease) { l.Ordinal++ }},
		{"public", func(l *Lease) { l.Public = 1 - l.Public }},
	}
}

func TestScientificLeaseCannotUseFixtureCompletionByForgingMode(t *testing.T) {
	s, lease := leaseIdentityFixture(t, "scientific-smoke")
	s.Objects = &rollout.Store{Root: filepath.Join(s.Root, "objects")}
	forged := lease
	forged.Mode = "simulated"
	if err := s.CompleteFixture(context.Background(), forged, 1, "1.0", "local fixture must not become scientific evidence"); status(err) != 409 {
		t.Fatalf("scientific lease accepted by fixture completion after Mode forgery: status=%d", status(err))
	}
	if err := s.ValidateLease(lease); err != nil {
		t.Fatal("rejected forgery changed original lease", err)
	}
	var state string
	if err := s.DB.QueryRow("SELECT state FROM runs WHERE id=?", lease.RunID).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != "running" {
		t.Fatalf("forgery changed scientific state to %s", state)
	}
}

func TestValidateLeaseBindsModeKindOrdinalAndPublic(t *testing.T) {
	for _, mode := range []string{"simulated", "scientific-smoke"} {
		t.Run(mode, func(t *testing.T) {
			s, lease := leaseIdentityFixture(t, mode)
			for _, mutation := range semanticLeaseMutations() {
				t.Run(mutation.name, func(t *testing.T) {
					forged := lease
					mutation.mutate(&forged)
					if err := s.ValidateLease(forged); status(err) != 409 {
						t.Errorf("mutated %s accepted: status=%d", mutation.name, status(err))
					}
					if err := s.ValidateLease(lease); err != nil {
						t.Fatal("original lease no longer valid", err)
					}
				})
			}
		})
	}
}

func TestResumeUploadLeaseBindsModeKindOrdinalAndPublic(t *testing.T) {
	for _, mode := range []string{"simulated", "scientific-smoke"} {
		t.Run(mode, func(t *testing.T) {
			s, lease := leaseIdentityFixture(t, mode)
			// Only establish the state-machine boundary; no sandbox or science trial.
			if err := s.BeginUpload(lease); err != nil {
				t.Fatal(err)
			}
			if mode == "scientific-smoke" {
				scientificRecoveryReceipt(t, s, lease, true)
			}
			for _, mutation := range semanticLeaseMutations() {
				t.Run(mutation.name, func(t *testing.T) {
					forged := lease
					mutation.mutate(&forged)
					if err := s.ResumeUploadLease(forged); status(err) != 409 {
						t.Errorf("mutated %s accepted by upload recovery: status=%d", mutation.name, status(err))
					}
					if err := s.ValidateLease(lease); err != nil {
						t.Fatal("original lease no longer valid", err)
					}
				})
			}
			if err := s.ResumeUploadLease(lease); err != nil {
				t.Fatal("valid upload recovery refused", err)
			}
		})
	}
}
