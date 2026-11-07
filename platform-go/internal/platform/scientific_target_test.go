package platform

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func targetAction(version string, index int) Action {
	return Action{Action: "scientific_target", Target: version, TaskIndex: &index, Actor: "acceptance operator", Reason: "Frozen catalog isolated acceptance"}
}

func TestScientificTargetAuthorizationImmutabilityAndPrivacy(t *testing.T) {
	s := testStore(t)
	team, v := approved(t, s)
	admin := readyUser(t, s, "staff@example.test", "admin")
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	mux := http.NewServeMux()
	s.RegisterBusiness(mux, http.NotFoundHandler())
	a := targetAction(v.ID, 48)
	payload, _ := json.Marshal(a)
	if res := call(t, mux, "POST", "/api/admin/actions", team, string(payload)); res.Code != 403 {
		t.Fatal("team bound hidden target", res.Code)
	}
	r := httptest.NewRequest("POST", "/api/admin/actions", strings.NewReader(string(payload)))
	r.AddCookie(&http.Cookie{Name: "qiqc_session", Value: admin})
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal("csrf bypass", w.Code)
	}
	for _, index := range []int{-1, 49} {
		if status(s.AdminAction(admin, targetAction(v.ID, index), "local")) != 400 {
			t.Fatal("invalid index accepted")
		}
	}
	if res := call(t, mux, "POST", "/api/admin/actions", admin, string(payload)); res.Code != 200 {
		t.Fatal(res.Code, res.Body.String())
	}
	if err := s.AdminAction(admin, a, "local"); err != nil {
		t.Fatal("same binding not idempotent", err)
	}
	if status(s.AdminAction(admin, targetAction(v.ID, 0), "local")) != 409 {
		t.Fatal("binding changed")
	}
	var audits int
	s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action='scientific_target_bound'").Scan(&audits)
	if audits != 1 {
		t.Fatal("duplicate audit", audits)
	}
	b, err := s.Confirm(team, v.ID, "infra", "scientific-smoke", "acceptance-once")
	if err != nil {
		t.Fatal(err)
	}
	if b["catalog_acceptance"] != true || b["agent_budget_seconds"] != 300 {
		t.Fatal("safe diagnostic flags absent", b)
	}
	for _, key := range []string{"task_index", "task_id", "scientific_target", "scientific_targets"} {
		if _, ok := b[key]; ok {
			t.Fatal("hidden target exposed", key)
		}
	}
	l := mustClaim(t, s, "worker", 1)
	if l.Public != 0 {
		t.Fatal("catalog trace public")
	}
	target, err := s.ScientificTargetForLease(*l)
	if err != nil || target.TaskIndex != 48 || target.AgentBudgetSeconds != 300 || target.SubmissionSHA != v.SHA256 {
		t.Fatal("target not pinned", target, err)
	}
	forged := *l
	forged.Public = 1
	if _, err = s.ScientificTargetForLease(forged); err == nil {
		t.Fatal("forged lease accepted")
	}
	if _, err = s.Trace(context.Background(), team, l.BatchID, 0); status(err) != 404 {
		t.Fatal("hidden trace readable", err)
	}
	if _, err = s.Trace(context.Background(), admin, l.BatchID, 0); status(err) != 404 {
		t.Fatal("admin participant route leaked hidden trace", err)
	}
	var policy string
	s.DB.QueryRow("SELECT policy_json FROM batch_policies WHERE batch_id=?", l.BatchID).Scan(&policy)
	if !strings.Contains(policy, `"task_index":48`) || !strings.Contains(policy, ScientificReleaseID) {
		t.Fatal("policy omitted target")
	}
	if _, err = s.DB.Exec("UPDATE runs SET state='completed',reward=0,seconds='300.000000' WHERE id=?", l.RunID); err != nil {
		t.Fatal(err)
	}
	if _, err = s.DB.Exec("UPDATE batches SET status='completed',passed=0,mean='300.000000' WHERE id=?", l.BatchID); err != nil {
		t.Fatal(err)
	}
	completed, err := s.GetBatch(team, l.BatchID)
	if err != nil || len(completed["public_runs"].([]map[string]any)) != 0 {
		t.Fatal("completed trace leaked", err)
	}
	if _, err = s.Trace(context.Background(), team, l.BatchID, 0); status(err) != 404 {
		t.Fatal("completed hidden trace readable", err)
	}
	var debits int
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger WHERE action='accepted' AND batch_id=?", l.BatchID).Scan(&debits)
	if debits != 1 {
		t.Fatal("quota changed", debits)
	}
}

