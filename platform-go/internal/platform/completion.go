package platform

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

type ObjectStore interface {
	Provider() string
	PutFile(context.Context, string, string) (rollout.Info, error)
	WithFile(context.Context, string, string, func(string) error) error
}
type Pending struct {
	Lease      Lease  `json:"lease"`
	Reward     int    `json:"reward"`
	Seconds    string `json:"seconds"`
	Provenance string `json:"provenance"`
	SHA        string `json:"bundle_sha256,omitempty"`
}

func (s *Store) FinishStored(ctx context.Context, l Lease, reward int, seconds, key, sha string) error {
	if reward != 0 && reward != 1 {
		return problem(400, "Verifier reward must be binary")
	}
	if _, err := MeanSeconds([]string{seconds}); err != nil {
		return err
	}
	if s.Objects == nil {
		return errors.New("rollout store not configured")
	}
	if err := s.Objects.WithFile(ctx, key, sha, func(string) error { return nil }); err != nil {
		return err
	}
	return s.write(func(tx *sql.Tx) error {
		if err := s.fence(tx, l); err != nil {
			return err
		}
		var state string
		if err := tx.QueryRow("SELECT status FROM batches WHERE id=?", l.BatchID).Scan(&state); err != nil {
			return err
		}
		next := "completed"
		if state == "cancelling" {
			next = "cancelled"
		}
		if _, err := tx.Exec("UPDATE runs SET state=?,reward=?,seconds=?,object_key=?,object_sha=?,lease_until=NULL WHERE id=?", next, reward, seconds, key, sha, l.RunID); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE attempts SET state=?,finished=? WHERE id=?", next, s.now(), l.AttemptID); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", l.RunID); err != nil {
			return err
		}
		return s.finalize(tx, l.BatchID)
	})
}
func (s *Store) finalize(tx *sql.Tx, bid string) error {
	var state, kind, team, mode string
	if err := tx.QueryRow("SELECT status,kind,team_id,mode FROM batches WHERE id=?", bid).Scan(&state, &kind, &team, &mode); err != nil {
		return err
	}
	if state == "completed" || state == "cancelled" {
		return nil
	}
	if state == "cancelling" {
		var n int
		if err := tx.QueryRow("SELECT COUNT(*) FROM runs WHERE batch_id=? AND state IN ('running','uploading','attention')", bid).Scan(&n); err != nil {
			return err
		}
		if n == 0 {
			_, err := tx.Exec("UPDATE batches SET status='cancelled' WHERE id=?", bid)
			return err
		}
		return nil
	}
	rows, err := tx.Query("SELECT state,reward,seconds,object_key,object_sha FROM runs WHERE batch_id=?", bid)
	if err != nil {
		return err
	}
	total, passed := 0, 0
	valid := true
	durations := []string{}
	for rows.Next() {
		var st string
		var r sql.NullInt64
		var sec, key, sha sql.NullString
		if err = rows.Scan(&st, &r, &sec, &key, &sha); err != nil {
			rows.Close()
			return err
		}
		total++
		if st != "completed" || !r.Valid || (r.Int64 != 0 && r.Int64 != 1) || !sec.Valid || !key.Valid || !sha.Valid || key.String == "" || sha.String == "" {
			valid = false
		}
		passed += int(r.Int64)
		durations = append(durations, sec.String)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	expected, err := batchTaskCount(tx, bid, kind, mode)
	if err != nil {
		return err
	}
	if !valid || total != expected {
		return nil
	}
	mean, err := MeanSeconds(durations)
	if err != nil {
		return err
	}
	if _, err = tx.Exec("UPDATE batches SET status='completed',passed=?,mean=? WHERE id=?", passed, mean, bid); err != nil {
		return err
	}
	payload, _ := json.Marshal(map[string]string{"batch_id": bid, "mode": mode})
	_, err = tx.Exec("INSERT INTO outbox(id,event,team_id,payload,created) VALUES(?,'evaluation_completed',?,?,?)", id(), team, string(payload), s.now())
	return err
}

// RetryZeroResourceStartup requeues the original attempt exactly once when the
// trusted controller failed before creating any E2B resource. This is safe
// because there is no remote sandbox to reconcile; later startup failures are
// still held for staff review.
func (s *Store) RetryZeroResourceStartup(l Lease, reason string) error {
	return s.write(func(tx *sql.Tx) error {
		if l.Attempt != 1 {
			return errors.New("zero-resource startup retry already used")
		}
		if err := s.fence(tx, l); err != nil {
			return err
		}
		var state string
		if err := tx.QueryRow("SELECT status FROM batches WHERE id=?", l.BatchID).Scan(&state); err != nil {
			return err
		}
		if state != "testing" {
			return errors.New("batch is not testing")
		}
		result, err := tx.Exec("UPDATE runs SET state='queued',error='platform:startup_retry_scheduled',owner=NULL,lease_until=NULL WHERE id=? AND state='running' AND attempt=?", l.RunID, l.Attempt)
		if err != nil {
			return err
		}
		if n, err := result.RowsAffected(); err != nil || n != 1 {
			return errors.New("startup failure is not the active attempt")
		}
		if _, err := tx.Exec("UPDATE attempts SET state='failed',finished=?,error='platform:startup_failure_retried' WHERE id=? AND state='running'", s.now(), l.AttemptID); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", l.RunID); err != nil {
			return err
		}
		_, err = tx.Exec("INSERT INTO audit(id,created,actor,action,object_id,reason) VALUES(?,?,'automatic-startup-retry','startup_retry',?,?)", id(), s.now(), l.RunID, reason)
		return err
	})
}

