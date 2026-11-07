package platform

import (
	"database/sql"
	"errors"
	"regexp"
)

// The index is the lexically sorted task ID in this frozen release. This is
// organizer-only acceptance metadata, never a participant task selector.
const ScientificReleaseID = "qiqc49-20261020-v1"

func ValidScientificAgentBudget(seconds int) bool { return seconds == 180 || seconds == 300 }

// ConfigureScientificAgentBudget is startup-only operator configuration. The
// value is frozen into each target; request bodies cannot override it.
func (s *Store) ConfigureScientificAgentBudget(seconds int) error {
	if !ValidScientificAgentBudget(seconds) {
		return errors.New("invalid scientific agent budget")
	}
	s.scientificAgentBudgetSeconds = seconds
	return nil
}

func (s *Store) ScientificAgentBudget() int {
	if s.scientificAgentBudgetSeconds == 0 {
		return 300
	}
	return s.scientificAgentBudgetSeconds
}

// ConfigureScientificCandidate is operator-only initialization of an empty,
// separate database. It cannot retarget an existing competition or account.
func (s *Store) ConfigureScientificCandidate(id string) error {
	if !regexp.MustCompile(`^native-candidate-[a-f0-9]{64}$`).MatchString(id) {
		return errors.New("invalid candidate source identity")
	}
	return s.write(func(tx *sql.Tx) error {
		var existing string
		err := tx.QueryRow("SELECT value FROM settings WHERE key='scientific_candidate'").Scan(&existing)
		if err == nil {
			if existing == id {
				return nil
			}
			return errors.New("candidate database identity immutable")
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		for _, table := range []string{"users", "versions", "batches"} {
			var count int
			if err := tx.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil {
				return err
			}
			if count != 0 {
				return errors.New("candidate requires empty independent database")
			}
		}
		_, err = tx.Exec("INSERT INTO settings(key,value) VALUES('scientific_candidate',?)", id)
		return err
	})
}

func scientificRelease(q queryer) (string, error) {
	var id string
	err := q.QueryRow("SELECT value FROM settings WHERE key='scientific_candidate'").Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return ScientificReleaseID, nil
	}
	if err != nil {
		return "", err
	}
	if !regexp.MustCompile(`^native-candidate-[a-f0-9]{64}$`).MatchString(id) {
		return "", errors.New("invalid candidate database identity")
	}
	candidate := id
	err = q.QueryRow("SELECT value FROM settings WHERE key='scientific_active_release'").Scan(&id)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return "", err
	}
	if errors.Is(err, sql.ErrNoRows) {
		id = candidate
	}
	if !validHistoricalRelease(q, id) {
		return "", errors.New("unregistered active release")
	}
	if err := checkNativePointer(q, id, candidate); err != nil {
		return "", err
	}
	return id, nil
}

type ScientificTarget struct {
	Kind               string     `json:"kind,omitempty"`
	Limits             TaskLimits `json:"limits,omitempty"`
	Formal             bool       `json:"formal,omitempty"`
	TaskID             string     `json:"task_id,omitempty"`
	CatalogSHA         string     `json:"catalog_sha256,omitempty"`
	TaskIndex          int        `json:"task_index"`
	ReleaseID          string     `json:"release_id"`
	AgentBudgetSeconds int        `json:"agent_budget_seconds"`
	SubmissionSHA      string     `json:"submission_sha256"`
}

func (s *Store) bindScientificTarget(tx *sql.Tx, version string, index *int) (*ScientificTarget, *ScientificTarget, error) {
	if index == nil || *index < 0 || *index >= 49 {
		return nil, nil, problem(400, "验收编号必须在 1–49 范围内")
	}
	var sha, state string
	if err := tx.QueryRow("SELECT sha256,status FROM versions WHERE id=?", version).Scan(&sha, &state); errors.Is(err, sql.ErrNoRows) {
		return nil, nil, problem(404, "版本不存在")
	} else if err != nil {
		return nil, nil, err
	}
	if state != "approved" || !s.ScienceHashes[sha] {
		return nil, nil, problem(409, "仅可绑定已审查的官方科学模板")
	}
	if err := s.CheckVersion(sha); err != nil {
		return nil, nil, err
	}
	release, err := scientificRelease(tx)
	if err != nil {
		return nil, nil, err
	}
	budget := s.ScientificAgentBudget()
	want := &ScientificTarget{TaskIndex: *index, ReleaseID: release, AgentBudgetSeconds: budget, SubmissionSHA: sha}
	old := &ScientificTarget{}
	err = tx.QueryRow("SELECT task_index,release_id,agent_budget_seconds,submission_sha256 FROM scientific_targets WHERE version_id=?", version).Scan(&old.TaskIndex, &old.ReleaseID, &old.AgentBudgetSeconds, &old.SubmissionSHA)
	if err == nil {
		if *old != *want {
			return nil, nil, problem(409, "此版本已冻结验收目标；请使用新的提交版本")
		}
		return old, old, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, nil, err
	}
	var accepted int
	if err = tx.QueryRow("SELECT COUNT(*) FROM batches WHERE version_id=?", version).Scan(&accepted); err != nil {
		return nil, nil, err
	}
	if accepted != 0 {
		return nil, nil, problem(409, "此版本已有评测，不能改变任务绑定")
	}
	_, err = tx.Exec("INSERT INTO scientific_targets VALUES(?,?,?,?,?,?)", version, sha, *index, release, budget, s.now())
	return nil, want, err
}

func readScientificTarget(q queryer, batchID string) (*ScientificTarget, error) {
	t := &ScientificTarget{}
	err := q.QueryRow("SELECT task_index,release_id,agent_budget_seconds,submission_sha256 FROM batch_scientific_targets WHERE batch_id=?", batchID).Scan(&t.TaskIndex, &t.ReleaseID, &t.AgentBudgetSeconds, &t.SubmissionSHA)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if t.TaskIndex < 0 || t.TaskIndex >= 49 || !validHistoricalRelease(q, t.ReleaseID) || !ValidScientificAgentBudget(t.AgentBudgetSeconds) {
		return nil, errors.New("invalid frozen scientific target")
	}
	return t, nil
}

// ScientificTargetForLease returns only the immutable batch snapshot, fenced
// against a live attempt. No participant request can set controller arguments.
func (s *Store) ScientificTargetForLease(l Lease) (*ScientificTarget, error) {
	var target *ScientificTarget
	err := s.write(func(tx *sql.Tx) error {
		if err := s.fence(tx, l); err != nil {
			return err
		}
		var err error
		if l.Mode == "scientific" {
			target, err = readRunScientificTarget(tx, l)
			return err
		}
		target, err = readScientificTarget(tx, l.BatchID)
		if err != nil {
			return err
		}
		if target != nil && (target.SubmissionSHA != l.SHA256 || l.Mode != "scientific-smoke" || l.Kind != "infra" || l.Public != 0 || l.Ordinal != 0) {
			return errors.New("scientific target lease mismatch")
		}
		return nil
	})
	return target, err
}
