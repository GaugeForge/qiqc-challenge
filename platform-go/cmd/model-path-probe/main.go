// model-path-probe compares fixed public synthetic streams through the production
// proxy and directly through the same transport settings. It never opens a run DB.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/gaugeforge/qiqc-platform-go/internal/modelproxy"
)

const model = "tencent/hy4-preview"
const prompt = "This is a public synthetic network diagnostic. Write approximately 400 to 700 tokens explaining how to organize a bookshelf by author and title. Use only ordinary public information. Do not call tools."
const scientificPrompt = "This is a public synthetic programming and reasoning diagnostic; it contains no contest task. Consider books numbered 1 through 18 with integer widths [7,3,9,4,8,6,2,5,11,10,3,7,4,6,9,2,8,5]. Three identical shelves each have width capacity 40. Design an exact algorithm to assign all books while minimizing the difference between the widest and narrowest occupied shelf. Explain correctness, symmetry reduction, pruning, and complexity. Provide Python code and test cases, and reason carefully about a concrete feasible assignment and a lower bound. Do not call tools or access the internet."
const toolPrompt = "This is a fixed public synthetic tool-call diagnostic, not a contest task. Books numbered 1 through 18 have widths [7,3,9,4,8,6,2,5,11,10,3,7,4,6,9,2,8,5]. Carefully sum their widths and call record_book_widths exactly once. Supply total_width and all 18 books in original order, each with its id and width. Do not provide code, access external data, or invent another tool. The caller will validate arguments in memory and will not execute a tool."
const syntheticToolName = "record_book_widths"

type scenario struct {
	name      string
	endpoints []string
	maxTokens int
	timeout   time.Duration
	input     string
	reasoning any
	pairs     int
}

func concurrentPrompt() string {
	var b strings.Builder
	b.WriteString(scientificPrompt)
	for i := 0; b.Len() < 32*1024; i++ {
		fmt.Fprintf(&b, "\nPublic synthetic note %04d: The book labels and shelf capacities above are public examples. A correct deterministic search visits each feasible assignment, prunes capacity violations, and treats shelf permutations as equivalent. This note adds public network-test padding, not new constraints.", i)
	}
	return b.String()
}

func selectScenario(name string) (scenario, error) {
	switch name {
	case "short":
		return scenario{name, []string{"responses", "chat/completions"}, 1024, 90 * time.Second, prompt, map[string]any{"effort": "none"}, 1}, nil
	case "scientific-stream":
		return scenario{name, []string{"responses"}, 4096, 180 * time.Second, scientificPrompt, nil, 1}, nil
	case "concurrent-stream":
		return scenario{name, []string{"responses"}, 2048, 180 * time.Second, concurrentPrompt(), nil, 10}, nil
	case "tool-call":
		return scenario{name, []string{"responses", "chat/completions"}, 4096, 180 * time.Second, toolPrompt, nil, 1}, nil
	case "namespace-tool-call":
		return scenario{name, []string{"responses"}, 2048, 180 * time.Second, strings.Replace(toolPrompt, "call record_book_widths", "call functions.record_book_widths", 1), nil, 1}, nil
	default:
		return scenario{}, errors.New("unsupported fixed scenario")
	}
}

type result struct {
	SampleID                 int               `json:"sample_id"`
	Endpoint                 string            `json:"endpoint"`
	Path                     string            `json:"path"`
	HTTPStatus               int               `json:"http_status"`
	Elapsed                  float64           `json:"elapsed_seconds"`
	Bytes                    int               `json:"body_bytes"`
	Terminal                 string            `json:"terminal"`
	Complete                 bool              `json:"complete"`
	Events                   []string          `json:"event_types"`
	Usage                    map[string]int64  `json:"usage"`
	ErrorClass               string            `json:"error_class,omitempty"`
	ProxyUsage               []usageSummary    `json:"proxy_usage,omitempty"`
	StartedUTC               string            `json:"started_utc"`
	FinishedUTC              string            `json:"finished_utc"`
	PeerIP                   string            `json:"direct_peer_ip,omitempty"`
	ALPN                     string            `json:"direct_alpn,omitempty"`
	Headers                  map[string]string `json:"safe_response_headers,omitempty"`
	FirstByteSeconds         *float64          `json:"http_first_byte_seconds,omitempty"`
	TestBoundary             string            `json:"test_boundary,omitempty"`
	UpstreamPeerIP           string            `json:"upstream_peer_ip,omitempty"`
	UpstreamLocalPort        string            `json:"upstream_local_port,omitempty"`
	UpstreamALPN             string            `json:"upstream_alpn,omitempty"`
	UpstreamFirstByteSeconds *float64          `json:"upstream_http_first_byte_seconds,omitempty"`
	ToolCallCount            int               `json:"tool_call_count,omitempty"`
	ToolArgumentBytes        int               `json:"tool_argument_bytes,omitempty"`
	ToolContractValid        *bool             `json:"tool_contract_valid,omitempty"`
}
type usageSummary struct {
	Status     int              `json:"status"`
	Attempt    int              `json:"attempt"`
	Usage      modelproxy.Usage `json:"usage"`
	ErrorClass string           `json:"error_class,omitempty"`
}

