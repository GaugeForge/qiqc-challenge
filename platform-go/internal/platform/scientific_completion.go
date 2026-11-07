package platform

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
)

// CompleteScientificPending uploads the immutable evidence already durably
// spooled by the trusted SDK bridge. R2 failure leaves the same attempt and
// archive for ResumeUploads; the solver is never rerun for an upload outage.
func (s *Store) CompleteScientificPending(ctx context.Context, l Lease, reward int, seconds, sha string) error {
	if !IsScientificMode(l.Mode) || (l.Mode == "scientific-smoke" && l.Kind != "infra") {
		return errors.New("scientific completion mode mismatch")
	}
	if err := s.scientificCleanupConfirmed(l); err != nil {
		return err
	}
	cache := filepath.Join(s.Root, "cache", l.RunID, strconv.Itoa(l.Attempt))
	data, err := os.ReadFile(filepath.Join(cache, "pending.json"))
	if os.IsNotExist(err) {
		// Periodic ResumeUploads may have finished this exact bundle while the
		// controller was confirming sandbox cleanup. Accept only that same result.
		var state, storedSHA, storedSeconds string
		var storedReward, attempt int
		err = s.DB.QueryRow("SELECT state,object_sha,seconds,reward,attempt FROM runs WHERE id=?", l.RunID).Scan(&state, &storedSHA, &storedSeconds, &storedReward, &attempt)
		if err == nil && (state == "completed" || state == "cancelled") && storedSHA == sha && storedReward == reward && storedSeconds == seconds && attempt == l.Attempt {
			return nil
		}
		return errors.New("scientific pending upload absent or result changed")
	}
	if err != nil {
		return err
	}
	if len(data) > 65536 {
		return errors.New("oversized pending metadata")
	}
	var p Pending
	if err = json.Unmarshal(data, &p); err != nil {
		return err
	}
	if p.Lease != l || p.Provenance != p.Lease.Mode || !IsScientificMode(p.Lease.Mode) || p.Reward != reward || p.Seconds != seconds || p.SHA != sha {
		return errors.New("scientific pending result mismatch")
	}
	return s.uploadPending(ctx, cache, p)
}

// PromoteScientificPending exposes upload recovery work only after the trusted
// controller has returned and every recorded E2B sandbox is positively destroyed.
// A staged archive survives crashes, but ResumeUploads deliberately ignores it.
func (s *Store) PromoteScientificPending(l Lease) error {
	if err := s.scientificCleanupConfirmed(l); err != nil {
		return err
	}
	cache := filepath.Join(s.Root, "cache", l.RunID, strconv.Itoa(l.Attempt))
	raw, err := os.ReadFile(filepath.Join(cache, "staged.json"))
	if err != nil {
		return err
	}
	var p Pending
	if len(raw) > 65536 || json.Unmarshal(raw, &p) != nil || p.Lease != l || p.Provenance != p.Lease.Mode || !IsScientificMode(p.Lease.Mode) {
		return errors.New("scientific staging receipt mismatch")
	}
	if err = s.ValidateLease(l); err != nil {
		return err
	}
	if err = syncWrite(filepath.Join(cache, "pending.json"), raw); err != nil {
		return err
	}
	return os.Remove(filepath.Join(cache, "staged.json"))
}
