package modelproxy

// These diagnostics intentionally contain no error strings, URLs, headers,
// model inputs, response text, or capability hashes.
import (
	"bufio"
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"reflect"
	"syscall"
)

func diagnosticTokenCount(value *int64) any {
	if value == nil {
		return "unknown"
	}
	return *value
}

func transportClass(err error) string {
	if err == nil {
		return "eof_before_terminal"
	}
	switch {
	case errors.Is(err, context.Canceled):
		return "context_cancelled"
	case errors.Is(err, context.DeadlineExceeded):
		return "context_deadline"
	case errors.Is(err, io.ErrUnexpectedEOF):
		return "unexpected_eof"
	case errors.Is(err, io.EOF):
		return "eof_before_terminal"
	case errors.Is(err, syscall.ECONNRESET):
		return "connection_reset"
	case errors.Is(err, syscall.EPIPE):
		return "broken_pipe"
	case errors.Is(err, net.ErrClosed):
		return "connection_closed"
	case errors.Is(err, bufio.ErrTooLong):
		return "scanner_line_limit"
	}
	var n net.Error
	if errors.As(err, &n) && n.Timeout() {
		return "transport_timeout"
	}
	// net/http embeds its HTTP/2 implementation. Match concrete type names only;
	// never call Error(), which can contain a remote URL or server-supplied text.
	for current, depth := err, 0; current != nil && depth < 8; current, depth = errors.Unwrap(current), depth+1 {
		t := reflect.TypeOf(current)
		if t.Kind() == reflect.Pointer {
			t = t.Elem()
		}
		switch t.Name() {
		case "http2StreamError", "StreamError":
			return "http2_stream_error"
		case "http2ConnectionError", "ConnectionError":
			return "http2_connection_error"
		case "http2GoAwayError", "GoAwayError":
			return "http2_goaway"
		}
	}
	return "other_transport_error"
}

func transportDiagnostic(stage string, err error, ctx context.Context, response *http.Response, grant Grant) {
	protocol := "no_response"
	if response != nil {
		switch response.ProtoMajor {
		case 1:
			protocol = "http1"
		case 2:
			protocol = "http2"
		}
	}
	contextState := "active"
	if errors.Is(ctx.Err(), context.Canceled) {
		contextState = "cancelled"
	}
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		contextState = "deadline_exceeded"
	}
	fields := []any{"stage", stage, "class", transportClass(err),
		"protocol", protocol, "context_state", contextState,
		"request_id", grant.RequestID, "request_correlation", grant.RequestCorrelation,
		"run_id", grant.RunID, "run_attempt", grant.Attempt}
	fields = append(fields, providerTraceFields(ctx)...)
	if contextState == "cancelled" || errors.Is(err, context.Canceled) {
		// Trusted budget stops routinely cancel requests. Keep their correlation
		// data without promoting expected shutdowns to transport warnings.
		slog.Info("model_transport_diagnostic", fields...)
		return
	}
	slog.Warn("model_transport_diagnostic", fields...)
}