func directClient() *http.Client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.Proxy = nil
	t.ResponseHeaderTimeout = 30 * time.Second
	t.ForceAttemptHTTP2 = false
	t.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
	t.TLSClientConfig = &tls.Config{NextProtos: []string{"http/1.1"}}
	t.DisableKeepAlives = true
	t.MaxConnsPerHost = 2
	return &http.Client{Transport: t, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

func errorClass(err error) string {
	if err == nil {
		return ""
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "deadline_exceeded"
	}
	if errors.Is(err, context.Canceled) {
		return "cancelled"
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return "network_timeout"
	}
	msg := strings.ToLower(err.Error())
	if strings.Contains(msg, "connection reset") {
		return "connection_reset"
	}
	if strings.Contains(msg, "unexpected eof") {
		return "unexpected_eof"
	}
	if errors.Is(err, io.EOF) {
		return "eof"
	}
	return "transport_error"
}

func tokenUsage(v any, dst map[string]int64) {
	m, ok := v.(map[string]any)
	if !ok {
		return
	}
	for _, key := range []string{"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens"} {
		if n, ok := m[key].(float64); ok {
			dst[key] = int64(n)
		}
	}
}

// Only public routing/correlation metadata is retained; arbitrary response
// headers, provider errors, text deltas, and credentials must not enter reports.
func safeHeaders(h http.Header) map[string]string {
	result := map[string]string{}
	for _, key := range []string{"x-provider-name", "cf-ray", "x-generation-id", "request-id", "x-request-id"} {
		v := h.Get(key)
		if v == "" || len(v) > 256 {
			continue
		}
		safe := true
		for _, c := range v {
			if c < 32 || c > 126 {
				safe = false
				break
			}
		}
		if safe {
			result[key] = v
		}
	}
	return result
}
func summarize(body []byte, endpoint string) result {
	r := result{Bytes: len(body), Terminal: "missing", Usage: map[string]int64{}}
	events := map[string]bool{}
	finish, done, failed := false, false, false
	s := bufio.NewScanner(bytes.NewReader(body))
	s.Buffer(make([]byte, 4096), 2<<20)
	for s.Scan() {
		line := s.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "[DONE]" {
			done = true
			events["DONE"] = true
			continue
		}
		var item map[string]any
		if json.Unmarshal([]byte(data), &item) != nil {
			failed = true
			r.ErrorClass = "invalid_sse_json"
			continue
		}
		kind, _ := item["type"].(string)
		switch kind {
		case "response.created", "response.in_progress", "response.output_item.added", "response.content_part.added", "response.output_text.delta", "response.output_text.done", "response.content_part.done", "response.output_item.done", "response.function_call_arguments.delta", "response.function_call_arguments.done", "response.completed", "response.incomplete", "response.failed", "error":
			events[kind] = true
		default:
			if kind != "" {
				events["other_event"] = true
			}
		}
		if kind == "response.completed" {
			r.Terminal = "response.completed"
			r.Complete = true
		}
		if kind == "response.failed" || kind == "response.incomplete" || kind == "error" || item["error"] != nil {
			failed = true
			r.Terminal = "error_or_incomplete"
		}
		tokenUsage(item["usage"], r.Usage)
		if response, ok := item["response"].(map[string]any); ok {
			tokenUsage(response["usage"], r.Usage)
			if detail, ok := response["incomplete_details"].(map[string]any); ok && detail["reason"] == "max_output_tokens" {
				r.TestBoundary = "max_output_tokens"
			}
			if st, _ := response["status"].(string); st == "failed" || st == "incomplete" {
				failed = true
				r.Terminal = "error_or_incomplete"
			}
		}
		if choices, ok := item["choices"].([]any); ok {
			events["chat.completion.chunk"] = true
			for _, choice := range choices {
				c, _ := choice.(map[string]any)
				reason, _ := c["finish_reason"].(string)
				if reason == "stop" || reason == "tool_calls" {
					finish = true
				}
				if reason == "length" || reason == "content_filter" || reason == "error" {
					failed = true
					r.Terminal = "error_or_incomplete"
				}
			}
		}
	}
	if s.Err() != nil {
		failed = true
		r.ErrorClass = "sse_scan_limit"
	}
	if endpoint == "chat/completions" && done && finish && !failed {
		r.Terminal = "finish_and_done"
		r.Complete = true
	}
	if failed {
		r.Complete = false
	}
	for kind := range events {
		r.Events = append(r.Events, kind)
	}
	sort.Strings(r.Events)
	return r
}

func payload(endpoint string) []byte {
	s, _ := selectScenario("short")
	return scenarioPayload(endpoint, s)
}

func scenarioPayload(endpoint string, s scenario) []byte {
	body := map[string]any{"model": model, "stream": true, "reasoning": s.reasoning}
	if endpoint == "responses" {
		body["input"] = s.input
		body["max_output_tokens"] = s.maxTokens
	} else {
		body["messages"] = []any{map[string]any{"role": "user", "content": s.input}}
		body["max_tokens"] = s.maxTokens
		body["stream_options"] = map[string]any{"include_usage": true}
	}
	if s.name == "tool-call" || s.name == "namespace-tool-call" {
		parameters := map[string]any{"type": "object", "additionalProperties": false, "required": []string{"total_width", "books"}, "properties": map[string]any{
			"total_width": map[string]any{"type": "integer"},
			"books":       map[string]any{"type": "array", "items": map[string]any{"type": "object", "additionalProperties": false, "required": []string{"id", "width"}, "properties": map[string]any{"id": map[string]any{"type": "integer"}, "width": map[string]any{"type": "integer"}}}},
		}}
		function := map[string]any{"name": syntheticToolName, "description": "Record the public synthetic book widths and total; no code is executed.", "parameters": parameters, "strict": true}
		body["parallel_tool_calls"] = false
		if endpoint == "responses" {
			function["type"] = "function"
			body["tools"] = []any{function}
			body["tool_choice"] = map[string]any{"type": "function", "name": syntheticToolName}
			if s.name == "namespace-tool-call" {
				body["tools"] = []any{map[string]any{"type": "namespace", "name": "functions", "description": "Public synthetic bookshelf metadata functions.", "tools": []any{function}}}
				body["tool_choice"] = "required"
			}
		} else {
			body["tools"] = []any{map[string]any{"type": "function", "function": function}}
			body["tool_choice"] = map[string]any{"type": "function", "function": map[string]any{"name": syntheticToolName}}
		}
	}
	raw, _ := json.Marshal(body)
	return raw
}

// Tool arguments remain only in memory. This checks a fixed public fixture and
// never executes model-generated code, tools, shell text, or external requests.
func summarizeToolCalls(raw []byte, endpoint string) (int, int, bool) {
	return summarizeToolCallsExpected(raw, endpoint, "")
}
func summarizeToolCallsExpected(raw []byte, endpoint, expectedNamespace string) (int, int, bool) {
	type call struct{ name, args, namespace string }
	calls := map[int]*call{}
	toolFinish := endpoint == "responses"
	s := bufio.NewScanner(bytes.NewReader(raw))
	s.Buffer(make([]byte, 4096), 2<<20)
	for s.Scan() {
		line := s.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		var item map[string]any
		if json.Unmarshal([]byte(strings.TrimSpace(strings.TrimPrefix(line, "data:"))), &item) != nil {
			continue
		}
		if endpoint == "responses" {
			if response, ok := item["response"].(map[string]any); ok {
				if output, ok := response["output"].([]any); ok {
					calls = map[int]*call{}
					for i, value := range output {
						v, _ := value.(map[string]any)
						if v["type"] == "function_call" {
							name, _ := v["name"].(string)
							args, _ := v["arguments"].(string)
							namespace, _ := v["namespace"].(string)
							calls[i] = &call{name, args, namespace}
						}
					}
				}
			}
		} else if choices, ok := item["choices"].([]any); ok {
			for _, choice := range choices {
				c, _ := choice.(map[string]any)
				if c["finish_reason"] == "tool_calls" {
					toolFinish = true
				}
				delta, _ := c["delta"].(map[string]any)
				tools, _ := delta["tool_calls"].([]any)
				for _, value := range tools {
					v, _ := value.(map[string]any)
					index, ok := v["index"].(float64)
					if !ok || index < 0 || index > 7 || index != float64(int(index)) {
						return len(calls), 0, false
					}
					entry := calls[int(index)]
					if entry == nil {
						entry = &call{}
						calls[int(index)] = entry
					}
					function, _ := v["function"].(map[string]any)
					name, _ := function["name"].(string)
					args, _ := function["arguments"].(string)
					entry.name += name
					entry.args += args
				}
			}
		}
	}
	valid := s.Err() == nil && len(calls) == 1 && toolFinish
	totalBytes := 0
	widths := []int{7, 3, 9, 4, 8, 6, 2, 5, 11, 10, 3, 7, 4, 6, 9, 2, 8, 5}
	for _, entry := range calls {
		totalBytes += len(entry.args)
		if entry.name != syntheticToolName || entry.namespace != expectedNamespace {
			valid = false
		}
		var arguments struct {
			TotalWidth int `json:"total_width"`
			Books      []struct {
				ID    int `json:"id"`
				Width int `json:"width"`
			} `json:"books"`
		}
		d := json.NewDecoder(strings.NewReader(entry.args))
		d.DisallowUnknownFields()
		if d.Decode(&arguments) != nil {
			valid = false
			continue
		}
		var trailing any
		if d.Decode(&trailing) != io.EOF {
			valid = false
		}
		if arguments.TotalWidth != 109 || len(arguments.Books) != len(widths) {
			valid = false
			continue
		}
		for i, book := range arguments.Books {
			if book.ID != i+1 || book.Width != widths[i] {
				valid = false
			}
		}
	}
	return len(calls), totalBytes, valid
}

func probe(base, key, endpoint, path string, s scenario) result {
	start := time.Now()
	r := result{Endpoint: endpoint, Path: path, Terminal: "missing", Usage: map[string]int64{}}
	var mu sync.Mutex
	var recorded []usageSummary
	var upstreamPeer, upstreamPort, upstreamALPN string
	var upstreamFirstByte *float64
	upstreamTrace := &httptrace.ClientTrace{
		GotConn: func(info httptrace.GotConnInfo) {
			host, _, err := net.SplitHostPort(info.Conn.RemoteAddr().String())
			_, port, portErr := net.SplitHostPort(info.Conn.LocalAddr().String())
			mu.Lock()
			defer mu.Unlock()
			if err == nil && net.ParseIP(host) != nil {
				upstreamPeer = host
			}
			if portErr == nil {
				upstreamPort = port
			}
		},
		TLSHandshakeDone: func(state tls.ConnectionState, err error) {
			if err == nil {
				mu.Lock()
				upstreamALPN = state.NegotiatedProtocol
				mu.Unlock()
			}
		},
		GotFirstResponseByte: func() { elapsed := time.Since(start).Seconds(); mu.Lock(); upstreamFirstByte = &elapsed; mu.Unlock() },
	}
	client := directClient()
	target, _ := modelproxy.UpstreamURL(base, endpoint)
	auth := key
	if path == "proxy" {
		tokenBytes := make([]byte, 32)
		if _, err := rand.Read(tokenBytes); err != nil {
			r.ErrorClass = "random_failed"
			return r
		}
		token := hex.EncodeToString(tokenBytes)
		p, err := modelproxy.New(modelproxy.Config{BaseURL: base, MasterKey: key, RequiredModel: model, MaxAttempts: 1, MaxConcurrent: 2, RequestTimeout: s.timeout,
			Authorize: func(_ context.Context, t, m string) (modelproxy.Grant, error) {
				if t != token || m != model {
					return modelproxy.Grant{}, errors.New("synthetic token rejected")
				}
				return modelproxy.Grant{RunID: "synthetic-public-probe", Attempt: 1, Model: model, TokenHash: "synthetic-only"}, nil
			},
			RecordUsage: func(_ context.Context, u modelproxy.UsageRecord) error {
				mu.Lock()
				defer mu.Unlock()
				recorded = append(recorded, usageSummary{u.Status, u.Attempt, u.Usage, u.ErrorClass})
				return nil
			},
		})
		if err != nil {
			r.ErrorClass = "proxy_config"
			return r
		}
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			p.ServeHTTP(w, r.WithContext(httptrace.WithClientTrace(r.Context(), upstreamTrace)))
		}))
		defer srv.Close()
		target = srv.URL + "/v1/" + endpoint
		auth = token
	}
	ctx, cancel := context.WithTimeout(context.Background(), s.timeout)
	defer cancel()
	var peerIP, alpn string
	var firstByte *float64
	trace := &httptrace.ClientTrace{GotFirstResponseByte: func() { elapsed := time.Since(start).Seconds(); mu.Lock(); firstByte = &elapsed; mu.Unlock() }}
	if path == "direct" {
		ctx = httptrace.WithClientTrace(ctx, upstreamTrace)
		trace.GotConn = func(info httptrace.GotConnInfo) {
			host, _, err := net.SplitHostPort(info.Conn.RemoteAddr().String())
			if err == nil && net.ParseIP(host) != nil {
				mu.Lock()
				peerIP = host
				mu.Unlock()
			}
		}
		trace.TLSHandshakeDone = func(state tls.ConnectionState, err error) {
			if err == nil {
				mu.Lock()
				alpn = state.NegotiatedProtocol
				mu.Unlock()
			}
		}
	}
	ctx = httptrace.WithClientTrace(ctx, trace)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(scenarioPayload(endpoint, s)))
	req.Header.Set("Authorization", "Bearer "+auth)
	req.Header.Set("Content-Type", "application/json")
	response, err := client.Do(req)
	if err != nil {
		r.ErrorClass = errorClass(err)
	} else {
		r.HTTPStatus = response.StatusCode
		raw, readErr := io.ReadAll(io.LimitReader(response.Body, 2<<20))
		response.Body.Close()
		summary := summarize(raw, endpoint)
		summary.HTTPStatus = r.HTTPStatus
		summary.Endpoint = endpoint
		summary.Path = path
		summary.Headers = safeHeaders(response.Header)
		if s.name == "tool-call" || s.name == "namespace-tool-call" {
			namespace := ""
			if s.name == "namespace-tool-call" {
				namespace = "functions"
			}
			count, size, valid := summarizeToolCallsExpected(raw, endpoint, namespace)
			summary.ToolCallCount = count
			summary.ToolArgumentBytes = size
			summary.ToolContractValid = &valid
		}
		r = summary
		if readErr != nil {
			r.ErrorClass = errorClass(readErr)
			r.Complete = false
		}
		if r.HTTPStatus != 200 {
			r.Complete = false
			r.ErrorClass = "http_non_200"
			if r.HTTPStatus == 429 {
				r.ErrorClass = "http_rate_limited"
			}
		}
		if len(raw) >= 2<<20 {
			r.Complete = false
			r.ErrorClass = "body_limit"
		}
	}
	r.Elapsed = time.Since(start).Seconds()
	if r.ErrorClass == "deadline_exceeded" && r.Elapsed >= s.timeout.Seconds()-1 {
		r.TestBoundary = "diagnostic_deadline"
	}
	r.StartedUTC = start.UTC().Format(time.RFC3339Nano)
	r.FinishedUTC = time.Now().UTC().Format(time.RFC3339Nano)
	mu.Lock()
	r.ProxyUsage = append([]usageSummary(nil), recorded...)
	r.PeerIP, r.ALPN = peerIP, alpn
	r.FirstByteSeconds = firstByte
	r.UpstreamPeerIP, r.UpstreamLocalPort, r.UpstreamALPN = upstreamPeer, upstreamPort, upstreamALPN
	r.UpstreamFirstByteSeconds = upstreamFirstByte
	mu.Unlock()
	return r
}

