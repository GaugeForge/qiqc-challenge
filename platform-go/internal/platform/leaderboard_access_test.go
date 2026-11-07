package platform

import (
	"net/http"
	"testing"
)

func TestLeaderboardIsVisibleToSignedInTeamsAndStaffOnly(t *testing.T) {
	s := testStore(t)
	mux := http.NewServeMux()
	s.Register(mux)
	s.RegisterBusiness(mux, http.NotFoundHandler())
	team := readyUser(t, s, "team@example.test", "team")
	staff := readyUser(t, s, "staff@example.test", "admin")
	fresh := account(t, s, "fresh@example.test", "team")
	freshToken, _, err := s.Login(fresh.Email, "Initial-password-123", "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if got := call(t, mux, "GET", "/api/leaderboard", "", ""); got.Code != 401 {
		t.Fatal("anonymous leaderboard", got.Code)
	}
	if got := call(t, mux, "GET", "/api/leaderboard", freshToken, ""); got.Code != 403 {
		t.Fatal("temporary-password leaderboard", got.Code)
	}
	for _, token := range []string{team, staff} {
		if got := call(t, mux, "GET", "/api/leaderboard", token, ""); got.Code != 200 {
			t.Fatal("signed-in leaderboard", got.Code, got.Body.String())
		}
	}
}
