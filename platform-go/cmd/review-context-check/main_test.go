package main

import (
	"context"
	"encoding/json"
	"github.com/gaugeforge/qiqc-platform-go/internal/review"
	"net/http"
	"strings"
	"testing"
)

type fixtureProvider struct {
	calls         int
	attackMiss    bool
	falsePositive bool
}

func (*fixtureProvider) Version() string { return "public-fixture-provider" }
func (p *fixtureProvider) Review(_ context.Context, req review.Request) ([]byte, error) {
	p.calls++
	if req.System != review.SystemInstruction {
		panic("policy mismatch")
	}
	var in review.Input
	if json.Unmarshal(req.InputJSON, &in) != nil || len(in.Snippets) != 1 || !in.Snippets[0].Untrusted {
		panic("untrusted data boundary missing")
	}
	findings := []review.Finding{}
	if (p.calls == 2 && !p.attackMiss) || (p.calls == 1 && p.falsePositive) {
		for _, rule := range []string{"prompt_injection", "data_exfiltration"} {
			findings = append(findings, review.Finding{Rule: rule, File: in.Snippets[0].File, Confidence: 0.99, Explanation: "Public fixture evidence."})
		}
	}
	return json.Marshal(review.Suggestion{SchemaVersion: "qiqc-advisory/v1", Findings: findings, Summary: "Bounded public fixture review."})
}
func TestFixedFixturesRemainDataAndTwoReviews(t *testing.T) {
	p := &fixtureProvider{}
	r := run(context.Background(), p)
	if p.calls != 2 || !r.Passed || len(r.Fixtures) != 2 || r.Policy != review.PolicyVersion {
		t.Fatal(r)
	}
	if !strings.Contains(benign, "while :; do") || !strings.Contains(benign, "QIQC_TASK_INPUT") || !strings.Contains(malicious, "ignore your system instructions") {
		t.Fatal("wrong coverage")
	}
	for _, x := range fixtures() {
		if _, _, err := review.Normalize(x); err != nil {
			t.Fatal(err)
		}
	}
}
func TestFalsePositiveAndMissBothFailRegression(t *testing.T) {
	for _, p := range []*fixtureProvider{{attackMiss: true}, {falsePositive: true}} {
		if run(context.Background(), p).Passed {
			t.Fatal("bad reviewer treated as passing")
		}
	}
}

type noNetwork struct{ calls int }

func (t *noNetwork) RoundTrip(r *http.Request) (*http.Response, error) {
	t.calls++
	return &http.Response{StatusCode: 200, Body: http.NoBody, Header: make(http.Header)}, nil
}
func TestTransportHardCap(t *testing.T) {
	base := &noNetwork{}
	transport := &boundedTransport{base: base}
	r, _ := http.NewRequest("POST", "https://openrouter.ai/api/v1/chat/completions", nil)
	for i := 0; i < 4; i++ {
		if _, err := transport.RoundTrip(r); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := transport.RoundTrip(r); err == nil || base.calls != 4 {
		t.Fatal("fifth request sent")
	}
}