func credentials() (string, string, error) {
	data, err := os.ReadFile("/etc/qiqc-staging/provider.env")
	if err != nil {
		return "", "", errors.New("credentials unavailable")
	}
	values := map[string]string{}
	for _, line := range strings.Split(string(data), "\n") {
		k, v, ok := strings.Cut(strings.TrimSpace(line), "=")
		k = strings.TrimPrefix(k, "export ")
		if ok && (k == "HY_API" || k == "HY_URL") {
			values[k] = strings.Trim(strings.TrimSpace(v), "\"'")
		}
	}
	endpoint, err := modelproxy.UpstreamURL(values["HY_URL"], "responses")
	if err != nil {
		return "", "", errors.New("invalid provider URL")
	}
	u, err := url.Parse(endpoint)
	if err != nil || u.Scheme != "https" || u.Host != "openrouter.ai" || u.Path != "/api/v1/responses" || values["HY_API"] == "" {
		return "", "", errors.New("expected private OpenRouter configuration")
	}
	return values["HY_URL"], values["HY_API"], nil
}

func run() error {
	real := flag.Bool("real-hy4", false, "authorize bounded real paid Hy4 requests for the selected fixed public scenario")
	scenarioName := flag.String("scenario", "short", "short (4), scientific-stream (2), concurrent-stream (20), tool-call (4), or namespace-tool-call (2)")
	output := flag.String("output", "", "new summary JSON file (0600), no content or credentials")
	flag.Parse()
	if !*real || *output == "" || flag.NArg() != 0 {
		return errors.New("requires --real-hy4 and --output")
	}
	s, err := selectScenario(*scenarioName)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(*output, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return errors.New("output must be a new writable file")
	}
	defer f.Close()
	base, key, err := credentials()
	if err != nil {
		return err
	}
	slog.SetDefault(slog.New(slog.NewTextHandler(io.Discard, nil)))
	rows := make([]result, len(s.endpoints)*s.pairs*2)
	started := time.Now().UTC().Format(time.RFC3339)
	for i, endpoint := range s.endpoints {
		var wg sync.WaitGroup
		startBarrier := make(chan struct{})
		for pair := 0; pair < s.pairs; pair++ {
			for j, path := range []string{"direct", "proxy"} {
				wg.Add(1)
				go func(index int, e, p string) {
					defer wg.Done()
					<-startBarrier
					rows[index] = probe(base, key, e, p, s)
					rows[index].SampleID = index + 1
				}(i*s.pairs*2+pair*2+j, endpoint, path)
			}
		}
		close(startBarrier)
		wg.Wait()
	}
	passed := true
	for _, row := range rows {
		passed = passed && row.Complete
		if s.name == "tool-call" || s.name == "namespace-tool-call" {
			passed = passed && row.ToolContractValid != nil && *row.ToolContractValid
		}
	}
	report := map[string]any{"started_utc": started, "finished_utc": time.Now().UTC().Format(time.RFC3339), "scenario": s.name, "reasoning": s.reasoning, "model": model, "requests": len(rows), "max_concurrency": s.pairs * 2, "max_attempts": 1, "request_timeout_seconds": s.timeout.Seconds(), "max_output_tokens": s.maxTokens, "total_output_token_limit": len(rows) * s.maxTokens, "public_input_bytes": len(s.input), "independent_proxy_instances": true, "public_synthetic_input_only": true, "scientific_runs": 0, "database_access": false, "passed": passed, "results": rows, "limitation": "Bounded public requests cannot prove that long scientific streams or high concurrency reset failures are resolved; token and total deadline limits are diagnostic boundaries. Proxy samples use independent instances and do not validate production shared admission capacity."}
	if err := json.NewEncoder(f).Encode(report); err != nil {
		return errors.New("summary write failed")
	}
	fmt.Printf("synthetic requests=%d complete=%t summary_written=true\n", len(rows), passed)
	return nil
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(1)
	}
}
