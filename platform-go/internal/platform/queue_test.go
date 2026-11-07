package platform

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"
)

func queueBatch(t *testing.T, s *Store, n int, mode string) string {
	t.Helper()
	team, version, batch := id(), id(), id()
	for _, q := range []struct {
		sql  string
		args []any
	}{
		{"INSERT INTO teams VALUES(?,?)", []any{team, "Synthetic team"}},
		{"INSERT INTO versions(id,team_id,sha256,status) VALUES(?,?,?,'approved')", []any{version, team, "synthetic-sha"}},
		{"INSERT INTO batches(id,team_id,version_id,kind,mode,idem,status,accepted_at,last_dispatch) VALUES(?,?,?,'public',?,?,'queued',?,0)", []any{batch, team, version, mode, id(), s.now()}},
	} {
		if _, err := s.DB.Exec(q.sql, q.args...); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < n; i++ {
		if _, err := s.DB.Exec("INSERT INTO runs(id,batch_id,ordinal,public,state,attempt) VALUES(?,?,?,1,'queued',0)", id(), batch, i); err != nil {
			t.Fatal(err)
		}
	}
	return batch
}

func queueKindBatch(t *testing.T, s *Store, kind string, n int, mode string) string {
	t.Helper()
	team, version, batch := id(), id(), id()
	if _, err := s.DB.Exec("INSERT INTO teams VALUES(?,?)", team, "Synthetic "+kind+" team"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.DB.Exec("INSERT INTO versions(id,team_id,sha256,status) VALUES(?,?,?,'approved')", version, team, "synthetic-"+kind+"-sha"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.DB.Exec("INSERT INTO batches(id,team_id,version_id,kind,mode,idem,status,accepted_at,last_dispatch) VALUES(?,?,?,?,?,?,'queued',?,0)", batch, team, version, kind, mode, id(), s.now()); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < n; i++ {
		if _, err := s.DB.Exec("INSERT INTO runs(id,batch_id,ordinal,public,state,attempt) VALUES(?,?,?,0,'queued',0)", id(), batch, i); err != nil {
			t.Fatal(err)
		}
	}
	return batch
}

func mustClaim(t *testing.T, s *Store, owner string, slots int) *Lease {
	t.Helper()
	lease, err := s.Claim(owner, slots)
	if err != nil || lease == nil {
		t.Fatalf("claim: %v %#v", err, lease)
	}
	return lease
}

func TestQueueConcurrentStoresRespectGlobalSlots(t *testing.T) {
	s := testStore(t)
	queueBatch(t, s, 49, "simulated")
	second, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	var wg sync.WaitGroup
	claims := make(chan *Lease, 64)
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			store := s
			if i%2 == 0 {
				store = second
			}
			lease, err := store.Claim(fmt.Sprintf("worker-%d", i), 4)
			if err != nil {
				t.Error(err)
			}
			if lease != nil {
				claims <- lease
			}
		}(i)
	}
	wg.Wait()
	close(claims)
	seen := map[string]bool{}
	for lease := range claims {
		if seen[lease.RunID] {
			t.Fatal("duplicate claim")
		}
		seen[lease.RunID] = true
	}
	if len(seen) != 4 {
		t.Fatalf("claimed %d, expected four global task slots", len(seen))
	}
	var attempts int
	s.DB.QueryRow("SELECT COUNT(*) FROM attempts").Scan(&attempts)
	if attempts != 4 {
		t.Fatal("non-atomic attempts")
	}
}

func TestQueuePoolsDoNotCrossClaim(t *testing.T) {
	s := testStore(t)
	queueBatch(t, s, 1, "simulated")
	formal := queueKindBatch(t, s, "full", 1, "scientific")
	publicLease := mustClaim(t, s, "key2-worker", 2)
	if publicLease.Kind != "public" {
		t.Fatalf("key2 pool claimed %s", publicLease.Kind)
	}
	formalLease, err := s.ClaimPool("key1-worker", 2, "key1")
	if err != nil || formalLease == nil || formalLease.BatchID != formal {
		t.Fatalf("key1 pool claim = %#v, %v", formalLease, err)
	}
	if _, err := s.ClaimPool("bad-worker", 1, "unknown"); status(err) != 400 {
		t.Fatalf("invalid pool status=%d", status(err))
	}
}

func TestQueueRoundRobinEvenAtFixedClock(t *testing.T) {
	s := testStore(t)
	s.Now = func() time.Time { return time.Unix(1000, 0) }
	a := queueBatch(t, s, 3, "simulated")
	b := queueBatch(t, s, 3, "simulated")
	counts := map[string]int{}
	for i := 0; i < 6; i++ {
		lease := mustClaim(t, s, "worker", 6)
		counts[lease.BatchID]++
		if delta := counts[a] - counts[b]; delta > 1 || delta < -1 {
			t.Fatal("one batch starved")
		}
	}
}

func TestQueuePauseAndClosedAcceptanceAreDifferent(t *testing.T) {
	s := testStore(t)
	queueBatch(t, s, 3, "simulated")
	s.DB.Exec("UPDATE settings SET value='true' WHERE key='competition_closed'")
	mustClaim(t, s, "drain-accepted", 4)
	s.DB.Exec("UPDATE settings SET value='true' WHERE key='paused'")
	if lease, err := s.Claim("paused", 4); err != nil || lease != nil {
		t.Fatal("pause ignored")
	}
	s.DB.Exec("UPDATE settings SET value='not-json' WHERE key='paused'")
	if _, err := s.Claim("bad-state", 4); err == nil {
		t.Fatal("malformed pause state accepted")
	}
	s.DB.Exec("UPDATE settings SET value='null' WHERE key='paused'")
	if _, err := s.Claim("bad-null", 4); err == nil {
		t.Fatal("null pause state accepted")
	}
	if _, err := s.Claim("", 4); status(err) != 400 {
		t.Fatal("empty worker accepted")
	}
	if _, err := s.Claim("worker", 0); status(err) != 400 {
		t.Fatal("zero slots accepted")
	}
}

func TestQueueHeartbeatAndFencingSurviveRestart(t *testing.T) {
	s := testStore(t)
	now := time.Unix(1000, 0)
	s.Now = func() time.Time { return now }
	queueBatch(t, s, 2, "simulated")
	lease := mustClaim(t, s, "worker", 2)
	now = now.Add(30 * time.Second)
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	reopened.Now = s.Now
	if err = reopened.Heartbeat(*lease); err != nil {
		t.Fatal(err)
	}
	var until float64
	s.DB.QueryRow("SELECT lease_until FROM runs WHERE id=?", lease.RunID).Scan(&until)
	if until != 1090 {
		t.Fatal("heartbeat not persisted")
	}
	for _, mutate := range []func(*Lease){func(l *Lease) { l.Owner = "wrong" }, func(l *Lease) { l.Attempt++ }, func(l *Lease) { l.AttemptID = "wrong" }, func(l *Lease) { l.BatchID = "wrong" }, func(l *Lease) { l.TeamID = "wrong" }, func(l *Lease) { l.SHA256 = "wrong" }} {
		invalid := *lease
		mutate(&invalid)
		if status(s.ValidateLease(invalid)) != 409 {
			t.Fatal("invalid execution capability accepted")
		}
	}
	now = time.Unix(1090, 0)
	if status(reopened.Heartbeat(*lease)) != 409 {
		t.Fatal("expired lease revived")
	}
	report, err := reopened.Recover()
	if err != nil || report.Rescheduled != 1 {
		t.Fatalf("recover: %#v %v", report, err)
	}
	next := mustClaim(t, reopened, "new-worker", 2)
	if next.RunID != lease.RunID || next.Attempt != 2 {
		t.Fatal("retry lost attempt lineage")
	}
	if status(s.BeginUpload(*lease)) != 409 {
		t.Fatal("stale worker began upload")
	}
}

func TestQueueRealLossRequiresReconciliationAndReservesSlot(t *testing.T) {
	s := testStore(t)
	now := time.Unix(1000, 0)
	s.Now = func() time.Time { return now }
	bid := queueBatch(t, s, 2, "codex")
	lease := mustClaim(t, s, "real-worker", 1)
	s.DB.Exec("INSERT INTO run_tokens VALUES(?,?,?,?,?,0)", "synthetic-token-hash", lease.RunID, 1, "test-model", 2000)
	now = now.Add(61 * time.Second)
	report, err := s.Recover()
	if err != nil || report.Attention != 1 || report.Rescheduled != 0 {
		t.Fatalf("unsafe recovery %#v %v", report, err)
	}
	var state string
	s.DB.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&state)
	if state != "attention" {
		t.Fatal("real lost run not held")
	}
	var revoked int
	s.DB.QueryRow("SELECT revoked FROM run_tokens").Scan(&revoked)
	if revoked != 1 {
		t.Fatal("lost execution token live")
	}
	queueBatch(t, s, 1, "simulated")
	if other, err := s.Claim("must-reserve-old-sandbox", 1); err != nil || other != nil {
		t.Fatal("unknown real sandbox slot reused")
	}
	if status(s.ValidateLease(*lease)) != 409 {
		t.Fatal("lost real worker can publish")
	}
}

