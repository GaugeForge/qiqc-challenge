// Package modelproxy implements a run-authorized, bounded OpenAI-compatible
// gateway. Only trusted host configuration contains the provider master key.
package modelproxy

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/gaugeforge/qiqc-platform-go/internal/providerprofile"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type Grant struct {
	RequestCorrelation string `json:"request_correlation,omitempty"`
	RequestID          string `json:"request_id,omitempty"`
	RunID              string `json:"run_id"`
	Attempt            int    `json:"attempt"`
	Model              string `json:"model"`
	TokenHash          string `json:"token_hash"`
}
type Usage struct {
	InputTokens  *int64 `json:"input_tokens"`
	OutputTokens *int64 `json:"output_tokens"`
	CachedTokens *int64 `json:"cached_tokens"`
}
type UsageRecord struct {
	Grant      Grant  `json:"grant"`
	Status     int    `json:"status"`
	Attempt    int    `json:"upstream_attempt"`
	Usage      Usage  `json:"usage"`
	ErrorClass string `json:"error_class,omitempty"`
}
type HTTPError struct {
	Status  int
	Message string
}

func (e *HTTPError) Error() string { return fmt.Sprintf("model authorization rejected (%d)", e.Status) }

type Config struct {
	AllowedEndpoints  []string
	RequiredModel     string
	PublicUpstream    bool
	UpstreamIPFamily  string   // auto (default), ipv4 or ipv6; never changes a custom Client transport
	AllowedToolTypes  []string // defaults to local function/custom tools; no provider web/MCP execution
	BaseURL           string
	MasterKey         string
	Client            *http.Client
	AllowInsecureHTTP bool
	MaxConcurrent     int
	MaxAttempts       int
	MaxRequestBytes   int64
	MaxResponseBytes  int64
	MaxEventBytes     int
	MaxRetryAfter     time.Duration
	RequestTimeout    time.Duration
	Authorize         func(context.Context, string, string) (Grant, error)
	RecordUsage       func(context.Context, UsageRecord) error
	Reserve           func(context.Context, Grant) error // atomic provider-attempt admission, including in-flight work
}
type Proxy struct {
	cfg    Config
	client *http.Client
	slots  chan struct{}
}

