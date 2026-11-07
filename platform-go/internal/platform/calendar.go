package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// CurrentRuleVersion applies only to new policy snapshots and current config.
// Historical batch_policies retain their originally accepted version and hash.
const CurrentRuleVersion = "PRD 1.5"

// Calendar is admission policy only. Enabling a schedule never enables an
// unverified executor or declares the deployment ready for production.
type Calendar struct {
	Mode           string     `json:"mode"`
	Version        string     `json:"version"`
	StartsAt       time.Time  `json:"starts_at"`
	EndsAt         time.Time  `json:"ends_at"`
	AppealClosesAt time.Time  `json:"appeal_closes_at"`
	InfraStartsAt  *time.Time `json:"infra_starts_at,omitempty"`
	InfraEndsAt    *time.Time `json:"infra_ends_at,omitempty"`
}
type BatchPolicy struct {
	BudgetPolicy        string            `json:"budget_policy,omitempty"`
	FormalExecution     *FormalExecution  `json:"formal_execution,omitempty"`
	Phase               string            `json:"phase,omitempty"`
	EvaluationRole      string            `json:"evaluation_role,omitempty"`
	LeaderboardEligible bool              `json:"leaderboard_eligible,omitempty"`
	Calendar            Calendar          `json:"calendar"`
	RuleVersion         string            `json:"rule_version"`
	Model               string            `json:"model"`
	Catalog             string            `json:"catalog"`
	Scoring             string            `json:"scoring"`
	SubmissionSHA       string            `json:"submission_sha256"`
	ScientificTarget    *ScientificTarget `json:"scientific_target,omitempty"`
}

