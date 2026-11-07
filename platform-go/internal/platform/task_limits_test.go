package platform

import "testing"

func TestNativeBudgetPolicyKeepsPublicTaskLimitsDuringPractice(t *testing.T) {
	catalog := ScientificTaskSet{Schema: "qiqc-scientific-task-set/v2", AgentBudgetSeconds: 180}
	public := ScientificTask{Limits: TaskLimits{AgentSeconds: 1800}}
	infra := ScientificTask{Limits: TaskLimits{AgentSeconds: 9000}}
	if got := catalog.budget(public, "practice", "public"); got != 1800 {
		t.Fatalf("public practice budget = %d, want native 1800", got)
	}
	if got := catalog.budget(public, "competition", "public"); got != 1800 {
		t.Fatalf("public competition budget = %d, want native 1800", got)
	}
	if got := catalog.budget(infra, "practice", "infra"); got != 180 {
		t.Fatalf("infra practice budget = %d, want fixed 180", got)
	}
}
