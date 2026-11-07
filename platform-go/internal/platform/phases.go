package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"time"
)

// Phase is a public capability snapshot, never an authorization token.
type Phase struct {
	Name               string     `json:"name"`
	ServerTime         time.Time  `json:"server_time"`
	NextAt             *time.Time `json:"next_at,omitempty"`
	AllowedKinds       []string   `json:"allowed_kinds"`
	UploadAllowed      bool       `json:"upload_allowed"`
	LeaderboardEnabled bool       `json:"leaderboard_enabled"`
	Reason             string     `json:"reason"`
}

func (c Calendar) QuietStart() time.Time {
	// The reviewed calendar may publish an explicit infrastructure end. That
	// boundary is authoritative for the quiet period; retain the six-hour
	// fallback only for legacy calendars without infra boundaries.
	if c.InfraEndsAt != nil {
		return *c.InfraEndsAt
	}
	return c.StartsAt.Add(-6 * time.Hour)
}
func (c Calendar) Phase(now time.Time) Phase {
	p := Phase{Name: "preparation", ServerTime: now, AllowedKinds: []string{}, Reason: "The infrastructure test has not opened yet."}
	if c.Mode == "demo" {
		p.Name = "practice"
		p.AllowedKinds = []string{"infra"}
		p.UploadAllowed = true
		p.Reason = "Infrastructure tests are open; Public tests and Full evaluation are reserved for the competition window."
		return p
	}
	quiet := c.QuietStart()
	switch {
	case !now.Before(c.EndsAt):
		p.Name = "closed"
		p.LeaderboardEnabled = true
		p.Reason = "New submissions are closed. Accepted competition evaluations continue."
	case !now.Before(c.StartsAt):
		p.Name = "competition"
		// Infrastructure checks remain available during the formal window. They
		// are unranked and use the separate key2 worker pool, while Public and
		// Full evaluation keep their normal competition semantics.
		p.AllowedKinds = []string{"infra", "public", "full"}
		p.UploadAllowed = true
		p.LeaderboardEnabled = true
		p.NextAt = &c.EndsAt
		p.Reason = "Competition admission is open."
	case !now.Before(quiet):
		p.Name = "quiet"
		p.NextAt = &c.StartsAt
		p.Reason = "Quiet period. New uploads and evaluations are closed."
	case c.InfraStartsAt != nil && !now.Before(*c.InfraStartsAt) && c.InfraEndsAt != nil && now.Before(*c.InfraEndsAt):
		p.Name = "practice"
		p.AllowedKinds = []string{"infra"}
		p.UploadAllowed = true
		p.NextAt = c.InfraEndsAt
		p.Reason = "Infrastructure tests are open; Public tests and Full evaluation are reserved for the competition window."
	case c.InfraEndsAt != nil && !now.Before(*c.InfraEndsAt):
		p.NextAt = &quiet
	default:
		p.NextAt = c.InfraStartsAt
	}
	return p
}
func (s *Store) Phase() (Phase, error) {
	c, err := loadCalendar(s.DB)
	if err != nil {
		return Phase{}, err
	}
	p := c.Phase(s.Now())
	var closed string
	if err = s.DB.QueryRow("SELECT value FROM settings WHERE key='competition_closed'").Scan(&closed); err != nil {
		return p, err
	}
	if c.Mode != "demo" {
		var prior int
		e := s.DB.QueryRow("SELECT rank FROM event_phase WHERE version=?", c.Version).Scan(&prior)
		rank := map[string]int{"preparation": 0, "practice": 1, "quiet": 2, "competition": 3, "closed": 4}
		if e != nil && !errors.Is(e, sql.ErrNoRows) {
			return p, e
		}
		if prior > rank[p.Name] {
			p.UploadAllowed = false
			p.AllowedKinds = []string{}
			p.Reason = "Server clock moved backwards across an event boundary; admission is paused."
		}
	}
	if p.Name == "competition" {
		fp, e := formalExecution(s.DB)
		if e != nil {
			return p, e
		}
		if fp == nil {
			p.UploadAllowed = false
			p.AllowedKinds = []string{}
			p.Reason = "Formal model configuration is not ready. Admission is paused; no attempts are charged."
		}
	}
	if closed == "true" {
		p.UploadAllowed = false
		p.AllowedKinds = []string{}
		p.Reason = "Admission has been paused by the organizers."
	}
	if e := s.checkClockHealth(); e != nil {
		p.UploadAllowed = false
		p.AllowedKinds = []string{}
		p.Reason = e.Error()
	}
	return p, nil
}

