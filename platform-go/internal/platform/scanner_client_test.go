package platform

import (
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
)

func TestSocketScannerAcceptsOnlyReviewedBytesAndNoCommandProtocol(t *testing.T) {
	for _, match := range []bool{true, false} {
		t.Run(map[bool]string{true: "matching", false: "mismatch"}[match], func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "incoming-123.zip")
			data := []byte("synthetic")
			os.WriteFile(path, data, 0600)
			socket := filepath.Join(dir, "s")
			listener, err := net.Listen("unix", socket)
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			done := make(chan struct{})
			go func() {
				defer close(done)
				conn, err := listener.Accept()
				if err != nil {
					return
				}
				defer conn.Close()
				var req map[string]string
				json.NewDecoder(conn).Decode(&req)
				if len(req) != 1 || req["filename"] != "incoming-123.zip" {
					t.Error("unexpected root helper request", req)
				}
				sha := shaBytes(data)
				if !match {
					sha = "wrong"
				}
				json.NewEncoder(conn).Encode(map[string]any{"ok": true, "source_sha256": sha, "report": Report{Files: 1, Manifest: Manifest{Schema: "qiqc-harness/v1-demo"}, RulesetVersion: "qiqc-archive/2", ParserSHA256: shaBytes([]byte("parser")), ScannerImage: "sha256:" + shaBytes([]byte("image"))}})
			}()
			report, err := SocketScanner(socket)(path)
			<-done
			if match && (err != nil || report.Files != 1) {
				t.Fatal(report, err)
			}
			if !match && status(err) != 503 {
				t.Fatal("mismatched scanner byte identity accepted")
			}
		})
	}
}

func TestSocketScannerSeparatesInvalidArchiveFromUnavailableService(t *testing.T) {
	generic := "Submission package failed format checks. Check the ZIP layout and harness.json."
	for _, tc := range []struct {
		name, body string
		want       int
		message    string
	}{
		{"invalid", `{"ok":false,"error_kind":"invalid_package"}`, 400, generic},
		{"manifest", `{"ok":false,"error_kind":"invalid_package","error":"harness.json: \"runtime\" is required and must be one of codex, deepseek or pi"}`, 400, `harness.json: "runtime" is required and must be one of codex, deepseek or pi`},
		{"unsafe-message", `{"ok":false,"error_kind":"invalid_package","error":"harness.json: <b>x</b>"}`, 400, generic},
		{"unavailable", `{"ok":false,"error_kind":"unavailable"}`, 503, ""},
		{"legacy-or-invalid", `{"ok":false}`, 503, ""},
		{"malformed", `not-json`, 503, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "incoming-123.zip")
			os.WriteFile(path, []byte("untrusted"), 0600)
			socket := filepath.Join(dir, "s")
			listener, err := net.Listen("unix", socket)
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			done := make(chan struct{})
			go func() {
				defer close(done)
				c, e := listener.Accept()
				if e != nil {
					return
				}
				defer c.Close()
				var request map[string]string
				json.NewDecoder(c).Decode(&request)
				c.Write([]byte(tc.body))
			}()
			_, err = SocketScanner(socket)(path)
			<-done
			if status(err) != tc.want {
				t.Fatalf("status=%d want=%d", status(err), tc.want)
			}
			if tc.message != "" && err.Error() != tc.message {
				t.Fatalf("message=%q want=%q", err.Error(), tc.message)
			}
		})
	}
}
