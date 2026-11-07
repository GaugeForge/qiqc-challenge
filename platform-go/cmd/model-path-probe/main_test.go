package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestTerminalRequired(t *testing.T) {
	cases := []struct {
		name, endpoint, body string
		complete             bool
	}{
		{"response completed", "responses", "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"usage\":{\"input_tokens\":3,\"output_tokens\":7}}}\n\n", true},
		{"response incomplete", "responses", "data: {\"type\":\"response.incomplete\"}\n\n", false},
		{"response no terminal", "responses", "data: {\"type\":\"response.output_text.delta\",\"delta\":\"private text must not appear\"}\n\n", false},
		{"chat terminal", "chat/completions", "data: {\"choices\":[{\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n", true},
		{"chat only done", "chat/completions", "data: [DONE]\n\n", false},
		{"chat missing done", "chat/completions", "data: {\"choices\":[{\"finish_reason\":\"stop\"}]}\n\n", false},
		{"chat length", "chat/completions", "data: {\"choices\":[{\"finish_reason\":\"length\"}]}\n\ndata: [DONE]\n\n", false},
		{"late error", "responses", "data: {\"type\":\"response.completed\"}\n\ndata: {\"error\":{\"message\":\"private error\"}}\n\n", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := summarize([]byte(tc.body), tc.endpoint)
			if r.Complete != tc.complete {
				t.Fatalf("complete=%v expected %v", r.Complete, tc.complete)
			}
		})
	}
}

func syntheticArguments() string {
	books := []map[string]int{}
	for i, width := range []int{7, 3, 9, 4, 8, 6, 2, 5, 11, 10, 3, 7, 4, 6, 9, 2, 8, 5} {
		books = append(books, map[string]int{"id": i + 1, "width": width})
	}
	raw, _ := json.Marshal(map[string]any{"total_width": 109, "books": books})
	return string(raw)
}
func sse(value any) string { raw, _ := json.Marshal(value); return "data: " + string(raw) + "\n\n" }
func TestToolScenarioIsBoundedAndUsesCorrectEndpointShapes(t *testing.T) {
	s, err := selectScenario("tool-call")
	if err != nil {
		t.Fatal(err)
	}
	if len(s.endpoints) != 2 || s.pairs != 1 || s.maxTokens != 4096 || s.reasoning != nil || s.timeout != 180*time.Second {
		t.Fatal("tool scenario bounds changed")
	}
	for _, endpoint := range s.endpoints {
		var body map[string]any
		json.Unmarshal(scenarioPayload(endpoint, s), &body)
		tool := body["tools"].([]any)[0].(map[string]any)
		if tool["type"] != "function" {
			t.Fatal("not a function")
		}
		if endpoint == "chat/completions" {
			tool = tool["function"].(map[string]any)
		}
		if tool["name"] != syntheticToolName || tool["strict"] != true {
			t.Fatal("tool contract changed")
		}
	}
}
func TestResponsesToolArgumentsRequireValidFixture(t *testing.T) {
	for _, valid := range []bool{true, false} {
		args := syntheticArguments()
		if !valid {
			args = strings.Replace(args, "109", "999", 1)
		}
		body := sse(map[string]any{"type": "response.completed", "response": map[string]any{"status": "completed", "output": []any{map[string]any{"type": "function_call", "name": syntheticToolName, "arguments": args}}}})
		count, size, ok := summarizeToolCalls([]byte(body), "responses")
		if count != 1 || size != len(args) || ok != valid {
			t.Fatal("tool validation failed")
		}
	}
}

