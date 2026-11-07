// Package diagnosticmodel owns the explicit free-only infrastructure profile.
// Its shared durable budget is an operator safeguard, not an assertion of the
// provider account's remaining quota (other clients may consume that quota).
package diagnosticmodel

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"time"
)

const Model = "minimax/minimax-m3.1-flash:free"
const Version = "free-infra-minimax-m31/v1"

var ErrLimited = errors.New("diagnostic free request budget exhausted")

func Validate(model string, rpm, daily int) error {
	if model != Model || rpm < 1 || rpm > 20 || daily < 1 || daily > 1000 {
		return errors.New("invalid free diagnostic profile")
	}
	return nil
}

// Reserve counts starts before network I/O across review and scientific gateway
// processes using the same database. Retries and unknown outcomes also count.
func Reserve(ctx context.Context, db *sql.DB, now time.Time, rpm, daily int) error {
	if err := Validate(Model, rpm, daily); err != nil {
		return err
	}
	if _, err := db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS diagnostic_free_admissions (id INTEGER PRIMARY KEY, started_ns INTEGER NOT NULL)`); err != nil {
		return err
	}
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var minute, day int
	start := time.Date(now.UTC().Year(), now.UTC().Month(), now.UTC().Day(), 0, 0, 0, 0, time.UTC).UnixNano()
	if err = tx.QueryRowContext(ctx, `SELECT COALESCE(SUM(started_ns>?),0),COALESCE(SUM(started_ns>=?),0) FROM diagnostic_free_admissions`, now.Add(-time.Minute).UnixNano(), start).Scan(&minute, &day); err != nil {
		return err
	}
	if minute >= rpm || day >= daily {
		reason := "minute_limit"
		if day >= daily {
			reason = "daily_limit"
		}
		slog.Warn("diagnostic_free_admission_rejected", "reason", reason, "minute_used", minute, "daily_used", day, "rpm_cap", rpm, "daily_cap", daily)
		return ErrLimited
	}
	// Retain yesterday to make clock rollback conservative; this is a limiter,
	// not usage billing. Actual provider outcomes remain in their normal ledger.
	if _, err = tx.ExecContext(ctx, `DELETE FROM diagnostic_free_admissions WHERE started_ns<?`, start-int64(24*time.Hour)); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO diagnostic_free_admissions(started_ns) VALUES(?)`, now.UnixNano()); err != nil {
		return err
	}
	return tx.Commit()
}
