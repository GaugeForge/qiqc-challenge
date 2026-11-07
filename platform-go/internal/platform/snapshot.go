package platform

import (
	"encoding/json"
	"errors"
	"os"
	"strconv"
	"strings"
)

// snapshotRows only accepts organizer-authored SQL with explicit field lists.
// Credentials, session hashes and run tokens are deliberately never selected.
func snapshotRows(q queryer, statement string, args ...any) ([]map[string]any, error) {
	rows, err := q.Query(statement, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	result := []map[string]any{}
	for rows.Next() {
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for i := range values {
			pointers[i] = &values[i]
		}
		if err = rows.Scan(pointers...); err != nil {
			return nil, err
		}
		row := map[string]any{}
		for i, key := range columns {
			value := values[i]
			if raw, ok := value.([]byte); ok {
				value = string(raw)
			}
			row[key] = value
		}
		result = append(result, row)
	}
	return result, rows.Err()
}
func snapshotSettings(q queryer) (map[string]string, error) {
	rows, err := q.Query("SELECT key,value FROM settings WHERE key IN ('paused','competition_closed','global_task_slots','e2b_key2_slots','e2b_key1_slots','deployment_evidence','pricing')")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]string{}
	for rows.Next() {
		var k, v string
		if err = rows.Scan(&k, &v); err != nil {
			return nil, err
		}
		result[k] = v
	}
	return result, rows.Err()
}
func booleanSetting(settings map[string]string, key string) (bool, error) {
	raw, ok := settings[key]
	if !ok || (raw != "true" && raw != "false") {
		return false, errors.New("invalid persisted platform state")
	}
	return raw == "true", nil
}
func snapshotSlots(settings map[string]string) (int, error) {
	raw, ok := settings["global_task_slots"]
	if !ok {
		return 4, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 1 || n > 10000 {
		return 0, errors.New("invalid persisted task slot limit")
	}
	return n, nil
}
func snapshotPoolSlots(settings map[string]string) map[string]int {
	out := map[string]int{"infra_public": 20, "formal": 100}
	for key, name := range map[string]string{"e2b_key2_slots": "infra_public", "e2b_key1_slots": "formal"} {
		if n, err := strconv.Atoi(settings[key]); err == nil && n > 0 && n <= 10000 {
			out[name] = n
		}
	}
	return out
}
func snapshotEvidence(settings map[string]string) map[string]bool {
	out := map[string]bool{}
	// Invalid or absent evidence never produces a successful verification claim.
	if raw, ok := settings["deployment_evidence"]; ok {
		if json.Unmarshal([]byte(raw), &out) != nil {
			return map[string]bool{}
		}
	}
	return out
}
func (s *Store) snapshotStorage() string {
	if s.Objects == nil {
		return "not_configured"
	}
	return s.Objects.Provider()
}

// Config preserves the frontend's public shape without hardcoding cloud
// acceptance. Configured storage and verified science are separate statements.
func (s *Store) Config() (map[string]any, error) {
	settings, err := snapshotSettings(s.DB)
	if err != nil {
		return nil, err
	}
	closed, err := booleanSetting(settings, "competition_closed")
	if err != nil {
		return nil, err
	}
	slots, err := snapshotSlots(settings)
	if err != nil {
		return nil, err
	}
	evidence := snapshotEvidence(settings)
	provider := s.snapshotStorage()
	calendar, err := loadCalendar(s.DB)
	if err != nil {
		return nil, err
	}
	phase, err := s.Phase()
	if err != nil {
		return nil, err
	}
	activeModel := s.Model
	if phase.Name == "competition" {
		p, e := formalExecution(s.DB)
		if e != nil {
			return nil, e
		}
		if p != nil {
			activeModel = p.Model
		} else {
			activeModel = "not_configured"
		}
	}
	science := map[string]any{"configured": false, "public_ready": false, "full_ready": false, "elo_ready": false, "infra_ready": false, "bounded": true, "leaderboard_eligible": false}
	if catalog, _, err := activeTaskSet(s.DB); err == nil {
		count := 0
		roles := map[string]int{}
		for _, task := range catalog.Tasks {
			if task.Public {
				count++
			}
			if task.EvaluationRole != "" {
				roles[task.EvaluationRole]++
			}
		}
		science["configured"] = true
		if catalog.native() {
			science["public_time_limits"] = "native"
			science["full_time_limits"] = "native"
			science["infra_agent_budget_seconds"] = catalog.AgentBudgetSeconds
		}
		science["public_task_count"] = count
		science["public_ready"] = count == 5
		if catalog.hasEvaluationRoles() {
			science["binary_task_count"] = roles["binary"]
			science["elo_task_count"] = roles["racing"]
			science["infrastructure_test_count"] = roles["infrastructure"]
			science["full_ready"] = roles["binary"] == 39
			science["elo_ready"] = roles["racing"] == 7
			science["infra_ready"] = roles["infrastructure"] == 3
		} else {
			// Historical catalogs remain visible while the candidate is being
			// migrated; they are deliberately not eligible for the new ranking.
			science["full_ready"] = true
			science["infra_ready"] = true
		}
		science["bounded"] = phase.Name != "competition"
		science["leaderboard_eligible"] = catalog.native() && catalog.hasEvaluationRoles() && phase.Name == "competition"
		if phase.Name != "competition" {
			science["agent_budget_seconds"] = catalog.AgentBudgetSeconds
		}
	}
	return map[string]any{"name": "QIQC Challenge", "mode": "competition-platform", "competition_portal": CompetitionPortal(), "schema_version": "native-go/v1", "timezone": "Asia/Singapore",
		"scientific_agent_budget_seconds": s.ScientificAgentBudget(),
		"scientific_task_set":             science,
		"cloud_ready":                     evidence["cloud_ready"], "r2_ready": provider == "cloudflare-r2", "e2b_smoke_verified": evidence["e2b_smoke_verified"], "hy4_smoke_verified": evidence["hy4_smoke_verified"],
		"phase": phase, "calendar": calendar, "server_time": s.Now().UTC(), "always_open": !closed && calendar.Mode == "demo", "competition_closed": closed, "global_task_slots": slots, "worker_pool_slots": snapshotPoolSlots(settings), "production_initial_slots": nil, "model": activeModel, "storage": provider,
		"execution": "Go 原生平台；模拟任务与科学执行验收分开记录", "model_provider": "Scoped model gateway; formal configuration is required before admission.", "support_email": "qiqc@gauge-forge.com",
		"rule_version": CurrentRuleVersion, "quotas": map[string]int{"full": 1, "public": 3, "infra": 10}}, nil
}

func snapshotPricing(settings map[string]string) any {
	raw, exists := settings["pricing"]
	if !exists {
		return nil
	}
	var parsed map[string]any
	if json.Unmarshal([]byte(raw), &parsed) != nil {
		return nil
	}
	out := map[string]any{}
	for _, key := range []string{"version", "model", "currency", "basis", "created", "input_per_million", "cached_per_million", "output_per_million"} {
		if value, ok := parsed[key]; ok {
			out[key] = value
		}
	}
	return out
}
func sanitizeSnapshotJSON(raw any) any {
	text, ok := raw.(string)
	if !ok {
		return raw
	}
	var value any
	if json.Unmarshal([]byte(text), &value) != nil {
		return "[invalid JSON withheld]"
	}
	var scrub func(any)
	scrub = func(v any) {
		switch obj := v.(type) {
		case map[string]any:
			for key, child := range obj {
				switch strings.ToLower(key) {
				case "password", "initial_password", "api_key", "access_token", "token", "secret", "authorization", "cookie", "credentials":
					obj[key] = "[redacted]"
				default:
					scrub(child)
				}
			}
		case []any:
			for _, child := range obj {
				scrub(child)
			}
		}
	}
	scrub(value)
	encoded, err := json.Marshal(value)
	if err != nil {
		return "[invalid JSON withheld]"
	}
	return string(encoded)
}

func (s *Store) AdminSnapshot(token string) (map[string]any, error) {
	return s.adminSnapshot(token, snapshotOptions{})
}

func (s *Store) adminSnapshot(token string, options snapshotOptions) (map[string]any, error) {
	var snapshot map[string]any
	err := func() error {
		user, err := s.session(s.DB, token)
		if err != nil {
			return err
		}
		if user.Role != "admin" || user.MustChange != 0 {
			return problem(403, "仅完成首次改密的管理员可访问")
		}
		settings, err := snapshotSettings(s.DB)
		if err != nil {
			return err
		}
		paused, err := booleanSetting(settings, "paused")
		if err != nil {
			return err
		}
		slots, err := snapshotSlots(settings)
		if err != nil {
			return err
		}
		versionWhere, versionArgs := options.versionFilters()
		versions, versionPage, err := snapshotPage(s.DB, "versions v JOIN teams t ON t.id=v.team_id",
			"v.id,v.team_id,v.sha256,'v' || "+versionNumberSQL("v")+" name,v.name harness_name,v.status,v.report,v.reason,v.created,t.name team",
			"(v.status='pending_review') DESC,v.created DESC,v.id", versionWhere, versionArgs, options.pages["versions"])
		if err != nil {
			return err
		}
		for _, version := range versions {
			var report map[string]any
			raw, _ := version["report"].(string)
			if json.Unmarshal([]byte(raw), &report) != nil || report == nil {
				report = map[string]any{"invalid": true, "risks": []map[string]string{{"rule": "stored_review_corrupt", "file": ""}}, "snippets": []any{}}
			}
			if report["risks"] == nil {
				report["risks"] = []any{}
			}
			vid, _ := version["id"].(string)
			sha, _ := version["sha256"].(string)
			advice, adviceErr := advisorySnapshot(s.DB, vid, sha, raw)
			if adviceErr != nil {
				return adviceErr
			}
			version["ai_advisory"] = advice
			version["report"] = report
			targets, err := snapshotRows(s.DB, "SELECT task_index,release_id,agent_budget_seconds FROM scientific_targets WHERE version_id=?", vid)
			if err != nil {
				return err
			}
			version["scientific_targets"] = targets
		}
		batchWhere, batchArgs := options.batchFilters()
		batches, batchPage, err := snapshotPage(s.DB, "batches b JOIN teams t ON t.id=b.team_id",
			"b.id,b.team_id,b.version_id,b.kind,b.mode,b.idem,b.status,b.accepted_at,b.last_dispatch,b.passed,b.mean,b.invalid,t.name team",
			"b.accepted_at DESC,b.id", batchWhere, batchArgs, options.pages["batches"])
		if err != nil {
			return err
		}
		for _, batch := range batches {
			states, err := snapshotRows(s.DB, "SELECT state,COUNT(*) n FROM runs WHERE batch_id=? GROUP BY state", batch["id"])
			if err != nil {
				return err
			}
			counts := map[string]int64{}
			for _, state := range states {
				name, _ := state["state"].(string)
				count, _ := state["n"].(int64)
				counts[name] = count
			}
			batch["run_states"] = counts
			targets, err := snapshotRows(s.DB, "SELECT task_index,release_id,agent_budget_seconds FROM batch_scientific_targets WHERE batch_id=?", batch["id"])
			if err != nil {
				return err
			}
			batch["scientific_targets"] = targets
			rows, err := snapshotRows(s.DB, "SELECT ordinal,public,state,attempt,error,reward,seconds FROM runs WHERE batch_id=? ORDER BY ordinal", batch["id"])
			if err != nil {
				return err
			}
			batch["runs"] = rows
			batch["errors"] = rows
		}
		teams, err := snapshotRows(s.DB, "SELECT DISTINCT t.id,t.name FROM teams t JOIN users u ON u.team_id=t.id WHERE u.role='team' ORDER BY t.name,t.id")
		if err != nil {
			return err
		}
		for _, team := range teams {
			id, _ := team["id"].(string)
			q, err := quotas(s.DB, id)
			if err != nil {
				return err
			}
			team["quotas"] = q
		}
		totals, err := snapshotRows(s.DB, `SELECT COUNT(*) calls,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,
   SUM(amount) known_amount,COALESCE(SUM(CASE WHEN amount IS NULL THEN 1 ELSE 0 END),0) unpriced FROM usage`)
		if err != nil {
			return err
		}
		usageRows, usagePage, err := snapshotPage(s.DB, "usage", "id,run_id,attempt,model,created,status,input_tokens,output_tokens,cached_tokens,amount,price_version,source", "created DESC,id", nil, nil, options.pages["usage"])
		if err != nil {
			return err
		}
		audit, auditPage, err := snapshotPage(s.DB, "audit", "id,created,user_id,actor,action,object_id,reason,before_json,after_json,session_fingerprint,ip", "created DESC,id", nil, nil, options.pages["audit"])
		if err != nil {
			return err
		}
		for _, row := range audit {
			row["before_json"] = sanitizeSnapshotJSON(row["before_json"])
			row["after_json"] = sanitizeSnapshotJSON(row["after_json"])
		}
		appeals, err := snapshotRows(s.DB, "SELECT a.id,a.team_id,a.body,a.status,a.created,a.response,t.name team FROM appeals a JOIN teams t ON t.id=a.team_id ORDER BY a.created DESC,a.id")
		if err != nil {
			return err
		}
		outbox, err := snapshotRows(s.DB, "SELECT id,event,team_id,payload,created,status FROM outbox ORDER BY created DESC,id LIMIT 25")
		if err != nil {
			return err
		}
		for _, row := range outbox {
			row["payload"] = sanitizeSnapshotJSON(row["payload"])
		}
		health, err := s.operationalHealth(s.DB)
		if err != nil {
			return err
		}
		disk := health["upload_disk"].(UploadDiskStatus)
		provider := s.snapshotStorage()
		e2b := "not_configured"
		if os.Getenv("E2B_API_KEY") != "" || os.Getenv("E2B_API_KEY_2") != "" {
			e2b = "configured"
		}
		notificationDelivery, err := snapshotNotificationDelivery(s.DB)
		if err != nil {
			return err
		}
		accounts, err := snapshotRows(s.DB, "SELECT u.id,u.email,u.team_id,u.role,u.must_change,t.name team FROM users u JOIN teams t ON t.id=u.team_id ORDER BY u.email,u.id")
		if err != nil {
			return err
		}
		counts, err := snapshotRows(s.DB, `SELECT
          (SELECT COUNT(*) FROM versions WHERE status='pending_review') pending_reviews,
          (SELECT COUNT(*) FROM batches WHERE status='queued') queued_batches,
          (SELECT COUNT(*) FROM batches WHERE status IN ('testing','cancelling')) running_batches,
          (SELECT COUNT(*) FROM batches WHERE status='attention') attention_batches,
          (SELECT COUNT(*) FROM batches) total_batches,
          (SELECT COUNT(*) FROM versions) total_versions,
          (SELECT COUNT(*) FROM runs WHERE state='running') running_tasks,
          (SELECT COUNT(*) FROM runs WHERE state='queued') queued_tasks`)
		if err != nil {
			return err
		}
		formal, err := formalExecution(s.DB)
		if err != nil {
			return err
		}
		execution := map[string]any{"formal_configured": formal != nil, "formal_model": "", "formal_max_requests_per_task": nil, "test_model": s.Model, "test_agent_budget_seconds": s.ScientificAgentBudget(), "formal_native_task_limits": true}
		if formal != nil {
			execution["formal_model"] = formal.Model
			execution["formal_max_requests_per_task"] = formal.MaxRequestsPerTask
		}
		snapshot = map[string]any{"execution_policy": execution, "pagination": map[string]any{"versions": versionPage, "batches": batchPage, "usage": usagePage, "audit": auditPage}, "accounts": accounts, "counts": counts[0], "host_status": readHostStatus("/run/qiqc-status/health.json", s.now()), "notification_delivery": notificationDelivery, "versions": versions, "batches": batches, "teams": teams, "paused": paused, "slots": slots, "usage": totals[0], "usage_rows": usageRows,
			"audit": audit, "appeals": appeals, "outbox": outbox, "disk_free_bytes": disk.AvailableBytes, "operational_health": health, "storage": provider, "e2b": e2b,
			"ai_review": "manual_fallback", "model": s.Model, "real_cloud_verified": snapshotEvidence(settings)["real_cloud_verified"], "pricing": snapshotPricing(settings), "schema_version": "native-go/v1"}
		return nil
	}()
	return snapshot, err
}
