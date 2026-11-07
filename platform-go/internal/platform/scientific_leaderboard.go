package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"os"
)

func (s *Store) ScientificLeaderboard() (map[string]any, error) {
	phase, e := s.Phase()
	if e != nil {
		return nil, e
	}
	baseline := map[string]any{"label": "Codex + Astra(xhigh)", "passed": nil, "mean": nil, "verified": false}
	if phase.LeaderboardEnabled && s.BaselinePath != "" {
		if raw, err := os.ReadFile(s.BaselinePath); err == nil && len(raw) <= 131072 {
			var candidate map[string]any
			if json.Unmarshal(raw, &candidate) == nil && candidate["task_count"] == float64(39) {
				if passed, ok := candidate["passed"].(float64); ok && passed >= 0 && passed <= 39 {
					mean, _ := candidate["mean_runtime_seconds"].(string)
					if mean != "" {
						baseline["passed"], baseline["mean"], baseline["verified"] = passed, mean, candidate["verified"] == true
						baseline["source"] = candidate["source_report"]
					}
				}
			}
		}
	}
	board := map[string]any{"teams": []Standing{}, "baseline": baseline, "queued_batches": 0, "testing_batches": 0, "mode": "competition", "provisional": true, "publication_status": "not_started"}
	if !phase.LeaderboardEnabled {
		return board, nil
	}
	board["publication_status"] = "live"
	rows, e := s.DB.Query(`SELECT b.id,b.team_id,t.name,b.passed,b.mean,b.accepted_at,v.sha256 FROM batches b JOIN teams t ON t.id=b.team_id JOIN versions v ON v.id=b.version_id WHERE b.kind='full' AND b.mode='scientific' AND b.status='completed' AND b.invalid=0 ORDER BY b.accepted_at,b.id`)
	if e != nil {
		return nil, e
	}
	candidates := []Standing{}
	for rows.Next() {
		var r Standing
		if e = rows.Scan(&r.ID, &r.TeamID, &r.Team, &r.Passed, &r.Mean, &r.AcceptedAt, &r.SHA256); e != nil {
			rows.Close()
			return nil, e
		}
		candidates = append(candidates, r)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	valid := []Standing{}
	for _, r := range candidates {
		p, e := batchPolicy(s.DB, r.ID)
		if e != nil {
			return nil, e
		}
		// Only the new binary-role catalog is eligible for the live 39-task
		// board. Historical 49-task policies have no role marker and remain
		// readable in their batch pages without being mixed into the new score
		// denominator.
		if !p.LeaderboardEligible || p.EvaluationRole != "binary" || p.Phase != "competition" || p.Scoring != "qiqc-native-binary-half-up6/v1" {
			continue
		}
		if p.SubmissionSHA != r.SHA256 {
			return nil, errors.New("ranking submission binding mismatch")
		}
		passed, mean, e := s.verifiedFullScore(r.ID)
		if e != nil {
			return nil, e
		}
		if passed != r.Passed || mean != r.Mean {
			return nil, errors.New("ranking aggregate integrity failure")
		}
		valid = append(valid, r)
	}
	// An unattainable baseline disables the challenge marker without inventing
	// an Astra score or blocking ordinary scientific standings.
	ranked, e := Rank(valid, 50, "0")
	if e != nil {
		return nil, e
	}
	board["teams"] = ranked
	var queued, running int
	if e = s.DB.QueryRow(`SELECT COUNT(*) FROM batches b JOIN batch_policies p ON p.batch_id=b.id WHERE b.mode='scientific' AND b.status='queued' AND json_extract(p.policy_json,'$.phase')='competition'`).Scan(&queued); e != nil {
		return nil, e
	}
	if e = s.DB.QueryRow(`SELECT COUNT(*) FROM batches b JOIN batch_policies p ON p.batch_id=b.id WHERE b.mode='scientific' AND b.status IN ('testing','attention','cancelling') AND json_extract(p.policy_json,'$.phase')='competition'`).Scan(&running); e != nil {
		return nil, e
	}
	board["queued_batches"] = queued
	board["testing_batches"] = running
	return board, nil
}
func (s *Store) verifiedFullScore(bid string) (int, string, error) {
	var digest string
	if e := s.DB.QueryRow("SELECT catalog_sha256 FROM batch_scientific_task_sets WHERE batch_id=?", bid).Scan(&digest); e != nil {
		return 0, "", e
	}
	catalog, e := readTaskSet(s.DB, digest)
	if e != nil {
		return 0, "", e
	}
	if !catalog.native() {
		return 0, "", errors.New("legacy diagnostic cannot rank")
	}
	selected := catalog.selected("full")
	if len(selected) != 39 && catalog.hasEvaluationRoles() {
		return 0, "", errors.New("scientific ranking requires the 39 binary tasks")
	}
	rows, e := s.DB.Query(`SELECT r.ordinal,r.state,r.reward,r.seconds,r.object_key,r.object_sha,t.task_index,t.task_id,t.catalog_sha256,t.agent_budget_seconds,t.submission_sha256,a.state FROM runs r JOIN run_scientific_targets t ON t.run_id=r.id JOIN attempts a ON a.run_id=r.id AND a.number=r.attempt WHERE r.batch_id=? ORDER BY r.ordinal`, bid)
	if e != nil {
		return 0, "", e
	}
	defer rows.Close()
	p, e := batchPolicy(s.DB, bid)
	if e != nil {
		return 0, "", e
	}
	count, passed := 0, 0
	durations := []string{}
	for rows.Next() {
		var ordinal, index, budget int
		var state, task, hash, sub, attempt string
		var reward sql.NullInt64
		var seconds, key, sha sql.NullString
		if e = rows.Scan(&ordinal, &state, &reward, &seconds, &key, &sha, &index, &task, &hash, &budget, &sub, &attempt); e != nil {
			return 0, "", e
		}
		if count >= len(selected) || ordinal != count || index != selected[count].TaskIndex || selected[count].TaskID != task || hash != digest || sub != p.SubmissionSHA || budget != selected[count].Limits.AgentSeconds || state != "completed" || attempt != "completed" || !reward.Valid || (reward.Int64 != 0 && reward.Int64 != 1) || !seconds.Valid || !key.Valid || key.String == "" || !sha.Valid || !validDigest(sha.String) {
			return 0, "", errors.New("incomplete or mismatched scientific scoring evidence")
		}
		count++
		passed += int(reward.Int64)
		durations = append(durations, seconds.String)
	}
	if e = rows.Err(); e != nil {
		return 0, "", e
	}
	if count != len(selected) {
		return 0, "", errors.New("scientific ranking requires all binary tasks")
	}
	mean, e := MeanSeconds(durations)
	return passed, mean, e
}
