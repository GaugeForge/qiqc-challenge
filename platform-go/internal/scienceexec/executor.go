// Package scienceexec invokes only an organizer-owned controller bridge. Uploaded
// archives are bytes transported to E2B; no archive program executes on this host.
package scienceexec

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"github.com/gaugeforge/qiqc-platform-go/internal/providerprofile"
)

type Config struct{ ProjectRoot, Python, GatewayURL, Bridge, DiagnosticModel, ProviderProfile string }
type Executor struct {
	Store  *platform.Store
	Config Config
}
type result struct {
	Mode                        string  `json:"mode"`
	RunID                       string  `json:"run_id"`
	SHA                         string  `json:"submission_sha256"`
	Reward                      int     `json:"reward"`
	AgentExit                   int     `json:"agent_exit"`
	VerifierExit                int     `json:"verifier_exit"`
	VerifierScientificRejection bool    `json:"verifier_scientific_rejection"`
	InfrastructureOK            bool    `json:"infrastructure_ok"`
	StandardHarbor              bool    `json:"standard_harbor_rollout"`
	LeaderboardEligible         bool    `json:"leaderboard_eligible"`
	Runtime                     float64 `json:"runtime_seconds"`
	BudgetExhausted             bool    `json:"budget_exhausted"`
	AgentStopReason             string  `json:"agent_stop_reason"`
	AgentBudgetSeconds          int     `json:"agent_budget_seconds"`
	AcceptanceEarlyStopVerified bool    `json:"acceptance_early_stop_verified"`
	AcceptanceReadinessSHA      string  `json:"acceptance_readiness_sha256"`
	Rollout                     struct {
		Key      string `json:"key"`
		SHA      string `json:"sha256"`
		Readback bool   `json:"readback_verified"`
		Pending  bool   `json:"pending_upload"`
	} `json:"rollout"`
}
type event struct {
	Type      string   `json:"type"`
	Status    int      `json:"status"`
	Category  string   `json:"category"`
	ToolTypes []string `json:"tool_types"`
	Phase     string   `json:"phase"`
	State     string   `json:"state"`
	Role      string   `json:"role"`
	SandboxID string   `json:"sandbox_id"`
	Class     string   `json:"class"`
	Detail    string   `json:"detail"`
	Result    *result  `json:"result"`
}

// The target passed here has already been authenticated against the immutable
// batch snapshot by ScientificTargetForLease. The native bridge has its own
// per-task deadline covering setup, the frozen 180/300-second agent budget, the original
// verifier timeout and cleanup. This outer watchdog must not truncate that
// verifier at the historical bridge's 20-minute limit.
func controllerWatchdog(cfg Config, target *platform.ScientificTarget) time.Duration {
	if target != nil && target.Limits.Valid() && target.AgentBudgetSeconds > 0 && target.AgentBudgetSeconds <= target.Limits.AgentSeconds && regexp.MustCompile(`^native-(candidate|release)-[a-f0-9]{64}$`).MatchString(target.ReleaseID) {
		l := target.Limits
		agentSeconds := l.AgentSeconds
		if target.Kind == "infra" {
			agentSeconds = target.AgentBudgetSeconds
		}
		return time.Duration(l.BuildSeconds+l.SetupSeconds+agentSeconds+l.VerifierSeconds+900) * time.Second
	}
	if target != nil && platform.ValidScientificAgentBudget(target.AgentBudgetSeconds) &&
		regexp.MustCompile(`^native-(candidate|release)-[a-f0-9]{64}$`).MatchString(target.ReleaseID) &&
		filepath.Clean(cfg.Bridge) == filepath.Join(cfg.ProjectRoot, "benchmark-assets/adapters/science-probe/native_worker_bridge.py") {
		return time.Hour
	}
	return 20 * time.Minute
}

