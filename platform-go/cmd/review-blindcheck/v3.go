package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/gaugeforge/qiqc-platform-go/internal/review"
)

// Labels are not read, decoded, or passed into the review worker. Only the
// explicit allowlist of source/corpus files is opened by this provider runner.
func loadV3Quality(root string) ([]fixture, string, error) {
	base := "competition-platform/platform/tests/review_blindtest/v3/"
	raw, err := os.ReadFile(filepath.Join(root, base, "frozen-manifest.json"))
	if err != nil {
		return nil, "", errors.New("v3 freeze unavailable")
	}
	var manifest struct {
		Schema  string            `json:"schema"`
		Policy  string            `json:"policy_version"`
		Maximum int               `json:"maximum_requests"`
		Files   map[string]string `json:"files"`
	}
	if json.Unmarshal(raw, &manifest) != nil || manifest.Schema != "qiqc-review-v3-freeze/v1" || manifest.Policy != review.PolicyVersion || manifest.Maximum != 24 {
		return nil, "", errors.New("invalid v3 quality freeze")
	}
	for _, relative := range []string{base + "fixtures.json", base + "evaluate.py",
		"competition-platform/platform-go/internal/review/review.go", "competition-platform/platform-go/internal/review/provider.go",
		"competition-platform/platform/qiqc/archive.py", "competition-platform/platform-go/cmd/review-blindcheck/main.go",
		"competition-platform/platform-go/cmd/review-blindcheck/v3.go"} {
		data, e := os.ReadFile(filepath.Join(root, relative))
		if e != nil {
			return nil, "", errors.New("v3 source unavailable")
		}
		h := sha256.Sum256(data)
		if manifest.Files[relative] != hex.EncodeToString(h[:]) {
			return nil, "", errors.New("v3 frozen source changed")
		}
	}
	data, err := os.ReadFile(filepath.Join(root, base, "fixtures.json"))
	if err != nil {
		return nil, "", err
	}
	var corpus struct {
		Schema   string    `json:"schema"`
		Fixtures []fixture `json:"fixtures"`
	}
	if json.Unmarshal(data, &corpus) != nil || corpus.Schema != "qiqc-review-blind-fixtures/v1" || len(corpus.Fixtures) != 24 {
		return nil, "", errors.New("invalid v3 corpus")
	}
	for i, f := range corpus.Fixtures {
		if f.ID != fmt.Sprintf("v%02d", i+1) || len(f.Code) == 0 || len(f.Code) > 2048 {
			return nil, "", errors.New("invalid v3 bounded fixture")
		}
	}
	h := sha256.Sum256(raw)
	return corpus.Fixtures, hex.EncodeToString(h[:]), nil
}
