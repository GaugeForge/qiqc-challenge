package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
)

type TaskLimits struct {
	AgentSeconds    int    `json:"agent_seconds"`
	SetupSeconds    int    `json:"setup_seconds"`
	BuildSeconds    int    `json:"build_seconds"`
	VerifierSeconds int    `json:"verifier_seconds"`
	CPUs            int    `json:"cpus"`
	MemoryMB        int    `json:"memory_mb"`
	SourceSHA256    string `json:"source_sha256"`
}

func (l TaskLimits) Valid() bool {
	return l.AgentSeconds > 0 && l.AgentSeconds <= 604800 && l.SetupSeconds > 0 && l.SetupSeconds <= 86400 && l.BuildSeconds > 0 && l.BuildSeconds <= 86400 && l.VerifierSeconds > 0 && l.VerifierSeconds <= 86400 && l.CPUs > 0 && l.CPUs <= 256 && l.MemoryMB >= 128 && l.MemoryMB <= 1048576 && validDigest(l.SourceSHA256)
}
func (c ScientificTaskSet) native() bool { return c.Schema == "qiqc-scientific-task-set/v2" }
func (c ScientificTaskSet) hasEvaluationRoles() bool {
	for _, t := range c.Tasks {
		if t.EvaluationRole != "" {
			return true
		}
	}
	return false
}
func (c ScientificTaskSet) selected(kind string) []ScientificTask {
	out := []ScientificTask{}
	for _, t := range c.Tasks {
		selected := false
		switch kind {
		case "full":
			// The real competition leaderboard is the paper's 39 binary
			// science gates. Legacy catalogs without role metadata retain
			// their historical all-task behavior.
			selected = !c.hasEvaluationRoles() || t.EvaluationRole == "binary"
		case "elo":
			selected = c.hasEvaluationRoles() && t.EvaluationRole == "racing"
		case "public":
			selected = t.Public
		case "infra":
			if c.hasEvaluationRoles() {
				selected = t.EvaluationRole == "infrastructure"
			} else {
				selected = (c.native() && t.Public) || (!c.native() && t.TaskIndex == c.InfraTaskIndex)
			}
		}
		if selected {
			out = append(out, t)
		}
	}
	return out
}
func (c ScientificTaskSet) budget(t ScientificTask, phase, kind string) int {
	if !c.native() {
		return c.AgentBudgetSeconds
	}
	// Infrastructure acceptance is intentionally bounded to the fixed
	// operator budget. Public samples and all competition-facing batches keep
	// the immutable Harbor task limit, even during the pre-contest window.
	if kind != "infra" {
		return t.Limits.AgentSeconds
	}
	if c.AgentBudgetSeconds < t.Limits.AgentSeconds {
		return c.AgentBudgetSeconds
	}
	return t.Limits.AgentSeconds
}

func (c ScientificTaskSet) pinnedBudget(t ScientificTask, p BatchPolicy, kind string) int {
	if p.BudgetPolicy == "task-native-except-infra/v1" {
		return c.budget(t, p.Phase, kind)
	}
	if c.native() && p.Phase == "competition" {
		return t.Limits.AgentSeconds
	}
	return c.budget(t, p.Phase, "infra")
}
func batchPolicy(q queryer, bid string) (BatchPolicy, error) {
	var p BatchPolicy
	var raw, sha string
	e := q.QueryRow("SELECT policy_json,policy_sha256 FROM batch_policies WHERE batch_id=?", bid).Scan(&raw, &sha)
	if e != nil {
		return p, e
	}
	if shaBytes([]byte(raw)) != sha || json.Unmarshal([]byte(raw), &p) != nil {
		return p, errors.New("batch policy integrity failure")
	}
	return p, nil
}
func batchTaskCount(q queryer, bid, kind, mode string) (int, error) {
	if mode != "scientific" {
		return kinds[kind], nil
	}
	var digest string
	if e := q.QueryRow("SELECT catalog_sha256 FROM batch_scientific_task_sets WHERE batch_id=?", bid).Scan(&digest); e != nil {
		return 0, e
	}
	c, e := readTaskSet(q, digest)
	if e != nil {
		return 0, e
	}
	return len(c.selected(kind)), nil
}

// Upgrade only the per-run leaf table. Legacy diagnostic tables and receipts
// retain their narrow budgets; long budgets require a verified v2 catalog.
func (s *Store) ensureNativeBudgetSchema() error {
	return s.write(func(tx *sql.Tx) error {
		var definition string
		if e := tx.QueryRow("SELECT sql FROM sqlite_master WHERE name='run_scientific_targets'").Scan(&definition); e != nil {
			return e
		}
		if strings.Contains(definition, "BETWEEN 1 AND 604800") {
			return nil
		}
		if !strings.Contains(definition, "IN (180,300)") {
			return errors.New("unknown run target budget schema")
		}
		definition = strings.Replace(definition, "IN (180,300)", "BETWEEN 1 AND 604800", 1)
		definition = strings.Replace(definition, "run_scientific_targets(", "run_scientific_targets_native_upgrade(", 1)
		definition = strings.Replace(definition, "IF NOT EXISTS ", "", 1)
		for _, query := range []string{definition, "INSERT INTO run_scientific_targets_native_upgrade SELECT * FROM run_scientific_targets", "DROP TABLE run_scientific_targets", "ALTER TABLE run_scientific_targets_native_upgrade RENAME TO run_scientific_targets"} {
			if _, e := tx.Exec(query); e != nil {
				return e
			}
		}
		return nil
	})
}
