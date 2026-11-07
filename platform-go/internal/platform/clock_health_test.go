package platform

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestClockHealthPausesWithoutCharging(t *testing.T) {
	s := testStore(t)
	s.ClockHealthPath = filepath.Join(t.TempDir(), "clock.json")
	if status(s.checkClockHealth()) != 503 {
		t.Fatal("missing clock admitted")
	}
	for _, x := range []struct {
		age        float64
		synced, ok bool
	}{{0, true, true}, {181, true, false}, {-6, true, false}, {0, false, false}} {
		raw, _ := json.Marshal(map[string]any{"sampled_at": s.now() - x.age, "synchronized": x.synced})
		os.WriteFile(s.ClockHealthPath, raw, 0600)
		if (s.checkClockHealth() == nil) != x.ok {
			t.Fatal(x)
		}
	}
}
func TestFormalConfigurationMissingStopsAdmission(t *testing.T) {
	t.Setenv("QIQC_COMPETITION_PORTAL", "1")
	s := testStore(t)
	nativeCatalogFixture(t, s)
	team, v := approved(t, s)
	s.DB.Exec("DELETE FROM settings WHERE key='formal_execution'")
	c := scheduledCalendar()
	installCalendar(t, s, c)
	s.Now = func() time.Time { return c.StartsAt }
	s.DB.Exec("UPDATE sessions SET expires=?", float64(c.AppealClosesAt.Unix()))
	if _, e := s.Confirm(team, v.ID, "full", "scientific", "formal-missing"); status(e) != 409 {
		t.Fatal(e)
	}
	if status(s.CheckUploadPhase()) != 409 {
		t.Fatal("upload allowed without formal config")
	}
	phase, e := s.Phase()
	if e != nil || phase.UploadAllowed {
		t.Fatal(phase, e)
	}
	var count int
	s.DB.QueryRow("SELECT count(*) FROM ledger").Scan(&count)
	if count != 0 {
		t.Fatal("attempt charged")
	}
}
