package platform

import (
	"database/sql"
	"errors"
	"strconv"
	"strings"
	"unicode/utf8"
)

// "elo" is an organizer-only scientific batch. It is present in the shared
// kind vocabulary so the worker lease fence can bind the seven racing tasks,
// but participant admission never grants it directly.
var kinds = map[string]int{"full": 39, "public": 5, "infra": 1, "elo": 7}

const batchFields = "id,team_id,version_id,kind,mode,idem,status,accepted_at,last_dispatch,passed,mean,invalid"

type batch struct {
	ID, TeamID, VersionID, Kind, Mode, Idem, Status string
	AcceptedAt, LastDispatch                        float64
	Passed                                          sql.NullInt64
	Mean                                            sql.NullString
	Invalid                                         int
}

func batchRow(row interface{ Scan(...any) error }) (batch, error) {
	var b batch
	err := row.Scan(&b.ID, &b.TeamID, &b.VersionID, &b.Kind, &b.Mode, &b.Idem, &b.Status, &b.AcceptedAt, &b.LastDispatch, &b.Passed, &b.Mean, &b.Invalid)
	return b, err
}
func batchView(q queryer, b batch) (map[string]any, error) {
	var sha, harnessName string
	var number int
	if err := q.QueryRow("SELECT sha256,name,"+versionNumberSQL("versions")+" FROM versions WHERE id=?", b.VersionID).Scan(&sha, &harnessName, &number); err != nil {
		return nil, err
	}
	name := VersionLabel(number)
	state := b.Status
	count, err := batchTaskCount(q, b.ID, b.Kind, b.Mode)
	if err != nil {
		return nil, err
	}
	result := map[string]any{"id": b.ID, "version_id": b.VersionID, "kind": b.Kind, "mode": b.Mode, "accepted_at": b.AcceptedAt, "invalid": b.Invalid, "status": state, "sha256": sha, "name": name, "harness_name": harnessName, "task_count": count, "score": nil, "public_runs": []map[string]any{}}
	// Public attempts are numbered from the team's accepted public batches. The
	// number is display metadata only; it never changes the immutable batch ID
	// or the trace authorization check.
	if b.Kind == "public" {
		var attempt int
		if err := q.QueryRow(`SELECT COUNT(*) FROM batches WHERE team_id=? AND kind='public' AND (accepted_at<? OR (accepted_at=? AND id<=?))`, b.TeamID, b.AcceptedAt, b.AcceptedAt, b.ID).Scan(&attempt); err != nil {
			return nil, err
		}
		result["public_test_number"] = attempt
	}
	if state == "attention" {
		// Expose the batch-level interruption, never hidden per-task progress,
		// raw provider errors, object keys or incomplete scientific scores.
		result["failure_code"] = "evaluation_paused"
		result["status_message"] = "Some tasks need attention. They can be cancelled or retried individually; running tasks continue."
	}
	if state == "cancelling" {
		result["status_message"] = "Evaluation cancellation is in progress. Running tasks are stopping; attention tasks can be cancelled individually."
	}
	if state == "cancelled" {
		var quietRuns int
		if err := q.QueryRow("SELECT COUNT(*) FROM runs WHERE batch_id=? AND error='quiet_period_started'", b.ID).Scan(&quietRuns); err != nil {
			return nil, err
		}
		if quietRuns > 0 {
			result["failure_code"] = "quiet_period_started"
			result["status_message"] = "The quiet period has started. Queued evaluations were closed; tasks that had not begun did not run."
		}
	}
	var policySHA string
	if err := q.QueryRow("SELECT policy_sha256 FROM batch_policies WHERE batch_id=?", b.ID).Scan(&policySHA); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	result["policy_sha256"] = policySHA
	var budget int
	if err := q.QueryRow("SELECT agent_budget_seconds FROM batch_scientific_targets WHERE batch_id=?", b.ID).Scan(&budget); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	} else if err == nil {
		result["catalog_acceptance"] = true
		result["agent_budget_seconds"] = budget
	}
	if b.Mode == "scientific" {
		policy, e := batchPolicy(q, b.ID)
		if e != nil {
			return nil, e
		}
		result["phase"] = policy.Phase
		result["evaluation_role"] = policy.EvaluationRole
		result["bounded_scientific"] = policy.Phase != "competition"
		result["leaderboard_eligible"] = policy.LeaderboardEligible
		var digest string
		if err := q.QueryRow("SELECT catalog_sha256 FROM batch_scientific_task_sets WHERE batch_id=?", b.ID).Scan(&digest); err != nil {
			return nil, err
		}
		catalog, err := readTaskSet(q, digest)
		if err != nil {
			return nil, err
		}
		if policy.BudgetPolicy == "task-native-except-infra/v1" && b.Kind != "infra" {
			result["task_time_limits"] = "native"
		} else if policy.Phase != "competition" {
			result["agent_budget_seconds"] = catalog.AgentBudgetSeconds
		}
	}
	if b.Status == "queued" {
		var ahead int
		err := q.QueryRow(`SELECT COUNT(*) FROM batches b WHERE b.id!=? AND b.status IN ('queued','testing') AND EXISTS(SELECT 1 FROM runs x WHERE x.batch_id=b.id AND x.state='queued') AND (b.last_dispatch<? OR (b.last_dispatch=? AND b.accepted_at<?))`, b.ID, b.LastDispatch, b.LastDispatch, b.AcceptedAt).Scan(&ahead)
		if err != nil {
			return nil, err
		}
		result["tasks_ahead"] = ahead
	}
	if b.Status == "queued" || b.Status == "testing" || b.Status == "attention" || b.Status == "cancelling" {
		taskRuns, err := batchTaskRuns(q, b)
		if err != nil {
			return nil, err
		}
		result["task_runs"] = taskRuns
	}
	if b.Status == "completed" {
		result["score"] = map[string]any{"passed": b.Passed.Int64, "total": count, "mean_seconds": b.Mean.String}
		// Participant trajectories belong only to the three Public tests. A
		// Full evaluation may contain the same task identities internally, but
		// it must never expose a downloadable public trace surface.
		if b.Kind != "public" {
			return result, nil
		}
		rows, err := q.Query("SELECT ordinal FROM runs WHERE batch_id=? AND public=1 ORDER BY ordinal", b.ID)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		public := []map[string]any{}
		for rows.Next() {
			var ordinal int
			if err = rows.Scan(&ordinal); err != nil {
				return nil, err
			}
			label := "Mock task"
			if b.Mode == "scientific-smoke" {
				label = "公开科学任务 · CZ 诊断"
			}
			if b.Kind != "infra" {
				label = "Public task " + decimalOrdinal(ordinal+1)
			}
			if b.Mode == "scientific" {
				// Display only the public list position; real catalog ordinals
				// remain routing keys and private task identities stay private.
				label = "Public task " + decimalOrdinal(len(public)+1)
			}
			public = append(public, map[string]any{"ordinal": ordinal, "label": label})
		}
		if err = rows.Err(); err != nil {
			return nil, err
		}
		result["public_runs"] = public
	}
	return result, nil
}
func decimalOrdinal(n int) string { return strconv.Itoa(n) }

