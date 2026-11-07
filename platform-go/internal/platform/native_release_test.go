package platform

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNativeActivationDrainedAdmissionPointerAndHistoricalIdentity(t *testing.T) {
	s := testStore(t)
	candidate := "native-candidate-" + strings.Repeat("a", 64)
	release := "native-release-" + strings.Repeat("b", 64)
	if err := s.ConfigureScientificCandidate(candidate); err != nil {
		t.Fatal(err)
	}
	team, v := approved(t, s)
	admin := readyUser(t, s, "native-admin@example.test", "admin")
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	if err := s.AdminAction(admin, targetAction(v.ID, 48), "local"); err != nil {
		t.Fatal(err)
	}
	first, err := s.Confirm(team, v.ID, "infra", "scientific-smoke", "before")
	if err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(t.TempDir(), "release.json")
	raw := []byte(`{"frozen":true}`)
	os.WriteFile(record, raw, 0600)
	sum := sha256.Sum256(raw)
	digest := hex.EncodeToString(sum[:])
	if s.ActivateNativeRelease(release, candidate, digest, "operator", "49 accepted") == nil {
		t.Fatal("activated over active work")
	}
	if _, err = s.DB.Exec("UPDATE runs SET state='completed' WHERE batch_id=?", first["id"]); err != nil {
		t.Fatal(err)
	}
	if _, err = s.DB.Exec("UPDATE batches SET status='completed' WHERE id=?", first["id"]); err != nil {
		t.Fatal(err)
	}
	if err = s.ActivateNativeRelease(release, candidate, digest, "operator", "49 accepted"); err != nil {
		t.Fatal(err)
	}
	if s.ActivateNativeRelease(release, candidate, strings.Repeat("c", 64), "operator", "changed") == nil {
		t.Fatal("release mutation accepted")
	}
	pointer := filepath.Join(t.TempDir(), "active-release.json")
	t.Setenv("QIQC_NATIVE_RELEASE_POINTER", pointer)
	if _, err = scientificRelease(s.DB); err == nil {
		t.Fatal("missing pointer accepted")
	}
	data, _ := json.Marshal(map[string]string{"release_id": release, "release_path": record, "release_sha256": digest})
	os.WriteFile(pointer, data, 0600)
	if got, err := scientificRelease(s.DB); err != nil || got != release {
		t.Fatal(got, err)
	}
	if old, err := readScientificTarget(s.DB, first["id"].(string)); err != nil || old.ReleaseID != candidate {
		t.Fatal("historical identity changed", old, err)
	}
	// Same reviewed version retains its immutable candidate binding, but a new
	// admission snapshots the accepted frozen runtime after operator activation.
	var teamID string
	s.DB.QueryRow("SELECT team_id FROM versions WHERE id=?", v.ID).Scan(&teamID)
	if err := s.AdminAction(admin, Action{Action: "quota", Target: teamID, Kind: "infra", Delta: 1, Actor: "operator", Reason: "post-activation acceptance"}, "local"); err != nil {
		t.Fatal(err)
	}
	second, err := s.Confirm(team, v.ID, "infra", "scientific-smoke", "after")
	if err != nil {
		t.Fatal(err)
	}
	target, err := readScientificTarget(s.DB, second["id"].(string))
	if err != nil || target.ReleaseID != release {
		t.Fatal(target, err)
	}
	var versionRelease string
	s.DB.QueryRow("SELECT release_id FROM scientific_targets WHERE version_id=?", v.ID).Scan(&versionRelease)
	if versionRelease != candidate {
		t.Fatal("reviewed version target rewritten")
	}
	var audits int
	s.DB.QueryRow("SELECT count(*) FROM audit WHERE action='native_release_activated'").Scan(&audits)
	if audits != 1 {
		t.Fatal("activation audit missing", audits)
	}
	os.Remove(pointer)
	linked := filepath.Join(t.TempDir(), "pointer.json")
	os.WriteFile(linked, data, 0600)
	os.Symlink(linked, pointer)
	if _, err = scientificRelease(s.DB); err == nil {
		t.Fatal("symlinked pointer accepted")
	}
	os.Remove(pointer)
	os.WriteFile(pointer, data, 0600)
	os.WriteFile(record, []byte(`changed`), 0600)
	if _, err = scientificRelease(s.DB); err == nil {
		t.Fatal("changed release admitted")
	}
}

func TestNativeRollbackPreservesRegistryAndAudit(t *testing.T) {
	s := testStore(t)
	candidate := "native-candidate-" + strings.Repeat("a", 64)
	release := "native-release-" + strings.Repeat("b", 64)
	if err := s.ConfigureScientificCandidate(candidate); err != nil {
		t.Fatal(err)
	}
	if err := s.ActivateNativeRelease(release, candidate, strings.Repeat("c", 64), "operator", "accepted all49"); err != nil {
		t.Fatal(err)
	}
	if err := s.RollbackNativeRelease("native-release-"+strings.Repeat("d", 64), candidate, "operator", "bad target"); err == nil {
		t.Fatal("unregistered rollback accepted")
	}
	if err := s.RollbackNativeRelease("", candidate, "operator", "pointer startup failed"); err != nil {
		t.Fatal(err)
	}
	if got, err := scientificRelease(s.DB); err != nil || got != candidate {
		t.Fatal(got, err)
	}
	if !validHistoricalRelease(s.DB, release) {
		t.Fatal("rollback erased release registry")
	}
	var audits int
	s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action IN ('native_release_activated','native_release_rollback')").Scan(&audits)
	if audits != 2 {
		t.Fatal("rollback erased audits", audits)
	}
	rows, err := s.DB.Query("SELECT before_json,after_json FROM audit WHERE action IN ('native_release_activated','native_release_rollback')")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var before, after string
		if err := rows.Scan(&before, &after); err != nil {
			t.Fatal(err)
		}
		if !json.Valid([]byte(before)) || !json.Valid([]byte(after)) {
			t.Fatal("invalid audit JSON")
		}
	}
}
