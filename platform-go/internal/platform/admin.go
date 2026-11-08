package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"unicode/utf8"
)

type Action struct {
	TaskIndex *int               `json:"task_index,omitempty"`
	Calendar  *Calendar          `json:"calendar,omitempty"`
	Action    string             `json:"action"`
	Target    string             `json:"target"`
	Decision  string             `json:"decision"`
	Actor     string             `json:"actor"`
	Reason    string             `json:"reason"`
	Kind      string             `json:"kind"`
	Delta     int                `json:"delta"`
	Paused    bool               `json:"paused"`
	Rates     map[string]float64 `json:"rates"`
	Idem      string             `json:"idempotency_key"`
	Force     bool               `json:"force,omitempty"`
}

func (s *Store) AdminAction(token string, a Action, ip string) error {
	if a.Action == "review" {
		return s.Review(token, a.Target, a.Decision, a.Actor, a.Reason, ip)
	}
	a.Actor = strings.TrimSpace(a.Actor)
	a.Reason = strings.TrimSpace(a.Reason)
	return s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.Role != "admin" || u.MustChange != 0 {
			return problem(403, "仅管理员可访问")
		}
		if a.Actor == "" || len(a.Actor) > 240 || utf8.RuneCountInString(a.Reason) < 3 || utf8.RuneCountInString(a.Reason) > 1000 || len(a.Idem) > 120 {
			return problem(400, "请输入操作者和操作理由")
		}
		if a.Idem != "" {
			payload, _ := json.Marshal(a)
			hash := shaBytes(payload)
			var previous string
			err = tx.QueryRow("SELECT request_hash FROM admin_operations WHERE user_id=? AND operation_key=?", u.ID, a.Idem).Scan(&previous)
			if err == nil {
				if previous != hash {
					return problem(409, "幂等键已绑定其他管理操作")
				}
				return nil
			}
			if !errors.Is(err, sql.ErrNoRows) {
				return err
			}
			if _, err = tx.Exec("INSERT INTO admin_operations(user_id,operation_key,request_hash,created) VALUES(?,?,?,?)", u.ID, a.Idem, hash, s.now()); err != nil {
				return err
			}
		}
		record := func(action, target string, before, after any) error {
			b, _ := json.Marshal(before)
			c, _ := json.Marshal(after)
			return s.audit(tx, u, token, ip, a.Actor, action, target, a.Reason, string(b), string(c))
		}
		switch a.Action {
		case "scientific_target":
			before, after, err := s.bindScientificTarget(tx, a.Target, a.TaskIndex)
			if err != nil {
				return err
			}
			if before != nil {
				return nil
			}
			return record("scientific_target_bound", a.Target, nil, after)
		case "calendar":
			if a.Calendar == nil {
				return problem(400, "缺少日历配置")
			}
			previous, err := setCalendar(tx, *a.Calendar)
			if err != nil {
				return err
			}
			return record("calendar_update", "competition", previous, *a.Calendar)
		case "pause", "close_demo":
			key, action, target, next := "paused", "queue_pause", "queue", a.Paused
			if a.Action == "close_demo" {
				key, action, target, next = "competition_closed", "competition_close", "competition", true
			}
			var raw string
			if err = tx.QueryRow("SELECT value FROM settings WHERE key=?", key).Scan(&raw); err != nil {
				return err
			}
			before := raw == "true"
			if before == next {
				return nil
			}
			if err = record(action, target, before, next); err != nil {
				return err
			}
			value := "false"
			if next {
				value = "true"
			}
			_, err = tx.Exec("UPDATE settings SET value=? WHERE key=?", value, key)
			return err
		case "quota":
			if _, ok := kinds[a.Kind]; !ok || a.Delta == 0 || a.Delta < -20 || a.Delta > 20 {
				return problem(400, "无效额度调整")
			}
			var exists int
			if err = tx.QueryRow("SELECT COUNT(*) FROM teams WHERE id=?", a.Target).Scan(&exists); err != nil {
				return err
			}
			if exists == 0 {
				return problem(404, "队伍不存在")
			}
			quota, e := quotas(tx, a.Target)
			if e != nil {
				return e
			}
			before := quota[a.Kind]
			if before+a.Delta < 0 {
				return problem(409, "剩余次数不能为负")
			}
			if err = record("quota_adjust", a.Target, before, before+a.Delta); err != nil {
				return err
			}
			_, err = tx.Exec("INSERT INTO ledger(id,team_id,kind,delta,action,batch_id,reason,created) VALUES(?,?,?,?,'adjustment',NULL,?,?)", id(), a.Target, a.Kind, a.Delta, a.Reason, s.now())
			return err
		case "retry", "retry_task", "invalidate":
			var state string
			var invalid int
			if err = tx.QueryRow("SELECT status,invalid FROM batches WHERE id=?", a.Target).Scan(&state, &invalid); errors.Is(err, sql.ErrNoRows) {
				return problem(404, "评测不存在")
			} else if err != nil {
				return err
			}
			if a.Action == "retry" || a.Action == "retry_task" {
				if a.Action == "retry" && state != "attention" {
					return problem(409, "只有待处理平台故障可重试")
				}
				if a.Action == "retry_task" && state != "attention" && state != "testing" {
					return problem(409, "只有待处理或进行中的评测可重试单题")
				}
				where := "batch_id=? AND state='attention'"
				args := []any{a.Target}
				if a.Action == "retry_task" {
					if a.TaskIndex == nil || *a.TaskIndex < 0 {
						return problem(400, "缺少有效题目编号")
					}
					var selectedState string
					if err = tx.QueryRow("SELECT state FROM runs WHERE batch_id=? AND ordinal=?", a.Target, *a.TaskIndex).Scan(&selectedState); errors.Is(err, sql.ErrNoRows) {
						return problem(404, "题目不存在")
					} else if err != nil {
						return err
					}
					if selectedState != "attention" {
						return problem(409, "只能重试待处理的题目")
					}
					where += " AND ordinal=?"
					args = append(args, *a.TaskIndex)
				}
				var unresolved int
				if err = tx.QueryRow("SELECT COUNT(*) FROM runs WHERE "+where+" AND error=?", append(args, lostWorkerError)...).Scan(&unresolved); err != nil {
					return err
				}
				if unresolved > 0 && !a.Force {
					return problem(409, "远端沙箱尚未完成回收对账。若已确认这是平台故障，请勾选强制重跑并记录理由。")
				}
				if a.Action == "retry_task" {
					if err = record("retry_task", a.Target, map[string]any{"ordinal": *a.TaskIndex, "state": "attention"}, map[string]any{"ordinal": *a.TaskIndex, "state": "queued", "forced": a.Force}); err != nil {
						return err
					}
					result, err := tx.Exec("UPDATE runs SET state='queued',owner=NULL,lease_until=0,error=NULL,failure_category=NULL,failure_phase=NULL,failure_code=NULL WHERE "+where, args...)
					if err != nil {
						return err
					}
					affected, err := result.RowsAffected()
					if err != nil {
						return err
					}
					if affected != 1 {
						return problem(409, "只能重试待处理的题目")
					}
					return s.refreshBatchAfterTaskAction(tx, a.Target)
				}
				if err = record("retry", a.Target, "attention", map[string]any{"state": "testing", "forced": a.Force}); err != nil {
					return err
				}
				if _, err = tx.Exec("UPDATE runs SET state='queued',owner=NULL,lease_until=0,error=NULL,failure_category=NULL,failure_phase=NULL,failure_code=NULL WHERE batch_id=? AND state='attention'", a.Target); err != nil {
					return err
				}
				return s.refreshBatchAfterTaskAction(tx, a.Target)
			}
			if state != "completed" {
				return problem(409, "只能对已完成成绩作废")
			}
			if invalid == 1 {
				return nil
			}
			if err = record("invalidate_score", a.Target, invalid, 1); err != nil {
				return err
			}
			_, err = tx.Exec("UPDATE batches SET invalid=1 WHERE id=?", a.Target)
			return err
		case "appeal":
			var state string
			if err = tx.QueryRow("SELECT status FROM appeals WHERE id=?", a.Target).Scan(&state); errors.Is(err, sql.ErrNoRows) {
				return problem(404, "申诉不存在")
			} else if err != nil {
				return err
			}
			if state == "answered" {
				return problem(409, "申诉已经答复")
			}
			if err = record("appeal_response", a.Target, state, "answered"); err != nil {
				return err
			}
			_, err = tx.Exec("UPDATE appeals SET status='answered',response=? WHERE id=?", a.Reason, a.Target)
			return err
		case "pricing":
			for _, key := range []string{"input_per_million", "cached_per_million", "output_per_million"} {
				v, ok := a.Rates[key]
				if !ok || math.IsInf(v, 0) || math.IsNaN(v) || v < 0 || v > 1000000 {
					return problem(400, "请输入非负有限单价")
				}
			}
			version := id()
			pricing := map[string]any{"version": version, "model": s.Model, "currency": "USD", "basis": "operator-estimate", "created": s.now()}
			for k, v := range a.Rates {
				pricing[k] = v
			}
			var before any
			var raw string
			if e := tx.QueryRow("SELECT value FROM settings WHERE key='pricing'").Scan(&raw); e == nil {
				json.Unmarshal([]byte(raw), &before)
			} else if !errors.Is(e, sql.ErrNoRows) {
				return e
			}
			if err = record("pricing_update", s.Model, before, pricing); err != nil {
				return err
			}
			data, _ := json.Marshal(pricing)
			if _, err = tx.Exec("INSERT OR REPLACE INTO settings(key,value) VALUES('pricing',?)", string(data)); err != nil {
				return err
			}
			// Same estimate semantics as the legacy worker. Unknown usage remains NULL.
			_, err = tx.Exec(`UPDATE usage SET amount=((input_tokens-MIN(input_tokens,MAX(0,COALESCE(cached_tokens,0))))*?+MIN(input_tokens,MAX(0,COALESCE(cached_tokens,0)))*?+output_tokens*?)/1000000.0,price_version=? WHERE amount IS NULL AND model=? AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL`, a.Rates["input_per_million"], a.Rates["cached_per_million"], a.Rates["output_per_million"], version, s.Model)
			return err
		default:
			return problem(400, "未知管理操作")
		}
	})
}
func (s *Store) Appeal(token, body string) error {
	if utf8.RuneCountInString(body) < 10 || utf8.RuneCountInString(body) > 4000 {
		return problem(400, "申诉内容应为 10–4000 字符")
	}
	return s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.Role != "team" || u.MustChange != 0 {
			return problem(403, "请使用已改密队伍账号")
		}
		calendar, err := loadCalendar(tx)
		if err != nil {
			return err
		}
		if calendar.Mode == "scheduled" && !s.Now().Before(calendar.AppealClosesAt) {
			return problem(409, "申诉受理已截止")
		}
		var n int
		if err = tx.QueryRow("SELECT COUNT(*) FROM appeals WHERE team_id=?", u.TeamID).Scan(&n); err != nil {
			return err
		}
		if n > 0 {
			return problem(409, "每队只有一次申诉机会")
		}
		_, err = tx.Exec("INSERT INTO appeals(id,team_id,body,status,created) VALUES(?,?,?,'open',?)", id(), u.TeamID, body, s.now())
		return err
	})
}
func (s *Store) MyAppeal(token string) (map[string]any, error) {
	u, err := s.Session(token)
	if err != nil {
		return nil, err
	}
	if u.MustChange != 0 {
		return nil, problem(403, "请先修改初始密码")
	}
	var body, state string
	var created float64
	var response sql.NullString
	err = s.DB.QueryRow("SELECT body,status,created,response FROM appeals WHERE team_id=?", u.TeamID).Scan(&body, &state, &created, &response)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var reply any
	if response.Valid {
		reply = response.String
	}
	return map[string]any{"body": body, "status": state, "created": created, "response": reply}, nil
}
