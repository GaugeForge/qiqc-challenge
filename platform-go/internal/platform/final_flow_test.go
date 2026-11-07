package platform

import (
	"bytes"
	"context"
	"encoding/json"
	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func nativeCatalogFixture(t *testing.T, s *Store) ScientificTaskSet {
	c := automaticCatalog(t, s, 5)
	policy := FormalExecution{ProfilePath: "/synthetic/profile.json", ProfileSHA256: strings.Repeat("d", 64), Model: "fixture-model", GatewayURL: "http://127.0.0.1:18789", MaxRequestsPerTask: 1000}
	formalRaw, _ := json.Marshal(policy)
	if _, e := s.DB.Exec("INSERT INTO settings(key,value) VALUES('formal_execution',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(formalRaw)); e != nil {
		t.Fatal(e)
	}
	c.Schema = "qiqc-scientific-task-set/v2"
	for i := range c.Tasks {
		c.Tasks[i].Limits = TaskLimits{AgentSeconds: 3600 + (i%3)*3600, SetupSeconds: 600, BuildSeconds: 300, VerifierSeconds: 1200, CPUs: 2, MemoryMB: 4096, SourceSHA256: strings.Repeat("c", 64)}
	}
	raw, _ := json.Marshal(c)
	if e := s.ConfigureScientificTaskSet(raw); e != nil {
		t.Fatal(e)
	}
	return c
}
func finishFixtureBatch(t *testing.T, s *Store, count, passed int) {
	t.Helper()
	s.Objects = &rollout.Store{Root: filepath.Join(s.Root, "objects")}
	for i := 0; i < count; i++ {
		l := mustClaim(t, s, "fixture-executor", count)
		p := prepareAutomaticScientificPending(t, s, *l, "destroyed")
		if i < passed {
			p.Reward = 1
		}
		// The synthetic object and trusted completion are a local dependency fixture,
		// not an E2B run or a scientific verification claim.
		if p.Reward == 1 {
			raw, _ := json.Marshal(p)
			if e := syncWrite(filepath.Join(s.Root, "cache", l.RunID, "1", "staged.json"), raw); e != nil {
				t.Fatal(e)
			}
		}
		if e := s.PromoteScientificPending(*l); e != nil {
			t.Fatal(e)
		}
		if e := s.CompleteScientificPending(context.Background(), *l, p.Reward, p.Seconds, p.SHA); e != nil {
			t.Fatal(e)
		}
	}
}
func TestAutomaticSubmissionReviewRestartAndFiveTaskCompletion(t *testing.T) {
	t.Setenv("QIQC_COMPETITION_PORTAL", "1")
	s := testStore(t)
	nativeCatalogFixture(t, s)
	team, _ := advisoryVersion(t, s)
	intent, e := s.UploadAndQueue(team, []byte("automatic review frozen fixture"), "infra", "upload-one")
	if e != nil {
		t.Fatal(e)
	}
	worker := automaticReviewWorker(t, cleanAutomaticAdvice, nil)
	for n := 0; n < 2; n++ {
		if _, e = s.ProcessReviewOnce(context.Background(), worker, "fixture-reviewer"); e != nil {
			t.Fatal(e)
		}
	}
	reopened, e := Open(s.Path)
	if e != nil {
		t.Fatal(e)
	}
	defer reopened.Close()
	var wg sync.WaitGroup
	for n := 0; n < 8; n++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if e := reopened.ProcessSubmissionIntents(); e != nil {
				t.Error(e)
			}
		}()
	}
	wg.Wait()
	intents, e := reopened.SubmissionIntents(team)
	if e != nil || len(intents) != 1 || intents[0].State != "accepted" {
		t.Fatal(intents, e)
	}
	var runs, debits int
	reopened.DB.QueryRow("SELECT count(*) FROM runs").Scan(&runs)
	reopened.DB.QueryRow("SELECT count(*) FROM ledger").Scan(&debits)
	if runs != 5 || debits != 1 {
		t.Fatal("wrong admission", runs, debits)
	}
	finishFixtureBatch(t, reopened, 5, 0)
	batch, e := reopened.GetBatch(team, intents[0].BatchID)
	if e != nil || batch["status"] != "completed" {
		t.Fatal(batch, e)
	}
	same, e := s.UploadAndQueue(team, []byte("automatic review frozen fixture"), "infra", "upload-one")
	if e != nil || same.ID != intent.ID {
		t.Fatal("idempotent replay", e)
	}
	other := readyUser(t, s, "other-flow@example.test", "team")
	private, e := s.SubmissionIntents(other)
	if e != nil || len(private) != 0 {
		t.Fatal("cross team intents")
	}
	staff := readyUser(t, s, "flow-staff@example.test", "admin")
	if _, e = s.ReviewReport(team, intent.VersionID, "fixture"); status(e) != 403 {
		t.Fatal("team obtained review report")
	}
	report, e := s.ReviewReport(staff, intent.VersionID, "fixture")
	if e != nil || len(report["reviews"].([]map[string]any)) != 1 {
		t.Fatal(report, e)
	}
}

