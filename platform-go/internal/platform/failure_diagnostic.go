package platform

import (
	"database/sql"
	"errors"
	"strings"
)

// RunFailure is the safe, bounded failure metadata attached to a run. Raw
// provider bodies, prompts, hidden task identities and harness text never enter
// these columns; they remain in the private controller/failure evidence.
type RunFailure struct {
	Category string
	Phase    string
	Code     string
}

// ExecutionFailure lets the Go worker distinguish a real infrastructure
// failure from a participant harness or a valid-but-incomplete scientific
// result. Only the platform category is eligible for the one automatic retry.
type ExecutionFailure struct {
	Category  string
	Phase     string
	Code      string
	Message   string
	Retryable bool
}

func (e *ExecutionFailure) Error() string {
	if e == nil {
		return ""
	}
	if e.Message != "" {
		return e.Message
	}
	return "execution failed: " + e.Category
}

func AsExecutionFailure(err error) (*ExecutionFailure, bool) {
	var failure *ExecutionFailure
	if errors.As(err, &failure) {
		return failure, true
	}
	return nil, false
}

func receiptString(receipt map[string]any, key string) string {
	value, _ := receipt[key].(string)
	return strings.TrimSpace(value)
}

func receiptBool(receipt map[string]any, key string) bool {
	value, _ := receipt[key].(bool)
	return value
}

// DiagnosticFromReceipt classifies only trusted controller fields. The failure
// code is intentionally kept staff-only for the participant view.
func DiagnosticFromReceipt(receipt map[string]any) RunFailure {
	phase := receiptString(receipt, "phase")
	code := receiptString(receipt, "controller_error_detail")
	if code == "" {
		code = receiptString(receipt, "controller_error_class")
	}
	lowerCode := strings.ToLower(code)
	lowerPhase := strings.ToLower(phase)
	budget := receiptBool(receipt, "budget_exhausted") || strings.Contains(lowerCode, "budget")

	category := "platform"
	switch {
	case containsAny(lowerCode, "pack", "archive", "evidence") ||
		containsAny(lowerPhase, "packing_rollout", "uploading_rollout", "failure_evidence"):
		category = "evidence"
	case containsAny(lowerCode, "model", "incomplete_stream", "provider"):
		category = "model"
	case containsAny(lowerCode, "verifier") || strings.Contains(lowerPhase, "verif"):
		category = "verifier"
	case containsAny(lowerCode, "cancelled", "cancel"):
		category = "cancelled"
	case budget:
		category = "budget"
	case containsAny(lowerCode, "runner", "adapter", "harness") || lowerPhase == "native_harbor_trial":
		category = "harness"
	}
	return RunFailure{Category: category, Phase: phase, Code: code}
}

func containsAny(value string, needles ...string) bool {
	for _, needle := range needles {
		if strings.Contains(value, needle) {
			return true
		}
	}
	return false
}

// NewExecutionFailure converts a trusted receipt into the worker-visible error
// category. Only a platform failure is automatically retryable, and the worker
// additionally caps that retry at one additional attempt.
func NewExecutionFailure(receipt map[string]any, message string, retryable bool) *ExecutionFailure {
	diagnostic := DiagnosticFromReceipt(receipt)
	if diagnostic.Category == "" {
		diagnostic.Category = "platform"
	}
	return &ExecutionFailure{
		Category:  diagnostic.Category,
		Phase:     diagnostic.Phase,
		Code:      diagnostic.Code,
		Message:   message,
		Retryable: retryable && diagnostic.Category == "platform",
	}
}

// RecordRunDiagnostic is best-effort metadata. It never changes the run state
// and is ignored if the attempt has already been superseded.
func (s *Store) RecordRunDiagnostic(runID string, attempt int, diagnostic RunFailure) error {
	if runID == "" || attempt < 1 || diagnostic.Category == "" {
		return nil
	}
	return s.write(func(tx *sql.Tx) error {
		_, err := tx.Exec("UPDATE runs SET failure_category=?,failure_phase=?,failure_code=? WHERE id=? AND attempt=?",
			diagnostic.Category, diagnostic.Phase, diagnostic.Code, runID, attempt)
		return err
	})
}

