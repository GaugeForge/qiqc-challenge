package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestGoEdgeProxiesBrowserAndAPIRequests(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, `{"status":"ok"}`)
			return
		}
		if r.Method == http.MethodPost && r.Header.Get("X-QIQC-Request") != "1" {
			t.Errorf("proxy dropped request protection header")
		}
		w.Header().Set("Set-Cookie", "qiqc_session=test; Path=/; HttpOnly")
		_, _ = io.WriteString(w, "proxied "+r.URL.Path)
	}))
	defer backend.Close()
	got, err := routes(config{Backend: backend.URL})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(got)
	defer server.Close()
	for _, path := range []string{"/", "/submit", "/api/me"} {
		req, _ := http.NewRequest(http.MethodGet, server.URL+path, nil)
		req.Header.Set("Cookie", "qiqc_session=abc")
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(res.Body)
		res.Body.Close()
		if res.StatusCode != http.StatusOK || !strings.Contains(string(body), "proxied "+path) {
			t.Fatalf("path %s: status=%d body=%q", path, res.StatusCode, body)
		}
		if res.Header.Get("X-QIQC-Edge") != "go" {
			t.Fatalf("path %s missing edge marker", path)
		}
	}
	// Health is checked against the backend rather than merely reporting that
	// the edge process itself is alive.
	res, err := http.Get(server.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		t.Fatalf("health status=%d", res.StatusCode)
	}
}

func TestAdapterTemplateCatalogDoesNotExposeSecrets(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "codex"), 0700); err != nil { t.Fatal(err) }
	if err := os.WriteFile(filepath.Join(dir, "codex", "harness.zip"), []byte("zip"), 0600); err != nil { t.Fatal(err) }
	h, err := routes(config{Backend: "http://127.0.0.1:1", Adapters: dir})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodGet, "/api/adapter-templates", nil)
	res := httptest.NewRecorder()
	h.ServeHTTP(res, req)
	if res.Code != http.StatusOK || strings.Contains(res.Body.String(), "key") || strings.Contains(res.Body.String(), "token") {
		t.Fatalf("unexpected catalog response: %d %s", res.Code, res.Body.String())
	}
	zipReq := httptest.NewRequest(http.MethodGet, "/adapters/codex/harness.zip", nil)
	zipRes := httptest.NewRecorder()
	h.ServeHTTP(zipRes, zipReq)
	if zipRes.Code != http.StatusOK || zipRes.Body.String() != "zip" {
		t.Fatalf("adapter archive status=%d body=%q", zipRes.Code, zipRes.Body.String())
	}
	traversal := httptest.NewRequest(http.MethodGet, "/adapters/../main.go", nil)
	traversalRes := httptest.NewRecorder()
	h.ServeHTTP(traversalRes, traversal)
	if traversalRes.Code == http.StatusOK { t.Fatal("adapter path traversal was served") }
}