// RetryPlatformFailure requeues one genuine infrastructure failure after the
// trusted executor has returned. It never touches model, harness, verifier,
// evidence or budget failures, and it refuses unresolved worker-loss rows
// because those still reserve a remote-sandbox slot. A run gets at most one
// automatic retry; later failures require an explicit team or staff action.
func (s *Store) RetryPlatformFailure(l Lease, reason string) error {
	return s.write(func(tx *sql.Tx) error {
		var state, failure, batchState string
		var attempt int
		if err := tx.QueryRow(`SELECT r.state,r.error,r.attempt,b.status FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.id=? AND r.batch_id=?`, l.RunID, l.BatchID).Scan(&state, &failure, &attempt, &batchState); err != nil {
			return err
		}
		if state != "attention" || !oneOf(batchState, "attention", "testing") || failure != "platform:operator_review_required" || attempt >= 2 {
			return errors.New("platform failure is not eligible for automatic retry")
		}
		if _, err := tx.Exec("UPDATE runs SET state='queued',error='platform:auto_retry_scheduled',owner=NULL,lease_until=NULL WHERE id=? AND state='attention' AND error=?", l.RunID, failure); err != nil {
			return err
		}
		if err := s.refreshBatchAfterTaskAction(tx, l.BatchID); err != nil {
			return err
		}
		_, err := tx.Exec("INSERT INTO audit(id,created,actor,action,object_id,reason) VALUES(?,?,'automatic-worker','platform_retry',?,?)", id(), s.now(), l.RunID, reason)
		return err
	})
}

// CancelFailedController closes an attempt whose trusted controller exited
// after confirming every remote resource was destroyed. This is cancellation,
// not an unknown-resource recovery: the execution slot is no longer uncertain.
func (s *Store) CancelFailedController(l Lease) error {
	return s.write(func(tx *sql.Tx) error {
		if err := s.fence(tx, l); err != nil {
			return err
		}
		var state string
		if err := tx.QueryRow("SELECT status FROM batches WHERE id=?", l.BatchID).Scan(&state); err != nil {
			return err
		}
		if state != "cancelling" {
			return errors.New("batch is not cancelling")
		}
		if _, err := tx.Exec("UPDATE runs SET state='cancelled',error='cancelled:controller_stopped',owner=NULL,lease_until=NULL WHERE id=?", l.RunID); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE attempts SET state='interrupted',finished=?,error='platform:cancelled' WHERE id=?", s.now(), l.AttemptID); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", l.RunID); err != nil {
			return err
		}
		return s.finalize(tx, l.BatchID)
	})
}

