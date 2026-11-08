package platform

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gaugeforge/qiqc-platform-go/internal/review"
)

// AI evidence remains isolated from decisions. Only the trusted platform applies
// the automatic policy below; the provider cannot update versions or quotas.
const automaticReviewPolicy = "qiqc-automatic-review/v1"

func reviewPolicyIdentity(provider string) string {
	return automaticReviewPolicy + ":" + review.PolicyVersion + "@" + provider
}
func (s *Store) ensureReviewSchema() error {
	_, err := s.DB.Exec(`CREATE TABLE IF NOT EXISTS review_advisories(
 version_id TEXT NOT NULL REFERENCES versions(id), submission_sha TEXT NOT NULL,
 report_sha TEXT NOT NULL, policy_version TEXT NOT NULL, state TEXT NOT NULL,
 lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
 input_sha TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, finished REAL,
 PRIMARY KEY(version_id,submission_sha,report_sha,policy_version))`)
	return err
}

type reviewLease struct {
	Policy    string
	Input     review.Input
	Token     string
	SHA       string
	ReportSHA string
	ID        string
}

func boundedUTF8(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	s = s[:limit]
	for !utf8.ValidString(s) {
		s = s[:len(s)-1]
	}
	return s
}
func frozenReviewInput(vid, sha, raw string) (review.Input, error) {
	in := review.Input{VersionID: vid, SHA256: sha, Risks: []review.Risk{}, Snippets: []review.Snippet{}}
	sum := sha256.Sum256([]byte(raw))
	in.ReportSHA256 = hex.EncodeToString(sum[:])
	if len(raw) > 256*1024 {
		return in, errors.New("scanner report exceeds review boundary")
	}
	var report struct {
		Ruleset  string              `json:"ruleset_version"`
		Risks    []review.Risk       `json:"risks"`
		Snippets []map[string]string `json:"snippets"`
	}
	if json.Unmarshal([]byte(raw), &report) != nil {
		return in, errors.New("invalid frozen scanner report")
	}
	in.ScannerVersion = report.Ruleset
	if in.ScannerVersion == "" || len(report.Snippets) == 0 {
		return in, errors.New("legacy or empty scanner evidence cannot be automatically approved")
	}
	hasText := false
	for _, snippet := range report.Snippets {
		if strings.TrimSpace(snippet["text"]) != "" {
			hasText = true
		}
	}
	if !hasText {
		return in, errors.New("scanner evidence has no reviewable text")
	}
	if len(report.Risks) > 100 || len(report.Snippets) > 24 {
		return in, errors.New("too many scanner entries")
	}
	in.Risks = report.Risks
	remaining := 16 * 1024
	for _, snippet := range report.Snippets {
		if remaining == 0 {
			break
		}
		limit := 2048
		if remaining < limit {
			limit = remaining
		}
		text := boundedUTF8(snippet["text"], limit)
		remaining -= len(text)
		in.Snippets = append(in.Snippets, review.Snippet{File: snippet["file"], Text: text})
	}
	normalized, _, err := review.Normalize(in)
	return normalized, err
}

func (s *Store) claimAdvisory(owner string, ttl time.Duration, policy string) (*reviewLease, error) {
	if strings.TrimSpace(owner) == "" || len(owner) > 100 {
		return nil, errors.New("invalid review worker owner")
	}
	var result *reviewLease
	err := s.write(func(tx *sql.Tx) error {
		// Page frozen reports so completed advice never starves later uploads.
		for offset := 0; ; offset += 64 {
			rows, err := tx.Query(`SELECT v.id,v.sha256,v.report FROM versions v WHERE v.status='pending_review'
  ORDER BY v.created,v.id LIMIT 64 OFFSET ?`, offset)
			if err != nil {
				return err
			}
			type candidate struct{ id, sha, raw string }
			items := []candidate{}
			for rows.Next() {
				var c candidate
				if err = rows.Scan(&c.id, &c.sha, &c.raw); err != nil {
					rows.Close()
					return err
				}
				items = append(items, c)
			}
			if err = rows.Err(); err != nil {
				rows.Close()
				return err
			}
			rows.Close()
			for _, c := range items {
				sum := sha256.Sum256([]byte(c.raw))
				reportSHA := hex.EncodeToString(sum[:])
				now := s.now()
				_, err = tx.Exec(`INSERT OR IGNORE INTO review_advisories(version_id,submission_sha,report_sha,policy_version,state,created) VALUES(?,?,?,?, 'queued',?)`, c.id, c.sha, reportSHA, policy, now)
				if err != nil {
					return err
				}
				token := id()
				updated, err := tx.Exec(`UPDATE review_advisories SET state='running',lease_token=?,lease_until=?,attempts=attempts+1 WHERE version_id=? AND submission_sha=? AND report_sha=? AND policy_version=? AND (state='queued' OR (state IN ('running','retry_wait') AND lease_until<=?))`, token, now+ttl.Seconds(), c.id, c.sha, reportSHA, policy, now)
				if err != nil {
					return err
				}
				n, _ := updated.RowsAffected()
				if n == 0 {
					continue
				}
				in, inputErr := frozenReviewInput(c.id, c.sha, c.raw)
				if inputErr != nil {
					in = review.Input{VersionID: c.id, SHA256: c.sha, ReportSHA256: reportSHA}
				}
				result = &reviewLease{Input: in, Token: token, SHA: c.sha, ReportSHA: reportSHA, ID: c.id, Policy: policy}
				return nil
			}
			if len(items) < 64 {
				return nil
			}
		}
	})
	return result, err
}

