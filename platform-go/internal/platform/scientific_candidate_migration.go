package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"regexp"
)

// MigrateScientificCandidate is an offline organizer operation. It admits only
// future batches under a new roster identity; immutable run/catalog evidence
// continues to name the candidate under which it was actually executed.
func (s *Store) MigrateScientificCandidate(previous, next, actor, reason string) error {
	valid := regexp.MustCompile(`^native-candidate-[a-f0-9]{64}$`)
	if !valid.MatchString(previous) || !valid.MatchString(next) || previous == next || actor == "" || reason == "" {
		return errors.New("invalid candidate migration")
	}
	return s.write(func(tx *sql.Tx) error {
		var actual string
		if err := tx.QueryRow("SELECT value FROM settings WHERE key='scientific_candidate'").Scan(&actual); err != nil {
			return err
		}
		// A restart between DB commit and atomic config rename may retry the same
		// migration. Accept only the exact already-audited transition.
		if actual == next && registeredHistoricalCandidate(tx, previous) {
			before, _ := json.Marshal(map[string]string{"candidate_id": previous})
			after, _ := json.Marshal(map[string]string{"candidate_id": next})
			var count int
			if err := tx.QueryRow("SELECT count(*) FROM audit WHERE action='native_candidate_migrated' AND object_id=? AND before_json=? AND after_json=?", next, string(before), string(after)).Scan(&count); err != nil {
				return err
			}
			if count > 0 {
				return nil
			}
		}
		if actual != previous {
			return errors.New("candidate migration previous identity mismatch")
		}
		var active int
		if err := tx.QueryRow("SELECT count(*) FROM runs WHERE state IN ('queued','running','uploading') OR (state='attention' AND error=?)", lostWorkerError).Scan(&active); err != nil {
			return err
		}
		if active != 0 {
			return errors.New("candidate migration requires drained execution")
		}
		if err := tx.QueryRow("SELECT count(*) FROM settings WHERE key='scientific_active_release'").Scan(&active); err != nil {
			return err
		}
		if active != 0 {
			return errors.New("frozen release cannot migrate candidate")
		}
		if _, err := tx.Exec("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING", "native_historical_candidate:"+previous, previous); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE settings SET value=? WHERE key='scientific_candidate'", next); err != nil {
			return err
		}
		// Fail closed between migration and startup with the new matching catalog.
		if _, err := tx.Exec("DELETE FROM settings WHERE key='scientific_task_set'"); err != nil {
			return err
		}
		before, _ := json.Marshal(map[string]string{"candidate_id": previous})
		after, _ := json.Marshal(map[string]string{"candidate_id": next})
		_, err := tx.Exec("INSERT INTO audit(id,created,actor,action,object_id,reason,before_json,after_json) VALUES(?,?,?,'native_candidate_migrated',?,?,?,?)", id(), s.now(), actor, next, reason, string(before), string(after))
		return err
	})
}

func registeredHistoricalCandidate(q queryer, candidate string) bool {
	if !regexp.MustCompile(`^native-candidate-[a-f0-9]{64}$`).MatchString(candidate) {
		return false
	}
	var actual string
	return q.QueryRow("SELECT value FROM settings WHERE key=?", "native_historical_candidate:"+candidate).Scan(&actual) == nil && actual == candidate
}