// Admission uses a persisted maximum observed phase. A clock rollback cannot
// reopen practice after quiet or reopen admission after closing.
func (s *Store) observePhase(tx *sql.Tx, c Calendar) (Phase, error) {
	p := c.Phase(s.Now())
	if c.Mode == "demo" {
		return p, nil
	}
	rank := map[string]int{"preparation": 0, "practice": 1, "quiet": 2, "competition": 3, "closed": 4}
	var old int
	err := tx.QueryRow("SELECT rank FROM event_phase WHERE version=?", c.Version).Scan(&old)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return p, err
	}
	if rank[p.Name] < old {
		return p, problem(503, "Server clock moved backwards across an event boundary; admission is paused.")
	}
	if rank[p.Name] > old || errors.Is(err, sql.ErrNoRows) {
		if _, err = tx.Exec("INSERT INTO event_phase(version,rank,phase,observed) VALUES(?,?,?,?) ON CONFLICT(version) DO UPDATE SET rank=excluded.rank,phase=excluded.phase,observed=excluded.observed", c.Version, rank[p.Name], p.Name, s.now()); err != nil {
			return p, err
		}
		payload, _ := json.Marshal(map[string]string{"phase": p.Name, "calendar_version": c.Version})
		_, err = tx.Exec("INSERT INTO outbox(id,event,payload,created) VALUES(?,'phase_changed',?,?)", id(), string(payload), s.now())
	}
	if p.Name == "quiet" {
		if err := cancelQueuedBatchesForQuiet(tx, s.now()); err != nil {
			return p, err
		}
	}
	return p, err
}

// Quiet starts a durable admission boundary. Queued work is cancelled and a
// user-facing outbox event is emitted for each affected team; a batch already
// in testing is left alone so its active sandbox can finish naturally.
func cancelQueuedBatchesForQuiet(tx *sql.Tx, now float64) error {
	rows, err := tx.Query("SELECT id,team_id FROM batches WHERE status='queued'")
	if err != nil {
		return err
	}
	type queuedBatch struct{ id, team string }
	var batches []queuedBatch
	for rows.Next() {
		var b queuedBatch
		if err := rows.Scan(&b.id, &b.team); err != nil {
			rows.Close()
			return err
		}
		batches = append(batches, b)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	for _, b := range batches {
		if _, err := tx.Exec("UPDATE runs SET state='cancelled',error='quiet_period_started',lease_until=NULL WHERE batch_id=? AND state='queued'", b.id); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE batches SET status='cancelled' WHERE id=? AND status='queued'", b.id); err != nil {
			return err
		}
		payload, _ := json.Marshal(map[string]string{
			"batch_id": b.id,
			"reason":   "quiet_period_started",
		})
		if _, err := tx.Exec("INSERT INTO outbox(id,event,team_id,payload,created) VALUES(?,'evaluation_cancelled_quiet',?,?,?)", id(), b.team, string(payload), now); err != nil {
			return err
		}
	}
	return nil
}
func (s *Store) CheckUploadPhase() error {
	if !CompetitionPortal() {
		return nil
	}
	if e := s.checkClockHealth(); e != nil {
		return e
	}
	return s.write(func(tx *sql.Tx) error {
		c, e := loadCalendar(tx)
		if e != nil {
			return e
		}
		p, e := s.observePhase(tx, c)
		if e != nil {
			return e
		}
		var closed string
		if e = tx.QueryRow("SELECT value FROM settings WHERE key='competition_closed'").Scan(&closed); e != nil {
			return e
		}
		if p.Name == "competition" {
			fp, e := formalExecution(tx)
			if e != nil {
				return e
			}
			if fp == nil {
				return problem(409, "Formal model configuration is not ready. No attempt charged.")
			}
		}
		if closed == "true" || !p.UploadAllowed {
			return problem(409, p.Reason)
		}
		return nil
	})
}
func (s *Store) ConfigureCalendarFile(path string) error {
	raw, e := os.ReadFile(path)
	if e != nil {
		return e
	}
	if len(raw) > 8192 {
		return errors.New("calendar file too large")
	}
	var c Calendar
	if json.Unmarshal(raw, &c) != nil || c.validate() != nil {
		return errors.New("invalid calendar file")
	}
	return s.write(func(tx *sql.Tx) error { _, e := setCalendar(tx, c); return e })
}
