package review

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestBoundedReviewRequestAndTruncationRecovery(t *testing.T) {
	calls := 0
	p, err := NewHTTPSProvider("synthetic-only-key", "tencent/hy4-preview", roundTripFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		var request map[string]any
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		reasoning, ok := request["reasoning"].(map[string]any)
		if request["max_tokens"] != float64(8192) || !ok || reasoning["effort"] != "none" || reasoning["exclude"] != true {
			t.Fatal("unbounded or unsupported reasoning request")
		}
		finish := "length"
		if calls == 2 {
			finish = "stop"
		}
		// Even syntactically complete JSON is never accepted on a length finish.
		content, _ := json.Marshal(string(validReply()))
		body := fmt.Sprintf(`{"choices":[{"finish_reason":%q,"message":{"content":%s,"reasoning":"PRIVATE REASONING MUST NOT PERSIST"}}],"usage":{"prompt_tokens":120,"completion_tokens":8192,"completion_tokens_details":{"reasoning_tokens":8000}}}`, finish, content)
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
	}))
	if err != nil {
		t.Fatal(err)
	}
	w := NewWorker(p, time.Second)
	first := w.Review(context.Background(), fixture())
	if first.Outcome != "provider_error" || first.FailureCode != "completion_token_limit" || calls != 1 {
		t.Fatal(first, calls)
	}
	second := w.Review(context.Background(), fixture())
	if second.Outcome != "advice_ready" || second.Status != "manual_required" || calls != 2 {
		t.Fatal(second, calls)
	}
	for _, advice := range []Advice{first, second} {
		if advice.Diagnostics == nil || advice.Diagnostics.CompletionTokens == nil || *advice.Diagnostics.CompletionTokens != 8192 || advice.Diagnostics.ReasoningTokens == nil || *advice.Diagnostics.ReasoningTokens != 8000 || advice.Diagnostics.MaxTokens != 8192 {
			t.Fatal(advice.Diagnostics)
		}
		encoded, _ := json.Marshal(advice)
		if strings.Contains(string(encoded), "PRIVATE REASONING") || strings.Contains(string(encoded), "synthetic-only-key") {
			t.Fatal("private provider data persisted")
		}
	}
}

func TestCompactFindingsNeverConcealUncoveredScannerRisks(t *testing.T) {
	in := fixture()
	in.Risks = append(in.Risks, Risk{Rule: "credential_access", File: "agent.py"})
	response := Suggestion{SchemaVersion: "qiqc-advisory/v1", Findings: []Finding{{Rule: "webhook", File: "agent.py", Confidence: .7, Explanation: "Sends task content outside the allowed endpoint."}}, Summary: "Manual follow-up required."}
	data, _ := json.Marshal(response)
	advice := NewWorker(providerFunc(func(context.Context, Request) ([]byte, error) { return data, nil }), time.Second).Review(context.Background(), in)
	if advice.Outcome != "advice_ready" || advice.UncoveredScannerRisks != 1 || !advice.ManualFollowupRequired || advice.ReviewScope != "bounded_scanner_snippets_only" || len(in.Risks) != 2 {
		t.Fatal(advice)
	}
	response.Findings[0].Explanation = strings.Repeat("x", MaxExplanationBytes+1)
	data, _ = json.Marshal(response)
	advice = NewWorker(providerFunc(func(context.Context, Request) ([]byte, error) { return data, nil }), time.Second).Review(context.Background(), in)
	if advice.Outcome != "invalid_response" || advice.FailureCode != "explanation_bounds" {
		t.Fatal(advice)
	}
}

func TestNonStopCompletionCannotBecomeAdvice(t *testing.T) {
	for _, finish := range []string{"", "tool_calls", "content_filter", "error", "untrusted-secret-string"} {
		t.Run(finish, func(t *testing.T) {
			p, _ := NewHTTPSProvider("synthetic-only-key", "tencent/hy4-preview", roundTripFunc(func(*http.Request) (*http.Response, error) {
				content, _ := json.Marshal(string(validReply()))
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(fmt.Sprintf(`{"choices":[{"finish_reason":%q,"message":{"content":%s}}]}`, finish, content)))}, nil
			}))
			a := NewWorker(p, time.Second).Review(context.Background(), fixture())
			if a.Outcome != "provider_error" || a.FailureCode != "incomplete_completion" {
				t.Fatal(a)
			}
			encoded, _ := json.Marshal(a)
			if strings.Contains(string(encoded), "untrusted-secret-string") {
				t.Fatal("unbounded upstream string retained")
			}
		})
	}
}

func TestMissingAndInvalidUsageNeverPretendZeroTokens(t *testing.T) {
	for _, usage := range []string{`{}`, `{"prompt_tokens":-1,"completion_tokens":9999999}`, `{"completion_tokens":"private-text"}`} {
		p, _ := NewHTTPSProvider("synthetic-only-key", "tencent/hy4-preview", roundTripFunc(func(*http.Request) (*http.Response, error) {
			content, _ := json.Marshal(string(validReply()))
			body := fmt.Sprintf(`{"choices":[{"finish_reason":"stop","message":{"content":%s}}],"usage":%s}`, content, usage)
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
		}))
		a := NewWorker(p, time.Second).Review(context.Background(), fixture())
		if a.Diagnostics == nil || a.Diagnostics.PromptTokens != nil || a.Diagnostics.CompletionTokens != nil || a.Diagnostics.ReasoningTokens != nil {
			t.Fatal("missing or invalid usage represented as measured tokens", a.Diagnostics)
		}
		if strings.Contains(usage, "private-text") && a.Outcome != "provider_error" {
			t.Fatal("invalid usage schema accepted")
		}
		b, _ := json.Marshal(a)
		if strings.Contains(string(b), "private-text") {
			t.Fatal("untrusted usage text persisted")
		}
	}
}

func TestSyntheticProviderAlsoRespectsCompactContract(t *testing.T) {
	in := fixture()
	in.Risks = nil
	for i := 0; i < 100; i++ {
		in.Risks = append(in.Risks, Risk{Rule: "webhook", File: fmt.Sprintf("file%d.py", i)})
	}
	a := NewWorker(FakeProvider{}, time.Second).Review(context.Background(), in)
	if a.Outcome != "advice_ready" || len(a.Findings) != MaxFindings || a.UncoveredScannerRisks != 100-MaxFindings || !a.ManualFollowupRequired {
		t.Fatal(a)
	}
}
