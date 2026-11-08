package platform

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

// FinalizeScientificFailureArtifacts closes a controller attempt once its
// private failure archive has been durably indexed and the receipt proves that
// every remote resource was destroyed. This is deliberately a terminal
// attention transition: it never retries the Harness or publishes a score.
// It covers the small window where the controller has finished archiving but
// the worker process has not yet returned from its executor call.
func (s *Store) FinalizeScientificFailureArtifacts() (int, error) {
	rows, err := s.DB.Query(`SELECT r.id,r.batch_id,r.attempt,b.mode
FROM runs r JOIN batches b ON b.id=r.batch_id
WHERE r.state IN ('running','uploading') AND b.mode IN ('scientific','scientific-smoke')
  AND EXISTS (SELECT 1 FROM run_artifacts a WHERE a.run_id=r.id AND a.attempt=r.attempt AND a.kind='failed-rollout')`)
	if err != nil {
		return 0, err
	}
	type candidate struct {
		run, batch, mode string
		attempt          int
	}
	var pending []candidate
	for rows.Next() {
		var c candidate
		if err := rows.Scan(&c.run, &c.batch, &c.attempt, &c.mode); err != nil {
			rows.Close()
			return 0, err
		}
		pending = append(pending, c)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, err
	}
	rows.Close()
	count := 0
	for _, c := range pending {
		path := filepath.Join(s.Root, "scientific", c.run+"_"+strconv.Itoa(c.attempt)+".json")
		raw, err := os.ReadFile(path)
		if err != nil || len(raw) > 65536 {
			continue
		}
		var receipt struct {
			RunID     string            `json:"run_id"`
			Phase     string            `json:"phase"`
			Resources map[string]string `json:"resources"`
			Error     string            `json:"controller_error_class"`
			Detail    string            `json:"controller_error_detail"`
		}
		if json.Unmarshal(raw, &receipt) != nil || receipt.RunID != c.run || receipt.Error == "" || len(receipt.Resources) == 0 {
			continue
		}
		diagnostic := DiagnosticFromReceipt(map[string]any{
			"phase":                   receipt.Phase,
			"controller_error_class":  receipt.Error,
			"controller_error_detail": receipt.Detail,
		})
		allDestroyed := true
		for _, state := range receipt.Resources {
			if state != "destroyed" {
				allDestroyed = false
				break
			}
		}
		if !allDestroyed {
			continue
		}
		err = s.write(func(tx *sql.Tx) error {
			var state string
			if err := tx.QueryRow("SELECT state FROM runs WHERE id=? AND attempt=?", c.run, c.attempt).Scan(&state); err != nil {
				return err
			}
			if state != "running" && state != "uploading" {
				return nil
			}
			if _, err := tx.Exec("UPDATE runs SET state='attention',error=?,failure_category=?,failure_phase=?,failure_code=?,owner=NULL,lease_until=NULL WHERE id=? AND attempt=?", diagnostic.Category+":operator_review_required", diagnostic.Category, diagnostic.Phase, diagnostic.Code, c.run, c.attempt); err != nil {
				return err
			}
			if _, err := tx.Exec("UPDATE attempts SET state='failed',finished=?,error=? WHERE run_id=? AND number=?", s.now(), diagnostic.Category, c.run, c.attempt); err != nil {
				return err
			}
			if _, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=? AND attempt=?", c.run, c.attempt); err != nil {
				return err
			}
			_, err := tx.Exec("UPDATE batches SET status='attention' WHERE id=? AND status!='cancelling'", c.batch)
			return err
		})
		if err != nil {
			return count, err
		}
		count++
	}
	return count, nil
}

// FailedScientificEvidence describes a private failed-attempt archive. It is
// deliberately independent of score completion and sandbox/lease reconciliation.
type FailedScientificEvidence struct {
	Lease      Lease  `json:"lease"`
	SHA        string `json:"sha256"`
	Format     string `json:"format"`
	ErrorClass string `json:"error_class"`
}

func (s *Store) initFailureArtifacts() error {
	_, err := s.DB.Exec(`CREATE TABLE IF NOT EXISTS run_artifacts (
 run_id TEXT NOT NULL REFERENCES runs(id), attempt INTEGER NOT NULL,
 kind TEXT NOT NULL, object_key TEXT NOT NULL, object_sha TEXT NOT NULL,
 format TEXT NOT NULL, error_class TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(run_id,attempt,kind))`)
	return err
}

