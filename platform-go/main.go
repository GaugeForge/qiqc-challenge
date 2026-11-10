// Command qiqc-platform-go serves the native Go Demo API and public website.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

type config struct {
	Listen     string
	Backend    string
	Adapters   string
	Native     *platform.Store
	Standalone bool
	Site       string
	Web        string
	Reference  string
}

func env(name, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return fallback
}

func routes(cfg config) (http.Handler, error) {
	if cfg.Standalone && cfg.Native == nil {
		return nil, errors.New("standalone requires native database")
	}
	target, err := url.Parse(cfg.Backend)
	if err != nil || target.Scheme == "" || target.Host == "" {
		return nil, errors.New("QIQC_BACKEND must be an absolute http(s) URL")
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		http.Error(w, `{"error":"platform backend unavailable"}`, http.StatusBadGateway)
	}
	proxy.ModifyResponse = func(resp *http.Response) error {
		resp.Header.Set("X-QIQC-Edge", "go")
		return nil
	}

	mux := http.NewServeMux()
	if cfg.Native != nil {
		cfg.Native.Register(mux)
		fallback := http.Handler(proxy)
		if cfg.Standalone {
			fallback = http.NotFoundHandler()
		}
		cfg.Native.RegisterBusiness(mux, fallback)
	}
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if cfg.Native != nil && cfg.Native.Check() != nil {
			http.Error(w, `{"status":"database_unavailable"}`, http.StatusServiceUnavailable)
			return
		}
		if cfg.Standalone {
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Cache-Control", "no-store")
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "edge": "go", "backend": "go-native"})
			return
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(cfg.Backend, "/")+"/healthz", nil)
		if err != nil {
			http.Error(w, `{"status":"error"}`, http.StatusBadGateway)
			return
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			http.Error(w, `{"status":"backend_unavailable"}`, http.StatusBadGateway)
			return
		}
		defer res.Body.Close()
		if res.StatusCode < 200 || res.StatusCode >= 300 {
			http.Error(w, `{"status":"backend_unhealthy"}`, http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-QIQC-Edge", "go")
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "edge": "go", "backend": cfg.Backend})
	})

	// This endpoint is intentionally static and contains no credentials.  It is
	// used by the temporary collaborator page to discover the three supported
	// adapter starter packages.
	mux.HandleFunc("/api/adapter-templates", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"schema": "qiqc-harness/v1",
			"templates": []map[string]string{
				{"id": "codex", "name": "Codex Harness Adapter", "download": "/adapters/codex/harness.zip"},
				{"id": "deepseek", "name": "DeepSeek Harness Adapter", "download": "/adapters/deepseek/harness.zip"},
				{"id": "pi", "name": "Pi Agent Harness Adapter", "download": "/adapters/pi/harness.zip"},
			},
		})
	})
	// Adapter ZIPs are static organizer artifacts. Keep the root configurable
	// and reject path traversal before serving anything.
	adapterRoot, _ := filepath.Abs(cfg.Adapters)
	mux.HandleFunc("/adapters/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		rel := strings.TrimPrefix(r.URL.Path, "/adapters/")
		if rel != "codex/harness.zip" && rel != "deepseek/harness.zip" && rel != "pi/harness.zip" {
			http.NotFound(w, r)
			return
		}
		path, err := filepath.Abs(filepath.Join(adapterRoot, rel))
		if err != nil || !strings.HasPrefix(path, adapterRoot+string(os.PathSeparator)) {
			http.NotFound(w, r)
			return
		}
		if filepath.Ext(path) != ".zip" {
			http.NotFound(w, r)
			return
		}
		resolved, e := filepath.EvalSymlinks(path)
		if e != nil || resolved != path {
			http.NotFound(w, r)
			return
		}
		servePublicFile(w, r, path)
	})

	// The frontend and every /api route use the same-origin Python API during
	// this migration.  Preserve cookies, request headers and response bodies.
	if cfg.Standalone {
		mux.Handle("/", publicFiles(cfg.Site, cfg.Web))
		mux.HandleFunc("/api/reference.zip", func(w http.ResponseWriter, r *http.Request) { servePublicFile(w, r, cfg.Reference) })
	} else {
		mux.Handle("/", proxy)
	}
	return securityHeaders(mux), nil
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "strict-origin-when-cross-origin")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
		next.ServeHTTP(w, r)
	})
}