// ProcessReviewOnce applies the automatic policy to at most one pending version.
// Evidence, immutable identity checks, decision and audit commit atomically.
// This operation never creates an evaluation or changes quota.
func (s *Store) ProcessReviewOnce(ctx context.Context, worker *review.Worker, owner string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if worker == nil {
		return false, errors.New("review worker missing")
	}
	providerVersion := "disabled"
	if worker.Provider != nil {
		providerVersion = worker.Provider.Version()
	}
	policy := reviewPolicyIdentity(providerVersion)
	lease, err := s.claimAdvisory(owner, worker.Timeout+30*time.Second, policy)
	if err != nil || lease == nil {
		return false, err
	}
	advice := worker.Review(ctx, lease.Input)
	// Bind trusted identity outside provider control, even on invalid input.
	advice.VersionID = lease.ID
	advice.SHA256 = lease.SHA
	advice.ReportSHA256 = lease.ReportSHA
	encoded, err := json.Marshal(advice)
	if err != nil {
		return true, err
	}
	err = s.write(func(tx *sql.Tx) error {
		var sha, raw, state string
		if err := tx.QueryRow(`SELECT sha256,report,status FROM versions WHERE id=?`, lease.ID).Scan(&sha, &raw, &state); err != nil {
			return err
		}
		sum := sha256.Sum256([]byte(raw))
		same := sha == lease.SHA && hex.EncodeToString(sum[:]) == lease.ReportSHA && state == "pending_review"
		next := "complete"
		if !same {
			next = "superseded"
		}
		var attempts int
		if err := tx.QueryRow("SELECT attempts FROM review_advisories WHERE version_id=? AND submission_sha=? AND report_sha=? AND policy_version=?", lease.ID, lease.SHA, lease.ReportSHA, lease.Policy).Scan(&attempts); err != nil {
			return err
		}
		_, liveHTTPS := worker.Provider.(*review.HTTPSProvider)
		_, liveDiagnostic := worker.Provider.(*review.DiagnosticProvider)
		retry := same && (liveHTTPS || liveDiagnostic) && attempts < 3 && (advice.Outcome == "provider_timeout" || advice.Outcome == "provider_error" || advice.Outcome == "provider_busy")
		if retry {
			next = "retry_wait"
		}
		update, err := tx.Exec(`UPDATE review_advisories SET state=?,result=?,input_sha=?,finished=?,lease_until=0 WHERE version_id=? AND submission_sha=? AND report_sha=? AND policy_version=? AND state='running' AND lease_token=? AND lease_until>?`, next, string(encoded), advice.InputSHA256, s.now(), lease.ID, lease.SHA, lease.ReportSHA, lease.Policy, lease.Token, s.now())
		if err != nil {
			return err
		}
		n, _ := update.RowsAffected()
		if n != 1 {
			return errors.New("review lease expired or superseded")
		}
		if !same {
			return nil
		}
		if retry {
			reason := "Review service temporarily unavailable; automatic retry scheduled. No evaluation attempt charged."
			if _, err = tx.Exec("UPDATE review_advisories SET lease_until=? WHERE version_id=? AND submission_sha=? AND report_sha=? AND policy_version=?", s.now()+float64(15*attempts), lease.ID, lease.SHA, lease.ReportSHA, lease.Policy); err != nil {
				return err
			}
			if _, err = tx.Exec(`INSERT INTO audit(id,created,user_id,actor,action,object_id,reason,before_json,after_json,session_fingerprint,ip) VALUES(?,?,'','automatic-review','automatic_review_retry',?,?,'{}',?, '', '')`, id(), s.now(), lease.ID, reason, string(encoded)); err != nil {
				return err
			}
			_, err = tx.Exec("UPDATE versions SET reason=? WHERE id=? AND status='pending_review'", reason, lease.ID)
			return err
		}
		nextStatus, reason := s.automaticReviewDecision(worker, advice, raw, sha)
		before, _ := json.Marshal(map[string]string{"status": state, "submission_sha": sha})
		after, _ := json.Marshal(map[string]string{"status": nextStatus, "policy": policy, "report_sha": lease.ReportSHA, "input_sha": advice.InputSHA256})
		if _, err = tx.Exec(`INSERT INTO audit(id,created,user_id,actor,action,object_id,reason,before_json,after_json,session_fingerprint,ip) VALUES(?,?,'','automatic-review','automatic_review',?,?,?,?, '', '')`, id(), s.now(), lease.ID, reason, string(before), string(after)); err != nil {
			return err
		}
		_, err = tx.Exec("UPDATE versions SET status=?,reason=? WHERE id=? AND status='pending_review'", nextStatus, reason, lease.ID)
		return err
	})
	return true, err
}

