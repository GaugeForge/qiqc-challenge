package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gaugeforge/qiqc-platform-go/internal/review"
)

type probeProvider struct{ calls int }

func (p *probeProvider) Version() string { return "offline-blindcheck-probe" }
func (p *probeProvider) Review(_ context.Context, _ review.Request) ([]byte, error) {
	p.calls++
	return []byte(`{}`), nil
}

func TestBudgetExactlyEightAndNoRetry(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "journal-")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	spy := &probeProvider{}
	p := &budgetProvider{Provider: spy, journal: file}
	for i := 0; i < 8; i++ {
		if _, err = p.Review(context.Background(), review.Request{InputJSON: []byte(`{"snippets":[]}`)}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = p.Review(context.Background(), review.Request{}); err == nil {
		t.Fatal("ninth request permitted")
	}
	if spy.calls != 8 || p.count() != 8 {
		t.Fatalf("calls=%d count=%d", spy.calls, p.count())
	}
	data, err := os.ReadFile(file.Name())
	if err != nil {
		t.Fatal(err)
	}
	if len(strings.Split(strings.TrimSpace(string(data)), "\n")) != 8 {
		t.Fatal("budget journal does not contain eight records")
	}
	if strings.Contains(string(data), "snippets") {
		t.Fatal("journal contains source input rather than digest")
	}
}
func TestCancelledRequestNeverCallsProvider(t *testing.T) {
	file, _ := os.CreateTemp(t.TempDir(), "journal-")
	defer file.Close()
	spy := &probeProvider{}
	p := &budgetProvider{Provider: spy, journal: file}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.Review(ctx, review.Request{}); !errors.Is(err, context.Canceled) {
		t.Fatal("cancellation not enforced")
	}
	if spy.calls != 0 || p.count() != 0 {
		t.Fatal("cancelled request consumed model call")
	}
}
func TestFrozenInputsAndLabelsAreSeparate(t *testing.T) {
	root, err := filepath.Abs("../../../..")
	if err != nil {
		t.Fatal(err)
	}
	base := filepath.Join(root, "competition-platform/platform/tests/review_blindtest")
	raw, err := os.ReadFile(filepath.Join(base, "frozen-manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m frozenManifest
	if json.Unmarshal(raw, &m) != nil {
		t.Fatal("invalid freeze")
	}
	data, err := os.ReadFile(filepath.Join(base, "fixtures.json"))
	if err != nil {
		t.Fatal(err)
	}
	fixtureHash := sha256.Sum256(data)
	if m.Files["competition-platform/platform/tests/review_blindtest/fixtures.json"] != hex.EncodeToString(fixtureHash[:]) {
		t.Fatal("historical fixtures were modified")
	}
	fixtures, sha, err := loadFrozenFixtures(root)
	currentPolicy, _ := os.ReadFile(filepath.Join(root, "competition-platform/platform-go/internal/review/review.go"))
	policyHash := sha256.Sum256(currentPolicy)
	if m.Files["competition-platform/platform-go/internal/review/review.go"] != hex.EncodeToString(policyHash[:]) {
		if err == nil {
			t.Fatal("old blind run allowed against a changed policy")
		}
	} else if err != nil || len(fixtures) != 8 || len(sha) != 64 {
		t.Fatalf("invalid frozen dataset: %v", err)
	}
	// Source contract forbids reading expected-labels; scoring is a separate offline program.
	source, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(source), "expected-labels.json") {
		t.Fatal("model runner must not read labels")
	}
}

func TestSmokeBudgetExactlyOne(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "journal-")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	spy := &probeProvider{}
	p := &budgetProvider{Provider: spy, journal: file, Limit: 1}
	if _, err = p.Review(context.Background(), review.Request{}); err != nil {
		t.Fatal(err)
	}
	if _, err = p.Review(context.Background(), review.Request{}); err == nil {
		t.Fatal("second smoke request permitted")
	}
	if spy.calls != 1 {
		t.Fatal("smoke budget exceeded")
	}
}