// UpstreamURL accepts a base or a full supported endpoint and normalizes it
// before switching protocols. Query credentials, fragments and userinfo fail.
func UpstreamURL(base, endpoint string) (string, error) {
	if endpoint != "responses" && endpoint != "chat/completions" {
		return "", errors.New("unsupported model endpoint")
	}
	u, err := url.Parse(base)
	if err != nil || u.Host == "" || (u.Scheme != "https" && u.Scheme != "http") || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" {
		return "", errors.New("invalid provider base URL")
	}
	path := strings.TrimRight(u.Path, "/")
	for _, suffix := range []string{"/chat/completions", "/responses"} {
		if strings.HasSuffix(path, suffix) {
			path = strings.TrimSuffix(path, suffix)
			break
		}
	}
	u.Path = path + "/" + endpoint
	u.RawPath = ""
	return u.String(), nil
}
func New(cfg Config) (*Proxy, error) {
	network, err := UpstreamNetwork(cfg.UpstreamIPFamily)
	if err != nil {
		return nil, err
	}
	if cfg.UpstreamIPFamily == "" {
		cfg.UpstreamIPFamily = "auto"
	}
	normalized, err := UpstreamURL(cfg.BaseURL, "responses")
	if err != nil {
		return nil, err
	}
	if strings.HasPrefix(normalized, "http:") && !cfg.AllowInsecureHTTP {
		return nil, errors.New("plaintext upstream requires explicit local-test opt-in")
	}
	if cfg.MasterKey == "" || strings.ContainsAny(cfg.MasterKey, "\r\n") || cfg.Authorize == nil || cfg.RecordUsage == nil {
		return nil, errors.New("missing trusted provider or authorization configuration")
	}
	if len(cfg.AllowedToolTypes) == 0 {
		cfg.AllowedToolTypes = []string{"function", "custom"}
	}
	if cfg.MaxConcurrent == 0 {
		cfg.MaxConcurrent = 2
	}
	if cfg.MaxAttempts == 0 {
		cfg.MaxAttempts = 3
	}
	if cfg.MaxRequestBytes == 0 {
		cfg.MaxRequestBytes = 4 << 20
	}
	if cfg.MaxResponseBytes == 0 {
		cfg.MaxResponseBytes = 16 << 20
	}
	if cfg.MaxEventBytes == 0 {
		cfg.MaxEventBytes = 4 << 20
	}
	if cfg.MaxRetryAfter == 0 {
		cfg.MaxRetryAfter = 4 * time.Second
	}
	if cfg.RequestTimeout == 0 {
		cfg.RequestTimeout = 120 * time.Second
	}
	if cfg.MaxConcurrent < 1 || cfg.MaxAttempts < 1 || cfg.MaxAttempts > 10 || cfg.MaxRequestBytes < 1 || cfg.MaxResponseBytes < 1 || cfg.MaxEventBytes < 1 || cfg.MaxRetryAfter < 0 || cfg.RequestTimeout < 0 {
		return nil, errors.New("invalid model proxy limits")
	}
	client := &http.Client{}
	if cfg.Client != nil {
		*client = *cfg.Client
	} else {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.Proxy = nil
		if cfg.PublicUpstream {
			transport.DialContext = func(ctx context.Context, _ string, address string) (net.Conn, error) {
				return providerprofile.DialPublic(ctx, network, address)
			}
		} else if network != "tcp" {
			dial := transport.DialContext
			transport.DialContext = func(ctx context.Context, _ string, address string) (net.Conn, error) {
				return dial(ctx, network, address)
			}
		}
		transport.ResponseHeaderTimeout = 30 * time.Second
		// Concurrent HTTP/2 scientific streams experienced grouped resets.
		// Isolate each upstream request on its own HTTP/1.1 connection to
		// bound a single-connection failure without reducing admission concurrency.
		transport.ForceAttemptHTTP2 = false
		transport.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
		transport.TLSClientConfig = &tls.Config{NextProtos: []string{"http/1.1"}}
		transport.DisableKeepAlives = true
		transport.MaxConnsPerHost = cfg.MaxConcurrent
		client.Transport = transport
	}
	// A caller-provided client's redirect policy may not forward the master key.
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	client.Jar = nil
	return &Proxy{cfg: cfg, client: client, slots: make(chan struct{}, cfg.MaxConcurrent)}, nil
}

func fail(w http.ResponseWriter, status int, category string) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"type": category, "message": "Model request could not be completed; retain the platform request ID"}})
}
func authStatus(err error) int {
	var issue *HTTPError
	if errors.As(err, &issue) && (issue.Status == 401 || issue.Status == 403 || issue.Status == 429) {
		return issue.Status
	}
	return 503
}
func validGrant(g Grant, model string) bool {
	return g.RunID != "" && g.Attempt > 0 && g.Model == model && g.TokenHash != ""
}
func foreignReference(value any) bool {
	switch v := value.(type) {
	case map[string]any:
		if v["type"] == "item_reference" {
			return true
		}
		for _, child := range v {
			if foreignReference(child) {
				return true
			}
		}
	case []any:
		for _, child := range v {
			if foreignReference(child) {
				return true
			}
		}
	}
	return false
}
func (p *Proxy) record(ctx context.Context, r UsageRecord) error {
	// Client cancellation must not erase billed or unknown usage. The callback
	// gets a short independent deadline and no provider secret or prompt content.
	// SQLite's shared store allows a 30-second writer busy wait. A shorter
	// accounting deadline discarded outcomes during an otherwise recoverable lock.
	independent, cancel := context.WithTimeout(context.WithoutCancel(ctx), 35*time.Second)
	defer cancel()
	err := p.cfg.RecordUsage(independent, r)
	if err != nil {
		slog.Error("model_usage_record_failed", "request_id", r.Grant.RequestID,
			"request_correlation", r.Grant.RequestCorrelation, "run_id", r.Grant.RunID,
			"run_attempt", r.Grant.Attempt, "upstream_attempt", r.Attempt,
			"status", r.Status, "error_class", r.ErrorClass,
			"input_tokens", diagnosticTokenCount(r.Usage.InputTokens), "output_tokens", diagnosticTokenCount(r.Usage.OutputTokens),
			"cached_tokens", diagnosticTokenCount(r.Usage.CachedTokens), "failure_class", transportClass(err))
	}
	return err
}

