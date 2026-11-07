// Independent frozen blind validation. Fixture code is NEVER executed; expected labels are never loaded.
package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"github.com/gaugeforge/qiqc-platform-go/internal/review"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type budgetProvider struct {
	review.Provider
	mu      sync.Mutex
	Calls   int
	Limit   int
	journal *os.File
}

func (p *budgetProvider) Review(c context.Context, r review.Request) ([]byte, error) {
	// Hold the lock through the request: a timed-out worker cannot overlap the next request.
	p.mu.Lock()
	defer p.mu.Unlock()
	limit := p.Limit
	if limit == 0 {
		limit = 8
	}
	if p.Calls >= limit {
		return nil, errors.New("blind fixture call budget exhausted")
	}
	if err := c.Err(); err != nil {
		return nil, err
	}
	p.Calls++
	digest := sha256.Sum256(r.InputJSON)
	entry, _ := json.Marshal(map[string]any{"request": p.Calls, "input_sha256": hex.EncodeToString(digest[:]), "at": time.Now().UTC().Format(time.RFC3339Nano)})
	if _, err := p.journal.Write(append(entry, '\n')); err != nil {
		return nil, errors.New("cannot persist request budget")
	}
	if err := p.journal.Sync(); err != nil {
		return nil, errors.New("cannot persist request budget")
	}
	return p.Provider.Review(c, r)
}
func (p *budgetProvider) count() int { p.mu.Lock(); defer p.mu.Unlock(); return p.Calls }

type fixture struct {
	ID   string `json:"id"`
	Code string `json:"code"`
}
type frozenManifest struct {
	Schema string            `json:"schema"`
	Files  map[string]string `json:"files"`
}

func loadFrozenFixtures(root string) ([]fixture, string, error) {
	base := filepath.Join(root, "competition-platform/platform/tests/review_blindtest")
	raw, err := os.ReadFile(filepath.Join(base, "frozen-manifest.json"))
	if err != nil {
		return nil, "", errors.New("frozen manifest unavailable")
	}
	var m frozenManifest
	if json.Unmarshal(raw, &m) != nil || m.Schema != "qiqc-review-blind-freeze/v1" {
		return nil, "", errors.New("invalid frozen manifest")
	}
	// Expected-label file digest is recorded in manifest but its contents are never read here.
	for _, relative := range []string{
		"competition-platform/platform/tests/review_blindtest/fixtures.json",
		"competition-platform/platform-go/internal/review/review.go",
		"competition-platform/platform-go/internal/review/provider.go",
		"competition-platform/platform/qiqc/archive.py",
	} {
		content, e := os.ReadFile(filepath.Join(root, relative))
		if e != nil {
			return nil, "", errors.New("frozen source unavailable")
		}
		digest := sha256.Sum256(content)
		if m.Files[relative] != hex.EncodeToString(digest[:]) {
			return nil, "", errors.New("frozen source digest mismatch")
		}
	}
	data, err := os.ReadFile(filepath.Join(base, "fixtures.json"))
	if err != nil {
		return nil, "", errors.New("fixtures unavailable")
	}
	var f struct {
		Schema   string    `json:"schema"`
		Fixtures []fixture `json:"fixtures"`
	}
	if json.Unmarshal(data, &f) != nil || f.Schema != "qiqc-review-blind-fixtures/v1" || len(f.Fixtures) != 8 {
		return nil, "", errors.New("invalid blind fixtures")
	}
	seen := map[string]bool{}
	for _, item := range f.Fixtures {
		if len(item.ID) != 3 || item.ID[0] != 'b' || len(item.Code) == 0 || len(item.Code) > 2048 || seen[item.ID] {
			return nil, "", errors.New("invalid bounded fixture")
		}
		seen[item.ID] = true
	}
	digest := sha256.Sum256(raw)
	return f.Fixtures, hex.EncodeToString(digest[:]), nil
}

