package platform

import "testing"

func TestCompetitionPortalRejectsNewSimulationAndPreservesReplay(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	old, err := s.Confirm(token, v.ID, "infra", "simulated", "existing-rehearsal")
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("QIQC_COMPETITION_PORTAL", "1")
	for _, mode := range []string{"simulated", "codex", "scientific-smoke"} {
		if _, err := s.Confirm(token, v.ID, "infra", mode, "new-"+mode); status(err) != 409 {
			t.Fatalf("accepted %s: %v", mode, err)
		}
	}
	replay, err := s.Confirm(token, v.ID, "infra", "simulated", "existing-rehearsal")
	if err != nil || replay["id"] != old["id"] {
		t.Fatal("historical replay changed", err)
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger WHERE action='accepted'").Scan(&n)
	if n != 1 {
		t.Fatal("unexpected quota debit", n)
	}
}

func TestCompetitionPortalDoesNotPublishRehearsalOrFabricatedBaseline(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	b, err := s.Confirm(token, v.ID, "full", "simulated", "historical-score")
	if err != nil {
		t.Fatal(err)
	}
	s.DB.Exec("UPDATE batches SET status='completed',passed=49,mean='1.000000' WHERE id=?", b["id"])
	t.Setenv("QIQC_COMPETITION_PORTAL", "1")
	board, err := s.Leaderboard()
	if err != nil {
		t.Fatal(err)
	}
	if len(board["teams"].([]Standing)) != 0 {
		t.Fatal("rehearsal promoted")
	}
	baseline := board["baseline"].(map[string]any)
	if baseline["passed"] != nil || baseline["mean"] != nil || baseline["verified"] != false {
		t.Fatal("unverified baseline published")
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM batches WHERE passed=49").Scan(&n)
	if n != 1 {
		t.Fatal("historical data removed")
	}
	cfg, err := s.Config()
	if err != nil || cfg["competition_portal"] != true {
		t.Fatal(cfg, err)
	}
}