func (s *Store) Fail(l Lease, category string) error {
	// Callers retain detailed private error evidence separately; the task row gets
	// only a controlled category, never upstream bodies, tokens or hidden inputs.
	switch category {
	case "platform", "model", "harness", "verifier", "cancelled":
	default:
		category = "platform"
	}
	return s.write(func(tx *sql.Tx) error {
		if err := s.fence(tx, l); err != nil {
			return err
		}
		var bstate string
		if err := tx.QueryRow("SELECT status FROM batches WHERE id=?", l.BatchID).Scan(&bstate); err != nil {
			return err
		}
		state := "attention"
		if bstate == "cancelling" {
			state = "cancelled"
		}
		if _, err := tx.Exec("UPDATE runs SET state=?,error=?,lease_until=NULL WHERE id=?", state, category+":operator_review_required", l.RunID); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE attempts SET state='failed',finished=?,error=? WHERE id=?", s.now(), category, l.AttemptID); err != nil {
			return err
		}
		if _, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=?", l.RunID); err != nil {
			return err
		}
		if state == "attention" {
			_, err := tx.Exec("UPDATE batches SET status='attention' WHERE id=?", l.BatchID)
			return err
		}
		return s.finalize(tx, l.BatchID)
	})
}
func syncWrite(path string, data []byte) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".pending-")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if _, err = f.Write(data); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(name, path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
func (s *Store) CompleteFixture(ctx context.Context, l Lease, reward int, seconds, trace string) error {
	if reward != 0 && reward != 1 {
		return problem(400, "Verifier reward must be binary")
	}
	if _, err := MeanSeconds([]string{seconds}); err != nil {
		return err
	}
	if l.Mode != "simulated" {
		return problem(400, "Fixture completion is simulated-only")
	}
	if err := s.ValidateLease(l); err != nil {
		return err
	}
	cache := filepath.Join(s.Root, "cache", l.RunID, strconv.Itoa(l.Attempt))
	if err := os.MkdirAll(cache, 0700); err != nil {
		return err
	}
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tarw := tar.NewWriter(gz)
	metadata, _ := json.Marshal(map[string]any{"provenance": "simulated", "reward": reward, "seconds": seconds, "run_id": l.RunID, "attempt": l.Attempt})
	for _, f := range []struct {
		name string
		data []byte
	}{{"platform-result.json", metadata}, {"public-view.txt", []byte(trace)}} {
		if err := tarw.WriteHeader(&tar.Header{Name: f.name, Size: int64(len(f.data)), Mode: 0600}); err != nil {
			return err
		}
		if _, err := tarw.Write(f.data); err != nil {
			return err
		}
	}
	if err := tarw.Close(); err != nil {
		return err
	}
	if err := gz.Close(); err != nil {
		return err
	}
	bundle := filepath.Join(cache, "rollout.tar.gz")
	if err := syncWrite(bundle, buf.Bytes()); err != nil {
		return err
	}
	pending := Pending{Lease: l, Reward: reward, Seconds: seconds, Provenance: "simulated", SHA: shaBytes(buf.Bytes())}
	data, _ := json.Marshal(pending)
	if err := syncWrite(filepath.Join(cache, "pending.json"), data); err != nil {
		return err
	}
	// Publish durable evidence before the upload-state commit. A crash before
	// publication leaves a recoverable running fixture; after publication the
	// same attempt can be resumed without executing the fixture again.
	if err := s.BeginUpload(l); err != nil {
		return err
	}
	return s.uploadPending(ctx, cache, pending)
}
func (s *Store) uploadPending(ctx context.Context, cache string, p Pending) error {
	if s.Objects == nil {
		return errors.New("rollout store not configured")
	}
	file := filepath.Join(cache, "rollout.tar.gz")
	if p.SHA != "" {
		sha, _, err := rollout.Digest(file)
		if err != nil {
			return err
		}
		if sha != p.SHA {
			return errors.New("pending rollout integrity failed")
		}
	}
	key := fmt.Sprintf("%s/%s/%s/%s/%d.tar.gz", p.Lease.TeamID, p.Lease.VersionID, p.Lease.BatchID, p.Lease.RunID, p.Lease.Attempt)
	if err := s.ValidateLease(p.Lease); err != nil {
		return err
	}
	info, err := s.Objects.PutFile(ctx, key, file)
	if err != nil {
		return err
	}
	if err = s.FinishStored(ctx, p.Lease, p.Reward, p.Seconds, key, info.SHA256); err != nil {
		return err
	}
	return os.RemoveAll(cache)
}

// ResumeUploadLease is narrower than reclaiming execution: same attempt, same
// evidence and no agent rerun. A simulated run with durable pending evidence
// may also bridge the crash before its upload-state commit; scientific running
// leases are never promoted here because sandbox cleanup requires its own fence.
func (s *Store) ResumeUploadLease(l Lease) error {
	if IsScientificMode(l.Mode) {
		if err := s.scientificCleanupConfirmed(l); err != nil {
			return err
		}
	}
	return s.write(func(tx *sql.Tx) error {
		var n int
		if err := tx.QueryRow(`SELECT COUNT(*) FROM runs r JOIN attempts a ON a.run_id=r.id AND a.number=r.attempt JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id WHERE r.id=? AND r.batch_id=? AND r.owner=? AND r.attempt=? AND (r.state='uploading' OR (r.state='running' AND b.mode='simulated')) AND a.id=? AND a.state='running' AND b.status IN ('testing','cancelling','attention') AND b.team_id=? AND b.version_id=? AND v.sha256=? AND b.mode=? AND b.kind=? AND r.ordinal=? AND r.public=?`, l.RunID, l.BatchID, l.Owner, l.Attempt, l.AttemptID, l.TeamID, l.VersionID, l.SHA256, l.Mode, l.Kind, l.Ordinal, l.Public).Scan(&n); err != nil {
			return err
		}
		if n != 1 {
			return problem(409, "Upload attempt no longer current")
		}
		if _, err := tx.Exec("UPDATE runs SET state='uploading',lease_until=? WHERE id=?", s.now()+leaseSeconds, l.RunID); err != nil {
			return err
		}
		_, err := tx.Exec("UPDATE run_tokens SET revoked=1 WHERE run_id=? AND attempt=?", l.RunID, l.Attempt)
		return err
	})
}
func (s *Store) ResumeUploads(ctx context.Context) (int, error) {
	files, err := filepath.Glob(filepath.Join(s.Root, "cache", "*", "*", "pending.json"))
	if err != nil {
		return 0, err
	}
	done := 0
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			return done, err
		}
		if len(data) > 65536 {
			return done, errors.New("oversized pending metadata")
		}
		var p Pending
		if err = json.Unmarshal(data, &p); err != nil {
			return done, err
		}
		if filepath.Base(filepath.Dir(filepath.Dir(file))) != p.Lease.RunID || filepath.Base(filepath.Dir(file)) != strconv.Itoa(p.Lease.Attempt) {
			return done, errors.New("pending path mismatch")
		}
		if err = s.ResumeUploadLease(p.Lease); err != nil {
			if status(err) != 409 {
				return done, err
			}
			cleaned, cleanupErr := s.cleanupCompletedPending(ctx, filepath.Dir(file), p)
			if cleanupErr != nil {
				return done, cleanupErr
			}
			if cleaned {
				done++
			}
			continue
		}
		if err = s.uploadPending(ctx, filepath.Dir(file), p); err != nil {
			return done, err
		}
		done++
	}
	return done, nil
}

