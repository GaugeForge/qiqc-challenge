package review

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/gaugeforge/qiqc-platform-go/internal/diagnosticmodel"
	"github.com/gaugeforge/qiqc-platform-go/internal/providerprofile"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestFreeDiagnosticReviewContract(t *testing.T) {
	calls, reservations := 0, 0
	rt := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		var b map[string]any
		if json.NewDecoder(r.Body).Decode(&b) != nil {
			t.Fatal("body")
		}
		if b["model"] != diagnosticmodel.Model || len(b) != 4 || b["stream"] != false || b["max_tokens"] != float64(8192) {
			t.Fatal("nonfree or unverified parameters")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}`)), Header: make(http.Header)}, nil
	})
	p, e := NewDiagnosticProvider("synthetic-only-key", diagnosticmodel.Model, rt, func(context.Context) error { reservations++; return nil })
	if e != nil {
		t.Fatal(e)
	}
	if p.Version() == "openrouter-hy4-chat-v3" {
		t.Fatal("false provider identity")
	}
	if _, e = p.Review(context.Background(), Request{InputJSON: []byte(`{}`)}); e != nil {
		t.Fatal(e)
	}
	if calls != 1 || reservations != 1 {
		t.Fatal("unexpected calls")
	}
	denied, _ := NewDiagnosticProvider("synthetic-only-key", diagnosticmodel.Model, rt, func(context.Context) error { return errors.New("limit") })
	if _, e = denied.Review(context.Background(), Request{InputJSON: []byte(`{}`)}); e == nil || calls != 1 {
		t.Fatal("limit sent provider call")
	}
	if _, e = NewDiagnosticProvider("synthetic-only-key", "tencent/hy4-preview", rt, func(context.Context) error { return nil }); e == nil {
		t.Fatal("paid model accepted")
	}
}

func TestProfileReviewEndpointAndReportedReasoning(t *testing.T) {
	raw := []byte(`{"schema":"qiqc-provider-profile/v1","id":"other","base_url":"https://api.example.com/v1","model":"different-model","endpoints":["chat/completions"],"key_file":"/opt/private/key","quota_group":"account","requests_per_minute":500,"requests_per_day":5000,"allow_paid_fallback":false,"parameter_policy":"passthrough/v1","authentication":"bearer","billing":"paid"}`)
	p, id, e := providerprofile.Parse(raw)
	if e != nil {
		t.Fatal(e)
	}
	calls := 0
	rt := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.URL.String() != "https://api.example.com/v1/chat/completions" {
			t.Fatal("wrong trusted endpoint")
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["model"] != "different-model" || body["reasoning"] != nil {
			t.Fatal("profile body changed")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}`)), Header: make(http.Header)}, nil
	})
	provider, e := NewProfileProvider("synthetic-only-key", p, id, rt, func(context.Context) error { return nil })
	if e != nil {
		t.Fatal(e)
	}
	_, d, e := provider.ReviewWithDiagnostics(context.Background(), Request{InputJSON: []byte(`{}`)})
	if e != nil || d.ReasoningEffort != "provider-default" || calls != 1 {
		t.Fatal("inaccurate profile reasoning diagnostics", e)
	}
	p.Endpoints = []string{"responses"}
	if _, e = NewProfileProvider("synthetic-only-key", p, id, rt, func(context.Context) error { return nil }); e == nil {
		t.Fatal("unsupported reviewer protocol accepted")
	}
}
