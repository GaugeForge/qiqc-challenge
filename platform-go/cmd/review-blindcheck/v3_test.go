package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"github.com/gaugeforge/qiqc-platform-go/internal/review"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestV3FrozenLoaderNeverNeedsLabelsAndRejectsDrift(t *testing.T) {
	root, err := filepath.Abs("../../../..")
	if err != nil {
		t.Fatal(err)
	}
	base := "competition-platform/platform/tests/review_blindtest/v3/"
	raw, err := os.ReadFile(filepath.Join(root, base, "frozen-manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest frozenManifest
	if err = json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	isolated := t.TempDir()
	for name := range manifest.Files {
		if strings.HasSuffix(name, "expected-labels.json") {
			continue
		}
		data, e := os.ReadFile(filepath.Join(root, name))
		if e != nil {
			t.Fatal(e)
		}
		target := filepath.Join(isolated, name)
		os.MkdirAll(filepath.Dir(target), 0700)
		if e = os.WriteFile(target, data, 0600); e != nil {
			t.Fatal(e)
		}
	}
	// The checked-in v3 quality result is historical evidence. Do not rewrite
	// it to bless the current policy. Build a temporary unit-test manifest for
	// today's sources; this tests loader behavior, not AI quality acceptance.
	var current map[string]any
	if err = json.Unmarshal(raw, &current); err != nil {
		t.Fatal(err)
	}
	if current["policy_version"] != review.PolicyVersion {
		if _, _, err = loadV3Quality(root); err == nil {
			t.Fatal("historical policy accepted as current")
		}
	}
	current["policy_version"] = review.PolicyVersion
	hashes := map[string]string{}
	for name := range manifest.Files {
		if strings.HasSuffix(name, "expected-labels.json") {
			continue
		}
		data, e := os.ReadFile(filepath.Join(isolated, name))
		if e != nil {
			t.Fatal(e)
		}
		digest := sha256.Sum256(data)
		hashes[name] = hex.EncodeToString(digest[:])
	}
	current["files"] = hashes
	prepared, err := json.Marshal(current)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(isolated, base, "frozen-manifest.json"), prepared, 0600); err != nil {
		t.Fatal(err)
	}
	rows, hash, err := loadV3Quality(isolated)
	if err != nil || len(rows) != 24 || len(hash) != 64 {
		t.Fatalf("label-free load failed: %v", err)
	}
	os.WriteFile(filepath.Join(isolated, base, "fixtures.json"), []byte("changed corpus"), 0600)
	if _, _, err = loadV3Quality(isolated); err == nil {
		t.Fatal("changed frozen input accepted")
	}
}

func TestV3BudgetTwentyFourNeverCallsTwentyFifth(t *testing.T) {
	f, err := os.CreateTemp(t.TempDir(), "budget-")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	spy := &probeProvider{}
	p := budgetProvider{Provider: spy, Limit: 24, journal: f}
	for i := 0; i < 24; i++ {
		if _, err = p.Review(context.Background(), review.Request{InputJSON: []byte("{}")}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = p.Review(context.Background(), review.Request{}); err == nil || spy.calls != 24 {
		t.Fatal("provider budget escaped")
	}
}
