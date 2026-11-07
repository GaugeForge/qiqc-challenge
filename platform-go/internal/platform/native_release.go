package platform

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"regexp"
)

type nativeBinding struct {
	ReleaseID   string `json:"release_id"`
	CandidateID string `json:"candidate_id"`
	ManifestSHA string `json:"release_sha256"`
}

// ActivateNativeRelease is an offline operator command. The caller verifies the
// complete release evidence and stops admission before this atomic DB transition.
// Serving processes separately require the same root-controlled pointer.
func (s *Store) ActivateNativeRelease(release, candidate, manifest, actor, reason string) error {
	if !regexp.MustCompile(`^native-release-[a-f0-9]{64}$`).MatchString(release) || !regexp.MustCompile(`^native-candidate-[a-f0-9]{64}$`).MatchString(candidate) || !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(manifest) || actor == "" || reason == "" {
		return errors.New("invalid release activation")
	}
	return s.write(func(tx *sql.Tx) error {
		var actual string
		if err := tx.QueryRow("SELECT value FROM settings WHERE key='scientific_candidate'").Scan(&actual); err != nil {
			return err
		}
		if actual != candidate {
			return errors.New("release candidate mismatch")
		}
		var active int
		if err := tx.QueryRow("SELECT count(*) FROM runs WHERE state IN ('queued','running','uploading') OR (state='attention' AND error=?)", lostWorkerError).Scan(&active); err != nil {
			return err
		}
		if active != 0 {
			return errors.New("release activation requires drained execution")
		}
		binding := nativeBinding{release, candidate, manifest}
		raw, _ := json.Marshal(binding)
		var old string
		err := tx.QueryRow("SELECT value FROM settings WHERE key=?", "native_release:"+release).Scan(&old)
		if err == nil && old != string(raw) {
			return errors.New("registered release immutable")
		}
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if _, err = tx.Exec("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", "native_release:"+release, string(raw)); err != nil {
			return err
		}
		var previous string
		err = tx.QueryRow("SELECT value FROM settings WHERE key='scientific_active_release'").Scan(&previous)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if previous == release {
			return nil
		}
		if _, err = tx.Exec("INSERT INTO settings(key,value) VALUES('scientific_active_release',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", release); err != nil {
			return err
		}
		before, _ := json.Marshal(map[string]string{"release_id": previous})
		_, err = tx.Exec("INSERT INTO audit(id,created,actor,action,object_id,reason,before_json,after_json) VALUES(?,?,?,'native_release_activated',?,?,?,?)", id(), s.now(), actor, release, reason, string(before), string(raw))
		return err
	})
}

func checkNativePointer(q queryer, release, candidate string) error {
	path := os.Getenv("QIQC_NATIVE_RELEASE_POINTER")
	if path == "" {
		if release != candidate {
			return errors.New("native release pointer not configured")
		}
		return nil
	}
	pointerInfo, pointerErr := os.Lstat(path)
	if os.IsNotExist(pointerErr) && release == candidate {
		return nil
	}
	if pointerErr != nil || !pointerInfo.Mode().IsRegular() || pointerInfo.Size() > 16384 {
		return errors.New("native release pointer unavailable")
	}
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) && release == candidate {
		return nil
	}
	if err != nil {
		return errors.New("native release pointer unavailable")
	}
	if len(raw) > 16384 {
		return errors.New("native pointer oversized")
	}
	var pointer struct {
		ReleaseID   string `json:"release_id"`
		ReleasePath string `json:"release_path"`
		ManifestSHA string `json:"release_sha256"`
	}
	if json.Unmarshal(raw, &pointer) != nil || pointer.ReleaseID != release {
		return errors.New("native pointer and database disagree")
	}
	var registered string
	if err = q.QueryRow("SELECT value FROM settings WHERE key=?", "native_release:"+release).Scan(&registered); err != nil {
		return err
	}
	var binding nativeBinding
	if json.Unmarshal([]byte(registered), &binding) != nil || binding.CandidateID != candidate || binding.ManifestSHA != pointer.ManifestSHA {
		return errors.New("native pointer binding mismatch")
	}
	info, err := os.Lstat(pointer.ReleasePath)
	if err != nil || !info.Mode().IsRegular() || info.Size() > 32*1024*1024 {
		return errors.New("native release record unavailable")
	}
	record, err := os.ReadFile(pointer.ReleasePath)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(record)
	if hex.EncodeToString(sum[:]) != binding.ManifestSHA {
		return errors.New("native release record changed")
	}
	return nil
}

func validHistoricalRelease(q queryer, release string) bool {
	var candidate string
	err := q.QueryRow("SELECT value FROM settings WHERE key='scientific_candidate'").Scan(&candidate)
	if errors.Is(err, sql.ErrNoRows) {
		return release == ScientificReleaseID
	}
	if err != nil {
		return false
	}
	if release == candidate || registeredHistoricalCandidate(q, release) {
		return true
	}
	var raw string
	if q.QueryRow("SELECT value FROM settings WHERE key=?", "native_release:"+release).Scan(&raw) != nil {
		return false
	}
	var binding nativeBinding
	return json.Unmarshal([]byte(raw), &binding) == nil && binding.ReleaseID == release && (binding.CandidateID == candidate || registeredHistoricalCandidate(q, binding.CandidateID))
}

// RollbackNativeRelease restores admission only. All release registrations,
// submissions, attempts, scores and audits survive the rollback.
func (s *Store) RollbackNativeRelease(release, candidate, actor, reason string) error {
	if actor == "" || reason == "" || !regexp.MustCompile(`^native-candidate-[a-f0-9]{64}$`).MatchString(candidate) {
		return errors.New("invalid rollback")
	}
	return s.write(func(tx *sql.Tx) error {
		var actual string
		if err := tx.QueryRow("SELECT value FROM settings WHERE key='scientific_candidate'").Scan(&actual); err != nil {
			return err
		}
		if actual != candidate {
			return errors.New("rollback candidate mismatch")
		}
		var active int
		if err := tx.QueryRow("SELECT count(*) FROM runs WHERE state IN ('queued','running','uploading') OR (state='attention' AND error=?)", lostWorkerError).Scan(&active); err != nil {
			return err
		}
		if active != 0 {
			return errors.New("rollback requires drained execution")
		}
		if release != "" && (!regexp.MustCompile(`^native-release-[a-f0-9]{64}$`).MatchString(release) || !validHistoricalRelease(tx, release)) {
			return errors.New("rollback release not registered")
		}
		var previous string
		err := tx.QueryRow("SELECT value FROM settings WHERE key='scientific_active_release'").Scan(&previous)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if release == "" {
			_, err = tx.Exec("DELETE FROM settings WHERE key='scientific_active_release'")
		} else {
			_, err = tx.Exec("INSERT INTO settings(key,value) VALUES('scientific_active_release',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", release)
		}
		if err != nil {
			return err
		}
		before, _ := json.Marshal(map[string]string{"release_id": previous})
		after, _ := json.Marshal(map[string]string{"release_id": release, "candidate_id": candidate})
		_, err = tx.Exec("INSERT INTO audit(id,created,actor,action,object_id,reason,before_json,after_json) VALUES(?,?,?,'native_release_rollback',?,?,?,?)", id(), s.now(), actor, candidate, reason, string(before), string(after))
		return err
	})
}
