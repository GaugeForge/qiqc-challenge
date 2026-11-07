package platform

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/gaugeforge/qiqc-platform-go/internal/review"
	"strings"
	"sync"
	"testing"
	"time"
)

type advisoryProvider func(context.Context, review.Request) ([]byte, error)

func (advisoryProvider) Version() string                                              { return "advisory-test-v1" }
func (f advisoryProvider) Review(c context.Context, r review.Request) ([]byte, error) { return f(c, r) }
func advisoryVersion(t *testing.T, s *Store) (string, Version) {
	t.Helper()
	s.Scanner = func(string) (Report, error) {
		return Report{Manifest: Manifest{Schema: "qiqc-harness/v1-demo", Name: "advisory fixture", Entrypoint: "agent.py"}, Files: 2, Risks: []Risk{}, RulesetVersion: "qiqc-archive/2", ParserSHA256: strings.Repeat("a", 64), ScannerImage: "sha256:" + strings.Repeat("b", 64), Snippets: []map[string]string{{"file": "agent.py", "text": "print(1)", "untrusted": "true"}}, AIReview: "manual_required"}, nil
	}
	token := readyUser(t, s, "review-team@example.test", "team")
	v, e := s.Upload(token, []byte("review frozen fixture"))
	if e != nil {
		t.Fatal(e)
	}
	return token, v
}
func TestAutomaticReviewFakeProviderRejectsWithoutCharging(t *testing.T) {
	s := testStore(t)
	team, v := advisoryVersion(t, s)
	admin := readyUser(t, s, "review-admin@example.test", "admin")
	w := review.NewWorker(review.FakeProvider{}, time.Second)
	worked, err := s.ProcessReviewOnce(context.Background(), w, "reviewer")
	if err != nil || !worked {
		t.Fatal(worked, err)
	}
	worked, err = s.ProcessReviewOnce(context.Background(), w, "reviewer")
	if err != nil || worked {
		t.Fatal("duplicate", worked, err)
	}
	var state string
	var ledger, count int
	s.DB.QueryRow("SELECT status FROM versions WHERE id=?", v.ID).Scan(&state)
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&ledger)
	s.DB.QueryRow("SELECT COUNT(*) FROM review_advisories").Scan(&count)
	if state != "rejected" || ledger != 0 || count != 1 {
		t.Fatal(state, ledger, count)
	}
	snapshot, e := s.AdminSnapshot(admin)
	if e != nil {
		t.Fatal(e)
	}
	data, _ := json.Marshal(snapshot)
	if !strings.Contains(string(data), "ai_advisory") || !strings.Contains(string(data), "advice_ready") {
		t.Fatal(string(data))
	}
	if _, e = s.AdminSnapshot(team); status(e) != 403 {
		t.Fatal("team access")
	}
	if e = s.Review(admin, v.ID, "approve", "staff", "Inspected frozen code", "local"); status(e) != 409 {
		t.Fatal("automatic rejection cannot be silently overridden", e)
	}
}
func TestAutomaticReviewFailuresConcurrentClaimsReject(t *testing.T) {
	s := testStore(t)
	_, v := advisoryVersion(t, s)
	w := review.NewWorker(advisoryProvider(func(context.Context, review.Request) ([]byte, error) { return nil, errors.New("private upstream key") }), time.Second)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := s.ProcessReviewOnce(context.Background(), w, "worker")
			if err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	var raw, state string
	var attempts int
	s.DB.QueryRow("SELECT result,attempts FROM review_advisories").Scan(&raw, &attempts)
	s.DB.QueryRow("SELECT status FROM versions WHERE id=?", v.ID).Scan(&state)
	if attempts != 1 || state != "rejected" || !strings.Contains(raw, "provider_error") || strings.Contains(raw, "private upstream key") {
		t.Fatal(state, attempts, raw)
	}
}