func TestQueueUploadRecoveryPreservesAttemptAndOnlyCopy(t *testing.T) {
	s := testStore(t)
	now := time.Unix(1000, 0)
	s.Now = func() time.Time { return now }
	queueBatch(t, s, 2, "simulated")
	lease := mustClaim(t, s, "uploader", 1)
	if err := s.BeginUpload(*lease); err != nil {
		t.Fatal(err)
	}
	now = now.Add(time.Hour)
	report, err := s.Recover()
	if err != nil || report.UploadsPreserved != 1 || report.Rescheduled != 0 {
		t.Fatalf("upload rerun %#v %v", report, err)
	}
	if other, err := s.Claim("other", 1); err != nil || other != nil {
		t.Fatal("upload slot released before durability")
	}
	var state string
	var attempt int
	s.DB.QueryRow("SELECT state,attempt FROM runs WHERE id=?", lease.RunID).Scan(&state, &attempt)
	if state != "uploading" || attempt != 1 {
		t.Fatal("pending upload changed")
	}
}

func TestQueueCancellationOnlyAutoReclaimsSimulatedWork(t *testing.T) {
	for _, mode := range []string{"simulated", "codex"} {
		t.Run(mode, func(t *testing.T) {
			s := testStore(t)
			now := time.Unix(1000, 0)
			s.Now = func() time.Time { return now }
			bid := queueBatch(t, s, 1, mode)
			mustClaim(t, s, "worker", 1)
			s.DB.Exec("UPDATE batches SET status='cancelling' WHERE id=?", bid)
			now = now.Add(time.Minute)
			report, err := s.Recover()
			if err != nil {
				t.Fatal(err)
			}
			var state string
			s.DB.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&state)
			if mode == "simulated" {
				if state != "cancelled" || report.Cancelled != 1 {
					t.Fatal("simulated cancellation stuck")
				}
			} else {
				if state != "cancelling" || report.Attention != 1 {
					t.Fatal("unconfirmed real cancellation released team")
				}
			}
		})
	}
}