func TestSubmissionIntentCanBeCancelledBeforeAcceptanceWithoutQuotaDebit(t *testing.T) {
	s := testStore(t)
	team, _ := advisoryVersion(t, s)
	intent, err := s.UploadAndQueue(team, []byte("cancel-before-acceptance"), "infra", "cancel-before-acceptance")
	if err != nil || intent.State != "reviewing" {
		t.Fatal(intent, err)
	}
	if err = s.CancelSubmissionIntent(team, intent.ID); err != nil {
		t.Fatal(err)
	}
	intents, err := s.SubmissionIntents(team)
	if err != nil || len(intents) != 1 || intents[0].State != "cancelled" || !strings.Contains(intents[0].Reason, "No attempt charged") {
		t.Fatal(intents, err)
	}
	var versionStatus string
	if err = s.DB.QueryRow("SELECT status FROM versions WHERE id=?", intent.VersionID).Scan(&versionStatus); err != nil || versionStatus != "cancelled" {
		t.Fatal(versionStatus, err)
	}
	var ledger, batches int
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&ledger)
	s.DB.QueryRow("SELECT COUNT(*) FROM batches").Scan(&batches)
	if ledger != 0 || batches != 0 {
		t.Fatalf("cancelled review charged or queued: ledger=%d batches=%d", ledger, batches)
	}
}
func TestNativeRankingSingleResultAndCloseDrain(t *testing.T) {
	s := testStore(t)
	c := nativeCatalogFixture(t, s)
	team, v := approved(t, s)
	calendar := scheduledCalendar()
	installCalendar(t, s, calendar)
	now := calendar.StartsAt
	s.Now = func() time.Time { return now }
	s.DB.Exec("UPDATE sessions SET expires=?", float64(calendar.AppealClosesAt.Unix()+86400))
	t.Setenv("QIQC_COMPETITION_PORTAL", "1")
	first, e := s.Confirm(team, v.ID, "full", "scientific", "rank-first")
	if e != nil {
		t.Fatal(e)
	}
	var budget int
	s.DB.QueryRow("SELECT agent_budget_seconds FROM run_scientific_targets WHERE task_index=2").Scan(&budget)
	if budget != c.Tasks[2].Limits.AgentSeconds {
		t.Fatal("native budget lost", budget)
	}
	finishFixtureBatch(t, s, 49, 12)
	board, e := s.Leaderboard()
	if e != nil {
		t.Fatal(e)
	}
	ranks := board["teams"].([]Standing)
	if len(ranks) != 1 || ranks[0].Passed != 12 || ranks[0].ID != first["id"] || ranks[0].Challenger {
		t.Fatal(board)
	}
	if _, e := s.Confirm(team, v.ID, "full", "scientific", "rank-second"); e == nil {
		t.Fatal("second full evaluation was accepted", e)
	}
	now = calendar.EndsAt
	if _, e = s.Confirm(team, v.ID, "public", "scientific", "late"); e == nil {
		t.Fatal("late request accepted")
	}
	board, e = s.Leaderboard()
	if e != nil {
		t.Fatal(e)
	}
	ranks = board["teams"].([]Standing)
	if len(ranks) != 1 || ranks[0].Passed != 12 || ranks[0].ID != first["id"] {
		t.Fatal(board)
	}
	s.DB.Exec("UPDATE runs SET object_sha=NULL WHERE batch_id=? AND ordinal=0", first["id"])
	if _, e = s.Leaderboard(); e == nil {
		t.Fatal("missing evidence ranked")
	}
}
func TestAutomaticIntentCannotCrossQuietOrCompetition(t *testing.T) {
	t.Setenv("QIQC_COMPETITION_PORTAL", "1")
	s := testStore(t)
	nativeCatalogFixture(t, s)
	team, _ := advisoryVersion(t, s)
	c := scheduledCalendar()
	start := c.StartsAt.Add(-24 * time.Hour)
	end := c.QuietStart()
	c.InfraStartsAt = &start
	c.InfraEndsAt = &end
	installCalendar(t, s, c)
	now := end.Add(-time.Second)
	s.Now = func() time.Time { return now }
	s.DB.Exec("UPDATE sessions SET expires=?", float64(c.AppealClosesAt.Unix()+86400))
	i, e := s.UploadAndQueue(team, []byte("pending phase test"), "infra", "cross-phase")
	if e != nil {
		t.Fatal(e)
	}
	now = c.StartsAt
	s.DB.Exec("UPDATE versions SET status='approved' WHERE id=?", i.VersionID)
	if e = s.ProcessSubmissionIntents(); e != nil {
		t.Fatal(e)
	}
	list, e := s.SubmissionIntents(team)
	if e != nil || list[0].State != "blocked" {
		t.Fatal(list, e)
	}
	var n int
	s.DB.QueryRow("SELECT count(*) FROM ledger").Scan(&n)
	if n != 0 {
		t.Fatal("phase crossing charged")
	}
}

