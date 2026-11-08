package platform

import (
	"testing"
	"time"
)

func TestEventPhaseBoundaries(t *testing.T) {
	c := scheduledCalendar()
	start := c.StartsAt.Add(-13 * 24 * time.Hour)
	end := c.QuietStart()
	c.InfraStartsAt = &start
	c.InfraEndsAt = &end
	for _, x := range []struct {
		at     time.Time
		name   string
		upload bool
	}{{start.Add(-time.Second), "preparation", false}, {start, "practice", true}, {end.Add(-time.Second), "practice", true}, {end, "quiet", false}, {c.StartsAt, "competition", true}, {c.EndsAt, "closed", false}} {
		got := c.Phase(x.at)
		if got.Name != x.name || got.UploadAllowed != x.upload {
			t.Fatalf("%v: %+v", x, got)
		}
	}
	practice := c.Phase(start)
	if len(practice.AllowedKinds) != 1 || practice.AllowedKinds[0] != "infra" {
		t.Fatalf("practice allowed kinds = %#v, want infra only", practice.AllowedKinds)
	}
	competitionPhase := c.Phase(c.StartsAt)
	if len(competitionPhase.AllowedKinds) != 3 || competitionPhase.AllowedKinds[0] != "infra" || competitionPhase.AllowedKinds[1] != "public" || competitionPhase.AllowedKinds[2] != "full" {
		t.Fatalf("competition allowed kinds = %#v, want infra/public/full", competitionPhase.AllowedKinds)
	}
	if err := c.allow("infra", c.StartsAt); err != nil {
		t.Fatalf("infra at competition opening rejected: %v", err)
	}
	if err := c.allow("infra", c.EndsAt.Add(-time.Second)); err != nil {
		t.Fatalf("infra during competition rejected: %v", err)
	}
	if status(c.allow("infra", c.EndsAt)) != 409 {
		t.Fatal("infra accepted after competition closes")
	}
	if err := c.validate(); err != nil {
		t.Fatal(err)
	}
	end = end.Add(time.Second)
	if c.validate() == nil {
		t.Fatal("practice overlaps quiet")
	}
}
func TestUploadClosedAndRestartBoundary(t *testing.T) {
	t.Setenv("QIQC_COMPETITION_PORTAL", "1")
	s := testStore(t)
	nativeCatalogFixture(t, s)
	c := scheduledCalendar()
	installCalendar(t, s, c)
	now := c.StartsAt
	s.Now = func() time.Time { return now }
	if err := s.CheckUploadPhase(); err != nil {
		t.Fatal(err)
	}
	now = c.QuietStart()
	if status(s.CheckUploadPhase()) != 503 {
		t.Fatal("rollback admitted")
	}
	reopened, e := Open(s.Path)
	if e != nil {
		t.Fatal(e)
	}
	defer reopened.Close()
	reopened.Now = s.Now
	if status(reopened.CheckUploadPhase()) != 503 {
		t.Fatal("restart forgot watermark")
	}
	now = c.EndsAt
	if status(s.CheckUploadPhase()) != 409 {
		t.Fatal("late upload accepted")
	}
}

func TestExplicitInfrastructureEndDefinesQuietBoundary(t *testing.T) {
	start := time.Date(2026, 11, 6, 20, 0, 0, 0, time.FixedZone("UTC+8", 8*3600))
	infraEnd := time.Date(2026, 11, 8, 0, 0, 0, 0, time.FixedZone("UTC+8", 8*3600))
	competition := time.Date(2026, 11, 8, 8, 0, 0, 0, time.FixedZone("UTC+8", 8*3600))
	c := Calendar{Mode: "scheduled", Version: "explicit-infra-end", StartsAt: competition,
		EndsAt: competition.Add(48 * time.Hour), AppealClosesAt: competition.Add(72 * time.Hour),
		InfraStartsAt: &start, InfraEndsAt: &infraEnd}
	if got := c.QuietStart(); !got.Equal(infraEnd) {
		t.Fatalf("quiet boundary = %v, want %v", got, infraEnd)
	}
	if got := c.Phase(infraEnd.Add(-time.Second)); got.Name != "practice" || !got.UploadAllowed {
		t.Fatalf("before explicit infra end = %+v", got)
	}
	if got := c.Phase(infraEnd); got.Name != "quiet" || got.UploadAllowed || got.NextAt == nil || !got.NextAt.Equal(competition) {
		t.Fatalf("at explicit infra end = %+v", got)
	}
}
