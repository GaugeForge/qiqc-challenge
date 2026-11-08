package main

import (
	"context"
	"errors"
	"flag"
	"github.com/gaugeforge/qiqc-platform-go/internal/diagnosticmodel"
	"github.com/gaugeforge/qiqc-platform-go/internal/modelproxy"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"github.com/gaugeforge/qiqc-platform-go/internal/providerprofile"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func main() {
	data := flag.String("data", os.Getenv("QIQC_DATA"), "private data directory")
	env := flag.String("provider-env", os.Getenv("QIQC_PROJECT_ENV"), "private Hy4 credentials file")
	listen := flag.String("listen", "127.0.0.1:8789", "loopback listen address")
	maxConcurrent := flag.Int("max-concurrent", 2, "bounded concurrent model requests (1..100)")
	requestTimeout := flag.Duration("request-timeout", 120*time.Second, "bounded total model request deadline (30s..10m)")
	diagnostic := flag.String("diagnostic-model", "", "explicit free-only infrastructure model; empty retains existing policy")
	rpm := flag.Int("diagnostic-rpm", 10, "shared diagnostic starts per rolling minute (1..20)")
	daily := flag.Int("diagnostic-daily-cap", 50, "shared conservative daily request cap (1..1000)")
	profilePath := flag.String("provider-profile", "", "private organizer provider profile with key reference")
	ipFamily := flag.String("upstream-ip-family", os.Getenv("QIQC_MODEL_UPSTREAM_IP_FAMILY"), "upstream IP family: auto (default), ipv4, ipv6")
	flag.Parse()
	if _, err := modelproxy.UpstreamNetwork(*ipFamily); err != nil {
		slog.Error("invalid upstream IP family")
		os.Exit(2)
	}
	if *diagnostic != "" && diagnosticmodel.Validate(*diagnostic, *rpm, *daily) != nil {
		slog.Error("invalid free diagnostic profile")
		os.Exit(2)
	}
	if *maxConcurrent < 1 || *maxConcurrent > 100 {
		slog.Error("invalid model concurrency")
		os.Exit(2)
	}
	if *requestTimeout < 30*time.Second || *requestTimeout > 10*time.Minute {
		slog.Error("invalid request deadline")
		os.Exit(2)
	}
	if *data == "" || (*env == "" && *profilePath == "") {
		slog.Error("data and provider config required")
		os.Exit(2)
	}
	values := map[string]string{}
	var profile providerprofile.Profile
	if *profilePath != "" {
		var key string
		var e error
		profile, _, key, e = providerprofile.Load(*profilePath)
		if e != nil || (*diagnostic != "" && (profile.Model != *diagnostic || profile.Billing != "free")) {
			slog.Error("provider profile unavailable or diagnostic policy mismatch")
			os.Exit(2)
		}
		values["HY_URL"], values["HY_API"] = profile.BaseURL, key
	} else {
		info, err := os.Stat(*env)
		if err != nil || info.Mode().Perm()&0077 != 0 {
			slog.Error("provider file must be private 0600")
			os.Exit(2)
		}
		b, err := os.ReadFile(*env)
		if err != nil {
			slog.Error("provider config unavailable")
			os.Exit(2)
		}

		for _, line := range strings.Split(string(b), "\n") {
			line = strings.TrimSpace(line)
			if strings.HasPrefix(line, "#") {
				continue
			}
			k, v, ok := strings.Cut(strings.TrimPrefix(line, "export "), "=")
			if ok {
				values[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), "\"'")
			}
		}
		// The production VPS keeps the frozen provider under the suffixed names
		// so it cannot be confused with an old staging credential. Normalize only
		// inside this private process; no secret is exposed through the API.
		if values["HY_API"] == "" {
			values["HY_API"] = values["HY_API_2"]
		}
		if values["HY_URL"] == "" {
			values["HY_URL"] = values["HY_URL_2"]
		}

	}
	store, err := platform.Open(filepath.Join(*data, "platform.db"))
	if err != nil {
		slog.Error("database unavailable")
		os.Exit(2)
	}
	defer store.Close()
	authorize := store.AuthorizeModel
	reserve := store.ReserveModelRequest
	if *diagnostic != "" {
		authorize = func(ctx context.Context, token, model string) (modelproxy.Grant, error) {
			if model != *diagnostic {
				return modelproxy.Grant{}, &modelproxy.HTTPError{Status: 403, Message: "diagnostic model required"}
			}
			return store.AuthorizeModel(ctx, token, model)
		}
		reserve = func(ctx context.Context, g modelproxy.Grant) error {
			if err := diagnosticmodel.Wait(ctx, store.DB, "openrouter-free", *rpm, *daily); err != nil {
				if errors.Is(err, diagnosticmodel.ErrLimited) {
					return &modelproxy.HTTPError{Status: 429, Message: "diagnostic budget exhausted"}
				}
				return err
			}
			return store.ReserveModelRequest(ctx, g)
		}
	}
	cfg := modelproxy.Config{BaseURL: values["HY_URL"], MasterKey: values["HY_API"], Authorize: authorize, Reserve: reserve, RecordUsage: store.RecordModelUsage, RequestTimeout: *requestTimeout, MaxConcurrent: *maxConcurrent, UpstreamIPFamily: *ipFamily}
	if *profilePath != "" {
		cfg.RequiredModel = profile.Model
		cfg.AllowedEndpoints = profile.Endpoints
		cfg.PublicUpstream = true
		cfg.Reserve = func(ctx context.Context, g modelproxy.Grant) error {
			if e := diagnosticmodel.Wait(ctx, store.DB, profile.QuotaGroup, profile.RPM, profile.Daily); e != nil {
				if errors.Is(e, diagnosticmodel.ErrLimited) {
					return &modelproxy.HTTPError{Status: 429, Message: "provider daily budget exhausted"}
				}
				return e
			}
			return store.ReserveModelRequest(ctx, g)
		}
	}
	proxy, err := modelproxy.New(cfg)
	if err != nil {
		slog.Error("provider config invalid")
		os.Exit(2)
	}
	server := &http.Server{Addr: *listen, Handler: proxy, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 35 * time.Second, WriteTimeout: *requestTimeout + 45*time.Second, IdleTimeout: 60 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		// Allow the independent 35-second usage accounting window to drain.
		shutdown, cancel := context.WithTimeout(context.Background(), 45*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	if err = server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		slog.Error("model gateway stopped")
		os.Exit(1)
	}
}
