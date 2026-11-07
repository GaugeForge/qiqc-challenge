package platform

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

func automaticCatalog(t *testing.T, s *Store, publicCount int) ScientificTaskSet {
	t.Helper()
	release := "native-candidate-" + strings.Repeat("a", 64)
	if err := s.ConfigureScientificCandidate(release); err != nil {
		t.Fatal(err)
	}
	c := ScientificTaskSet{Schema: "qiqc-scientific-task-set/v1", ReleaseID: release, AgentBudgetSeconds: 180, InfraTaskIndex: 48}
	public := map[int]bool{0: true, 26: true, 46: true, 48: true}
	if publicCount == 5 {
		public[17] = true
	}
	for i := 0; i < 49; i++ {
		c.Tasks = append(c.Tasks, ScientificTask{TaskIndex: i, TaskID: fmt.Sprintf("public_fixture_task_%02d", i), Public: public[i]})
	}
	raw, _ := json.Marshal(c)
	if err := s.ConfigureScientificTaskSet(raw); err != nil {
		t.Fatal(err)
	}
	return c
}

func TestScientificRolePartitionSelectsWorkerBatches(t *testing.T) {
	c := ScientificTaskSet{
		Schema: "qiqc-scientific-task-set/v1", ReleaseID: "native-candidate-" + strings.Repeat("a", 64),
		AgentBudgetSeconds: 180, InfraTaskIndex: 48,
	}
	tracing := map[int]bool{5: true, 15: true, 16: true, 32: true, 33: true, 34: true, 39: true}
	infrastructure := map[int]bool{2: true, 19: true, 44: true}
	public := map[int]bool{0: true, 25: true, 45: true, 47: true, 48: true}
	for i := 0; i < 49; i++ {
		role := "binary"
		if tracing[i] {
			role = "racing"
		} else if infrastructure[i] {
			role = "infrastructure"
		}
		c.Tasks = append(c.Tasks, ScientificTask{TaskIndex: i, TaskID: fmt.Sprintf("role_fixture_%02d", i), Public: public[i], EvaluationRole: role})
	}
	if err := c.validate(); err != nil {
		t.Fatal(err)
	}
	if got := len(c.selected("full")); got != 39 {
		t.Fatalf("normal task count = %d", got)
	}
	if got := len(c.selected("elo")); got != 7 {
		t.Fatalf("elo task count = %d", got)
	}
	if got := len(c.selected("infra")); got != 3 {
		t.Fatalf("infra task count = %d", got)
	}
	if got := len(c.selected("public")); got != 5 {
		t.Fatalf("public task count = %d", got)
	}
	if c.selected("full")[0].TaskIndex != 0 || c.selected("elo")[0].TaskIndex != 5 || c.selected("infra")[0].TaskIndex != 2 {
		t.Fatal("role selection changed lexical task order")
	}
}