// UpgradeRunDiagnostic replaces only the generic platform attention marker with
// a trusted category when the failure-artifact finalizer won the race with the
// executor. It never changes state, a cancelled task or a more specific error.
func (s *Store) UpgradeRunDiagnostic(runID string, attempt int, diagnostic RunFailure) (bool, error) {
	if runID == "" || attempt < 1 || diagnostic.Category == "" {
		return false, nil
	}
	updated := false
	err := s.write(func(tx *sql.Tx) error {
		result, err := tx.Exec("UPDATE runs SET error=?,failure_category=?,failure_phase=?,failure_code=? WHERE id=? AND attempt=? AND state='attention' AND error='platform:operator_review_required'",
			diagnostic.Category+":operator_review_required", diagnostic.Category, diagnostic.Phase, diagnostic.Code, runID, attempt)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		updated = n == 1
		return err
	})
	return updated, err
}

func publicPhaseLabel(phase string) string {
	lower := strings.ToLower(strings.TrimSpace(phase))
	switch {
	case lower == "":
		return "task execution"
	case containsAny(lower, "starting", "preparation", "precreate", "creating_native_compose_host"):
		return "startup"
	case containsAny(lower, "native_harbor_trial", "agent"):
		return "agent execution"
	case containsAny(lower, "verif"):
		return "verification"
	case containsAny(lower, "pack", "upload", "evidence", "archive"):
		return "evidence packaging"
	case containsAny(lower, "validating_native_receipt", "reading_native_receipt"):
		return "result validation"
	case containsAny(lower, "cancel", "cleanup"):
		return "cancellation"
	default:
		return "task execution"
	}
}

func publicCategoryLabel(category string) string {
	switch category {
	case "harness":
		return "harness"
	case "model":
		return "model service"
	case "evidence":
		return "evidence"
	case "cancelled":
		return "cancelled"
	case "budget":
		return "budget"
	case "verifier":
		return "verifier"
	default:
		return "platform"
	}
}

func publicFailureMessage(category string) string {
	switch category {
	case "harness":
		return "The submitted harness stopped during agent execution."
	case "model":
		return "A model response stream could not be recovered during the task."
	case "evidence":
		return "The platform could not package the run evidence within its limits."
	case "cancelled":
		return "The task was cancelled."
	case "budget":
		return "The agent used its full task budget without completing."
	case "verifier":
		return "The verifier did not complete the task."
	default:
		return "The task failed during platform startup or recovery."
	}
}

func publicFailureCode(category, phase string) string {
	switch category {
	case "harness":
		return "harness_process_stopped"
	case "model":
		return "model_response_stream_interrupted"
	case "evidence":
		return "evidence_packaging_failed"
	case "cancelled":
		return "task_cancelled"
	case "budget":
		return "agent_budget_exhausted"
	case "verifier":
		return "verifier_infrastructure_failure"
	default:
		switch publicPhaseLabel(phase) {
		case "startup":
			return "platform_startup_failed"
		case "evidence packaging":
			return "evidence_packaging_failed"
		case "verification":
			return "verifier_infrastructure_failure"
		case "agent execution":
			return "agent_execution_failed"
		default:
			return "platform_recovery_failed"
		}
	}
}

// PublicFailureDetail returns only bounded, actionable location information.
// It never includes provider bodies, hidden task identities, raw harness output
// or remediation advice.
func PublicFailureDetail(state, reason, category, phase, code string) (string, string, string, string) {
	if state == "cancelled" {
		if reason == "cancelled:operator" {
			return "cancelled", "cancellation", "task_cancelled", "This task was cancelled by the team."
		}
		return "cancelled", "evaluation", "task_cancelled", "This evaluation was cancelled."
	}
	if state != "attention" {
		return "", "", "", ""
	}
	if category == "" {
		category = "platform"
	}
	return publicCategoryLabel(category), publicPhaseLabel(phase), publicFailureCode(category, phase), publicFailureMessage(category)
}
