package platform

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
)

func bulkAdmin(t *testing.T, s *Store) string {
	t.Helper()
	account(t, s, "bulk-admin@example.test", "admin")
	token, _, err := s.Login("bulk-admin@example.test", "Initial-password-123", "test-admin")
	if err != nil {
		t.Fatal(err)
	}
	if err = s.ChangePassword(token, "Permanent-admin-password-123"); err != nil {
		t.Fatal(err)
	}
	return token
}
func bulkRows(n int) []AccountInput {
	rows := make([]AccountInput, n)
	for i := range rows {
		rows[i] = AccountInput{Email: fmt.Sprintf("bulk-%d@example.test", i), Team: fmt.Sprintf("Team %d", i), Password: fmt.Sprintf("Unique-temporary-password-%d", i)}
	}
	return rows
}
func TestBulkAccountsAtomicCreationAndCredentials(t *testing.T) {
	s := testStore(t)
	token := bulkAdmin(t, s)
	rows := bulkRows(3)
	rows[0].Email = " BULK-0@EXAMPLE.TEST "
	result, err := s.IssueTeamAccounts(token, rows, "Organizer", "Participant onboarding", "test")
	if err != nil || result.Count != 3 {
		t.Fatal(result, err)
	}
	raw, _ := json.Marshal(result)
	for i, u := range result.Accounts {
		if u.MustChange != 1 || u.Role != "team" || u.Quotas["full"] != 1 || u.Quotas["public"] != 3 {
			t.Fatal(u)
		}
		session, logged, e := s.Login(u.Email, rows[i].Password, fmt.Sprint("team-ip-", i))
		if e != nil || logged.MustChange != 1 {
			t.Fatal("temporary login failed", e)
		}
		if _, e = s.IssueTeamAccounts(session, bulkRows(1), "Team", "Must reject", "test"); status(e) != 403 {
			t.Fatal("team accessed bulk", e)
		}
		var stored string
		if e = s.DB.QueryRow("SELECT password FROM users WHERE id=?", u.ID).Scan(&stored); e != nil {
			t.Fatal(e)
		}
		if stored == rows[i].Password || !passwordMatches(rows[i].Password, stored) || strings.Contains(string(raw), rows[i].Password) {
			t.Fatal("credential storage or response leak")
		}
		var audit string
		s.DB.QueryRow("SELECT after_json FROM audit WHERE object_id=? AND action='account_created'", u.ID).Scan(&audit)
		if strings.Contains(audit, rows[i].Password) || audit == "" {
			t.Fatal("audit invalid")
		}
	}
	var outbox int
	s.DB.QueryRow("SELECT count(*) FROM outbox WHERE event='account_ready'").Scan(&outbox)
	if outbox != 3 {
		t.Fatal(outbox)
	}
	conflict := []AccountInput{{Email: "new@example.test", Team: "New", Password: "Another-password-123"}, rows[2]}
	if _, e := s.IssueTeamAccounts(token, conflict, "Organizer", "Retry import", "test"); status(e) != 409 {
		t.Fatal(e)
	}
	var n int
	s.DB.QueryRow("SELECT count(*) FROM users WHERE email='new@example.test'").Scan(&n)
	if n != 0 {
		t.Fatal("partial import")
	}
}
func TestBulkAccountsRejectInvalidRowsAndRollback(t *testing.T) {
	s := testStore(t)
	token := bulkAdmin(t, s)
	bad := bulkRows(2)
	bad[1].Email = "BULK-0@EXAMPLE.TEST"
	cases := [][]AccountInput{nil, bulkRows(201), bad, {{Email: "ok@example.test", Team: "OK", Password: "Temporary-password-123"}, {Email: "wrong", Team: "Bad", Password: "Temporary-password-456"}}}
	for _, rows := range cases {
		if _, e := s.IssueTeamAccounts(token, rows, "Organizer", "Validate import", "test"); status(e) != 400 {
			t.Fatal(e)
		}
	}
	if _, e := s.IssueTeamAccounts(token, bulkRows(1), "Organizer", "x", "test"); status(e) != 400 {
		t.Fatal(e)
	}
	_, err := s.DB.Exec("CREATE TRIGGER fail_account_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'test failure'); END")
	if err != nil {
		t.Fatal(err)
	}
	if _, e := s.IssueTeamAccounts(token, bulkRows(2), "Organizer", "Failure rollback", "test"); e == nil {
		t.Fatal("expected failure")
	}
	var users, teams, audit int
	s.DB.QueryRow("SELECT count(*) FROM users").Scan(&users)
	s.DB.QueryRow("SELECT count(*) FROM teams").Scan(&teams)
	s.DB.QueryRow("SELECT count(*) FROM audit WHERE action='account_created'").Scan(&audit)
	if users != 1 || teams != 1 || audit != 0 {
		t.Fatal("transaction leaked", users, teams, audit)
	}
}
func TestBulkAccountsConcurrentDuplicateAndHTTPBoundary(t *testing.T) {
	s := testStore(t)
	token := bulkAdmin(t, s)
	rows := bulkRows(2)
	var wg sync.WaitGroup
	codes := make(chan int, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := s.IssueTeamAccounts(token, rows, "Organizer", "Concurrent import", "test")
			if err == nil {
				codes <- 200
			} else {
				codes <- status(err)
			}
		}()
	}
	wg.Wait()
	close(codes)
	counts := map[int]int{}
	for code := range codes {
		counts[code]++
	}
	if counts[200] != 1 || counts[409] != 1 {
		t.Fatal(counts)
	}
	mux := http.NewServeMux()
	s.Register(mux)
	body, _ := json.Marshal(map[string]any{"accounts": rows, "actor": "Organizer", "reason": "HTTP import"})
	if r := call(t, mux, "POST", "/api/admin/accounts/bulk", "", string(body)); r.Code != 401 {
		t.Fatal(r.Code)
	}
	fresh := account(t, s, "new-admin@example.test", "admin")
	freshToken, _, _ := s.Login(fresh.Email, "Initial-password-123", "fresh")
	if r := call(t, mux, "POST", "/api/admin/accounts/bulk", freshToken, string(body)); r.Code != 403 {
		t.Fatal(r.Code)
	}
	if r := call(t, mux, "POST", "/api/admin/accounts/bulk", token, string(body)); r.Code != 409 {
		t.Fatal(r.Code)
	}
	newRows := []AccountInput{{Email: "api@example.test", Team: "API team", Password: "HTTP-password-123456"}}
	body, _ = json.Marshal(map[string]any{"accounts": newRows, "actor": "Organizer", "reason": "HTTP import"})
	r := call(t, mux, "POST", "/api/admin/accounts/bulk", token, string(body))
	if r.Code != 200 || strings.Contains(r.Body.String(), newRows[0].Password) {
		t.Fatal("HTTP result", r.Code)
	}
}
func TestBulkAccountsMaximumImport(t *testing.T) {
	if testing.Short() {
		t.Skip("200 account password hashes")
	}
	s := testStore(t)
	token := bulkAdmin(t, s)
	r, e := s.IssueTeamAccounts(token, bulkRows(200), "Organizer", "Maximum roster import", "test")
	if e != nil || r.Count != 200 {
		t.Fatal("maximum import", r.Count, e)
	}
}
