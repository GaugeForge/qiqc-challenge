package main

import (
	"context"
	"flag"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
	"github.com/gaugeforge/qiqc-platform-go/internal/scienceexec"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
)

func main() {
	data := flag.String("data", os.Getenv("QIQC_DATA"), "private data directory")
	slots := flag.Int("slots", 4, "global task limit")
	scienceRoot := flag.String("science-project", os.Getenv("QIQC_SCIENCE_PROJECT"), "enable official scientific-smoke controller under this trusted project root")
	sciencePython := flag.String("science-python", os.Getenv("QIQC_SCIENCE_PYTHON"), "trusted Python interpreter for E2B SDK bridge")
	gateway := flag.String("model-gateway", "http://127.0.0.1:8789", "trusted loopback Go model gateway")
	bridge := flag.String("science-bridge", "", "absolute organizer candidate controller; default official bridge")
	diagnosticModel := flag.String("diagnostic-model", "", "explicit free native infrastructure diagnostic model; no paid fallback")
	providerProfile := flag.String("provider-profile", "", "organizer-owned approved provider profile JSON")
	flag.Parse()
	if *data == "" {
		slog.Error("data directory required")
		os.Exit(2)
	}
	s, err := platform.Open(filepath.Join(*data, "platform.db"))
	if err != nil {
		slog.Error("database unavailable")
		os.Exit(2)
	}
	defer s.Close()
	s.Objects, err = rollout.FromEnvironment(filepath.Join(*data, "objects"))
	if err != nil {
		slog.Error("rollout configuration unavailable")
		os.Exit(2)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	var realExecutor func(context.Context, platform.Lease) error
	if *scienceRoot != "" {
		executor, e := scienceexec.New(s, scienceexec.Config{ProjectRoot: *scienceRoot, Python: *sciencePython, GatewayURL: *gateway, Bridge: *bridge, DiagnosticModel: *diagnosticModel, ProviderProfile: *providerProfile})
		if e != nil {
			slog.Error("scientific controller configuration invalid")
			os.Exit(2)
		}
		realExecutor = executor.Run
	}
	if err = s.RunWorker(ctx, *slots, realExecutor); err != nil {
		slog.Error("worker stopped; inspect private runtime state")
		os.Exit(1)
	}
}
