package platform

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func workspaceRequest(h http.Handler, method, path, workspace, body string, cookies []*http.Cookie) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	r.RemoteAddr = "127.0.0.1:12345"
	r.Header.Set("X-QIQC-Request", "1")
	r.Header.Set("X-QIQC-Workspace", workspace)
	for _, c := range cookies {
		r.AddCookie(c)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}
func TestWorkspaceSessionsIndependentLoginLogoutAndLegacy(t *testing.T) {
	s := testStore(t)
	s.SecureCookies = true
	account(t, s, "staff@workspace.test", "admin")
	account(t, s, "team@workspace.test", "team")
	mux := http.NewServeMux()
	s.Register(mux)
	login := func(email, workspace string, cookies []*http.Cookie) *http.Cookie {
		t.Helper()
		b, _ := json.Marshal(map[string]string{"email": email, "password": "Initial-password-123"})
		r := workspaceRequest(mux, "POST", "/api/login", workspace, string(b), cookies)
		if r.Code != 200 || len(r.Result().Cookies()) != 1 {
			t.Fatalf("login failed: %d", r.Code)
		}
		c := r.Result().Cookies()[0]
		if !c.HttpOnly || !c.Secure || c.SameSite != http.SameSiteStrictMode || c.Path != "/" {
			t.Fatal("cookie policy")
		}
		return c
	}
	legacy := login("staff@workspace.test", "", nil)
	staff := login("staff@workspace.test", "staff", []*http.Cookie{legacy})
	if staff.Name != "qiqc_staff_session" {
		t.Fatal("staff cookie")
	}
	cookies := []*http.Cookie{legacy, staff}
	if r := workspaceRequest(mux, "GET", "/api/me", "team", "", cookies); r.Code != 401 {
		t.Fatal("team borrowed staff/legacy identity", r.Code)
	}
	team := login("team@workspace.test", "team", cookies)
	cookies = append(cookies, team)
	if team.Name != "qiqc_team_session" {
		t.Fatal("team cookie")
	}
	for workspace, role := range map[string]string{"team": "team", "staff": "admin", "": "admin"} {
		r := workspaceRequest(mux, "GET", "/api/me", workspace, "", cookies)
		var u User
		if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &u) != nil || u.Role != role {
			t.Fatal("wrong identity", workspace, r.Code)
		}
	}
	if r := workspaceRequest(mux, "POST", "/api/password", "team", `{"password":"New-participant-password-123"}`, cookies); r.Code != 200 {
		t.Fatal("password", r.Code)
	}
	if r := workspaceRequest(mux, "POST", "/api/admin/accounts/bulk", "team", `{}`, cookies); r.Code != 403 {
		t.Fatal("team elevated", r.Code)
	}
	out := workspaceRequest(mux, "POST", "/api/logout", "team", "", cookies)
	if out.Code != 200 || out.Result().Cookies()[0].Name != "qiqc_team_session" || out.Result().Cookies()[0].MaxAge != -1 {
		t.Fatal("logout cookie")
	}
	if r := workspaceRequest(mux, "GET", "/api/me", "team", "", cookies); r.Code != 401 {
		t.Fatal("team session retained")
	}
	if r := workspaceRequest(mux, "GET", "/api/me", "staff", "", cookies); r.Code != 200 {
		t.Fatal("team logout invalidated staff")
	}
	if r := workspaceRequest(mux, "GET", "/api/me", "", "", cookies); r.Code != 200 {
		t.Fatal("legacy compatibility")
	}
	team = login("staff@workspace.test", "", nil) // Valid legacy staff session still cannot fill the team slot.
	if r := workspaceRequest(mux, "GET", "/api/me", "team", "", []*http.Cookie{team}); r.Code != 401 {
		t.Fatal("legacy fallback")
	}
}
func TestWorkspaceWrongRoleAndInvalidSelector(t *testing.T) {
	s := testStore(t)
	account(t, s, "staff@wrong.test", "admin")
	account(t, s, "team@wrong.test", "team")
	mux := http.NewServeMux()
	s.Register(mux)
	for _, tc := range []struct{ email, workspace string }{{"staff@wrong.test", "team"}, {"team@wrong.test", "staff"}} {
		b, _ := json.Marshal(map[string]string{"email": tc.email, "password": "Initial-password-123"})
		r := workspaceRequest(mux, "POST", "/api/login", tc.workspace, string(b), nil)
		if r.Code != 403 || len(r.Result().Cookies()) != 0 {
			t.Fatal("wrong role logged in", r.Code)
		}
	}
	var n int
	if e := s.DB.QueryRow("select count(*) from sessions").Scan(&n); e != nil || n != 0 {
		t.Fatal("rejected login left sessions", n, e)
	}
	if r := workspaceRequest(mux, "GET", "/api/me", "wrong", "", nil); r.Code != 400 {
		t.Fatal("invalid selector accepted")
	}
}
