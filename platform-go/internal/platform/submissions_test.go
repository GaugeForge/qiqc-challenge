package platform

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestTeamVersionsExposeBoundedReviewLocationsOnly(t *testing.T) {
	s := testStore(t)
	s.Scanner = func(string) (Report, error) {
		return Report{
			Manifest: Manifest{Schema: "qiqc-harness/v1-demo", Name: "risk fixture", Entrypoint: "adapter.sh"},
			Files:    2,
			Risks:    []Risk{{File: "harness/qiqc_agent/config.py", Rule: "external network address"}},
			AIReview: "not_connected_manual_review_required",
		}, nil
	}
	tok := readyUser(t, s, "review-team@example.test", "team")
	v, err := s.Upload(tok, []byte("review fixture bytes"))
	if err != nil {
		t.Fatal(err)
	}
	var raw string
	if err = s.DB.QueryRow("SELECT report FROM versions WHERE id=?", v.ID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte(raw))
	advice := `{"status":"manual_required","outcome":"advice_ready","summary":"Review the flagged endpoint.","findings":[{"rule":"external_endpoint","file":"harness/qiqc_agent/config.py","confidence":0.35,"explanation":"The scanner flagged a default internal qsim URL; no external destination was shown."}],"manual_followup_required":true,"uncovered_scanner_risks":1}`
	if _, err = s.DB.Exec(`INSERT INTO review_advisories(version_id,submission_sha,report_sha,policy_version,state,result,created,finished) VALUES(?,?,?,?,?,?,?,?)`, v.ID, v.SHA256, hex.EncodeToString(digest[:]), "test-policy", "complete", advice, s.now(), s.now()); err != nil {
		t.Fatal(err)
	}
	versions, err := s.Versions(tok)
	if err != nil || len(versions) != 1 || versions[0].Review == nil {
		t.Fatalf("review not returned: %+v %v", versions, err)
	}
	got := versions[0].Review
	if len(got.StaticRisks) != 1 || got.StaticRisks[0].File != "harness/qiqc_agent/config.py" {
		t.Fatalf("static location missing: %+v", got.StaticRisks)
	}
	if got.AIAdvisory == nil || len(got.AIAdvisory.Findings) != 1 || got.AIAdvisory.Findings[0].Explanation == "" {
		t.Fatalf("advisory details missing: %+v", got.AIAdvisory)
	}
	encoded, _ := json.Marshal(versions[0])
	if string(encoded) == "" || string(encoded) == "{}" {
		t.Fatal("empty version response")
	}
}

func withScanner(s *Store) {
	s.Scanner = func(path string) (Report, error) {
		return Report{Manifest: Manifest{Schema: "qiqc-harness/v1-demo", Name: "test", Entrypoint: "adapter.sh"}, Files: 2, Risks: []Risk{}, AIReview: "not_connected_manual_review_required"}, nil
	}
}
func readyUser(t *testing.T, s *Store, email, role string) string {
	t.Helper()
	account(t, s, email, role)
	tok, _, err := s.Login(email, "Initial-password-123", "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if err = s.ChangePassword(tok, "Replacement-password-123"); err != nil {
		t.Fatal(err)
	}
	return tok
}
func TestSubmissionFrozenConcurrentAndTeamPrivate(t *testing.T) {
	s := testStore(t)
	withScanner(s)
	tok := readyUser(t, s, "a@example.test", "team")
	other := readyUser(t, s, "b@example.test", "team")
	var wg sync.WaitGroup
	ids := make(chan string, 12)
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			v, err := s.Upload(tok, []byte("opaque bytes inspected by fixture"))
			if err != nil {
				t.Error(err)
				return
			}
			ids <- v.ID
		}()
	}
	wg.Wait()
	close(ids)
	seen := map[string]bool{}
	for id := range ids {
		seen[id] = true
	}
	if len(seen) != 1 {
		t.Fatal("duplicate version")
	}
	vs, err := s.Versions(tok)
	if err != nil || len(vs) != 1 || vs[0].Status != "pending_review" {
		t.Fatal("version contract")
	}
	others, err := s.Versions(other)
	if err != nil || len(others) != 0 {
		t.Fatal("cross team leak")
	}
	p := filepath.Join(s.Root, "uploads", vs[0].SHA256+".zip")
	b, err := os.ReadFile(p)
	if err != nil || string(b) != "opaque bytes inspected by fixture" {
		t.Fatal("bytes changed")
	}
	if err = s.CheckVersion(vs[0].SHA256); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(p, []byte("tamper"), 0600)
	if err = s.CheckVersion(vs[0].SHA256); err == nil {
		t.Fatal("tamper accepted")
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&n)
	if n != 0 {
		t.Fatal("upload charged quota")
	}
}
func TestScannerFailureCleansQuarantineAndNeverApproves(t *testing.T) {
	s := testStore(t)
	tok := readyUser(t, s, "a@example.test", "team")
	s.Scanner = func(string) (Report, error) { return Report{}, errors.New("scanner unavailable") }
	if _, err := s.Upload(tok, []byte("untrusted")); status(err) != 503 {
		t.Fatal(err)
	}
	files, _ := os.ReadDir(filepath.Join(s.Root, "quarantine"))
	if len(files) != 0 {
		t.Fatal("quarantine leak")
	}
	vs, _ := s.Versions(tok)
	if len(vs) != 0 {
		t.Fatal("failed scanner stored version")
	}
}
func TestReviewRequiresAdminAndBindsImmutableBytes(t *testing.T) {
	s := testStore(t)
	withScanner(s)
	tok := readyUser(t, s, "a@example.test", "team")
	admin := readyUser(t, s, "admin@example.test", "admin")
	v, err := s.Upload(tok, []byte("inspected version"))
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Review(tok, v.ID, "approve", "actor", "reviewed", "127.0.0.1"); status(err) != 403 {
		t.Fatal("nonadmin review")
	}
	if err = s.Review(admin, v.ID, "approve", "actor", "", "127.0.0.1"); status(err) != 400 {
		t.Fatal("no reason")
	}
	if err = s.Review(admin, v.ID, "approve", "actor", "reviewed version", "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	if err = s.Review(admin, v.ID, "reject", "actor", "duplicate review", "127.0.0.1"); status(err) != 409 {
		t.Fatal("repeated review")
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action='review'").Scan(&n)
	if n != 1 {
		t.Fatal("review audit")
	}
}
