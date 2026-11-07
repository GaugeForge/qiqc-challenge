package platform

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func TestAccountIssuanceIsAuthorizedAtomicAndNeverPersistsPlaintext(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "admin@example.test", "admin")
	team := readyUser(t, s, "team@example.test", "team")
	mux := http.NewServeMux()
	s.Register(mux)
	password := "One-time-invitation-123"
	payload := `{"email":"new@example.test","password":"` + password + `","team":"New team","actor":"staff","reason":"Verified team registration"}`
	if got := call(t, mux, "POST", "/api/admin/accounts", team, payload); got.Code != 403 {
		t.Fatal("team issued account", got.Code)
	}
	res := call(t, mux, "POST", "/api/admin/accounts", admin, payload)
	if res.Code != 200 {
		t.Fatal(res.Code, res.Body.String())
	}
	var user User
	if err := json.Unmarshal(res.Body.Bytes(), &user); err != nil {
		t.Fatal(err)
	}
	if user.Role != "team" || user.MustChange != 1 {
		t.Fatal("issued account contract")
	}
	if dup := call(t, mux, "POST", "/api/admin/accounts", admin, payload); dup.Code != 409 {
		t.Fatal("duplicate accepted")
	}
	var audit, outbox int
	s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action='account_created'").Scan(&audit)
	s.DB.QueryRow("SELECT COUNT(*) FROM outbox WHERE event='account_ready'").Scan(&outbox)
	if audit != 1 || outbox != 1 {
		t.Fatal("nonatomic invitation", audit, outbox)
	}
	snapshot, err := s.AdminSnapshot(admin)
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(snapshot)
	if strings.Contains(string(encoded), password) {
		t.Fatal("password leaked into snapshot")
	}
	token, _, err := s.Login(user.Email, password, "invitation-test")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.Versions(token); status(err) != 403 {
		t.Fatal("first password change bypass")
	}
}

func TestScienceRequestsRequirePinnedPackageInfraAndExplicitConfiguration(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	if _, err := s.Confirm(token, v.ID, "infra", "scientific-smoke", "sci"); status(err) != 409 {
		t.Fatal("science allowed without configuration")
	}
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	if _, err := s.Confirm(token, v.ID, "full", "scientific-smoke", "sci"); status(err) != 409 {
		t.Fatal("science incorrectly ranking")
	}
	b, err := s.Confirm(token, v.ID, "infra", "scientific-smoke", "sci")
	if err != nil {
		t.Fatal(err)
	}
	if b["task_count"] != 1 {
		t.Fatal("not a single diagnostic")
	}
	u, _ := s.Session(token)
	if u.Quotas["infra"] != 9 || u.Quotas["full"] != 1 || u.Quotas["public"] != 3 {
		t.Fatal("wrong quota charged")
	}
}

func TestUnreconciledSandboxCannotBeReleasedByRetryOrCancellation(t *testing.T) {
	s := testStore(t)
	token, v := approved(t, s)
	admin := readyUser(t, s, "organizer@example.test", "admin")
	s.ScienceHashes = map[string]bool{v.SHA256: true}
	batch, err := s.Confirm(token, v.ID, "infra", "scientific-smoke", "science")
	if err != nil {
		t.Fatal(err)
	}
	l := mustClaim(t, s, "lost-worker", 1)
	if _, err = s.DB.Exec("UPDATE runs SET state='attention',error=? WHERE id=?", lostWorkerError, l.RunID); err != nil {
		t.Fatal(err)
	}
	s.DB.Exec("UPDATE batches SET status='attention' WHERE id=?", l.BatchID)
	if err = s.AdminAction(admin, Action{Action: "retry", Target: l.BatchID, Actor: "staff", Reason: "Attempt without remote cleanup"}, "local"); status(err) != 409 {
		t.Fatal("unsafe retry", err)
	}
	if err = s.Cancel(token, batch["id"].(string)); err != nil {
		t.Fatal(err)
	}
	got, err := s.GetBatch(token, l.BatchID)
	if err != nil || got["status"] != "cancelling" {
		t.Fatal("released team before cleanup", got, err)
	}
	queueBatch(t, s, 1, "simulated")
	next, err := s.Claim("other-worker", 1)
	if err != nil || next != nil {
		t.Fatal("released unknown remote capacity", err)
	}
}
