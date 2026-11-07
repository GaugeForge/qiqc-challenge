package review

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type repairProvider struct {
	calls atomic.Int32
	fn    func(context.Context, Request, int) ([]byte, error)
}

func (*repairProvider) Version() string { return "format-repair-fixture" }
func (p *repairProvider) Review(ctx context.Context, r Request) ([]byte, error) {
	return p.fn(ctx, r, int(p.calls.Add(1)))
}
func (p *repairProvider) ReviewWithDiagnostics(ctx context.Context, r Request) ([]byte, *CompletionDiagnostics, error) {
	n := int(p.calls.Add(1))
	raw, err := p.fn(ctx, r, n)
	tokens := n * 10
	return raw, &CompletionDiagnostics{FinishReason: "stop", CompletionTokens: &tokens, MaxTokens: 8192, ReasoningEffort: "none"}, err
}
func repairJSON(t *testing.T, summary, explanation string) []byte {
	t.Helper()
	raw, err := json.Marshal(Suggestion{SchemaVersion: "qiqc-advisory/v1", Findings: []Finding{{Rule: "data_exfiltration", File: "agent.py", Confidence: 0.9, Explanation: explanation}}, Summary: summary})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
func TestBoundsOnlyRepairPreservesRiskAndBothUsageRecords(t *testing.T) {
	for _, which := range []string{"summary_bounds", "explanation_bounds"} {
		t.Run(which, func(t *testing.T) {
			first := repairJSON(t, strings.Repeat("长", 201), "Task content leaves the container.")
			if which == "explanation_bounds" {
				first = repairJSON(t, "Task data theft found.", strings.Repeat("x", 241))
			}
			second := repairJSON(t, "Task data theft found.", "Task content leaves the container.")
			p := &repairProvider{fn: func(ctx context.Context, r Request, n int) ([]byte, error) {
				if n == 1 {
					return first, nil
				}
				if n != 2 || !strings.Contains(r.System, formatRepairInstruction) {
					t.Error("unbounded or missing trusted correction")
				}
				var input struct {
					Original json.RawMessage `json:"original_untrusted_input"`
					Previous Suggestion      `json:"previous_untrusted_advice"`
				}
				if json.Unmarshal(r.InputJSON, &input) != nil || len(input.Previous.Findings) != 1 || input.Previous.Findings[0].Rule != "data_exfiltration" {
					t.Error("risk dropped from repair input")
				}
				return second, nil
			}}
			a := NewWorker(p, time.Second).Review(context.Background(), fixture())
			if a.Outcome != "advice_ready" || a.FormatRepairAttempts != 1 || a.FirstFormatFailureCode != which || a.ProviderAttempts != 2 || p.calls.Load() != 2 || len(a.Findings) != 1 || a.Findings[0].Rule != "data_exfiltration" {
				t.Fatal(a)
			}
			if len(a.ProviderAttemptDiagnostics) != 2 || *a.ProviderAttemptDiagnostics[0].CompletionTokens != 10 || *a.ProviderAttemptDiagnostics[1].CompletionTokens != 20 {
				t.Fatal("usage attempts not preserved", a)
			}
		})
	}
}
func TestSecondOversizeResponseFailsWithoutThirdCall(t *testing.T) {
	raw := repairJSON(t, strings.Repeat("x", 601), "Known risk.")
	p := &repairProvider{fn: func(context.Context, Request, int) ([]byte, error) { return raw, nil }}
	a := NewWorker(p, time.Second).Review(context.Background(), fixture())
	if a.Outcome != "invalid_response" || a.FailureCode != "summary_bounds" || a.FormatRepairAttempts != 1 || p.calls.Load() != 2 || len(a.Findings) != 0 {
		t.Fatal(a)
	}
}
func TestFormatRepairCannotDropChangeOrAddRisk(t *testing.T) {
	for _, change := range []string{"remove", "rule", "file", "confidence", "add"} {
		t.Run(change, func(t *testing.T) {
			first := repairJSON(t, strings.Repeat("x", 601), "Known risk.")
			p := &repairProvider{fn: func(_ context.Context, _ Request, n int) ([]byte, error) {
				if n == 1 {
					return first, nil
				}
				var s Suggestion
				json.Unmarshal(first, &s)
				s.Summary = "Concise."
				switch change {
				case "remove":
					s.Findings = []Finding{}
				case "rule":
					s.Findings[0].Rule = "other_risk"
				case "file":
					s.Findings[0].File = "other.py"
				case "confidence":
					s.Findings[0].Confidence = 0.1
				case "add":
					s.Findings = append(s.Findings, s.Findings[0])
				}
				return json.Marshal(s)
			}}
			a := NewWorker(p, time.Second).Review(context.Background(), fixture())
			if a.Outcome != "invalid_response" || p.calls.Load() != 2 || len(a.Findings) != 0 {
				t.Fatal(a)
			}
		})
	}
}
func TestProviderAndSemanticErrorsDoNotTriggerFormatRepair(t *testing.T) {
	for _, kind := range []string{"provider", "json", "unknown_rule", "unknown_file", "confidence", "bytes"} {
		t.Run(kind, func(t *testing.T) {
			p := &repairProvider{fn: func(context.Context, Request, int) ([]byte, error) {
				if kind == "provider" {
					return nil, errors.New("fixture unavailable")
				}
				if kind == "json" {
					return []byte("not-json"), nil
				}
				if kind == "bytes" {
					return []byte(strings.Repeat("x", MaxProviderBytes+1)), nil
				}
				var s Suggestion
				json.Unmarshal(repairJSON(t, strings.Repeat("x", 601), "Known risk."), &s)
				switch kind {
				case "unknown_rule":
					s.Findings[0].Rule = "invented"
				case "unknown_file":
					s.Findings[0].File = "other.py"
				case "confidence":
					s.Findings[0].Confidence = 9
				}
				return json.Marshal(s)
			}}
			a := NewWorker(p, time.Second).Review(context.Background(), fixture())
			if a.Outcome == "advice_ready" || p.calls.Load() != 1 || a.FormatRepairAttempts != 0 {
				t.Fatal(a)
			}
		})
	}
}
func TestFormatRepairSharesOriginalDeadlineAndOccupiedSlot(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	p := &repairProvider{fn: func(ctx context.Context, r Request, n int) ([]byte, error) {
		if n == 1 {
			time.Sleep(35 * time.Millisecond)
			return repairJSON(t, strings.Repeat("x", 601), "Known risk."), nil
		}
		close(entered)
		<-release
		return repairJSON(t, "Concise.", "Known risk."), nil
	}}
	w := NewWorker(p, 60*time.Millisecond)
	start := time.Now()
	a := w.Review(context.Background(), fixture())
	if a.Outcome != "provider_timeout" || time.Since(start) > 300*time.Millisecond || a.FormatRepairAttempts != 1 || a.FirstFormatFailureCode != "summary_bounds" || a.ProviderAttempts != 2 || len(a.ProviderAttemptDiagnostics) != 1 {
		t.Fatal(a)
	}
	<-entered
	if a := w.Review(context.Background(), fixture()); a.Outcome != "provider_busy" {
		t.Fatal("uncooperative repair lost slot", a)
	}
	close(release)
}
