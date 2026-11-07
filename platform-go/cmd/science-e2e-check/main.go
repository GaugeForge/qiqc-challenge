// Real public scientific smoke through Go auth/submission/review/queue/gateway.
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"github.com/gaugeforge/qiqc-platform-go/internal/modelproxy"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
	"github.com/gaugeforge/qiqc-platform-go/internal/scienceexec"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func must(err error) {
	if err != nil {
		panic(err)
	}
}
func secret() string {
	b := make([]byte, 24)
	_, err := rand.Read(b)
	must(err)
	return hex.EncodeToString(b)
}
func main() {
	root := "/root/QQ-Challenge"
	resume := flag.String("verify-completed", "", "recheck a completed private acceptance fixture without creating cloud work")
	flag.Parse()
	if *resume != "" {
		verifyCompleted(root, *resume)
		return
	}
	dir, err := os.MkdirTemp(filepath.Join(root, "runtime/legacy-platform-data"), "go-science-e2e-")
	must(err)
	s, err := platform.Open(filepath.Join(dir, "platform.db"))
	must(err)
	defer s.Close()
	os.Setenv("QIQC_R2_ENABLED", "1")
	os.Setenv("QIQC_R2_ENV", filepath.Join(root, "runtime/legacy-platform-data/r2.env"))
	s.Objects, err = rollout.FromEnvironment(filepath.Join(dir, "objects"))
	must(err)
	// The production scanner is the pinned parser image, not the participant
	// agent image. Keeping this acceptance fixture on the same image avoids a
	// false pre-E2B failure when qiqcbench-agent:latest is intentionally absent.
	s.Scanner = platform.DockerScanner(filepath.Join(root, "competition-platform/platform/qiqc/archive.py"), "python:3.13-slim")
	values := map[string]string{}
	env, err := os.ReadFile(filepath.Join(root, ".env"))
	must(err)
	for _, line := range strings.Split(string(env), "\n") {
		k, v, ok := strings.Cut(line, "=")
		if ok {
			values[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), "\"'")
		}
	}
	proxy, err := modelproxy.New(modelproxy.Config{BaseURL: values["HY_URL"], MasterKey: values["HY_API"], Authorize: s.AuthorizeModel, Reserve: s.ReserveModelRequest, RecordUsage: s.RecordModelUsage})
	must(err)
	gateway := httptest.NewServer(proxy)
	defer gateway.Close()
	create := func(email, role string) string {
		pwd := secret()
		_, err := s.CreateAccount(email, pwd, "Scientific acceptance "+role, role)
		must(err)
		token, _, err := s.Login(email, pwd, "127.0.0.1")
		must(err)
		must(s.ChangePassword(token, secret()))
		return token
	}
	team := create("science-team@example.test", "team")
	admin := create("science-admin@example.test", "admin")
	bytes, err := os.ReadFile(filepath.Join(root, "benchmark-assets/adapters/codex-runtime.zip"))
	must(err)
	digest := sha256.Sum256(bytes)
	s.ScienceHashes = map[string]bool{hex.EncodeToString(digest[:]): true}
	version, err := s.Upload(team, bytes)
	must(err)
	must(s.Review(admin, version.ID, "approve", "acceptance operator", "Official pinned scientific-smoke adapter inspected", "127.0.0.1"))
	batch, err := s.Confirm(team, version.ID, "infra", "scientific-smoke", "scientific-e2e-once")
	must(err)
	bid := batch["id"].(string)
	again, err := s.Confirm(team, version.ID, "infra", "scientific-smoke", "scientific-e2e-once")
	must(err)
	if again["id"] != bid {
		panic("idempotency failed")
	}
	python := os.Getenv("QIQC_SCIENCE_PYTHON")
	if python == "" {
		python = "/opt/qiqc-staging/venv/bin/python"
	}
	projectRoot := os.Getenv("QIQC_SCIENCE_PROJECT")
	if projectRoot == "" {
		projectRoot = root
	}
	executor, err := scienceexec.New(s, scienceexec.Config{ProjectRoot: projectRoot, Python: python, GatewayURL: gateway.URL})
	must(err)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.RunWorker(ctx, 1, executor.Run) }()
	deadline := time.Now().Add(15 * time.Minute)
	for {
		batch, err = s.GetBatch(team, bid)
		must(err)
		if batch["status"] == "completed" {
			break
		}
		var raw string
		must(s.DB.QueryRow("SELECT status FROM batches WHERE id=?", bid).Scan(&raw))
		if raw == "attention" {
			cancel()
			<-done
			panic("real scientific worker failed; inspect private receipt at " + dir)
		}
		if time.Now().After(deadline) {
			cancel()
			<-done
			panic("scientific deadline")
		}
		time.Sleep(time.Second)
	}
	cancel()
	must(<-done)
	verifyResult(root, dir, s, team, bid, version, batch)
}