func TestAdvisoryChangesWhileRunningSuperseded(t *testing.T) {
	for _, change := range []string{"sha", "approved"} {
		t.Run(change, func(t *testing.T) {
			s := testStore(t)
			_, v := advisoryVersion(t, s)
			p := advisoryProvider(func(context.Context, review.Request) ([]byte, error) {
				if change == "sha" {
					s.DB.Exec("UPDATE versions SET sha256=? WHERE id=?", strings.Repeat("c", 64), v.ID)
				} else {
					s.DB.Exec("UPDATE versions SET status='approved' WHERE id=?", v.ID)
				}
				return []byte(`{"schema_version":"qiqc-advisory/v1","findings":[],"summary":"advice"}`), nil
			})
			_, err := s.ProcessReviewOnce(context.Background(), review.NewWorker(p, time.Second), "worker")
			if err != nil {
				t.Fatal(err)
			}
			var state string
			s.DB.QueryRow("SELECT state FROM review_advisories").Scan(&state)
			if state != "superseded" {
				t.Fatal(state)
			}
		})
	}
}
func TestReviewLeaseRecoversRestart(t *testing.T) {
	s := testStore(t)
	advisoryVersion(t, s)
	l, err := s.claimAdvisory("crashed", time.Second, reviewPolicyIdentity("disabled"))
	if err != nil || l == nil {
		t.Fatal(err)
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.DB.Close()
	reopened.Now = func() time.Time { return time.Now().Add(2 * time.Second) }
	worked, err := reopened.ProcessReviewOnce(context.Background(), review.NewWorker(nil, time.Second), "replacement")
	if err != nil || !worked {
		t.Fatal(worked, err)
	}
	var attempts int
	var raw string
	s.DB.QueryRow("SELECT attempts,result FROM review_advisories").Scan(&attempts, &raw)
	if attempts != 2 || !strings.Contains(raw, "provider_unavailable") {
		t.Fatal(attempts, raw)
	}
}
func TestInvalidOversizeReportNeverCallsProvider(t *testing.T) {
	s := testStore(t)
	_, v := advisoryVersion(t, s)
	s.DB.Exec("UPDATE versions SET report=? WHERE id=?", strings.Repeat("x", 256*1024+1), v.ID)
	called := false
	p := advisoryProvider(func(context.Context, review.Request) ([]byte, error) { called = true; return nil, nil })
	_, err := s.ProcessReviewOnce(context.Background(), review.NewWorker(p, time.Second), "worker")
	if err != nil || called {
		t.Fatal(err, called)
	}
	var raw string
	s.DB.QueryRow("SELECT result FROM review_advisories").Scan(&raw)
	if !strings.Contains(raw, "invalid_input") {
		t.Fatal(raw)
	}
}

func TestRejectedVersionIsNotSilentlyRereviewedAfterProviderChange(t *testing.T) {
	s := testStore(t)
	advisoryVersion(t, s)
	if worked, err := s.ProcessReviewOnce(context.Background(), review.NewWorker(nil, time.Second), "disabled"); err != nil || !worked {
		t.Fatal(worked, err)
	}
	if worked, err := s.ProcessReviewOnce(context.Background(), review.NewWorker(review.FakeProvider{}, time.Second), "enabled"); err != nil || worked {
		t.Fatal(worked, err)
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM review_advisories").Scan(&n)
	if n != 1 {
		t.Fatal("rejected decision unexpectedly replaced", n)
	}
}
func TestCompletedReviewPageDoesNotStarveNewVersions(t *testing.T) {
	s := testStore(t)
	_, v := advisoryVersion(t, s)
	var team, raw string
	s.DB.QueryRow("SELECT team_id,report FROM versions WHERE id=?", v.ID).Scan(&team, &raw)
	tx, err := s.DB.Begin()
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 501; i++ {
		vid := fmt.Sprintf("pressure-%04d", i)
		sha := fmt.Sprintf("%064x", i+1)
		if _, err = tx.Exec("INSERT INTO versions(id,team_id,sha256,name,status,report,reason,created) VALUES(?,?,?,'fixture','pending_review',?,'',?)", vid, team, sha, raw, float64(i)); err != nil {
			t.Fatal(err)
		}
		if i < 500 {
			sum := sha256.Sum256([]byte(raw))
			_, err = tx.Exec("INSERT INTO review_advisories(version_id,submission_sha,report_sha,policy_version,state,created) VALUES(?,?,?,?,'complete',0)", vid, sha, hex.EncodeToString(sum[:]), reviewPolicyIdentity("disabled"))
			if err != nil {
				t.Fatal(err)
			}
		}
	}
	if err = tx.Commit(); err != nil {
		t.Fatal(err)
	}
	worked, err := s.ProcessReviewOnce(context.Background(), review.NewWorker(nil, time.Second), "page-worker")
	if err != nil || !worked {
		t.Fatal(worked, err)
	}
	var state string
	s.DB.QueryRow("SELECT state FROM review_advisories WHERE version_id='pressure-0500'").Scan(&state)
	if state != "complete" {
		t.Fatal("starved later pending version", state)
	}
}

func TestLegacyAndMissingSnippetReportsNeverInvokeAI(t *testing.T) {
	for name, raw := range map[string]string{
		"legacy":  `{"risks":[],"snippets":[{"file":"agent.py","text":"print(1)"}]}`,
		"missing": `{"ruleset_version":"qiqc-archive/2","risks":[]}`,
		"empty":   `{"ruleset_version":"qiqc-archive/2","risks":[],"snippets":[]}`,
		"blank":   `{"ruleset_version":"qiqc-archive/2","risks":[],"snippets":[{"file":"agent.py","text":"  "}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			s := testStore(t)
			_, v := advisoryVersion(t, s)
			s.DB.Exec("UPDATE versions SET report=? WHERE id=?", raw, v.ID)
			called := false
			p := advisoryProvider(func(context.Context, review.Request) ([]byte, error) { called = true; return nil, nil })
			worked, err := s.ProcessReviewOnce(context.Background(), review.NewWorker(p, time.Second), "worker")
			if err != nil || !worked || called {
				t.Fatal(worked, err, called)
			}
			var stored, state, advice string
			s.DB.QueryRow("SELECT report,status FROM versions WHERE id=?", v.ID).Scan(&stored, &state)
			s.DB.QueryRow("SELECT result FROM review_advisories WHERE version_id=?", v.ID).Scan(&advice)
			if stored != raw || state != "rejected" || !strings.Contains(advice, "invalid_input") {
				t.Fatal("legacy report mutated or fake review", state, advice)
			}
		})
	}
}