// A crash after the result transaction but before cache deletion must not leave
// evidence on the VPS indefinitely. A rejected lease alone is never permission
// to delete: exact terminal identity, local digest and object readback must agree.
func (s *Store) cleanupCompletedPending(ctx context.Context, cache string, p Pending) (bool, error) {
	if p.SHA == "" || p.Provenance != p.Lease.Mode {
		return false, nil
	}
	l := p.Lease
	key := fmt.Sprintf("%s/%s/%s/%s/%d.tar.gz", l.TeamID, l.VersionID, l.BatchID, l.RunID, l.Attempt)
	var count int
	err := s.DB.QueryRow(`SELECT COUNT(*) FROM runs r
 JOIN attempts a ON a.run_id=r.id AND a.number=r.attempt
 JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id
 WHERE r.id=? AND r.batch_id=? AND r.attempt=? AND r.owner=? AND a.id=?
 AND r.state IN ('completed','cancelled') AND a.state=r.state
 AND b.team_id=? AND b.version_id=? AND v.sha256=? AND b.mode=? AND b.kind=?
 AND r.ordinal=? AND r.public=? AND r.reward=? AND r.seconds=? AND r.object_key=? AND r.object_sha=?`,
		l.RunID, l.BatchID, l.Attempt, l.Owner, l.AttemptID, l.TeamID, l.VersionID,
		l.SHA256, l.Mode, l.Kind, l.Ordinal, l.Public, p.Reward, p.Seconds, key, p.SHA).Scan(&count)
	if err != nil || count != 1 {
		return false, err
	}
	sha, _, err := rollout.Digest(filepath.Join(cache, "rollout.tar.gz"))
	if err != nil {
		return false, err
	}
	if sha != p.SHA {
		return false, errors.New("completed rollout cache integrity failed")
	}
	if s.Objects == nil {
		return false, errors.New("rollout store not configured")
	}
	if err = s.Objects.WithFile(ctx, key, p.SHA, func(string) error { return nil }); err != nil {
		return false, err
	}
	if err = os.RemoveAll(cache); err != nil {
		return false, err
	}
	return true, nil
}
func (s *Store) Trace(ctx context.Context, token, bid string, ordinal int) (map[string]any, error) {
	u, err := s.Session(token)
	if err != nil {
		return nil, err
	}
	if u.MustChange != 0 {
		return nil, problem(403, "请先修改初始密码")
	}
	var key, sha, mode string
	err = s.DB.QueryRow(`SELECT r.object_key,r.object_sha,b.mode FROM runs r JOIN batches b ON b.id=r.batch_id WHERE b.id=? AND b.team_id=? AND b.kind='public' AND b.status='completed' AND r.ordinal=? AND r.public=1`, bid, u.TeamID, ordinal).Scan(&key, &sha, &mode)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, problem(404, "轨迹不可访问")
	}
	if err != nil {
		return nil, err
	}
	if s.Objects == nil {
		return nil, problem(503, "轨迹存储不可用")
	}
	var text string
	err = s.Objects.WithFile(ctx, key, sha, func(file string) error {
		f, err := os.Open(file)
		if err != nil {
			return err
		}
		defer f.Close()
		gz, err := gzip.NewReader(f)
		if err != nil {
			return err
		}
		defer gz.Close()
		tr := tar.NewReader(io.LimitReader(gz, 100*1024*1024))
		for {
			hdr, err := tr.Next()
			if err == io.EOF {
				return errors.New("public view missing")
			}
			if err != nil {
				return err
			}
			if hdr.Name != "public-view.txt" {
				continue
			}
			if hdr.Typeflag != tar.TypeReg || hdr.Size > 500000 {
				return errors.New("invalid public view")
			}
			b, err := io.ReadAll(io.LimitReader(tr, 500001))
			if err != nil {
				return err
			}
			text = strings.ToValidUTF8(string(b), "�")
			return nil
		}
	})
	if err != nil {
		return nil, problem(503, "证据完整性或读取检查失败")
	}
	return map[string]any{"text": text, "mode": mode, "ordinal": ordinal, "filtered": true}, nil
}