func (c Calendar) validate() error {
	if c.Version == "demo-open/v1" && c.Mode != "demo" {
		return errors.New("reserved demo calendar version")
	}
	if c.Version == "" || len(c.Version) > 120 {
		return errors.New("calendar version required")
	}
	if c.Mode == "demo" {
		if !c.StartsAt.IsZero() || !c.EndsAt.IsZero() || !c.AppealClosesAt.IsZero() || c.InfraStartsAt != nil || c.InfraEndsAt != nil {
			return errors.New("demo calendar must not silently ignore dates")
		}
		return nil
	}
	if c.Mode != "scheduled" || c.StartsAt.IsZero() || !c.EndsAt.After(c.StartsAt) || !c.AppealClosesAt.After(c.EndsAt) {
		return errors.New("invalid competition calendar")
	}
	if (c.InfraStartsAt == nil) != (c.InfraEndsAt == nil) {
		return errors.New("both infra boundaries required")
	}
	// An explicit infrastructure end may create a longer quiet period than
	// the legacy six-hour fallback, but it cannot move past that fallback and
	// overlap the competition window.
	legacyQuiet := c.StartsAt.Add(-6 * time.Hour)
	if c.InfraStartsAt != nil && (c.InfraStartsAt.IsZero() || !c.InfraEndsAt.After(*c.InfraStartsAt) || c.InfraEndsAt.After(legacyQuiet)) {
		return errors.New("invalid infra calendar")
	}
	return nil
}
func loadCalendar(q queryer) (Calendar, error) {
	var raw string
	err := q.QueryRow("SELECT value FROM settings WHERE key='calendar'").Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return Calendar{Mode: "demo", Version: "demo-open/v1"}, nil
	}
	if err != nil {
		return Calendar{}, err
	}
	var c Calendar
	if len(raw) > 8192 || json.Unmarshal([]byte(raw), &c) != nil || c.validate() != nil {
		return c, fmt.Errorf("stored calendar invalid; admission disabled")
	}
	return c, nil
}
func (c Calendar) allow(kind string, now time.Time) error {
	if c.Mode == "demo" {
		if CompetitionPortal() && kind != "infra" {
			return problem(409, "Only infrastructure test batches are available before the contest.")
		}
		return nil
	}
	start, end := c.StartsAt, c.EndsAt
	if kind == "infra" {
		if c.InfraStartsAt == nil {
			return problem(409, "Infra 开放时间尚未冻结")
		}
		start, end = *c.InfraStartsAt, *c.InfraEndsAt
	} else if kind == "public" && now.Before(c.StartsAt) {
		return problem(409, "Public tests open when the competition starts.")
	}
	if now.Before(start) {
		return problem(409, "评测尚未开始")
	}
	if !now.Before(end) {
		return problem(409, "评测受理已截止")
	}
	return nil
}
func (s *Store) pinBatchPolicy(tx *sql.Tx, bid string, c Calendar, v Version, kind, mode string) error {
	catalog := "fixture-" + kind + "/v1"
	scoring := "demo-binary-half-up6/v1"
	if mode == "scientific-smoke" {
		catalog = "public-cz-diagnostic/v1"
		scoring = "harbor-0.23-cz-diagnostic/v1"
	}
	role := map[string]string{"full": "binary", "elo": "racing", "infra": "infrastructure", "public": "public"}[kind]
	p := BatchPolicy{Phase: c.Phase(s.Now()).Name, EvaluationRole: role, Calendar: c, RuleVersion: CurrentRuleVersion, Model: s.Model, Catalog: catalog, Scoring: scoring, SubmissionSHA: v.SHA256}
	target, err := readScientificTarget(tx, bid)
	if err != nil {
		return err
	}
	if target != nil {
		p.Catalog = "private-catalog-acceptance/" + target.ReleaseID
		p.Scoring = "harbor-0.23-bounded-diagnostic/v1"
		p.ScientificTarget = target
	}
	if mode == "scientific" {
		var digest string
		if err := tx.QueryRow("SELECT catalog_sha256 FROM batch_scientific_task_sets WHERE batch_id=?", bid).Scan(&digest); err != nil {
			return err
		}
		p.Catalog = "scientific-task-set/" + digest
		p.Scoring = "harbor-0.23-bounded-scientific-nonranking/v1"
		catalog, e := readTaskSet(tx, digest)
		if e != nil {
			return e
		}
		if catalog.native() {
			p.BudgetPolicy = "task-native-except-infra/v1"
		}
		if catalog.native() && p.Phase == "competition" {
			p.FormalExecution, e = formalExecution(tx)
			if e != nil {
				return e
			}
			if p.FormalExecution == nil {
				return problem(409, "Formal model configuration is not ready. No attempt charged.")
			}
			p.Model = p.FormalExecution.Model
			p.Scoring = "qiqc-native-binary-half-up6/v1"
			p.LeaderboardEligible = kind == "full"
		}
	}
	raw, err := json.Marshal(p)
	if err != nil {
		return err
	}
	_, err = tx.Exec("INSERT INTO batch_policies(batch_id,policy_sha256,policy_json) VALUES(?,?,?)", bid, shaBytes(raw), string(raw))
	return err
}
func setCalendar(tx *sql.Tx, c Calendar) (Calendar, error) {
	if err := c.validate(); err != nil {
		return Calendar{}, problem(400, err.Error())
	}
	previous, err := loadCalendar(tx)
	if err != nil {
		return previous, err
	}
	raw, _ := json.Marshal(c)
	digest := shaBytes(raw)
	var old string
	err = tx.QueryRow("SELECT sha256 FROM calendar_versions WHERE version=?", c.Version).Scan(&old)
	if err == nil && old != digest {
		return previous, problem(409, "日历版本已绑定不同内容，请使用新版本并说明理由")
	}
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return previous, err
	}
	if _, err = tx.Exec("INSERT OR IGNORE INTO calendar_versions(version,sha256,policy_json) VALUES(?,?,?)", c.Version, digest, string(raw)); err != nil {
		return previous, err
	}
	_, err = tx.Exec("INSERT INTO settings(key,value) VALUES('calendar',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(raw))
	return previous, err
}
