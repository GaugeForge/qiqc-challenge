package modelproxy

import (
	"context"
	"crypto/tls"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"strconv"
	"sync"
	"time"
)

type providerTraceKey struct{}

// Each provider invocation owns one trace. Trace callbacks may run concurrently
// with body reads or failure logging. Never retain URLs, headers or body data.
type providerAttemptTrace struct {
	mu                                     sync.Mutex
	attempt                                int
	started, wrote, firstResponse, headers time.Time
	firstBody, lastBody                    time.Time
	peerIP, alpn, writeClass               string
	localPort                              int
	cfRay, generationID, requestID         string
	gotConnection, reused                  bool
	bodyBytes                              int64
}

func safeALPN(protocol string) string {
	switch protocol {
	case "h2", "http/1.1":
		return protocol
	case "":
		return "none"
	default:
		return "other"
	}
}

func peerIP(connection net.Conn) string {
	if connection == nil || connection.RemoteAddr() == nil {
		return "unknown"
	}
	if address, ok := connection.RemoteAddr().(*net.TCPAddr); ok {
		if address.IP.To16() != nil {
			return address.IP.String()
		}
		return "unknown"
	}
	host, _, err := net.SplitHostPort(connection.RemoteAddr().String())
	if err != nil {
		return "unknown"
	}
	if ip := net.ParseIP(host); ip != nil {
		return ip.String()
	}
	return "unknown"
}

// Keep only the local TCP port needed to correlate a reset with a request.
// Never retain an arbitrary Addr.String(), local hostname or interface zone.
func upstreamLocalPort(connection net.Conn) int {
	if connection == nil {
		return 0
	}
	address := connection.LocalAddr()
	if address == nil {
		return 0
	}
	if tcp, ok := address.(*net.TCPAddr); ok {
		if tcp != nil && tcp.Port >= 1 && tcp.Port <= 65535 {
			return tcp.Port
		}
		return 0
	}
	raw := address.String()
	if len(raw) > 128 {
		return 0
	}
	host, port, err := net.SplitHostPort(raw)
	if err != nil || net.ParseIP(host) == nil || len(port) == 0 || len(port) > 5 {
		return 0
	}
	for _, char := range port {
		if char < '0' || char > '9' {
			return 0
		}
	}
	value, err := strconv.ParseUint(port, 10, 16)
	if err != nil || value == 0 {
		return 0
	}
	return int(value)
}

func newProviderAttemptTrace(ctx context.Context, attempt int) (context.Context, *providerAttemptTrace) {
	trace := &providerAttemptTrace{attempt: attempt, started: time.Now(), peerIP: "unknown", alpn: "unknown", writeClass: "not_observed"}
	ctx = context.WithValue(ctx, providerTraceKey{}, trace)
	callbacks := &httptrace.ClientTrace{
		GotConn: func(info httptrace.GotConnInfo) {
			trace.mu.Lock()
			defer trace.mu.Unlock()
			trace.peerIP, trace.reused, trace.gotConnection = peerIP(info.Conn), info.Reused, true
			trace.localPort = upstreamLocalPort(info.Conn)
			if connection, ok := info.Conn.(interface{ ConnectionState() tls.ConnectionState }); ok {
				trace.alpn = safeALPN(connection.ConnectionState().NegotiatedProtocol)
			} else if trace.alpn == "unknown" {
				trace.alpn = "none"
			}
		},
		TLSHandshakeDone: func(state tls.ConnectionState, _ error) {
			trace.mu.Lock()
			defer trace.mu.Unlock()
			trace.alpn = safeALPN(state.NegotiatedProtocol)
		},
		WroteRequest: func(info httptrace.WroteRequestInfo) {
			trace.mu.Lock()
			defer trace.mu.Unlock()
			trace.wrote, trace.writeClass = time.Now(), "ok"
			if info.Err != nil {
				trace.writeClass = transportClass(info.Err)
			}
		},
		GotFirstResponseByte: func() {
			trace.mu.Lock()
			defer trace.mu.Unlock()
			if trace.firstResponse.IsZero() {
				trace.firstResponse = time.Now()
			}
		},
	}
	return httptrace.WithClientTrace(ctx, callbacks), trace
}

