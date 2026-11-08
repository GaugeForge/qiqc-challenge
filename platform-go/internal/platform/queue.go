package platform

import (
	"database/sql"
	"errors"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Lease is a persisted, attempt-scoped execution capability. Only a trusted
// controller may call these methods; they are not participant-facing endpoints.
type Lease struct {
	RunID      string  `json:"run_id"`
	BatchID    string  `json:"batch_id"`
	TeamID     string  `json:"team_id"`
	VersionID  string  `json:"version_id"`
	SHA256     string  `json:"sha256"`
	Kind       string  `json:"kind"`
	Mode       string  `json:"mode"`
	Ordinal    int     `json:"ordinal"`
	Public     int     `json:"public"`
	Attempt    int     `json:"attempt"`
	AttemptID  string  `json:"attempt_id"`
	Owner      string  `json:"owner"`
	LeaseUntil float64 `json:"lease_until"`
}

const leaseSeconds = 60
const lostWorkerError = "worker_lost:reconciliation_required"

// Claim reserves one global task slot in the same transaction as the attempt
// and batch state changes. A closed contest still drains accepted requests.
// slots must come from one trusted deployment setting shared by all workers.
// An unresolved real sandbox continues occupying a slot until reconciliation.
func (s *Store) Claim(owner string, slots int) (*Lease, error) {
	return s.claimPool(owner, slots, "")
}

// ClaimPool is the dispatch boundary for the two E2B credentials. An empty
// pool retains the compatibility behaviour used by local fixtures; production
// workers set key2 for infrastructure/public traffic or key1 for formal/ELO.
func (s *Store) ClaimPool(owner string, slots int, pool string) (*Lease, error) {
	return s.claimPool(owner, slots, pool)
}

func (s *Store) claimPool(owner string, slots int, pool string) (*Lease, error) {
	if strings.TrimSpace(owner) == "" || len(owner) > 256 || slots < 1 {
		return nil, problem(400, "Invalid worker identity or task slot limit")
	}
	poolFilter := "1=1"
	switch pool {
	case "":
	case "key2":
		poolFilter = "b.kind IN ('infra','public')"
	case "key1":
		poolFilter = "b.kind IN ('full','elo')"
	default:
		return nil, problem(400, "Invalid worker pool")
	}
	var result *Lease
	err := s.write(func(tx *sql.Tx) error {
		var raw string
		if err := tx.QueryRow("SELECT value FROM settings WHERE key='paused'").Scan(&raw); err != nil {
			return err
		}
		if raw != "true" && raw != "false" {
			return errors.New("invalid persisted pause state")
		}
		if raw == "true" {
			return nil
		}
		// Workers are also phase observers. This closes queued evaluations even
		// if the admission loop is between ticks, while preserving runs already
		// claimed by another worker.
		calendar, err := loadCalendar(tx)
		if err != nil {
			return err
		}
		if _, err = s.observePhase(tx, calendar); err != nil {
			return err
		}
		var count int
		if err := tx.QueryRow(`SELECT COUNT(*) FROM runs r JOIN batches b ON b.id=r.batch_id
    WHERE r.state IN ('running','uploading') OR
    (r.state='attention' AND r.error=? AND b.mode!='simulated')`, lostWorkerError).Scan(&count); err != nil {
			return err
		}
		var configured string
		settingErr := tx.QueryRow("SELECT value FROM settings WHERE key='global_task_slots'").Scan(&configured)
		if settingErr == nil {
			limit, e := strconv.Atoi(configured)
			if e != nil || limit < 1 || limit > 10000 {
				return errors.New("invalid global task slots")
			}
			slots = limit
		} else if !errors.Is(settingErr, sql.ErrNoRows) {
			return settingErr
		}
		// Infra and Public share E2B_API_KEY_2; formal Full/ELO runs use the
		// production key. Keep independent pool ceilings so a busy shared key
		// cannot consume all formal capacity (or vice versa).
		poolLimit := func(key string, fallback int) (int, error) {
			var raw string
			err := tx.QueryRow("SELECT value FROM settings WHERE key=?", key).Scan(&raw)
			if errors.Is(err, sql.ErrNoRows) {
				return fallback, nil
			}
			if err != nil {
				return 0, err
			}
			n, e := strconv.Atoi(raw)
			if e != nil || n < 1 || n > 10000 {
				return 0, errors.New("invalid persisted worker pool limit")
			}
			return n, nil
		}
		key2Slots, err := poolLimit("e2b_key2_slots", 20)
		if err != nil {
			return err
		}
		formalSlots, err := poolLimit("e2b_key1_slots", 100)
		if err != nil {
			return err
		}
		if count >= slots {
			return nil
		}
		lease := &Lease{Owner: owner}
		err = tx.QueryRow(`SELECT r.id,r.batch_id,r.ordinal,r.public,r.attempt,b.team_id,b.version_id,b.kind,b.mode,v.sha256
   FROM runs r JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id
   WHERE r.state='queued' AND b.status IN ('queued','testing') AND `+poolFilter+` AND
     ((b.kind IN ('infra','public') AND (SELECT COUNT(*) FROM runs rr JOIN batches bb ON bb.id=rr.batch_id WHERE (rr.state IN ('running','uploading') OR (rr.state='attention' AND rr.error=?)) AND bb.kind IN ('infra','public')) < ?) OR
      (b.kind IN ('full','elo') AND (SELECT COUNT(*) FROM runs rr JOIN batches bb ON bb.id=rr.batch_id WHERE (rr.state IN ('running','uploading') OR (rr.state='attention' AND rr.error=?)) AND bb.kind IN ('full','elo')) < ?))
   ORDER BY b.last_dispatch,b.accepted_at,b.id,r.ordinal LIMIT 1`, lostWorkerError, key2Slots, lostWorkerError, formalSlots).Scan(
			&lease.RunID, &lease.BatchID, &lease.Ordinal, &lease.Public, &lease.Attempt, &lease.TeamID, &lease.VersionID, &lease.Kind, &lease.Mode, &lease.SHA256)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		lease.Attempt++
		lease.AttemptID = id()
		now := s.now()
		leaseDuration := float64(leaseSeconds)
		// Scientific Harbor executions use the immutable task limits.  A fixed
		// one-minute capability lease can expire while a legitimate long task is
		// still running, causing its model mailbox calls to receive 403 and
		// turning an ordinary execution into a false infrastructure failure.
		// Keep the heartbeat as the normal renewal path, but make the initial
		// lease cover the frozen execution envelope plus a bounded cleanup grace.
		if lease.Mode == "scientific" {
			if target, e := readRunScientificTarget(tx, *lease); e == nil && target != nil && target.Limits.Valid() {
				agentSeconds := target.Limits.AgentSeconds
				// Infrastructure admission is the bounded three-minute path.  The
				// catalog still carries each task's original agent limit for the
				// later Public/Full paths, so do not use that value for infra leases.
				if lease.Kind == "infra" && target.AgentBudgetSeconds > 0 {
					agentSeconds = target.AgentBudgetSeconds
				}
				leaseDuration = float64(target.Limits.SetupSeconds + target.Limits.BuildSeconds + agentSeconds + target.Limits.VerifierSeconds + 900)
				if leaseDuration < float64(leaseSeconds) {
					leaseDuration = float64(leaseSeconds)
				}
			}
		}
		lease.LeaseUntil = now + leaseDuration
		if _, err = tx.Exec("UPDATE runs SET state='running',attempt=?,owner=?,lease_until=?,error=NULL WHERE id=?", lease.Attempt, owner, lease.LeaseUntil, lease.RunID); err != nil {
			return err
		}
		if _, err = tx.Exec("INSERT INTO attempts VALUES(?,?,?,?,?,?,?)", lease.AttemptID, lease.RunID, lease.Attempt, "running", now, nil, nil); err != nil {
			return err
		}
		// Keep round-robin ordering even when a fake/frozen clock or low-resolution
		// clock reports the same time for several claims.
		var previous float64
		if err = tx.QueryRow("SELECT COALESCE(MAX(last_dispatch),0) FROM batches").Scan(&previous); err != nil {
			return err
		}
		dispatch := now
		if previous >= dispatch {
			dispatch = math.Nextafter(previous, math.Inf(1))
		}
		if _, err = tx.Exec("UPDATE batches SET status='testing',last_dispatch=? WHERE id=?", dispatch, lease.BatchID); err != nil {
			return err
		}
		result = lease
		return nil
	})
	return result, err
}

func (s *Store) fence(tx *sql.Tx, lease Lease) error {
	var count int
	err := tx.QueryRow(`SELECT COUNT(*) FROM runs r JOIN attempts a ON a.run_id=r.id AND a.number=r.attempt
  JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id WHERE r.id=? AND r.batch_id=? AND r.attempt=? AND r.owner=?
  AND a.id=? AND a.state='running' AND r.state IN ('running','uploading')
  AND r.lease_until>? AND b.status IN ('testing','cancelling','attention')
  AND b.team_id=? AND b.version_id=? AND v.sha256=? AND b.mode=? AND b.kind=? AND r.ordinal=? AND r.public=?`, lease.RunID, lease.BatchID, lease.Attempt, lease.Owner, lease.AttemptID, s.now(), lease.TeamID, lease.VersionID, lease.SHA256, lease.Mode, lease.Kind, lease.Ordinal, lease.Public).Scan(&count)
	if err != nil {
		return err
	}
	if count != 1 {
		return problem(409, "Expired, stale or mismatched execution lease")
	}
	return nil
}

// ValidateLease is a diagnostic check, not a replacement for checking inside
// the same transaction that publishes a result or changes a run's state.
func (s *Store) ValidateLease(lease Lease) error {
	return s.write(func(tx *sql.Tx) error { return s.fence(tx, lease) })
}

func (s *Store) Heartbeat(lease Lease) error {
	return s.write(func(tx *sql.Tx) error {
		if err := s.fence(tx, lease); err != nil {
			return err
		}
		_, err := tx.Exec("UPDATE runs SET lease_until=? WHERE id=?", s.now()+leaseSeconds, lease.RunID)
		return err
	})
}

// BeginUpload retains the execution slot while unique evidence is pending.
// Recovery must retransmit that evidence instead of rerunning the Agent.
func (s *Store) BeginUpload(lease Lease) error {
	return s.write(func(tx *sql.Tx) error {
		if err := s.fence(tx, lease); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE runs SET state='uploading' WHERE id=?", lease.RunID); err != nil {
			return err
		}
		_, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=? AND attempt=?", lease.RunID, lease.Attempt)
		return err
	})
}

