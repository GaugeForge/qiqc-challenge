package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
)

// CancelTask closes one attention task without touching sibling running tasks
// and without turning the whole batch into "cancelling". The trusted cleanup
// receipt must prove that every created remote resource is gone; a zero-resource
// startup failure is also safe. Unknown lost-worker resources stay blocked.
func (s *Store) CancelTask(token, bid string, ordinal int, ip string) error {
	return s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.MustChange != 0 {
			return problem(403, "请先修改初始密码")
		}
		cancelled, err := s.cancelTaskTx(tx, u, bid, ordinal, u.Email, "Team requested cancellation of an attention task.", ip, token, false)
		if err != nil {
			return err
		}
		if !cancelled {
			return problem(409, "远端资源清理仍在确认中，暂不能取消该任务；确认完成后可重试")
		}
		return nil
	})
}

// RetryTask queues one attention task, or one task the team explicitly
// cancelled, without affecting any running siblings. A batch-level cancellation
// is never resurrected.
func (s *Store) RetryTask(token, bid string, ordinal int, ip string) error {
	return s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.MustChange != 0 {
			return problem(403, "请先修改初始密码")
		}
		retried, err := s.retryTaskTx(tx, u, bid, ordinal, u.Email, "Team requested retry of an attention task.", ip, token, false, false)
		if err != nil {
			return err
		}
		if !retried {
			return problem(409, "该任务当前不能重试，或远端资源清理仍在确认中")
		}
		return nil
	})
}

// CancelAttentionTasks closes only the tasks currently waiting for attention.
// Running and queued siblings are deliberately left alone.
func (s *Store) CancelAttentionTasks(token, bid, ip string) (map[string]any, error) {
	result := map[string]any{"cancelled": 0, "blocked": 0, "remaining_attention": 0}
	err := s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.MustChange != 0 {
			return problem(403, "请先修改初始密码")
		}
		if err = s.requireTaskBatch(tx, u, bid, false); err != nil {
			return err
		}
		rows, err := tx.Query("SELECT ordinal FROM runs WHERE batch_id=? AND state='attention' ORDER BY ordinal", bid)
		if err != nil {
			return err
		}
		ordinals := []int{}
		for rows.Next() {
			var ordinal int
			if err = rows.Scan(&ordinal); err != nil {
				rows.Close()
				return err
			}
			ordinals = append(ordinals, ordinal)
		}
		if err = rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		for _, ordinal := range ordinals {
			cancelled, err := s.cancelTaskTx(tx, u, bid, ordinal, u.Email, "Team requested cancellation of all attention tasks.", ip, token, false)
			if err != nil {
				return err
			}
			if cancelled {
				result["cancelled"] = result["cancelled"].(int) + 1
			} else {
				result["blocked"] = result["blocked"].(int) + 1
			}
		}
		var remaining int
		if err = tx.QueryRow("SELECT COUNT(*) FROM runs WHERE batch_id=? AND state='attention'", bid).Scan(&remaining); err != nil {
			return err
		}
		result["remaining_attention"] = remaining
		if result["cancelled"].(int) == 0 && result["blocked"].(int) > 0 {
			return problem(409, "远端资源清理仍在确认中，暂不能取消这些任务；确认完成后可重试")
		}
		return nil
	})
	return result, err
}

// RetryAttentionTasks requeues the attention tasks and tasks the team itself
// cancelled. It is the explicit "retry only the affected tasks" action; a
// whole-batch cancellation is never resurrected.
func (s *Store) RetryAttentionTasks(token, bid, ip string) (map[string]any, error) {
	result := map[string]any{"retried": 0, "skipped": 0, "remaining_attention": 0}
	err := s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.MustChange != 0 {
			return problem(403, "请先修改初始密码")
		}
		if err = s.requireTaskBatch(tx, u, bid, false); err != nil {
			return err
		}
		rows, err := tx.Query(`SELECT ordinal FROM runs
WHERE batch_id=? AND (state='attention' OR (state='cancelled' AND error='cancelled:operator'))
ORDER BY ordinal`, bid)
		if err != nil {
			return err
		}
		ordinals := []int{}
		for rows.Next() {
			var ordinal int
			if err = rows.Scan(&ordinal); err != nil {
				rows.Close()
				return err
			}
			ordinals = append(ordinals, ordinal)
		}
		if err = rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		for _, ordinal := range ordinals {
			retried, err := s.retryTaskTx(tx, u, bid, ordinal, u.Email, "Team requested retry of all attention tasks.", ip, token, false, false)
			if err != nil {
				return err
			}
			if retried {
				result["retried"] = result["retried"].(int) + 1
			} else {
				result["skipped"] = result["skipped"].(int) + 1
			}
		}
		var remaining int
		if err = tx.QueryRow("SELECT COUNT(*) FROM runs WHERE batch_id=? AND state='attention'", bid).Scan(&remaining); err != nil {
			return err
		}
		result["remaining_attention"] = remaining
		return nil
	})
	return result, err
}

