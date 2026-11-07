package platform

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
)

// Scientific mode is a bounded real evaluation, separate from diagnostic smoke
// and simulated leaderboard data. These modes share strict resource recovery.
func IsScientificMode(mode string) bool { return mode == "scientific-smoke" || mode == "scientific" }

type ScientificTask struct {
	TaskIndex      int        `json:"task_index"`
	TaskID         string     `json:"task_id"`
	Public         bool       `json:"public"`
	EvaluationRole string     `json:"evaluation_role,omitempty"`
	Limits         TaskLimits `json:"limits,omitempty"`
}
type ScientificTaskSet struct {
	Schema             string           `json:"schema"`
	ReleaseID          string           `json:"release_id"`
	AgentBudgetSeconds int              `json:"agent_budget_seconds"`
	InfraTaskIndex     int              `json:"infra_task_index"`
	Tasks              []ScientificTask `json:"tasks"`
}

func (c ScientificTaskSet) validate() error {
	if (c.Schema != "qiqc-scientific-task-set/v1" && c.Schema != "qiqc-scientific-task-set/v2") || !regexp.MustCompile(`^native-(candidate|release)-[a-f0-9]{64}$`).MatchString(c.ReleaseID) || !ValidScientificAgentBudget(c.AgentBudgetSeconds) || len(c.Tasks) != 49 {
		return errors.New("invalid bounded scientific task set")
	}
	public := 0
	roles := map[string]int{}
	roleEntries := 0
	previous := ""
	for i, t := range c.Tasks {
		if t.TaskIndex != i || !regexp.MustCompile(`^[a-z][a-z0-9_]{0,99}$`).MatchString(t.TaskID) || t.TaskID <= previous {
			return errors.New("task set must contain 49 distinct lexically indexed task IDs")
		}
		if c.native() && !t.Limits.Valid() {
			return errors.New("native task limits missing or invalid")
		}
		previous = t.TaskID
		if t.Public {
			public++
		}
		if t.EvaluationRole != "" {
			if t.EvaluationRole != "binary" && t.EvaluationRole != "racing" && t.EvaluationRole != "infrastructure" {
				return errors.New("invalid task evaluation role")
			}
			roles[t.EvaluationRole]++
			roleEntries++
		}
	}
	// v1 catalogs remain readable for historical runs. A catalog carrying the
	// new role field must describe the complete paper partition, otherwise a
	// worker could silently route a task to the wrong batch.
	if roleEntries != 0 && roleEntries != len(c.Tasks) ||
		roleEntries == len(c.Tasks) && (roles["binary"] != 39 || roles["racing"] != 7 || roles["infrastructure"] != 3) {
		return errors.New("task evaluation roles must contain exactly 39 binary, 7 racing and 3 infrastructure tasks")
	}
	if public < 1 || public > 5 || c.native() && public != 5 {
		return errors.New("task set requires one to five explicit public tasks")
	}
	if c.InfraTaskIndex < 0 || c.InfraTaskIndex >= 49 || !c.Tasks[c.InfraTaskIndex].Public {
		return errors.New("infra task must be an explicitly public task")
	}
	return nil
}
func (s *Store) ensureScientificTaskSetSchema() error {
	_, err := s.DB.Exec(`CREATE TABLE IF NOT EXISTS scientific_task_sets(sha256 TEXT PRIMARY KEY,document TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS batch_scientific_task_sets(batch_id TEXT PRIMARY KEY REFERENCES batches(id),catalog_sha256 TEXT NOT NULL REFERENCES scientific_task_sets(sha256));
 CREATE TABLE IF NOT EXISTS run_scientific_targets(run_id TEXT PRIMARY KEY REFERENCES runs(id),catalog_sha256 TEXT NOT NULL REFERENCES scientific_task_sets(sha256),submission_sha256 TEXT NOT NULL,task_index INTEGER NOT NULL CHECK(task_index BETWEEN 0 AND 48),task_id TEXT NOT NULL,public INTEGER NOT NULL CHECK(public IN (0,1)),release_id TEXT NOT NULL,agent_budget_seconds INTEGER NOT NULL CHECK(agent_budget_seconds IN (180,300)));`)
	return err
}

