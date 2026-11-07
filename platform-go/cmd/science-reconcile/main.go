// Operator-only CLI; never accepts uploaded commands or code paths.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
)

func fail() { fmt.Fprintln(os.Stderr, "reconciliation failed; slot remains reserved"); os.Exit(1) }
func main() {
	data := flag.String("data", "", "private platform directory")
	run := flag.String("run", "", "run id")
	attempt := flag.Int("attempt", 0, "attempt number")
	reason := flag.String("reason", "", "operator reason")
	proofPath := flag.String("native-precreate-proof", "", "private import-failure proof; only for no-resource native startup failure")
	allowNoResourceStartup := flag.Bool("allow-no-resource-startup", false, "operator-confirmed startup failure with no resource-created events")
	pythonDefault := os.Getenv("QIQC_SCIENCE_PYTHON")
	if pythonDefault == "" {
		pythonDefault = "/opt/qiqc-staging/venv/bin/python"
	}
	python := flag.String("python", pythonDefault, "trusted reconciler interpreter (preserve venv path)")
	flag.Parse()
	if *data == "" || *run == "" || *attempt < 1 || *reason == "" || filepath.Base(*run) != *run || (*proofPath != "" && *allowNoResourceStartup) {
		fail()
	}
	s, err := platform.Open(filepath.Join(*data, "platform.db"))
	if err != nil {
		fail()
	}
	defer s.Close()
	var executionMode string
	if err := s.DB.QueryRow("SELECT b.mode FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.id=? AND r.attempt=?", *run, *attempt).Scan(&executionMode); err != nil || !platform.IsScientificMode(executionMode) {
		fail()
	}
	path := filepath.Join(*data, "scientific", *run+"_"+strconv.Itoa(*attempt)+".json")
	info, err := os.Stat(path)
	if os.IsNotExist(err) && *proofPath != "" {
		info, err = os.Stat(filepath.Dir(path))
	}
	if err != nil {
		fail()
	}
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) && *proofPath != "" {
		raw, err = json.Marshal(map[string]any{"run_id": *run, "attempt": *attempt, "mode": executionMode, "resources": map[string]string{}})
	}
	if err != nil || len(raw) > 65536 {
		fail()
	}
	var receipt map[string]any
	if json.Unmarshal(raw, &receipt) != nil || receipt["run_id"] != *run || receipt["attempt"] != float64(*attempt) || receipt["mode"] != executionMode {
		fail()
	}
	if *proofPath != "" {
		proof, readErr := os.ReadFile(*proofPath)
		if readErr != nil || len(proof) > 32768 {
			fail()
		}
		receipt["precreate_proof"] = string(proof)
		raw, err = json.Marshal(receipt)
		if err != nil {
			fail()
		}
	}
	if *allowNoResourceStartup {
		var phase, controllerError string
		var resources map[string]string
		var current struct {
			Phase string            `json:"phase"`
			Error string            `json:"controller_error_class"`
			Resources map[string]string `json:"resources"`
		}
		if json.Unmarshal(raw, &current) != nil || current.Phase != "starting" || current.Error == "" || len(current.Resources) != 0 {
			fail()
		}
		phase, controllerError, resources = current.Phase, current.Error, current.Resources
		_ = phase
		_ = controllerError
		_ = resources
		receipt["allow_no_resource_startup"] = true
		receipt["phase"] = current.Phase
		receipt["controller_error_class"] = current.Error
		raw, err = json.Marshal(receipt)
		if err != nil { fail() }
	}
	// A stale receipt must not cause provider-side deletion before the database
	// discovers that this is the wrong attempt or an active healthy worker.
	if s.CheckScientificReconciliation(*run, *attempt) != nil {
		fail()
	}
	cmd := exec.Command(*python, "/root/QQ-Challenge/benchmark-assets/adapters/science-probe/reconcile_resources.py")
	cmd.Env = append(os.Environ(), "PYTHONPATH=/root/QQ-Challenge/competition-platform/platform/scripts/science")
	cmd.Stdin = bytes.NewReader(append(raw, '\n'))
	output, err := cmd.Output()
	if err != nil {
		fail()
	}
	var checked map[string]any
	if json.Unmarshal(output, &checked) != nil || checked["provider_checked"] != true || checked["run_id"] != *run || checked["attempt"] != float64(*attempt) {
		fail()
	}
	receipt["resources"] = checked["resources"]
	receipt["provider_checked"] = true
	if proof, exists := checked["no_resources_created"]; exists {
		receipt["no_resources_created"] = proof
	}
	raw, err = json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		fail()
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".reconcile-")
	if err != nil {
		fail()
	}
	tmp := f.Name()
	if stat, ok := info.Sys().(*syscall.Stat_t); ok && os.Geteuid() == 0 {
		if f.Chown(int(stat.Uid), int(stat.Gid)) != nil {
			f.Close()
			os.Remove(tmp)
			fail()
		}
	}
	defer os.Remove(tmp)
	if _, err = f.Write(raw); err != nil {
		fail()
	}
	if f.Sync() != nil || f.Close() != nil {
		fail()
	}
	if os.Rename(tmp, path) != nil {
		fail()
	}
	d, err := os.Open(filepath.Dir(path))
	if err != nil {
		fail()
	}
	if d.Sync() != nil {
		fail()
	}
	d.Close()
	// Operator execution may be root while the uploader has a dedicated uid.
	// Keep recovery sidecars owned by the original trusted cache writer; the
	// archive itself remains unchanged and no participant receives access.
	cache := filepath.Join(*data, "cache", *run, strconv.Itoa(*attempt))
	var cacheOwner *syscall.Stat_t
	if cached, statErr := os.Stat(filepath.Join(cache, "rollout.tar.gz")); statErr == nil {
		cacheOwner, _ = cached.Sys().(*syscall.Stat_t)
	}
	if s.ReconcileScientificResources(*run, *attempt, *reason) != nil {
		fail()
	}
	if cacheOwner != nil && os.Geteuid() == 0 {
		pending := filepath.Join(cache, "pending.json")
		if err := os.Chown(pending, int(cacheOwner.Uid), int(cacheOwner.Gid)); err != nil && !os.IsNotExist(err) {
			fail()
		}
		if file, err := os.Open(pending); err == nil {
			if file.Sync() != nil {
				file.Close()
				fail()
			}
			file.Close()
		}
	}
	fmt.Println("provider-confirmed resources reconciled; completed artifacts resume upload, otherwise operator may retry the existing request")
}
