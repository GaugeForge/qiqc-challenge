package main

import (
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestReviewWorkerHelperProcess(t *testing.T) {
	if os.Getenv("QIQC_REVIEW_HELPER") != "1" {
		return
	}
	split := 0
	for i, arg := range os.Args {
		if arg == "--" {
			split = i + 1
			break
		}
	}
	os.Args = append([]string{"review-worker"}, os.Args[split:]...)
	main()
	os.Exit(0)
}
func workerCommand(args ...string) *exec.Cmd {
	cmd := exec.Command(os.Args[0], append([]string{"-test.run=^TestReviewWorkerHelperProcess$", "--"}, args...)...)
	cmd.Env = append(os.Environ(), "QIQC_REVIEW_HELPER=1")
	return cmd
}
func TestOnceDatabaseFailureExitsNonzeroWithoutPrivateDetails(t *testing.T) {
	p := filepath.Join(t.TempDir(), "platform.sqlite")
	store, err := platform.Open(p)
	if err != nil {
		t.Fatal(err)
	}
	defer store.DB.Close()
	store.DB.Exec("INSERT INTO teams(id,name) VALUES('t','fixture')")
	_, err = store.DB.Exec("INSERT INTO versions(id,team_id,sha256,name,status,report,reason,created) VALUES('v','t',?,'fixture','pending_review',?,'',0)", strings.Repeat("a", 64), `{"ruleset_version":"test","risks":[],"snippets":[]}`)
	if err != nil {
		t.Fatal(err)
	}
	_, err = store.DB.Exec(`CREATE TRIGGER private_failure BEFORE UPDATE ON review_advisories BEGIN SELECT RAISE(ABORT,'private-upstream-body-SECRET'); END`)
	if err != nil {
		t.Fatal(err)
	}
	output, err := workerCommand("--db", p, "--provider", "fake", "--once").CombinedOutput()
	if err == nil {
		t.Fatal("database failure falsely successful", string(output))
	}
	if strings.Contains(string(output), "SECRET") || !strings.Contains(string(output), "not committed") {
		t.Fatal("unsafe/unhelpful failure", string(output))
	}
	var n int
	store.DB.QueryRow("SELECT COUNT(*) FROM review_advisories WHERE state='complete'").Scan(&n)
	if n != 0 {
		t.Fatal("failed attempt committed")
	}
}
func TestWorkerTimeoutBoundsAndPrivateKeySymlink(t *testing.T) {
	for _, timeout := range []string{"4s", "121s"} {
		out, err := workerCommand("--db", "unused", "--timeout", timeout, "--once").CombinedOutput()
		if err == nil || !strings.Contains(string(out), "5s..120s") {
			t.Fatal(timeout, err, string(out))
		}
	}
	dir := t.TempDir()
	key := filepath.Join(dir, "key")
	os.WriteFile(key, []byte("synthetic-only-key"), 0600)
	os.Symlink(key, filepath.Join(dir, "link"))
	out, err := workerCommand("--db", "unused", "--provider", "https", "--key-file", filepath.Join(dir, "link"), "--once").CombinedOutput()
	if err == nil || !strings.Contains(string(out), "private regular file") {
		t.Fatal(err, string(out))
	}
}
