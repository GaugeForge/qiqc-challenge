package platform

import (
	"encoding/json"
	"testing"
	"time"
)

func scheduledCalendar() Calendar {
	return Calendar{Mode: "scheduled", Version: "test-calendar-1", StartsAt: time.Date(2026, 11, 8, 0, 0, 0, 0, time.UTC), EndsAt: time.Date(2026, 11, 10, 13, 0, 0, 0, time.UTC), AppealClosesAt: time.Date(2026, 11, 11, 13, 0, 0, 0, time.UTC)}
}
func installCalendar(t *testing.T, s *Store, c Calendar) {
	t.Helper()
	data, _ := json.Marshal(c)
	if _, err := s.DB.Exec("INSERT OR REPLACE INTO settings(key,value) VALUES('calendar',?)", string(data)); err != nil {
		t.Fatal(err)
	}
}
func TestScheduledAdmissionBoundariesAndReplayAfterClose(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	c := scheduledCalendar()
	installCalendar(t, s, c)
	clock := c.StartsAt.Add(-time.Nanosecond)
	s.Now = func() time.Time { return clock }
	// Keep only this fixture session valid across the simulated event dates.
	s.DB.Exec("UPDATE sessions SET expires=?", float64(c.AppealClosesAt.Unix()+86400))
	if _, err := s.Confirm(token, v.ID, "full", "simulated", "before"); status(err) != 409 {
		t.Fatal("prestart", err)
	}
	clock = c.StartsAt
	b, err := s.Confirm(token, v.ID, "full", "simulated", "start")
	if err != nil {
		t.Fatal(err)
	}
	var snapshot string
	if err = s.DB.QueryRow("SELECT policy_json FROM batch_policies WHERE batch_id=?", b["id"]).Scan(&snapshot); err != nil {
		t.Fatal(err)
	}
	var pinned BatchPolicy
	if err = json.Unmarshal([]byte(snapshot), &pinned); err != nil || pinned.Calendar.Version != c.Version || pinned.SubmissionSHA != v.SHA256 {
		t.Fatal("policy not frozen", err)
	}
	clock = c.EndsAt
	same, err := s.Confirm(token, v.ID, "full", "simulated", "start")
	if err != nil || same["id"] != b["id"] {
		t.Fatal("accepted replay after cutoff", err)
	}
	if err = s.Cancel(token, b["id"].(string)); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Confirm(token, v.ID, "full", "simulated", "end"); status(err) != 409 {
		t.Fatal("end inclusive bug", err)
	}
	var n int
	s.DB.QueryRow("SELECT count(*) FROM ledger WHERE action='accepted'").Scan(&n)
	if n != 1 {
		t.Fatal("rejected admission changed quota", n)
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	reopened.Now = s.Now
	if _, err = reopened.Confirm(token, v.ID, "full", "simulated", "restart"); status(err) != 409 {
		t.Fatal("restart lost calendar", err)
	}
}
func TestCalendarAcceptedWorkDrainsAndAppealCutoff(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	c := scheduledCalendar()
	installCalendar(t, s, c)
	clock := c.EndsAt.Add(-time.Second)
	s.Now = func() time.Time { return clock }
	s.DB.Exec("UPDATE sessions SET expires=?", float64(c.AppealClosesAt.Unix()+86400))
	if _, err := s.Confirm(token, v.ID, "public", "simulated", "drain"); err != nil {
		t.Fatal(err)
	}
	clock = c.EndsAt.Add(time.Second)
	if l, err := s.Claim("after-close", 1); err != nil || l == nil {
		t.Fatal("accepted work must drain", err)
	}
	clock = c.AppealClosesAt
	if err := s.Appeal(token, "This is a sufficiently long appeal."); status(err) != 409 {
		t.Fatal("late appeal", err)
	}
	clock = c.AppealClosesAt.Add(-time.Nanosecond)
	if err := s.Appeal(token, "This is a sufficiently long appeal."); err != nil {
		t.Fatal("on time appeal", err)
	}
}

func TestQuietCancelsQueuedBatchesButLeavesClaimedRunAlone(t *testing.T) {
	s := testStore(t)
	c := scheduledCalendar()
	installCalendar(t, s, c)
	// Keep the fixture in the pre-quiet interval regardless of the host date;
	// the test later advances the clock explicitly to the quiet boundary.
	s.Now = func() time.Time { return c.StartsAt.Add(-8 * time.Hour) }
	queued := queueBatch(t, s, 2, "simulated")
	claimed := queueBatch(t, s, 1, "simulated")
	if _, err := s.Claim("quiet-running", 1); err != nil {
		t.Fatal(err)
	}
	// The first claim may have selected either synthetic batch; make the
	// assertion independent of dispatch order by identifying the testing one.
	var testingBatch string
	if err := s.DB.QueryRow("SELECT id FROM batches WHERE status='testing'").Scan(&testingBatch); err != nil {
		t.Fatal(err)
	}
	if testingBatch == queued {
		queued, claimed = claimed, queued
	}
	s.Now = func() time.Time { return c.QuietStart().Add(time.Second) }
	if lease, err := s.Claim("quiet-observer", 1); err != nil || lease != nil {
		t.Fatalf("quiet claim = %#v, %v", lease, err)
	}
	var state, runState, reason string
	if err := s.DB.QueryRow("SELECT status FROM batches WHERE id=?", queued).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if err := s.DB.QueryRow("SELECT state,error FROM runs WHERE batch_id=? ORDER BY ordinal LIMIT 1", queued).Scan(&runState, &reason); err != nil {
		t.Fatal(err)
	}
	if state != "cancelled" || runState != "cancelled" || reason != "quiet_period_started" {
		t.Fatalf("queued batch was not closed: %s %s %s", state, runState, reason)
	}
	var activeState string
	if err := s.DB.QueryRow("SELECT status FROM batches WHERE id=?", testingBatch).Scan(&activeState); err != nil {
		t.Fatal(err)
	}
	if activeState != "testing" {
		t.Fatalf("claimed batch was interrupted: %s", activeState)
	}
	var notices int
	if err := s.DB.QueryRow("SELECT COUNT(*) FROM outbox WHERE event='evaluation_cancelled_quiet'").Scan(&notices); err != nil || notices != 1 {
		t.Fatalf("quiet notice count = %d, %v", notices, err)
	}
}

func TestCalendarMalformedFailsClosedAndInfraNeedsExplicitWindow(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	s.DB.Exec("INSERT INTO settings(key,value) VALUES('calendar','{broken')")
	if _, err := s.Confirm(token, v.ID, "infra", "simulated", "malformed"); status(err) != 503 {
		t.Fatal("malformed policy admitted", err)
	}
	c := scheduledCalendar()
	installCalendar(t, s, c)
	s.Now = func() time.Time { return c.StartsAt }
	s.DB.Exec("UPDATE sessions SET expires=?", float64(c.AppealClosesAt.Unix()+86400))
	if _, err := s.Confirm(token, v.ID, "infra", "simulated", "unknown-infra"); status(err) != 409 {
		t.Fatal("invented infra window", err)
	}
}

func TestPublicAdmissionWaitsForCompetitionWindow(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	c := scheduledCalendar()
	start := c.StartsAt.Add(-8 * time.Hour)
	end := c.StartsAt.Add(-7 * time.Hour)
	c.InfraStartsAt, c.InfraEndsAt = &start, &end
	installCalendar(t, s, c)
	s.Now = func() time.Time { return start.Add(time.Minute) }
	s.DB.Exec("UPDATE sessions SET expires=?", float64(c.AppealClosesAt.Unix()+86400))
	if _, err := s.Confirm(token, v.ID, "public", "simulated", "before-competition"); status(err) != 409 {
		t.Fatalf("public practice admission = %v, want 409", err)
	}
	if _, err := s.Confirm(token, v.ID, "infra", "simulated", "infra-window"); err != nil {
		t.Fatalf("infra admission during infra window: %v", err)
	}
}

func TestCalendarAdminAuditedVersionedAndPinned(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	admin := readyUser(t, s, "calendar-admin@example.test", "admin")
	c := scheduledCalendar()
	a := Action{Action: "calendar", Calendar: &c, Actor: "operator", Reason: "Freeze PRD calendar", Idem: "schedule-one"}
	if err := s.AdminAction(token, a, "local"); status(err) != 403 {
		t.Fatal("team changed calendar", err)
	}
	if err := s.AdminAction(admin, a, "local"); err != nil {
		t.Fatal(err)
	}
	if err := s.AdminAction(admin, a, "local"); err != nil {
		t.Fatal("idempotent admin replay", err)
	}
	var audits int
	s.DB.QueryRow("SELECT count(*) FROM audit WHERE action='calendar_update'").Scan(&audits)
	if audits != 1 {
		t.Fatal("audit duplicated", audits)
	}
	changed := c
	changed.EndsAt = changed.EndsAt.Add(time.Hour)
	if err := s.AdminAction(admin, Action{Action: "calendar", Calendar: &changed, Actor: "operator", Reason: "Try reusing version", Idem: "reuse"}, "local"); status(err) != 409 {
		t.Fatal("version mutated", err)
	}
	now := c.StartsAt
	s.Now = func() time.Time { return now }
	s.DB.Exec("UPDATE sessions SET expires=?", float64(c.AppealClosesAt.Unix()+86400))
	b, err := s.Confirm(token, v.ID, "public", "simulated", "pin")
	if err != nil {
		t.Fatal(err)
	}
	var before, after string
	s.DB.QueryRow("SELECT policy_sha256 FROM batch_policies WHERE batch_id=?", b["id"]).Scan(&before)
	changed.Version = "calendar-adjusted-2"
	if err = s.AdminAction(admin, Action{Action: "calendar", Calendar: &changed, Actor: "operator", Reason: "Audited timing correction", Idem: "correction"}, "local"); err != nil {
		t.Fatal(err)
	}
	s.DB.QueryRow("SELECT policy_sha256 FROM batch_policies WHERE batch_id=?", b["id"]).Scan(&after)
	if before != after || before == "" {
		t.Fatal("accepted policy changed")
	}
	config, err := s.Config()
	if err != nil || config["always_open"] != false {
		t.Fatal("public config misleading", err)
	}
}