// A cancellation while waiting has no new provider attempt. Preserve the
// terminal request outcome without inventing billable/unknown provider usage.
func (p *Proxy) stopBeforeUpstream(w http.ResponseWriter, r *http.Request, ctx context.Context, grant Grant) {
	status, category := 504, "admission_deadline_before_upstream"
	if r.Context().Err() != nil {
		status, category = 499, "client_cancelled_before_upstream"
	}
	zero := int64(0)
	if p.record(ctx, UsageRecord{Grant: grant, Status: status, Attempt: 0,
		Usage: Usage{InputTokens: &zero, OutputTokens: &zero, CachedTokens: &zero}, ErrorClass: category}) != nil {
		fail(w, 503, "usage_record_error")
		return
	}
	fail(w, status, category)
}

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" && r.Method == http.MethodGet {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"status":"ok"}`)
		return
	}
	endpoint := strings.TrimPrefix(r.URL.Path, "/v1/")
	if r.URL.Path != "/v1/"+endpoint || (endpoint != "responses" && endpoint != "chat/completions") {
		fail(w, 404, "endpoint_not_allowed")
		return
	}
	if len(p.cfg.AllowedEndpoints) > 0 {
		allowed := false
		for _, v := range p.cfg.AllowedEndpoints {
			if v == endpoint {
				allowed = true
			}
		}
		if !allowed {
			fail(w, 400, "profile_endpoint_unsupported")
			return
		}
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		fail(w, 405, "method_not_allowed")
		return
	}
	if r.URL.RawQuery != "" {
		fail(w, 400, "query_not_allowed")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, p.cfg.MaxRequestBytes+1))
	if err != nil {
		fail(w, 400, "invalid_body")
		return
	}
	if int64(len(raw)) > p.cfg.MaxRequestBytes {
		fail(w, 413, "request_too_large")
		return
	}
	var body map[string]any
	if json.Unmarshal(raw, &body) != nil || body == nil {
		fail(w, 400, "invalid_json")
		return
	}
	model, ok := body["model"].(string)
	if !ok || strings.TrimSpace(model) == "" {
		fail(w, 400, "model_required")
		return
	}
	if p.cfg.RequiredModel != "" && model != p.cfg.RequiredModel {
		fail(w, 403, "profile_model_not_allowed")
		return
	}
	stream := false
	if value, exists := body["stream"]; exists {
		stream, ok = value.(bool)
		if !ok {
			fail(w, 400, "invalid_stream_flag")
			return
		}
	}
	if value, exists := body["previous_response_id"]; exists && value != nil {
		fail(w, 400, "cross_request_reference_disabled")
		return
	}
	if value, exists := body["conversation"]; exists && value != nil {
		fail(w, 400, "cross_request_reference_disabled")
		return
	}
	// Provider-side plugins and remote attachments can create an outbound path
	// even when the Agent sandbox cannot access the internet. A run capability
	// authorizes the configured model, not alternate models or provider fetches.
	for _, key := range []string{"plugins", "web_search_options", "provider", "models", "route", "transforms", "fallback_models", "background"} {
		if value, exists := body[key]; exists && value != nil {
			if key == "background" && value == false {
				continue
			}
			fail(w, 400, "provider_extension_not_allowed")
			return
		}
	}
	if remoteInput(body["input"]) || remoteInput(body["messages"]) {
		fail(w, 400, "remote_attachment_not_allowed")
		return
	}
	if tools, exists := body["tools"]; exists {
		count := 0
		if !allowedTools(tools, p.cfg.AllowedToolTypes, 0, &count) {
			fail(w, 400, "provider_tool_not_allowed")
			return
		}
	}
	if foreignReference(body) {
		fail(w, 400, "cross_request_reference_disabled")
		return
	}
	authorization := r.Header.Get("Authorization")
	if !strings.HasPrefix(authorization, "Bearer ") || strings.TrimSpace(strings.TrimPrefix(authorization, "Bearer ")) == "" {
		fail(w, 401, "run_token_required")
		return
	}
	token := strings.TrimPrefix(authorization, "Bearer ")
	ctx, cancel := context.WithTimeout(r.Context(), p.cfg.RequestTimeout)
	defer cancel()
	grant, err := p.cfg.Authorize(ctx, token, model)
	if err != nil {
		fail(w, authStatus(err), "run_not_authorized")
		return
	}
	if !validGrant(grant, model) {
		fail(w, 403, "invalid_run_grant")
		return
	}
	var requestID [16]byte
	if _, err := rand.Read(requestID[:]); err != nil {
		fail(w, 503, "request_identity_error")
		return
	}
	grant.RequestID = hex.EncodeToString(requestID[:])
	correlation := r.Header.Get("X-QIQC-Controller-Correlation-ID")
	if correlation != "" {
		decoded, decodeErr := hex.DecodeString(correlation)
		if decodeErr != nil || len(decoded) != 16 || hex.EncodeToString(decoded) != correlation {
			fail(w, 400, "invalid_controller_correlation")
			return
		}
		grant.RequestCorrelation = correlation
	}
	w.Header().Set("X-QIQC-Request-ID", grant.RequestID)
	select {
	case p.slots <- struct{}{}:
		defer func() { <-p.slots }()
	case <-ctx.Done():
		p.stopBeforeUpstream(w, r, ctx, grant)
		return
	}
	if endpoint == "responses" {
		body["store"] = false
	}
	// HY's Responses compatibility layer accepts the OpenAI message roles
	// user/assistant/system, while Codex may emit the standard Responses
	// `developer` role. Preserve the instruction semantics at the provider
	// boundary by translating only that role to its equivalent system role.
	normalizeProviderRoles(body)
	normalizeProviderTools(body)
	if stream && endpoint == "chat/completions" {
		options, ok := body["stream_options"].(map[string]any)
		if !ok {
			options = map[string]any{}
		}
		options["include_usage"] = true
		body["stream_options"] = options
	}
	// Canonical serialization removes duplicate-key parser differences upstream.
	raw, err = json.Marshal(body)
	if err != nil {
		fail(w, 400, "invalid_json")
		return
	}
	upstream, _ := UpstreamURL(p.cfg.BaseURL, endpoint)
	for attempt := 1; attempt <= p.cfg.MaxAttempts; attempt++ {
		if ctx.Err() != nil {
			p.stopBeforeUpstream(w, r, ctx, grant)
			return
		}
		current, err := p.cfg.Authorize(ctx, token, model)
		if err != nil {
			if ctx.Err() != nil {
				p.stopBeforeUpstream(w, r, ctx, grant)
				return
			}
			fail(w, authStatus(err), "run_not_authorized")
			return
		}
		current.RequestID = grant.RequestID
		current.RequestCorrelation = grant.RequestCorrelation
		if current != grant || !validGrant(current, model) {
			fail(w, 403, "run_grant_changed")
			return
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, upstream, bytes.NewReader(raw))
		if err != nil {
			fail(w, 503, "provider_configuration_error")
			return
		}
		req.Header.Set("Authorization", "Bearer "+p.cfg.MasterKey)
		req.Header.Set("Content-Type", "application/json")
		if stream {
			req.Header.Set("Accept", "text/event-stream")
		} else {
			req.Header.Set("Accept", "application/json")
		}
		if p.cfg.Reserve != nil {
			admissionStarted := time.Now()
			reserveErr := p.cfg.Reserve(ctx, grant)
			slog.Info("model_admission_wait_finished", "request_id", grant.RequestID, "request_correlation", grant.RequestCorrelation, "run_id", grant.RunID, "run_attempt", grant.Attempt, "wait_ms", time.Since(admissionStarted).Milliseconds(), "admitted", reserveErr == nil, "context_cancelled", ctx.Err() != nil)
			if err := reserveErr; err != nil {
				if r.Context().Err() != nil || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
					status, category := 504, "admission_deadline_before_upstream"
					if r.Context().Err() != nil || errors.Is(err, context.Canceled) {
						status, category = 499, "client_cancelled_before_upstream"
					}
					zero := int64(0)
					if p.record(ctx, UsageRecord{Grant: grant, Status: status, Attempt: 0, Usage: Usage{InputTokens: &zero, OutputTokens: &zero, CachedTokens: &zero}, ErrorClass: category}) != nil {
						fail(w, 503, "usage_record_error")
						return
					}
					fail(w, status, category)
					return
				}
				status := authStatus(err)
				zero := int64(0)
				if p.record(ctx, UsageRecord{Grant: grant, Status: status, Attempt: 0, Usage: Usage{InputTokens: &zero, OutputTokens: &zero, CachedTokens: &zero}, ErrorClass: "admission_rejected_before_upstream"}) != nil {
					fail(w, 503, "usage_record_error")
					return
				}
				fail(w, status, "admission_rejected_before_upstream")
				return
			}
		}
		attemptCtx, attemptTrace := newProviderAttemptTrace(ctx, attempt)
		req = req.WithContext(attemptCtx)
		response, err := p.client.Do(req)
		attemptTrace.response(response)
		if err != nil {
			transportDiagnostic("request", err, attemptCtx, response, grant)
			status := 504
			category := "provider_transport_error"
			if r.Context().Err() != nil {
				status = 499
				category = "client_cancelled"
			}
			if p.record(ctx, UsageRecord{Grant: grant, Status: status, Attempt: attempt, ErrorClass: category}) != nil {
				fail(w, 503, "usage_record_error")
				return
			}
			fail(w, status, category)
			return
		}
		if response.StatusCode < 200 || response.StatusCode >= 300 {
			status := response.StatusCode
			delay := retryDelay(response.Header.Get("Retry-After"), attempt, p.cfg.MaxRetryAfter)
			response.Body.Close()
			if p.record(ctx, UsageRecord{Grant: grant, Status: status, Attempt: attempt, ErrorClass: "provider_http_error"}) != nil {
				fail(w, 503, "usage_record_error")
				return
			}
			if retryable(status) && attempt < p.cfg.MaxAttempts {
				timer := time.NewTimer(delay)
				select {
				case <-timer.C:
					continue
				case <-ctx.Done():
					timer.Stop()
					p.stopBeforeUpstream(w, r, ctx, grant)
					return
				}
			}
			downstream := 502
			if status == 429 {
				downstream = 429
			}
			fail(w, downstream, "upstream_error")
			return
		}
		if stream {
			p.forwardStream(w, r, attemptCtx, response, grant, attempt)
			return
		}
		p.forwardJSON(w, attemptCtx, response, grant, attempt)
		return
	}
}