func (t *providerAttemptTrace) response(response *http.Response) {
	if response == nil {
		return
	}
	t.mu.Lock()
	t.headers = time.Now()
	t.cfRay = safeProviderIdentifier(response.Header, "Cf-Ray")
	t.generationID = safeProviderIdentifier(response.Header, "X-Generation-Id")
	t.requestID = safeProviderIdentifier(response.Header, "X-Request-Id")
	t.mu.Unlock()
	if response.Body != nil {
		response.Body = &tracedProviderBody{ReadCloser: response.Body, trace: t}
	}
}

func safeProviderIdentifier(headers http.Header, name string) string {
	values := headers.Values(name)
	if len(values) != 1 || len(values[0]) == 0 || len(values[0]) > 128 {
		return ""
	}
	for _, c := range []byte(values[0]) {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '_') {
			return ""
		}
	}
	return values[0]
}

type tracedProviderBody struct {
	io.ReadCloser
	trace *providerAttemptTrace
}

func (b *tracedProviderBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if n > 0 {
		now := time.Now()
		b.trace.mu.Lock()
		if b.trace.firstBody.IsZero() {
			b.trace.firstBody = now
		}
		b.trace.lastBody = now
		b.trace.bodyBytes += int64(n)
		b.trace.mu.Unlock()
	}
	return n, err
}

func elapsedMilliseconds(start, end time.Time) int64 {
	if start.IsZero() || end.IsZero() {
		return -1 // Not observed; never fabricate a zero-duration observation.
	}
	return end.Sub(start).Milliseconds()
}

func providerTraceFields(ctx context.Context) []any {
	t, ok := ctx.Value(providerTraceKey{}).(*providerAttemptTrace)
	if !ok {
		return nil
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	now := time.Now()
	var localPort any = "unknown"
	if t.localPort != 0 {
		localPort = t.localPort
	}
	fields := []any{
		"upstream_attempt", t.attempt,
		"upstream_started_at", t.started.UTC().Format(time.RFC3339Nano),
		"upstream_elapsed_ms", elapsedMilliseconds(t.started, now),
		"upstream_peer_ip", t.peerIP,
		"upstream_local_port", localPort,
		"upstream_alpn", t.alpn,
		"upstream_connection_observed", t.gotConnection,
		"upstream_connection_reused", t.reused,
		"upstream_request_written_ms", elapsedMilliseconds(t.started, t.wrote),
		"upstream_request_write_class", t.writeClass,
		"upstream_first_response_byte_ms", elapsedMilliseconds(t.started, t.firstResponse),
		"upstream_response_headers_ms", elapsedMilliseconds(t.started, t.headers),
		"upstream_first_body_byte_ms", elapsedMilliseconds(t.started, t.firstBody),
		"upstream_last_body_byte_ms", elapsedMilliseconds(t.started, t.lastBody),
		"upstream_body_elapsed_ms", elapsedMilliseconds(t.firstBody, t.lastBody),
		"upstream_body_idle_ms", elapsedMilliseconds(t.lastBody, now),
		"upstream_response_body_bytes", t.bodyBytes,
	}
	// Only fixed identifier headers reach trusted administrator diagnostics.
	// Do not copy them into participant responses or enumerate other headers.
	for _, identifier := range []struct{ key, value string }{
		{"upstream_cf_ray", t.cfRay},
		{"upstream_generation_id", t.generationID},
		{"upstream_provider_request_id", t.requestID},
	} {
		if identifier.value != "" {
			fields = append(fields, identifier.key, identifier.value)
		}
	}
	return fields
}