// batchTaskRuns exposes only bounded task state for the authenticated team's
// active evaluation. It never returns the private task identity, raw provider
// text, traces or object keys.
func batchTaskRuns(q queryer, b batch) ([]map[string]any, error) {
	rows, err := q.Query(`SELECT ordinal,public,state,attempt,COALESCE(error,''),reward,
       COALESCE(failure_category,''),COALESCE(failure_phase,''),COALESCE(failure_code,'')
       FROM runs WHERE batch_id=? ORDER BY ordinal`, b.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var ordinal, public, attempt int
		var state, reason, category, phase, code string
		var reward sql.NullInt64
		if err = rows.Scan(&ordinal, &public, &state, &attempt, &reason, &reward, &category, &phase, &code); err != nil {
			return nil, err
		}
		label := "Task " + decimalOrdinal(ordinal+1)
		if b.Kind == "public" {
			label = "Public task " + decimalOrdinal(ordinal+1)
		} else if b.Kind == "infra" {
			label = "Validation task"
		}
		categoryLabel, phaseLabel, errorCode, message := PublicFailureDetail(state, reason, category, phase, code)
		canCancel := state == "attention"
		canRetry := (state == "attention" || (state == "cancelled" && reason == "cancelled:operator")) && b.Status != "cancelling"
		task := map[string]any{
			"ordinal": ordinal, "label": label, "state": state, "attempt": attempt,
			"can_cancel": canCancel, "can_retry": canRetry,
		}
		if reward.Valid {
			task["reward"] = reward.Int64
		}
		if message != "" {
			task["error_category"] = categoryLabel
			task["error_phase"] = phaseLabel
			task["error_code"] = errorCode
			task["error_message"] = message
		}
		out = append(out, task)
	}
	return out, rows.Err()
}