func normalizeProviderRoles(value any) {
	switch item := value.(type) {
	case map[string]any:
		if role, ok := item["role"].(string); ok && role == "developer" {
			item["role"] = "system"
		}
		for _, child := range item {
			normalizeProviderRoles(child)
		}
	case []any:
		for _, child := range item {
			normalizeProviderRoles(child)
		}
	}
}

// The frozen HY Responses endpoint accepts ordinary function tools but rejects
// OpenAI namespace tool containers. Remove those provider-side containers;
// qsim remains guarded by the local relay and the run still records a normal
// scientific zero when an Agent cannot use a tool.
func normalizeProviderTools(value any) {
	if body, ok := value.(map[string]any); ok {
		if tools, ok := body["tools"].([]any); ok {
			filtered := make([]any, 0, len(tools))
			for _, tool := range tools {
				if item, ok := tool.(map[string]any); ok && item["type"] == "namespace" {
					continue
				}
				filtered = append(filtered, tool)
			}
			body["tools"] = filtered
		}
		for _, child := range body {
			normalizeProviderTools(child)
		}
	} else if list, ok := value.([]any); ok {
		for _, child := range list {
			normalizeProviderTools(child)
		}
	}
}

func retryable(status int) bool {
	return status == 429 || status == 502 || status == 503 || status == 504
}
func retryDelay(header string, attempt int, maximum time.Duration) time.Duration {
	delay := time.Duration(1<<uint(attempt-1)) * time.Second
	if number, err := strconv.ParseInt(strings.TrimSpace(header), 10, 64); err == nil {
		if number < 0 {
			return 0
		}
		if number > int64(maximum/time.Second) {
			return maximum
		}
		delay = time.Duration(number) * time.Second
	} else if date, err := http.ParseTime(header); err == nil {
		delay = time.Until(date)
	}
	if delay < 0 {
		return 0
	}
	if delay > maximum {
		return maximum
	}
	return delay
}
func decodeObject(raw []byte) (map[string]any, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var body map[string]any
	if err := decoder.Decode(&body); err != nil || body == nil {
		return nil, errors.New("invalid provider JSON")
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, errors.New("trailing provider JSON")
	}
	return body, nil
}
func count(value any) *int64 {
	number, ok := value.(json.Number)
	if !ok {
		return nil
	}
	n, err := number.Int64()
	if err != nil || n < 0 {
		return nil
	}
	return &n
}
func usageFrom(value any) Usage {
	obj, ok := value.(map[string]any)
	if !ok {
		return Usage{}
	}
	input := obj["input_tokens"]
	if input == nil {
		input = obj["prompt_tokens"]
	}
	output := obj["output_tokens"]
	if output == nil {
		output = obj["completion_tokens"]
	}
	details, _ := obj["input_tokens_details"].(map[string]any)
	if details == nil {
		details, _ = obj["prompt_tokens_details"].(map[string]any)
	}
	return Usage{InputTokens: count(input), OutputTokens: count(output), CachedTokens: count(details["cached_tokens"])}
}
func (p *Proxy) forwardJSON(w http.ResponseWriter, ctx context.Context, response *http.Response, grant Grant, attempt int) {
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, p.cfg.MaxResponseBytes+1))
	record := UsageRecord{Grant: grant, Status: response.StatusCode, Attempt: attempt}
	if err != nil || int64(len(raw)) > p.cfg.MaxResponseBytes {
		if err != nil {
			transportDiagnostic("json_body", err, ctx, response, grant)
		}
		record.Status = 502
		record.ErrorClass = "invalid_provider_body"
		if errors.Is(ctx.Err(), context.Canceled) {
			record.Status = 499
			record.ErrorClass = "client_cancelled"
		} else if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			record.Status = 504
			record.ErrorClass = "provider_timeout"
		}
	} else if body, e := decodeObject(raw); e != nil {
		record.Status = 502
		record.ErrorClass = "invalid_provider_json"
	} else if body["error"] != nil {
		record.Status = 502
		record.ErrorClass = "provider_body_error"
	} else {
		record.Usage = usageFrom(body["usage"])
	}
	if p.record(ctx, record) != nil {
		fail(w, 503, "usage_record_error")
		return
	}
	if record.Status >= 300 {
		fail(w, record.Status, "upstream_error")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(response.StatusCode)
	_, _ = w.Write(raw)
}
func streamError(w http.ResponseWriter) {
	_, _ = io.WriteString(w, "event: error\ndata: {\"error\":{\"type\":\"upstream_error\",\"message\":\"Model stream interrupted\"}}\n\n")
	if flush, ok := w.(http.Flusher); ok {
		flush.Flush()
	}
}
func (p *Proxy) forwardStream(w http.ResponseWriter, r *http.Request, ctx context.Context, response *http.Response, grant Grant, attempt int) {
	defer response.Body.Close()
	record := UsageRecord{Grant: grant, Status: 200, Attempt: attempt}
	recorded := false
	defer func() {
		if !recorded {
			if r.Context().Err() != nil {
				record.Status = 499
				record.ErrorClass = "client_cancelled"
			} else if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				record.Status = 504
				record.ErrorClass = "provider_timeout"
			} else if record.Status == 200 {
				record.Status = 502
				record.ErrorClass = "incomplete_stream"
			}
			if p.record(ctx, record) != nil && r.Context().Err() == nil {
				streamError(w)
			}
		}
	}()
	if !strings.HasPrefix(strings.ToLower(response.Header.Get("Content-Type")), "text/event-stream") {
		record.Status = 502
		record.ErrorClass = "invalid_stream_type"
		fail(w, 502, "upstream_error")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Accel-Buffering", "no")
	scanner := bufio.NewScanner(response.Body)
	scanner.Buffer(make([]byte, min(64<<10, p.cfg.MaxEventBytes)), p.cfg.MaxEventBytes)
	// Count SSE body bytes consumed by the parser, including comments, UTF-8
	// bytes and CRLF delimiters. Per-event limits alone permit an unbounded
	// sequence of small events. Count at splitting time so a valid terminal
	// does not require reading or validating an arbitrary trailing body.
	streamLimitErr := errors.New("stream response byte limit")
	remaining := p.cfg.MaxResponseBytes
	scanner.Split(func(data []byte, atEOF bool) (int, []byte, error) {
		advance, token, err := bufio.ScanLines(data, atEOF)
		if int64(advance) > remaining || (advance == 0 && int64(len(data)) > remaining) {
			return 0, nil, streamLimitErr
		}
		remaining -= int64(advance)
		return advance, token, err
	})
	event := []string{}
	eventBytes := 0
	emit := func() bool {
		if len(event) == 0 {
			return true
		}
		data := []string{}
		for _, line := range event {
			if strings.HasPrefix(line, "data:") {
				data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
			}
		}
		terminal := false
		if len(data) > 0 {
			payload := strings.Join(data, "\n")
			if payload == "[DONE]" {
				terminal = true
			} else {
				object, err := decodeObject([]byte(payload))
				if err != nil {
					record.Status = 502
					record.ErrorClass = "invalid_stream_json"
					streamError(w)
					return false
				}
				if raw, ok := object["usage"]; ok {
					record.Usage = usageFrom(raw)
				}
				if object["type"] == "response.completed" || object["type"] == "response.incomplete" {
					terminal = true
					responseObject, _ := object["response"].(map[string]any)
					record.Usage = usageFrom(responseObject["usage"])
				}
				if object["type"] == "response.failed" || object["type"] == "error" || object["error"] != nil {
					record.Status = 502
					record.ErrorClass = "provider_stream_error"
					streamError(w)
					return false
				}
			}
		}
		if terminal {
			recorded = true
			if p.record(ctx, record) != nil {
				streamError(w)
				return false
			}
		}
		if _, err := io.WriteString(w, strings.Join(event, "\n")+"\n\n"); err != nil {
			record.Status = 499
			record.ErrorClass = "client_write_error"
			return false
		}
		if flush, ok := w.(http.Flusher); ok {
			flush.Flush()
		}
		event = nil
		eventBytes = 0
		return !terminal
	}
	for scanner.Scan() {
		line := strings.TrimSuffix(scanner.Text(), "\r")
		if line == "" {
			if !emit() {
				return
			}
			continue
		}
		eventBytes += len(line) + 1
		if eventBytes > p.cfg.MaxEventBytes {
			record.Status = 502
			record.ErrorClass = "stream_event_too_large"
			streamError(w)
			return
		}
		event = append(event, line)
	}
	if err := scanner.Err(); err != nil {
		// A buffered partial event is not complete after a read/size failure;
		// in particular it must not turn an over-budget terminal into success.
		if r.Context().Err() != nil {
			transportDiagnostic("stream_body", r.Context().Err(), ctx, response, grant)
		} else {
			record.Status = 502
			record.ErrorClass = "incomplete_stream"
			if errors.Is(err, streamLimitErr) {
				record.ErrorClass = "stream_response_too_large"
			} else if errors.Is(err, bufio.ErrTooLong) {
				record.ErrorClass = "stream_event_too_large"
			} else {
				transportDiagnostic("stream_body", err, ctx, response, grant)
			}
			streamError(w)
		}
		return
	}
	if len(event) > 0 {
		if !emit() {
			return
		}
	}
	if r.Context().Err() == nil {
		transportDiagnostic("stream_body", scanner.Err(), ctx, response, grant)
		record.Status = 502
		record.ErrorClass = "incomplete_stream"
		streamError(w)
	}
}
