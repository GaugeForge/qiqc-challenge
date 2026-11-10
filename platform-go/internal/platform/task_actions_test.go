package platform

import (
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func taskActionFixture(t *testing.T, s *Store, states ...string) (string, string) {
	t.Helper()
	token := readyUser(t, s, "task-actions-"+id()+"@example.test", "team")
	u, err := s.Session(token)
	if err != nil {
		t.Fatal(err)
	}
	vid, bid := id(), id()
	if _, err = s.DB.Exec("INSERT INTO versions(id,team_id,sha256,status) VALUES(?,?,?,'approved')", vid, u.TeamID, strings.Repeat("a", 64)); err != nil {
		t.Fatal(err)
	}
	if _, err = s.DB.Exec("INSERT INTO batches(id,team_id,version_id,kind,mode,idem,status,accepted_at,last_dispatch) VALUES(?,?,?,'public','simulated',?,'attention',?,0)", bid, u.TeamID, vid, id(), s.now()); err != nil {
		t.Fatal(err)
	}
	for ordinal, state := range states {
		owner, lease := any(nil), any(nil)
		if state == "running" {
			owner = "fixture-worker"
			lease = s.now() + 600
		}
		if _, err = s.DB.Exec("INSERT INTO runs(id,batch_id,ordinal,public,state,attempt,owner,lease_until) VALUES(?,?,?,1,?,1,?,?)", id(), bid, ordinal, state, owner, lease); err != nil {
			t.Fatal(err)
		}
		if state != "queued" && state != "cancelled" {
			if _, err = s.DB.Exec("INSERT INTO attempts(id,run_id,number,state,started) SELECT ?,id,1,'failed',? FROM runs WHERE batch_id=? AND ordinal=?", id(), s.now(), bid, ordinal); err != nil {
				t.Fatal(err)
			}
		}
	}
	return token, bid
}

func runState(t *testing.T, s *Store, bid string, ordinal int) string {
	t.Helper()
	var state string
	if err := s.DB.QueryRow("SELECT state FROM runs WHERE batch_id=? AND ordinal=?", bid, ordinal).Scan(&state); err != nil {
		t.Fatal(err)
	}
	return state
}

func TestTaskCancelAndRetryLeaveRunningSiblingsAlone(t *testing.T) {
	s := testStore(t)
	token, bid := taskActionFixture(t, s, "attention", "running", "queued")
	if err := s.CancelTask(token, bid, 0, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	if runState(t, s, bid, 0) != "cancelled" || runState(t, s, bid, 1) != "running" || runState(t, s, bid, 2) != "queued" {
		t.Fatal("single-task cancellation touched a sibling")
	}
	var status string
	s.DB.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&status)
	if status != "testing" {
		t.Fatalf("batch should keep running siblings, got %s", status)
	}
	if err := s.RetryTask(token, bid, 0, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	if runState(t, s, bid, 0) != "queued" || runState(t, s, bid, 1) != "running" {
		t.Fatal("task retry touched a sibling")
	}
}

func TestBulkAttentionCancelAndRetry(t *testing.T) {
	s := testStore(t)
	token, bid := taskActionFixture(t, s, "attention", "attention", "running")
	result, err := s.CancelAttentionTasks(token, bid, "127.0.0.1")
	if err != nil || result["cancelled"] != 2 {
		t.Fatalf("bulk cancel: %#v %v", result, err)
	}
	if runState(t, s, bid, 2) != "running" {
		t.Fatal("bulk cancel touched a running sibling")
	}
	result, err = s.RetryAttentionTasks(token, bid, "127.0.0.1")
	if err != nil || result["retried"] != 2 {
		t.Fatalf("bulk retry: %#v %v", result, err)
	}
	if runState(t, s, bid, 0) != "queued" || runState(t, s, bid, 1) != "queued" {
		t.Fatal("bulk retry did not queue the affected tasks")
	}
}

func TestClaimDispatchesQueuedSiblingWhileBatchAttention(t *testing.T) {
	s := testStore(t)
	now := time.Unix(2000, 0)
	s.Now = func() time.Time { return now }
	token, bid := taskActionFixture(t, s, "attention", "attention", "queued")
	if err := s.RetryTask(token, bid, 0, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	var status string
	s.DB.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&status)
	if status != "attention" {
		t.Fatalf("remaining attention task should stay visible, got %s", status)
	}
	lease, err := s.Claim("attention-sibling-worker", 2)
	if err != nil || lease == nil || lease.BatchID != bid {
		t.Fatalf("queued sibling was not claimable while batch attention: %#v %v", lease, err)
	}
	s.DB.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&status)
	if status != "testing" {
		t.Fatalf("claimed batch did not return to testing, got %s", status)
	}
}

func TestCancellingLastOutstandingTaskCompletesBatch(t *testing.T) {
	s := testStore(t)
	token, bid := taskActionFixture(t, s, "attention", "completed", "completed", "completed", "completed")
	for ordinal := 1; ordinal < 5; ordinal++ {
		if _, err := s.DB.Exec("UPDATE runs SET reward=1,seconds='100.000000',object_key=?,object_sha=? WHERE batch_id=? AND ordinal=?", "runs/"+id()+".tar.gz", strings.Repeat("b", 64), bid, ordinal); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.CancelTask(token, bid, 0, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	var status, mean string
	var passed int
	if err := s.DB.QueryRow("SELECT status,passed,COALESCE(mean,'') FROM batches WHERE id=?", bid).Scan(&status, &passed, &mean); err != nil {
		t.Fatal(err)
	}
	if status != "completed" || passed != 4 || mean != "100.000000" {
		t.Fatalf("cancelled forfeit did not complete batch: status=%s passed=%d mean=%s", status, passed, mean)
	}
	if runState(t, s, bid, 0) != "cancelled" {
		t.Fatal("cancelled task was resurrected")
	}
}

func TestCancellingAllTasksCompletesZeroScoreBatch(t *testing.T) {
	s := testStore(t)
	token, bid := taskActionFixture(t, s, "attention", "attention", "attention", "attention", "attention")
	for ordinal := 0; ordinal < 5; ordinal++ {
		if err := s.CancelTask(token, bid, ordinal, "127.0.0.1"); err != nil {
			t.Fatal(err)
		}
	}
	var status, mean string
	var passed int
	if err := s.DB.QueryRow("SELECT status,passed,COALESCE(mean,'') FROM batches WHERE id=?", bid).Scan(&status, &passed, &mean); err != nil {
		t.Fatal(err)
	}
	if status != "completed" || passed != 0 || mean != "0.000000" {
		t.Fatalf("all-cancelled batch did not close: status=%s passed=%d mean=%s", status, passed, mean)
	}
}

func TestCancellingTaskWithRunningSiblingKeepsBatchOpen(t *testing.T) {
	s := testStore(t)
	token, bid := taskActionFixture(t, s, "attention", "running", "completed", "completed", "completed")
	if err := s.CancelTask(token, bid, 0, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	var status string
	if err := s.DB.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "testing" {
		t.Fatalf("batch with a running sibling must stay open, got %s", status)
	}
}

func TestFinalizeTerminalBatchesClosesCancelledForfeit(t *testing.T) {
	s := testStore(t)
	_, bid := taskActionFixture(t, s, "cancelled", "completed", "completed", "completed", "completed")
	if _, err := s.DB.Exec("UPDATE runs SET error='cancelled:operator' WHERE batch_id=? AND ordinal=0", bid); err != nil {
		t.Fatal(err)
	}
	for ordinal := 1; ordinal < 5; ordinal++ {
		if _, err := s.DB.Exec("UPDATE runs SET reward=1,seconds='100.000000',object_key=?,object_sha=? WHERE batch_id=? AND ordinal=?", "runs/"+id()+".tar.gz", strings.Repeat("b", 64), bid, ordinal); err != nil {
			t.Fatal(err)
		}
	}
	n, err := s.FinalizeTerminalBatches()
	if err != nil || n != 1 {
		t.Fatalf("finalize terminal batches: %d %v", n, err)
	}
	var status string
	if err := s.DB.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "completed" {
		t.Fatalf("terminal batch not completed, got %s", status)
	}
}

func TestPlatformCancelledRunDoesNotCloseBatch(t *testing.T) {
	s := testStore(t)
	_, bid := taskActionFixture(t, s, "cancelled", "completed", "completed", "completed", "completed")
	for ordinal := 1; ordinal < 5; ordinal++ {
		if _, err := s.DB.Exec("UPDATE runs SET reward=1,seconds='100.000000',object_key=?,object_sha=? WHERE batch_id=? AND ordinal=?", "runs/"+id()+".tar.gz", strings.Repeat("b", 64), bid, ordinal); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := s.DB.Exec("UPDATE runs SET error='platform:reconciled' WHERE batch_id=? AND ordinal=0", bid); err != nil {
		t.Fatal(err)
	}
	if err := s.write(func(tx *sql.Tx) error { return s.finalize(tx, bid) }); err != nil {
		t.Fatal(err)
	}
	var status string
	if err := s.DB.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status == "completed" {
		t.Fatal("platform reconciliation must not be treated as a team forfeit")
	}
}

func TestTaskCancelRejectsUnconfirmedRemoteResource(t *testing.T) {
	s := testStore(t)
	token, bid := taskActionFixture(t, s, "attention")
	if _, err := s.DB.Exec("UPDATE batches SET mode='scientific' WHERE id=?", bid); err != nil {
		t.Fatal(err)
	}
	if err := s.CancelTask(token, bid, 0, "127.0.0.1"); status(err) != 409 {
		t.Fatal("unknown remote resource was released", err)
	}
	if runState(t, s, bid, 0) != "attention" {
		t.Fatal("blocked cancellation changed the task")
	}
}

func TestPreflightReceiptAllowsCancelWithoutRemoteResource(t *testing.T) {
	s := testStore(t)
	// A second attention task keeps finalize from looking up the scientific
	// catalog, which this fixture intentionally does not configure.
	token, bid := taskActionFixture(t, s, "attention", "attention")
	if _, err := s.DB.Exec("UPDATE batches SET mode='scientific' WHERE id=?", bid); err != nil {
		t.Fatal(err)
	}
	var runID string
	if err := s.DB.QueryRow("SELECT id FROM runs WHERE batch_id=? AND ordinal=0", bid).Scan(&runID); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(s.Root, "scientific", runID+"_1.json")
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatal(err)
	}
	value := map[string]any{"run_id": runID, "attempt": 1, "mode": "scientific", "phase": "starting", "controller_error_class": "executor_preflight", "resources": map[string]string{}}
	raw, _ := json.Marshal(value)
	if err := os.WriteFile(path, raw, 0o640); err != nil {
		t.Fatal(err)
	}
	if err := s.CancelTask(token, bid, 0, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	if runState(t, s, bid, 0) != "cancelled" {
		t.Fatal("preflight task not cancelled")
	}
}
