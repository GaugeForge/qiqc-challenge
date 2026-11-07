package platform

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/mail"
	"strings"
	"unicode/utf8"
)

// AccountInput is never written to an audit record, response, or notification.
// The browser retains temporary credentials only for the operator's download.
type AccountInput struct {
	Email    string `json:"email"`
	Team     string `json:"team"`
	Password string `json:"password"`
}

type BulkAccountResult struct {
	Accounts []User `json:"accounts"`
	Count    int    `json:"count"`
}

// IssueTeamAccounts validates the complete import before hashing, then creates
// every team/account/audit/notification in one transaction. Uniqueness checks
// inside the transaction also protect concurrent and repeated submissions.
func (s *Store) IssueTeamAccounts(token string, rows []AccountInput, actor, reason, ip string) (BulkAccountResult, error) {
	empty := BulkAccountResult{}
	admin, err := s.Session(token)
	if err != nil {
		return empty, err
	}
	if admin.Role != "admin" || admin.MustChange != 0 {
		return empty, problem(403, "仅管理员可开通账号")
	}
	actor, reason = strings.TrimSpace(actor), strings.TrimSpace(reason)
	if actor == "" || len(actor) > 240 || utf8.RuneCountInString(reason) < 3 || utf8.RuneCountInString(reason) > 1000 {
		return empty, problem(400, "请输入操作者和操作理由")
	}
	if len(rows) < 1 || len(rows) > 200 {
		return empty, problem(400, "Provide between 1 and 200 team accounts.")
	}
	normalized := make([]AccountInput, len(rows))
	seen := map[string]bool{}
	for i, row := range rows {
		row.Email = strings.ToLower(strings.TrimSpace(row.Email))
		row.Team = strings.TrimSpace(row.Team)
		address, e := mail.ParseAddress(row.Email)
		if e != nil || address.Address != row.Email || len(row.Email) > 200 || row.Team == "" || !utf8.ValidString(row.Team) || utf8.RuneCountInString(row.Team) > 100 {
			return empty, problem(400, fmt.Sprintf("Row %d: enter a valid email and a team name of 1–100 characters.", i+1))
		}
		if validatePassword(row.Password) != nil {
			return empty, problem(400, fmt.Sprintf("Row %d: the temporary password must contain 12–200 characters.", i+1))
		}
		if seen[row.Email] {
			return empty, problem(400, fmt.Sprintf("Row %d: duplicate email in this import. No accounts were created.", i+1))
		}
		seen[row.Email] = true
		normalized[i] = row
	}
	s.authSlots <- struct{}{}
	defer func() { <-s.authSlots }()
	hashes := make([]string, len(rows))
	for i, row := range normalized {
		hashes[i], err = hashPassword(row.Password)
		if err != nil {
			return empty, err
		}
	}
	result := BulkAccountResult{Accounts: make([]User, 0, len(rows)), Count: len(rows)}
	err = s.write(func(tx *sql.Tx) error {
		// Recheck the session after password hashing, including password-reset revocation.
		current, e := s.session(tx, token)
		if e != nil {
			return e
		}
		if current.Role != "admin" || current.MustChange != 0 {
			return problem(403, "仅管理员可开通账号")
		}
		for i, row := range normalized {
			var count int
			if e = tx.QueryRow("SELECT COUNT(*) FROM users WHERE email=?", row.Email).Scan(&count); e != nil {
				return e
			}
			if count != 0 {
				return problem(409, fmt.Sprintf("Row %d: this email already has an account. No accounts were created; check the account directory before retrying.", i+1))
			}
		}
		for i, row := range normalized {
			uid, tid := id(), id()
			if _, e = tx.Exec("INSERT INTO teams(id,name) VALUES(?,?)", tid, row.Team); e != nil {
				return e
			}
			if _, e = tx.Exec("INSERT INTO users(id,email,password,team_id,role,must_change) VALUES(?,?,?,?,'team',1)", uid, row.Email, hashes[i], tid); e != nil {
				return e
			}
			after, _ := json.Marshal(map[string]any{"must_change": 1, "role": "team", "bulk_size": len(rows)})
			if e = s.audit(tx, current, token, ip, actor, "account_created", uid, reason, "null", string(after)); e != nil {
				return e
			}
			if _, e = tx.Exec("INSERT INTO outbox(id,event,team_id,payload,created) VALUES(?,'account_ready',?,'{\"delivery\":\"manual\",\"contains_password\":false}',?)", id(), tid, s.now()); e != nil {
				return e
			}
			result.Accounts = append(result.Accounts, User{ID: uid, Email: row.Email, TeamID: tid, Team: row.Team, Role: "team", MustChange: 1, Quotas: map[string]int{"full": 1, "public": 3, "infra": 10}})
		}
		return nil
	})
	if err != nil {
		return empty, err
	}
	return result, nil
}