func main() {
	clockHealth := flag.String("clock-health", "/run/qiqc-status/clock.json", "Trusted host clock health file; empty only for isolated acceptance")
	formalExecution := flag.String("formal-execution", "", "Private formal execution JSON; missing configuration keeps formal admission closed")
	eventCalendar := flag.String("event-calendar", "", "Trusted persistent event calendar JSON")
	cfg := config{}
	var data string
	var scanner string
	var scienceManifest string
	var scienceTaskSet string
	var scienceAgentBudget int
	var secureCookies bool
	flag.StringVar(&cfg.Listen, "listen", env("QIQC_GO_LISTEN", "127.0.0.1:8790"), "listen address")
	flag.StringVar(&cfg.Backend, "backend", env("QIQC_BACKEND", "http://127.0.0.1:8765"), "trusted local business API")
	flag.StringVar(&cfg.Adapters, "adapters", env("QIQC_ADAPTERS", "./adapters"), "static adapter archive directory")
	flag.StringVar(&data, "data", env("QIQC_GO_DATA", ""), "private data directory enabling native Go modules")
	flag.BoolVar(&secureCookies, "secure-cookies", false, "require HTTPS for session cookies")
	flag.StringVar(&scanner, "scanner", env("QIQC_SCANNER", "../platform/qiqc/archive.py"), "organizer-owned isolated archive parser")
	flag.BoolVar(&cfg.Standalone, "standalone", false, "serve all business routes without legacy API")
	flag.StringVar(&cfg.Site, "site", "../dist", "public website build directory")
	flag.StringVar(&cfg.Web, "web", "../platform/web", "public console directory")
	flag.StringVar(&cfg.Reference, "reference", "../adapters/reference.zip", "organizer mock reference archive")
	flag.StringVar(&scienceManifest, "science-manifest", "", "organizer pinned scientific template SHA manifest; empty disables science requests")
	flag.IntVar(&scienceAgentBudget, "science-agent-budget-seconds", 300, "trusted catalog acceptance budget: 180 or 300 seconds")
	flag.StringVar(&scienceTaskSet, "science-task-set", "", "trusted bounded scientific 49-task catalog; no per-submission admin binding")
	flag.Parse()
	if !platform.ValidScientificAgentBudget(scienceAgentBudget) {
		slog.Error("invalid scientific agent budget")
		os.Exit(2)
	}
	if data != "" {
		store, err := platform.Open(filepath.Join(data, "platform.db"))
		if err != nil {
			slog.Error("cannot open native platform database")
			os.Exit(2)
		}
		defer store.Close()
		if *eventCalendar != "" {
			if err := store.ConfigureCalendarFile(*eventCalendar); err != nil {
				slog.Error("invalid event calendar", "error", err)
				os.Exit(2)
			}
		}
		if err := store.ConfigureScientificAgentBudget(scienceAgentBudget); err != nil {
			slog.Error("invalid scientific agent budget")
			os.Exit(2)
		}
		objects, err := rollout.FromEnvironment(filepath.Join(data, "objects"))
		if err != nil {
			slog.Error("invalid rollout store configuration")
			os.Exit(2)
		}
		if scienceManifest != "" {
			bytes, err := os.ReadFile(scienceManifest)
			var entries map[string]struct {
				SHA string `json:"sha256"`
			}
			if err != nil || json.Unmarshal(bytes, &entries) != nil {
				slog.Error("invalid science manifest")
				os.Exit(2)
			}
			store.ScienceHashes = map[string]bool{}
			for _, entry := range entries {
				if len(entry.SHA) != 64 {
					os.Exit(2)
				}
				store.ScienceHashes[entry.SHA] = true
			}
		}
		if scienceTaskSet != "" {
			if err := store.ConfigureScientificTaskSetFile(scienceTaskSet); err != nil {
				slog.Error("invalid scientific task set configuration")
				os.Exit(2)
			}
		}
		store.Objects = objects
		if *formalExecution != "" {
			if err := store.ConfigureFormalExecutionFile(*formalExecution); err != nil {
				slog.Error("formal execution configuration unavailable")
				os.Exit(2)
			}
		}
		store.ClockHealthPath = *clockHealth
		store.Model = env("QIQC_MODEL", "tencent/hy4-preview")
		store.SecureCookies = secureCookies
		if assistant, err := platform.AssistantFromEnvironment(strings.Split(env("QIQC_ASSISTANT_KNOWLEDGE", ""), ":")); err != nil {
			slog.Error("invalid assistant configuration", "error", err)
			os.Exit(2)
		} else if assistant != nil {
			store.Assistant = assistant
		}
		store.BaselinePath = env("QIQC_BASELINE", "/root/QQ-Challenge/docs/baselines/gpt-6-astra-xhigh-normal-39-final/baseline.json")
		scannerPath, err := filepath.Abs(scanner)
		if err != nil {
			os.Exit(2)
		}
		if socket := os.Getenv("QIQC_SCANNER_SOCKET"); socket != "" {
			store.Scanner = platform.SocketScanner(socket)
		} else {
			store.Scanner = platform.DockerScanner(scannerPath, "qiqcbench-agent:latest")
		}
		cfg.Native = store
	}
	h, err := routes(cfg)
	if err != nil {
		slog.Error("invalid configuration", "error", err)
		os.Exit(2)
	}
	srv := &http.Server{Addr: cfg.Listen, Handler: h, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 35 * time.Second, WriteTimeout: 120 * time.Second, IdleTimeout: 60 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if cfg.Native != nil {
		go cfg.Native.RunAdmissionLoop(ctx)
		go cfg.Native.RunCompletionLoop(ctx)
	}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	slog.Info("QIQC Go edge listening", "listen", cfg.Listen, "backend", cfg.Backend)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server stopped", "error", err)
		os.Exit(1)
	}
}