// ResumeScientificFailureUploads retries only bytes already persisted by the
// trusted controller. It never runs a solver, publishes a score, refunds quota,
// releases a slot or exposes failed/hidden transcripts to participants.
func (s *Store) ResumeScientificFailureUploads(ctx context.Context) (int, error) {
	if err := s.initFailureArtifacts(); err != nil {
		return 0, err
	}
	paths, err := filepath.Glob(filepath.Join(s.Root, "failure-evidence", "*", "*", "pending.json"))
	if err != nil {
		return 0, err
	}
	count := 0
	var first error
	for _, p := range paths {
		if err := s.uploadScientificFailure(ctx, p); err != nil {
			if first == nil {
				first = err
			}
			continue
		}
		count++
	}
	return count, first
}

func (s *Store) uploadScientificFailure(ctx context.Context, pendingPath string) error {
	raw, err := os.ReadFile(pendingPath)
	if err != nil {
		return err
	}
	var p FailedScientificEvidence
	if len(raw) > 65536 || json.Unmarshal(raw, &p) != nil || !IsScientificMode(p.Lease.Mode) || p.Lease.Attempt < 1 || len(p.SHA) != 64 || p.ErrorClass == "" || len(p.ErrorClass) > 100 || (p.Format != "harbor-trial-0.23" && p.Format != "controller-diagnostic") {
		return errors.New("invalid failed scientific artifact receipt")
	}
	expected := filepath.Join(s.Root, "failure-evidence", p.Lease.RunID, strconv.Itoa(p.Lease.Attempt), "pending.json")
	if filepath.Clean(expected) != filepath.Clean(pendingPath) {
		return errors.New("failure receipt path mismatch")
	}
	var mode, sha string
	var attempt int
	if err = s.DB.QueryRow(`SELECT b.mode,v.sha256,r.attempt FROM runs r JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id WHERE r.id=? AND b.id=? AND b.team_id=?`, p.Lease.RunID, p.Lease.BatchID, p.Lease.TeamID).Scan(&mode, &sha, &attempt); err != nil {
		return err
	}
	if !IsScientificMode(mode) || mode != p.Lease.Mode || sha != p.Lease.SHA256 || p.Lease.Attempt > attempt {
		return errors.New("failure artifact does not match accepted run")
	}
	var recorded int
	if err = s.DB.QueryRow("SELECT COUNT(*) FROM attempts WHERE run_id=? AND number=?", p.Lease.RunID, p.Lease.Attempt).Scan(&recorded); err != nil || recorded != 1 {
		return errors.New("failure attempt does not exist")
	}
	file := filepath.Join(filepath.Dir(pendingPath), "rollout.tar.gz")
	got, n, err := rollout.Digest(file)
	if err != nil {
		return err
	}
	if got != p.SHA || n > 100*1024*1024 {
		return errors.New("failure artifact integrity or size mismatch")
	}
	if s.Objects == nil {
		return errors.New("failure artifact store unavailable")
	}
	key := fmt.Sprintf("scientific-failures/%s/%s/%d/%s.tar.gz", p.Lease.TeamID, p.Lease.RunID, p.Lease.Attempt, p.SHA)
	info, err := s.Objects.PutFile(ctx, key, file)
	if err != nil {
		return err
	}
	if info.SHA256 != p.SHA {
		return errors.New("failure object digest mismatch")
	}
	if err = s.Objects.WithFile(ctx, key, p.SHA, func(string) error { return nil }); err != nil {
		return err
	}
	_, err = s.DB.Exec(`INSERT INTO run_artifacts(run_id,attempt,kind,object_key,object_sha,format,error_class,created) VALUES(?,?,'failed-rollout',?,?,?,?,?) ON CONFLICT(run_id,attempt,kind) DO NOTHING`, p.Lease.RunID, p.Lease.Attempt, key, p.SHA, p.Format, p.ErrorClass, s.now())
	if err != nil {
		return err
	}
	var stored string
	if err = s.DB.QueryRow("SELECT object_sha FROM run_artifacts WHERE run_id=? AND attempt=? AND kind='failed-rollout'", p.Lease.RunID, p.Lease.Attempt).Scan(&stored); err != nil {
		return err
	}
	if stored != p.SHA {
		return errors.New("failed artifact index collision")
	}
	// Drop pending first; a crash may leave a redundant private archive, never a
	// pending receipt whose sole copy vanished. Operators can clean indexed files.
	if err = os.Remove(pendingPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err = os.Remove(file); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
