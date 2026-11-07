package platform

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// Opt-in, one real empty E2B sandbox. No Harbor trial, benchmark, Harness,
// model request, R2 operation or staging database is involved.
func TestM05RealE2BOrphanReconciliation(t *testing.T) {
	if os.Getenv("QIQC_REAL_E2B_RECONCILE_TEST") != "1" {
		t.Skip("requires explicit low-volume E2B drill opt-in")
	}
	module, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	project := filepath.Dir(filepath.Dir(module))
	python := os.Getenv("QIQC_SCIENCE_PYTHON")
	if python == "" {
		python = "/opt/qiqc-staging/venv/bin/python"
	}
	helper := `import json,sys,importlib.metadata
sys.path.insert(0,sys.argv[1]+'/competition-platform/platform/scripts/science')
from e2b import Sandbox
from e2b_runtime import load_key
try:
 key=load_key(); action=sys.argv[2]
 if action=='create':
  sb=Sandbox.create(api_key=key,timeout=180,network={'allow_public_traffic':False},metadata={'purpose':'qiqc-m05-empty-orphan-drill'})
  print(json.dumps({'sandbox_id':sb.sandbox_id,'state':'created','sdk_version':importlib.metadata.version('e2b')}))
 else:
  sid=sys.argv[3]
  if action=='cleanup':
   try: Sandbox.connect(sid,api_key=key).kill()
   except Exception as exc:
    if type(exc).__name__ not in ('NotFoundException','SandboxNotFoundException'):raise
  try:
   Sandbox.get_info(sid,api_key=key)
   print(json.dumps({'sandbox_id':sid,'state':'exists'}))
  except Exception as exc:
   if type(exc).__name__ not in ('NotFoundException','SandboxNotFoundException'):raise
   print(json.dumps({'sandbox_id':sid,'state':'provider_not_found'}))
except Exception as exc:
 print(json.dumps({'error_class':type(exc).__name__}));sys.exit(1)
`
	provider := func(action, sid string) (map[string]string, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, python, "-c", helper, project, action, sid)
		out, e := cmd.Output()
		var value map[string]string
		if json.Unmarshal(out, &value) != nil {
			return nil, fmt.Errorf("provider helper returned no structured result")
		}
		if e != nil {
			return value, fmt.Errorf("provider helper failed (%s)", value["error_class"])
		}
		return value, nil
	}
	report := map[string]any{"schema": "qiqc-m05-real-orphan/v1", "status": "failed", "scope": "one empty real E2B sandbox plus isolated Go state machine; no scientific execution", "harbor": false, "model_calls": 0, "r2_operations": 0, "worker_process_killed": false, "lease_clock": "real wall time", "fixtures": "official Codex archive hash copied only as immutable local bytes; no upload API or execution"}
	started := time.Now()
	var sandboxID string
	defer func() {
		if sandboxID != "" {
			state, e := provider("cleanup", sandboxID)
			report["finally_cleanup_confirmed"] = e == nil && state["state"] == "provider_not_found"
			if e != nil || state["state"] != "provider_not_found" {
				t.Error("this drill's E2B cleanup remains unconfirmed")
			}
		}
		if !t.Failed() {
			report["status"] = "passed"
		}
		report["elapsed_seconds"] = time.Since(started).Seconds()
		report["created_at"] = time.Now().UTC().Format(time.RFC3339)
		if path := os.Getenv("QIQC_M05_REPORT"); path != "" {
			raw, _ := json.MarshalIndent(report, "", "  ")
			if err := os.WriteFile(path, append(raw, '\n'), 0600); err != nil {
				t.Error("cannot write drill report")
			}
		}
	}()
	s := testStore(t)
	token := readyUser(t, s, "orphan-owner@example.test", "team")
	other := readyUser(t, s, "waiting-team@example.test", "team")
	admin := readyUser(t, s, "reconciler@example.test", "admin")
	manifestRaw, err := os.ReadFile(filepath.Join(project, "benchmark-assets/adapters/science-probe/allowed-submissions.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest map[string]struct {
		SHA     string `json:"sha256"`
		Archive string `json:"archive"`
	}
	if err = json.Unmarshal(manifestRaw, &manifest); err != nil {
		t.Fatal(err)
	}
	official := manifest["codex"]
	archive, err := os.ReadFile(filepath.Join(project, "benchmark-assets/adapters", official.Archive))
	if err != nil {
		t.Fatal(err)
	}
	if shaBytes(archive) != official.SHA {
		t.Fatal("official fixture hash mismatch")
	}
	if err = os.MkdirAll(filepath.Join(s.Root, "uploads"), 0700); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(s.Root, "uploads", official.SHA+".zip"), archive, 0600); err != nil {
		t.Fatal(err)
	}
	s.ScienceHashes = map[string]bool{official.SHA: true}
	seedVersion := func(token string) string {
		user, e := s.Session(token)
		if e != nil {
			t.Fatal(e)
		}
		vid := id()
		_, e = s.DB.Exec("INSERT INTO versions(id,team_id,sha256,name,status,report,reason,created) VALUES(?,?,?,'M05 immutable official fixture','approved','{}','state machine setup only',?)", vid, user.TeamID, official.SHA, s.now())
		if e != nil {
			t.Fatal(e)
		}
		return vid
	}
	bid, err := s.Confirm(token, seedVersion(token), "infra", "scientific-smoke", "m05-orphan")
	if err != nil {
		t.Fatal(err)
	}
	waiting, err := s.Confirm(other, seedVersion(other), "infra", "scientific-smoke", "m05-waiting")
	if err != nil {
		t.Fatal(err)
	}
	binary := filepath.Join(t.TempDir(), "science-reconcile")
	build := exec.Command("go", "build", "-o", binary, "./cmd/science-reconcile")
	build.Dir = module
	if _, err = build.Output(); err != nil {
		t.Fatal("could not build operator CLI")
	}
	created, err := provider("create", "")
	if err != nil {
		t.Fatal(err)
	}
	sandboxID = created["sandbox_id"]
	if sandboxID == "" {
		t.Fatal("no created sandbox id")
	}
	report["sandbox_id"] = sandboxID
	report["e2b_sdk_version"] = created["sdk_version"]
	lease, err := s.Claim("lost-controller-fixture", 1)
	if err != nil || lease == nil || lease.BatchID != bid["id"] {
		t.Fatal("initial scientific lease", err)
	}
	receiptDir := filepath.Join(s.Root, "scientific")
	if err = os.MkdirAll(receiptDir, 0700); err != nil {
		t.Fatal(err)
	}
	receiptPath := filepath.Join(receiptDir, lease.RunID+"_1.json")
	receipt := map[string]any{"run_id": lease.RunID, "attempt": 1, "mode": "scientific-smoke", "resources": map[string]string{sandboxID: "created"}, "provider_checked": false}
	raw, _ := json.Marshal(receipt)
	if err = os.WriteFile(receiptPath, raw, 0600); err != nil {
		t.Fatal(err)
	}
	runCLI := func() error {
		ctx, cancel := context.WithTimeout(context.Background(), 75*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, binary, "--data", s.Root, "--run", lease.RunID, "--attempt", "1", "--reason", "M05 isolated real E2B orphan drill")
		return cmd.Run()
	}
	// A healthy/current worker must never have its sandbox killed by the operator
	// CLI merely because a matching resource receipt exists.
	if err = runCLI(); err == nil {
		t.Fatal("CLI accepted live lease before worker loss")
	}
	live, err := provider("inspect", sandboxID)
	if err != nil || live["state"] != "exists" {
		t.Fatal("early CLI deleted healthy sandbox")
	}
	report["live_lease_cli_rejected_before_provider_deletion"] = true
	deadline := time.Unix(0, int64(lease.LeaseUntil*1e9)).Add(150 * time.Millisecond)
	for time.Now().Before(deadline) {
		remaining := time.Until(deadline)
		if remaining > time.Second {
			remaining = time.Second
		}
		time.Sleep(remaining)
	}
	recovered, err := s.Recover()
	if err != nil || recovered.Attention != 1 {
		t.Fatal("expired scientific lease not held for reconciliation", err)
	}
	if err = s.AdminAction(admin, Action{Action: "retry", Target: lease.BatchID, Actor: "M05 test operator", Reason: "Cannot retry unresolved sandbox"}, "local"); status(err) != 409 {
		t.Fatal("retry bypassed unresolved sandbox")
	}
	if err = s.Cancel(token, lease.BatchID); err != nil {
		t.Fatal(err)
	}
	cancelled, err := s.GetBatch(token, lease.BatchID)
	if err != nil || cancelled["status"] != "cancelling" {
		t.Fatal("cancel released unresolved slot")
	}
	if next, e := s.Claim("replacement-controller", 1); e != nil || next != nil {
		t.Fatal("unknown sandbox slot reused before provider confirmation")
	}
	if err = s.Heartbeat(*lease); status(err) != 409 {
		t.Fatal("stale controller regained lease")
	}
	report["unresolved_retry_blocked"] = true
	report["cancellation_kept_slot"] = true
	report["stale_heartbeat_rejected"] = true
	if err = runCLI(); err != nil {
		t.Fatal("real provider-confirmed reconcile CLI failed")
	}
	absent, err := provider("inspect", sandboxID)
	if err != nil || absent["state"] != "provider_not_found" {
		t.Fatal("provider did not confirm destroyed sandbox")
	}
	raw, err = os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	var checked struct {
		ProviderChecked bool              `json:"provider_checked"`
		Resources       map[string]string `json:"resources"`
	}
	if json.Unmarshal(raw, &checked) != nil || !checked.ProviderChecked || len(checked.Resources) != 1 || checked.Resources[sandboxID] != "destroyed" {
		t.Fatal("provider receipt not durable")
	}
	final, err := s.GetBatch(token, lease.BatchID)
	if err != nil || final["status"] != "cancelled" {
		t.Fatal("cancel did not finalize after provider cleanup")
	}
	next, err := s.Claim("replacement-controller", 1)
	if err != nil || next == nil || next.BatchID != waiting["id"] {
		t.Fatal("confirmed cleanup did not release capacity")
	}
	var ledger, attempts, audit int
	if err = s.DB.QueryRow("SELECT COUNT(*) FROM ledger WHERE batch_id=?", lease.BatchID).Scan(&ledger); err != nil {
		t.Fatal(err)
	}
	if err = s.DB.QueryRow("SELECT COUNT(*) FROM attempts WHERE run_id=?", lease.RunID).Scan(&attempts); err != nil {
		t.Fatal(err)
	}
	if err = s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action='reconcile_resources' AND object_id=?", lease.RunID).Scan(&audit); err != nil {
		t.Fatal(err)
	}
	if ledger != 1 || attempts != 1 || audit != 1 {
		t.Fatal("duplicate debit, attempt or reconciliation audit")
	}
	if err = runCLI(); err == nil {
		t.Fatal("duplicate CLI reconciliation accepted")
	}
	report["provider_404_confirmed"] = true
	report["durable_receipt_checked"] = true
	report["slot_released_after_confirmation"] = true
	report["original_batch_final_state"] = "cancelled"
	report["original_ledger_records"] = ledger
	report["original_attempts"] = attempts
	report["reconciliation_audits"] = audit
	report["duplicate_reconciliation_rejected"] = true
	t.Log("M05 one empty real E2B sandbox: lease expired, unresolved slot held, actual SDK deletion and 404 confirmed, capacity released; no Harness/Harbor/model execution")
}