func TestAutomaticHTTPReplayAfterQuietReturnsOriginalReceipt(t *testing.T) {
	t.Setenv("QIQC_COMPETITION_PORTAL", "1")
	s := testStore(t)
	nativeCatalogFixture(t, s)
	team, _ := advisoryVersion(t, s)
	c := scheduledCalendar()
	start := c.StartsAt.Add(-24 * time.Hour)
	end := c.QuietStart()
	c.InfraStartsAt = &start
	c.InfraEndsAt = &end
	installCalendar(t, s, c)
	now := end.Add(-time.Second)
	s.Now = func() time.Time { return now }
	s.DB.Exec("UPDATE sessions SET expires=?", float64(c.AppealClosesAt.Unix()))
	body := []byte("immutable replay fixture")
	first, e := s.UploadAndQueue(team, body, "infra", "same-http-request")
	if e != nil {
		t.Fatal(e)
	}
	now = end
	mux := http.NewServeMux()
	s.RegisterBusiness(mux, http.NotFoundHandler())
	send := func(key string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "/api/submissions", bytes.NewReader(body))
		r.Header.Set("X-QIQC-Request", "1")
		r.Header.Set("Idempotency-Key", key)
		r.Header.Set("X-QIQC-Evaluation", "infra")
		r.AddCookie(&http.Cookie{Name: "qiqc_session", Value: team})
		out := httptest.NewRecorder()
		mux.ServeHTTP(out, r)
		return out
	}
	replay := send("same-http-request")
	var receipt SubmissionIntent
	if replay.Code != 200 || json.Unmarshal(replay.Body.Bytes(), &receipt) != nil || receipt.ID != first.ID {
		t.Fatal("replay lost original receipt", replay.Code)
	}
	if code := send("new-http-request").Code; code != 409 {
		t.Fatal("new request admitted in quiet", code)
	}
}
