package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"path/filepath"

	"github.com/gaugeforge/qiqc-platform-go/internal/providerprofile"
)

// Organizer configuration only. Never populated from an uploaded manifest.
// A distinct loopback gateway may serve formal credentials on the same VPS.
type FormalExecution struct {
	ProfilePath        string `json:"profile_path"`
	ProfileSHA256      string `json:"profile_sha256"`
	Model              string `json:"model"`
	GatewayURL         string `json:"gateway_url"`
	MaxRequestsPerTask int    `json:"max_requests_per_task"`
}

func (p FormalExecution) valid() bool {
	u, e := url.Parse(p.GatewayURL)
	return e == nil && u.Scheme == "http" && (u.Hostname() == "127.0.0.1" || u.Hostname() == "localhost" || u.Hostname() == "::1") && u.User == nil && u.RawQuery == "" && u.Fragment == "" && (u.Path == "" || u.Path == "/") && filepath.IsAbs(p.ProfilePath) && validDigest(p.ProfileSHA256) && p.Model != "" && p.MaxRequestsPerTask > 0 && p.MaxRequestsPerTask <= 1000000
}
func formalExecution(q queryer) (*FormalExecution, error) {
	var raw string
	e := q.QueryRow("SELECT value FROM settings WHERE key='formal_execution'").Scan(&raw)
	if errors.Is(e, sql.ErrNoRows) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	var p FormalExecution
	if json.Unmarshal([]byte(raw), &p) != nil || !p.valid() {
		return nil, errors.New("invalid formal execution configuration")
	}
	return &p, nil
}
func (s *Store) ConfigureFormalExecutionFile(path string) error {
	raw, e := os.ReadFile(path)
	if e != nil {
		return e
	}
	if len(raw) > 16384 {
		return errors.New("formal execution configuration too large")
	}
	var p FormalExecution
	if json.Unmarshal(raw, &p) != nil || !p.valid() {
		return errors.New("formal execution configuration invalid")
	}
	profile, identity, e := providerprofile.LoadMetadata(p.ProfilePath)
	if e != nil || profile.Model != p.Model || identity.SHA256 != p.ProfileSHA256 {
		return errors.New("formal provider profile/key unavailable or mismatched")
	}
	return s.write(func(tx *sql.Tx) error {
		_, e := tx.Exec("INSERT INTO settings(key,value) VALUES('formal_execution',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(raw))
		return e
	})
}
func (s *Store) FormalExecutionForLease(l Lease) (*FormalExecution, error) {
	p, e := batchPolicy(s.DB, l.BatchID)
	if e != nil {
		return nil, e
	}
	return p.FormalExecution, nil
}
func runRequestLimit(q queryer, runID string) (int, error) {
	var bid, mode string
	if e := q.QueryRow("SELECT r.batch_id,b.mode FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.id=?", runID).Scan(&bid, &mode); e != nil {
		return 0, e
	}
	p, e := batchPolicy(q, bid)
	if errors.Is(e, sql.ErrNoRows) && mode != "scientific" {
		return 100, nil
	}
	if e != nil {
		return 0, e
	}
	if p.FormalExecution != nil {
		if !p.FormalExecution.valid() {
			return 0, errors.New("invalid pinned formal policy")
		}
		return p.FormalExecution.MaxRequestsPerTask, nil
	}
	return 100, nil // Existing practice policy is retained; formal is explicit.
}
