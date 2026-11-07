package platform

import "testing"

func TestAdminQuotaPauseClosureAndAuditAtomicity(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	admin := readyUser(t, s, "admin@example.test", "admin")
	u, _ := s.Session(tok)
	if err := s.AdminAction(tok, Action{Action: "quota", Target: u.TeamID, Kind: "full", Delta: 1, Actor: "operator", Reason: "verified"}, "ip"); status(err) != 403 {
		t.Fatal("team admin bypass")
	}
	a := Action{Action: "quota", Target: u.TeamID, Kind: "full", Delta: 1, Actor: "operator", Reason: "verified adjustment", Idem: "quota-once"}
	if err := s.AdminAction(admin, a, "ip"); err != nil {
		t.Fatal(err)
	}
	if err := s.AdminAction(admin, a, "ip"); err != nil {
		t.Fatal(err)
	}
	after, _ := s.Session(tok)
	if after.Quotas["full"] != 2 {
		t.Fatal("duplicate admin debit")
	}
	a.Delta = 2
	if err := s.AdminAction(admin, a, "ip"); status(err) != 409 {
		t.Fatal("idem different body")
	}
	a.Idem = "new"
	a.Reason = ""
	if err := s.AdminAction(admin, a, "ip"); status(err) != 400 {
		t.Fatal("missing reason")
	}
	if err := s.AdminAction(admin, Action{Action: "pause", Paused: true, Actor: "operator", Reason: "incident"}, "ip"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Confirm(tok, v.ID, "infra", "simulated", "accepted-paused"); err != nil {
		t.Fatal("pause incorrectly rejects acceptance")
	}
	if err := s.AdminAction(admin, Action{Action: "close_demo", Actor: "operator", Reason: "deadline"}, "ip"); err != nil {
		t.Fatal(err)
	}
	var closed string
	s.DB.QueryRow("SELECT value FROM settings WHERE key='competition_closed'").Scan(&closed)
	if closed != "true" {
		t.Fatal("not closed")
	}
}
func TestAppealIsSinglePerTeamAndPrivate(t *testing.T) {
	s := testStore(t)
	tok := readyUser(t, s, "a@example.test", "team")
	other := readyUser(t, s, "b@example.test", "team")
	if err := s.Appeal(tok, "Please review this affected submission."); err != nil {
		t.Fatal(err)
	}
	if err := s.Appeal(tok, "Please review this affected submission."); status(err) != 409 {
		t.Fatal("duplicate appeal")
	}
	a, err := s.MyAppeal(other)
	if err != nil || a != nil {
		t.Fatal("cross-team appeal leak")
	}
}