func TestAutomaticScientificTaskSetConcurrentConfirmAndFrozenRuns(t *testing.T) {
	s := testStore(t)
	c := automaticCatalog(t, s, 5)
	token, v := approved(t, s)
	// No official SHA map and no per-ZIP administrator task action.
	var wg sync.WaitGroup
	errs := make(chan error, 12)
	ids := make(chan string, 12)
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			b, err := s.Confirm(token, v.ID, "full", "scientific", "automatic-once")
			if err != nil {
				errs <- err
				return
			}
			ids <- b["id"].(string)
		}()
	}
	wg.Wait()
	close(errs)
	close(ids)
	for err := range errs {
		t.Fatal(err)
	}
	bid := ""
	for b := range ids {
		if bid != "" && bid != b {
			t.Fatal("duplicate batch")
		}
		bid = b
	}
	for table, want := range map[string]int{"batches": 1, "runs": 49, "ledger": 1, "run_scientific_targets": 49, "scientific_targets": 0} {
		var count int
		s.DB.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count)
		if count != want {
			t.Fatalf("%s=%d", table, count)
		}
	}
	// Future configuration must not retarget accepted work, even across restart.
	c.AgentBudgetSeconds = 300
	raw, _ := json.Marshal(c)
	if err := s.ConfigureScientificTaskSet(raw); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	for i := 0; i < 49; i++ {
		l := mustClaim(t, reopened, fmt.Sprint("worker", i), 49)
		target, err := reopened.ScientificTargetForLease(*l)
		if err != nil {
			t.Fatal(err)
		}
		if target.TaskIndex != i || target.TaskID != c.Tasks[i].TaskID || target.AgentBudgetSeconds != 180 || target.SubmissionSHA != v.SHA256 || l.Public != boolInt(c.Tasks[i].Public) {
			t.Fatal("binding changed", i, target)
		}
		forged := *l
		forged.Public = 1 - l.Public
		if _, err := reopened.ScientificTargetForLease(forged); err == nil {
			t.Fatal("forged visibility accepted")
		}
	}
}
func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
func TestAutomaticScientificIncompletePublicDoesNotSpendQuota(t *testing.T) {
	s := testStore(t)
	automaticCatalog(t, s, 4)
	token, v := approved(t, s)
	if _, err := s.Confirm(token, v.ID, "public", "scientific", "blocked-public"); status(err) != 409 {
		t.Fatal(err)
	}
	var count int
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&count)
	if count != 0 {
		t.Fatal("blocked public spent quota")
	}
	b, err := s.Confirm(token, v.ID, "infra", "scientific", "automatic-infra")
	if err != nil {
		t.Fatal(err)
	}
	l := mustClaim(t, s, "worker", 1)
	target, err := s.ScientificTargetForLease(*l)
	if err != nil || target.TaskIndex != 48 || l.Public != 1 || l.Ordinal != 0 {
		t.Fatal(target, l, err)
	}
	if b["bounded_scientific"] != true || b["leaderboard_eligible"] != false {
		t.Fatal("missing bounded label")
	}
	raw, _ := json.Marshal(b)
	if strings.Contains(string(raw), "task_index") || strings.Contains(string(raw), "catalog_sha256") {
		t.Fatal("private binding leaked")
	}
}
func TestAutomaticScientificPublicSelectsRealMappingNotFirstFive(t *testing.T) {
	s := testStore(t)
	c := automaticCatalog(t, s, 5)
	token, v := approved(t, s)
	if _, err := s.Confirm(token, v.ID, "public", "scientific", "public"); err != nil {
		t.Fatal(err)
	}
	want := []int{0, 17, 26, 46, 48}
	for i, index := range want {
		l := mustClaim(t, s, "worker", 5)
		target, err := s.ScientificTargetForLease(*l)
		if err != nil || l.Ordinal != i || l.Public != 1 || target.TaskIndex != index || target.TaskID != c.Tasks[index].TaskID {
			t.Fatal(i, target, err)
		}
	}
}
func TestAutomaticScientificRejectsBadCatalogAndDisabledAdmission(t *testing.T) {
	s := testStore(t)
	c := automaticCatalog(t, s, 5)
	for _, change := range []func(*ScientificTaskSet){func(c *ScientificTaskSet) { c.Tasks[1].TaskIndex = 0 }, func(c *ScientificTaskSet) { c.Tasks[1].TaskID = c.Tasks[0].TaskID }, func(c *ScientificTaskSet) { c.Tasks[1].Public = true }, func(c *ScientificTaskSet) { c.InfraTaskIndex = 1 }, func(c *ScientificTaskSet) { c.ReleaseID = "native-candidate-" + strings.Repeat("b", 64) }, func(c *ScientificTaskSet) { c.AgentBudgetSeconds = 181 }} {
		bad := c
		bad.Tasks = append([]ScientificTask(nil), c.Tasks...)
		change(&bad)
		raw, _ := json.Marshal(bad)
		if err := s.ConfigureScientificTaskSet(raw); err == nil {
			t.Fatal("invalid catalog accepted")
		}
	}
	token, v := approved(t, s)
	s.DB.Exec("DELETE FROM settings WHERE key='scientific_task_set'")
	if _, err := s.Confirm(token, v.ID, "full", "scientific", "disabled"); status(err) != 409 {
		t.Fatal(err)
	}
	var count int
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&count)
	if count != 0 {
		t.Fatal("disabled spent quota")
	}
}
func syntheticScientificArchive(t *testing.T) []byte {
	t.Helper()
	var b bytes.Buffer
	gz := gzip.NewWriter(&b)
	tw := tar.NewWriter(gz)
	data := []byte("PUBLIC SYNTHETIC TRAJECTORY")
	if err := tw.WriteHeader(&tar.Header{Name: "public-view.txt", Mode: 0600, Size: int64(len(data))}); err != nil {
		t.Fatal(err)
	}
	tw.Write(data)
	tw.Close()
	gz.Close()
	return b.Bytes()
}
func prepareAutomaticScientificPending(t *testing.T, s *Store, l Lease, state string) Pending {
	t.Helper()
	if err := s.BeginUpload(l); err != nil {
		t.Fatal(err)
	}
	cache := filepath.Join(s.Root, "cache", l.RunID, "1")
	os.MkdirAll(cache, 0700)
	raw := syntheticScientificArchive(t)
	os.WriteFile(filepath.Join(cache, "rollout.tar.gz"), raw, 0600)
	p := Pending{Lease: l, Reward: 0, Seconds: "180.200000", Provenance: l.Mode, SHA: shaBytes(raw)}
	data, _ := json.Marshal(p)
	os.WriteFile(filepath.Join(cache, "staged.json"), data, 0600)
	directory := filepath.Join(s.Root, "scientific")
	os.MkdirAll(directory, 0700)
	r, _ := json.Marshal(map[string]any{"run_id": l.RunID, "attempt": l.Attempt, "mode": l.Mode, "phase": "uploading_rollout", "resources": map[string]string{"native-vm": state}})
	os.WriteFile(filepath.Join(directory, l.RunID+"_1.json"), r, 0600)
	return p
}
func TestAutomaticScientificRealPathSyntheticArchiveRecoveryAndPrivacy(t *testing.T) {
	s := testStore(t)
	automaticCatalog(t, s, 5)
	token, v := approved(t, s)
	b, err := s.Confirm(token, v.ID, "full", "scientific", "full-recovery")
	if err != nil {
		t.Fatal(err)
	}
	objects := &brokenObjects{&rollout.Store{Root: filepath.Join(s.Root, "objects")}, true}
	s.Objects = objects
	first := mustClaim(t, s, "worker", 49)
	p := prepareAutomaticScientificPending(t, s, *first, "created")
	if s.PromoteScientificPending(*first) == nil {
		t.Fatal("running sandbox published")
	}
	p = prepareAutomaticScientificPending(t, s, *first, "destroyed")
	if err := s.PromoteScientificPending(*first); err != nil {
		t.Fatal(err)
	}
	if err := s.CompleteScientificPending(context.Background(), *first, p.Reward, p.Seconds, p.SHA); err == nil {
		t.Fatal("object outage hidden")
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	objects.broken = false
	reopened.Objects = objects
	if n, err := reopened.ResumeUploads(context.Background()); err != nil || n != 1 {
		t.Fatal(n, err)
	}
	for i := 1; i < 49; i++ {
		l := mustClaim(t, reopened, "worker", 49)
		p := prepareAutomaticScientificPending(t, reopened, *l, "destroyed")
		if err := reopened.PromoteScientificPending(*l); err != nil {
			t.Fatal(err)
		}
		if err := reopened.CompleteScientificPending(context.Background(), *l, p.Reward, p.Seconds, p.SHA); err != nil {
			t.Fatal(err)
		}
	}
	view, err := reopened.GetBatch(token, b["id"].(string))
	if err != nil || view["status"] != "completed" || len(view["public_runs"].([]map[string]any)) != 0 {
		t.Fatal(view, err)
	}
	if _, err := reopened.Trace(context.Background(), token, first.BatchID, 0); status(err) != 404 {
		t.Fatal("full evaluation trajectory exposed", err)
	}
	other := readyUser(t, reopened, "other@fixture.test", "team")
	if _, err := reopened.Trace(context.Background(), other, first.BatchID, 0); status(err) != 404 {
		t.Fatal("cross-team trajectory exposed", err)
	}
	var attempts, debits int
	reopened.DB.QueryRow("SELECT COUNT(*) FROM attempts").Scan(&attempts)
	reopened.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&debits)
	if attempts != 49 || debits != 1 {
		t.Fatal(attempts, debits)
	}
	board, err := reopened.Leaderboard()
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(board)
	if bytes.Contains(raw, []byte(v.SHA256)) {
		t.Fatal("bounded result entered ranking")
	}
}
func TestCancelFinalizesDestroyedWorkerLostController(t *testing.T) {
	s := testStore(t)
	automaticCatalog(t, s, 4)
	token, v := approved(t, s)
	b, err := s.Confirm(token, v.ID, "infra", "scientific", "destroyed-controller")
	if err != nil {
		t.Fatal(err)
	}
	l := mustClaim(t, s, "worker", 1)
	if _, err = s.DB.Exec("UPDATE runs SET state='attention',error=?,lease_until=NULL WHERE id=?", lostWorkerError, l.RunID); err != nil {
		t.Fatal(err)
	}
	if _, err = s.DB.Exec("UPDATE batches SET status='attention' WHERE id=?", l.BatchID); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(s.Root, "scientific")
	if err = os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(map[string]any{
		"run_id": l.RunID, "attempt": 1, "mode": "scientific",
		"resources": map[string]string{"native-vm": "destroyed"},
	})
	if err = os.WriteFile(filepath.Join(dir, l.RunID+"_1.json"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	if err = s.Cancel(token, b["id"].(string)); err != nil {
		t.Fatal(err)
	}
	view, err := s.GetBatch(token, l.BatchID)
	if err != nil || view["status"] != "cancelled" {
		t.Fatal(view, err)
	}
	var state string
	if err = s.DB.QueryRow("SELECT state FROM runs WHERE id=?", l.RunID).Scan(&state); err != nil || state != "cancelled" {
		t.Fatal("run not cancelled", state, err)
	}
}

func TestCancelFailedControllerFinalizesCancellingBatch(t *testing.T) {
	s := testStore(t)
	automaticCatalog(t, s, 4)
	token, v := approved(t, s)
	b, err := s.Confirm(token, v.ID, "infra", "scientific", "controller-stopped")
	if err != nil {
		t.Fatal(err)
	}
	l := mustClaim(t, s, "worker", 1)
	if _, err = s.DB.Exec("UPDATE batches SET status='cancelling' WHERE id=?", l.BatchID); err != nil {
		t.Fatal(err)
	}
	if err = s.CancelFailedController(*l); err != nil {
		t.Fatal(err)
	}
	view, err := s.GetBatch(token, b["id"].(string))
	if err != nil || view["status"] != "cancelled" {
		t.Fatal(view, err)
	}
	var state, attemptState string
	if err = s.DB.QueryRow("SELECT r.state,a.state FROM runs r JOIN attempts a ON a.run_id=r.id AND a.number=r.attempt WHERE r.id=?", l.RunID).Scan(&state, &attemptState); err != nil || state != "cancelled" || attemptState != "interrupted" {
		t.Fatal(state, attemptState, err)
	}
}

func TestRetryZeroResourceStartupOnlyOnce(t *testing.T) {
	s := testStore(t)
	automaticCatalog(t, s, 4)
	token, v := approved(t, s)
	b, err := s.Confirm(token, v.ID, "infra", "scientific", "startup-retry")
	if err != nil {
		t.Fatal(err)
	}
	l := mustClaim(t, s, "worker", 1)
	if _, err = s.DB.Exec("INSERT INTO run_tokens VALUES('startup-token',?,?, 'model',9999999999,0)", l.RunID, l.Attempt); err != nil {
		t.Fatal(err)
	}
	if err = s.RetryZeroResourceStartup(*l, "synthetic zero-resource startup retry"); err != nil {
		t.Fatal(err)
	}
	var runState, attemptState, attemptError string
	var revoked int
	if err = s.DB.QueryRow(`SELECT r.state,a.state,a.error,rt.revoked FROM runs r JOIN attempts a ON a.run_id=r.id AND a.number=r.attempt JOIN run_tokens rt ON rt.run_id=r.id WHERE r.id=?`, l.RunID).Scan(&runState, &attemptState, &attemptError, &revoked); err != nil {
		t.Fatal(err)
	}
	if runState != "queued" || attemptState != "failed" || attemptError != "platform:startup_failure_retried" || revoked != 1 {
		t.Fatal(runState, attemptState, attemptError, revoked)
	}
	view, err := s.GetBatch(token, b["id"].(string))
	if err != nil || view["status"] != "testing" {
		t.Fatal(view, err)
	}
	second := mustClaim(t, s, "worker", 1)
	if second.Attempt != 2 {
		t.Fatalf("retry attempt = %d", second.Attempt)
	}
	if err = s.RetryZeroResourceStartup(*second, "must not retry twice"); err == nil {
		t.Fatal("second startup retry accepted")
	}
}

func TestAutomaticScientificCrashRetainsSlotAndCancelUntilReconciled(t *testing.T) {
	s := testStore(t)
	automaticCatalog(t, s, 4)
	token, v := approved(t, s)
	b, err := s.Confirm(token, v.ID, "infra", "scientific", "crash")
	if err != nil {
		t.Fatal(err)
	}
	l := mustClaim(t, s, "worker", 1)
	s.Now = func() time.Time { return time.Now().Add(2 * time.Minute) }
	if _, err := s.Recover(); err != nil {
		t.Fatal(err)
	}
	if err := s.Cancel(token, b["id"].(string)); err != nil {
		t.Fatal(err)
	}
	var state string
	s.DB.QueryRow("SELECT state FROM runs WHERE id=?", l.RunID).Scan(&state)
	if state != "attention" {
		t.Fatal("lost VM slot released", state)
	}
	if err := s.CheckScientificReconciliation(l.RunID, l.Attempt); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(s.Root, "scientific")
	os.MkdirAll(dir, 0700)
	raw, _ := json.Marshal(map[string]any{"run_id": l.RunID, "attempt": 1, "mode": "scientific", "provider_checked": true, "resources": map[string]string{"native-vm": "destroyed"}})
	os.WriteFile(filepath.Join(dir, l.RunID+"_1.json"), raw, 0600)
	if err := s.ReconcileScientificResources(l.RunID, 1, "synthetic confirmed cleanup"); err != nil {
		t.Fatal(err)
	}
	view, err := s.GetBatch(token, l.BatchID)
	if err != nil || view["status"] != "cancelled" {
		t.Fatal(view, err)
	}
}