// This separate one-case smoke is never scored as blind validation.
func loadContractSmoke(root string) ([]fixture, string, error) {
	if review.PolicyVersion != "qiqc-readonly-advisory/v2" {
		return nil, "", errors.New("contract smoke requires corrected v2 policy")
	}
	base := filepath.Join(root, "competition-platform/platform/tests/review_blindtest")
	raw, err := os.ReadFile(filepath.Join(base, "contract-smoke-manifest.json"))
	if err != nil {
		return nil, "", errors.New("smoke freeze unavailable")
	}
	var manifest frozenManifest
	if json.Unmarshal(raw, &manifest) != nil || manifest.Schema != "qiqc-review-contract-smoke-freeze/v1" {
		return nil, "", errors.New("invalid smoke freeze")
	}
	for _, relative := range []string{"competition-platform/platform/tests/review_blindtest/contract-smoke-fixture.json", "competition-platform/platform-go/internal/review/review.go", "competition-platform/platform-go/internal/review/provider.go", "competition-platform/platform/qiqc/archive.py"} {
		content, e := os.ReadFile(filepath.Join(root, relative))
		if e != nil {
			return nil, "", errors.New("smoke source unavailable")
		}
		digest := sha256.Sum256(content)
		if manifest.Files[relative] != hex.EncodeToString(digest[:]) {
			return nil, "", errors.New("smoke source changed")
		}
	}
	data, err := os.ReadFile(filepath.Join(base, "contract-smoke-fixture.json"))
	if err != nil {
		return nil, "", errors.New("smoke fixture unavailable")
	}
	var input struct {
		Schema  string  `json:"schema"`
		Fixture fixture `json:"fixture"`
	}
	if json.Unmarshal(data, &input) != nil || input.Schema != "qiqc-review-contract-smoke/v1" || input.Fixture.ID != "s01" || len(input.Fixture.Code) == 0 || len(input.Fixture.Code) > 2048 {
		return nil, "", errors.New("invalid smoke fixture")
	}
	digest := sha256.Sum256(raw)
	return []fixture{input.Fixture}, hex.EncodeToString(digest[:]), nil
}

type row struct {
	Fixture        string        `json:"fixture"`
	SubmissionSHA  string        `json:"submission_sha256"`
	ScannerVersion string        `json:"scanner_version"`
	ParserSHA      string        `json:"parser_sha256"`
	ScannerImage   string        `json:"scanner_image"`
	ScannerRules   []string      `json:"scanner_rules"`
	Status         string        `json:"version_status"`
	Ledger         int           `json:"ledger_rows"`
	AdminVisible   bool          `json:"admin_visible"`
	Advice         review.Advice `json:"advice"`
	Seconds        float64       `json:"seconds"`
}
type report struct {
	Started   string `json:"started_at"`
	Mode      string `json:"mode"`
	FreezeSHA string `json:"freeze_manifest_sha256"`
	Calls     int    `json:"provider_calls"`
	Rows      []row  `json:"fixtures"`
	Passed    bool   `json:"system_invariants_passed"`
	Cleaned   bool   `json:"temporary_resources_cleaned"`
	Failure   string `json:"failure,omitempty"`
}

