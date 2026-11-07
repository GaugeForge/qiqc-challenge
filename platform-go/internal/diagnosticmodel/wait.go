package diagnosticmodel

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"time"
)

// Wait shares durable pacing across processes/keys in one quota group. Waiting
// consumes the caller's existing deadline, never extends its scientific budget.
// Daily exhaustion fails immediately; local minute pressure never fabricates a
// provider HTTP 429. Failed/unknown network attempts retain their reservation.
func Wait(ctx context.Context, db *sql.DB, group string, rpm, daily int) error {
	return wait(ctx, db, group, rpm, daily, time.Now, time.Second*60)
}
func wait(ctx context.Context, db *sql.DB, group string, rpm, daily int, now func() time.Time, window time.Duration) error {
	started := time.Now()
	waited := false
	defer func() {
		if waited {
			slog.Info("provider_admission_wait_finished", "quota_group", group, "wait_ms", time.Since(started).Milliseconds(), "context_cancelled", ctx.Err() != nil)
		}
	}()
	for {
		delay, err := pacedReserve(ctx, db, group, rpm, daily, now(), window)
		if err != nil {
			return err
		}
		if delay <= 0 {
			return nil
		}
		if !waited {
			slog.Info("provider_admission_wait_started", "quota_group", group)
			waited = true
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}
func pacedReserve(ctx context.Context, db *sql.DB, group string, rpm, daily int, now time.Time, window time.Duration) (time.Duration, error) {
	if group == "" || len(group) > 64 || rpm < 1 || rpm > 100000 || daily < 1 || daily > 10000000 {
		return 0, errors.New("invalid provider pacing profile")
	}
	for _, c := range group {
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-' || c == '_') {
			return 0, errors.New("invalid quota group")
		}
	}
	if _, e := db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS provider_pacing_admissions(id INTEGER PRIMARY KEY,quota_group TEXT NOT NULL,started_ns INTEGER NOT NULL)`); e != nil {
		return 0, e
	}
	tx, e := db.BeginTx(ctx, nil)
	if e != nil {
		return 0, e
	}
	defer tx.Rollback()
	start := time.Date(now.UTC().Year(), now.UTC().Month(), now.UTC().Day(), 0, 0, 0, 0, time.UTC).UnixNano()
	var day, minute int
	var last, oldest sql.NullInt64
	e = tx.QueryRowContext(ctx, `SELECT COUNT(*),MAX(started_ns) FROM provider_pacing_admissions WHERE quota_group=? AND started_ns>=?`, group, start).Scan(&day, &last)
	if e != nil {
		return 0, e
	}
	if day >= daily {
		slog.Warn("diagnostic_free_admission_rejected", "reason", "daily_limit", "quota_group", group, "daily_used", day, "daily_cap", daily)
		return 0, ErrLimited
	}
	e = tx.QueryRowContext(ctx, `SELECT COUNT(*),MIN(started_ns),MAX(started_ns) FROM provider_pacing_admissions WHERE quota_group=? AND started_ns>?`, group, now.Add(-window).UnixNano()).Scan(&minute, &oldest, &last)
	if e != nil {
		return 0, e
	}
	ready := now.UnixNano()
	if last.Valid && last.Int64+int64(window)/int64(rpm) > ready {
		ready = last.Int64 + int64(window)/int64(rpm)
	}
	if minute >= rpm && oldest.Valid && oldest.Int64+int64(window) > ready {
		ready = oldest.Int64 + int64(window)
	}
	if ready > now.UnixNano() {
		return time.Duration(ready - now.UnixNano()), nil
	}
	if _, e = tx.ExecContext(ctx, `INSERT INTO provider_pacing_admissions(quota_group,started_ns) VALUES(?,?)`, group, now.UnixNano()); e != nil {
		return 0, e
	}
	if _, e = tx.ExecContext(ctx, `DELETE FROM provider_pacing_admissions WHERE started_ns<?`, start-int64(24*time.Hour)); e != nil {
		return 0, e
	}
	return 0, tx.Commit()
}
