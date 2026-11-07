package platform

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestCandidateRosterMigrationKeepsHistoricalBatchAndEnablesFivePublic(t *testing.T) {
	s := testStore(t)
	old := automaticCatalog(t, s, 4)
	token, version := approved(t, s)
	batch, err := s.Confirm(token, version.ID, "infra", "scientific", "before-roster")
	if err != nil {
		t.Fatal(err)
	}
	next := "native-candidate-" + strings.Repeat("b", 64)
	if s.MigrateScientificCandidate(old.ReleaseID, next, "operator", "paper roster") == nil {
		t.Fatal("allowed migration with queued work")
	}
	if err := s.Cancel(token, batch["id"].(string)); err != nil {
		t.Fatal(err)
	}
	var before string
	if err := s.DB.QueryRow("SELECT json_group_array(json_object('run',run_id,'release',release_id,'task',task_id,'catalog',catalog_sha256)) FROM run_scientific_targets").Scan(&before); err != nil {
		t.Fatal(err)
	}
	if err := s.MigrateScientificCandidate(old.ReleaseID, next, "operator", "paper roster"); err != nil {
		t.Fatal(err)
	}
	if err := s.MigrateScientificCandidate(old.ReleaseID, next, "operator", "resume after config rename interruption"); err != nil {
		t.Fatal("idempotent migration retry failed", err)
	}
	if !validHistoricalRelease(s.DB, old.ReleaseID) || !validHistoricalRelease(s.DB, next) {
		t.Fatal("lost historical candidate registration")
	}
	if _, _, err := activeTaskSet(s.DB); err == nil {
		t.Fatal("old catalog remained active")
	}
	if _, err := s.GetBatch(token, batch["id"].(string)); err != nil {
		t.Fatal("historical batch unreadable", err)
	}
	old.ReleaseID = next
	old.Tasks[17].Public = true
	raw, _ := json.Marshal(old)
	if err := s.ConfigureScientificTaskSet(raw); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Confirm(token, version.ID, "public", "scientific", "after-roster"); err != nil {
		t.Fatal(err)
	}
	var after string
	if err := s.DB.QueryRow("SELECT json_group_array(json_object('run',run_id,'release',release_id,'task',task_id,'catalog',catalog_sha256)) FROM run_scientific_targets WHERE release_id!=?", next).Scan(&after); err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatal("historical target changed")
	}
	var count int
	s.DB.QueryRow("SELECT count(*) FROM audit WHERE action='native_candidate_migrated'").Scan(&count)
	if count != 1 {
		t.Fatalf("migration audit count %d", count)
	}
}

func TestCandidateRosterMigrationRejectsWrongPreviousFrozenAndMalformed(t *testing.T) {
	s := testStore(t)
	old := automaticCatalog(t, s, 4)
	next := "native-candidate-" + strings.Repeat("b", 64)
	for _, values := range [][4]string{
		{next, old.ReleaseID, "operator", "reason"},
		{old.ReleaseID, old.ReleaseID, "operator", "reason"},
		{old.ReleaseID, next, "", "reason"},
		{old.ReleaseID, "bad", "operator", "reason"},
	} {
		if s.MigrateScientificCandidate(values[0], values[1], values[2], values[3]) == nil {
			t.Fatal("invalid migration accepted")
		}
	}
	s.DB.Exec("INSERT INTO settings(key,value) VALUES('scientific_active_release',?)", "native-release-"+strings.Repeat("c", 64))
	if s.MigrateScientificCandidate(old.ReleaseID, next, "operator", "reason") == nil {
		t.Fatal("frozen release migrated")
	}
	var actual string
	s.DB.QueryRow("SELECT value FROM settings WHERE key='scientific_candidate'").Scan(&actual)
	if actual != old.ReleaseID {
		t.Fatal("failed migration altered identity")
	}
}
