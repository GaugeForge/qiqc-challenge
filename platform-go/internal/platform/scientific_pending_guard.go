package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
)

// readPrivateRecoveryJSON refuses links and oversized/private-recovery sidecars.
// These files are organizer-controlled evidence, never participant inputs.
func readPrivateRecoveryJSON(path string, into any) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() > 65536 {
		return errors.New("invalid recovery metadata file")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if len(raw) > 65536 {
		return errors.New("oversized recovery metadata")
	}
	return json.Unmarshal(raw, into)
}

// scientificCleanupConfirmed is shared by publication and upload recovery. A
// pending filename or expired lease cannot replace positive controller evidence
// that every resource in this attempt's registered topology was destroyed.
func (s *Store) scientificCleanupConfirmed(l Lease) error {
	return s.scientificCleanupConfirmedFor(s.DB, l)
}

// Recovery also calls this inside its transaction. Use that same queryer rather
// than trying to acquire a second database connection while the writer is held.
func (s *Store) scientificCleanupConfirmedFor(q queryer, l Lease) error {
	denied := func() error { return problem(409, "Scientific sandbox cleanup is not confirmed") }
	if !IsScientificMode(l.Mode) || l.Attempt < 1 || l.RunID == "" || l.RunID == "." || l.RunID == ".." || filepath.Base(l.RunID) != l.RunID {
		return denied()
	}
	var receipt struct {
		RunID     string            `json:"run_id"`
		Attempt   int               `json:"attempt"`
		Mode      string            `json:"mode"`
		Resources map[string]string `json:"resources"`
	}
	path := filepath.Join(s.Root, "scientific", l.RunID+"_"+strconv.Itoa(l.Attempt)+".json")
	if readPrivateRecoveryJSON(path, &receipt) != nil || receipt.RunID != l.RunID || receipt.Attempt != l.Attempt || receipt.Mode != l.Mode {
		return denied()
	}
	if l.Mode == "scientific" {
		var actual Lease
		err := q.QueryRow(`SELECT r.id,r.batch_id,r.ordinal,r.public,r.attempt,b.team_id,b.version_id,b.kind,b.mode,v.sha256
		FROM runs r JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id
		WHERE r.id=? AND r.attempt=?`, l.RunID, l.Attempt).Scan(&actual.RunID, &actual.BatchID, &actual.Ordinal, &actual.Public, &actual.Attempt, &actual.TeamID, &actual.VersionID, &actual.Kind, &actual.Mode, &actual.SHA256)
		if err != nil || actual.Mode != l.Mode || (l.BatchID != "" && actual.BatchID != l.BatchID) || (l.SHA256 != "" && actual.SHA256 != l.SHA256) {
			return denied()
		}
		if _, err := readRunScientificTarget(q, actual); err != nil {
			return denied()
		}
		if len(receipt.Resources) != 1 {
			return denied()
		}
		for id, state := range receipt.Resources {
			if id == "" || state != "destroyed" {
				return denied()
			}
		}
		return nil
	}
	expected := 3 // Historical execution uses one E2B VM per role.
	var candidate string
	err := q.QueryRow("SELECT value FROM settings WHERE key='scientific_candidate'").Scan(&candidate)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return denied()
	}
	if err == nil {
		if !regexp.MustCompile(`^native-candidate-[a-f0-9]{64}$`).MatchString(candidate) {
			return denied()
		}
		var batch, sha string
		// Native execution owns one VM containing the three Docker roles. Its
		// immutable batch binding, not a receipt label or filename, selects this
		// topology. Missing/foreign/stale bindings must never fall back to it.
		err = q.QueryRow(`SELECT r.batch_id,v.sha256 FROM runs r
		 JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id
		 WHERE r.id=? AND r.attempt=? AND r.public=0 AND r.ordinal=0
		 AND b.mode='scientific-smoke' AND b.kind='infra'`, l.RunID, l.Attempt).Scan(&batch, &sha)
		if err != nil || (l.BatchID != "" && l.BatchID != batch) || (l.SHA256 != "" && l.SHA256 != sha) {
			return denied()
		}
		target, err := readScientificTarget(q, batch)
		if err != nil || target == nil || target.SubmissionSHA != sha {
			return denied()
		}
		expected = 1
	}
	if len(receipt.Resources) != expected {
		return denied()
	}
	for id, state := range receipt.Resources {
		if id == "" || state != "destroyed" {
			return denied()
		}
	}
	return nil
}