func New(store *platform.Store, cfg Config) (*Executor, error) {
	if cfg.DiagnosticModel != "" && cfg.DiagnosticModel != "minimax/minimax-m3.1-flash:free" {
		return nil, errors.New("unsupported diagnostic model")
	}
	if store == nil || !filepath.IsAbs(cfg.ProjectRoot) || !filepath.IsAbs(cfg.Python) {
		return nil, errors.New("trusted absolute controller paths required")
	}
	u, err := url.Parse(cfg.GatewayURL)
	if err != nil || u.Scheme != "http" || (u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost" && u.Hostname() != "::1") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("loopback model gateway required")
	}
	if cfg.Bridge == "" {
		cfg.Bridge = filepath.Join(cfg.ProjectRoot, "benchmark-assets/adapters/science-probe/worker_bridge.py")
	}
	if !filepath.IsAbs(cfg.Bridge) {
		return nil, errors.New("absolute organizer bridge required")
	}
	for _, p := range []string{cfg.Python, cfg.Bridge} {
		fi, err := os.Stat(p)
		if err != nil || !fi.Mode().IsRegular() {
			return nil, errors.New("trusted controller unavailable")
		}
	}
	return &Executor{Store: store, Config: cfg}, nil
}
func (e *Executor) Run(ctx context.Context, l platform.Lease) error {
	if !platform.IsScientificMode(l.Mode) || (l.Mode == "scientific-smoke" && (l.Kind != "infra" || l.Ordinal != 0)) || (l.Public != 1 && l.Public != 0) {
		return errors.New("scientific executor requires an approved bounded scientific lease")
	}
	if err := e.Store.ValidateLease(l); err != nil {
		return err
	}
	target, err := e.Store.ScientificTargetForLease(l)
	if err != nil {
		return err
	}
	if target == nil && (l.Public != 1 || l.Mode == "scientific") {
		return errors.New("scientific task requires immutable execution target")
	}
	watchdog := controllerWatchdog(e.Config, target)
	if err := e.Store.CheckVersion(l.SHA256); err != nil {
		return err
	}
	selectedModel := "tencent/hy4-preview"
	if e.Config.ProviderProfile != "" {
		raw, err := providerprofile.ReadPrivate(e.Config.ProviderProfile, 16384, true)
		if err != nil {
			return err
		}
		profile, _, err := providerprofile.Parse(raw)
		if err != nil {
			return err
		}
		if e.Config.DiagnosticModel != "" && (profile.Model != e.Config.DiagnosticModel || profile.Billing != "free") {
			return errors.New("diagnostic provider profile mismatch")
		}
		selectedModel = profile.Model
	}
	if e.Config.DiagnosticModel != "" || e.Config.ProviderProfile != "" {
		if target == nil || !strings.HasPrefix(target.ReleaseID, "native-") || filepath.Clean(e.Config.Bridge) != filepath.Join(e.Config.ProjectRoot, "benchmark-assets/adapters/science-probe/native_worker_bridge.py") {
			return errors.New("diagnostic model requires explicit native target")
		}
		if e.Config.DiagnosticModel != "" {
			selectedModel = e.Config.DiagnosticModel
		}
	}
	gatewayURL := e.Config.GatewayURL
	if target != nil && target.Formal {
		policy, err := e.Store.FormalExecutionForLease(l)
		if err != nil {
			return err
		}
		if policy == nil {
			return errors.New("formal model policy missing")
		}
		profile, identity, err := providerprofile.LoadMetadata(policy.ProfilePath)
		if err != nil || identity.SHA256 != policy.ProfileSHA256 || profile.Model != policy.Model {
			return errors.New("formal provider unavailable or changed")
		}
		if e.Config.DiagnosticModel != "" {
			return errors.New("diagnostic worker cannot execute formal batch")
		}
		selectedModel = profile.Model
		gatewayURL = policy.GatewayURL
	}
	token, err := e.Store.MintModelToken(l, selectedModel)
	if err != nil {
		return err
	}
	request := map[string]any{"submission_path": filepath.Join(e.Store.Root, "uploads", l.SHA256+".zip"), "sha256": l.SHA256, "run_id": l.RunID, "model_token": token, "gateway_url": strings.TrimRight(gatewayURL, "/"), "data_root": e.Store.Root, "lease": l, "model_id": selectedModel, "diagnostic_model": e.Config.DiagnosticModel}
	// Infra/public traffic uses the shared low-capacity key; formal batches use
	// the separately provisioned production key. The bridge reads only this
	// selector and never receives a key value in its request payload.
	e2bKeyName := "E2B_API_KEY"
	if l.Kind == "infra" || l.Kind == "public" {
		e2bKeyName = "E2B_API_KEY_2"
	}
	if target != nil {
		request["science_task_index"] = target.TaskIndex
		if target.Limits.Valid() {
			request["native_limits"] = target.Limits
			request["formal_execution"] = target.Formal
		}
		if l.Mode == "scientific" {
			request["science_task_id"] = target.TaskID
			request["catalog_sha256"] = target.CatalogSHA
		}
		request["agent_budget_seconds"] = target.AgentBudgetSeconds
		request["science_release_id"] = target.ReleaseID
	}
	input, _ := json.Marshal(request)
	input = append(input, '\n')
	cmd := exec.Command(e.Config.Python, e.Config.Bridge)
	cmd.Dir = e.Config.ProjectRoot
	cmd.Env = append(os.Environ(), "QIQC_PROJECT_ROOT="+e.Config.ProjectRoot, "QIQC_E2B_KEY_NAME="+e2bKeyName)
	cmd.Stdin = bytes.NewReader(input)
	cmd.Stderr = io.Discard
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	if err = cmd.Start(); err != nil {
		return err
	}
	done := make(chan struct{})
	defer close(done)
	// Gracefully signal the trusted controller so it kills its E2B resources in
	// finally. An immediate CommandContext SIGKILL would orphan remote sandboxes.
	go func() {
		tick := time.NewTicker(time.Second)
		defer tick.Stop()
		deadline := time.NewTimer(watchdog)
		defer deadline.Stop()
		stop := false
		for !stop {
			select {
			case <-done:
				return
			case <-ctx.Done():
				stop = true
			case <-deadline.C:
				stop = true
			case <-tick.C:
				var status string
				if e.Store.DB.QueryRow("SELECT status FROM batches WHERE id=?", l.BatchID).Scan(&status) != nil || status == "cancelling" || status == "cancelled" || e.Store.ValidateLease(l) != nil {
					stop = true
				}
			}
		}
		_ = cmd.Process.Signal(syscall.SIGTERM)
		select {
		case <-done:
			return
		case <-time.After(7 * time.Minute):
			_ = cmd.Process.Kill()
		}
	}()
	receipt := map[string]any{"run_id": l.RunID, "attempt": l.Attempt, "mode": l.Mode, "resources": map[string]string{}, "phase": "starting"}
	resources := receipt["resources"].(map[string]string)
	receiptPath := filepath.Join(e.Store.Root, "scientific", l.RunID+"_"+strconv.Itoa(l.Attempt)+".json")
	if err = os.MkdirAll(filepath.Dir(receiptPath), 0o750|os.ModeSetgid); err != nil {
		_ = cmd.Process.Signal(syscall.SIGTERM)
		_ = cmd.Wait()
		return err
	}
	// The API must verify cleanup receipts before a participant can cancel or
	// retry a failed task. Keep the directory private to the worker and the
	// trusted data group, never world-readable.
	if err = os.Chmod(filepath.Dir(receiptPath), 0o750|os.ModeSetgid); err != nil {
		_ = cmd.Process.Signal(syscall.SIGTERM)
		_ = cmd.Wait()
		return err
	}
	var final *result
	var eventErr error
	scan := bufio.NewScanner(stdout)
	scan.Buffer(make([]byte, 65536), 2*1024*1024)
	for scan.Scan() {
		var v event
		if json.Unmarshal(scan.Bytes(), &v) != nil {
			eventErr = errors.New("invalid controller event")
			_ = cmd.Process.Signal(syscall.SIGTERM)
			continue
		}
		switch v.Type {
		case "phase":
			receipt["phase"] = v.Phase
			if v.Phase == "uploading_rollout" {
				if x := e.Store.BeginUpload(l); x != nil {
					eventErr = &platform.ExecutionFailure{Category: "platform", Phase: "uploading_rollout", Code: "begin_upload_failed", Message: x.Error(), Retryable: true}
					_ = cmd.Process.Signal(syscall.SIGTERM)
				}
			}
		case "resource":
			resources[v.SandboxID] = v.State
		case "model_status":
			receipt["last_model_status"] = v.Status
			receipt["last_model_category"] = v.Category
			receipt["model_tool_types"] = v.ToolTypes
		case "error":
			receipt["controller_error_class"] = v.Class
			receipt["controller_error_detail"] = v.Detail
			eventErr = fmt.Errorf("scientific controller failed: %s", v.Class)
		case "result":
			final = v.Result
		}
		if x := writeReceipt(receiptPath, receipt); x != nil {
			phase, _ := receipt["phase"].(string)
			eventErr = &platform.ExecutionFailure{Category: "platform", Phase: phase, Code: "receipt_write_failed", Message: x.Error(), Retryable: false}
			_ = cmd.Process.Signal(syscall.SIGTERM)
		}
	}
	scanErr := scan.Err()
	waitErr := cmd.Wait()
	// Failure evidence is independent of score completion and remote cleanup.
	// An R2 outage retains the private spool for maintenance, never reruns science.
	_, artifactErr := e.Store.ResumeScientificFailureUploads(context.Background())
	if artifactErr != nil {
		receipt["failure_evidence_upload"] = "pending_retry"
		_ = writeReceipt(receiptPath, receipt)
	}
	unresolved := false
	for _, state := range resources {
		if state != "destroyed" {
			unresolved = true
		}
	}
	// A controller that fails in the initial starting phase has not reported a
	// provider resource. Requeue that original attempt once automatically; a
	// second startup failure (or any failure after resource creation) remains
	// operator-visible instead of entering a retry loop.
	if receiptPhase, _ := receipt["phase"].(string); !unresolved && len(resources) == 0 && receiptPhase == "starting" && waitErr != nil && final == nil {
		if err := e.Store.RetryZeroResourceStartup(l, "controller failed before creating an E2B resource; automatic one-time retry"); err == nil {
			return nil
		}
	}
	// Cancellation stops the trusted controller with SIGTERM. If the controller
	// still reports every created resource as destroyed, the slot is no longer
	// uncertain and the cancellation can finalize immediately. A controller that
	// failed before creating any resource is equally safe to close: there is no
	// remote sandbox to reconcile, and treating it as a normal failure leaves the
	// batch in "cancelling" until the long task lease expires. Treating either
	// case as a lost worker leaves the batch stuck forever.
	if !unresolved && waitErr != nil && final == nil {
		var batchState string
		if e.Store.DB.QueryRow("SELECT status FROM batches WHERE id=?", l.BatchID).Scan(&batchState) == nil && batchState == "cancelling" {
			if err := e.Store.CancelFailedController(l); err == nil {
				return nil
			}
		}
	}
	if unresolved {
		// Keep the global slot reserved until an operator positively reconciles
		// remote resources. A receipt that lists every created resource as
		// destroyed is proof E2B stopped, so an ordinary controller failure must
		// not be misclassified as a lost worker and block retries.
		_, _ = e.Store.DB.Exec("UPDATE runs SET state='attention',error='worker_lost:reconciliation_required',lease_until=NULL WHERE id=? AND attempt=? AND owner=?", l.RunID, l.Attempt, l.Owner)
		_, _ = e.Store.DB.Exec("UPDATE batches SET status='attention' WHERE id=? AND status!='cancelling'", l.BatchID)
		_, _ = e.Store.DB.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", l.RunID)
		return errors.New("sandbox cleanup unconfirmed; reconciliation required")
	}
	if eventErr != nil {
		return classifyExecutionFailure(receipt, eventErr, "", true)
	}
	if scanErr != nil {
		return classifyExecutionFailure(receipt, scanErr, "platform", true)
	}
	if waitErr != nil {
		return classifyExecutionFailure(receipt, errors.New("scientific controller terminated before producing a trusted result"), "", true)
	}
	if err = validateCatalogBudget(final, target); err != nil {
		return classifyExecutionFailure(receipt, err, "platform", false)
	}
	if err = validateResult(final, l); err != nil {
		category := "harness"
		if final != nil && final.VerifierExit != 0 {
			category = "verifier"
		}
		return classifyExecutionFailure(receipt, err, category, false)
	}
	if final.Rollout.Pending {
		if err = e.Store.PromoteScientificPending(l); err != nil {
			return classifyExecutionFailure(receipt, err, "evidence", false)
		}
		if err = e.Store.CompleteScientificPending(context.Background(), l, final.Reward, fmt.Sprintf("%.6f", final.Runtime), final.Rollout.SHA); err != nil {
			return classifyExecutionFailure(receipt, err, "evidence", false)
		}
		return nil
	}
	return classifyExecutionFailure(receipt, errors.New("scientific bridge did not produce durable pending upload"), "evidence", false)
}