func TestNamespaceScenarioOnlyTwoBoundedCallsAndNoFlattening(t *testing.T) {
	s, err := selectScenario("namespace-tool-call")
	if err != nil {
		t.Fatal(err)
	}
	if len(s.endpoints) != 1 || s.endpoints[0] != "responses" || s.pairs != 1 || s.maxTokens != 2048 || s.reasoning != nil {
		t.Fatal("namespace budget changed")
	}
	var body map[string]any
	json.Unmarshal(scenarioPayload("responses", s), &body)
	tools := body["tools"].([]any)
	namespace := tools[0].(map[string]any)
	if len(tools) != 1 || namespace["type"] != "namespace" || namespace["name"] != "functions" || body["tool_choice"] != "required" {
		t.Fatal("namespace flattened or selection changed")
	}
	function := namespace["tools"].([]any)[0].(map[string]any)
	if function["type"] != "function" || function["name"] != syntheticToolName {
		t.Fatal("wrong namespace child")
	}
}
func TestNamespaceOutputRequiresExactSeparateNamespaceAndName(t *testing.T) {
	for _, tc := range []struct {
		namespace, name string
		valid           bool
	}{{"functions", syntheticToolName, true}, {"", syntheticToolName, false}, {"wrong", syntheticToolName, false}, {"", "functions." + syntheticToolName, false}, {"functions", "other", false}} {
		body := sse(map[string]any{"type": "response.completed", "response": map[string]any{"status": "completed", "output": []any{map[string]any{"type": "function_call", "namespace": tc.namespace, "name": tc.name, "arguments": syntheticArguments()}}}})
		count, _, valid := summarizeToolCallsExpected([]byte(body), "responses", "functions")
		if count != 1 || valid != tc.valid {
			t.Fatal("namespace output validation failed")
		}
	}
}
func TestChatFragmentedToolArgumentsAndTerminal(t *testing.T) {
	args := syntheticArguments()
	body := ""
	for i, chunk := range []string{args[:19], args[19:]} {
		name := ""
		if i == 0 {
			name = syntheticToolName
		}
		body += sse(map[string]any{"choices": []any{map[string]any{"delta": map[string]any{"tool_calls": []any{map[string]any{"index": 0, "function": map[string]any{"name": name, "arguments": chunk}}}}}}})
	}
	if _, _, ok := summarizeToolCalls([]byte(body), "chat/completions"); ok {
		t.Fatal("missing terminal accepted")
	}
	body += sse(map[string]any{"choices": []any{map[string]any{"finish_reason": "tool_calls"}}}) + "data: [DONE]\n\n"
	count, size, ok := summarizeToolCalls([]byte(body), "chat/completions")
	if count != 1 || size != len(args) || !ok {
		t.Fatal(fmt.Sprintf("fragmented tool failed: count=%d size=%d valid=%v", count, size, ok))
	}
	if !summarize([]byte(body), "chat/completions").Complete {
		t.Fatal("valid tool terminal rejected")
	}
}

func TestScientificStreamScenario(t *testing.T) {
	s, err := selectScenario("scientific-stream")
	if err != nil {
		t.Fatal(err)
	}
	if len(s.endpoints) != 1 || s.endpoints[0] != "responses" || s.maxTokens != 4096 || s.timeout != 180*time.Second {
		t.Fatal("scientific scenario limits changed")
	}
	var body map[string]any
	if err := json.Unmarshal(scenarioPayload("responses", s), &body); err != nil {
		t.Fatal(err)
	}
	v, present := body["reasoning"]
	if !present || v != nil {
		t.Fatal("scientific reasoning must be explicit null")
	}
	if body["max_output_tokens"] != float64(4096) || body["input"] != scientificPrompt {
		t.Fatal("fixed public input or limit changed")
	}
	if _, err := selectScenario("arbitrary"); err == nil {
		t.Fatal("arbitrary scenario accepted")
	}
}

func TestConcurrentScenarioBounded(t *testing.T) {
	s, err := selectScenario("concurrent-stream")
	if err != nil {
		t.Fatal(err)
	}
	if s.pairs != 10 || len(s.endpoints) != 1 || s.maxTokens != 2048 || s.timeout != 180*time.Second || s.reasoning != nil {
		t.Fatal("concurrent bounds changed")
	}
	if len(s.input) < 32*1024 || len(s.input) > 33*1024 || s.input != concurrentPrompt() {
		t.Fatal("public input padding changed")
	}
	if s.pairs*2*s.maxTokens != 40960 {
		t.Fatal("output ceiling changed")
	}
}
func TestOutputLimitIsBoundary(t *testing.T) {
	r := summarize([]byte("data: {\"type\":\"response.incomplete\",\"response\":{\"status\":\"incomplete\",\"incomplete_details\":{\"reason\":\"max_output_tokens\"}}}\n\n"), "responses")
	if r.Complete || r.TestBoundary != "max_output_tokens" {
		t.Fatal("output limit classified incorrectly")
	}
}

func TestSafeHeaderAllowlist(t *testing.T) {
	h := http.Header{}
	h.Set("Cf-Ray", "public-edge")
	h.Set("X-Provider-Name", "public-provider")
	h.Set("Authorization", "secret")
	h.Set("Set-Cookie", "secret")
	h.Set("X-Request-Id", "unsafe\nheader")
	got := safeHeaders(h)
	if len(got) != 2 || got["cf-ray"] != "public-edge" || got["x-provider-name"] != "public-provider" {
		t.Fatal("header allowlist failed")
	}
}
func TestFixedPayload(t *testing.T) {
	for _, endpoint := range []string{"responses", "chat/completions"} {
		var body map[string]any
		if err := json.Unmarshal(payload(endpoint), &body); err != nil {
			t.Fatal(err)
		}
		if body["model"] != model || body["stream"] != true {
			t.Fatal("payload contract changed")
		}
		reasoning := body["reasoning"].(map[string]any)
		if reasoning["effort"] != "none" {
			t.Fatal("reasoning budget changed")
		}
	}
}
