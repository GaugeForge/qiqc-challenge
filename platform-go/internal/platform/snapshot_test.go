package platform

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestAdminSnapshotRequiresChangedAdminAndNeverExposesSecrets(t *testing.T) {
	s := testStore(t)
	teamToken := readyUser(t, s, "captain-snapshot@example.test", "team")
	initialAdmin := account(t, s, "initial-admin@example.test", "admin")
	initialToken, _, err := s.Login(initialAdmin.Email, "Initial-password-123", "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	for _, token := range []string{"", teamToken, initialToken} {
		if _, err := s.AdminSnapshot(token); err == nil {
			t.Fatal("unauthorized admin snapshot")
		}
	}
	adminToken := readyUser(t, s, "snapshot-admin@example.test", "admin")
	s.DB.Exec("INSERT INTO run_tokens VALUES('RUN_SECRET_MARKER','synthetic-run',1,'test',9999999999,0)")
	// Put a marker in an unrelated participant hash without invalidating the admin.
	u, _ := s.Session(teamToken)
	s.DB.Exec("UPDATE users SET password='PASSWORD_SECRET_MARKER' WHERE id=?", u.ID)
	s.DB.Exec("INSERT INTO sessions VALUES('SESSION_SECRET_MARKER',?,9999999999,'127.0.0.1')", u.ID)
	snap, err := s.AdminSnapshot(adminToken)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(snap)
	for _, secret := range []string{"RUN_SECRET_MARKER", "PASSWORD_SECRET_MARKER", "SESSION_SECRET_MARKER", adminToken} {
		if strings.Contains(string(data), secret) {
			t.Fatal("secret leaked", secret)
		}
	}
	for _, key := range []string{"versions", "batches", "teams", "usage_rows", "audit", "appeals", "outbox", "pricing", "paused", "slots", "model", "disk_free_bytes", "storage", "e2b", "ai_review", "real_cloud_verified"} {
		if _, ok := snap[key]; !ok {
			t.Fatal("missing frontend field", key)
		}
	}
	if snap["real_cloud_verified"] != false {
		t.Fatal("fresh database pretends cloud verified")
	}
}

func TestAdminSnapshotHasReviewDetailsRunStatesErrorsAndUnknownPrices(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	admin := readyUser(t, s, "admin-details@example.test", "admin")
	b, err := s.Confirm(token, v.ID, "infra", "simulated", "snapshot-run")
	if err != nil {
		t.Fatal(err)
	}
	lease := mustClaim(t, s, "test", 4)
	if _, err = s.DB.Exec("UPDATE runs SET state='attention',error='platform:test_failure' WHERE id=?", lease.RunID); err != nil {
		t.Fatal(err)
	}
	s.DB.Exec("UPDATE batches SET status='attention' WHERE id=?", b["id"])
	s.DB.Exec("INSERT INTO usage(id,run_id,attempt,model,created,status,input_tokens,output_tokens,cached_tokens,amount,price_version,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", id(), lease.RunID, 1, "test", s.now(), 429, nil, nil, nil, nil, nil, "fake")
	snap, err := s.AdminSnapshot(admin)
	if err != nil {
		t.Fatal(err)
	}
	batches := snap["batches"].([]map[string]any)
	if len(batches) != 1 || batches[0]["status"] != "attention" || batches[0]["run_states"].(map[string]int64)["attention"] != 1 {
		t.Fatal("lost internal diagnostics", batches)
	}
	if len(batches[0]["errors"].([]map[string]any)) != 1 {
		t.Fatal("missing error")
	}
	versions := snap["versions"].([]map[string]any)
	if len(versions) != 1 {
		t.Fatal("missing review")
	}
	report := versions[0]["report"].(map[string]any)
	if report["risks"] == nil {
		t.Fatal("no risk list")
	}
	usage := snap["usage"].(map[string]any)
	if usage["known_amount"] != nil || usage["unpriced"] != int64(1) || usage["calls"] != int64(1) {
		t.Fatal("unknown billing became free", usage)
	}
}

func TestSnapshotEmptyListsAndConfigClosureRemainFrontendCompatible(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "empty-admin@example.test", "admin")
	snapshot, err := s.AdminSnapshot(admin)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(snapshot)
	for _, key := range []string{"versions", "batches", "teams", "usage_rows", "audit", "appeals", "outbox"} {
		if !strings.Contains(string(raw), `"`+key+`":[]`) {
			t.Fatal("empty list encoded null", key, string(raw))
		}
	}
	cfg, err := s.Config()
	if err != nil {
		t.Fatal(err)
	}
	if cfg["cloud_ready"] != false || cfg["e2b_smoke_verified"] != false || cfg["always_open"] != true || cfg["global_task_slots"] != 4 {
		t.Fatal("inaccurate new config", cfg)
	}
	s.DB.Exec("UPDATE settings SET value='true' WHERE key='competition_closed'")
	s.DB.Exec("INSERT INTO settings(key,value) VALUES('global_task_slots','7')")
	s.DB.Exec(`INSERT INTO settings(key,value) VALUES('deployment_evidence','{"e2b_smoke_verified":true,"hy4_smoke_verified":true}')`)
	cfg, err = s.Config()
	if err != nil {
		t.Fatal(err)
	}
	if cfg["always_open"] != false || cfg["competition_closed"] != true || cfg["global_task_slots"] != 7 || cfg["e2b_smoke_verified"] != true || cfg["cloud_ready"] != false {
		t.Fatal("config source mismatch", cfg)
	}
}

func TestSnapshotMalformedSettingsFailClosedAndInvalidReviewVisible(t *testing.T) {
	s := testStore(t)
	_, v := approved(t, s)
	admin := readyUser(t, s, "corrupt-admin@example.test", "admin")
	s.DB.Exec("UPDATE versions SET report='invalid-json' WHERE id=?", v.ID)
	snap, err := s.AdminSnapshot(admin)
	if err != nil {
		t.Fatal(err)
	}
	report := snap["versions"].([]map[string]any)[0]["report"].(map[string]any)
	if report["invalid"] != true {
		t.Fatal("corrupt review silently hidden")
	}
	s.DB.Exec("UPDATE settings SET value='null' WHERE key='competition_closed'")
	if _, err = s.Config(); err == nil {
		t.Fatal("invalid closure appears open")
	}
}

func TestSnapshotInvalidEvidenceNeverClaimsVerifiedAndPayloadCredentialsRedacted(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "redact-admin@example.test", "admin")
	s.DB.Exec(`INSERT INTO settings(key,value) VALUES('deployment_evidence','{"e2b_smoke_verified":true,"hy4_smoke_verified":"wrong-type"}')`)
	cfg, err := s.Config()
	if err != nil || cfg["e2b_smoke_verified"] != false {
		t.Fatal("partially decoded evidence treated as verified")
	}
	s.DB.Exec(`INSERT INTO outbox(id,event,payload,created) VALUES(?,'test','{"initial_password":"SECRET_TO_REDACT","nested":{"token":"TOKEN_TO_REDACT"},"batch_id":"visible"}',?)`, id(), s.now())
	snap, err := s.AdminSnapshot(admin)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(snap)
	if strings.Contains(string(data), "SECRET_TO_REDACT") || strings.Contains(string(data), "TOKEN_TO_REDACT") || !strings.Contains(string(data), "visible") {
		t.Fatal("payload credential redaction failed")
	}
}
