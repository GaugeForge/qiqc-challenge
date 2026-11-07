package review

import (
	"encoding/json"
	"strings"
	"testing"
)

// The literal example is part of the provider contract, so it must itself be
// valid JSON even though natural-language instructions follow the object.
func TestPolicyEmbeddedOutputExampleIsValidContractJSON(t *testing.T) {
	_, tail, ok := strings.Cut(SystemInstruction, "Exact output example: ")
	if !ok {
		t.Fatal("policy omitted its exact output example")
	}
	decoder := json.NewDecoder(strings.NewReader(tail))
	var suggestion Suggestion
	if err := decoder.Decode(&suggestion); err != nil {
		t.Fatalf("policy embeds invalid JSON example: %v", err)
	}
	if err := strictJSON([]byte(tail[:decoder.InputOffset()]), &suggestion); err != nil {
		t.Fatalf("example violates strict contract: %v", err)
	}
	if suggestion.SchemaVersion != "qiqc-advisory/v1" || len(suggestion.Findings) != 1 || suggestion.Summary == "" {
		t.Fatal("example does not illustrate expected output")
	}
	for _, finding := range suggestion.Findings {
		if !rules[finding.Rule] || finding.File == "" || finding.Confidence < 0 || finding.Confidence > 1 || finding.Explanation == "" {
			t.Fatal("example finding violates output contract")
		}
	}
}

func TestCorrectedPolicyHasDistinctEvidenceVersion(t *testing.T) {
	if PolicyVersion != "qiqc-readonly-advisory/v6" {
		t.Fatal("execution-context policy must not reuse prior blinded evaluation identity")
	}
}
