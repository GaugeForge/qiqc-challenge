package platform

import (
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