// classifyExecutionFailure attaches a bounded category to the trusted receipt.
// Only a genuine platform/startup failure is eligible for the one automatic
// retry; every model, harness, verifier, evidence and budget outcome goes to
// attention without rerunning the Agent.
func classifyExecutionFailure(receipt map[string]any, err error, category string, retryable bool) error {
	if err == nil {
		return nil
	}
	if failure, ok := platform.AsExecutionFailure(err); ok {
		return failure
	}
	failure := platform.NewExecutionFailure(receipt, err.Error(), retryable)
	if category != "" {
		failure.Category = category
		failure.Retryable = retryable && category == "platform"
	}
	return failure
}
func validAcceptanceEarlyStop(r *result) bool {
	return r != nil && r.AcceptanceEarlyStopVerified &&
		r.AgentStopReason == "acceptance_early_stop" && !r.BudgetExhausted &&
		platform.ValidScientificAgentBudget(r.AgentBudgetSeconds) && (r.AgentExit == 137 || r.AgentExit == 143) &&
		r.Runtime > 0 && r.Runtime < float64(r.AgentBudgetSeconds) &&
		regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(r.AcceptanceReadinessSHA)
}

func validExecutionBudget(seconds int) bool { return seconds > 0 && seconds <= 604800 }
func validateResult(r *result, l platform.Lease) error {
	if l.Mode == "scientific" && (r == nil || r.Runtime <= 0 || r.AcceptanceEarlyStopVerified || r.AgentStopReason == "acceptance_early_stop" || (r.BudgetExhausted && ((r.AgentExit != 124 && r.AgentExit != 137) || r.Runtime < float64(r.AgentBudgetSeconds-1) || r.Runtime >= float64(r.AgentBudgetSeconds+30)))) {
		return errors.New("bounded scientific result duration rejected")
	}
	if r == nil || r.Mode != l.Mode || !platform.IsScientificMode(l.Mode) || r.RunID != l.RunID || r.SHA != l.SHA256 || !r.InfrastructureOK || (r.AgentExit != 0 && !((l.Public == 0 || l.Mode == "scientific") && ((r.BudgetExhausted && r.AgentStopReason == "budget_exhausted" && validExecutionBudget(r.AgentBudgetSeconds)) || validAcceptanceEarlyStop(r)))) || (r.VerifierExit != 0 && !((l.Public == 0 || l.Mode == "scientific") && r.VerifierExit == 1 && r.Reward == 0 && r.VerifierScientificRejection)) || (r.VerifierScientificRejection && ((l.Public != 0 && l.Mode != "scientific") || r.VerifierExit != 1 || r.Reward != 0)) || (r.Reward != 0 && r.Reward != 1) || (r.Runtime < 0 || math.IsNaN(r.Runtime) || math.IsInf(r.Runtime, 0)) || r.LeaderboardEligible || !r.StandardHarbor || (!r.Rollout.Readback && !r.Rollout.Pending) || len(r.Rollout.SHA) != 64 {
		return errors.New("scientific result contract rejected")
	}
	return nil
}
func validateCatalogBudget(r *result, target *platform.ScientificTarget) error {
	if r == nil {
		return errors.New("missing scientific result")
	}
	if target == nil {
		if r.BudgetExhausted || r.VerifierScientificRejection || r.AcceptanceEarlyStopVerified || r.AcceptanceReadinessSHA != "" || r.AgentStopReason == "acceptance_early_stop" {
			return errors.New("unbounded public diagnostic cannot claim catalog budget stop")
		}
		return nil
	}
	if !(platform.ValidScientificAgentBudget(target.AgentBudgetSeconds) || target.Limits.Valid() && target.AgentBudgetSeconds > 0 && target.AgentBudgetSeconds <= target.Limits.AgentSeconds) || r.AgentBudgetSeconds != target.AgentBudgetSeconds {
		return errors.New("catalog budget result contract rejected")
	}
	if r.AcceptanceEarlyStopVerified || r.AcceptanceReadinessSHA != "" || r.AgentStopReason == "acceptance_early_stop" {
		if target.Formal {
			return errors.New("formal execution cannot use diagnostic early stop")
		}
		if !regexp.MustCompile(`^native-(candidate|release)-[a-f0-9]{64}$`).MatchString(target.ReleaseID) ||
			!validAcceptanceEarlyStop(r) {
			return errors.New("native acceptance early stop proof rejected")
		}
		return nil
	}
	if r.AgentBudgetSeconds != target.AgentBudgetSeconds ||
		(r.BudgetExhausted && r.AgentStopReason != "budget_exhausted") ||
		(!r.BudgetExhausted && (r.AgentStopReason != "completed" || r.AgentExit != 0)) {
		return errors.New("catalog budget result contract rejected")
	}
	return nil
}
func writeReceipt(path string, v any) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".receipt-")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if _, err = f.Write(data); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	// The trusted API process verifies this receipt before allowing a
	// per-task cancel/retry. Make it group-readable but never world-readable.
	if err = os.Chmod(tmp, 0o640); err != nil {
		return err
	}
	if err = os.Rename(tmp, path); err != nil {
		return err
	}
	d, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
