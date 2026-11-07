package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
)

const supportFields = "t.id,t.team_id,g.name,t.subject,COALESCE(t.batch_id,''),COALESCE(t.submission_sha,''),COALESCE(t.event_id,''),t.status,t.created,t.updated"

func supportRow(row interface{ Scan(...any) error }) (SupportTicket, error) {
	var t SupportTicket
	err := row.Scan(&t.ID, &t.TeamID, &t.Team, &t.Subject, &t.BatchID, &t.SubmissionSHA, &t.EventID, &t.Status, &t.Created, &t.Updated)
	return t, err
}
func ticketFor(q queryer, u User, ticketID string, withMessages bool) (SupportTicket, error) {
	t, err := supportRow(q.QueryRow("SELECT "+supportFields+" FROM support_tickets t JOIN teams g ON g.id=t.team_id WHERE t.id=? AND (?='admin' OR t.team_id=?)", ticketID, u.Role, u.TeamID))
	if errors.Is(err, sql.ErrNoRows) {
		return t, problem(404, "工单不可访问")
	}
	if err != nil || !withMessages {
		return t, err
	}
	rows, err := q.Query("SELECT id,sequence,author_role,body,created FROM support_messages WHERE ticket_id=? ORDER BY sequence LIMIT ?", ticketID, maxSupportMessages)
	if err != nil {
		return t, err
	}
	defer rows.Close()
	t.Messages = []SupportMessage{}
	for rows.Next() {
		var m SupportMessage
		if err = rows.Scan(&m.ID, &m.Sequence, &m.AuthorRole, &m.Body, &m.Created); err != nil {
			return t, err
		}
		t.Messages = append(t.Messages, m)
	}
	return t, rows.Err()
}
func optionalCommunity(value string) any {
	if value == "" {
		return nil
	}
	return value
}
func validateSupportRefs(tx *sql.Tx, team string, in SupportInput) (string, error) {
	sha := in.SubmissionSHA
	if sha != "" && !validDigest(sha) {
		return "", problem(400, "提交摘要格式无效")
	}
	if in.BatchID != "" {
		var batchSHA string
		err := tx.QueryRow("SELECT v.sha256 FROM batches b JOIN versions v ON v.id=b.version_id WHERE b.id=? AND b.team_id=?", in.BatchID, team).Scan(&batchSHA)
		if errors.Is(err, sql.ErrNoRows) {
			return "", problem(404, "关联记录不可访问")
		}
		if err != nil {
			return "", err
		}
		if sha != "" && sha != batchSHA {
			return "", problem(409, "评测与提交摘要不匹配")
		}
		sha = batchSHA
	}
	if sha != "" {
		var n int
		if err := tx.QueryRow("SELECT COUNT(*) FROM versions WHERE team_id=? AND sha256=?", team, sha).Scan(&n); err != nil {
			return "", err
		}
		if n == 0 {
			return "", problem(404, "关联记录不可访问")
		}
	}
	if in.EventID != "" {
		var payload string
		err := tx.QueryRow("SELECT COALESCE(payload,'{}') FROM outbox WHERE id=? AND team_id=?", in.EventID, team).Scan(&payload)
		if errors.Is(err, sql.ErrNoRows) {
			return "", problem(404, "关联记录不可访问")
		}
		if err != nil {
			return "", err
		}
		var event struct {
			BatchID string `json:"batch_id"`
		}
		if len(payload) > 65536 || json.Unmarshal([]byte(payload), &event) != nil {
			return "", problem(409, "关联事件记录无效，请联系赛事组")
		}
		if event.BatchID != "" && in.BatchID != "" && event.BatchID != in.BatchID {
			return "", problem(409, "事件与评测记录不匹配")
		}
		if event.BatchID != "" && sha != "" {
			var eventSHA string
			err = tx.QueryRow("SELECT v.sha256 FROM batches b JOIN versions v ON v.id=b.version_id WHERE b.id=? AND b.team_id=?", event.BatchID, team).Scan(&eventSHA)
			if errors.Is(err, sql.ErrNoRows) {
				return "", problem(404, "关联记录不可访问")
			}
			if err != nil {
				return "", err
			}
			if eventSHA != sha {
				return "", problem(409, "事件与提交摘要不匹配")
			}
		}
	}
	return sha, nil
}
func (s *Store) supportTeamAudit(tx *sql.Tx, u User, token, ip, action, ticketID, before, after string) error {
	_, err := tx.Exec(`INSERT INTO audit(id,created,user_id,actor,action,object_id,reason,before_json,after_json,session_fingerprint,ip) VALUES(?,?,?,?,?,?,?,?,?,?,?)`, id(), s.now(), u.ID, u.Team, action, ticketID, "队伍提交支持请求或追加信息", before, after, tokenHash(token)[:16], ip)
	return err
}
func (s *Store) CreateSupportTicket(token string, in SupportInput, ip string) (SupportTicket, error) {
	var result SupportTicket
	in.Subject = strings.TrimSpace(in.Subject)
	in.Body = strings.TrimSpace(in.Body)
	in.BatchID = strings.TrimSpace(in.BatchID)
	in.SubmissionSHA = strings.TrimSpace(in.SubmissionSHA)
	in.EventID = strings.TrimSpace(in.EventID)
	err := s.write(func(tx *sql.Tx) error {
		u, err := communitySession(s, tx, token)
		if err != nil {
			return err
		}
		if u.Role != "team" {
			return problem(403, "仅队伍可提交支持工单")
		}
		if !communityText(in.Subject, 3, 120) || !communityText(in.Body, 10, 8000) || len(in.BatchID) > 120 || len(in.EventID) > 120 {
			return problem(400, "工单标题、正文或关联编号无效")
		}
		old, err := operationReplay(tx, u, in.Idem, "support_create", in)
		if err != nil {
			return err
		}
		if old != "" {
			result, err = ticketFor(tx, u, old, true)
			return err
		}
		sha, err := validateSupportRefs(tx, u.TeamID, in)
		if err != nil {
			return err
		}
		var count int
		if err = tx.QueryRow("SELECT COUNT(*) FROM support_tickets WHERE team_id=? AND status='open'", u.TeamID).Scan(&count); err != nil {
			return err
		}
		if count >= maxOpenSupportTickets {
			return problem(429, "未结束支持工单达到20个，请先处理已有工单")
		}
		ticketID, now := id(), s.now()
		if _, err = tx.Exec("INSERT INTO support_tickets(id,team_id,subject,batch_id,submission_sha,event_id,status,created,updated) VALUES(?,?,?,?,?,?,'open',?,?)", ticketID, u.TeamID, in.Subject, optionalCommunity(in.BatchID), optionalCommunity(sha), optionalCommunity(in.EventID), now, now); err != nil {
			return err
		}
		if _, err = tx.Exec("INSERT INTO support_messages(id,ticket_id,sequence,author_id,author_role,body,created) VALUES(?,?,1,?,'team',?,?)", id(), ticketID, u.ID, in.Body, now); err != nil {
			return err
		}
		if err = s.supportTeamAudit(tx, u, token, ip, "support_created", ticketID, "null", `{"status":"open","sequence":1}`); err != nil {
			return err
		}
		if err = recordOperation(tx, u, in.Idem, "support_create", in, ticketID, now); err != nil {
			return err
		}
		result, err = ticketFor(tx, u, ticketID, true)
		return err
	})
	return result, err
}
func (s *Store) GetSupportTicket(token, ticketID string) (SupportTicket, error) {
	u, err := communitySession(s, s.DB, token)
	if err != nil {
		return SupportTicket{}, err
	}
	return ticketFor(s.DB, u, ticketID, true)
}
func (s *Store) ListSupportTickets(token string, page int) ([]SupportTicket, error) {
	if err := communityPage(page); err != nil {
		return nil, err
	}
	u, err := communitySession(s, s.DB, token)
	if err != nil {
		return nil, err
	}
	rows, err := s.DB.Query("SELECT "+supportFields+" FROM support_tickets t JOIN teams g ON g.id=t.team_id WHERE ?='admin' OR t.team_id=? ORDER BY t.updated DESC,t.id DESC LIMIT ? OFFSET ?", u.Role, u.TeamID, communityPageSize, page*communityPageSize)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []SupportTicket{}
	for rows.Next() {
		item, err := supportRow(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}
func (s *Store) ReplySupportTicket(token, ticketID string, in SupportReply, ip string) (SupportTicket, error) {
	var result SupportTicket
	in.Body = strings.TrimSpace(in.Body)
	in.Actor = strings.TrimSpace(in.Actor)
	in.Reason = strings.TrimSpace(in.Reason)
	err := s.write(func(tx *sql.Tx) error {
		u, err := communitySession(s, tx, token)
		if err != nil {
			return err
		}
		current, err := ticketFor(tx, u, ticketID, false)
		if err != nil {
			return err
		}
		if !communityText(in.Body, 3, 8000) {
			return problem(400, "回复长度为3至8000个字符")
		}
		if u.Role == "admin" {
			if err = communityAuditInput(in.Actor, in.Reason, in.Idem); err != nil {
				return err
			}
			if in.Status != "open" && in.Status != "resolved" && in.Status != "closed" {
				return problem(400, "请选择工单状态")
			}
		} else if in.Status != "" || in.Actor != "" || in.Reason != "" {
			return problem(403, "队伍不能设置管理员处理字段")
		}
		request := struct {
			TicketID string       `json:"ticket_id"`
			Reply    SupportReply `json:"reply"`
		}{ticketID, in}
		old, err := operationReplay(tx, u, in.Idem, "support_reply", request)
		if err != nil {
			return err
		}
		if old != "" {
			result, err = ticketFor(tx, u, old, true)
			return err
		}
		if current.Status == "closed" {
			return problem(409, "工单已关闭，请新建工单说明新的问题")
		}
		// Team follow-up cannot bypass the new-ticket protection by reopening
		// resolved threads. An administrator may reopen with an audited reason.
		if u.Role == "team" && current.Status != "open" {
			var open int
			if err = tx.QueryRow("SELECT COUNT(*) FROM support_tickets WHERE team_id=? AND status='open'", u.TeamID).Scan(&open); err != nil {
				return err
			}
			if open >= maxOpenSupportTickets {
				return problem(429, "未结束支持工单达到20个，请先处理已有工单")
			}
		}
		var sequence int
		if err = tx.QueryRow("SELECT COUNT(*) FROM support_messages WHERE ticket_id=?", ticketID).Scan(&sequence); err != nil {
			return err
		}
		if sequence >= maxSupportMessages {
			return problem(429, "工单达到100条记录，请新建工单引用原编号")
		}
		sequence++
		next := in.Status
		if u.Role == "team" {
			next = "open"
		}
		now := s.now()
		if _, err = tx.Exec("INSERT INTO support_messages(id,ticket_id,sequence,author_id,author_role,body,created) VALUES(?,?,?,?,?,?,?)", id(), ticketID, sequence, u.ID, u.Role, in.Body, now); err != nil {
			return err
		}
		if _, err = tx.Exec("UPDATE support_tickets SET status=?,updated=? WHERE id=?", next, now, ticketID); err != nil {
			return err
		}
		before, _ := json.Marshal(map[string]any{"status": current.Status, "sequence": sequence - 1})
		after, _ := json.Marshal(map[string]any{"status": next, "sequence": sequence})
		if u.Role == "admin" {
			err = s.audit(tx, u, token, ip, in.Actor, "support_admin_reply", ticketID, in.Reason, string(before), string(after))
		} else {
			err = s.supportTeamAudit(tx, u, token, ip, "support_team_reply", ticketID, string(before), string(after))
		}
		if err != nil {
			return err
		}
		if err = recordOperation(tx, u, in.Idem, "support_reply", request, ticketID, now); err != nil {
			return err
		}
		result, err = ticketFor(tx, u, ticketID, true)
		return err
	})
	return result, err
}
