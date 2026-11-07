package platform

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"
)

type SubmissionIntent struct {
	ID        string  `json:"id"`
	VersionID string  `json:"version_id"`
	SHA256    string  `json:"sha256"`
	Kind      string  `json:"kind"`
	Phase     string  `json:"phase"`
	State     string  `json:"state"`
	Reason    string  `json:"reason"`
	BatchID   string  `json:"batch_id"`
	Updated   float64 `json:"updated"`
}

const intentFields = "id,version_id,sha256,kind,phase,state,reason,COALESCE(batch_id,''),updated"

func scanIntent(row interface{ Scan(...any) error }) (SubmissionIntent, error) {
	var i SubmissionIntent
	err := row.Scan(&i.ID, &i.VersionID, &i.SHA256, &i.Kind, &i.Phase, &i.State, &i.Reason, &i.BatchID, &i.Updated)
	return i, err
}

func (s *Store) UploadAndQueue(token string, data []byte, kind, idem string) (SubmissionIntent, error) {
	var out SubmissionIntent
	u, err := s.Session(token)
	if err != nil {
		return out, err
	}
	if u.Role != "team" || u.MustChange != 0 {
		return out, problem(403, "Team account and password change required.")
	}
	if _, ok := kinds[kind]; !ok || utf8.RuneCountInString(idem) < 1 || utf8.RuneCountInString(idem) > 100 {
		return out, problem(400, "Evaluation type and idempotency key are required.")
	}
	old, err := scanIntent(s.DB.QueryRow("SELECT "+intentFields+" FROM submission_intents WHERE team_id=? AND idem=?", u.TeamID, idem))
	if err == nil {
		if old.SHA256 != shaBytes(data) || old.Kind != kind {
			return out, problem(409, "This upload key belongs to a different package or evaluation type.")
		}
		return old, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return out, err
	}
	c, err := loadCalendar(s.DB)
	if err != nil {
		return out, err
	}
	phase := c.Phase(s.Now()).Name
	if err = c.allow(kind, s.Now()); err != nil {
		return out, err
	}
	v, err := s.Upload(token, data)
	if err != nil {
		return out, err
	}
	err = s.write(func(tx *sql.Tx) error {
		current, e := s.session(tx, token)
		if e != nil {
			return e
		}
		if current.Role != "team" || current.MustChange != 0 {
			return problem(403, "Team account required.")
		}
		cal, e := loadCalendar(tx)
		if e != nil {
			return e
		}
		if cal.Phase(s.Now()).Name != phase {
			return problem(409, "Event phase changed during upload. No attempt charged.")
		}
		if e = cal.allow(kind, s.Now()); e != nil {
			return e
		}
		existing, e := scanIntent(tx.QueryRow("SELECT "+intentFields+" FROM submission_intents WHERE team_id=? AND idem=?", u.TeamID, idem))
		if e == nil {
			if existing.SHA256 != v.SHA256 || existing.Kind != kind {
				return problem(409, "Upload key conflict.")
			}
			out = existing
			return nil
		}
		if !errors.Is(e, sql.ErrNoRows) {
			return e
		}
		// Do not invent automatic deferred admission for a busy team. Existing rule
		// remains one active batch; a retained version is never charged silently.
		var n int
		if e = tx.QueryRow("SELECT COUNT(*) FROM batches WHERE team_id=? AND status IN ('queued','testing','attention','cancelling')", u.TeamID).Scan(&n); e != nil {
			return e
		}
		if n > 0 {
			return problem(409, "Your team already has an active evaluation. This version was retained; no attempt charged.")
		}
		out = SubmissionIntent{ID: id(), VersionID: v.ID, SHA256: v.SHA256, Kind: kind, Phase: phase, State: "reviewing", Reason: "Upload received. Automated review is in progress.", Updated: s.now()}
		_, e = tx.Exec("INSERT INTO submission_intents(id,user_id,team_id,version_id,sha256,kind,mode,phase,idem,state,reason,created,updated) VALUES(?,?,?,?,?,?,'scientific',?,?,?,?,?,?)", out.ID, u.ID, u.TeamID, v.ID, v.SHA256, kind, phase, idem, out.State, out.Reason, out.Updated, out.Updated)
		return e
	})
	return out, err
}
func (s *Store) SubmissionIntents(token string) ([]SubmissionIntent, error) {
	u, e := s.Session(token)
	if e != nil {
		return nil, e
	}
	if u.Role != "team" || u.MustChange != 0 {
		return nil, problem(403, "Team account required.")
	}
	rows, e := s.DB.Query("SELECT "+intentFields+" FROM submission_intents WHERE team_id=? ORDER BY created DESC LIMIT 100", u.TeamID)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []SubmissionIntent{}
	for rows.Next() {
		v, e := scanIntent(rows)
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// CancelSubmissionIntent withdraws a submission while it is still in the
// review/admission queue. No batch exists at this point, so no quota ledger
// entry has been written and no E2B slot can be occupied. The immutable
// version is marked cancelled so an in-flight reviewer cannot admit it after
// the participant has withdrawn the request.
func (s *Store) CancelSubmissionIntent(token, intentID string) error {
	if strings.TrimSpace(intentID) == "" || len(intentID) > 120 {
		return problem(400, "Invalid submission request")
	}
	return s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.Role != "team" || u.MustChange != 0 {
			return problem(403, "Team account required.")
		}
		var versionID, state string
		if err = tx.QueryRow("SELECT version_id,state FROM submission_intents WHERE id=? AND team_id=?", intentID, u.TeamID).Scan(&versionID, &state); errors.Is(err, sql.ErrNoRows) {
			return problem(404, "Submission request not found.")
		} else if err != nil {
			return err
		}
		if state != "reviewing" {
			return problem(409, "This submission has already left the review queue.")
		}
		now := s.now()
		if _, err = tx.Exec("UPDATE submission_intents SET state='cancelled',reason=?,batch_id=NULL,updated=? WHERE id=? AND team_id=? AND state='reviewing'", "Cancelled by the team before acceptance. No attempt charged.", now, intentID, u.TeamID); err != nil {
			return err
		}
		_, err = tx.Exec("UPDATE versions SET status='cancelled',reason=? WHERE id=? AND team_id=? AND status='pending_review'", "Submission cancelled by the team before acceptance. No attempt charged.", versionID, u.TeamID)
		return err
	})
}
func (s *Store) ProcessSubmissionIntents() error {
	rows, e := s.DB.Query("SELECT i.id,i.user_id,i.version_id,i.kind,i.mode,i.phase,v.status,v.reason FROM submission_intents i JOIN versions v ON v.id=i.version_id WHERE i.state='reviewing' ORDER BY i.created LIMIT 100")
	if e != nil {
		return e
	}
	type item struct{ id, user, version, kind, mode, phase, status, reason string }
	items := []item{}
	for rows.Next() {
		var i item
		if e = rows.Scan(&i.id, &i.user, &i.version, &i.kind, &i.mode, &i.phase, &i.status, &i.reason); e != nil {
			rows.Close()
			return e
		}
		items = append(items, i)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return e
	}
	for _, i := range items {
		state, reason, bid := "reviewing", i.reason, ""
		if i.status == "rejected" {
			state = "rejected"
		} else if i.status == "approved" {
			b, e := s.confirmIdentity(func(tx *sql.Tx) (User, error) {
				var u User
				err := tx.QueryRow("SELECT id,team_id,role,must_change FROM users WHERE id=?", i.user).Scan(&u.ID, &u.TeamID, &u.Role, &u.MustChange)
				return u, err
			}, i.version, i.kind, i.mode, "automatic:"+i.id, i.phase)
			if e == nil {
				state = "accepted"
				reason = "Evaluation accepted and queued."
				bid = b["id"].(string)
			} else if status(e) >= 500 {
				continue
			} else {
				state = "blocked"
				reason = e.Error()
			}
		} else {
			c, e := loadCalendar(s.DB)
			if e != nil {
				continue
			}
			if c.Phase(s.Now()).Name != i.phase {
				state = "blocked"
				reason = "Event phase changed before acceptance. No attempt charged."
			} else {
				continue
			}
		}
		if _, e = s.DB.Exec("UPDATE submission_intents SET state=?,reason=?,batch_id=NULLIF(?,''),updated=? WHERE id=? AND state='reviewing'", state, reason, bid, s.now(), i.id); e != nil {
			return e
		}
	}
	return nil
}

