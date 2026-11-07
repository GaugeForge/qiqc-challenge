package modelproxy

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"net/url"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func captureAttemptLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	buffer := &bytes.Buffer{}
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(buffer, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return buffer
}

func decodedAttemptLogs(t *testing.T, buffer *bytes.Buffer) []map[string]any {
	t.Helper()
	var logs []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(buffer.String()), "\n") {
		var entry map[string]any
		if json.Unmarshal([]byte(line), &entry) != nil {
			t.Fatal("invalid diagnostic JSON")
		}
		if entry["msg"] == "model_transport_diagnostic" {
			logs = append(logs, entry)
		}
	}
	return logs
}

type traceFixtureTransport func(*http.Request) (*http.Response, error)

func (f traceFixtureTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type traceFixtureConn struct{ address net.Addr }

type traceLocalFixtureConn struct {
	traceFixtureConn
	local net.Addr
}

func (c traceLocalFixtureConn) LocalAddr() net.Addr { return c.local }

func (c traceFixtureConn) RemoteAddr() net.Addr           { return c.address }
func (traceFixtureConn) LocalAddr() net.Addr              { return &net.TCPAddr{} }
func (traceFixtureConn) Read([]byte) (int, error)         { return 0, io.EOF }
func (traceFixtureConn) Write(b []byte) (int, error)      { return len(b), nil }
func (traceFixtureConn) Close() error                     { return nil }
func (traceFixtureConn) SetDeadline(time.Time) error      { return nil }
func (traceFixtureConn) SetReadDeadline(time.Time) error  { return nil }
func (traceFixtureConn) SetWriteDeadline(time.Time) error { return nil }

func TestAttemptTraceRealTLSBodyFailureContainsOnlySafeMetadata(t *testing.T) {
	buffer := captureAttemptLogs(t)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Length", "10000")
		w.Header().Set("X-Request-ID", "forbidden provider header")
		w.Header().Set("X-Generation-ID", "gen-123_Fixture")
		w.Header().Set("Cf-Ray", "0123456789abcdef-FRA")
		fmt.Fprint(w, `{"private":"forbidden-response-text"}`)
	}))
	defer server.Close()
	cfg, records, _ := config(t, func(http.ResponseWriter, *http.Request) {})
	cfg.BaseURL, cfg.Client = server.URL+"/private-provider-path", server.Client()
	out := perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test","input":"forbidden-request-text"}`)
	logs := decodedAttemptLogs(t, buffer)
	if len(logs) != 1 || len(*records) != 1 {
		t.Fatalf("logs=%d records=%d", len(logs), len(*records))
	}
	v := logs[0]
	if port, ok := v["upstream_local_port"].(float64); !ok || port < 1 || port > 65535 {
		t.Fatal("real TLS connection missing bounded local port")
	}
	if v["upstream_generation_id"] != "gen-123_Fixture" || v["upstream_cf_ray"] != "0123456789abcdef-FRA" || v["upstream_provider_request_id"] != nil {
		t.Fatal("provider identifier allowlist not enforced")
	}
	if strings.Contains(out.Body.String(), "gen-123_Fixture") || out.Header().Get("X-Generation-ID") != "" || out.Header().Get("Cf-Ray") != "" {
		t.Fatal("administrator trace exposed to participant")
	}
	if v["class"] != "unexpected_eof" || v["context_state"] != "active" || v["upstream_peer_ip"] != "127.0.0.1" || v["upstream_connection_observed"] != true || v["upstream_connection_reused"] != false || v["upstream_attempt"] != float64(1) || v["upstream_response_body_bytes"] != float64(len(`{"private":"forbidden-response-text"}`)) {
		t.Fatalf("missing trace metadata: %+v", v)
	}
	for _, field := range []string{"upstream_elapsed_ms", "upstream_request_written_ms", "upstream_first_response_byte_ms", "upstream_response_headers_ms", "upstream_first_body_byte_ms", "upstream_last_body_byte_ms", "upstream_body_elapsed_ms", "upstream_body_idle_ms"} {
		if number, ok := v[field].(float64); !ok || number < 0 {
			t.Fatalf("missing timing %s", field)
		}
	}
	if _, err := time.Parse(time.RFC3339Nano, v["upstream_started_at"].(string)); err != nil {
		t.Fatal("missing actual start time")
	}
	for _, secret := range []string{"provider-secret-not-run-token", "forbidden provider header", "forbidden-request-text", "forbidden-response-text", "private-provider-path"} {
		if strings.Contains(buffer.String(), secret) {
			t.Fatal("sensitive value in trace")
		}
	}
}