func TestQueuePythonRequestGoClaimPythonCompletion(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	pyroot := filepath.Clean(filepath.Join(filepath.Dir(file), "../../../platform"))
	python := filepath.Join(pyroot, ".venv/bin/python")
	if _, err := os.Stat(python); err != nil {
		t.Skip("Python compatibility environment unavailable")
	}
	s := testStore(t)
	execute := func(script string, args ...string) []byte {
		t.Helper()
		cmd := exec.Command(python, append([]string{"-c", script}, args...)...)
		cmd.Env = append(os.Environ(), "PYTHONPATH="+pyroot, "QIQC_R2_ENABLED=0", "QIQC_R2_ENV="+filepath.Join(t.TempDir(), "no-credentials"))
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("Python interoperability: %v %s", err, out)
		}
		return out
	}
	execute(`from pathlib import Path
import sys
from qiqc.core import Engine,Settings
from qiqc.archive import reference_package
e=Engine(Settings(data_dir=Path(sys.argv[1]),scan_docker=False))

a=e.create_account('interop@example.test','synthetic-password-only','Interop',must_change=False)
v=e.upload(a['id'],reference_package())
e.confirm(a['id'],v['id'],'infra','simulated','interop-key')`, s.Root)
	lease := mustClaim(t, s, "go-controller", 1)
	data, _ := json.Marshal(lease)
	execute(`from pathlib import Path
import sys,json
from qiqc.core import Engine,Settings
e=Engine(Settings(data_dir=Path(sys.argv[1]),scan_docker=False))
l=json.loads(sys.argv[2]);e.heartbeat(l);e.complete_task(l,1,'1.234567','synthetic interoperability trace')`, s.Root, string(data))
	var state, mean string
	var passed int
	if err := s.DB.QueryRow("SELECT status,passed,mean FROM batches WHERE id=?", lease.BatchID).Scan(&state, &passed, &mean); err != nil {
		t.Fatal(err)
	}
	if state != "completed" || passed != 1 || mean != "1.234567" {
		t.Fatal("Python worker could not finalize Go lease")
	}
	if status(s.ValidateLease(*lease)) != 409 {
		t.Fatal("completed attempt still publishable")
	}
}