func main() {
	root := flag.String("root", "", "QQ-Challenge project root")
	out := flag.String("out", "", "report file")
	smoke := flag.Bool("contract-smoke", false, "one new v2 contract smoke request, separately authorized; never a blind rerun")
	v3 := flag.Bool("v3-quality", false, "new frozen v3 quality corpus: at most 24 serial requests, no retries")
	real := flag.Bool("real", false, "permit real model requests only for the selected frozen suite")
	flag.Parse()
	if *root == "" || *out == "" || !*real || (*smoke && *v3) {
		fmt.Fprintln(os.Stderr, "requires --root --out --real and at most one suite flag; v1 up to 8, v3 up to 24 billed requests; no retries")
		os.Exit(2)
	}
	journal, err := os.OpenFile(*out+".started", os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		fmt.Fprintln(os.Stderr, "refusing to reuse an existing run journal or unavailable output directory")
		os.Exit(2)
	}
	defer journal.Close()
	reserved, err := os.OpenFile(*out, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		fmt.Fprintln(os.Stderr, "refusing to overwrite existing acceptance report")
		os.Exit(2)
	}
	reserved.Close()
	r := report{Started: time.Now().UTC().Format(time.RFC3339), Mode: "independent-frozen-blind-real-docker-scanner-openrouter-hy4", Rows: []row{}}
	err = run(*root, &r, journal, *out, *smoke, *v3)
	if err != nil {
		r.Failure = err.Error()
	}
	data, _ := json.MarshalIndent(r, "", "  ")
	if e := os.WriteFile(*out, append(data, '\n'), 0600); e != nil {
		fmt.Fprintln(os.Stderr, "cannot write acceptance report")
		os.Exit(1)
	}
	fmt.Printf("review fixtures=%d model_calls=%d passed=%t cleaned=%t\n", len(r.Rows), r.Calls, r.Passed, r.Cleaned)
	if err != nil || !r.Passed {
		os.Exit(1)
	}
}
func loadConfig(root string) (string, error) {
	data, err := os.ReadFile(filepath.Join(root, ".env"))
	if err != nil {
		return "", errors.New("private model configuration unavailable")
	}
	values := map[string]string{}
	for _, line := range strings.Split(string(data), "\n") {
		key, value, ok := strings.Cut(line, "=")
		if ok && (strings.TrimSpace(key) == "HY_API" || strings.TrimSpace(key) == "HY_URL") {
			values[strings.TrimSpace(key)] = strings.Trim(strings.TrimSpace(value), "\"'")
		}
	}
	endpoint, err := url.Parse(values["HY_URL"])
	if err != nil || endpoint.Scheme != "https" || endpoint.Hostname() != "openrouter.ai" || endpoint.User != nil {
		return "", errors.New("configured model host does not match fixed reviewer endpoint")
	}
	return values["HY_API"], nil
}
func run(root string, r *report, journal *os.File, out string, smoke bool, v3 bool) error {
	var fixtures []fixture
	var freezeSHA string
	var err error
	if v3 {
		r.Mode = "v3-independent-frozen-quality-real-docker-openrouter"
		fixtures, freezeSHA, err = loadV3Quality(root)
	} else if smoke {
		r.Mode = "v2-contract-single-new-fixture-smoke-not-blind"
		fixtures, freezeSHA, err = loadContractSmoke(root)
	} else {
		fixtures, freezeSHA, err = loadFrozenFixtures(root)
	}
	if err != nil {
		return err
	}
	r.FreezeSHA = freezeSHA
	key, err := loadConfig(root)
	if err != nil {
		return err
	}
	live, err := review.NewHTTPSProvider(key, "tencent/hy4-preview", nil)
	if err != nil {
		return errors.New("reviewer model configuration invalid")
	}
	provider := &budgetProvider{Provider: live, journal: journal, Limit: len(fixtures)}
	defer func() { r.Calls = provider.count() }()
	dir, err := os.MkdirTemp("", "qiqc-review-acceptance-")
	if err != nil {
		return errors.New("cannot create private fixture directory")
	}
	defer func() { r.Cleaned = os.RemoveAll(dir) == nil }()
	s, err := platform.Open(filepath.Join(dir, "platform.sqlite"))
	if err != nil {
		return errors.New("fixture database creation failed")
	}
	defer s.Close()
	s.Scanner = platform.DockerScanner(filepath.Join(root, "competition-platform/platform/qiqc/archive.py"), "python:3.13-slim")
	passwordBytes := make([]byte, 18)
	if _, err = rand.Read(passwordBytes); err != nil {
		return errors.New("fixture credential generation failed")
	}
	password := hex.EncodeToString(passwordBytes)
	login := func(email, role string) (string, error) {
		if _, e := s.CreateAccount(email, password, "Public review fixture", role); e != nil {
			return "", e
		}
		token, _, e := s.Login(email, password, "127.0.0.1")
		if e != nil {
			return "", e
		}
		e = s.ChangePassword(token, password+"-changed")
		return token, e
	}
	captain, err := login("fixture-team@example.invalid", "team")
	if err != nil {
		return errors.New("fixture login failed")
	}
	admin, err := login("fixture-admin@example.invalid", "admin")
	if err != nil {
		return errors.New("fixture admin failed")
	}
	for _, fixture := range fixtures {
		started := time.Now()
		data, e := fixtureZIP(fixture.ID, fixture.Code)
		if e != nil {
			return errors.New("fixture zip failed")
		}
		v, e := s.Upload(captain, data)
		if e != nil {
			return errors.New("actual isolated scanner rejected fixture or unavailable")
		}
		worked, e := s.ProcessReviewOnce(context.Background(), review.NewWorker(provider, 120*time.Second), "real-public-fixture-reviewer")
		if e != nil || !worked {
			return errors.New("advisory worker failed")
		}
		var raw, scan string
		entry := row{Fixture: fixture.ID, SubmissionSHA: v.SHA256, Seconds: time.Since(started).Seconds()}
		if e = s.DB.QueryRow("SELECT status,report FROM versions WHERE id=?", v.ID).Scan(&entry.Status, &scan); e != nil {
			return errors.New("fixture state unavailable")
		}
		var provenance platform.Report
		json.Unmarshal([]byte(scan), &provenance)
		entry.ScannerVersion = provenance.RulesetVersion
		entry.ParserSHA = provenance.ParserSHA256
		entry.ScannerImage = provenance.ScannerImage
		entry.ScannerRules = []string{}
		seenRules := map[string]bool{}
		for _, risk := range provenance.Risks {
			if !seenRules[risk.Rule] {
				entry.ScannerRules = append(entry.ScannerRules, risk.Rule)
				seenRules[risk.Rule] = true
			}
		}
		s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&entry.Ledger)
		if e = s.DB.QueryRow("SELECT result FROM review_advisories WHERE version_id=?", v.ID).Scan(&raw); e != nil {
			return errors.New("advisory result unavailable")
		}
		if json.Unmarshal([]byte(raw), &entry.Advice) != nil {
			return errors.New("advisory evidence invalid")
		}
		snapshot, e := s.AdminSnapshot(admin)
		if e != nil {
			return errors.New("admin evidence unavailable")
		}
		encoded, _ := json.Marshal(snapshot)
		entry.AdminVisible = bytes.Contains(encoded, []byte(entry.Advice.InputSHA256)) && bytes.Contains(encoded, []byte("ai_advisory"))
		r.Rows = append(r.Rows, entry)
		r.Calls = provider.count()
		checkpoint, _ := json.MarshalIndent(r, "", "  ")
		if os.WriteFile(out, append(checkpoint, '\n'), 0600) != nil {
			return errors.New("cannot persist blind result checkpoint")
		}
		fmt.Printf("fixture=%s outcome=%s calls=%d manual=%s quota_rows=%d seconds=%.3f\n", entry.Fixture, entry.Advice.Outcome, r.Calls, entry.Advice.Status, entry.Ledger, entry.Seconds)
	}
	r.Passed = len(r.Rows) == len(fixtures) && provider.count() == len(fixtures)
	for _, entry := range r.Rows {
		if smoke {
			r.Passed = r.Passed && entry.Advice.Outcome == "advice_ready"
		}
		// Detection quality and invalid responses are reported separately, never converted to approval.
		r.Passed = r.Passed && entry.Status == "pending_review" && entry.Ledger == 0 && entry.AdminVisible && entry.Advice.Status == "manual_required" && entry.Advice.SHA256 == entry.SubmissionSHA
	}
	return nil
}
func fixtureZIP(name, code string) ([]byte, error) {
	var buf bytes.Buffer
	z := zip.NewWriter(&buf)
	for _, file := range []struct{ name, body string }{{"harness.json", `{"schema":"qiqc-harness/v1-demo","name":"` + name + ` public review fixture","entrypoint":"agent.py"}`}, {"agent.py", code}} {
		h := &zip.FileHeader{Name: file.name, Method: zip.Store}
		h.SetMode(0644)
		w, e := z.CreateHeader(h)
		if e != nil {
			return nil, e
		}
		if _, e = w.Write([]byte(file.body)); e != nil {
			return nil, e
		}
	}
	if e := z.Close(); e != nil {
		return nil, e
	}
	return buf.Bytes(), nil
}
