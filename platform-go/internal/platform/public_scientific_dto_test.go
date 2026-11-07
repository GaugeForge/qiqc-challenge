package platform

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestNormalScientificPublicLabelsAndHiddenFilter(t *testing.T) {
	for _, kind := range []string{"infra", "public", "full"} {
		t.Run(kind, func(t *testing.T) {
			s := testStore(t)
			catalog := automaticCatalog(t, s, 5)
			token, v := approved(t, s)
			b, err := s.Confirm(token, v.ID, kind, "scientific", "public-label")
			if err != nil {
				t.Fatal(err)
			}
			// This test exercises the completed DTO, not execution or scoring.
			if _, err = s.DB.Exec("UPDATE batches SET status='completed',passed=0,mean='180.000000' WHERE id=?", b["id"]); err != nil {
				t.Fatal(err)
			}
			view, err := s.GetBatch(token, b["id"].(string))
			if err != nil {
				t.Fatal(err)
			}
			want := []int{}
			if kind == "public" {
				// Public batches expose five participant-facing positions; the
				// catalog task identities stay private.
				want = []int{0, 1, 2, 3, 4}
			}
			runs := view["public_runs"].([]map[string]any)
			if len(runs) != len(want) {
				t.Fatal("public visibility mismatch", runs)
			}
			for i, run := range runs {
				if run["ordinal"] != want[i] || run["label"] != fmt.Sprintf("Public task %d", i+1) {
					t.Fatal("misleading scientific label or wrong trace route", run)
				}
			}
			raw, _ := json.Marshal(view)
			if strings.Contains(string(raw), "Mock task") {
				t.Fatal("normal scientific execution labeled as mock")
			}
			for _, task := range catalog.Tasks {
				if strings.Contains(string(raw), task.TaskID) {
					t.Fatal("catalog identity exposed in public DTO")
				}
			}
		})
	}
}

func TestPublicBatchViewNumbersCompletedAttempts(t *testing.T) {
	s := testStore(t)
	automaticCatalog(t, s, 5)
	token, version := approved(t, s)
	for i := 1; i <= 2; i++ {
		batch, err := s.Confirm(token, version.ID, "public", "scientific", fmt.Sprintf("public-attempt-%d", i))
		if err != nil {
			t.Fatal(err)
		}
		if _, err = s.DB.Exec("UPDATE batches SET status='completed',passed=0,mean='180.000000' WHERE id=?", batch["id"]); err != nil {
			t.Fatal(err)
		}
		view, err := s.GetBatch(token, batch["id"].(string))
		if err != nil {
			t.Fatal(err)
		}
		if view["public_test_number"] != i {
			t.Fatalf("public attempt number = %#v, want %d", view["public_test_number"], i)
		}
	}
}

func TestScientificInterruptionIsVisibleWithoutPartialOrPrivateEvidence(t *testing.T) {
	s := testStore(t)
	automaticCatalog(t, s, 5)
	token, version := approved(t, s)
	b, err := s.Confirm(token, version.ID, "full", "scientific", "interrupted-view")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.DB.Exec("UPDATE batches SET status='attention',passed=3,mean='1.000000' WHERE id=?", b["id"]); err != nil {
		t.Fatal(err)
	}
	if _, err = s.DB.Exec("UPDATE runs SET state='attention',error='private-diagnostic-fixture' WHERE batch_id=?", b["id"]); err != nil {
		t.Fatal(err)
	}
	view, err := s.GetBatch(token, b["id"].(string))
	if err != nil {
		t.Fatal(err)
	}
	if view["status"] != "attention" || view["failure_code"] != "evaluation_paused" || view["status_message"] == "" || view["score"] != nil || len(view["public_runs"].([]map[string]any)) != 0 {
		t.Fatal("interruption must be visible without partial results", view)
	}
	raw, _ := json.Marshal(view)
	if strings.Contains(string(raw), "private-diagnostic-fixture") {
		t.Fatal("private error exposed")
	}
}

func TestCurrentRuleVersionDoesNotRewriteHistoricalPolicy(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	b, err := s.Confirm(token, v.ID, "infra", "simulated", "historical-rule")
	if err != nil {
		t.Fatal(err)
	}
	var raw string
	if err = s.DB.QueryRow("SELECT policy_json FROM batch_policies WHERE batch_id=?", b["id"]).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var p BatchPolicy
	if err = json.Unmarshal([]byte(raw), &p); err != nil || p.RuleVersion != CurrentRuleVersion {
		t.Fatal("new batch did not pin current PRD", err, p.RuleVersion)
	}
	// Install a previously accepted snapshot as an upgrade fixture. Production
	// only inserts new snapshots and must never rewrite this historical record.
	p.RuleVersion = "PRD 1.2"
	historical, _ := json.Marshal(p)
	historicalSHA := shaBytes(historical)
	if _, err = s.DB.Exec("UPDATE batch_policies SET policy_json=?,policy_sha256=? WHERE batch_id=?", string(historical), historicalSHA, b["id"]); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	config, err := reopened.Config()
	if err != nil || config["rule_version"] != CurrentRuleVersion {
		t.Fatal("current config has stale version", err)
	}
	replay, err := reopened.Confirm(token, v.ID, "infra", "simulated", "historical-rule")
	if err != nil || replay["policy_sha256"] != historicalSHA {
		t.Fatal("replay changed historical policy", err)
	}
	var after, afterSHA string
	if err = reopened.DB.QueryRow("SELECT policy_json,policy_sha256 FROM batch_policies WHERE batch_id=?", b["id"]).Scan(&after, &afterSHA); err != nil || after != string(historical) || afterSHA != historicalSHA {
		t.Fatal("historical policy changed across restart", err)
	}
}