// ConfigureScientificTaskSetFile is a startup-only trusted operator interface.
// It stores immutable content and changes the pointer only for future batches.
func (s *Store) ConfigureScientificTaskSetFile(path string) error {
	if !filepath.IsAbs(path) {
		return errors.New("absolute scientific task set path required")
	}
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() > 65536 {
		return errors.New("invalid scientific task set file")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return s.ConfigureScientificTaskSet(raw)
}
func (s *Store) ConfigureScientificTaskSet(raw []byte) error {
	if len(raw) > 65536 {
		return errors.New("oversized scientific task set")
	}
	var c ScientificTaskSet
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if dec.Decode(&c) != nil || dec.Decode(new(any)) != io.EOF || c.validate() != nil {
		return errors.New("invalid scientific task set configuration")
	}
	canonical, _ := json.Marshal(c)
	digest := shaBytes(canonical)
	return s.write(func(tx *sql.Tx) error {
		release, err := scientificRelease(tx)
		if err != nil {
			return err
		}
		if release != c.ReleaseID {
			return errors.New("task set release does not match registered execution release")
		}
		if _, err = tx.Exec("INSERT OR IGNORE INTO scientific_task_sets VALUES(?,?)", digest, string(canonical)); err != nil {
			return err
		}
		var previous string
		if err = tx.QueryRow("SELECT document FROM scientific_task_sets WHERE sha256=?", digest).Scan(&previous); err != nil {
			return err
		}
		if previous != string(canonical) {
			return errors.New("immutable task set changed")
		}
		_, err = tx.Exec("INSERT INTO settings VALUES('scientific_task_set',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", digest)
		return err
	})
}
func readTaskSet(q queryer, digest string) (ScientificTaskSet, error) {
	var c ScientificTaskSet
	var raw string
	if !validDigest(digest) {
		return c, errors.New("invalid task set digest")
	}
	if err := q.QueryRow("SELECT document FROM scientific_task_sets WHERE sha256=?", digest).Scan(&raw); err != nil {
		return c, err
	}
	if shaBytes([]byte(raw)) != digest || json.Unmarshal([]byte(raw), &c) != nil || c.validate() != nil || !validHistoricalRelease(q, c.ReleaseID) {
		return c, errors.New("scientific task set integrity failure")
	}
	return c, nil
}
func activeTaskSet(q queryer) (ScientificTaskSet, string, error) {
	var digest string
	if err := q.QueryRow("SELECT value FROM settings WHERE key='scientific_task_set'").Scan(&digest); err != nil {
		return ScientificTaskSet{}, "", problem(409, "真实科学任务集尚未配置")
	}
	c, err := readTaskSet(q, digest)
	if err != nil {
		return c, "", err
	}
	release, err := scientificRelease(q)
	if err != nil {
		return c, "", err
	}
	if release != c.ReleaseID {
		return c, "", problem(409, "科学任务集与活动执行版本不一致")
	}
	return c, digest, nil
}
func readRunScientificTarget(q queryer, l Lease) (*ScientificTarget, error) {
	t := ScientificTarget{Kind: l.Kind}
	var public int
	err := q.QueryRow(`SELECT task_index,release_id,agent_budget_seconds,submission_sha256,task_id,catalog_sha256,public FROM run_scientific_targets WHERE run_id=?`, l.RunID).Scan(&t.TaskIndex, &t.ReleaseID, &t.AgentBudgetSeconds, &t.SubmissionSHA, &t.TaskID, &t.CatalogSHA, &public)
	if err != nil {
		return nil, err
	}
	c, err := readTaskSet(q, t.CatalogSHA)
	if err != nil {
		return nil, err
	}
	if t.TaskIndex < 0 || t.TaskIndex >= 49 || l.Mode != "scientific" || (l.Kind != "public" && l.Kind != "full" && l.Kind != "elo" && l.Kind != "infra") || t.SubmissionSHA != l.SHA256 || public != l.Public || c.ReleaseID != t.ReleaseID {
		return nil, errors.New("scientific task lease binding mismatch")
	}
	task := c.Tasks[t.TaskIndex]
	if task.TaskID != t.TaskID || task.Public != (public == 1) {
		return nil, errors.New("scientific visibility binding mismatch")
	}
	policy, err := batchPolicy(q, l.BatchID)
	if err != nil {
		return nil, err
	}
	if t.AgentBudgetSeconds != c.pinnedBudget(task, policy, l.Kind) {
		return nil, errors.New("run budget policy mismatch")
	}
	if c.native() {
		t.Limits = task.Limits
		t.Formal = policy.Phase == "competition"
	}
	selected := c.selected(l.Kind)
	if l.Ordinal < 0 || l.Ordinal >= len(selected) || selected[l.Ordinal] != task {
		return nil, errors.New("scientific task ordinal binding mismatch")
	}
	var batchDigest string
	if err = q.QueryRow("SELECT catalog_sha256 FROM batch_scientific_task_sets WHERE batch_id=?", l.BatchID).Scan(&batchDigest); err != nil || batchDigest != t.CatalogSHA {
		return nil, errors.New("scientific batch catalog mismatch")
	}
	return &t, nil
}
