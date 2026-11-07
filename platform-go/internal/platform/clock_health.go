package platform

import (
	"encoding/json"
	"os"
	"time"
)

// Written by the host health timer, never by an HTTP client. Absence/staleness
// pauses admission; accepted runs keep their independent execution deadlines.
func (s *Store) checkClockHealth() error {
	if s.ClockHealthPath == "" {
		return nil
	} // Embedded local tests inject their own clock.
	raw, e := os.ReadFile(s.ClockHealthPath)
	var record struct {
		SampledAt    float64 `json:"sampled_at"`
		Synchronized bool    `json:"synchronized"`
	}
	if e != nil || len(raw) > 4096 || json.Unmarshal(raw, &record) != nil || !record.Synchronized {
		return problem(503, "Server time synchronization is unavailable. Admission is paused.")
	}
	age := s.Now().Sub(time.Unix(0, int64(record.SampledAt*1e9)))
	if age < -5*time.Second || age > 180*time.Second {
		return problem(503, "Server time health is stale or inconsistent. Admission is paused.")
	}
	return nil
}