func TestAttemptTraceIdentifierHeadersRejectUnboundedOrStructuredValues(t *testing.T) {
	for _, value := range []string{"", strings.Repeat("x", 129), "private value", "private\nvalue", "private:value", "https://private.invalid/key", "private?key=value", "中文", "private.value"} {
		headers := http.Header{"X-Generation-Id": {value}}
		if safeProviderIdentifier(headers, "X-Generation-Id") != "" {
			t.Fatal("invalid provider identifier accepted")
		}
	}
	for _, value := range []string{"gen-123_Abc", strings.Repeat("a", 128)} {
		if safeProviderIdentifier(http.Header{"X-Request-Id": {value}}, "X-Request-Id") != value {
			t.Fatal("valid provider identifier rejected")
		}
	}
	if safeProviderIdentifier(http.Header{"Cf-Ray": {"one", "two"}}, "Cf-Ray") != "" {
		t.Fatal("duplicate identifier accepted")
	}
}

func TestAttemptTraceConcurrentRequestsDoNotSharePeerTimingOrByteCounters(t *testing.T) {
	buffer := captureAttemptLogs(t)
	cfg, records, _ := config(t, func(http.ResponseWriter, *http.Request) {})
	cfg.MaxConcurrent = 49
	cfg.Authorize = func(_ context.Context, token, model string) (Grant, error) {
		return Grant{RunID: token, Attempt: 1, Model: model, TokenHash: "private-capability"}, nil
	}
	cfg.Client = &http.Client{Transport: traceFixtureTransport(func(r *http.Request) (*http.Response, error) {
		var body struct {
			Index int `json:"index"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			return nil, err
		}
		trace := httptrace.ContextClientTrace(r.Context())
		var callbacks sync.WaitGroup
		for i := 0; i < 10; i++ {
			callbacks.Add(1)
			go func() {
				defer callbacks.Done()
				trace.GotConn(httptrace.GotConnInfo{Conn: traceLocalFixtureConn{
					traceFixtureConn: traceFixtureConn{&net.TCPAddr{IP: net.IPv4(192, 0, 2, byte(body.Index+1)), Port: 443}},
					local:            &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 40000 + body.Index},
				}, Reused: body.Index%2 == 0})
				trace.GotFirstResponseByte()
				trace.WroteRequest(httptrace.WroteRequestInfo{})
				trace.TLSHandshakeDone(tls.ConnectionState{NegotiatedProtocol: "http/1.1"}, nil)
				_ = providerTraceFields(r.Context())
			}()
		}
		callbacks.Wait()
		return &http.Response{StatusCode: 200, ProtoMajor: 1, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: &terminalThenErrorReader{payload: ":" + strings.Repeat("x", body.Index+1) + "\n\n", err: syscall.ECONNRESET}}, nil
	})}
	p := build(t, cfg)
	var callers sync.WaitGroup
	for i := 0; i < 49; i++ {
		callers.Add(1)
		go func(i int) {
			defer callers.Done()
			r := httptest.NewRequest("POST", "/v1/responses", strings.NewReader(fmt.Sprintf(`{"model":"hy4-test","stream":true,"index":%d}`, i)))
			r.Header.Set("Authorization", fmt.Sprintf("Bearer run-%d", i))
			p.ServeHTTP(httptest.NewRecorder(), r)
		}(i)
	}
	callers.Wait()
	logs := decodedAttemptLogs(t, buffer)
	if len(logs) != 49 || len(*records) != 49 {
		t.Fatalf("logs=%d records=%d", len(logs), len(*records))
	}
	seen := map[string]bool{}
	for _, v := range logs {
		var index int
		if _, err := fmt.Sscanf(v["run_id"].(string), "run-%d", &index); err != nil {
			t.Fatal("missing run")
		}
		if v["upstream_peer_ip"] != fmt.Sprintf("192.0.2.%d", index+1) || v["upstream_local_port"] != float64(40000+index) || v["upstream_response_body_bytes"] != float64(index+4) || v["upstream_connection_reused"] != (index%2 == 0) || v["upstream_alpn"] != "http/1.1" || v["class"] != "connection_reset" || v["context_state"] != "active" {
			t.Fatalf("cross-request contamination: %+v", v)
		}
		seen[v["request_id"].(string)] = true
	}
	if len(seen) != 49 || strings.Contains(buffer.String(), "private-capability") {
		t.Fatal("identity collision or capability leak")
	}
}

func TestAttemptTraceRetryStartsFreshAndUnknownTimingIsExplicit(t *testing.T) {
	buffer := captureAttemptLogs(t)
	cfg, _, _ := config(t, func(http.ResponseWriter, *http.Request) {})
	attempt := 0
	cfg.Client = &http.Client{Transport: traceFixtureTransport(func(r *http.Request) (*http.Response, error) {
		attempt++
		trace := httptrace.ContextClientTrace(r.Context())
		trace.GotConn(httptrace.GotConnInfo{Conn: traceLocalFixtureConn{
			traceFixtureConn: traceFixtureConn{&net.TCPAddr{IP: net.IPv4(192, 0, 2, byte(attempt)), Port: 443}},
			local:            &net.TCPAddr{Port: 40000 + attempt},
		}})
		if attempt == 1 {
			trace.GotFirstResponseByte()
			return &http.Response{StatusCode: 503, Header: http.Header{}, Body: io.NopCloser(strings.NewReader("private-http-error-body"))}, nil
		}
		return nil, &url.Error{Op: "Post", URL: "https://private-url.invalid/secret?api_key=private-key", Err: syscall.ECONNRESET}
	})}
	perform(build(t, cfg), "/v1/responses", `{"model":"hy4-test"}`)
	logs := decodedAttemptLogs(t, buffer)
	if len(logs) != 1 || logs[0]["upstream_attempt"] != float64(2) || logs[0]["upstream_peer_ip"] != "192.0.2.2" || logs[0]["upstream_local_port"] != float64(40002) || logs[0]["upstream_first_response_byte_ms"] != float64(-1) || logs[0]["upstream_response_headers_ms"] != float64(-1) || logs[0]["upstream_response_body_bytes"] != float64(0) {
		t.Fatalf("retry reused prior observations: %+v", logs)
	}
	for _, secret := range []string{"private-http-error-body", "private-url", "private-key", "api_key"} {
		if strings.Contains(buffer.String(), secret) {
			t.Fatal("error string leaked")
		}
	}
}

type untrustedAddress string

func (a untrustedAddress) String() string { return string(a) }
func (untrustedAddress) Network() string  { return "tcp" }

func TestAttemptTraceLocalPortAllowsOnlyBoundedTCPPort(t *testing.T) {
	for _, fixture := range []struct {
		address net.Addr
		want    int
	}{
		{&net.TCPAddr{Port: 1}, 1},
		{&net.TCPAddr{Port: 65535}, 65535},
		{&net.TCPAddr{Port: 0}, 0},
		{&net.TCPAddr{Port: -1}, 0},
		{&net.TCPAddr{Port: 65536}, 0},
		{(*net.TCPAddr)(nil), 0},
		{nil, 0},
		{untrustedAddress("127.0.0.1:45123"), 45123},
		{untrustedAddress("[::1]:65535"), 65535},
		{untrustedAddress("private-secret:443"), 0},
		{untrustedAddress("127.0.0.1:secret-key"), 0},
		{untrustedAddress("127.0.0.1:443\nprivate-secret"), 0},
		{untrustedAddress("127.0.0.1:+443"), 0},
		{untrustedAddress("127.0.0.1:0"), 0},
		{untrustedAddress("127.0.0.1:65536"), 0},
		{untrustedAddress("127.0.0.1:000443"), 0},
		{untrustedAddress("127.0.0.1: 443"), 0},
		{untrustedAddress("127.0.0.1:443?key=private-secret"), 0},
		{untrustedAddress("[fe80::1%private-interface]:443"), 0},
		{untrustedAddress("private-secret" + strings.Repeat("x", 256)), 0},
	} {
		conn := traceLocalFixtureConn{local: fixture.address}
		if got := upstreamLocalPort(conn); got != fixture.want {
			t.Fatalf("local port classification: got %d want %d", got, fixture.want)
		}
		ctx, _ := newProviderAttemptTrace(context.Background(), 1)
		httptrace.ContextClientTrace(ctx).GotConn(httptrace.GotConnInfo{Conn: conn})
		fields := providerTraceFields(ctx)
		values := map[string]any{}
		for i := 0; i < len(fields); i += 2 {
			values[fields[i].(string)] = fields[i+1]
		}
		var want any = "unknown"
		if fixture.want != 0 {
			want = fixture.want
		}
		if values["upstream_local_port"] != want {
			t.Fatal("port diagnostic representation invalid")
		}
		raw, err := json.Marshal(values)
		if err != nil || strings.Contains(string(raw), "private-") || strings.Contains(string(raw), "secret-key") {
			t.Fatal("local address text exposed in trace")
		}
	}
	if upstreamLocalPort(nil) != 0 {
		t.Fatal("nil connection must be unknown")
	}
	ctx, _ := newProviderAttemptTrace(context.Background(), 1)
	fields := providerTraceFields(ctx)
	for i := 0; i < len(fields); i += 2 {
		if fields[i] == "upstream_local_port" && fields[i+1] != "unknown" {
			t.Fatal("unobserved local port fabricated")
		}
	}
}

func TestAttemptTraceCancellationAndUntrustedFieldsAreSanitized(t *testing.T) {
	buffer := captureAttemptLogs(t)
	ctx, cancel := context.WithCancel(context.Background())
	ctx, trace := newProviderAttemptTrace(ctx, 3)
	callbacks := httptrace.ContextClientTrace(ctx)
	callbacks.GotConn(httptrace.GotConnInfo{Conn: traceFixtureConn{untrustedAddress("private-host:secret-port")}})
	callbacks.TLSHandshakeDone(tls.ConnectionState{NegotiatedProtocol: "private-alpn"}, errors.New("private-handshake"))
	callbacks.WroteRequest(httptrace.WroteRequestInfo{Err: errors.New("private-write-error")})
	cancel()
	transportDiagnostic("request", context.Canceled, ctx, nil, Grant{RunID: "fixture", Attempt: 1})
	logs := decodedAttemptLogs(t, buffer)
	if len(logs) != 1 || logs[0]["level"] != "INFO" || logs[0]["class"] != "context_cancelled" || logs[0]["context_state"] != "cancelled" || logs[0]["upstream_peer_ip"] != "unknown" || logs[0]["upstream_alpn"] != "other" || logs[0]["upstream_request_write_class"] != "other_transport_error" {
		t.Fatalf("unsafe or incorrect cancellation trace: %+v", logs)
	}
	if strings.Contains(buffer.String(), "private-") {
		t.Fatal("untrusted callback value leaked")
	}
	if trace.attempt != 3 {
		t.Fatal("unexpected attempt identity")
	}
}

func TestAttemptTraceCancelledStreamLogsInfoWithoutChangingUsageClassification(t *testing.T) {
	buffer := captureAttemptLogs(t)
	cfg, records, _ := config(t, func(http.ResponseWriter, *http.Request) {})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	ctx, trace := newProviderAttemptTrace(ctx, 1)
	response := &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: cancelBeforeRead{cancel}}
	trace.response(response)
	r := httptest.NewRequest("POST", "/v1/responses", nil).WithContext(ctx)
	build(t, cfg).forwardStream(httptest.NewRecorder(), r, ctx, response, Grant{RunID: "fixture", Attempt: 1}, 1)
	logs := decodedAttemptLogs(t, buffer)
	if len(logs) != 1 || logs[0]["level"] != "INFO" || logs[0]["class"] != "context_cancelled" || logs[0]["upstream_attempt"] != float64(1) || logs[0]["upstream_response_body_bytes"] != float64(0) {
		t.Fatalf("missing cancellation observation: %+v", logs)
	}
	if len(*records) != 1 || (*records)[0].Status != 499 || (*records)[0].ErrorClass != "client_cancelled" {
		t.Fatal("cancellation ledger changed")
	}
}
