// qiqc-notifications is an opt-in local notification worker. It has no SMTP
// transport and is deliberately not added to any deployed service unit.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/gaugeforge/qiqc-platform-go/internal/notifications"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
)

func main() {
	var database, mode string
	var once bool
	flag.StringVar(&database, "database", "", "explicit trusted database path")
	flag.StringVar(&mode, "mode", notifications.LocalOnly, "local_only (default), or explicit fake simulation")
	flag.BoolVar(&once, "once", false, "process one bounded cycle")
	flag.Parse()
	if database == "" {
		log.Fatal("explicit -database path required")
	}
	if mode != notifications.LocalOnly && mode != notifications.Fake {
		log.Fatal("real mail is not configured or approved")
	}
	store, err := platform.Open(database)
	if err != nil {
		log.Fatal("notification database unavailable")
	}
	defer store.Close()
	var transport notifications.Transport
	if mode == notifications.Fake {
		transport = &notifications.FakeTransport{}
	}
	dispatcher, err := notifications.New(store.DB, notifications.Config{Mode: mode}, transport)
	if err != nil {
		log.Fatal("notification setup failed")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if once {
		err = dispatcher.Cycle(ctx)
	} else {
		err = dispatcher.Run(ctx)
	}
	if err != nil && ctx.Err() == nil {
		log.Fatal("notification worker failed; no delivery secrets logged")
	}
	stats, err := dispatcher.Statistics(context.Background())
	if err != nil {
		log.Fatal("notification statistics unavailable")
	}
	json.NewEncoder(os.Stdout).Encode(stats)
}