// Durable intent processing continues without a browser session. Idempotent
// acceptance handles a crash between the batch commit and intent update.
func (s *Store) RunAdmissionLoop(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := s.write(func(tx *sql.Tx) error {
				c, e := loadCalendar(tx)
				if e != nil {
					return e
				}
				_, e = s.observePhase(tx, c)
				return e
			}); err != nil {
				slog.Error("event phase observation failed; inspect calendar and clock health")
			}
			if err := s.ProcessSubmissionIntents(); err != nil {
				slog.Error("automatic submission processing failed; durable intents retained")
			}
		}
	}
}
func (s *Store) registerAutomaticSubmissions(mux *http.ServeMux) {
	mux.HandleFunc("/api/submissions", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
				v, e := s.SubmissionIntents(cookieToken(r))
				if e == nil {
					respond(w, v)
				}
				return e
			})(w, r)
			return
		}
		endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
			if _, e := s.Session(cookieToken(r)); e != nil {
				return e
			}
			select {
			case s.uploadSlots <- struct{}{}:
				defer func() { <-s.uploadSlots }()
			default:
				return problem(429, "Upload checks are busy. No attempt charged.")
			}
			data, e := ReadPackage(r.Body)
			if e != nil {
				return e
			}
			v, e := s.UploadAndQueue(cookieToken(r), data, r.Header.Get("X-QIQC-Evaluation"), r.Header.Get("Idempotency-Key"))
			if e == nil {
				respond(w, v)
			}
			return e
		})(w, r)
	})
	mux.HandleFunc("/api/submissions/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/submissions/"), "/")
		if len(parts) != 2 || parts[1] != "cancel" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusNotFound)
			respond(w, map[string]string{"error": "Submission request not found."})
			return
		}
		endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
			if err := s.CancelSubmissionIntent(cookieToken(r), parts[0]); err != nil {
				return err
			}
			respond(w, map[string]bool{"ok": true})
			return nil
		})(w, r)
	})
}
