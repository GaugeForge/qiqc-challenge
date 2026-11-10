package platform

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

// CheckScientificReconciliation refuses stale operator requests before any
// provider-side deletion. ReconcileScientificResources repeats this check in
// its transaction after the provider responds.
func (s *Store) CheckScientificReconciliation(runID string, attempt int) error {
	var state, why, mode string
	var current int
	if err := s.DB.QueryRow("SELECT r.state,r.attempt,r.error,b.mode FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.id=?", runID).Scan(&state, &current, &why, &mode); err != nil {
		return err
	}
	if attempt < 1 || state != "attention" || current != attempt || !IsScientificMode(mode) {
		return errors.New("run is not the unresolved scientific attempt")
	}
	if why == "platform:operator_review_required" {
		_, err := s.nativeStagedUploadFor(s.DB, runID, attempt)
		return err
	}
	if why != lostWorkerError {
		return errors.New("run is not the unresolved scientific attempt")
	}
	return nil
}

// A completed native rollout can survive an error at the publication boundary.
// Only an explicit operator reconciliation may resume those same bytes, after
// provider confirmation. A generic scientific/adapter failure is not eligible.
func (s *Store) nativeStagedUploadFor(q queryer, runID string, attempt int) (*Pending, error) {
	var candidate string
	if err := q.QueryRow("SELECT value FROM settings WHERE key='scientific_candidate'").Scan(&candidate); err != nil {
		return nil, errors.New("native staged recovery requires candidate binding")
	}
	var receipt struct {
		Phase string `json:"phase"`
		Error string `json:"controller_error_class"`
	}
	if err := readPrivateRecoveryJSON(filepath.Join(s.Root, "scientific", runID+"_"+strconv.Itoa(attempt)+".json"), &receipt); err != nil || receipt.Phase != "uploading_rollout" || receipt.Error != "" {
		return nil, errors.New("native controller did not finish scientific execution")
	}
	cache := filepath.Join(s.Root, "cache", runID, strconv.Itoa(attempt))
	var p Pending
	if err := readPrivateRecoveryJSON(filepath.Join(cache, "staged.json"), &p); err != nil || p.Lease.RunID != runID || p.Lease.Attempt != attempt || p.Provenance != p.Lease.Mode || !IsScientificMode(p.Lease.Mode) || !validDigest(p.SHA) || (p.Reward != 0 && p.Reward != 1) {
		return nil, errors.New("native staged recovery metadata invalid")
	}
	if _, err := MeanSeconds([]string{p.Seconds}); err != nil {
		return nil, err
	}
	if err := s.scientificCleanupConfirmedFor(q, p.Lease); err != nil {
		return nil, err
	}
	archive := filepath.Join(cache, "rollout.tar.gz")
	info, err := os.Lstat(archive)
	if err != nil || !info.Mode().IsRegular() || info.Size() > 128*1024*1024 {
		return nil, errors.New("native staged archive invalid")
	}
	digest, _, err := rollout.Digest(archive)
	if err != nil || digest != p.SHA {
		return nil, errors.New("native staged archive changed")
	}
	return &p, nil
}

