package platform

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/gaugeforge/qiqc-platform-go/internal/review"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type automaticReviewTransport func(*http.Request) (*http.Response, error)

func (f automaticReviewTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// This exercises the production parser with a local transport, not live AI.
func automaticReviewWorker(t *testing.T, response string, hook func(*http.Request) error) *review.Worker {
	t.Helper()
	provider, err := review.NewHTTPSProvider("synthetic-test-key-only", "tencent/hy4-preview", automaticReviewTransport(func(r *http.Request) (*http.Response, error) {
		if hook != nil {
			if err := hook(r); err != nil {
				return nil, err
			}
		}
		encoded, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]string{"content": response}}}})
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(encoded))), Header: make(http.Header)}, nil
	}))
	if err != nil {
		t.Fatal(err)
	}
	return review.NewWorker(provider, time.Second)
}

const cleanAutomaticAdvice = `{"schema_version":"qiqc-advisory/v1","findings":[],"summary":"No detected risks in the supplied snippets."}`

func TestAutomaticReviewApprovesImmutableSubmissionExactlyOnceWithoutAdmin(t *testing.T) {
	s := testStore(t)
	team, v := advisoryVersion(t, s)
	var raw string
	if err := s.DB.QueryRow("SELECT report FROM versions WHERE id=?", v.ID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	// Old advisory evidence is retained, but not reused as an automatic decision.
	if _, err := s.DB.Exec("INSERT INTO review_advisories(version_id,submission_sha,report_sha,policy_version,state,created) VALUES(?,?,?,?, 'complete',0)", v.ID, v.SHA256, shaBytes([]byte(raw)), review.PolicyVersion+"@openrouter-hy4-chat-v3"); err != nil {
		t.Fatal(err)
	}
	worker := automaticReviewWorker(t, cleanAutomaticAdvice, nil)
	var wg sync.WaitGroup
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := s.ProcessReviewOnce(context.Background(), worker, "automatic-test"); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	versions, err := s.Versions(team)
	if err != nil || len(versions) != 1 || versions[0].Status != "approved" {
		t.Fatal("automatic approval missing", err, versions)
	}
	for table, want := range map[string]int{"audit": 1, "review_advisories": 2, "ledger": 0, "batches": 0} {
		var n int
		if err := s.DB.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&n); err != nil || n != want {
			t.Fatal(table, n, err)
		}
	}
	var action, actor, after string
	if err := s.DB.QueryRow("SELECT action,actor,after_json FROM audit").Scan(&action, &actor, &after); err != nil || action != "automatic_review" || actor != "automatic-review" || !strings.Contains(after, automaticReviewPolicy) {
		t.Fatal("missing decision audit", err)
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	versions, err = reopened.Versions(team)
	if err != nil || versions[0].Status != "approved" {
		t.Fatal("restart lost decision", err)
	}
}
func TestAutomaticReviewFailClosedAndNeverCharge(t *testing.T) {
	cases := []struct {
		name, advice, code string
		prepare            func(*Store, Version)
		hook               func(*http.Request) error
		timeout            time.Duration
	}{
		{name: "static risk", advice: cleanAutomaticAdvice, code: "static_risk_detected", prepare: func(s *Store, v Version) {
			mutateAutomaticReport(t, s, v, func(r *Report) { r.Risks = []Risk{{Rule: "webhook", File: "agent.py"}} })
		}},
		{name: "AI risk", advice: `{"schema_version":"qiqc-advisory/v1","findings":[{"rule":"webhook","file":"agent.py","confidence":0.1,"explanation":"Possible exfiltration."}],"summary":"Risk detected."}`, code: "ai_risk_detected"},
		{name: "invalid JSON", advice: `{"approved":true}`, code: "invalid_response"},
		{name: "missing findings", advice: `{"schema_version":"qiqc-advisory/v1","summary":"No risks"}`, code: "invalid_response"},
		{name: "provider failure", advice: cleanAutomaticAdvice, code: "provider_error", hook: func(*http.Request) error { return errors.New("secret credential must not be recorded") }},
		{name: "timeout", advice: cleanAutomaticAdvice, code: "provider_timeout", timeout: time.Millisecond, hook: func(r *http.Request) error { <-r.Context().Done(); return r.Context().Err() }},
		{name: "invalid scanner provenance", advice: cleanAutomaticAdvice, code: "invalid_scanner_evidence", prepare: func(s *Store, v Version) { mutateAutomaticReport(t, s, v, func(r *Report) { r.ParserSHA256 = "" }) }},
		{name: "missing static findings", advice: cleanAutomaticAdvice, code: "invalid_scanner_evidence", prepare: func(s *Store, v Version) {
			mutateAutomaticReport(t, s, v, func(r *Report) { r.Risks = nil })
		}},
		{name: "wrong source digest", advice: cleanAutomaticAdvice, code: "invalid_scanner_evidence", prepare: func(s *Store, v Version) {
			mutateAutomaticReport(t, s, v, func(r *Report) { r.SourceSHA256 = strings.Repeat("a", 64) })
		}},
		{name: "tampered package", advice: cleanAutomaticAdvice, code: "frozen_package_invalid", prepare: func(s *Store, v Version) {
			if err := os.WriteFile(filepath.Join(s.Root, "uploads", v.SHA256+".zip"), []byte("changed bytes"), 0600); err != nil {
				t.Fatal(err)
			}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := testStore(t)
			team, v := advisoryVersion(t, s)
			if tc.prepare != nil {
				tc.prepare(s, v)
			}
			w := automaticReviewWorker(t, tc.advice, tc.hook)
			if tc.timeout != 0 {
				w.Timeout = tc.timeout
			}
			if worked, err := s.ProcessReviewOnce(context.Background(), w, "automatic-test"); err != nil || !worked {
				t.Fatal(worked, err)
			}
			if tc.code == "provider_error" || tc.code == "provider_timeout" {
				for retry := 0; retry < 2; retry++ {
					s.DB.Exec("UPDATE review_advisories SET lease_until=0 WHERE state='retry_wait'")
					if worked, err := s.ProcessReviewOnce(context.Background(), w, "automatic-test"); err != nil || !worked {
						t.Fatal(worked, err)
					}
				}
			}
			var state, reason string
			if err := s.DB.QueryRow("SELECT status,reason FROM versions WHERE id=?", v.ID).Scan(&state, &reason); err != nil || (tc.name == "static risk" && (state != "pending_review" || !strings.Contains(reason, "requiring organizer review"))) || (tc.name != "static risk" && (state != "rejected" || !strings.Contains(reason, tc.code))) || strings.Contains(reason, "secret credential") {
				t.Fatal(state, reason, err)
			}
			if tc.name == "static risk" {
				if _, err := s.Confirm(team, v.ID, "public", "mock", "refused-review"); err == nil {
					t.Fatal("unreviewed version entered queue")
				}
				return
			}
			if _, err := s.Confirm(team, v.ID, "public", "mock", "refused-review"); err == nil {
				t.Fatal("rejected version entered queue")
			}
			for _, table := range []string{"ledger", "batches"} {
				var n int
				s.DB.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&n)
				if n != 0 {
					t.Fatal("rejection charged or queued", table, n)
				}
			}
		})
	}
}
func mutateAutomaticReport(t *testing.T, s *Store, v Version, fn func(*Report)) {
	t.Helper()
	var raw string
	if err := s.DB.QueryRow("SELECT report FROM versions WHERE id=?", v.ID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var report Report
	if err := json.Unmarshal([]byte(raw), &report); err != nil {
		t.Fatal(err)
	}
	fn(&report)
	encoded, _ := json.Marshal(report)
	if _, err := s.DB.Exec("UPDATE versions SET report=? WHERE id=?", string(encoded), v.ID); err != nil {
		t.Fatal(err)
	}
}
func TestReferenceUploadCannotBypassAutomaticReview(t *testing.T) {
	s := testStore(t)
	withScanner(s)
	original := s.Scanner
	s.Scanner = func(path string) (Report, error) { r, e := original(path); r.Reference = true; return r, e }
	token := readyUser(t, s, "reference-review@example.test", "team")
	v, err := s.Upload(token, []byte("reference fixture"))
	if err != nil || v.Status != "pending_review" {
		t.Fatal("reference bypass", err, v.Status)
	}
}
func TestAutomaticReviewChangedReportIsSupersededWithoutDecision(t *testing.T) {
	s := testStore(t)
	_, v := advisoryVersion(t, s)
	w := automaticReviewWorker(t, cleanAutomaticAdvice, func(*http.Request) error {
		mutateAutomaticReport(t, s, v, func(r *Report) { r.Risks = []Risk{{File: "agent.py", Rule: "webhook"}} })
		return nil
	})
	if _, err := s.ProcessReviewOnce(context.Background(), w, "auto"); err != nil {
		t.Fatal(err)
	}
	var state string
	var decisions int
	s.DB.QueryRow("SELECT state FROM review_advisories").Scan(&state)
	s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action='automatic_review'").Scan(&decisions)
	if state != "superseded" || decisions != 0 {
		t.Fatal("changed evidence approved", state, decisions)
	}
}
