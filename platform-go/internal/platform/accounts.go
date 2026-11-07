package platform

import (
	"database/sql"
	"encoding/json"
	"strings"
	"unicode/utf8"
)

// IssueAccount issues team access with a temporary password supplied by the
// organizer. The plaintext is never persisted in audit or the mail outbox.
func (s *Store) IssueAccount(token, email, password, team, actor, reason, ip string) (User, error) {
	return s.IssueAccountWithRole(token, email, password, team, "team", actor, reason, ip)
}

// Only an authenticated, changed-password administrator may create another staff
// account. The chosen role is explicit and captured in the same audit transaction.
func (s *Store) IssueAccountWithRole(token, email, password, team, role, actor, reason, ip string) (User, error) {
	if role == "" {
		role = "team"
	}
	if role != "team" && role != "admin" {
		return User{}, problem(400, "Invalid account role.")
	}

	u, err := s.Session(token)
	if err != nil {
		return User{}, err
	}
	if u.Role != "admin" || u.MustChange != 0 {
		return User{}, problem(403, "仅管理员可开通账号")
	}
	actor, reason = strings.TrimSpace(actor), strings.TrimSpace(reason)
	if actor == "" || len(actor) > 240 || utf8.RuneCountInString(reason) < 3 || utf8.RuneCountInString(reason) > 1000 {
		return User{}, problem(400, "请输入操作者和操作理由")
	}
	s.authSlots <- struct{}{}
	defer func() { <-s.authSlots }()
	return s.createAccount(email, password, team, role, func(tx *sql.Tx, uid, tid string) error {
		admin, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if admin.Role != "admin" || admin.MustChange != 0 {
			return problem(403, "仅管理员可开通账号")
		}
		after, _ := json.Marshal(map[string]any{"must_change": 1, "role": role})
		if err = s.audit(tx, admin, token, ip, actor, "account_created", uid, reason, "null", string(after)); err != nil {
			return err
		}
		_, err = tx.Exec("INSERT INTO outbox(id,event,team_id,payload,created) VALUES(?,'account_ready',?,'{\"delivery\":\"manual\",\"contains_password\":false}',?)", id(), tid, s.now())
		return err
	})
}