func (s *Store) requireTaskBatch(tx *sql.Tx, u User, bid string, allowAnyTeam bool) error {
	var team, status string
	if err := tx.QueryRow("SELECT team_id,status FROM batches WHERE id=?", bid).Scan(&team, &status); errors.Is(err, sql.ErrNoRows) {
		return problem(404, "评测不存在")
	} else if err != nil {
		return err
	}
	if !allowAnyTeam && team != u.TeamID {
		return problem(404, "评测不存在")
	}
	if !oneOf(status, "testing", "attention", "cancelling") {
		return problem(409, "该评测已结束，不能操作单题")
	}
	return nil
}

func (s *Store) cancelTaskTx(tx *sql.Tx, u User, bid string, ordinal int, actor, reason, ip, token string, allowAnyTeam bool) (bool, error) {
	var batchStatus, team, mode string
	if err := tx.QueryRow("SELECT status,team_id,mode FROM batches WHERE id=?", bid).Scan(&batchStatus, &team, &mode); errors.Is(err, sql.ErrNoRows) {
		return false, problem(404, "评测不存在")
	} else if err != nil {
		return false, err
	}
	if !allowAnyTeam && team != u.TeamID {
		return false, problem(404, "评测不存在")
	}
	if !oneOf(batchStatus, "testing", "attention", "cancelling") {
		return false, problem(409, "该评测已结束，不能取消单题")
	}
	var runID, state, runError string
	var attempt int
	if err := tx.QueryRow("SELECT id,state,attempt,COALESCE(error,'') FROM runs WHERE batch_id=? AND ordinal=?", bid, ordinal).Scan(&runID, &state, &attempt, &runError); errors.Is(err, sql.ErrNoRows) {
		return false, problem(404, "题目不存在")
	} else if err != nil {
		return false, err
	}
	if state != "attention" {
		return false, problem(409, "只能取消待处理的题目")
	}
	if !s.taskReleaseConfirmed(tx, bid, runID, attempt, mode) {
		return false, nil
	}
	before, _ := json.Marshal(map[string]any{"state": state, "attempt": attempt, "error": runError})
	if _, err := tx.Exec("UPDATE runs SET state='cancelled',error='cancelled:operator',owner=NULL,lease_until=NULL WHERE id=? AND state='attention'", runID); err != nil {
		return false, err
	}
	if _, err := tx.Exec("UPDATE attempts SET state='interrupted',finished=?,error='platform:cancelled' WHERE run_id=? AND number=?", s.now(), runID, attempt); err != nil {
		return false, err
	}
	if _, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", runID); err != nil {
		return false, err
	}
	if err := s.refreshBatchAfterTaskAction(tx, bid); err != nil {
		return false, err
	}
	after, _ := json.Marshal(map[string]any{"state": "cancelled", "attempt": attempt, "error": "cancelled:operator"})
	return true, s.teamAudit(tx, u, token, ip, actor, "cancel_task", runID, reason, string(before), string(after))
}

func (s *Store) retryTaskTx(tx *sql.Tx, u User, bid string, ordinal int, actor, reason, ip, token string, allowAnyTeam, force bool) (bool, error) {
	var batchStatus, team, mode string
	if err := tx.QueryRow("SELECT status,team_id,mode FROM batches WHERE id=?", bid).Scan(&batchStatus, &team, &mode); errors.Is(err, sql.ErrNoRows) {
		return false, problem(404, "评测不存在")
	} else if err != nil {
		return false, err
	}
	if !allowAnyTeam && team != u.TeamID {
		return false, problem(404, "评测不存在")
	}
	if batchStatus == "cancelling" || batchStatus == "completed" || batchStatus == "cancelled" {
		return false, problem(409, "该评测正在取消或已结束，不能重试单题")
	}
	if !oneOf(batchStatus, "testing", "attention") {
		return false, problem(409, "该评测当前不能重试单题")
	}
	var runID, state, runError string
	var attempt int
	if err := tx.QueryRow("SELECT id,state,attempt,COALESCE(error,'') FROM runs WHERE batch_id=? AND ordinal=?", bid, ordinal).Scan(&runID, &state, &attempt, &runError); errors.Is(err, sql.ErrNoRows) {
		return false, problem(404, "题目不存在")
	} else if err != nil {
		return false, err
	}
	retryable := state == "attention" || (state == "cancelled" && runError == "cancelled:operator")
	if !retryable {
		return false, nil
	}
	if !force && !s.taskReleaseConfirmed(tx, bid, runID, attempt, mode) {
		return false, nil
	}
	before, _ := json.Marshal(map[string]any{"state": state, "attempt": attempt, "error": runError})
	if _, err := tx.Exec("UPDATE runs SET state='queued',owner=NULL,lease_until=0,error=NULL,failure_category=NULL,failure_phase=NULL,failure_code=NULL WHERE id=? AND state=?", runID, state); err != nil {
		return false, err
	}
	if err := s.refreshBatchAfterTaskAction(tx, bid); err != nil {
		return false, err
	}
	after, _ := json.Marshal(map[string]any{"state": "queued", "attempt": attempt, "error": nil, "force": force})
	return true, s.teamAudit(tx, u, token, ip, actor, "retry_task", runID, reason, string(before), string(after))
}