type RecoveryReport struct {
	Rescheduled      int `json:"rescheduled_simulated"`
	Attention        int `json:"real_requires_reconciliation"`
	Cancelled        int `json:"cancelled_simulated"`
	UploadsPreserved int `json:"pending_uploads_preserved"`
}

// Recover handles expired execution and pre-cleanup scientific upload leases.
// A missing heartbeat never proves an E2B sandbox stopped. Scientific uploads
// without both published pending evidence and confirmed cleanup require
// reconciliation and retain their reserved global slot.
func (s *Store) Recover() (RecoveryReport, error) {
	report := RecoveryReport{}
	err := s.write(func(tx *sql.Tx) error {
		now := s.now()
		if err := tx.QueryRow("SELECT COUNT(*) FROM runs WHERE state='uploading'").Scan(&report.UploadsPreserved); err != nil {
			return err
		}
		rows, err := tx.Query(`SELECT r.id,r.batch_id,r.attempt,b.mode,b.status,r.state FROM runs r
   JOIN batches b ON b.id=r.batch_id WHERE r.lease_until<=? AND
   (r.state='running' OR (r.state='uploading' AND b.mode IN ('scientific-smoke','scientific')))`, now)
		if err != nil {
			return err
		}
		type expired struct {
			run, batch, mode, status, state string
			attempt                         int
		}
		var stale []expired
		for rows.Next() {
			var row expired
			if err = rows.Scan(&row.run, &row.batch, &row.attempt, &row.mode, &row.status, &row.state); err != nil {
				rows.Close()
				return err
			}
			stale = append(stale, row)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, row := range stale {
			state := "attention"
			if IsScientificMode(row.mode) && row.state == "uploading" {
				// The controller announces uploading before writing staged.json
				// and destroying sandboxes. Only PromoteScientificPending makes
				// pending.json visible after that cleanup fence. Never promote
				// staged evidence or infer cleanup from an expired upload lease.
				var pending Pending
				cache := filepath.Join(s.Root, "cache", row.run, strconv.Itoa(row.attempt))
				published := readPrivateRecoveryJSON(filepath.Join(cache, "pending.json"), &pending) == nil &&
					pending.Lease.RunID == row.run && pending.Lease.BatchID == row.batch &&
					pending.Lease.Attempt == row.attempt && pending.Lease.Mode == row.mode &&
					pending.Provenance == row.mode && IsScientificMode(row.mode) && validDigest(pending.SHA)
				clean := s.scientificCleanupConfirmedFor(tx, Lease{RunID: row.run, Attempt: row.attempt, Mode: row.mode}) == nil
				if published && clean {
					continue // ResumeUploads validates the same attempt and archive.
				}
				report.UploadsPreserved--
			}
			if row.mode == "simulated" {
				// The fixture may have durably published evidence just before
				// crashing before BeginUpload. Preserve that attempt even if
				// another worker runs Recover before ResumeUploads.
				pending := filepath.Join(s.Root, "cache", row.run, strconv.Itoa(row.attempt), "pending.json")
				if _, statErr := os.Lstat(pending); statErr == nil {
					if _, err = tx.Exec("UPDATE runs SET state='uploading',lease_until=? WHERE id=?", now+leaseSeconds, row.run); err != nil {
						return err
					}
					if _, err = tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", row.run); err != nil {
						return err
					}
					report.UploadsPreserved++
					continue
				} else if !os.IsNotExist(statErr) {
					return statErr
				}
				if row.status == "cancelling" {
					state = "cancelled"
					report.Cancelled++
				} else {
					state = "queued"
					report.Rescheduled++
				}
			} else {
				report.Attention++
			}
			if _, err = tx.Exec("UPDATE runs SET state=?,owner=NULL,lease_until=NULL,error=? WHERE id=?", state, lostWorkerError, row.run); err != nil {
				return err
			}
			if _, err = tx.Exec("UPDATE attempts SET state='interrupted',finished=?,error=? WHERE run_id=? AND number=?", now, lostWorkerError, row.run, row.attempt); err != nil {
				return err
			}
			if _, err = tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", row.run); err != nil {
				return err
			}
			if state == "attention" && row.status != "cancelling" {
				if _, err = tx.Exec("UPDATE batches SET status='attention' WHERE id=?", row.batch); err != nil {
					return err
				}
			}
			if row.status == "cancelling" && row.mode == "simulated" {
				if _, err = tx.Exec(`UPDATE batches SET status='cancelled' WHERE id=? AND NOT EXISTS
      (SELECT 1 FROM runs WHERE batch_id=? AND state IN ('running','uploading','attention'))`, row.batch, row.batch); err != nil {
					return err
				}
			}
		}
		return nil
	})
	return report, err
}