func advisorySnapshot(tx queryer, versionID, sha, rawReport string) (any, error) {
	sum := sha256.Sum256([]byte(rawReport))
	reportSHA := hex.EncodeToString(sum[:])
	var result, state string
	err := tx.QueryRow(`SELECT result,state FROM review_advisories WHERE version_id=? AND submission_sha=? AND report_sha=? ORDER BY created DESC,finished DESC LIMIT 1`, versionID, sha, reportSHA).Scan(&result, &state)
	if errors.Is(err, sql.ErrNoRows) {
		return map[string]any{"status": "manual_required", "outcome": "not_requested"}, nil
	}
	if err != nil {
		return nil, err
	}
	if state != "complete" {
		return map[string]any{"status": "manual_required", "outcome": state}, nil
	}
	var advice review.Advice
	if json.Unmarshal([]byte(result), &advice) != nil {
		return map[string]any{"status": "manual_required", "outcome": "invalid_stored_advice"}, nil
	}
	return advice, nil
}

// Automatic approval is based on organizer-produced static evidence and a valid
// live-provider advisory. Snippets are explicitly bounded; approval does not
// certify that submitted code is safe to execute outside the isolated worker.
func (s *Store) automaticReviewDecision(worker *review.Worker, advice review.Advice, raw, sha string) (string, string) {
	reject := func(code string) (string, string) {
		return "rejected", "Automated review did not approve this version (" + code + "). No evaluation attempt charged."
	}
	var report Report
	if json.Unmarshal([]byte(raw), &report) != nil || !validScannerReport(report) || report.Risks == nil || report.Source != "authenticated_team_upload" || report.SourceSHA256 != sha {
		return reject("invalid_scanner_evidence")
	}
	if s.CheckVersion(sha) != nil {
		return reject("frozen_package_invalid")
	}
	if len(report.Risks) != 0 {
		// Static findings are evidence for organizer review, not an automatic
		// verdict. The scanner provides locations and explanations so a
		// participant can correct a real issue or explain a false positive.
		return "pending_review", "Automated review found static findings requiring organizer review. No evaluation attempt charged."
	}
	if advice.Outcome != "advice_ready" {
		switch advice.Outcome {
		case "provider_unavailable", "provider_timeout", "provider_error", "provider_busy":
			// A provider-side outage or completion limit is not a participant
			// rejection. After the bounded advisory retries it becomes a manual
			// staff review and never charges an evaluation attempt.
			code := advice.FailureCode
			if code == "" {
				code = advice.Outcome
			}
			return "pending_review", "Automated advisory was unavailable (" + code + "); manual review required. No evaluation attempt charged."
		case "invalid_provider_version", "invalid_response", "invalid_input":
			return reject(advice.Outcome)
		default:
			return reject("invalid_review_evidence")
		}
	}
	switch worker.Provider.(type) {
	case *review.HTTPSProvider, *review.DiagnosticProvider:
		// Only configured production provider implementations can approve.
	default:
		return "pending_review", "Automated review provider is not configured; manual review required. No evaluation attempt charged."
	}
	if len(advice.Findings) != 0 || advice.UncoveredScannerRisks != 0 {
		return reject("ai_risk_detected")
	}
	if !validDigest(advice.InputSHA256) || advice.ReportSHA256 != shaBytes([]byte(raw)) || advice.SHA256 != sha {
		return reject("invalid_review_identity")
	}
	return "approved", "Automated review passed: no static or AI findings in the reviewed scope. Execution remains sandboxed."
}