// refreshBatchAfterTaskAction keeps a partially attended evaluation visible
// without freezing unrelated dispatch. A batch remains "attention" while any
// task still needs attention; otherwise it returns to "testing" so queued
// retries can be claimed. A batch cancellation is left to its own finalizer.
func (s *Store) refreshBatchAfterTaskAction(tx *sql.Tx, bid string) error {
	var status string
	if err := tx.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&status); err != nil {
		return err
	}
	if status == "cancelling" {
		var active int
		if err := tx.QueryRow("SELECT COUNT(*) FROM runs WHERE batch_id=? AND state IN ('running','uploading','attention')", bid).Scan(&active); err != nil {
			return err
		}
		if active == 0 {
			_, err := tx.Exec("UPDATE batches SET status='cancelled' WHERE id=?", bid)
			return err
		}
		return nil
	}
	if status == "completed" || status == "cancelled" {
		return nil
	}
	var attention, active int
	if err := tx.QueryRow("SELECT COUNT(CASE WHEN state='attention' THEN 1 END),COUNT(CASE WHEN state IN ('running','uploading','queued') THEN 1 END) FROM runs WHERE batch_id=?", bid).Scan(&attention, &active); err != nil {
		return err
	}
	next := "testing"
	if attention > 0 {
		next = "attention"
	}
	_, err := tx.Exec("UPDATE batches SET status=? WHERE id=? AND status NOT IN ('completed','cancelled')", next, bid)
	return err
}

type privateScientificReceipt struct {
	RunID           string            `json:"run_id"`
	Attempt         int               `json:"attempt"`
	Mode            string            `json:"mode"`
	Phase           string            `json:"phase"`
	ControllerError string            `json:"controller_error_class"`
	Resources       map[string]string `json:"resources"`
}

// taskReleaseConfirmed accepts either the normal cleanup guard (one E2B VM
// destroyed) or a trusted zero-resource startup receipt. It never releases an
// unknown lost-worker sandbox.
func (s *Store) taskReleaseConfirmed(tx *sql.Tx, bid, runID string, attempt int, mode string) bool {
	if mode != "scientific" {
		return true
	}
	lease := Lease{RunID: runID, BatchID: bid, Attempt: attempt, Mode: mode}
	if s.scientificCleanupConfirmedFor(tx, lease) == nil {
		return true
	}
	var receipt privateScientificReceipt
	path := filepath.Join(s.Root, "scientific", runID+"_"+strconv.Itoa(attempt)+".json")
	if readPrivateRecoveryJSON(path, &receipt) != nil ||
		receipt.RunID != runID || receipt.Attempt != attempt || receipt.Mode != mode ||
		receipt.Phase != "starting" || len(receipt.Resources) != 0 || receipt.ControllerError == "" {
		return false
	}
	return true
}

func (s *Store) teamAudit(tx *sql.Tx, u User, token, ip, actor, action, target, reason, before, after string) error {
	if actor == "" {
		actor = u.Email
	}
	if len([]rune(reason)) < 3 {
		reason = fmt.Sprintf("%s by %s", action, actor)
	}
	fingerprint := ""
	if token != "" {
		fingerprint = tokenHash(token)[:16]
	}
	_, err := tx.Exec(`INSERT INTO audit(id,created,user_id,actor,action,object_id,reason,before_json,after_json,session_fingerprint,ip) VALUES(?,?,?,?,?,?,?,?,?,?,?)`,
		id(), s.now(), u.ID, actor, action, target, reason, before, after, fingerprint, ip)
	return err
}