func (s *Store) Confirm(token, versionID, kind, mode, idem string) (map[string]any, error) {
	return s.confirmIdentity(func(tx *sql.Tx) (User, error) { return s.session(tx, token) }, versionID, kind, mode, idem, "")
}
func (s *Store) confirmIdentity(identity func(*sql.Tx) (User, error), versionID, kind, mode, idem, expectedPhase string) (map[string]any, error) {
	var out map[string]any
	if _, ok := kinds[kind]; !ok || (mode != "simulated" && mode != "codex" && mode != "scientific-smoke" && mode != "scientific") || utf8.RuneCountInString(idem) < 1 || utf8.RuneCountInString(idem) > 120 {
		return nil, problem(400, "无效评测参数")
	}
	if kind == "elo" {
		return nil, problem(403, "Elo 评测由活动 worker 在最终前五名确定后自动排队")
	}
	err := s.write(func(tx *sql.Tx) error {
		u, err := identity(tx)
		if err != nil {
			return err
		}
		if u.Role != "team" || u.MustChange != 0 {
			return problem(403, "请先使用队伍账号完成改密")
		}
		old, err := batchRow(tx.QueryRow("SELECT "+batchFields+" FROM batches WHERE team_id=? AND idem=?", u.TeamID, idem))
		if err == nil {
			if old.VersionID != versionID || old.Kind != kind || old.Mode != mode {
				return problem(409, "幂等键已绑定其他请求")
			}
			out, err = batchView(tx, old)
			return err
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if CompetitionPortal() && mode != "scientific" {
			return problem(409, "Only scientific evaluations are accepted on the competition portal.")
		}
		var closed string
		if err = tx.QueryRow("SELECT value FROM settings WHERE key='competition_closed'").Scan(&closed); err != nil {
			return err
		}
		if closed == "true" {
			return problem(409, "评测受理已关闭")
		}
		v, err := versionRow(tx.QueryRow("SELECT "+versionFields+" FROM versions WHERE id=? AND team_id=?", versionID, u.TeamID))
		if errors.Is(err, sql.ErrNoRows) {
			return problem(404, "版本不存在")
		}
		if err != nil {
			return err
		}
		if v.Status != "approved" {
			return problem(409, "该版本尚未通过审查")
		}
		if err = s.CheckVersion(v.SHA256); err != nil {
			return err
		}
		if mode == "codex" && (kind != "infra" || !v.Reference) {
			return problem(409, "真实本地执行仅支持固定 Codex 参考包的 infra mock 题")
		}
		if mode == "scientific-smoke" && (kind != "infra" || !s.ScienceHashes[v.SHA256]) {
			return problem(409, "科学诊断仅支持已配置的三类官方原始模板，使用独立 Infra 练习额度，不参与排名")
		}
		var catalog ScientificTaskSet
		var catalogSHA string
		if mode == "scientific" {
			catalog, catalogSHA, err = activeTaskSet(tx)
			if err != nil {
				return err
			}
			if kind == "public" || kind == "infra" && catalog.native() {
				count := 0
				for _, task := range catalog.Tasks {
					if kind == "public" && task.Public || kind == "infra" && catalog.hasEvaluationRoles() && task.EvaluationRole == "infrastructure" || kind == "infra" && !catalog.hasEvaluationRoles() && task.Public {
						count++
					}
				}
				want := 5
				if kind == "infra" && catalog.hasEvaluationRoles() {
					want = 3
				}
				if count != want {
					return problem(409, "科学任务批次清单未配置完整，测试暂不可用")
				}
			}
		}
		var n int
		if err = tx.QueryRow("SELECT COUNT(*) FROM batches WHERE team_id=? AND status IN ('queued','testing','attention','cancelling')", u.TeamID).Scan(&n); err != nil {
			return err
		}
		if n > 0 {
			return problem(409, "本队已有未结束批次，请等待或取消")
		}
		quota, err := quotas(tx, u.TeamID)
		if err != nil {
			return err
		}
		if quota[kind] <= 0 {
			return problem(409, "该类型评测次数已用完")
		}
		calendar, err := loadCalendar(tx)
		if err != nil {
			return err
		}
		if _, err = s.observePhase(tx, calendar); err != nil {
			return err
		}
		if expectedPhase != "" && calendar.Phase(s.Now()).Name != expectedPhase {
			return problem(409, "The event phase changed before acceptance. No attempt was charged.")
		}
		if err = s.checkClockHealth(); err != nil {
			return err
		}
		accepted := s.Now()
		if mode == "scientific" && calendar.Phase(accepted).Name == "competition" && !catalog.native() {
			return problem(409, "Native per-task execution policy is not configured. No attempt charged.")
		}
		if mode == "scientific" && calendar.Phase(accepted).Name == "competition" {
			fp, e := formalExecution(tx)
			if e != nil {
				return e
			}
			if fp == nil {
				return problem(409, "Formal model configuration is not ready. No attempt charged.")
			}
		}
		if err = calendar.allow(kind, accepted); err != nil {
			return err
		}
		bid, now := id(), float64(accepted.UnixNano())/1e9
		if _, err = tx.Exec("INSERT INTO batches(id,team_id,version_id,kind,mode,idem,status,accepted_at) VALUES(?,?,?,?,?,?,'queued',?)", bid, u.TeamID, versionID, kind, mode, idem, now); err != nil {
			return err
		}
		catalogTarget := false
		if mode == "scientific-smoke" {
			var pinned string
			err = tx.QueryRow("SELECT submission_sha256 FROM scientific_targets WHERE version_id=?", v.ID).Scan(&pinned)
			if err != nil && !errors.Is(err, sql.ErrNoRows) {
				return err
			}
			if err == nil {
				if pinned != v.SHA256 {
					return problem(409, "验收绑定与提交摘要不匹配")
				}
				release, releaseErr := scientificRelease(tx)
				if releaseErr != nil {
					return releaseErr
				}
				if _, err = tx.Exec("INSERT INTO batch_scientific_targets SELECT ?,submission_sha256,task_index,?,agent_budget_seconds FROM scientific_targets WHERE version_id=?", bid, release, v.ID); err != nil {
					return err
				}
				catalogTarget = true
			}
		}
		if mode == "scientific" {
			if _, err = tx.Exec("INSERT INTO batch_scientific_task_sets VALUES(?,?)", bid, catalogSHA); err != nil {
				return err
			}
		}
		if err = s.pinBatchPolicy(tx, bid, calendar, v, kind, mode); err != nil {
			return err
		}
		count := kinds[kind]
		if mode == "scientific" {
			count = len(catalog.selected(kind))
		}
		for i := 0; i < count; i++ {
			public := 0
			if i < 5 && !catalogTarget {
				public = 1
			}
			var task ScientificTask
			if mode == "scientific" {
				selected := catalog.selected(kind)
				task = selected[i]
				public = 0
				if task.Public {
					public = 1
				}
			}
			runID := id()
			if _, err = tx.Exec("INSERT INTO runs(id,batch_id,ordinal,public,state) VALUES(?,?,?,?,'queued')", runID, bid, i, public); err != nil {
				return err
			}
			if mode == "scientific" {
				if _, err = tx.Exec("INSERT INTO run_scientific_targets VALUES(?,?,?,?,?,?,?,?)", runID, catalogSHA, v.SHA256, task.TaskIndex, task.TaskID, public, catalog.ReleaseID, catalog.budget(task, calendar.Phase(accepted).Name, kind)); err != nil {
					return err
				}
			}
		}
		if _, err = tx.Exec("INSERT INTO ledger(id,team_id,kind,delta,action,batch_id,reason,created) VALUES(?,?,?,-1,'accepted',?,'确认评测；取消不退次',?)", id(), u.TeamID, kind, bid, now); err != nil {
			return err
		}
		if _, err = tx.Exec("INSERT INTO outbox(id,event,team_id,payload,created) VALUES(?,'evaluation_accepted',?,?,?)", id(), u.TeamID, `{"batch_id":"`+bid+`","kind":"`+kind+`"}`, now); err != nil {
			return err
		}
		out, err = batchView(tx, batch{ID: bid, TeamID: u.TeamID, VersionID: versionID, Kind: kind, Mode: mode, Idem: idem, Status: "queued", AcceptedAt: now})
		return err
	})
	return out, err
}
func (s *Store) GetBatch(token, bid string) (map[string]any, error) {
	u, err := s.Session(token)
	if err != nil {
		return nil, err
	}
	if u.MustChange != 0 {
		return nil, problem(403, "请先修改初始密码")
	}
	b, err := batchRow(s.DB.QueryRow("SELECT "+batchFields+" FROM batches WHERE id=? AND team_id=?", bid, u.TeamID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, problem(404, "评测不存在")
	}
	if err != nil {
		return nil, err
	}
	return batchView(s.DB, b)
}
func (s *Store) Batches(token string) ([]map[string]any, error) {
	u, err := s.Session(token)
	if err != nil {
		return nil, err
	}
	if u.MustChange != 0 {
		return nil, problem(403, "请先修改初始密码")
	}
	rows, err := s.DB.Query("SELECT "+batchFields+" FROM batches WHERE team_id=? ORDER BY accepted_at DESC", u.TeamID)
	if err != nil {
		return nil, err
	}
	bs := []batch{}
	for rows.Next() {
		b, e := batchRow(rows)
		if e != nil {
			rows.Close()
			return nil, e
		}
		bs = append(bs, b)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	out := []map[string]any{}
	for _, b := range bs {
		v, e := batchView(s.DB, b)
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, nil
}
func (s *Store) Cancel(token, bid string) error {
	return s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.MustChange != 0 {
			return problem(403, "请先修改初始密码")
		}
		var state string
		if err = tx.QueryRow("SELECT status FROM batches WHERE id=? AND team_id=?", bid, u.TeamID).Scan(&state); errors.Is(err, sql.ErrNoRows) {
			return problem(404, "评测不存在")
		} else if err != nil {
			return err
		}
		if !strings.Contains("|queued|testing|attention|cancelling|", "|"+state+"|") {
			return nil
		}
		// A queued batch has not entered E2B yet. Return its admission debit
		// exactly once, so a participant can withdraw before execution without
		// consuming an attempt. Once any task is running/uploading, cancellation
		// keeps the original debit and follows the normal cleanup path.
		var running, uncertain int
		if err = tx.QueryRow("SELECT COUNT(CASE WHEN state IN ('running','uploading') THEN 1 END),COUNT(CASE WHEN state='attention' AND error=? THEN 1 END) FROM runs WHERE batch_id=?", lostWorkerError, bid).Scan(&running, &uncertain); err != nil {
			return err
		}
		refund := state == "queued" && running == 0 && uncertain == 0
		if _, err = tx.Exec("UPDATE runs SET state='cancelled' WHERE batch_id=? AND state IN ('queued','attention') AND COALESCE(error,'')!=?", bid, lostWorkerError); err != nil {
			return err
		}
		if err = s.cancelConfirmedLostRuns(tx, bid); err != nil {
			return err
		}
		var active int
		if err = tx.QueryRow("SELECT COUNT(*) FROM runs WHERE batch_id=? AND (state IN ('running','uploading') OR (state='attention' AND error=?))", bid, lostWorkerError).Scan(&active); err != nil {
			return err
		}
		next := "cancelled"
		if active > 0 {
			next = "cancelling"
		}
		if _, err = tx.Exec("UPDATE batches SET status=? WHERE id=?", next, bid); err != nil {
			return err
		}
		if refund {
			if _, err = tx.Exec("INSERT INTO ledger(id,team_id,kind,delta,action,batch_id,reason,created) SELECT ?,team_id,kind,1,'cancel_refund',?, 'Cancelled before E2B admission; attempt returned.',? FROM batches WHERE id=?", id(), bid, s.now(), bid); err != nil {
				return err
			}
		}
		_, err = tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id IN (SELECT id FROM runs WHERE batch_id=?)", bid)
		return err
	})
}

// cancelConfirmedLostRuns releases worker-lost scientific runs only when the
// trusted controller receipt already proves every created sandbox is gone.
// A missing/foreign/uncertain receipt remains in attention for provider-side
// reconciliation; cancellation must never release genuinely unknown capacity.
func (s *Store) cancelConfirmedLostRuns(tx *sql.Tx, bid string) error {
	rows, err := tx.Query(`SELECT r.id,r.attempt,b.mode FROM runs r JOIN batches b ON b.id=r.batch_id
WHERE r.batch_id=? AND r.state='attention' AND r.error=?`, bid, lostWorkerError)
	if err != nil {
		return err
	}
	type lost struct {
		runID   string
		attempt int
		mode    string
	}
	var candidates, confirmed []lost
	for rows.Next() {
		var r lost
		if err := rows.Scan(&r.runID, &r.attempt, &r.mode); err != nil {
			rows.Close()
			return err
		}
		candidates = append(candidates, r)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	for _, r := range candidates {
		lease := Lease{RunID: r.runID, BatchID: bid, Attempt: r.attempt, Mode: r.mode}
		if s.scientificCleanupConfirmedFor(tx, lease) == nil {
			confirmed = append(confirmed, r)
		}
	}
	for _, r := range confirmed {
		if _, err := tx.Exec("UPDATE runs SET state='cancelled',error='platform:reconciled',owner=NULL,lease_until=NULL WHERE id=?", r.runID); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE attempts SET state='interrupted',finished=?,error='platform:reconciled' WHERE run_id=? AND number=?", s.now(), r.runID, r.attempt); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", r.runID); err != nil {
			return err
		}
	}
	return nil
}
