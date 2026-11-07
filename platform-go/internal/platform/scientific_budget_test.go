package platform

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func TestScientific180BudgetIsOperatorOnlyFrozenAndRestartSafe(t *testing.T) {
	s := testStore(t)
	team, version := approved(t, s)
	admin := readyUser(t, s, "budget-admin@example.test", "admin")
	s.ScienceHashes = map[string]bool{version.SHA256: true}
	if s.ScientificAgentBudget() != 300 {
		t.Fatal("default changed")
	}
	for _, bad := range []int{0, -1, 179, 181, 299, 301, 3600} {
		if s.ConfigureScientificAgentBudget(bad) == nil {
			t.Fatal("arbitrary budget accepted")
		}
	}
	if err := s.ConfigureScientificAgentBudget(180); err != nil {
		t.Fatal(err)
	}
	config, err := s.Config()
	if err != nil || config["scientific_agent_budget_seconds"] != 180 {
		t.Fatal("UI config budget missing")
	}
	mux := http.NewServeMux()
	s.RegisterBusiness(mux, http.NotFoundHandler())
	payload, _ := json.Marshal(targetAction(version.ID, 39))
	// Even an administrator cannot override the trusted process budget by
	// adding a budget field to the binding request.
	var body map[string]any
	_ = json.Unmarshal(payload, &body)
	body["agent_budget_seconds"] = 300
	payload, _ = json.Marshal(body)
	response := call(t, mux, "POST", "/api/admin/actions", admin, string(payload))
	if response.Code != 200 && response.Code != 400 {
		t.Fatal("unexpected binding status", response.Code)
	}
	if response.Code == 400 {
		if err := s.AdminAction(admin, targetAction(version.ID, 39), "local"); err != nil {
			t.Fatal(err)
		}
	}
	batch, err := s.Confirm(team, version.ID, "infra", "scientific-smoke", "180-frozen")
	if err != nil || batch["agent_budget_seconds"] != 180 {
		t.Fatal("budget not frozen", err)
	}
	if err := s.ConfigureScientificAgentBudget(300); err != nil {
		t.Fatal(err)
	}
	if status(s.AdminAction(admin, targetAction(version.ID, 39), "local")) != 409 {
		t.Fatal("existing target rebound")
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	lease := mustClaim(t, reopened, "budget-worker", 1)
	target, err := reopened.ScientificTargetForLease(*lease)
	if err != nil || target.AgentBudgetSeconds != 180 {
		t.Fatal("worker lost immutable 180 budget", err)
	}
	for _, table := range []string{"scientific_targets", "batch_scientific_targets"} {
		if _, err := s.DB.Exec("UPDATE " + table + " SET agent_budget_seconds=181"); err == nil {
			t.Fatal("database accepted arbitrary budget")
		}
	}
}

func downgradeBudgetTablesForTest(t *testing.T, s *Store, corruptSecond bool) {
	t.Helper()
	if err := s.write(func(tx *sql.Tx) error {
		for _, table := range []string{"scientific_targets", "batch_scientific_targets"} {
			prefix := "CREATE TABLE IF NOT EXISTS " + table + "("
			start := strings.Index(schema, prefix)
			end := strings.Index(schema[start:], ";")
			definition := strings.Replace(schema[start:start+end+1], prefix, "CREATE TABLE "+table+"_old(", 1)
			check := "agent_budget_seconds=300"
			if corruptSecond && table == "batch_scientific_targets" {
				check = "agent_budget_seconds>=0"
			}
			definition = strings.Replace(definition, "agent_budget_seconds IN (180,300)", check, 1)
			for _, query := range []string{definition, "INSERT INTO " + table + "_old SELECT * FROM " + table,
				"DROP TABLE " + table, "ALTER TABLE " + table + "_old RENAME TO " + table} {
				if _, err := tx.Exec(query); err != nil {
					return err
				}
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func TestScientificBudgetMigrationPreservesExistingTargetsAndForeignKeys(t *testing.T) {
	s := testStore(t)
	team, version := approved(t, s)
	admin := readyUser(t, s, "migration-admin@example.test", "admin")
	s.ScienceHashes = map[string]bool{version.SHA256: true}
	if err := s.AdminAction(admin, targetAction(version.ID, 7), "local"); err != nil {
		t.Fatal(err)
	}
	batch, err := s.Confirm(team, version.ID, "infra", "scientific-smoke", "before-migration")
	if err != nil {
		t.Fatal(err)
	}
	downgradeBudgetTablesForTest(t, s, false)
	for attempt := 0; attempt < 2; attempt++ {
		reopened, err := Open(s.Path)
		if err != nil {
			t.Fatal(err)
		}
		for _, table := range []string{"scientific_targets", "batch_scientific_targets"} {
			var budget, index int
			var sha string
			if err := reopened.DB.QueryRow("SELECT agent_budget_seconds,task_index,submission_sha256 FROM "+table).Scan(&budget, &index, &sha); err != nil || budget != 300 || index != 7 || sha != version.SHA256 {
				t.Fatal("migration changed frozen data", err)
			}
		}
		if _, err := reopened.DB.Exec("INSERT INTO batch_scientific_targets VALUES('nonexistent',?,7,?,180)", version.SHA256, ScientificReleaseID); err == nil {
			t.Fatal("foreign key protection lost")
		}
		if attempt == 0 {
			if _, err := reopened.DB.Exec("UPDATE batch_scientific_targets SET agent_budget_seconds=180 WHERE batch_id=?", batch["id"]); err != nil {
				t.Fatal("migrated constraint rejects 180", err)
			}
			if _, err := reopened.DB.Exec("UPDATE batch_scientific_targets SET agent_budget_seconds=300 WHERE batch_id=?", batch["id"]); err != nil {
				t.Fatal(err)
			}
		}
		reopened.Close()
	}
}

func TestScientificBudgetMigrationRollsBackBothTablesOnUnknownSchema(t *testing.T) {
	s := testStore(t)
	downgradeBudgetTablesForTest(t, s, true)
	if err := s.ensureScientificBudgetSchema(); err == nil {
		t.Fatal("unknown schema silently migrated")
	}
	var definition string
	if err := s.DB.QueryRow("SELECT sql FROM sqlite_master WHERE name='scientific_targets'").Scan(&definition); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(definition, "agent_budget_seconds=300") {
		t.Fatal("partial migration committed")
	}
}
