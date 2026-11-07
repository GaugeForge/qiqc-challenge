package platform

import (
	"errors"
	"testing"
)

func TestAdminHealthSurvivesDiskProbeFailureAndKeepsUploadClosed(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "health-admin@example.test", "admin")
	team := readyUser(t, s, "health-team@example.test", "team")
	s.DiskProbe = func(string) (DiskSpace, error) { return DiskSpace{}, errors.New("private OS diagnostic never exposed") }
	d, err := s.AdminSnapshot(admin)
	if err != nil {
		t.Fatal(err)
	}
	health := d["operational_health"].(map[string]any)
	if health["disk_status"] != "unavailable" || health["delivery"] != "dashboard_only" {
		t.Fatal("missing degraded monitor", health)
	}
	if err = s.CheckUploadDisk(); status(err) != 503 {
		t.Fatal("disk failure admitted upload", err)
	}
	if _, err = s.AdminSnapshot(team); status(err) != 403 {
		t.Fatal("team read monitoring", err)
	}
	s.DiskProbe = func(string) (DiskSpace, error) {
		return DiskSpace{AvailableBytes: DefaultUploadReserveBytes, TotalBytes: 1 << 30}, nil
	}
	d, err = s.AdminSnapshot(admin)
	if err != nil || d["operational_health"].(map[string]any)["disk_status"] != "low" {
		t.Fatal("low disk not alerted", err)
	}
}

func TestOperationalHealthRemoteCleanupOnlyCountsUnresolvedRealRuns(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	b, err := s.Confirm(tok, v.ID, "infra", "simulated", "health-scope")
	if err != nil {
		t.Fatal(err)
	}
	bid := b["id"].(string)
	if _, err = s.DB.Exec("UPDATE runs SET state='cancelled',error=? WHERE batch_id=?", lostWorkerError, bid); err != nil {
		t.Fatal(err)
	}
	h, err := s.operationalHealth(s.DB)
	if err != nil {
		t.Fatal(err)
	}
	if h["counts"].(map[string]int)["remote_reconciliation"] != 0 {
		t.Fatal("cancelled fixture reported as remote resource")
	}
	if _, err = s.DB.Exec("UPDATE batches SET mode='scientific-smoke' WHERE id=?", bid); err != nil {
		t.Fatal(err)
	}
	if _, err = s.DB.Exec("UPDATE runs SET state='attention' WHERE batch_id=?", bid); err != nil {
		t.Fatal(err)
	}
	h, err = s.operationalHealth(s.DB)
	if err != nil {
		t.Fatal(err)
	}
	if h["counts"].(map[string]int)["remote_reconciliation"] != 1 {
		t.Fatal("unresolved real resource omitted")
	}
}