// ReconcileScientificResources is a trusted operator-only operation. The caller
// first positively checks every sandbox with the provider and fsyncs the receipt.
// It is intentionally not exposed by participant or administrator HTTP routes.
func (s *Store) ReconcileScientificResources(runID string, attempt int, reason string) error {
	if reason == "" || attempt < 1 {
		return errors.New("reconciliation reason and attempt required")
	}
	if filepath.Base(runID) != runID || runID == "." || runID == ".." {
		return errors.New("invalid run id")
	}
	p := filepath.Join(s.Root, "scientific", runID+"_"+strconv.Itoa(attempt)+".json")
	raw, err := os.ReadFile(p)
	if err != nil {
		return err
	}
	if len(raw) > 65536 {
		return errors.New("oversized resource receipt")
	}
	var receipt struct {
		RunID              string            `json:"run_id"`
		Attempt            int               `json:"attempt"`
		Mode               string            `json:"mode"`
		Phase              string            `json:"phase"`
		ControllerError    string            `json:"controller_error_class"`
		ProviderChecked    bool              `json:"provider_checked"`
		Resources          map[string]string `json:"resources"`
		NoResourcesCreated *struct {
			Kind      string   `json:"kind"`
			Purpose   string   `json:"purpose"`
			States    []string `json:"states"`
			Matches   int      `json:"matches"`
			CheckedAt int64    `json:"checked_at"`
			ProofSHA  string   `json:"proof_sha256"`
		} `json:"no_resources_created"`
		PrecreateProof string `json:"precreate_proof"`
	}
	if json.Unmarshal(raw, &receipt) != nil || receipt.RunID != runID || receipt.Attempt != attempt || !IsScientificMode(receipt.Mode) || !receipt.ProviderChecked {
		return errors.New("unchecked or mismatched resource receipt")
	}
	if len(receipt.Resources) == 0 {
		check := receipt.NoResourcesCreated
		var proof struct {
			Schema     string `json:"schema"`
			RunID      string `json:"run_id"`
			Attempt    int    `json:"attempt"`
			ExitCode   int    `json:"exit_code"`
			StdinEmpty bool   `json:"stdin_empty"`
			CloudCalls *bool  `json:"cloud_calls"`
			BeforeMain bool   `json:"failure_before_main"`
		}
		digest := sha256.Sum256([]byte(receipt.PrecreateProof))
		if check == nil || check.Purpose != "qiqc-native-candidate" || len(check.States) != 2 || check.States[0] != "running" || check.States[1] != "paused" || check.Matches != 0 || float64(check.CheckedAt) < s.now()-300 || float64(check.CheckedAt) > s.now()+60 {
			return errors.New("zero resources requires fresh provider and startup evidence")
		}
		if check.Kind == "native_startup_failure_no_resources" {
			if receipt.Phase != "starting" || receipt.ControllerError == "" || receipt.PrecreateProof != "" {
				return errors.New("invalid no-resource startup evidence")
			}
		} else if check.Kind != "native_precreate_import_failure" || check.ProofSHA != hex.EncodeToString(digest[:]) || json.Unmarshal([]byte(receipt.PrecreateProof), &proof) != nil || proof.Schema != "qiqc-native-startup-import-diagnostic/v1" || proof.RunID != runID || proof.Attempt != attempt || proof.ExitCode == 0 || !proof.StdinEmpty || proof.CloudCalls == nil || *proof.CloudCalls || !proof.BeforeMain {
			return errors.New("zero resources requires fresh provider and precreate failure evidence")
		}
	} else if receipt.NoResourcesCreated != nil {
		return errors.New("precreate proof conflicts with created resources")
	}
	for id, state := range receipt.Resources {
		if id == "" || state != "destroyed" {
			return errors.New("resources not positively destroyed")
		}
	}
	var staged *Pending
	stagedPath := filepath.Join(s.Root, "cache", runID, strconv.Itoa(attempt), "staged.json")
	if data, err := os.ReadFile(stagedPath); err == nil {
		var p Pending
		if len(data) > 65536 || json.Unmarshal(data, &p) != nil || p.Lease.RunID != runID || p.Lease.Attempt != attempt || p.Provenance != p.Lease.Mode || !IsScientificMode(p.Lease.Mode) {
			return errors.New("staged scientific evidence mismatch")
		}
		staged = &p
	} else if !os.IsNotExist(err) {
		return err
	}
	if staged != nil && receipt.NoResourcesCreated != nil {
		return errors.New("precreate failure conflicts with staged scientific output")
	}
	err = s.write(func(tx *sql.Tx) error {
		var batch, state, mode, why, bstate string
		var current int
		if err := tx.QueryRow("SELECT r.batch_id,r.state,r.attempt,r.error,b.mode,b.status FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.id=?", runID).Scan(&batch, &state, &current, &why, &mode, &bstate); err != nil {
			return err
		}
		if state != "attention" || current != attempt || !IsScientificMode(mode) || mode != receipt.Mode {
			return errors.New("run is not the unresolved scientific attempt")
		}
		if why == "platform:operator_review_required" {
			checked, err := s.nativeStagedUploadFor(tx, runID, attempt)
			if err != nil || staged == nil || *checked != *staged {
				return errors.New("native staged publication recovery not verified")
			}
		} else if why != lostWorkerError {
			return errors.New("run is not the unresolved scientific attempt")
		}
		if staged != nil {
			p := staged
			if _, err := tx.Exec("UPDATE runs SET state='uploading',error=NULL,owner=?,lease_until=? WHERE id=?", p.Lease.Owner, s.now()+leaseSeconds, runID); err != nil {
				return err
			}
			if _, err := tx.Exec("UPDATE attempts SET state='running',finished=NULL,error=NULL WHERE run_id=? AND number=?", runID, attempt); err != nil {
				return err
			}
			if bstate != "cancelling" {
				if _, err := tx.Exec("UPDATE batches SET status='testing' WHERE id=?", batch); err != nil {
					return err
				}
			}
			if _, err := tx.Exec("INSERT INTO audit(id,created,actor,action,object_id,reason) VALUES(?,?,'trusted-resource-reconciler','reconcile_upload',?,?)", id(), s.now(), runID, reason); err != nil {
				return err
			}
			return nil
		}
		next := "attention"
		if bstate == "cancelling" {
			next = "cancelled"
		}
		if _, err := tx.Exec("UPDATE runs SET state=?,error='platform:reconciled',owner=NULL,lease_until=NULL WHERE id=?", next, runID); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE attempts SET state='interrupted',finished=?,error='platform:reconciled' WHERE run_id=? AND number=?", s.now(), runID, attempt); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", runID); err != nil {
			return err
		}
		after, _ := json.Marshal(map[string]any{"reason": reason, "receipt": filepath.Base(p), "attempt": attempt, "resource_count": len(receipt.Resources)})
		if _, err := tx.Exec("INSERT INTO audit(id,created,actor,action,object_id,reason,after_json) VALUES(?,?,'trusted-resource-reconciler','reconcile_resources',?,?,?)", id(), s.now(), runID, reason, string(after)); err != nil {
			return err
		}
		return s.finalize(tx, batch)
	})
	if err != nil {
		return err
	}
	if staged != nil {
		return s.PromoteScientificPending(staged.Lease)
	}
	return nil
}