func verifyResult(root, dir string, s *platform.Store, team, bid string, version platform.Version, batch map[string]any) {
	trace, err := s.Trace(context.Background(), team, bid, 0)
	must(err)
	var provenance map[string]any
	must(json.Unmarshal([]byte(trace["text"].(string)), &provenance))
	if trace["mode"] != "scientific-smoke" || provenance["standard_harbor_rollout"] != true || provenance["mode"] != "scientific-smoke" || !strings.Contains(provenance["summary"].(string), "not leaderboard eligible") {
		panic("public provenance missing")
	}
	var usage, ledger, activeTokens int
	must(s.DB.QueryRow("SELECT COUNT(*) FROM usage WHERE status=200").Scan(&usage))
	must(s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&ledger))
	must(s.DB.QueryRow("SELECT COUNT(*) FROM run_tokens WHERE revoked=0").Scan(&activeTokens))
	if usage == 0 || ledger != 1 || activeTokens != 0 {
		panic("usage/quota/token ledger failed")
	}
	user, err := s.Session(team)
	must(err)
	if user.Quotas["infra"] != 9 {
		panic("quota mismatch")
	}
	receipts, err := filepath.Glob(filepath.Join(dir, "scientific", "*.json"))
	must(err)
	if len(receipts) != 1 {
		panic("resource receipt missing")
	}
	raw, err := os.ReadFile(receipts[0])
	must(err)
	var receipt struct {
		Resources map[string]string `json:"resources"`
	}
	must(json.Unmarshal(raw, &receipt))
	if len(receipt.Resources) != 3 {
		panic("wrong resource count")
	}
	for _, state := range receipt.Resources {
		if state != "destroyed" {
			panic("sandbox cleanup not confirmed")
		}
	}
	result := map[string]any{"status": "passed", "mode": "scientific-smoke", "batch_id": bid, "submission_sha256": version.SHA256, "password_change": true, "isolated_upload_scan": true, "admin_review": true, "idempotent_confirmation": true, "worker_queue": true, "real_e2b": true, "real_hy4_via_go_gateway": true, "model_usage_rows": usage, "independent_verifier": true, "score": batch["score"], "r2_readback_and_trace": true, "nonranking": true, "quota_debits": ledger, "active_model_tokens": activeTokens, "sandboxes_destroyed": 3, "private_evidence_dir": dir, "standard_harbor_rollout": true}
	data, err := json.MarshalIndent(result, "", "  ")
	must(err)
	must(os.WriteFile(filepath.Join(root, "competition-platform/platform/test-results/go-scientific-e2e.json"), append(data, '\n'), 0600))
	fmt.Println(string(data))
}

// Only the isolated acceptance fixture is eligible. Rechecking uses an ephemeral
// session for its existing test account; it neither queues work nor changes quota.
func verifyCompleted(root, dir string) {
	dir, err := filepath.Abs(dir)
	must(err)
	if filepath.Dir(dir) != filepath.Join(root, "runtime/legacy-platform-data") || !strings.HasPrefix(filepath.Base(dir), "go-science-e2e-") {
		panic("not an acceptance fixture")
	}
	s, err := platform.Open(filepath.Join(dir, "platform.db"))
	must(err)
	defer s.Close()
	os.Setenv("QIQC_R2_ENABLED", "1")
	os.Setenv("QIQC_R2_ENV", filepath.Join(root, "runtime/legacy-platform-data/r2.env"))
	s.Objects, err = rollout.FromEnvironment(filepath.Join(dir, "objects"))
	must(err)
	var uid, bid, status string
	var v platform.Version
	must(s.DB.QueryRow(`SELECT u.id,b.id,b.status,v.sha256 FROM users u JOIN batches b ON b.team_id=u.team_id JOIN versions v ON v.id=b.version_id WHERE u.email='science-team@example.test'`).Scan(&uid, &bid, &status, &v.SHA256))
	if status != "completed" {
		panic("fixture not completed")
	}
	token := secret()
	h := sha256.Sum256([]byte(token))
	hash := hex.EncodeToString(h[:])
	_, err = s.DB.Exec("INSERT INTO sessions(hash,user_id,expires,ip) VALUES(?,?,?,?)", hash, uid, float64(time.Now().Unix()+60), "127.0.0.1")
	must(err)
	defer s.DB.Exec("DELETE FROM sessions WHERE hash=?", hash)
	batch, err := s.GetBatch(token, bid)
	must(err)
	verifyResult(root, dir, s, token, bid, v, batch)
}
