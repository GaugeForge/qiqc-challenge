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
	profilePath2 := flag.String("provider-profile-2", "", "optional second provider profile for team-stable key routing")
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
	backends := []modelproxy.Backend{}
	var secondaryProfile *providerprofile.Profile
	if *profilePath != "" {
		var key string
		var e error
		profile, _, key, e = providerprofile.Load(*profilePath)
		if e != nil || (*diagnostic != "" && (profile.Model != *diagnostic || profile.Billing != "free")) {
			slog.Error("provider profile unavailable or diagnostic policy mismatch")
			os.Exit(2)
		}
		values["HY_URL"], values["HY_API"] = profile.BaseURL, key
		backends = append(backends, modelproxy.Backend{ID: "a", BaseURL: profile.BaseURL, MasterKey: key})
		if *profilePath2 != "" {
			var secondaryKey string
			secondary, _, secondaryKey, err := providerprofile.Load(*profilePath2)
			if err != nil || secondary.Model != profile.Model || secondary.Billing != profile.Billing || strings.Join(secondary.Endpoints, ",") != strings.Join(profile.Endpoints, ",") || (*diagnostic != "" && secondary.Billing != "free") {
				slog.Error("second provider profile is incompatible")
				os.Exit(2)
			}
			secondaryProfile = &secondary
			backends = append(backends, modelproxy.Backend{ID: "b", BaseURL: secondary.BaseURL, MasterKey: secondaryKey})
		} else if *env != "" {
			// A profile may be paired with one secondary env credential. This is
			// the HY_API_2/HY_URL_2 route used to spread team load without
			// changing the formal profile identity.
			if secondary, e := readProviderEnv(*env); e == nil && secondary["HY_API_2"] != "" && secondary["HY_URL_2"] != "" {
				backends = append(backends, modelproxy.Backend{ID: "b", BaseURL: secondary["HY_URL_2"], MasterKey: secondary["HY_API_2"]})
			}
		}
		// When the private env file carries both HY_API/HY_URL and
		// HY_API_2/HY_URL_2, those are the two organizer keys the operator asked
		// to mix. The profile still controls model, endpoints and quota policy.
		if *env != "" {
			if envValues, e := readProviderEnv(*env); e == nil && envValues["HY_URL"] != "" && envValues["HY_API"] != "" && envValues["HY_URL_2"] != "" && envValues["HY_API_2"] != "" {
				backends = []modelproxy.Backend{
					{ID: "a", BaseURL: envValues["HY_URL"], MasterKey: envValues["HY_API"]},
					{ID: "b", BaseURL: envValues["HY_URL_2"], MasterKey: envValues["HY_API_2"]},
				}
				secondaryProfile = nil
			}
		}
	} else {
		var e error
		values, e = readProviderEnv(*env)
		if e != nil {
			slog.Error("provider file must be private 0600")
			os.Exit(2)
		}
		// Keep the primary pair authoritative; the suffixed pair is only a
		// fallback for the frozen production file naming.
		primaryURL, primaryKey := values["HY_URL"], values["HY_API"]
		if primaryURL == "" || primaryKey == "" {
			primaryURL, primaryKey = values["HY_URL_2"], values["HY_API_2"]
		}
		if primaryURL == "" || primaryKey == "" {
			slog.Error("provider config missing primary credential")
			os.Exit(2)
		}
		backends = append(backends, modelproxy.Backend{ID: "a", BaseURL: primaryURL, MasterKey: primaryKey})
		if values["HY_URL_2"] != "" && values["HY_API_2"] != "" && (values["HY_URL_2"] != primaryURL || values["HY_API_2"] != primaryKey) {
			backends = append(backends, modelproxy.Backend{ID: "b", BaseURL: values["HY_URL_2"], MasterKey: values["HY_API_2"]})
		}
	}
	if len(backends) == 0 {
		slog.Error("no provider backend configured")
		os.Exit(2)
	}
	slog.Info("provider backends configured", "count", len(backends))
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
	cfg := modelproxy.Config{Backends: backends, Authorize: authorize, Reserve: reserve, RecordUsage: store.RecordModelUsage, RequestTimeout: *requestTimeout, MaxConcurrent: *maxConcurrent, UpstreamIPFamily: *ipFamily}
	if *profilePath == "" {
		if model := strings.TrimSpace(values["HY_MODEL"]); model != "" {
			cfg.RequiredModel = model
		}
	}
	if *profilePath != "" {
		cfg.RequiredModel = profile.Model
		cfg.AllowedEndpoints = profile.Endpoints
		cfg.PublicUpstream = true
		type quota struct {
			group string
			rpm   int
			daily int
		}
		quotas := map[string]quota{"a": {profile.QuotaGroup, profile.RPM, profile.Daily}}
		if secondaryProfile != nil {
			quotas["b"] = quota{secondaryProfile.QuotaGroup, secondaryProfile.RPM, secondaryProfile.Daily}
		} else if len(backends) > 1 {
			quotas["b"] = quota{profile.QuotaGroup + "-b", profile.RPM, profile.Daily}
		}
		cfg.Reserve = func(ctx context.Context, g modelproxy.Grant) error {
			selected, ok := quotas[g.RouteGroup]
			if !ok || len(backends) == 1 {
				selected = quotas["a"]
			}
			if e := diagnosticmodel.Wait(ctx, store.DB, selected.group, selected.rpm, selected.daily); e != nil {
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

// readProviderEnv accepts only a private owner-readable file. A second
// HY_URL_2/HY_API_2 pair is optional and is used for team-stable key routing.
func readProviderEnv(path string) (map[string]string, error) {
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return nil, errors.New("provider file must be private 0600")
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	values := map[string]string{}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(strings.TrimPrefix(line, "export "), "=")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		if _, exists := values[k]; exists {
			return nil, errors.New("duplicate provider key")
		}
		values[k] = strings.Trim(strings.TrimSpace(v), "\"'")
	}
	return values, nil
}
