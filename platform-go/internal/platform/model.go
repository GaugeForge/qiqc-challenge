package platform

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"github.com/gaugeforge/qiqc-platform-go/internal/modelproxy"
)

func (s *Store) MintModelToken(l Lease, model string) (string, error) {
	if model == "" {
		return "", problem(400, "model required")
	}
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	token := "qr_" + base64.RawURLEncoding.EncodeToString(b)
	err := s.write(func(tx *sql.Tx) error {
		if err := s.fence(tx, l); err != nil {
			return err
		}
		lifetime := 1800
		if l.Mode == "scientific" {
			target, e := readRunScientificTarget(tx, l)
			if e != nil {
				return e
			}
			if target.Limits.Valid() {
				lifetime = target.Limits.BuildSeconds + target.Limits.SetupSeconds + target.AgentBudgetSeconds + target.Limits.VerifierSeconds + 900
			}
		}
		_, err := tx.Exec("INSERT INTO run_tokens(hash,run_id,attempt,model,expires,revoked) VALUES(?,?,?,?,?,0)", tokenHash(token), l.RunID, l.Attempt, model, s.now()+float64(lifetime))
		return err
	})
	return token, err
}
func (s *Store) AuthorizeModel(ctx context.Context, token, model string) (modelproxy.Grant, error) {
	g := modelproxy.Grant{TokenHash: tokenHash(token)}
	err := s.DB.QueryRowContext(ctx, `SELECT t.run_id,t.attempt,t.model FROM run_tokens t JOIN runs r ON r.id=t.run_id JOIN batches b ON b.id=r.batch_id WHERE t.hash=? AND t.revoked=0 AND t.expires>? AND t.attempt=r.attempt AND r.state='running' AND b.status='testing' AND r.lease_until>?`, g.TokenHash, s.now(), s.now()).Scan(&g.RunID, &g.Attempt, &g.Model)
	if err != nil || g.Model != model {
		return g, &modelproxy.HTTPError{Status: 403, Message: "Invalid run capability"}
	}
	var n int
	if err = s.DB.QueryRowContext(ctx, "SELECT COUNT(*) FROM usage WHERE run_id=? AND attempt=? AND COALESCE(error_class,'') NOT IN ('client_cancelled_before_upstream','admission_deadline_before_upstream','admission_rejected_before_upstream')", g.RunID, g.Attempt).Scan(&n); err != nil {
		return g, err
	}
	limit, e := runRequestLimit(s.DB, g.RunID)
	if e != nil {
		return g, e
	}
	if n >= limit {
		return g, &modelproxy.HTTPError{Status: 429, Message: "Run request limit exceeded"}
	}
	return g, nil
}
func (s *Store) RecordModelUsage(ctx context.Context, r modelproxy.UsageRecord) error {
	// The provider may have consumed tokens even if cancellation invalidated the
	// grant while streaming. Retain this usage; never reauthorize and discard it.
	var amount any
	var priceVersion any
	var raw string
	err := s.DB.QueryRowContext(ctx, "SELECT value FROM settings WHERE key='pricing'").Scan(&raw)
	if err != nil && err != sql.ErrNoRows {
		return err
	}
	if err == nil && r.Usage.InputTokens != nil && r.Usage.OutputTokens != nil {
		var p struct {
			Model   string
			Version string
			Input   float64 `json:"input_per_million"`
			Cached  float64 `json:"cached_per_million"`
			Output  float64 `json:"output_per_million"`
		}
		if json.Unmarshal([]byte(raw), &p) == nil && p.Model == r.Grant.Model {
			cached := int64(0)
			if r.Usage.CachedTokens != nil {
				cached = *r.Usage.CachedTokens
			}
			if cached < 0 {
				cached = 0
			}
			if cached > *r.Usage.InputTokens {
				cached = *r.Usage.InputTokens
			}
			amount = (float64(*r.Usage.InputTokens-cached)*p.Input + float64(cached)*p.Cached + float64(*r.Usage.OutputTokens)*p.Output) / 1e6
			priceVersion = p.Version
		}
	}
	_, err = s.DB.ExecContext(ctx, "INSERT INTO usage(id,run_id,attempt,model,created,status,input_tokens,output_tokens,cached_tokens,amount,price_version,source,request_id,upstream_attempt,error_class,request_correlation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", id(), r.Grant.RunID, r.Grant.Attempt, r.Grant.Model, s.now(), r.Status, r.Usage.InputTokens, r.Usage.OutputTokens, r.Usage.CachedTokens, amount, priceVersion, "go-model-gateway", r.Grant.RequestID, r.Attempt, r.ErrorClass, r.Grant.RequestCorrelation)
	return err
}

// ReserveModelRequest atomically consumes one provider-attempt admission before
// network I/O. In-flight and interrupted requests count toward the existing 100
// cap. Authorization rechecks alone do not consume admissions. The durable count
// survives gateway restart and is shared across rotated tokens for one attempt.
func (s *Store) ReserveModelRequest(ctx context.Context, g modelproxy.Grant) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	_, err := s.DB.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS model_admissions (
 run_id TEXT NOT NULL REFERENCES runs(id), attempt INTEGER NOT NULL,
 used INTEGER NOT NULL, PRIMARY KEY(run_id,attempt))`)
	if err != nil {
		return err
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var runID, model string
	var attempt int
	err = tx.QueryRowContext(ctx, `SELECT t.run_id,t.attempt,t.model FROM run_tokens t JOIN runs r ON r.id=t.run_id JOIN batches b ON b.id=r.batch_id WHERE t.hash=? AND t.revoked=0 AND t.expires>? AND t.attempt=r.attempt AND r.state='running' AND b.status='testing' AND r.lease_until>?`, g.TokenHash, s.now(), s.now()).Scan(&runID, &attempt, &model)
	if err != nil || runID != g.RunID || attempt != g.Attempt || model != g.Model {
		return &modelproxy.HTTPError{Status: 403, Message: "Invalid run capability"}
	}
	// Existing persisted usage predates admissions on migrated local fixtures.
	// Seed exactly once under the same immediate write transaction.
	_, err = tx.ExecContext(ctx, `INSERT INTO model_admissions(run_id,attempt,used) SELECT ?,?,COUNT(*) FROM usage WHERE run_id=? AND attempt=? AND COALESCE(error_class,'') NOT IN ('client_cancelled_before_upstream','admission_deadline_before_upstream','admission_rejected_before_upstream') ON CONFLICT(run_id,attempt) DO NOTHING`, g.RunID, g.Attempt, g.RunID, g.Attempt)
	if err != nil {
		return err
	}
	limit, e := runRequestLimit(tx, g.RunID)
	if e != nil {
		return e
	}
	result, err := tx.ExecContext(ctx, "UPDATE model_admissions SET used=used+1 WHERE run_id=? AND attempt=? AND used<?", g.RunID, g.Attempt, limit)
	if err != nil {
		return err
	}
	n, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return &modelproxy.HTTPError{Status: 429, Message: "Run request limit exceeded"}
	}
	return tx.Commit()
}
