// qiqc-admin bootstraps a local administrator through private stdin. It is an
// operator command, never exposed as an unauthenticated HTTP endpoint.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"io"
	"os"
	"path/filepath"
)

func main() {
	data := flag.String("data", "", "private data directory")
	candidate := flag.String("candidate-id", "", "source SHA identity; empty independent candidate database only")
	flag.Parse()
	if *data == "" {
		fmt.Fprintln(os.Stderr, "private data directory required")
		os.Exit(2)
	}
	var input struct{ Email, Password, Name string }
	decoder := json.NewDecoder(io.LimitReader(os.Stdin, 8192))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil {
		fmt.Fprintln(os.Stderr, "invalid private input")
		os.Exit(2)
	}
	store, err := platform.Open(filepath.Join(*data, "platform.db"))
	if err != nil {
		fmt.Fprintln(os.Stderr, "database unavailable")
		os.Exit(1)
	}
	defer store.Close()
	if *candidate != "" {
		if err := store.ConfigureScientificCandidate(*candidate); err != nil {
			fmt.Fprintln(os.Stderr, "candidate initialization refused")
			os.Exit(1)
		}
	}
	user, err := store.CreateAccount(input.Email, input.Password, input.Name, "admin")
	if err != nil {
		fmt.Fprintln(os.Stderr, "administrator creation failed; account may already exist")
		os.Exit(1)
	}
	json.NewEncoder(os.Stdout).Encode(map[string]any{"id": user.ID, "email": user.Email, "role": user.Role, "must_change": user.MustChange})
}
