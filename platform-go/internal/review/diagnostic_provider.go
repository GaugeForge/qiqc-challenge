package review

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/gaugeforge/qiqc-platform-go/internal/diagnosticmodel"
	"github.com/gaugeforge/qiqc-platform-go/internal/providerprofile"
	"net/url"
	"strings"
)

// DiagnosticProvider reuses the bounded completion parser, but sends only the
// explicit free diagnostic request contract. It cannot fall back to Hy4.
// The frozen v3 provider and quality corpus remain unchanged.
type DiagnosticProvider struct {
	inner   *HTTPSProvider
	version string
}

func (p *DiagnosticProvider) Version() string {
	if p.version != "" {
		return p.version
	}
	return "openrouter-minimax-free-infra-v1"
}
func (p *DiagnosticProvider) Review(ctx context.Context, req Request) ([]byte, error) {
	data, _, err := p.ReviewWithDiagnostics(ctx, req)
	return data, err
}
func (p *DiagnosticProvider) ReviewWithDiagnostics(ctx context.Context, req Request) ([]byte, *CompletionDiagnostics, error) {
	data, d, err := p.inner.ReviewWithDiagnostics(ctx, req)
	if d != nil {
		d.ReasoningEffort = "provider-default"
	}
	return data, d, err
}
func NewDiagnosticProvider(key, model string, transport http.RoundTripper, reserve func(context.Context) error) (*DiagnosticProvider, error) {
	if model != diagnosticmodel.Model || len(key) < 12 || len(key) > 512 || reserve == nil {
		return nil, errors.New("invalid diagnostic review configuration")
	}
	if transport == nil {
		transport = &http.Transport{Proxy: nil, TLSHandshakeTimeout: 10 * time.Second, ResponseHeaderTimeout: 120 * time.Second, DisableKeepAlives: true}
	}
	rt := diagnosticReviewTransport{next: transport, reserve: reserve}
	return &DiagnosticProvider{inner: &HTTPSProvider{key: key, model: model, endpoint: "https://openrouter.ai/api/v1/chat/completions", client: &http.Client{Transport: rt, Timeout: 125 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("diagnostic redirects forbidden") }}}}, nil
}

type diagnosticReviewTransport struct {
	model    string
	endpoint string
	next     http.RoundTripper
	reserve  func(context.Context) error
}

func (t diagnosticReviewTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if r.URL.String() != "https://openrouter.ai/api/v1/chat/completions" || r.Method != "POST" {
		return nil, errors.New("diagnostic endpoint refused")
	}
	b, err := io.ReadAll(io.LimitReader(r.Body, MaxInputBytes+16385))
	if err != nil || len(b) > MaxInputBytes+16384 {
		return nil, errors.New("diagnostic request refused")
	}
	var original map[string]json.RawMessage
	if json.Unmarshal(b, &original) != nil {
		return nil, errors.New("diagnostic request invalid")
	}
	var model string
	expectedModel := t.model
	if expectedModel == "" {
		expectedModel = diagnosticmodel.Model
	}
	if json.Unmarshal(original["model"], &model) != nil || model != expectedModel {
		return nil, errors.New("diagnostic model refused")
	}
	// Avoid carrying Hy4-specific reasoning/json-mode parameters into another
	// model. Normal advisory JSON validation still applies to the returned text.
	body, err := json.Marshal(map[string]any{"model": model, "stream": false, "max_tokens": CompletionTokenBudget, "messages": original["messages"]})
	if err != nil {
		return nil, err
	}
	if err = t.reserve(r.Context()); err != nil {
		return nil, err
	}
	clone := r.Clone(r.Context())
	if t.endpoint != "" {
		u, e := url.Parse(t.endpoint)
		if e != nil {
			return nil, errors.New("profile endpoint invalid")
		}
		clone.URL = u
		clone.Host = ""
	}
	clone.Body = io.NopCloser(bytes.NewReader(body))
	clone.ContentLength = int64(len(body))
	clone.GetBody = nil
	if err := r.Context().Err(); err != nil {
		return nil, err
	}
	return t.next.RoundTrip(clone)
}

// NewProfileProvider supports only the profile's explicit chat capability.
// Advisory requests are generated here; no participant parameters are rewritten.
func NewProfileProvider(key string, p providerprofile.Profile, id providerprofile.Identity, transport http.RoundTripper, reserve func(context.Context) error) (*DiagnosticProvider, error) {
	if p.Validate() != nil || id.ID != p.ID || id.Model != p.Model || len(id.SHA256) != 64 || reserve == nil || len(key) < 12 || len(key) > 512 {
		return nil, errors.New("invalid review profile")
	}
	supported := false
	for _, e := range p.Endpoints {
		if e == "chat/completions" {
			supported = true
		}
	}
	if !supported {
		return nil, errors.New("review requires chat capability")
	}
	if transport == nil {
		transport = &http.Transport{Proxy: nil, DialContext: providerprofile.DialPublic, TLSHandshakeTimeout: 10 * time.Second, ResponseHeaderTimeout: 120 * time.Second, DisableKeepAlives: true}
	}
	endpoint := strings.TrimRight(p.BaseURL, "/")
	for _, suffix := range []string{"/responses", "/chat/completions"} {
		endpoint = strings.TrimSuffix(endpoint, suffix)
	}
	endpoint += "/chat/completions"
	rt := diagnosticReviewTransport{next: transport, reserve: reserve, model: p.Model, endpoint: endpoint}
	return &DiagnosticProvider{version: "profile-" + p.ID + "-" + id.SHA256[:12], inner: &HTTPSProvider{key: key, model: p.Model, endpoint: "https://openrouter.ai/api/v1/chat/completions", client: &http.Client{Transport: rt, Timeout: 125 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("profile redirects forbidden") }}}}, nil
}
