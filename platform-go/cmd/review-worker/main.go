// review-worker reads frozen scanner reports and writes only advisory evidence.
// No Harness archive, Docker socket, E2B credential or model gateway is required.
package main

import (
	"context"
	"flag"
	"fmt"
	"github.com/gaugeforge/qiqc-platform-go/internal/diagnosticmodel"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"github.com/gaugeforge/qiqc-platform-go/internal/providerprofile"
	"github.com/gaugeforge/qiqc-platform-go/internal/review"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func main() {
	db := flag.String("db", "", "existing platform database")
	mode := flag.String("provider", "disabled", "disabled, fake, or explicit https")
	keyfile := flag.String("key-file", "", "HTTPS reviewer-only key file; never a platform .env")
	model := flag.String("model", os.Getenv("QIQC_MODEL"), "organizer-frozen reviewer model")
	endpoint := flag.String("url", os.Getenv("QIQC_MODEL_URL"), "organizer-frozen reviewer endpoint")
	timeout := flag.Duration("timeout", 30*time.Second, "provider timeout, 5s to 120s")
	once := flag.Bool("once", false, "process at most one version")
	diagnostic := flag.String("diagnostic-model", "", "explicit free-only infrastructure reviewer")
	rpm := flag.Int("diagnostic-rpm", 10, "shared diagnostic rolling minute limit (1..20)")
	daily := flag.Int("diagnostic-daily-cap", 50, "shared diagnostic daily limit (1..1000)")
	profilePath := flag.String("provider-profile", "", "private organizer provider profile")
	flag.Parse()
	if *model == "" {
		*model = "tencent/hy4-preview"
	}
	if *endpoint == "" {
		*endpoint = "https://openrouter.ai/api/v1/chat/completions"
	}
	if *profilePath != "" && *mode != "https" {
		fmt.Fprintln(os.Stderr, "profile requires HTTPS provider")
		os.Exit(2)
	}
	if *diagnostic != "" && (*mode != "https" || diagnosticmodel.Validate(*diagnostic, *rpm, *daily) != nil) {
		fmt.Fprintln(os.Stderr, "invalid free diagnostic profile")
		os.Exit(2)
	}
	var diagnosticKey string
	if *timeout < 5*time.Second || *timeout > 120*time.Second {
		fmt.Fprintln(os.Stderr, "review timeout must be 5s..120s")
		os.Exit(2)
	}
	if *db == "" {
		fmt.Fprintln(os.Stderr, "--db is required")
		os.Exit(2)
	}
	var provider review.Provider
	switch *mode {
	case "disabled":
	case "fake":
		provider = review.FakeProvider{}
	case "https":
		if *profilePath != "" {
			break
		}
		info, e := os.Lstat(*keyfile)
		if e != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() > 1024 {
			fmt.Fprintln(os.Stderr, "reviewer key file must be a private regular file")
			os.Exit(2)
		}
		key, e := os.ReadFile(*keyfile)
		if e != nil {
			fmt.Fprintln(os.Stderr, "reviewer key unreadable")
			os.Exit(2)
		}
		if *diagnostic != "" {
			diagnosticKey = strings.TrimSpace(string(key))
		} else {
			provider, e = review.NewHTTPSProviderAt(strings.TrimSpace(string(key)), *model, *endpoint, nil)
		}
		if e != nil {
			fmt.Fprintln(os.Stderr, "invalid reviewer configuration")
			os.Exit(2)
		}
	default:
		fmt.Fprintln(os.Stderr, "unsupported provider")
		os.Exit(2)
	}
	if info, err := os.Lstat(*db); err != nil || !info.Mode().IsRegular() {
		fmt.Fprintln(os.Stderr, "existing regular platform database required")
		os.Exit(2)
	}
	store, err := platform.Open(*db)
	if err != nil {
		fmt.Fprintln(os.Stderr, "review database unavailable")
		os.Exit(1)
	}
	defer store.DB.Close()
	if *profilePath != "" {
		p, id, key, e := providerprofile.Load(*profilePath)
		if e != nil || (*diagnostic != "" && (p.Model != *diagnostic || p.Billing != "free")) {
			fmt.Fprintln(os.Stderr, "provider profile unavailable or diagnostic policy mismatch")
			os.Exit(2)
		}
		provider, err = review.NewProfileProvider(key, p, id, nil, func(ctx context.Context) error {
			return diagnosticmodel.Wait(ctx, store.DB, p.QuotaGroup, p.RPM, p.Daily)
		})
		if err != nil {
			fmt.Fprintln(os.Stderr, "review profile unsupported")
			os.Exit(2)
		}
	} else if *diagnostic != "" {
		provider, err = review.NewDiagnosticProvider(diagnosticKey, *diagnostic, nil, func(ctx context.Context) error {
			return diagnosticmodel.Wait(ctx, store.DB, "openrouter-free", *rpm, *daily)
		})
		if err != nil {
			fmt.Fprintln(os.Stderr, "invalid diagnostic reviewer")
			os.Exit(2)
		}
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	worker := review.NewWorker(provider, *timeout)
	for {
		worked, err := store.ProcessReviewOnce(ctx, worker, "review-worker")
		if err != nil {
			fmt.Fprintln(os.Stderr, "review attempt not committed; retry after lease expiry")
		}
		if *once {
			if err != nil {
				os.Exit(1)
			}
			return
		}
		if worked && err == nil {
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}