func TestScientificTargetConcurrentBindAndConfirmFrozen(t *testing.T) {
	s := testStore(t)
	team, v := approved(t, s)
	admin := readyUser(t, s, "staff@example.test", "admin")
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := s.AdminAction(admin, targetAction(v.ID, 7), "local"); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	ids := make(chan string, 16)
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			b, err := s.Confirm(team, v.ID, "infra", "scientific-smoke", "same")
			if err != nil {
				t.Error(err)
				return
			}
			ids <- b["id"].(string)
		}()
	}
	wg.Wait()
	close(ids)
	seen := map[string]bool{}
	for bid := range ids {
		seen[bid] = true
	}
	if len(seen) != 1 {
		t.Fatal("duplicate batch", seen)
	}
	var targets, ledger, audits int
	s.DB.QueryRow("SELECT COUNT(*) FROM batch_scientific_targets WHERE task_index=7").Scan(&targets)
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger WHERE action='accepted'").Scan(&ledger)
	s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action='scientific_target_bound'").Scan(&audits)
	if targets != 1 || ledger != 1 || audits != 1 {
		t.Fatal("non-atomic binding", targets, ledger, audits)
	}
}

func TestScientificTargetRejectsUnreviewedUnpinnedAndAlreadyAcceptedVersion(t *testing.T) {
	s := testStore(t)
	team, v := approved(t, s)
	admin := readyUser(t, s, "staff@example.test", "admin")
	if status(s.AdminAction(admin, targetAction(v.ID, 0), "local")) != 409 {
		t.Fatal("unconfigured package accepted")
	}
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	s.DB.Exec("UPDATE versions SET status='pending_review' WHERE id=?", v.ID)
	if status(s.AdminAction(admin, targetAction(v.ID, 0), "local")) != 409 {
		t.Fatal("unreviewed package accepted")
	}
	s.DB.Exec("UPDATE versions SET status='approved' WHERE id=?", v.ID)
	if _, err := s.Confirm(team, v.ID, "infra", "scientific-smoke", "old-public"); err != nil {
		t.Fatal(err)
	}
	if status(s.AdminAction(admin, targetAction(v.ID, 0), "local")) != 409 {
		t.Fatal("already accepted public request rebound")
	}
}

func TestScientificTargetSurvivesRestartAndBindsSHA(t *testing.T) {
	s := testStore(t)
	team, v := approved(t, s)
	admin := readyUser(t, s, "staff@example.test", "admin")
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	a := targetAction(v.ID, 0)
	a.Idem = "target-idempotency"
	if err := s.AdminAction(admin, a, "local"); err != nil {
		t.Fatal(err)
	}
	other := targetAction(v.ID, 1)
	other.Idem = a.Idem
	if status(s.AdminAction(admin, other, "local")) != 409 {
		t.Fatal("idempotency conflict ignored")
	}
	if _, err := s.Confirm(team, v.ID, "infra", "scientific-smoke", "before-restart"); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	l := mustClaim(t, reopened, "after-restart", 1)
	target, err := reopened.ScientificTargetForLease(*l)
	if err != nil || target.TaskIndex != 0 || target.ReleaseID != ScientificReleaseID {
		t.Fatal("frozen target lost across restart", target, err)
	}
	if _, err = s.DB.Exec("UPDATE batch_scientific_targets SET submission_sha256=? WHERE batch_id=?", strings.Repeat("f", 64), l.BatchID); err != nil {
		t.Fatal(err)
	}
	if _, err = reopened.ScientificTargetForLease(*l); err == nil {
		t.Fatal("changed target SHA accepted")
	}
}

func TestScientificCandidateRequiresEmptyImmutableSeparateDatabase(t *testing.T) {
	s := testStore(t)
	candidate := "native-candidate-" + strings.Repeat("a", 64)
	if err := s.ConfigureScientificCandidate(candidate); err != nil {
		t.Fatal(err)
	}
	team, v := approved(t, s)
	admin := readyUser(t, s, "native-admin@example.test", "admin")
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	if err := s.AdminAction(admin, targetAction(v.ID, 7), "local"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Confirm(team, v.ID, "infra", "scientific-smoke", "native-candidate-once"); err != nil {
		t.Fatal(err)
	}
	lease := mustClaim(t, s, "candidate-worker", 1)
	target, err := s.ScientificTargetForLease(*lease)
	if err != nil || target.ReleaseID != candidate {
		t.Fatal("candidate source not pinned", target, err)
	}
	if s.ConfigureScientificCandidate("native-candidate-"+strings.Repeat("b", 64)) == nil {
		t.Fatal("candidate database retargeted")
	}
	if err := s.ConfigureScientificCandidate(candidate); err != nil {
		t.Fatal("same candidate not idempotent", err)
	}
	ordinary := testStore(t)
	approved(t, ordinary)
	if ordinary.ConfigureScientificCandidate(candidate) == nil {
		t.Fatal("existing platform rebound to candidate")
	}
	if s.ConfigureScientificCandidate("native-candidate-short") == nil {
		t.Fatal("partial identity accepted")
	}
}
