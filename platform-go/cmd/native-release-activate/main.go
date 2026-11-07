// Native release activation is offline and never exposed over HTTP.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"os"
	"path/filepath"
)

func main() {
	data := flag.String("data", "", "private data directory")
	release := flag.String("release-id", "", "verified native release")
	candidate := flag.String("candidate-id", "", "bound candidate")
	digest := flag.String("manifest-sha256", "", "release record SHA256")
	actor := flag.String("actor", "", "operator")
	reason := flag.String("reason", "", "audit reason")
	rollback := flag.Bool("rollback", false, "restore prior admission identity; keep all history")
	migrate := flag.Bool("migrate-candidate", false, "offline drained roster migration; preserve historical evidence")
	previousCandidate := flag.String("previous-candidate-id", "", "expected prior candidate for roster migration")
	flag.Parse()
	if *data == "" {
		os.Exit(2)
	}
	s, err := platform.Open(filepath.Join(*data, "platform.db"))
	if err != nil {
		fmt.Fprintln(os.Stderr, "database unavailable")
		os.Exit(1)
	}
	defer s.Close()
	state := "activated"
	if *migrate && *rollback {
		fmt.Fprintln(os.Stderr, "migration and rollback cannot be combined")
		os.Exit(2)
	}
	if *migrate {
		err = s.MigrateScientificCandidate(*previousCandidate, *candidate, *actor, *reason)
		state = "candidate_migrated"
	} else if *rollback {
		err = s.RollbackNativeRelease(*release, *candidate, *actor, *reason)
		state = "rolled_back"
	} else {
		err = s.ActivateNativeRelease(*release, *candidate, *digest, *actor, *reason)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	json.NewEncoder(os.Stdout).Encode(map[string]any{"status": state, "release_id": *release, "candidate_id": *candidate})
}
