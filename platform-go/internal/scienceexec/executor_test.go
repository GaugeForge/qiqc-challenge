package scienceexec

import (
	"encoding/json"
	"github.com/gaugeforge/qiqc-platform-go/internal/platform"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestNativeWatchdogPreservesVerifierBudgetWithoutExtendingLegacy(t *testing.T) {
	root := "/trusted/project"
	cfg := Config{ProjectRoot: root, Bridge: filepath.Join(root, "benchmark-assets/adapters/science-probe/native_worker_bridge.py")}
	target := &platform.ScientificTarget{ReleaseID: "native-candidate-" + strings.Repeat("a", 64), AgentBudgetSeconds: 300}
	for _, id := range []string{target.ReleaseID, "native-release-" + strings.Repeat("b", 64)} {
		x := *target
		x.ReleaseID = id
		if controllerWatchdog(cfg, &x) != time.Hour {
			t.Fatal("registered native topology would truncate original verifier budget")
		}
	}
	if controllerWatchdog(cfg, nil) != 20*time.Minute {
		t.Fatal("unbound request extended")
	}
	for _, id := range []string{platform.ScientificReleaseID, "native-candidate-short", "native-release-" + strings.Repeat("a", 63), "native-candidate-" + strings.Repeat("z", 64)} {
		x := *target
		x.ReleaseID = id
		if controllerWatchdog(cfg, &x) != 20*time.Minute {
			t.Fatal("legacy or invalid release extended")
		}
	}
	for _, bridge := range []string{"", filepath.Join(root, "benchmark-assets/adapters/science-probe/worker_bridge.py"), "/other/native_worker_bridge.py"} {
		x := cfg
		x.Bridge = bridge
		if controllerWatchdog(x, target) != 20*time.Minute {
			t.Fatal("non-native bridge extended")
		}
	}
	x := *target
	x.AgentBudgetSeconds = 301
	if controllerWatchdog(cfg, &x) != 20*time.Minute {
		t.Fatal("unrecognized agent budget extended")
	}
}

func TestExplicit180BudgetMatchesFrozenTargetAndCannotHideFailure(t *testing.T) {
	lease := platform.Lease{Mode: "scientific-smoke", RunID: "budget180", SHA256: strings.Repeat("a", 64), Public: 0}
	target := &platform.ScientificTarget{TaskIndex: 39, ReleaseID: "native-candidate-" + strings.Repeat("b", 64), AgentBudgetSeconds: 180}
	r := result{Mode: "scientific-smoke", RunID: lease.RunID, SHA: lease.SHA256, InfrastructureOK: true,
		StandardHarbor: true, Runtime: 180.1, AgentExit: 124, BudgetExhausted: true,
		AgentStopReason: "budget_exhausted", AgentBudgetSeconds: 180}
	r.Rollout.SHA, r.Rollout.Pending = strings.Repeat("c", 64), true
	check := func(value result, bound *platform.ScientificTarget) bool {
		return validateResult(&value, lease) == nil && validateCatalogBudget(&value, bound) == nil
	}
	if !check(r, target) {
		t.Fatal("explicit frozen 180 budget rejected")
	}
	for _, change := range []func(*result){
		func(v *result) { v.AgentBudgetSeconds = 300 },
		func(v *result) { v.AgentBudgetSeconds = 181 },
		func(v *result) { v.InfrastructureOK = false },
		func(v *result) { v.AgentStopReason = "provider_timeout" },
		func(v *result) { v.BudgetExhausted = false },
		func(v *result) { v.VerifierExit = 2 },
		func(v *result) { v.Rollout.Pending = false },
	} {
		bad := r
		change(&bad)
		if check(bad, target) {
			t.Fatal("budget change concealed invalid result")
		}
	}
	if check(r, nil) {
		t.Fatal("unbound budget stop accepted")
	}
	badTarget := *target
	badTarget.AgentBudgetSeconds = 300
	if check(r, &badTarget) {
		t.Fatal("180 result accepted for original 300 target")
	}
	early := r
	early.BudgetExhausted, early.AgentExit, early.AgentStopReason, early.Runtime = false, 137, "acceptance_early_stop", 45
	early.AcceptanceEarlyStopVerified, early.AcceptanceReadinessSHA = true, strings.Repeat("d", 64)
	if !check(early, target) || check(early, &badTarget) {
		t.Fatal("early stop budget binding broken")
	}
	root := "/trusted/project"
	if controllerWatchdog(Config{ProjectRoot: root, Bridge: filepath.Join(root, "benchmark-assets/adapters/science-probe/native_worker_bridge.py")}, target) != time.Hour {
		t.Fatal("180 agent budget truncated independent verifier allowance")
	}
}

func TestRejectResultForOtherRunOrSimulatedProvenance(t *testing.T) {
	l := platform.Lease{Mode: "scientific-smoke", RunID: "run", SHA256: strings.Repeat("a", 64)}
	r := result{Mode: "scientific-smoke", RunID: l.RunID, SHA: l.SHA256, InfrastructureOK: true, StandardHarbor: true, Runtime: 1}
	r.Rollout.Key = "rollouts/scientific-smoke/run/object.tar.gz"
	r.Rollout.SHA = strings.Repeat("b", 64)
	r.Rollout.Readback = true
	if err := validateResult(&r, l); err != nil {
		t.Fatal(err)
	}
	cases := []func(*result){func(x *result) { x.RunID = "other" }, func(x *result) { x.SHA = "other" }, func(x *result) { x.Mode = "simulated" }, func(x *result) { x.InfrastructureOK = false }, func(x *result) { x.Reward = 2 }, func(x *result) { x.AgentExit = 1 }, func(x *result) { x.Rollout.Readback = false }, func(x *result) { x.LeaderboardEligible = true }, func(x *result) { x.StandardHarbor = false }}
	for _, mutate := range cases {
		x := r
		mutate(&x)
		if validateResult(&x, l) == nil {
			t.Fatal("invalid result accepted", x)
		}
	}
}
func TestResourceReceiptSurvivesAtomicRewriteWithoutTokens(t *testing.T) {
	p := filepath.Join(t.TempDir(), "receipt.json")
	for _, state := range []string{"created", "destroyed"} {
		if err := writeReceipt(p, map[string]any{"run_id": "run", "resources": map[string]string{"sandbox": state}}); err != nil {
			t.Fatal(err)
		}
	}
	if info, err := os.Stat(p); err != nil || info.Mode().Perm() != 0o640 {
		t.Fatalf("cleanup receipt must be trusted-group readable, got %v %v", info.Mode().Perm(), err)
	}
	data, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	if json.Unmarshal(data, &result) != nil || result["resources"].(map[string]any)["sandbox"] != "destroyed" {
		t.Fatal("receipt corrupt")
	}
	fi, _ := os.Stat(p)
	if fi.Mode().Perm()&0o007 != 0 {
		t.Fatal("receipt is world-accessible")
	}
	if strings.Contains(string(data), "token") {
		t.Fatal("receipt contains capability")
	}
}
func TestRemoteGatewayConfigurationFailsBeforeControllerLaunch(t *testing.T) {
	if _, err := New(&platform.Store{}, Config{ProjectRoot: "/tmp/trusted", Python: "/usr/bin/python3", GatewayURL: "https://attacker.invalid"}); err == nil {
		t.Fatal("remote gateway accepted")
	}
}

func TestBoundedCatalogStopCannotHideInfrastructureFailure(t *testing.T) {
	l := platform.Lease{Mode: "scientific-smoke", RunID: "bounded-run", SHA256: strings.Repeat("a", 64), Public: 0}
	target := &platform.ScientificTarget{TaskIndex: 48, ReleaseID: platform.ScientificReleaseID, AgentBudgetSeconds: 300}
	r := result{Mode: "scientific-smoke", RunID: l.RunID, SHA: l.SHA256, InfrastructureOK: true, StandardHarbor: true,
		Runtime: 300, AgentExit: 124, BudgetExhausted: true, AgentStopReason: "budget_exhausted", AgentBudgetSeconds: 300}
	r.Rollout.SHA = strings.Repeat("b", 64)
	r.Rollout.Pending = true
	check := func(value result, lease platform.Lease, binding *platform.ScientificTarget) bool {
		return validateCatalogBudget(&value, binding) == nil && validateResult(&value, lease) == nil
	}
	if !check(r, l, target) {
		t.Fatal("explicit bounded stop rejected")
	}
	for _, mutate := range []func(*result){
		func(v *result) { v.BudgetExhausted = false },
		func(v *result) { v.AgentStopReason = "provider_timeout" },
		func(v *result) { v.AgentBudgetSeconds = 301 },
		func(v *result) { v.InfrastructureOK = false },
		func(v *result) { v.VerifierExit = 1 },
		func(v *result) { v.Rollout.Pending = false },
		func(v *result) { v.Reward = -1 },
	} {
		changed := r
		mutate(&changed)
		if check(changed, l, target) {
			t.Fatal("failure swallowed as budget stop")
		}
	}
	if check(r, l, nil) {
		t.Fatal("unbound budget stop accepted")
	}
	public := l
	public.Public = 1
	if check(r, public, target) {
		t.Fatal("legacy public nonzero exit accepted")
	}
	r.AgentExit = 0
	r.BudgetExhausted = false
	r.AgentStopReason = "completed"
	if !check(r, l, target) {
		t.Fatal("early successful completion rejected")
	}
	r.VerifierExit = 1
	r.VerifierScientificRejection = true
	if !check(r, l, target) {
		t.Fatal("trusted catalog scientific rejection refused")
	}
	for _, mutate := range []func(*result){
		func(v *result) { v.VerifierScientificRejection = false },
		func(v *result) { v.Reward = 1 },
		func(v *result) { v.VerifierExit = 2 },
		func(v *result) { v.VerifierExit = 0 },
		func(v *result) { v.InfrastructureOK = false },
	} {
		changed := r
		mutate(&changed)
		if check(changed, l, target) {
			t.Fatal("unclassified verifier failure accepted")
		}
	}
	if check(r, public, target) || check(r, l, nil) {
		t.Fatal("private scientific rejection exception escaped catalog scope")
	}
}

func TestEarlyAcceptanceStopRequiresNativeBindingAndVerifiedReadiness(t *testing.T) {
	l := platform.Lease{Mode: "scientific-smoke", RunID: "early-run", SHA256: strings.Repeat("a", 64), Public: 0}
	target := &platform.ScientificTarget{TaskIndex: 48, ReleaseID: "native-candidate-" + strings.Repeat("b", 64), AgentBudgetSeconds: 300}
	r := result{Mode: "scientific-smoke", RunID: l.RunID, SHA: l.SHA256,
		InfrastructureOK: true, StandardHarbor: true, Runtime: 45.5, AgentExit: 137,
		AgentStopReason: "acceptance_early_stop", AgentBudgetSeconds: 300,
		AcceptanceEarlyStopVerified: true, AcceptanceReadinessSHA: strings.Repeat("c", 64)}
	r.Rollout.SHA = strings.Repeat("d", 64)
	r.Rollout.Pending = true
	check := func(value result, lease platform.Lease, binding *platform.ScientificTarget) bool {
		return validateCatalogBudget(&value, binding) == nil && validateResult(&value, lease) == nil
	}
	for _, code := range []int{137, 143} {
		accepted := r
		accepted.AgentExit = code
		if !check(accepted, l, target) {
			t.Fatal("verified early stop rejected")
		}
	}
	for _, mutate := range []func(*result){
		func(v *result) { v.AcceptanceEarlyStopVerified = false },
		func(v *result) { v.AcceptanceReadinessSHA = "" },
		func(v *result) { v.AcceptanceReadinessSHA = strings.Repeat("z", 64) },
		func(v *result) { v.BudgetExhausted = true },
		func(v *result) { v.AgentStopReason = "completed" },
		func(v *result) { v.AgentStopReason = "budget_exhausted" },
		func(v *result) { v.AgentExit = 1 },
		func(v *result) { v.AgentExit = 124 },
		func(v *result) { v.Runtime = 0 },
		func(v *result) { v.Runtime = 300 },
		func(v *result) { v.AgentBudgetSeconds = 299 },
		func(v *result) { v.InfrastructureOK = false },
		func(v *result) { v.VerifierExit = 2 },
		func(v *result) { v.LeaderboardEligible = true },
	} {
		changed := r
		mutate(&changed)
		if check(changed, l, target) {
			t.Fatal("unproven or failed early termination accepted")
		}
	}
	legacy := *target
	legacy.ReleaseID = platform.ScientificReleaseID
	public := l
	public.Public = 1
	if check(r, l, nil) || check(r, l, &legacy) || check(r, public, target) {
		t.Fatal("early stop escaped native operator-bound diagnostic scope")
	}
}

func TestDiagnosticModelDoesNotAcceptArbitraryPaidFallback(t *testing.T) {
	for _, model := range []string{"other/paid-model", "minimax/minimax-m3.1-flash", "tencent/hy4-preview"} {
		if _, err := New(nil, Config{DiagnosticModel: model}); err == nil || err.Error() != "unsupported diagnostic model" {
			t.Fatalf("unexpected diagnostic policy result for %q: %v", model, err)
		}
	}
}

func TestNormalScientificPublicAndHiddenResultsKeepBoundedContract(t *testing.T) {
	for _, public := range []int{0, 1} {
		l := platform.Lease{Mode: "scientific", Kind: "full", RunID: "normal", SHA256: strings.Repeat("a", 64), Public: public, Ordinal: 26}
		r := result{Mode: "scientific", RunID: l.RunID, SHA: l.SHA256, InfrastructureOK: true, StandardHarbor: true, Runtime: 180.2, AgentExit: 124, BudgetExhausted: true, AgentStopReason: "budget_exhausted", AgentBudgetSeconds: 180, VerifierExit: 1, VerifierScientificRejection: true}
		r.Rollout.SHA, r.Rollout.Pending = strings.Repeat("b", 64), true
		if err := validateResult(&r, l); err != nil {
			t.Fatal("normal real bounded result rejected", err)
		}
		for _, change := range []func(*result){func(r *result) { r.Mode = "scientific-smoke" }, func(r *result) { r.Runtime = 5 }, func(r *result) { r.AgentExit = 1 }, func(r *result) { r.LeaderboardEligible = true }, func(r *result) { r.VerifierExit = 2 }, func(r *result) { r.AcceptanceEarlyStopVerified = true }} {
			bad := r
			change(&bad)
			if validateResult(&bad, l) == nil {
				t.Fatal("normal scientific invalid result accepted")
			}
		}
	}
}

func TestNativeLongWatchdogIncludesAllStages(t *testing.T) {
	target := &platform.ScientificTarget{ReleaseID: "native-candidate-" + strings.Repeat("a", 64), AgentBudgetSeconds: 10800, Formal: true, Limits: platform.TaskLimits{AgentSeconds: 10800, SetupSeconds: 600, BuildSeconds: 300, VerifierSeconds: 1200, CPUs: 2, MemoryMB: 4096, SourceSHA256: strings.Repeat("b", 64)}}
	if got := controllerWatchdog(Config{}, target); got != time.Duration(13800)*time.Second {
		t.Fatal("truncates native stages", got)
	}
	r := result{AgentBudgetSeconds: 10800, AgentExit: 124, AgentStopReason: "budget_exhausted", BudgetExhausted: true}
	if e := validateCatalogBudget(&r, target); e != nil {
		t.Fatal(e)
	}
	r.AcceptanceEarlyStopVerified = true
	if validateCatalogBudget(&r, target) == nil {
		t.Fatal("formal early stop accepted")
	}
}
